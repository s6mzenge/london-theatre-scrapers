"""
LOVEtheatre.com scraper (pure requests, parallel)
=================================================

Scrapes show listings from four URLs on LOVEtheatre and fetches each
show's detail page for the rich performance, weekly schedule,
description, FAQ, venue and booking data.

The four listing URLs the user provided:

  * /whats-on/                                  — master "All Shows" catalogue
  * /special-offers/                            — all shows currently on offer
  * /special-offers/last-minute-theatre-tickets/ — last-minute filter slice
  * /special-offers/tickets-under-20/           — sub-£20 filter slice

Of those, `whats-on` is the natural master (full catalogue, "All Shows
(165)" header in the SSR HTML). The other three are filter slices drawn
from the same pool — they almost always overlap with whats-on but
occasionally a special-offer show won't be in whats-on if the editorial
team has fast-tracked it. We treat all four as listing sources, take
their union as the master, and record an `appears_in` field per show so
consumers can tell which slices a show belongs to.

Why no Playwright?
------------------
LOVEtheatre is fully SSR. Listing pages render every card in one
response (no lazy loading, no pagination — the "SHOW MORE" button on
last-minute is AJAX-driven but the initial HTML already carries the full
list). Show detail pages SSR everything too, including:

  * An array of schema.org TheaterEvent JSON-LD blocks (one per
    upcoming performance) carrying full ISO datetimes, per-performance
    booking URLs with #perf=<id> fragments, prices, and venue address.
  * A schema.org Product JSON-LD block with description, image, base
    Offer price/currency/availability.
  * A FAQPage JSON-LD block (mirrored in the visible accordion).
  * Visible widgets: a hero image, a "Next availability" card list,
    a Booking Period date range, a Weekly Performances times block,
    a venue panel with address, a Groups & Schools section, a
    breadcrumb that names the category, and a duration/category badge
    in the show header.

The booking flow itself (secure.lovetheatre.com/book/...) is an Angular
SPA (Ingresso Whitelabel) — we don't scrape that. The detail page
provides everything we need.

Identity & primary key
----------------------
Each show carries TWO IDs:
  * `data-showid` — alphanumeric Ingresso ID (e.g. "1GVLR", "1HN2A",
    "1HX4N"). This is the stable primary key used by the booking flow.
  * `data-postid` — numeric WordPress post ID (e.g. 249290). Useful
    as a secondary key but tied to the CMS, not the ticketing system.

We key shows by `show_id` (the alphanumeric Ingresso ID) because the
booking URLs use it (`/book/1GVLR-disney-s-hercules/`) and it survives
WordPress migrations / post-ID resets. The numeric `post_id` is recorded
alongside but not used for deduplication.

Setup
-----
    pip install requests beautifulsoup4

Usage
-----
    python lovetheatre_scraper.py                       # full scrape
    python lovetheatre_scraper.py --limit 5             # test with 5 shows
    python lovetheatre_scraper.py --out data/lt.json    # custom output path
    python lovetheatre_scraper.py --concurrency 12      # more parallel workers
    python lovetheatre_scraper.py --no-tag-lists        # only whats-on (skip offer slices)
    python lovetheatre_scraper.py --dry-run             # don't write

Output is a single JSON file:

    {
      "scraped_at": "2026-05-19T08:30:00+00:00",
      "source": "https://www.lovetheatre.com/",
      "show_count": 165,
      "performance_count": 1180,
      "report": { ... warnings, failures ... },
      "shows": [ { ... } ... ]
    }
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = "https://www.lovetheatre.com"

# The four listing URLs the user provided. Order here doubles as the
# preferred "primary list" ordering — whats-on first because it's the
# master catalogue, then the three offer slices. When we deduplicate
# across the four lists, the first list a show appears in determines
# its position in the master.
LISTING_URLS: dict[str, str] = {
    "whats_on":         f"{BASE}/whats-on/",
    "special_offers":   f"{BASE}/special-offers/",
    "last_minute":      f"{BASE}/special-offers/last-minute-theatre-tickets/",
    "tickets_under_20": f"{BASE}/special-offers/tickets-under-20/",
}

# The single URL we cite as the canonical source in the output JSON.
# Using the site root rather than any single listing because the
# catalogue is a union of four pages.
OUTPUT_SOURCE = f"{BASE}/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# LOVEtheatre is a WordPress site fronted by Cloudflare; under parallel
# load it comfortably handles 12 workers without timing out. We default
# to 8 to leave some headroom for slower networks / VPN setups, but it
# can be pushed higher via --concurrency.
DEFAULT_CONCURRENCY = 8

# Per-request HTTP timeout. WordPress pages can be slow when uncached;
# 45s is enough headroom for cold cache hits without leaving zombie
# requests.
REQUEST_TIMEOUT_S = 45

# urllib3-level retries (network errors, 5xx, 429). Backoff = 0.8 keeps
# total worst-case retry time under ~6s per request.
RETRY_TOTAL = 3
RETRY_BACKOFF = 0.8

# Application-level retry for parse errors (mid-deploy partial HTML).
# One retry only — anything flakier is genuinely broken.
DETAIL_PARSE_RETRY_DELAY_S = 2.0

# Output file rotation depth — keeps lovetheatre.json, .1, .2, ..., .5
DEFAULT_ROTATION_DEPTH = 5

# Exit codes — 0 clean, 1 hard fail (no output), 2 wrote with warnings.
EXIT_CLEAN = 0
EXIT_HARD_FAIL = 1
EXIT_WARNINGS = 2

# Show detail URLs look like /shows/{slug}/ where the slug typically
# ends in "-tickets" (e.g. "1536-tickets", "disneys-hercules-the-musical-tickets",
# "360-allstars-tickets"). The slug character class is permissive — we
# accept anything except "/" or "#".
SHOW_URL_RE = re.compile(
    r"^https?://www\.lovetheatre\.com/shows/([^/#?]+?)/?$",
    re.IGNORECASE,
)

# Booking URLs from the per-performance TheaterEvent JSON-LD look like
#   https://secure.lovetheatre.com/book/{showid}-{slug}/#perf={showid}-{perf}
# where {showid} matches the listing's data-showid (alphanumeric, e.g.
# "1GVLR") and {perf} is a short alphanumeric suffix (e.g. "10Z", "110").
# We extract the per-performance fragment to expose a stable per-perf ID.
BOOK_URL_PERF_RE = re.compile(
    r"#perf=([A-Z0-9]+-[A-Z0-9]+)$",
    re.IGNORECASE,
)

# CSS-class genre extraction — one of these per card, e.g. `genre-musical`,
# `genre-play`, `genre-event`, `genre-dance-and-opera`.
GENRE_CLASS_RE = re.compile(r"^genre-([a-z0-9-]+)$")

# Tag classes — multiple per card, e.g. `tag-critically-acclaimed`,
# `tag-hot-tickets`, `tag-musicals`, etc.
TAG_CLASS_RE = re.compile(r"^tag-([a-z0-9-]+)$")

# CSS background-image URL extractor for the hero image.
BG_IMAGE_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")

# Price strings — "£29.50", "£94.00", "From £18".
PRICE_RE = re.compile(r"£\s*(\d+(?:[.,]\d+)?)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lovetheatre")

# Silence urllib3's "Retrying (...)" chatter. urllib3 logs every retry at
# WARNING level, which looks alarming but is just library bookkeeping —
# the final ScrapeReport (with succeeded/failed counts and warnings) is
# the source of truth.
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ListingCard:
    """A card as it appears on any of the four listing pages."""
    show_id: str                   # data-showid (alphanumeric, e.g. "1HN2A")
    post_id: int | None            # data-postid (numeric WordPress ID)
    name: str
    url: str                       # canonical detail URL
    slug: str                      # path slug, e.g. "1536-tickets"
    image: str | None              # 230x355 thumbnail
    venue_text: str | None         # visible venue name on the card
    # The card carries two prices: an integer (data-price, rounded for
    # filter UI) and a decimal (data-original-price, the real "From" price).
    listing_price_value: float | None        # data-price (rounded for filters)
    listing_original_price: float | None     # data-original-price (real "From" price)
    listing_price_display: str | None        # visible .price text, e.g. "£94.00"
    # Editorial flags
    has_special_offer: bool        # "with-special-offer" CSS class present
    offer_label: str | None        # .custom-label text, e.g. "Save up to 48%"
    # Taxonomy
    genre: str | None              # from `genre-*` class, e.g. "musical"
    tags: list[str]                # from `tag-*` classes
    # Raw data-date attribute (not always a recognisable date — sometimes
    # an internal sort key). Recorded as-is for debugging.
    data_date: str | None


@dataclass
class Performance:
    """A single upcoming performance, sourced from the show detail page's
    TheaterEvent JSON-LD blocks. There's also a visible "Next availability"
    block in the HTML that we extract separately into `next_availability`;
    the two usually overlap but the JSON-LD goes further into the future."""
    perf_id: str | None          # from "#perf=" fragment, e.g. "1GVLR-10Z"
    iso: str | None              # full ISO datetime e.g. "2026-05-19T19:30:00+01:00"
    date: str | None             # "YYYY-MM-DD" derived from startDate
    time: str | None             # "HH:MM" derived from startDate
    end_date: str | None         # raw endDate from JSON-LD (often just date)
    status: str | None           # eventStatus, e.g. ".../EventScheduled"
    price: float | None          # offers.price as float
    currency: str | None         # offers.priceCurrency, e.g. "GBP"
    availability: str | None     # offers.availability, e.g. ".../InStock"
    valid_from: str | None       # offers.validFrom (often "DD-MM-YYYY")
    book_url: str | None         # offers.url is the canonical show page; the
                                 # per-performance booking URL is the event
                                 # `url` field with #perf=... — that's what
                                 # we record here.
    offer_url: str | None        # offers.url (canonical show URL)
    venue_name: str | None       # location.name (one per perf, but usually constant)


@dataclass
class FaqEntry:
    question: str | None
    answer: str | None


@dataclass
class WeeklyPerformanceRow:
    """One row of the sidebar Weekly Performances widget.

    The widget renders as a free-form `<p>` blob:
        Monday    7:30PM<br>
        Thursday  2:30PM, 7:30PM<br>
    We split it back into structured rows. `times` is a list because
    matinee+evening days have two entries on one line."""
    day: str            # "Monday" .. "Sunday"
    times: list[str]    # e.g. ["7:30PM"] or ["2:30PM", "7:30PM"]


@dataclass
class NextAvailabilityEntry:
    """One card from the visible "Next availability" tab on the detail
    page. Each card is a date and one or two booking links."""
    date_label: str               # "Tuesday 19th May" (joined from <br>)
    time: str                     # "7.30 PM"
    book_url: str | None          # the #perf=... link (rich form)


@dataclass
class Show:
    """Aggregate of listing card + show detail page."""
    # Identity
    show_id: str
    post_id: int | None
    name: str
    url: str                                  # canonical detail URL
    slug: str
    # From listing card
    image: str | None
    venue_text: str | None
    listing_price_value: float | None
    listing_original_price: float | None
    listing_price_display: str | None
    has_special_offer: bool
    offer_label: str | None
    genre: str | None
    tags: list[str]
    data_date: str | None
    # Filter membership across the four listing URLs
    appears_in: list[str]

    # ---- Detail page fields below ----
    detail_canonical: str | None              # <link rel="canonical"> on detail page
    detail_name: str | None                   # H1 text (should match listing name)
    breadcrumb_category: str | None           # e.g. "Musicals" / "Plays" / "Off West End"
    badge_category: str | None                # visible header badge, e.g. "musical"
    # Product JSON-LD
    product_description: str | None           # short product description (Product.description)
    product_image: str | None                 # main product image (Product.image[0])
    detail_low_price: float | None            # Product.offers.price
    detail_currency: str | None               # Product.offers.priceCurrency
    detail_availability: str | None           # Product.offers.availability
    detail_offer_url: str | None              # Product.offers.url
    # Visible detail-page widgets
    description_full: str | None              # "always-show" + extended body, joined
    hero_image: str | None                    # full-width hero background image
    show_thumbnail: str | None                # small image inside show-info header
    duration_text: str | None                 # "2hrs 10mins (incl. interval, ...)"
    venue_name: str | None                    # from venue link in header
    venue_url: str | None                     # /venue/<slug>/
    venue_address: str | None                 # visible venue panel address (one line)
    sidebar_price_value: float | None         # "Tickets From £X" sidebar
    sidebar_price_display: str | None         # "£29.50" sidebar display
    book_tickets_url: str | None              # main "Book Tickets" button URL
    calendar_id: str | None                   # data-id on #calendar (== show_id usually)
    calendar_start_date: str | None           # data-start on #calendar (ISO date)
    booking_period: str | None                # "6 Jun 2025 - 5 Sep 2026"
    weekly_performances: list[dict]           # WeeklyPerformanceRow entries
    next_availability: list[dict]             # NextAvailabilityEntry entries
    group_info: str | None                    # Groups & Schools paragraph text
    # JSON-LD-derived collections
    performances: list[dict]                  # Performance entries (from TheaterEvent[])
    faq: list[dict]                           # FaqEntry entries (from FAQPage)


@dataclass
class ShowFailure:
    """A show in the listings that we couldn't fetch or parse a detail
    page for. Recorded so consumers see what's missing and why."""
    show_id: str
    slug: str
    url: str
    error: str


@dataclass
class ScrapeReport:
    """Embedded in the output JSON under "report" so downstream consumers
    can detect partial / degraded runs without parsing log files."""
    master_show_count: int
    succeeded_show_count: int
    failed_show_count: int
    listings_scraped: list[str]
    listing_counts: dict[str, int]
    budget_exceeded: bool = False
    warnings: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    failures: list[ShowFailure] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

def build_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=retry,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _fetch_html(session: requests.Session, url: str) -> requests.Response:
    """GET with the response forced to UTF-8 decoding.

    LOVEtheatre serves UTF-8 HTML but the response headers don't declare a
    charset, so `requests` falls back to ISO-8859-1 per RFC 2616. That makes
    `r.text` produce mojibake on every string containing a smart quote,
    em-dash, pound sign, NBSP, star symbol, accented vowel, or any other
    non-ASCII character — turning U+2019 'right single quote' (UTF-8 bytes
    \\xe2\\x80\\x99) into the three Latin-1 characters 'â\\x80\\x99'.

    Fixed by forcing `r.encoding = 'utf-8'` BEFORE reading `r.text`. This
    is a one-line correction with no downside: every page on lovetheatre.com
    is genuinely UTF-8 (verified across all 165 detail pages) and the
    forced setting overrides only when the header is silent.
    """
    r = session.get(url, timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode(s: str | None) -> str | None:
    """HTML-entity-decode and strip a string. Handles the double-encoded
    entities (e.g. `&amp;#8217;`) that appear in some JSON-LD blocks by
    decoding twice."""
    if s is None:
        return None
    once = html.unescape(s)
    twice = html.unescape(once) if "&amp;" in once or "&#" in once else once
    return twice.strip() or None


def _clean_perf_venue_name(raw: str | None) -> str | None:
    """Tidy a TheaterEvent.location.name string into a bare venue name.

    LOVEtheatre's JSON-LD `location.name` is occasionally a multi-line
    address blob (the venue name on line 1, followed by street/postcode):

        "Belle Livingstone's 58th Street Country Club\\r\\nBussey Alley
         \\r\\nBtwn 133 Rye Road & Copeland Road"

    Other times it's the venue name with an area suffix, like

        "The Lost Estate, West Kensington"

    Both forms make dedupe miss the corresponding cluster from other
    sources (which list the bare venue). We extract just the first line
    and strip any ", <area>" tail. Conservative — only the FIRST comma is
    treated as the area-suffix boundary, preserving venues whose canonical
    name genuinely contains a comma (none observed in our data, but
    defensive).
    """
    if not raw:
        return None
    # Normalize line endings, take the first line
    first_line = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0].strip()
    if not first_line:
        return None
    # Strip ", <area>" suffix (area names are typically short, single-comma
    # separated; never the full address)
    name, _, _tail = first_line.partition(",")
    return name.strip() or None


def _to_float(s) -> float | None:
    """Coerce to float; treat empty string and None as None."""
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _to_int(s) -> int | None:
    """Coerce to int; treat empty string and None as None."""
    if s in (None, ""):
        return None
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return None


def _slug_from_url(url: str) -> str:
    """Extract the slug from a show URL.

    /shows/disneys-hercules-the-musical-tickets/  →  disneys-hercules-the-musical-tickets
    """
    m = SHOW_URL_RE.match(url.split("#", 1)[0].split("?", 1)[0])
    return m.group(1) if m else ""


def _is_placeholder_image(src: str | None) -> bool:
    """LOVEtheatre lazy-loads images with a 1x1 SVG data URI as the
    initial `src`. The real image URL is in `data-lazy-src`. We skip
    placeholders to avoid storing useless data: URIs."""
    if not src:
        return True
    return src.startswith("data:image/svg+xml")


def _best_image_src(img_tag) -> str | None:
    """Pick the real image URL out of an <img> tag, handling the lazy-load
    pattern (data-lazy-src is the real URL, src is a placeholder)."""
    if img_tag is None:
        return None
    # Preference order: data-lazy-src, then real src if not placeholder, then
    # the <noscript><img src="..."></noscript> fallback if we have it.
    lazy = img_tag.get("data-lazy-src")
    if lazy and not _is_placeholder_image(lazy):
        return lazy.strip()
    src = img_tag.get("src")
    if src and not _is_placeholder_image(src):
        return src.strip()
    return None


def _extract_perf_id(url: str | None) -> str | None:
    """Pull the per-performance ID from a TheaterEvent.url like
    https://secure.lovetheatre.com/book/1GVLR-disney-s-hercules/#perf=1GVLR-10Z
    → "1GVLR-10Z"."""
    if not url:
        return None
    m = BOOK_URL_PERF_RE.search(url)
    return m.group(1) if m else None


def _split_iso_datetime(iso: str | None) -> tuple[str | None, str | None]:
    """Split an ISO 8601 datetime into (date, time) — both as strings.

    "2026-05-19T19:30:00+01:00" → ("2026-05-19", "19:30")

    We don't use datetime.fromisoformat because Python <3.11 chokes on
    timezone offsets like "+01:00" in some forms, and we don't need a
    real datetime — just the two display strings."""
    if not iso:
        return None, None
    s = iso.strip()
    if "T" not in s:
        # endDate-only fields come through as plain "YYYY-MM-DD"
        return (s, None) if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else (None, None)
    date_part, _, time_part = s.partition("T")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_part):
        return None, None
    # Trim seconds and timezone for the time string
    t = time_part.split("+", 1)[0].split("-", 1)[0].split("Z", 1)[0]
    if len(t) >= 5 and t[2] == ":":
        return date_part, t[:5]
    return date_part, None


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

def _extract_jsonld_blocks(scope) -> list:
    """Parse every <script type='application/ld+json'> block under
    `scope`. Each block can be a dict OR a list of dicts (LOVEtheatre's
    TheaterEvent block is the latter — one JSON array with all
    performances inside).

    Returns a flat list where nested lists have been unwrapped (so
    consumers can iterate uniformly).

    Tolerates broken blocks: if one fails to parse, the others remain
    usable. Uses strict=False to accept literal newlines inside
    description strings (LOVEtheatre's TheaterEvent descriptions are
    multi-paragraph WordPress content with raw `\\r\\n` bytes)."""
    out: list = []
    for s in scope.select("script[type='application/ld+json']"):
        if not s.string:
            continue
        try:
            parsed = json.loads(s.string, strict=False)
        except json.JSONDecodeError:
            log.debug("skipping malformed JSON-LD block")
            continue
        # Unwrap top-level arrays so callers can scan a flat list.
        if isinstance(parsed, list):
            out.extend(parsed)
        else:
            out.append(parsed)
    return out


def _find_product_node(blocks: list) -> dict | None:
    """Find the schema.org Product JSON-LD block on a show detail page."""
    for b in blocks:
        if isinstance(b, dict) and b.get("@type") == "Product":
            return b
    return None


def _find_theater_events(blocks: list) -> list[dict]:
    """Return all TheaterEvent JSON-LD blocks in document order. The
    detail page emits one TheaterEvent per upcoming performance, all
    inside a single <script> as a JSON array, so they come through
    pre-flattened from _extract_jsonld_blocks."""
    return [b for b in blocks
            if isinstance(b, dict) and b.get("@type") == "TheaterEvent"]


def _find_faq_entries(blocks: list) -> list[dict]:
    """Extract FAQ entries from any FAQPage JSON-LD block on the page.

    The FAQ block mirrors the visible accordion 1:1 (we verified all 4
    accordion entries appear in the JSON-LD too), so reading the
    JSON-LD is preferable to walking the DOM — cleaner and less brittle."""
    for b in blocks:
        if not isinstance(b, dict) or b.get("@type") != "FAQPage":
            continue
        out: list[dict] = []
        for q in b.get("mainEntity") or []:
            if not isinstance(q, dict):
                continue
            answer = (q.get("acceptedAnswer") or {})
            if not isinstance(answer, dict):
                answer = {}
            out.append(asdict(FaqEntry(
                question=_decode(q.get("name")),
                answer=_decode(answer.get("text")),
            )))
        return out
    return []


def _extract_performances(blocks: list) -> list[Performance]:
    """Convert every TheaterEvent JSON-LD block into a Performance.

    The per-performance booking URL is the event's `url` field (which
    carries the #perf=<id> fragment). The Offer.url is the canonical
    show URL on lovetheatre.com — same for every perf — so we record it
    once as `offer_url` and use the event url as the actual `book_url`."""
    events = _find_theater_events(blocks)
    out: list[Performance] = []
    for ev in events:
        offers = ev.get("offers")
        if not isinstance(offers, dict):
            offers = {}
        loc = ev.get("location")
        if not isinstance(loc, dict):
            loc = {}
        iso = _decode(ev.get("startDate"))
        date, time_str = _split_iso_datetime(iso)
        book_url = _decode(ev.get("url"))
        out.append(Performance(
            perf_id=_extract_perf_id(book_url),
            iso=iso,
            date=date,
            time=time_str,
            end_date=_decode(ev.get("endDate")),
            status=_decode(ev.get("eventStatus")),
            price=_to_float(offers.get("price")),
            currency=_decode(offers.get("priceCurrency")),
            availability=_decode(offers.get("availability")),
            valid_from=_decode(offers.get("validFrom")),
            book_url=book_url,
            offer_url=_decode(offers.get("url")),
            venue_name=_decode(loc.get("name")),
        ))
    return out


# ---------------------------------------------------------------------------
# Listing parser (used for all four URLs)
# ---------------------------------------------------------------------------

def _parse_one_listing_card(card_div) -> ListingCard | None:
    """Build a ListingCard from one .post div.

    Returns None if the card lacks a parseable show URL or post_id
    (which would be a parser failure to flag and skip).

    Note on ID handling. Each card carries two IDs and they don't behave
    quite the same way:
      * `data-postid` — numeric WordPress post ID, ALWAYS present and
        ALWAYS unique per show. This is our deduplication key.
      * `data-showid` — alphanumeric Ingresso booking ID (e.g. "1GVLR").
        Present on ~90% of cards; empty for shows not yet linked to the
        Ingresso ticketing system ("coming soon" placeholders). On rare
        occasions two distinct WordPress posts share the same Ingresso
        showid (a CMS data-entry error — observed once in the wild).
    """
    post_id = _to_int(card_div.get("data-postid"))
    if post_id is None:
        # No post_id means this isn't a valid show card — could be a
        # promo tile or other layout element with a `.post` class.
        return None

    # show_id may be missing (placeholder shows) — keep as empty string
    # rather than None so the field type stays str across all records.
    show_id = (card_div.get("data-showid") or "").strip()

    # Title anchor — there are several <a> tags pointing at the show
    # URL on each card (image link, h3 link, footer link). We use the
    # h3 link as the canonical source because it carries the visible
    # title text that we want for `name`.
    h3 = card_div.select_one("h3")
    if h3 is None:
        return None
    title_a = h3.select_one("a")
    if title_a is None or not title_a.has_attr("href"):
        return None
    url = title_a["href"].strip()
    name = _decode(title_a.get_text(strip=True)) or ""
    slug = _slug_from_url(url)
    if not slug:
        # If the URL doesn't match /shows/<slug>/ we can't trust the
        # card — but we record it as a failure rather than silently
        # dropping. Return None and let the count diff catch it.
        log.debug("skipping card with unparseable URL: %s", url)
        return None

    # Venue name — the visible <p class="text-uppercase text-bold m-0">
    venue_el = card_div.select_one("div.top p.text-uppercase")
    venue_text = _decode(venue_el.get_text(strip=True)) if venue_el else None

    # Image — prefer data-lazy-src (the real URL) over src (placeholder).
    img_tag = card_div.select_one("figure.folio-img img")
    image = _best_image_src(img_tag)

    # Prices: data-price is the integer (used for filter UI), and
    # data-original-price is the decimal "From" value.
    listing_price_value = _to_float(card_div.get("data-price"))
    listing_original_price = _to_float(card_div.get("data-original-price"))

    # Visible price display — e.g. "£94.00" inside the footer
    price_el = card_div.select_one("footer .price")
    listing_price_display = (_decode(price_el.get_text(strip=True))
                             if price_el else None)

    # Special offer flags
    classes = card_div.get("class") or []
    has_special_offer = "with-special-offer" in classes
    label_el = card_div.select_one(".custom-label")
    offer_label = _decode(label_el.get_text(strip=True)) if label_el else None

    # Genre + tags from CSS classes
    genre: str | None = None
    tags: list[str] = []
    for cls in classes:
        g = GENRE_CLASS_RE.match(cls)
        if g:
            genre = g.group(1)
            continue
        t = TAG_CLASS_RE.match(cls)
        if t:
            tags.append(t.group(1))

    # Raw data-date string (kept as-is — the format varies and isn't
    # consistently parseable as a date)
    data_date = (card_div.get("data-date") or "").strip() or None

    return ListingCard(
        show_id=show_id,
        post_id=post_id,
        name=name,
        url=url,
        slug=slug,
        image=image,
        venue_text=venue_text,
        listing_price_value=listing_price_value,
        listing_original_price=listing_original_price,
        listing_price_display=listing_price_display,
        has_special_offer=has_special_offer,
        offer_label=offer_label,
        genre=genre,
        tags=tags,
        data_date=data_date,
    )


def parse_listing(html_text: str) -> list[ListingCard]:
    """Parse any of the four listing pages.

    All four share the same card markup:
        <div class="post col-xl-2 ... genre-X tag-Y ..."
             data-showid="..." data-postid="..." ...>
          <div class="folio-card">
            <div class="top">...</div>
            <footer>...</footer>
          </div>
        </div>

    The cards are wrapped in either `.post-listing` (whats-on) or a
    `.row.post-listing` (the offer slices) — we look for `.post` divs
    inside the main content area to cover both.

    Duplicates are deduplicated by `show_id` (keep first occurrence).
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # Scope to the main content area to avoid stray .post cards in
    # related-shows widgets etc. (Defensive — current LOVEtheatre layout
    # doesn't have related-show grids on listing pages, but the
    # detail page does, and we want this parser robust to mixed-shape
    # responses if the layout changes.)
    main = soup.select_one("main") or soup
    # The card wrapper is the outer `.post` div carrying the data attrs;
    # the `.folio-card` is a child div with no data attrs. We select on
    # the outer `[data-postid]` because post_id is always present (unlike
    # data-showid which is empty on some "coming soon" placeholders).
    cards_html = main.select("div.post[data-postid]")

    out: list[ListingCard] = []
    seen_post_ids: set[int] = set()
    for card_div in cards_html:
        card = _parse_one_listing_card(card_div)
        if card is None:
            continue
        # Dedup by post_id (always present and always unique). We
        # deliberately don't dedup by show_id — see the docstring on
        # _parse_one_listing_card for why (two distinct shows can share
        # one Ingresso showid due to a CMS quirk).
        if card.post_id in seen_post_ids:
            continue
        seen_post_ids.add(card.post_id)
        out.append(card)
    return out


# ---------------------------------------------------------------------------
# Show detail parser — visible HTML widgets
# ---------------------------------------------------------------------------

def _extract_canonical(soup: BeautifulSoup) -> str | None:
    """The <link rel="canonical"> on the show detail page. Lets
    consumers verify the URL we used matches the site's own canonical
    (catches edge cases where the listing's href has a trailing slash
    or trailing query string that the site would normalise)."""
    link = soup.select_one('link[rel="canonical"]')
    return _decode(link.get("href")) if link and link.has_attr("href") else None


def _extract_h1_name(soup: BeautifulSoup) -> str | None:
    """The main page <h1> — should match the listing card name but is
    occasionally richer on the detail page (e.g. include the subtitle)."""
    h1 = soup.select_one("header h1") or soup.select_one("h1")
    if h1 is None:
        return None
    return _decode(h1.get_text(strip=True))


def _extract_breadcrumb_category(soup: BeautifulSoup) -> str | None:
    """Pull the editorial category from the breadcrumb trail.

    The trail looks like:
        Home / What's On / Musicals / Disney's Hercules the Musical
    We take the second-from-last link text ("Musicals"). The last entry
    is the show name itself (a <span>, not a link), so we filter to
    anchors only and take the last anchor that isn't "Home" or the
    listing's filter slice."""
    bc = soup.select_one("section.breadcrumb")
    if bc is None:
        return None
    links = [a for a in bc.select("a") if a.get_text(strip=True)]
    if not links:
        return None
    # Skip "Home" and "What's On" — we want the category, which is the
    # last anchor in the trail.
    skip = {"home", "what's on", "what\u2019s on"}
    candidates = [_decode(a.get_text(strip=True))
                  for a in links
                  if a.get_text(strip=True).lower() not in skip]
    return candidates[-1] if candidates else None


def _extract_show_header_meta(soup: BeautifulSoup) -> dict:
    """Pull the basic header metadata: hero image, thumbnail, duration,
    venue name+url, badge category."""
    out: dict = {
        "hero_image": None,
        "show_thumbnail": None,
        "duration_text": None,
        "venue_name": None,
        "venue_url": None,
        "badge_category": None,
    }

    # Hero image — inline style="background-image: url('...')" on
    # .placeholder-bg inside figure.show-hero
    hero = soup.select_one("figure.show-hero .placeholder-bg")
    if hero and hero.has_attr("style"):
        m = BG_IMAGE_RE.search(hero["style"])
        if m:
            out["hero_image"] = _decode(m.group(1))
    if out["hero_image"] is None:
        # Fallback: the mobile-img inside the hero might carry a
        # data-lazy-src that's the same image at a smaller resolution.
        mobile_img = soup.select_one("figure.show-hero img.mobile-img")
        if mobile_img is not None:
            out["hero_image"] = _best_image_src(mobile_img)

    # Show thumbnail — the smaller image inside the show-info column
    info = soup.select_one(".show-info")
    if info is not None:
        # The thumbnail is the <img> sibling preceding .show-info inside
        # the row; we look for the first non-hero <img> inside the
        # figcaption.
        figcap = soup.select_one("figure.show-hero figcaption")
        if figcap is not None:
            for img in figcap.select("img"):
                if "mobile-img" in (img.get("class") or []):
                    continue
                cand = _best_image_src(img)
                if cand:
                    out["show_thumbnail"] = cand
                    break

    # Duration — visible inside li.duration; the icon SVG is also inside
    # it, so we get_text and collapse whitespace to drop the SVG noise.
    dur_li = soup.select_one(".show-hero-info li.duration")
    if dur_li is not None:
        raw = dur_li.get_text(" ", strip=False)
        text = re.sub(r"\s+", " ", raw).strip()
        out["duration_text"] = _decode(text) or None

    # Venue link inside the header — wraps li.location
    venue_a = soup.select_one(".show-hero-info a[href*='/venue/']")
    if venue_a is not None:
        out["venue_url"] = _decode(venue_a.get("href"))
        # The visible venue name is inside the li.location
        loc_li = venue_a.select_one("li.location")
        if loc_li is not None:
            raw = loc_li.get_text(" ", strip=False)
            text = re.sub(r"\s+", " ", raw).strip()
            out["venue_name"] = _decode(text) or None
        else:
            out["venue_name"] = _decode(venue_a.get_text(" ", strip=True))

    # Category badge — the pink "musical"/"play" tag in the header
    badge = soup.select_one(".show-info .badge-secondary")
    if badge is not None:
        out["badge_category"] = _decode(badge.get_text(strip=True))

    return out


def _extract_description_full(soup: BeautifulSoup) -> str | None:
    """The full visible synopsis body — joined paragraphs and headings
    from the .content-block.show-more block, before the "Read More"
    button.

    The block contains a `.always-show` div (visible by default) and
    then additional <h3>/<p> elements that are revealed by the "Read
    More" toggle. We collect them in document order — the resulting
    text mirrors what a user sees with the show expanded.

    Internal whitespace handling: <p> tags here often wrap individual
    words/phrases in <a> or <strong> for styling, and the naive
    get_text(" ", strip=True) puts a space between every child node —
    yielding "The musical , inspired" (space before the comma). We use
    get_text("", strip=False) and then collapse runs of whitespace
    afterwards to get natural text."""
    block = soup.select_one(".content-block.show-more.text")
    if block is None:
        return None

    seen_texts: list[str] = []

    def _push_el(el):
        if el.name not in {"p", "h3", "h4", "h5"}:
            return
        # Use empty-string separator so inline <a>/<strong> children
        # don't artificially split words. Then collapse whitespace.
        raw = el.get_text("", strip=False)
        if not raw:
            return
        text = re.sub(r"\s+", " ", raw).strip()
        text = _decode(text) or text
        if not text or text.lower() == "read more":
            return
        seen_texts.append(text)

    # First: paragraphs inside the always-visible block
    always = block.select_one(".always-show")
    if always is not None:
        for child in always.find_all(["p", "h3", "h4", "h5"], recursive=False):
            _push_el(child)

    # Then: direct children of `block` that come after always-show,
    # stopping at the Read More button. Some shows wrap extended text
    # in nested divs — we descend into non-paragraph wrappers to find
    # leaf <p>/<h3>/<h4>/<h5> nodes.
    for child in block.find_all(recursive=False):
        if child is always:
            continue
        if child.name == "button":
            break
        if child.name in {"p", "h3", "h4", "h5"}:
            _push_el(child)
            continue
        for sub in child.find_all(["p", "h3", "h4", "h5"]):
            _push_el(sub)

    return "\n\n".join(seen_texts) if seen_texts else None


def _extract_next_availability(soup: BeautifulSoup) -> list[dict]:
    """Parse the visible "Next availability" tab — a list of cards each
    showing a date and one or two booking links (matinee + evening).

    Each card layout:
        <div class="card d-flex justify-content-between p-1 p-xl-2 mb-1">
          <h3>Thursday<br>21st May</h3>          ← the date label
          <div>
            <a href="...#perf=...-111">
              <h3>2.30 PM <svg/></h3>
            </a>
          </div>
          <div>
            <a href="...#perf=...-112">
              <h3>7.30 PM <svg/></h3>
            </a>
          </div>
        </div>

    A day with one performance has one inner <div><a>; a day with
    matinee+evening has two. We emit one NextAvailabilityEntry per
    performance link (not per card), so consumers can index directly
    by performance."""
    section = soup.select_one("#nav-availability")
    if section is None:
        return []
    out: list[dict] = []
    for card in section.select("div.card"):
        # The first h3 in the card is the date label, e.g.
        #   <h3>Thursday<br>21st May</h3>
        # We separate by <br> to get "Thursday 21st May".
        date_h3 = card.select_one("h3")
        if date_h3 is None:
            continue
        # Join the lines with a single space
        date_label = " ".join(
            line.strip()
            for line in date_h3.get_text("\n", strip=True).splitlines()
            if line.strip()
        )
        date_label = _decode(date_label) or date_label

        # Each subsequent <a> in the card is a performance link
        for a in card.select("a[href]"):
            href = a["href"].strip()
            time_h3 = a.select_one("h3")
            if time_h3 is None:
                continue
            time_text = _decode(time_h3.get_text(" ", strip=True))
            if not time_text:
                continue
            out.append(asdict(NextAvailabilityEntry(
                date_label=date_label,
                time=time_text,
                book_url=href,
            )))
    return out


def _extract_weekly_performances(soup: BeautifulSoup) -> list[dict]:
    """Parse the sidebar Weekly Performances widget.

    The widget renders as a free-form <p> blob inside
    #weekly-performance-info, where each "row" is actually two text
    nodes — the day name and the times — on separate visible lines:

        Monday
            7:30PM<br>
        Tuesday
            7:30PM<br>
        Thursday
            2:30PM, 7:30PM<br>
        ...

    BeautifulSoup's get_text("\n") preserves the line breaks as \\n,
    but the heredoc-style template formatting interleaves them with
    blank/whitespace-only lines and tabs. We strip and pair adjacent
    non-blank lines: a day-name line followed by a times line.

    Days with no performances are simply absent from the source — we
    don't synthesise placeholder rows."""
    section = soup.select_one("#weekly-performance-info")
    if section is None:
        return []
    p = section.select_one("p")
    if p is None:
        return []
    raw = p.get_text("\n", strip=False)
    if not raw:
        return []

    days = {"Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"}

    # Collapse to non-blank lines, preserving order
    cleaned: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if s:
            cleaned.append(s)

    out: list[dict] = []
    i = 0
    while i < len(cleaned):
        if cleaned[i] in days and i + 1 < len(cleaned):
            day = cleaned[i]
            times_line = cleaned[i + 1]
            # Skip if next line is also a day name (defensive — would
            # mean the day has no times printed for it).
            if times_line in days:
                i += 1
                continue
            times = [t.strip() for t in times_line.split(",") if t.strip()]
            if times:
                out.append(asdict(WeeklyPerformanceRow(day=day, times=times)))
            i += 2
        else:
            i += 1
    return out


def _extract_booking_period(soup: BeautifulSoup) -> str | None:
    """The sidebar's Booking Period — a single date range string.

        <div id="booking_period-info">
            <h4>Booking Period</h4>
            <p>6 Jun 2025 - 5 Sep 2026</p>
        </div>
    """
    section = soup.select_one("#booking_period-info p")
    if section is None:
        return None
    return _decode(section.get_text(" ", strip=True))


def _extract_venue_address(soup: BeautifulSoup) -> str | None:
    """The visible venue panel address (one line).

    There are two places this appears on the page:
      1. Inside <article> in the #nav-venue tab (under <h5> venue name)
      2. Inside #venue-sidebar-info (also under <h5>)
    They carry the same text; we prefer (1) because it sits inside the
    main content. Fall back to (2) if (1) is missing."""
    venue_panel = soup.select_one("#nav-venue article")
    if venue_panel is not None:
        p = venue_panel.select_one("p.text-gray")
        if p is not None:
            text = _decode(p.get_text(" ", strip=True))
            if text:
                return text
    sidebar = soup.select_one("#venue-sidebar-info p.text-gray")
    if sidebar is not None:
        return _decode(sidebar.get_text(" ", strip=True))
    return None


def _extract_sidebar_price(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    """The "Tickets From £X" price shown in the booking sidebar/modal.

    The modal header has:
        <small>Tickets From</small>
        <div class="price">£29.50</div>
    Some shows show "Tickets From"; others might show a different label
    (or none at all if availability is empty). We just grab the .price
    div text and parse a float out of it."""
    price_el = soup.select_one(".show-calendar-modal .price")
    if price_el is None:
        return None, None
    display = _decode(price_el.get_text(strip=True))
    if not display:
        return None, None
    m = PRICE_RE.search(display)
    value = float(m.group(1).replace(",", ".")) if m else None
    return value, display


def _extract_book_tickets_url(soup: BeautifulSoup) -> str | None:
    """The main "Book Tickets" button URL — points at the booking flow
    root (no #perf= fragment), e.g.
    https://secure.lovetheatre.com/book/1GVLR-disney-s-hercules/"""
    btn = soup.select_one("a.btn-primary.btn-action")
    if btn is None or not btn.has_attr("href"):
        # Some shows have an alternative class set on the book button;
        # fall back to any anchor inside the modal that points at the
        # secure.lovetheatre.com origin.
        for a in soup.select(".show-calendar-modal a[href*='secure.lovetheatre.com/book/']"):
            href = a.get("href")
            if href and "#" not in href:
                return _decode(href)
        return None
    return _decode(btn.get("href"))


def _extract_calendar_meta(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """The calendar widget on the sidebar carries:
        <div id="calendar" data-id="1GVLR"
                           data-url="https://secure.lovetheatre.com/book/1GVLR-disney-s-hercules/"
                           data-start="2026-05-19"></div>
    data-id should match show_id; data-start is the calendar's initial
    rendered date (≈ today)."""
    cal = soup.select_one("#calendar")
    if cal is None:
        return None, None
    cal_id = (cal.get("data-id") or "").strip() or None
    start = (cal.get("data-start") or "").strip() or None
    return cal_id, start


def _extract_group_info(soup: BeautifulSoup) -> str | None:
    """The Groups & Schools section text. Multiple <p> tags — we join
    them with newlines. Strips empty paragraphs."""
    section = soup.select_one("#nav-group-school")
    if section is None:
        return None
    # The free text lives in a column inside the section; the heading
    # ("Groups & Schools") and the Submit a Request button are also
    # inside but we want the body paragraphs only.
    body_col = section.select_one("div.col-12.col-lg-10.text-gray") \
        or section.select_one("div.text-gray")
    if body_col is None:
        # Fall back to all <p> tags in the section
        paras = [p.get_text(" ", strip=True) for p in section.select("p")]
    else:
        paras = [p.get_text(" ", strip=True) for p in body_col.select("p")]
    paras = [_decode(p) for p in paras if p and p.strip()]
    paras = [p for p in paras if p]
    return "\n\n".join(paras) if paras else None


# ---------------------------------------------------------------------------
# Show detail parser — top-level
# ---------------------------------------------------------------------------

def parse_show(html_text: str) -> dict:
    """Parse a show detail page; return a dict of detail-only fields.

    Listing-card fields (image, venue_text, listing prices, etc.) come
    from the listing scrape and are merged into the final Show by the
    caller — we don't repeat them here."""
    soup = BeautifulSoup(html_text, "html.parser")
    blocks = _extract_jsonld_blocks(soup)

    # Identity / header
    canonical = _extract_canonical(soup)
    h1_name = _extract_h1_name(soup)
    breadcrumb_category = _extract_breadcrumb_category(soup)
    header_meta = _extract_show_header_meta(soup)

    # Product JSON-LD
    product = _find_product_node(blocks) or {}
    product_offers = product.get("offers")
    if not isinstance(product_offers, dict):
        product_offers = {}
    product_description = _decode(product.get("description"))
    product_image = None
    p_img = product.get("image")
    if isinstance(p_img, list) and p_img:
        product_image = _decode(p_img[0]) if isinstance(p_img[0], str) else None
    elif isinstance(p_img, str):
        product_image = _decode(p_img)

    # TheaterEvent[] JSON-LD → performances
    performances = _extract_performances(blocks)

    # Venue fallback — when the header anchor (`a[href*='/venue/']` inside
    # `.show-hero-info`) is absent because the show page hasn't been linked
    # to a venue record in WordPress, `_extract_show_header_meta` returns
    # `venue_name=None` and we'd ship the record without a venue. Recover
    # by looking at the JSON-LD TheaterEvent location.name on the first
    # performance — it's the same physical location and is set even on
    # pages that lack the header venue link. Observed missing-header cases:
    # 'Here Comes J. Edgar!' (King's Head Theatre), 'Chat Noir!' (The Lost
    # Estate), '58th Street' (Belle Livingstone's). The cleanup pass below
    # strips multi-line address junk and ", <area>" suffixes so the result
    # matches what other sources use for the same venue.
    venue_name = header_meta["venue_name"]
    if venue_name is None and performances:
        for p in performances:
            if p.venue_name:
                venue_name = _clean_perf_venue_name(p.venue_name)
                if venue_name:
                    log.debug(
                        "venue_name fallback: header lookup failed for %s, "
                        "recovered '%s' from performance JSON-LD",
                        canonical or "<unknown URL>", venue_name,
                    )
                    break

    # FAQPage JSON-LD
    faq = _find_faq_entries(blocks)

    # Visible body content
    description_full = _extract_description_full(soup)
    next_availability = _extract_next_availability(soup)
    weekly_performances = _extract_weekly_performances(soup)
    booking_period = _extract_booking_period(soup)
    venue_address = _extract_venue_address(soup)
    sidebar_price_value, sidebar_price_display = _extract_sidebar_price(soup)
    book_tickets_url = _extract_book_tickets_url(soup)
    calendar_id, calendar_start_date = _extract_calendar_meta(soup)
    group_info = _extract_group_info(soup)

    return {
        "detail_canonical": canonical,
        "detail_name": h1_name,
        "breadcrumb_category": breadcrumb_category,
        "badge_category": header_meta["badge_category"],
        "product_description": product_description,
        "product_image": product_image,
        "detail_low_price": _to_float(product_offers.get("price")),
        "detail_currency": _decode(product_offers.get("priceCurrency")),
        "detail_availability": _decode(product_offers.get("availability")),
        "detail_offer_url": _decode(product_offers.get("url")),
        "description_full": description_full,
        "hero_image": header_meta["hero_image"],
        "show_thumbnail": header_meta["show_thumbnail"],
        "duration_text": header_meta["duration_text"],
        "venue_name": venue_name,
        "venue_url": header_meta["venue_url"],
        "venue_address": venue_address,
        "sidebar_price_value": sidebar_price_value,
        "sidebar_price_display": sidebar_price_display,
        "book_tickets_url": book_tickets_url,
        "calendar_id": calendar_id,
        "calendar_start_date": calendar_start_date,
        "booking_period": booking_period,
        "weekly_performances": weekly_performances,
        "next_availability": next_availability,
        "group_info": group_info,
        "performances": [asdict(p) for p in performances],
        "faq": faq,
    }


# ---------------------------------------------------------------------------
# Stage 1 — fetch all listings, build the master union
# ---------------------------------------------------------------------------

def fetch_listing(session: requests.Session, url: str) -> list[ListingCard]:
    """Fetch a single listing URL and return its parsed cards.

    Raises requests.RequestException on network errors; the caller
    decides whether one listing failure is fatal (it isn't for the
    offer slices; the master `whats_on` failing IS fatal)."""
    log.info("Fetching listing %s", url)
    r = _fetch_html(session, url)
    cards = parse_listing(r.text)
    log.info("  → %d cards", len(cards))
    return cards


def fetch_all_listings(
    session: requests.Session,
    include_tag_lists: bool,
) -> tuple[list[ListingCard], dict[int, list[str]], dict[str, int]]:
    """Fetch the master `whats_on` plus the three offer-slice listings.

    Returns:
      * `master_cards` — the deduplicated union, ordered by
        whats_on-first appearance.
      * `appears_in_map` — post_id → sorted list of listing keys. Keyed
        on post_id because that's our primary key (see
        _parse_one_listing_card for why we don't use show_id).
      * `listing_counts` — listing key → card count (or -1 if skipped).

    The `whats_on` failure aborts the run; offer-slice failures are
    recorded as -1 and the run continues with a warning. This mirrors
    the policy in ttd_scraper / olt_scraper — slice failures shouldn't
    nuke the whole scrape because the master listing is the source of
    truth for "which shows exist"."""
    master_cards: list[ListingCard] = []
    appears_in: dict[int, list[str]] = {}
    counts: dict[str, int] = {k: -1 for k in LISTING_URLS}
    seen_post_ids: dict[int, ListingCard] = {}

    # Listing-iteration order: whats_on first, then the offer slices in
    # the order they're declared in LISTING_URLS.
    listings_to_fetch = list(LISTING_URLS.items())
    if not include_tag_lists:
        listings_to_fetch = listings_to_fetch[:1]  # whats_on only
        log.info("--no-tag-lists: fetching only the master listing")

    for key, url in listings_to_fetch:
        try:
            cards = fetch_listing(session, url)
        except requests.RequestException as e:
            if key == "whats_on":
                # The master listing failing is fatal — re-raise so
                # main() can bail without writing a partial output.
                raise
            log.warning("Listing %s failed (%s) — continuing without it", key, e)
            counts[key] = -1
            continue

        counts[key] = len(cards)
        for card in cards:
            if card.post_id in seen_post_ids:
                # Already in master — just record membership
                appears_in.setdefault(card.post_id, []).append(key)
            else:
                seen_post_ids[card.post_id] = card
                master_cards.append(card)
                appears_in.setdefault(card.post_id, []).append(key)

    # Sort appears_in values for stable output
    for pid in appears_in:
        appears_in[pid] = sorted(set(appears_in[pid]))

    return master_cards, appears_in, counts


# ---------------------------------------------------------------------------
# Stage 2 — fetch show detail pages in parallel
# ---------------------------------------------------------------------------

def fetch_show_detail(
    session: requests.Session,
    card: ListingCard,
) -> tuple[dict | None, str | None]:
    """Fetch one show detail page and parse it.

    Returns (detail_dict, None) on success or (None, error_message) on
    failure. One application-level retry on parse errors handles
    mid-deploy partial HTML."""
    try:
        r = _fetch_html(session, card.url)
    except requests.RequestException as e:
        return None, f"http: {e}"

    try:
        return parse_show(r.text), None
    except Exception as e:  # noqa: BLE001
        # Retry once after a brief wait — parse errors are usually
        # transient (partial HTML, mid-deploy).
        log.debug("First parse of %s failed (%s); retrying once", card.url, e)
        time.sleep(DETAIL_PARSE_RETRY_DELAY_S)
        try:
            r = _fetch_html(session, card.url)
            return parse_show(r.text), None
        except requests.RequestException as e2:
            return None, f"http (retry): {e2}"
        except Exception as e2:  # noqa: BLE001
            return None, f"parse: {e2}"


def fetch_all_details(
    session: requests.Session,
    cards: list[ListingCard],
    appears_in_map: dict[int, list[str]],
    concurrency: int,
    deadline: float | None,
) -> tuple[list[Show], list[ShowFailure], bool]:
    """Fetch every show's detail page in parallel; merge listing-card
    fields into the final Show records.

    Returns (shows, failures, budget_exceeded)."""
    log.info("Fetching %d show detail pages with %d workers...",
             len(cards), concurrency)

    shows: list[Show] = []
    failures: list[ShowFailure] = []
    budget_exceeded = False
    lock = Lock()
    progress = [0]

    def task(card: ListingCard) -> None:
        nonlocal budget_exceeded
        # Wall-clock budget check before starting work
        if deadline is not None and time.monotonic() > deadline:
            with lock:
                budget_exceeded = True
                failures.append(ShowFailure(
                    show_id=card.show_id,
                    slug=card.slug,
                    url=card.url,
                    error="skipped: wall-clock budget exceeded",
                ))
                progress[0] += 1
            return

        detail, err = fetch_show_detail(session, card)
        with lock:
            if detail is not None:
                shows.append(Show(
                    show_id=card.show_id,
                    post_id=card.post_id,
                    name=card.name,
                    url=card.url,
                    slug=card.slug,
                    image=card.image,
                    venue_text=card.venue_text,
                    listing_price_value=card.listing_price_value,
                    listing_original_price=card.listing_original_price,
                    listing_price_display=card.listing_price_display,
                    has_special_offer=card.has_special_offer,
                    offer_label=card.offer_label,
                    genre=card.genre,
                    tags=card.tags,
                    data_date=card.data_date,
                    appears_in=appears_in_map.get(card.post_id, []),
                    detail_canonical=detail["detail_canonical"],
                    detail_name=detail["detail_name"],
                    breadcrumb_category=detail["breadcrumb_category"],
                    badge_category=detail["badge_category"],
                    product_description=detail["product_description"],
                    product_image=detail["product_image"],
                    detail_low_price=detail["detail_low_price"],
                    detail_currency=detail["detail_currency"],
                    detail_availability=detail["detail_availability"],
                    detail_offer_url=detail["detail_offer_url"],
                    description_full=detail["description_full"],
                    hero_image=detail["hero_image"],
                    show_thumbnail=detail["show_thumbnail"],
                    duration_text=detail["duration_text"],
                    venue_name=detail["venue_name"],
                    venue_url=detail["venue_url"],
                    venue_address=detail["venue_address"],
                    sidebar_price_value=detail["sidebar_price_value"],
                    sidebar_price_display=detail["sidebar_price_display"],
                    book_tickets_url=detail["book_tickets_url"],
                    calendar_id=detail["calendar_id"],
                    calendar_start_date=detail["calendar_start_date"],
                    booking_period=detail["booking_period"],
                    weekly_performances=detail["weekly_performances"],
                    next_availability=detail["next_availability"],
                    group_info=detail["group_info"],
                    performances=detail["performances"],
                    faq=detail["faq"],
                ))
            else:
                failures.append(ShowFailure(
                    show_id=card.show_id,
                    slug=card.slug,
                    url=card.url,
                    error=err or "unknown",
                ))
            progress[0] += 1
            n = progress[0]
            if n % 25 == 0 or n == len(cards):
                log.info("  detail progress: %d/%d (%d ok, %d failed)",
                         n, len(cards), len(shows), len(failures))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(task, cards))

    # Keep `shows` in master-listing order (insertion order under the
    # GIL + lock is the work-submission order, but ThreadPoolExecutor.map
    # doesn't guarantee that the with-lock append order matches submission
    # order). Sort by master-card index to restore deterministic ordering.
    card_order = {c.post_id: i for i, c in enumerate(cards)}
    shows.sort(key=lambda s: card_order.get(s.post_id, 0))

    return shows, failures, budget_exceeded


# ---------------------------------------------------------------------------
# Stage 3 — sanity checks, previous-run comparison, validation
# ---------------------------------------------------------------------------

def run_sanity_checks(
    shows: list[Show],
    failures: list[ShowFailure],
    master_count: int,
) -> list[str]:
    """Operational sanity checks — flag conditions that suggest the
    scraper itself is degraded (not just that the site's content
    changed). Returns a list of warning strings, one per condition."""
    warnings: list[str] = []
    if master_count == 0:
        return warnings

    # Hard floor: if we lost more than 10% of shows to detail failures,
    # something is wrong with the detail-fetch path.
    failure_rate = len(failures) / master_count
    if failure_rate > 0.10:
        warnings.append(
            f"detail: {len(failures)}/{master_count} detail fetches failed "
            f"({100*failure_rate:.0f}%, >10% threshold)"
        )

    n_shows = len(shows)
    if n_shows == 0:
        warnings.append("detail: 0 shows produced — every detail fetch failed")
        return warnings

    # Missing critical fields — these should be near-universal
    def pct_missing(predicate, label: str, threshold: float = 0.10) -> None:
        n_missing = sum(1 for s in shows if predicate(s))
        if n_missing / n_shows > threshold:
            warnings.append(
                f"detail: {n_missing}/{n_shows} shows missing {label} "
                f"({100*n_missing/n_shows:.0f}%, >{int(threshold*100)}% threshold)"
            )

    pct_missing(lambda s: not s.venue_name, "venue_name")
    pct_missing(lambda s: not s.product_description, "product_description")
    pct_missing(lambda s: not s.performances, "performances", threshold=0.20)
    pct_missing(lambda s: not s.book_tickets_url, "book_tickets_url",
                threshold=0.05)

    # calendar_id is widely but not universally present on LOVEtheatre:
    # placeholder shows (no Ingresso show_id) have no calendar widget by
    # design, and some bookable shows that are between booking windows
    # don't render one either. We check only among bookable shows and use
    # a 15% threshold to allow for that real-world heterogeneity (we've
    # observed ~9% missing in normal operation).
    bookable_shows = [s for s in shows if s.show_id]
    if bookable_shows:
        n_no_cal = sum(1 for s in bookable_shows if not s.calendar_id)
        if n_no_cal / len(bookable_shows) > 0.15:
            warnings.append(
                f"detail: {n_no_cal}/{len(bookable_shows)} bookable shows "
                f"missing calendar_id "
                f"({100*n_no_cal/len(bookable_shows):.0f}%, >15% threshold)"
            )

    # Sanity: calendar_id should match show_id for the vast majority of
    # shows (they're the same Ingresso ID exposed in two places). If
    # they diverge a lot, our show_id key might be drifting from the
    # real one. We skip placeholder shows (empty show_id) because they
    # legitimately won't have a calendar widget.
    bookable = [s for s in shows if s.show_id]
    n_mismatch = sum(
        1 for s in bookable
        if s.calendar_id and s.calendar_id.upper() != s.show_id.upper()
    )
    if bookable and n_mismatch / len(bookable) > 0.05:
        warnings.append(
            f"identity: {n_mismatch}/{len(bookable)} shows have calendar_id "
            f"!= show_id ({100*n_mismatch/len(bookable):.0f}%, >5% "
            "threshold) — Ingresso ID mapping may be drifting"
        )

    # FAQ is optional on some pages; warn only if it's missing
    # everywhere (which would suggest the FAQPage JSON-LD shape changed)
    n_with_faq = sum(1 for s in shows if s.faq)
    if n_shows >= 20 and n_with_faq == 0:
        warnings.append(
            "detail: 0 shows have any FAQ entries — FAQPage JSON-LD shape "
            "may have changed"
        )

    return warnings


def compare_with_previous(shows: list[Show], previous_path: Path) -> list[str]:
    """Compare today's scrape against the previous JSON output and
    flag catastrophic regressions.

    Returns [] if there's no previous file or the diff is within normal
    daily-drift bounds. The thresholds mirror the other scrapers — a
    small amount of churn is fine (shows ending, new ones announced); a
    >20% drop in counts almost certainly means a scraper regression."""
    if not previous_path.exists():
        return []
    try:
        with previous_path.open("r", encoding="utf-8") as f:
            prev = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.info("Could not read previous output for comparison (%s)", e)
        return []

    prev_shows = prev.get("shows") or []
    if not prev_shows:
        return []

    warnings: list[str] = []
    n_new, n_prev = len(shows), len(prev_shows)

    if n_prev > 0 and n_new < n_prev * 0.80:
        warnings.append(
            f"prev-run: show count dropped {n_prev} → {n_new} "
            f"({100*(n_prev-n_new)/n_prev:.0f}% loss, >20% threshold)"
        )

    perf_new = sum(len(s.performances) for s in shows)
    perf_prev = prev.get("performance_count") or sum(
        len(s.get("performances") or []) for s in prev_shows
    )
    if perf_prev > 0 and perf_new < perf_prev * 0.50:
        warnings.append(
            f"prev-run: performance count dropped {perf_prev} → {perf_new} "
            f"({100*(perf_prev-perf_new)/perf_prev:.0f}% loss, >50% threshold)"
        )

    new_ids = {s.post_id for s in shows}
    prev_ids = {s["post_id"] for s in prev_shows
                if isinstance(s.get("post_id"), int)}
    if prev_ids:
        vanished = prev_ids - new_ids
        if len(vanished) > len(prev_ids) * 0.30:
            warnings.append(
                f"prev-run: {len(vanished)}/{len(prev_ids)} shows from "
                f"previous run are missing today "
                f"({100*len(vanished)/len(prev_ids):.0f}% churn, >30% threshold)"
            )

    return warnings


def validate_data_ranges(shows: list[Show]) -> list[str]:
    """Lightweight data-shape validation. Flags out-of-range values
    that almost certainly indicate a parser bug rather than real data
    (e.g. a £999.99 ticket or a year 1970 performance).

    Distinct from `run_sanity_checks` (which is about presence/absence
    of fields) — these are about value plausibility."""
    issues: list[str] = []
    if not shows:
        return issues

    current_year = datetime.now(timezone.utc).year

    def _price_ok(p) -> bool:
        # 0.0 is a legitimate value on LOVEtheatre — used for shows
        # between booking windows (data-price="0") and for placeholder
        # "coming soon" shows. We treat it as missing-data rather than
        # a parser bug; only flag prices that are negative or absurdly
        # high (which would indicate the parser captured the wrong
        # element, like a date or a sequence number).
        return p is None or float(p) == 0.0 or (1.0 <= float(p) <= 500.0)

    def _year_ok(y) -> bool:
        return 2020 <= y <= current_year + 5

    bad_listing_price = []
    bad_perf_price = []
    bad_perf_dates = []
    bad_urls = []

    for s in shows:
        for label, p in [
            ("listing_price_value", s.listing_price_value),
            ("listing_original_price", s.listing_original_price),
            ("detail_low_price", s.detail_low_price),
            ("sidebar_price_value", s.sidebar_price_value),
        ]:
            if not _price_ok(p):
                bad_listing_price.append((s.show_id, label, p))

        for perf in s.performances:
            p = perf.get("price")
            if not _price_ok(p):
                bad_perf_price.append((s.show_id, p))
            date = perf.get("date")
            if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
                if not _year_ok(int(date[:4])):
                    bad_perf_dates.append((s.show_id, date))

        # URLs should be on lovetheatre.com (or secure.lovetheatre.com
        # for booking URLs); flag anything else.
        for url_field in ("url", "detail_canonical", "detail_offer_url"):
            url = getattr(s, url_field, None)
            if url:
                host = urlparse(url).netloc.lower()
                if host and "lovetheatre.com" not in host:
                    bad_urls.append((s.show_id, url_field, url))

    if bad_listing_price:
        issues.append(
            f"data-range: {len(bad_listing_price)} prices out of 1–500 range "
            f"(e.g. show {bad_listing_price[0][0]}: "
            f"{bad_listing_price[0][1]}={bad_listing_price[0][2]})"
        )
    if bad_perf_price:
        issues.append(
            f"data-range: {len(bad_perf_price)} performance prices out of range"
        )
    if bad_perf_dates:
        issues.append(
            f"data-range: {len(bad_perf_dates)} performances with implausible "
            f"dates (expected 2020–{current_year+5}, "
            f"e.g. {bad_perf_dates[0][1]})"
        )
    if bad_urls:
        issues.append(
            f"data-range: {len(bad_urls)} shows with URLs outside the "
            f"lovetheatre.com origin (e.g. {bad_urls[0][2]})"
        )

    return issues


# ---------------------------------------------------------------------------
# Output (atomic write + rotation)
# ---------------------------------------------------------------------------

def rotate_output(path: Path, keep: int = DEFAULT_ROTATION_DEPTH) -> None:
    """Shift existing output files to make room for a new write.

    lovetheatre.json     →  lovetheatre.json.1
    lovetheatre.json.1   →  lovetheatre.json.2
    ...
    lovetheatre.json.4   →  lovetheatre.json.5     (oldest kept)
    lovetheatre.json.5   →  (deleted)
    """
    if not path.exists():
        return
    for i in range(keep, 0, -1):
        src = path.with_name(f"{path.name}.{i - 1}") if i > 1 else path
        dst = path.with_name(f"{path.name}.{i}")
        if src.exists():
            if dst.exists():
                dst.unlink()
            try:
                src.rename(dst)
            except OSError as e:
                log.warning("rotation: could not move %s → %s: %s", src, dst, e)


def write_output(shows: list[Show], path: Path, report: ScrapeReport) -> None:
    """Atomically write the JSON output and embedded scrape report.

    Atomic via write-to-tmp + rename: a previous run's good JSON
    survives a crashed current run."""
    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": OUTPUT_SOURCE,
        "show_count": len(shows),
        "performance_count": sum(len(s.performances) for s in shows),
        "report": asdict(report),
        "shows": [asdict(s) for s in shows],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)
    log.info(
        "Wrote %s — %d shows, %d performances, %d warning(s)",
        path, payload["show_count"], payload["performance_count"],
        len(report.warnings),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Scrape LOVEtheatre.com (pure requests, parallel).",
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Only fetch details for this many shows (for testing).")
    p.add_argument("--out", type=Path, default=Path("lovetheatre.json"),
                   help="Output JSON file path (default: ./lovetheatre.json).")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel workers for detail pages "
                        f"(default: {DEFAULT_CONCURRENCY}).")
    p.add_argument("--no-tag-lists", action="store_true",
                   help="Only fetch the master /whats-on/ listing — skip the "
                        "three offer slices. Shows will have appears_in "
                        "containing only 'whats_on'.")
    p.add_argument("--max-runtime-seconds", type=int, default=None, metavar="N",
                   help="Soft wall-clock budget. After N seconds, the detail "
                        "fetcher stops queuing new requests; in-flight ones "
                        "finish. Partial output still written with a warning. "
                        "Default: unlimited.")
    p.add_argument("--no-rotate", action="store_true",
                   help="Don't keep previous output files as .1/.2/.../.5. "
                        "By default the previous 5 runs are retained.")
    p.add_argument("--rotation-depth", type=int, default=DEFAULT_ROTATION_DEPTH,
                   metavar="N",
                   help=f"How many previous outputs to retain "
                        f"(default: {DEFAULT_ROTATION_DEPTH}).")
    p.add_argument("--dry-run", action="store_true",
                   help="Do everything except write the output file.")
    args = p.parse_args(argv)

    session = build_session(pool_size=max(args.concurrency, 8))
    t_start = time.monotonic()
    deadline = (t_start + args.max_runtime_seconds
                if args.max_runtime_seconds is not None else None)

    if args.dry_run:
        log.info("--dry-run: no output file will be written")
    if deadline is not None:
        log.info("Wall-clock budget: %ds", args.max_runtime_seconds)

    # Stage 1: listings (master + offer slices), dedupe, build appears_in
    try:
        master_cards, appears_in_map, listing_counts = fetch_all_listings(
            session, include_tag_lists=not args.no_tag_lists,
        )
    except requests.RequestException as e:
        log.error("Master listing fetch failed: %s — aborting "
                  "(previous output preserved).", e)
        return EXIT_HARD_FAIL

    if not master_cards:
        log.error("No shows found in master listing — aborting "
                  "(previous output preserved).")
        return EXIT_HARD_FAIL

    log.info("Master catalogue assembled: %d unique shows from %d listing(s)",
             len(master_cards),
             sum(1 for v in listing_counts.values() if v >= 0))

    cards_to_fetch = master_cards
    if args.limit is not None:
        cards_to_fetch = master_cards[: args.limit]
        log.info("--limit applied: fetching details for %d/%d shows",
                 len(cards_to_fetch), len(master_cards))

    # Stage 2: detail pages
    shows, failures, budget_exceeded = fetch_all_details(
        session, cards_to_fetch, appears_in_map,
        concurrency=args.concurrency, deadline=deadline,
    )

    # Stage 3: sanity checks + previous-run compare + data validation
    warnings = run_sanity_checks(shows, failures,
                                 master_count=len(cards_to_fetch))
    if budget_exceeded:
        warnings.append(
            f"budget: wall-clock budget ({args.max_runtime_seconds}s) "
            f"exceeded — only {len(shows)}/{len(cards_to_fetch)} shows complete"
        )
    validation_warnings = validate_data_ranges(shows)
    if args.limit is None and not args.dry_run:
        warnings.extend(compare_with_previous(shows, args.out))

    for w in warnings:
        log.warning("anomaly: %s", w)
    for v in validation_warnings:
        log.warning("validation: %s", v)

    report = ScrapeReport(
        master_show_count=len(master_cards),
        succeeded_show_count=len(shows),
        failed_show_count=len(failures),
        listings_scraped=sorted(k for k, v in listing_counts.items() if v >= 0),
        listing_counts=listing_counts,
        budget_exceeded=budget_exceeded,
        warnings=warnings,
        validation_warnings=validation_warnings,
        failures=failures,
    )

    if not shows:
        log.error("No shows successfully scraped — preserving previous output.")
        return EXIT_HARD_FAIL

    if args.dry_run:
        log.info(
            "--dry-run complete: would write %d shows, %d performances, "
            "%d warning(s), %d validation note(s) — but NOT writing.",
            len(shows), sum(len(s.performances) for s in shows),
            len(warnings), len(validation_warnings),
        )
    else:
        if not args.no_rotate:
            rotate_output(args.out, keep=args.rotation_depth)
        write_output(shows, args.out, report)

    if warnings or validation_warnings or budget_exceeded:
        return EXIT_WARNINGS
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
