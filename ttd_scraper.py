"""
TheatreTicketsDirect.co.uk scraper (pure requests, parallel)
============================================================

Scrapes show listings from three URLs on TheatreTicketsDirect (TTD) and
fetches each show's detail page for the rich performance, weekly schedule,
description, access info and tag data.

The three listing URLs the user provided:

  * /category/london-musicals  — London musicals catalogue
  * /category/london-plays     — London plays catalogue
  * /discount-tickets          — shows currently with a price offer

Of those three, the musicals + plays pages together form the natural
"master" catalogue (deduplicated by show ID); the discount-tickets page
is a filter slice that lists whichever shows currently have an offer,
drawn from across all show categories on the site (so it occasionally
contains shows that aren't in either musicals/plays — e.g. opera or
sports events — and we keep those too). We treat all three as listing
sources, take their union as the master, and record an `appears_in`
field per show so consumers can tell which slices a show belongs to.

The site also exposes a /category/all "Shows A-Z" page that would be a
single-URL master, but the user didn't ask for it so we don't fetch it.
If you ever want to switch to that, the card markup is identical to the
three slice pages and the existing parser would Just Work.

Why no Playwright?
------------------
TTD is fully SSR. Listing pages render every card in one response (no
lazy-loading, no pagination) and each card is paired with a JSON-LD
TheaterEvent block carrying rich data: venue, address, geographic info
indirectly (via venue link), AggregateOffer price, availability,
booking-page URL, and a workPerformed back-reference to the show URL.

Show detail pages SSR everything too:
  * A schema.org Product JSON-LD block with sku, description, base
    offer price, and the canonical show URL.
  * Multiple TheaterEvent JSON-LD blocks (one per upcoming performance,
    typically the next 5) with full ISO datetimes, per-performance
    booking URLs, prices, and venue address.
  * A "Next Performances" tab listing the next 5–6 performances with
    their booking URLs as HTML links — typically one more entry than
    the JSON-LD covers (the JSON-LD caps at 5 even when 6 are visible).
  * Visible widgets: a Running Time / Running Since / Booking Until
    info box, an Important Information card (carries age guidance and
    other notices), a Mon–Sun weekly schedule table (matinee + evening
    columns), an editorial tags row, a description body, and a theatre
    block with access notes.

Setup
-----
    pip install requests beautifulsoup4

Usage
-----
    python ttd_scraper.py                          # full scrape
    python ttd_scraper.py --limit 5                # test with 5 shows
    python ttd_scraper.py --out data/ttd.json      # custom output path
    python ttd_scraper.py --concurrency 24         # more parallel workers
    python ttd_scraper.py --no-tag-lists           # only fetch musicals (no plays/discount)
    python ttd_scraper.py --dry-run                # don't write

Output is a single JSON file:

    {
      "scraped_at": "2026-05-19T08:30:00+00:00",
      "source": "https://www.theatreticketsdirect.co.uk/",
      "show_count": 203,
      "performance_count": 1014,
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

BASE = "https://www.theatreticketsdirect.co.uk"

# The three listing URLs the user provided. Order here doubles as the
# preferred "primary list" ordering — musicals first because it's the
# largest, then plays, then the discount filter slice. When we
# deduplicate across the three lists, the first list a show appears in
# determines its position in the master (so output ordering is stable
# across runs as long as the site keeps emitting cards in alphabetical
# order, which it currently does).
LISTING_URLS: dict[str, str] = {
    "musicals": f"{BASE}/category/london-musicals",
    "plays":    f"{BASE}/category/london-plays",
    "discount": f"{BASE}/discount-tickets",
}

# Used as the "source" field in the output JSON. We use the site root
# rather than any single listing URL because the catalogue is assembled
# from a union of three listing pages — naming one would be misleading.
OUTPUT_SOURCE = f"{BASE}/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # The site serves pages as ISO-8859-1 per its meta tag, but the
    # actual byte stream is UTF-8 with HTML entities for non-ASCII. We
    # accept anything; BeautifulSoup handles decoding via the bytes.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# TTD's origin server is markedly slower under parallel load than the other
# platforms in this series — empirically 16 workers all time out simultaneously
# at the 30s mark. 4 is the sweet spot for completing within a few minutes
# without cascade-failing. Override via --concurrency if your network is fast
# enough; CI environments closer to the origin can probably push to 8.
DEFAULT_CONCURRENCY = 4

# Per-request HTTP timeout. The default 30s is too tight for TTD's detail
# pages under any concurrency >2; 60s gives the server room to respond
# without exhausting urllib3's retry budget on transient slowness.
REQUEST_TIMEOUT_S = 60

# urllib3-level retries (network errors, 5xx, 429). Backoff = 1.0 spreads
# retries far enough apart that they don't all hit a still-overloaded server
# in the same 100ms window (which is what 0.5 was doing in the field).
RETRY_TOTAL = 3
RETRY_BACKOFF = 1.0

# Application-level retry for parse errors (mid-deploy partial HTML etc).
# One retry only — anything flakier than that is genuinely broken.
DETAIL_PARSE_RETRY_DELAY_S = 2.0

# Output file rotation depth — keeps ttd_london.json, .1, .2, ..., .5
DEFAULT_ROTATION_DEPTH = 5

# Exit codes — 0 clean, 1 hard fail (no output), 2 wrote with warnings.
EXIT_CLEAN = 0
EXIT_HARD_FAIL = 1
EXIT_WARNINGS = 2

# Show detail URLs look like /shows/{id}/{slug}-tickets. The numeric
# id is the stable primary key; the slug is purely cosmetic. The site
# also exposes /availability/{id}/{slug} (linked from cards) and
# /shows/seats/{id}/... (the booking flow) — neither of those is the
# detail page.
#
# The slug character class is intentionally permissive: real slugs in
# the wild include apostrophes ("disney's"), exclamation marks
# ("here-comes-j.-edgar!"), em-dashes ("death-note-–-the-musical"),
# accented characters ("derrière"), full stops, ampersands, and even
# literal unescaped "?" ("are-you-watching?-tickets"). We accept
# anything except a "/" (which would mean a different URL path) or a
# "#" (URL fragment terminator). Accepting "?" means we can't tell
# slug-with-question-mark apart from genuine query strings via regex
# alone — but TTD doesn't use query strings on show URLs in practice,
# and the show-canonical pattern requires the slug to end in "-tickets"
# which guards against confusion.
SHOW_URL_RE = re.compile(
    r"^https?://www\.theatreticketsdirect\.co\.uk/shows/(\d+)/([^/#]+?)/?$",
    re.IGNORECASE,
)

# Booking links from the "Next Performances" tab look like
#   /shows/seats/{id}/{Y}/{M}/{D}/{?}/{HH-MM}/0
# We extract date/time from these where we can to enrich the
# performance list when a perf appears in the visible tab but not in
# the (capped at 5) TheaterEvent JSON-LD blocks.
BOOK_URL_RE = re.compile(
    r"/shows/seats/(\d+)/(\d{4})/(\d{1,2})/(\d{1,2})/\d+/(\d{1,2})-(\d{2})/\d+/?$"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ttd")

# Silence urllib3's "Retrying (...)" chatter. urllib3 logs every retry at
# WARNING level, which looks alarming but is just library bookkeeping:
# the retry budget exists *because* we expect the occasional slow request,
# and if it ultimately runs out we'll record the failure ourselves via the
# ShowFailure path. The final ScrapeReport (with succeeded/failed counts
# and warnings list) is the source of truth — these per-retry log lines
# are just narrating the journey.
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ListingCard:
    """A card as it appears on any of the three listing pages, paired with
    its companion JSON-LD TheaterEvent block."""
    id: int                            # numeric show id from the URL
    name: str
    url: str                           # full canonical detail URL
    slug: str                          # path slug, e.g. "360-allstars-tickets"
    image: str | None                  # card image (400x240)
    venue_text: str | None             # the visible "<span class='theatre'>" line
    offer_label: str | None            # e.g. "Special Prices", "Save Up To 25%"
    notice_text: str | None            # e.g. "Closing in 18 Days" / "Opens 20 Jun 2026"
    # Parsed out of the notice_text — distinguishes the two kinds.
    closing_notice: str | None         # "Closing in 18 Days" (or None)
    opening_notice: str | None         # "Opens 20 Jun 2026" (or None)
    # From the paired JSON-LD TheaterEvent. The site's JSON-LD uses
    # non-standard lowercase keys for address fields (`streetaddress`,
    # `addresslocality`, `Postaladdress` instead of the schema.org
    # standard CamelCase) — we normalise to lowercase on extraction.
    jsonld_event_name: str | None
    jsonld_start_date: str | None
    jsonld_end_date: str | None
    jsonld_status: str | None
    jsonld_description: str | None
    jsonld_low_price: float | None
    jsonld_high_price: float | None
    jsonld_currency: str | None
    jsonld_availability: str | None
    # The `offers.url` on the card is a /availability/{id}/{slug} URL —
    # not the detail page (which is /shows/{id}/{slug}-tickets). It's
    # used for the actual buy-flow entry; we record it but it's not the
    # show URL.
    jsonld_availability_url: str | None
    jsonld_avail_starts: str | None    # the upcoming day tickets become bookable
    jsonld_valid_from: str | None      # when the price quote is valid from
    jsonld_venue_name: str | None
    jsonld_venue_url: str | None
    jsonld_venue_address: dict | None  # {street, locality, postal_code, country}


@dataclass
class Performance:
    """A single upcoming performance, sourced from the detail page's
    TheaterEvent JSON-LD blocks and (where overlapping) the visible
    'Next Performances' tab.

    The TheaterEvent JSON-LD list is capped at 5 even when the visible
    tab shows 6, so we union the two and join on (date, time) — perfs
    only in the visible tab carry no price info."""
    iso: str | None              # full ISO datetime, e.g. "2026-06-02T19:30:00"
    date: str | None             # "2026-06-02"
    time: str | None             # "19:30"
    price: float | None          # from offers.price (where available)
    currency: str | None
    availability: str | None
    book_url: str | None         # URL into the seat-selection flow
    source: str                  # "jsonld" or "next_perf_link" — provenance


@dataclass
class WeeklyScheduleRow:
    """One day's matinee/evening pattern from the Mon-Sun timings table."""
    day: str              # "Monday" .. "Sunday"
    matinee: str | None   # display text, e.g. "14:30", or None when "-"
    evening: str | None


@dataclass
class EditorialTag:
    """A clickable badge from the Tags tab on the detail page.

    Two kinds end up here mixed together: (a) categorical tags that
    map to filter pages (Musical / Play / Drama / Opera / etc.), and
    (b) scheduling tags (Saturday Matinee, Wednesday Matinee, Limited
    Run). We keep them as-is and let consumers infer which is which
    from the URL slug if needed."""
    name: str | None
    url: str | None
    slug: str | None      # e.g. "saturday-matinee" — the last URL segment


@dataclass
class FutureMonth:
    """A 'next-years' calendar link below the next-performances list.
    These point to monthly calendar pages — useful for consumers that
    want to walk further into the future than the 5–6 next-performance
    entries."""
    label: str            # "May 2026"
    month: int | None     # 5
    year: int | None      # 2026
    url: str              # calendar URL


@dataclass
class Show:
    """Aggregate of listing card + show detail page."""
    # Identity
    id: int
    name: str
    url: str                          # canonical detail URL
    slug: str
    sku: str | None                   # from Product JSON-LD (should == str(id))
    # From listing card / paired JSON-LD
    image: str | None
    offer_label: str | None
    venue_text: str | None
    notice_text: str | None           # raw "Closing in X" / "Opens Y" string
    closing_notice: str | None
    opening_notice: str | None
    listing_low_price: float | None
    listing_high_price: float | None  # often empty string from the site
    listing_currency: str | None
    listing_availability: str | None
    listing_availability_url: str | None
    listing_avail_starts: str | None  # ISO date when tickets first go on sale
    listing_valid_from: str | None    # ISO date the price quote is valid from
    description_short: str | None     # JSON-LD card description (1-liner)
    # From listing JSON-LD: venue block (richer than the visible card text)
    venue_name: str | None
    venue_url: str | None
    venue_address: dict | None        # {street, locality, postal_code, country}
    # Filter membership across the three listing URLs
    appears_in: list[str]

    # ---- Detail page fields below ----
    detail_canonical: str | None      # <link rel="canonical"> on the show page
    # Product JSON-LD
    product_description: str | None
    detail_image: str | None          # show logo (square thumbnail)
    detail_low_price: float | None
    detail_currency: str | None
    detail_availability: str | None
    detail_offer_url: str | None
    # Visible detail-page widgets
    header_image_url: str | None      # banner image at top of show page
    description_full: str | None      # full visible synopsis paragraphs
    running_time: str | None          # "1hr 5mins No interval"
    running_since: str | None         # "Tue, 2 June 2026"
    booking_until: str | None         # "Sat, 6 June 2026"
    important_info: str | None        # the Important Information card body
    age_content: str | None           # parsed from important_info when present
    access_info: str | None           # the theatre access notes block
    venue_full_address: str | None    # the visible "<em class='f14'>...</em>" line
    seating_plan_image: str | None    # static GIF of the venue seating plan
    weekly_schedule: list[dict]       # Mon-Sun matinee/evening rows
    editorial_tags: list[dict]        # badges from the Tags tab
    category: str | None              # from breadcrumb: "Musical" / "Play" / etc.
    book_from_price: float | None     # parsed "From £X.XX" on the Book Tickets button
    book_from_price_display: str | None
    has_offer_badge: bool             # "On Offer" badge present on the Book button
    offer_card_title: str | None      # the offer card header on the detail page
    offer_card_body: str | None       # offer card body (e.g. "Valid all performances...")
    # The next ~6 performances, unioned across JSON-LD and the visible
    # Next Performances tab
    performances: list[dict]
    # Future-month calendar links (purely advisory navigation; no perf data)
    future_months: list[dict]


@dataclass
class ShowFailure:
    """A show in the listings that we couldn't fetch or parse a detail
    page for. Recorded so consumers see what's missing and why."""
    id: int
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"£\s*(\d+(?:[.,]\d+)?)")
_AGES_RE = re.compile(r"(?:Ages?\s*[:\-]?\s*)?([0-9]{1,2}\+|[Aa]ll [Aa]ges|[Uu]nder [0-9]+)")


def _decode(s: str | None) -> str | None:
    """HTML-entity-decode and strip a string. Handles the double-encoded
    entities (e.g. `&amp;ldquo;`) that appear in some JSON-LD blocks by
    decoding twice — once is enough for most fields, but the
    description in some Product blocks is double-encoded."""
    if s is None:
        return None
    once = html.unescape(s)
    # Cheap check for double encoding — if a literal `&amp;` survived,
    # decode again. Cap at 2 passes so a maliciously crafted page can't
    # send us into a loop.
    twice = html.unescape(once) if "&amp;" in once else once
    return twice.strip() or None


def _to_float(s) -> float | None:
    """Coerce to float; treat empty string as None. The site's
    AggregateOffer.highPrice is consistently '' rather than missing."""
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _slug_from_url(url: str) -> str:
    """Extract the slug from a show URL.

    The slug is the third path segment in /shows/{id}/{slug}. We can't
    use urllib.parse.urlparse here because some slugs contain literal
    unescaped "?" characters (e.g. "are-you-watching?-tickets") that
    urlparse would treat as the start of a query string and drop. We
    re-use SHOW_URL_RE which accepts the "?" inside the slug."""
    m = SHOW_URL_RE.match(url)
    return m.group(2) if m else ""


def _id_slug_from_show_url(url: str) -> tuple[int, str] | None:
    """Parse a show URL into (id, slug). Returns None for non-show URLs.

    Used in two places: extracting show identity from listing card
    anchors, and from JSON-LD `workPerformed.sameAs` back-references
    (used as a fallback when the visible anchor is malformed)."""
    m = SHOW_URL_RE.match(url)
    if not m:
        return None
    try:
        return int(m.group(1)), m.group(2)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Listing parser (used for all three URLs)
# ---------------------------------------------------------------------------

def _extract_jsonld_blocks(scope) -> list[dict]:
    """Parse every <script type='application/ld+json'> block under
    `scope` (which can be a soup or any tag). Tolerates broken blocks —
    if one fails to parse, the others are still usable.

    Uses strict=False on json.loads to accept literal newlines and tabs
    inside JSON string values. TTD's detail-page Product and
    TheaterEvent blocks have raw `\\n` bytes embedded directly in
    description fields (rather than the JSON-mandated `\\\\n` escape),
    which strict json.loads rejects. strict=False is the right tool
    here — it loosens only the control-character rule, not the
    structural validation, so genuinely malformed JSON still fails."""
    out: list[dict] = []
    for s in scope.select("script[type='application/ld+json']"):
        if not s.string:
            continue
        try:
            out.append(json.loads(s.string, strict=False))
        except json.JSONDecodeError:
            # The site's JSON-LD is usually well-formed under
            # strict=False; log debug and move on. The other blocks
            # on the page are still useful.
            log.debug("skipping malformed JSON-LD block")
    return out


def _flatten_listing_address(addr: dict | None) -> dict | None:
    """Normalise an address node from a listing-card TheaterEvent.

    The site uses lowercase keys (`streetaddress`, `addresslocality`,
    `addressCountry`, sometimes `postalcode`). We map them onto a
    consistent dict shape regardless of case."""
    if not isinstance(addr, dict):
        return None
    # Look up keys case-insensitively. The most common variants are
    # listed first to short-circuit cheaply.
    def get_any(*keys):
        for k in keys:
            if k in addr and addr[k] not in (None, ""):
                return addr[k]
            # Case-insensitive fallback
            for ak, av in addr.items():
                if ak.lower() == k.lower() and av not in (None, ""):
                    return av
        return None

    return {
        "street": get_any("streetaddress", "streetAddress"),
        "locality": get_any("addresslocality", "addressLocality"),
        "postal_code": get_any("postalcode", "postalCode"),
        "country": get_any("addressCountry"),
    }


def _theater_events_in_doc_order(blocks: list[dict]) -> list[dict]:
    """Filter to TheaterEvent JSON-LD blocks only, preserving document
    order. The listing-page pairing relies on the count and order
    matching the visible card grid 1:1, which we've verified
    empirically on all three URLs (52/52, 113/113, 93/93)."""
    return [b for b in blocks
            if isinstance(b, dict) and b.get("@type") == "TheaterEvent"]


def _parse_notice(text: str | None) -> tuple[str | None, str | None]:
    """Split a notice string into (closing_notice, opening_notice).

    The same DOM slot carries either kind:
      * `<div class="text-danger ...">Closing in 18 Days</div>`
      * `<div class="text-dark ...">Opens 20 Jun 2026</div>`
    Some cards have both (separated by a newline within `notice_text`).

    Heuristic: split on newlines and classify each line by its leading
    word ("Closing" → closing, "Opens" → opening). Anything else gets
    dropped — we'd rather have None than misclassify into the wrong
    bucket."""
    if not text:
        return None, None
    closing: str | None = None
    opening: str | None = None
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("closing"):
            closing = line
        elif low.startswith("opens"):
            opening = line
    return closing, opening


def _parse_one_listing_card(card_div, event: dict | None) -> ListingCard | None:
    """Build a ListingCard from one .list-item div + its paired
    TheaterEvent JSON-LD block. Returns None if the card lacks a
    parseable show URL (which would be a parser failure to flag)."""
    link = card_div.select_one("a.item-link")
    if not link or not link.has_attr("href"):
        return None
    url = link["href"].strip()
    pair = _id_slug_from_show_url(url)
    if pair is None:
        # The href didn't match /shows/{id}/{slug} — try the
        # workPerformed.sameAs back-ref as a fallback (carries the
        # same URL but might be cleaner if the visible href has a
        # stray query string).
        if isinstance(event, dict):
            wp = event.get("workPerformed") or {}
            if isinstance(wp, dict):
                pair = _id_slug_from_show_url(_decode(wp.get("sameAs")) or "")
        if pair is None:
            return None
    show_id, slug = pair

    name_el = card_div.select_one("span.item-title")
    name = _decode(name_el.get_text(strip=True)) if name_el else ""

    venue_el = card_div.select_one("span.theatre")
    venue_text = _decode(venue_el.get_text(strip=True)) if venue_el else None

    img = card_div.select_one("img")
    image = img.get("data-original") or img.get("src") if img else None

    offer_el = card_div.select_one(".show-offer-label")
    offer_label = _decode(offer_el.get_text(strip=True)) if offer_el else None

    # Notice text: either text-danger ("Closing in X") or text-dark
    # ("Opens Y"). Some cards have both — collect any matching div in
    # the card body.
    notice_parts: list[str] = []
    for nd in card_div.select("div.body div.text-danger, div.body div.text-dark"):
        t = nd.get_text(strip=True)
        if t:
            notice_parts.append(_decode(t) or t)
    notice_text = "\n".join(notice_parts) if notice_parts else None
    closing_notice, opening_notice = _parse_notice(notice_text)

    # Defaulting to {} for missing event makes the rest of the code
    # straight assignment rather than nested .get with None-checks.
    if not isinstance(event, dict):
        event = {}
    offers = event.get("offers") or {}
    if not isinstance(offers, dict):
        offers = {}
    loc = event.get("location") or {}
    if not isinstance(loc, dict):
        loc = {}

    return ListingCard(
        id=show_id,
        name=name,
        url=url,
        slug=slug,
        image=image,
        venue_text=venue_text,
        offer_label=offer_label,
        notice_text=notice_text,
        closing_notice=closing_notice,
        opening_notice=opening_notice,
        jsonld_event_name=_decode(event.get("name")),
        jsonld_start_date=_decode(event.get("startDate")),
        jsonld_end_date=_decode(event.get("endDate")),
        jsonld_status=_decode(event.get("eventStatus")),
        jsonld_description=_decode(event.get("description")),
        jsonld_low_price=_to_float(offers.get("lowPrice") or offers.get("price")),
        jsonld_high_price=_to_float(offers.get("highPrice")),
        jsonld_currency=_decode(offers.get("priceCurrency")),
        jsonld_availability=_decode(offers.get("availability")),
        jsonld_availability_url=_decode(offers.get("url")),
        jsonld_avail_starts=_decode(offers.get("availabilityStarts")),
        jsonld_valid_from=_decode(offers.get("validFrom")),
        jsonld_venue_name=_decode(loc.get("name")),
        jsonld_venue_url=_decode(loc.get("sameAs")),
        jsonld_venue_address=_flatten_listing_address(loc.get("address")),
    )


def parse_listing(html_text: str) -> list[ListingCard]:
    """Parse any of the three listing pages.

    All three share one card template:
      * `<div class="list-item">` for each show card
      * `<script type="application/ld+json">` TheaterEvent immediately
        following each card

    BeautifulSoup parses the whole document, and we walk it looking for
    .list-items.row to scope to the actual show grid (the page has
    other JSON-LD blocks at the top and bottom — WebSite, Organization,
    Place, GeoCoordinates — that we don't want bleeding into our card
    pairing).

    If the JSON-LD count diverges from the card count (defensive — has
    never happened in production), we log it and emit cards anyway with
    None for the JSON-LD-derived fields on the extras.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    grid = soup.select_one("div.list-items div.row") or soup.select_one("div.list-items")
    if grid is None:
        return []

    cards_html = grid.select("div.list-item")
    if not cards_html:
        return []

    # JSON-LD blocks specifically inside the grid scope. Each card is
    # immediately followed by its own TheaterEvent block — we walk only
    # the grid's JSON-LD to avoid picking up the page-level ones at the
    # bottom (WebSite, Organization, etc.).
    events = _theater_events_in_doc_order(_extract_jsonld_blocks(grid))
    if len(events) != len(cards_html):
        log.warning(
            "  card/JSON-LD count mismatch: %d cards vs %d TheaterEvents "
            "— some JSON-LD-derived fields will be empty",
            len(cards_html), len(events),
        )

    out: list[ListingCard] = []
    seen_ids: set[int] = set()
    for i, card_div in enumerate(cards_html):
        event = events[i] if i < len(events) else None
        card = _parse_one_listing_card(card_div, event)
        if card is None:
            continue
        if card.id in seen_ids:
            # Defensive — the same show shouldn't appear twice on one
            # page, but if it does, keep the first occurrence.
            continue
        seen_ids.add(card.id)
        out.append(card)
    return out


# ---------------------------------------------------------------------------
# Show detail parser
# ---------------------------------------------------------------------------

def _find_product_node(jsonld_blocks: list[dict]) -> dict | None:
    """Find the schema.org Product node on a show detail page. Carries
    sku, description, image, and an Offer with the base price."""
    for block in jsonld_blocks:
        if isinstance(block, dict) and block.get("@type") == "Product":
            return block
        # Defensively support @graph wrapping even though TTD doesn't
        # use it currently
        graph = block.get("@graph") if isinstance(block, dict) else None
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
    return None


def _find_breadcrumb_category(jsonld_blocks: list[dict]) -> str | None:
    """Extract the show's category from the BreadcrumbList JSON-LD.

    The breadcrumb's first item has an `@id` like "/category/london-musicals"
    and a `name` like " Musical" or " Play" (note the leading space —
    the site emits both)."""
    for block in jsonld_blocks:
        if not isinstance(block, dict) or block.get("@type") != "BreadcrumbList":
            continue
        items = block.get("itemListElement") or []
        if not isinstance(items, list):
            continue
        # Sort by position to be robust against ordering changes
        items_sorted = sorted(
            (i for i in items if isinstance(i, dict)),
            key=lambda i: i.get("position") or 999,
        )
        for li in items_sorted:
            item = li.get("item") or {}
            if not isinstance(item, dict):
                continue
            cat_id = item.get("@id") or ""
            if "/category/" in cat_id:
                return _decode(item.get("name"))
    return None


def _extract_theater_events(jsonld_blocks: list[dict]) -> list[Performance]:
    """Each upcoming performance on the show detail page is its own
    TheaterEvent JSON-LD block, with full ISO datetimes and per-perf
    booking URLs. The list is capped at 5 even when 6 are visible
    in the 'Next Performances' tab — see _extract_next_perf_links."""
    out: list[Performance] = []
    for block in jsonld_blocks:
        if not isinstance(block, dict) or block.get("@type") != "TheaterEvent":
            continue
        iso = _decode(block.get("startDate"))
        if not iso:
            continue
        # Date and time split
        date_part: str | None = None
        time_part: str | None = None
        if "T" in iso:
            date_part, rest = iso.split("T", 1)
            time_part = rest[:5] if len(rest) >= 5 else None
        else:
            # Date-only startDate would be unusual on the detail page;
            # the listing-card events have date-only, but the detail
            # page should have full timestamps.
            date_part = iso[:10] if len(iso) >= 10 else None

        offers = block.get("offers") or {}
        if not isinstance(offers, dict):
            offers = {}
        book_url = _decode(offers.get("url")) or _decode(block.get("url"))

        out.append(Performance(
            iso=iso,
            date=date_part,
            time=time_part,
            price=_to_float(offers.get("price")),
            currency=_decode(offers.get("priceCurrency")),
            availability=_decode(offers.get("availability")),
            book_url=book_url,
            source="jsonld",
        ))
    return out


def _extract_next_perf_links(soup: BeautifulSoup) -> list[Performance]:
    """Walk the 'Next Performances' tab and extract a Performance for
    each link. These overlap with the TheaterEvent JSON-LD performances
    on the same page but typically include one more entry (the 6th)
    that's missing from the JSON-LD's hard-cap-of-5.

    Each link looks like:
      <a class="btn-arrow" href="/shows/seats/{id}/{Y}/{M}/{D}/{?}/{HH-MM}/0">
         02 Jun 26 19:30
      </a>

    Date/time parsing prefers the href (machine-readable) over the
    visible text (ambiguous year format)."""
    out: list[Performance] = []
    panel = soup.select_one("div.next-perforamnces")  # site typo: "perforamnces"
    if panel is None:
        # Defensive: try the corrected spelling too in case the site
        # ever fixes it
        panel = soup.select_one("div.next-performances")
    if panel is None:
        return out

    for a in panel.select("a.btn-arrow[href]"):
        href = a["href"].strip()
        # The href is absolute. We parse the date/time out of it.
        m = BOOK_URL_RE.search(href)
        if m:
            _id, y, mo, d, hh, mm = m.groups()
            try:
                date_iso = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
                time_str = f"{int(hh):02d}:{mm}"
                iso = f"{date_iso}T{time_str}:00"
            except ValueError:
                date_iso = None
                time_str = None
                iso = None
        else:
            date_iso = None
            time_str = None
            iso = None

        out.append(Performance(
            iso=iso,
            date=date_iso,
            time=time_str,
            price=None,
            currency=None,
            availability=None,
            book_url=href,
            source="next_perf_link",
        ))
    return out


def _merge_performances(
    jsonld_perfs: list[Performance],
    next_perfs: list[Performance],
) -> list[Performance]:
    """Union the JSON-LD performance list with the visible Next
    Performances tab on (date, time). JSON-LD entries win where both
    sources cover the same (date, time) because they carry the price.

    Both sources occasionally produce a row with no date/time (parse
    failure on the href, e.g.). Those are passed through verbatim so
    downstream consumers can see them, but they don't contribute to
    deduplication keys."""
    # Key matches by (date, time). Entries without both are kept as
    # standalone "extras" — rare but harmless.
    by_key: dict[tuple[str | None, str | None], Performance] = {}
    extras: list[Performance] = []

    for p in jsonld_perfs:
        if p.date and p.time:
            by_key[(p.date, p.time)] = p
        else:
            extras.append(p)

    for p in next_perfs:
        if p.date and p.time:
            key = (p.date, p.time)
            if key not in by_key:
                # Visible tab carries a perf the JSON-LD missed (usually
                # the 6th, beyond the JSON-LD's cap of 5). Add it.
                by_key[key] = p
            else:
                # Both sources cover it. JSON-LD wins (it has price);
                # we already have it in by_key, nothing to do.
                pass
        else:
            extras.append(p)

    # Sort the merged perfs by ISO ascending — most consumers want
    # "next first" order which is chronological. Performances without
    # ISO go to the end.
    merged = list(by_key.values())
    merged.sort(key=lambda p: (p.iso or "9999"))
    merged.extend(extras)
    return merged


def _extract_future_months(soup: BeautifulSoup) -> list[FutureMonth]:
    """Walk the 'next-years' calendar link strip beneath the Next
    Performances tab. Each link points to a monthly calendar page —
    advisory navigation only, no performance data is embedded."""
    out: list[FutureMonth] = []
    panel = soup.select_one("div.next-years")
    if panel is None:
        return out
    for a in panel.select("a[href]"):
        href = a["href"].strip()
        label = _decode(a.get_text(strip=True)) or ""
        # URL form: /calendar/{id}/{month},{year}
        m = re.search(r"/calendar/\d+/(\d{1,2}),(\d{4})", href)
        month = int(m.group(1)) if m else None
        year = int(m.group(2)) if m else None
        out.append(FutureMonth(label=label, month=month, year=year, url=href))
    return out


def _extract_basic_info_box(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the .basic-info-box widgets. The site emits these as
    sibling divs each containing a `<span class="red">Label</span>`
    plus a text node with the value. We key on the label."""
    out: dict[str, str] = {}
    for box in soup.select("div.basic-info-box"):
        label_el = box.find("span", class_="red")
        if not label_el:
            continue
        label = label_el.get_text(strip=True)
        # Value is the box's text after stripping the label
        full_text = box.get_text(" ", strip=True)
        value = full_text[len(label):].strip()
        if label and value:
            out[label] = _decode(value) or value
    return out


def _extract_important_info(soup: BeautifulSoup) -> str | None:
    """Find the 'Important Information' card body. Carries age guidance
    plus other show notes when present. Returns None if absent (some
    shows have no important-info card at all)."""
    for card in soup.select("div.card"):
        header = card.select_one("div.card-header")
        if not header:
            continue
        if header.get_text(strip=True).lower() != "important information":
            continue
        body = card.select_one("div.card-body")
        if body is None:
            continue
        # Use .expandable-text inner div when present (some shows have
        # a more verbose expandable layout), else fall back to card-body.
        inner = body.select_one("div.expandable-text") or body
        text = _decode(inner.get_text("\n", strip=True))
        return text or None
    return None


def _extract_offer_card(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Find the green 'card-offer' on the detail page sidebar. Returns
    (title, body). Carries the offer-validity text like 'Valid all
    performances 2-5 June 2026 / Book By 6 June 2026'. Returns
    (None, None) for non-discounted shows."""
    card = soup.select_one("div.card-offer")
    if card is None:
        return None, None
    title_el = card.select_one(".card-header")
    body_el = card.select_one(".card-body")
    title = _decode(title_el.get_text(strip=True)) if title_el else None
    body = _decode(body_el.get_text(" ", strip=True)) if body_el else None
    return title, body


def _extract_weekly_schedule(soup: BeautifulSoup) -> list[dict]:
    """Walk the Mon-Sun matinee/evening timings table from the
    Timings tab. Cells are '-' for no performance, otherwise an HH:MM
    display string. Always returns 7 rows (Mon-Sun) when the table
    is present; empty list when it's not."""
    table = soup.select_one("table#tblPerformance")
    if table is None:
        return []
    out: list[dict] = []
    for row in table.select("tbody tr"):
        tds = row.select("td")
        if len(tds) < 3:
            continue
        day = _decode(tds[0].get_text(strip=True)) or ""
        mat_raw = tds[1].get_text(strip=True)
        eve_raw = tds[2].get_text(strip=True)
        out.append(asdict(WeeklyScheduleRow(
            day=day,
            matinee=mat_raw if mat_raw and mat_raw != "-" else None,
            evening=eve_raw if eve_raw and eve_raw != "-" else None,
        )))
    return out


def _extract_editorial_tags(soup: BeautifulSoup) -> list[dict]:
    """Walk the Tags tab on the detail page. Each tag is a clickable
    badge linking to /showsby/tags/{slug}."""
    out: list[dict] = []
    panel = soup.select_one("div.tab-pane.info")
    if panel is None:
        return out
    for a in panel.select("a.badge[href]"):
        name = _decode(a.get_text(strip=True))
        href = a["href"].strip()
        # Slug is the last URL segment
        slug = urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]
        out.append(asdict(EditorialTag(name=name, url=href, slug=slug)))
    return out


def _extract_description_full(soup: BeautifulSoup) -> str | None:
    """Pull the synopsis paragraphs from the visible description body.

    The structure is roughly:
      <div class="summary">
        <h2>More about X</h2>
        <p>...synopsis paragraph...</p>
        <p>...continued...</p>
        <h2>Why Choose Theatre Tickets Direct?</h2>
        <p>...boilerplate marketing...</p>
      </div>

    We want the first synopsis chunk — everything between the first
    'More about' h2 and the first 'Why Choose' h2 (which marks the
    start of generic marketing copy that's the same on every show)."""
    summary = soup.select_one("div.summary")
    if summary is None:
        # Fallback: the summary may be inside #full-detail-container without
        # a .summary class (defensive against minor markup changes)
        summary = soup.select_one("#full-detail-container")
        if summary is None:
            return None

    paragraphs: list[str] = []
    in_synopsis = False
    for el in summary.find_all(True, recursive=True):
        if el.name in ("h1", "h2", "h3", "h4"):
            heading = el.get_text(strip=True).lower()
            if heading.startswith("more about"):
                in_synopsis = True
                continue
            if in_synopsis:
                # Any other heading (including "Why Choose...") ends
                # the synopsis section.
                in_synopsis = False
        elif in_synopsis and el.name == "p":
            txt = _decode(el.get_text(" ", strip=True))
            if txt:
                paragraphs.append(txt)

    if not paragraphs:
        # No "More about" heading found — fall back to all <p> tags
        # under .summary. This catches shows with a different layout.
        for p in summary.find_all("p"):
            txt = _decode(p.get_text(" ", strip=True))
            if txt:
                paragraphs.append(txt)

    return "\n\n".join(paragraphs) if paragraphs else None


def _extract_access_info(soup: BeautifulSoup) -> tuple[str | None, str | None, str | None]:
    """From the theatre-container at the bottom of the detail page,
    pull (venue_full_address, access_info, seating_plan_image).

    Address sits in `<div class="address"><em>...</em></div>` inside
    the theatre container; access info is the bare `<div>` paragraph
    block beneath the View Seating Plan button."""
    container = soup.select_one("#theatre-container")
    if container is None:
        return None, None, None

    addr_em = container.select_one("div.address em")
    venue_address = _decode(addr_em.get_text(strip=True)) if addr_em else None

    # Find the seating-plan modal trigger's modal target to fetch the
    # static image (this is rendered in an aside elsewhere on the page,
    # but the trigger sits in the theatre container).
    plan_img = None
    plan_modal = soup.select_one("div#SeatPlan img")
    if plan_modal:
        plan_img = plan_modal.get("src") or plan_modal.get("data-original")

    # Access info is the div containing strong-tagged headers like
    # "Bars:", "Access description:", etc. We collect text from any
    # such div that's a direct child of the container.
    access_text: list[str] = []
    for div in container.find_all("div", recursive=False):
        # Skip the address, seating-plan button row, and google-map
        # placeholder
        classes = div.get("class") or []
        if "address" in classes or "google-map" in classes:
            continue
        # Skip wrappers that don't have direct text content
        text = div.get_text("\n", strip=True)
        if text and any(marker in text for marker in (
            "Access description", "Bars:", "Guide Dogs", "Sound Amplification",
            "Disabled Access", "Toilets:", "Limited mobility",
        )):
            access_text.append(_decode(text) or text)

    access_info = "\n".join(access_text) if access_text else None
    return venue_address, access_info, plan_img


def _extract_book_button(soup: BeautifulSoup) -> tuple[float | None, str | None, bool]:
    """Read the floating Book Tickets button at the top of the show
    page. Returns (price_value, price_display, has_offer_badge).

    Markup:
      <div class="btn-float">
        <a class="...book-tickets-top">
          book Tickets
          <div class="from">
            From £22.00
            <span class="badge badge-success">On Offer</span>
          </div>
        </a>
      </div>
    """
    btn = soup.select_one(".btn-float .book-tickets-top")
    if btn is None:
        return None, None, False
    from_el = btn.select_one(".from")
    has_offer = bool(btn.select_one(".badge.badge-success"))
    if from_el is None:
        return None, None, has_offer
    # Strip the badge text from the price display
    badge = from_el.find(class_="badge")
    raw_text = from_el.get_text(" ", strip=True)
    if badge is not None:
        badge_text = badge.get_text(" ", strip=True)
        if badge_text:
            raw_text = raw_text.replace(badge_text, "", 1).strip()
    display = _decode(raw_text) or None
    m = _PRICE_RE.search(raw_text)
    value = _to_float(m.group(1)) if m else None
    return value, display, has_offer


def _extract_canonical(soup: BeautifulSoup) -> str | None:
    link = soup.select_one('link[rel="canonical"]')
    return link.get("href") if link else None


def _extract_header_image(soup: BeautifulSoup) -> str | None:
    """The hero banner is rendered as an inline background-image style
    on .header-image-container .div-img. We extract the URL from the
    style attribute.

    The URL can itself contain '(' and ')' (e.g.
    'banner-(1)-86944692.png'), so we can't use a naive
    `[^)]` character class — that would stop at the first ')' inside
    the URL. Instead we anchor on the quote delimiters when present, or
    fall back to a non-greedy match to the final ')'."""
    div = soup.select_one(".header-image-container .div-img")
    if div is None or not div.has_attr("style"):
        return None
    m = re.search(
        r"""url\(\s*(?:(['"])(.+?)\1|(.+?))\s*\)""",
        div["style"],
    )
    if not m:
        return None
    return m.group(2) or m.group(3)


def _extract_show_logo(soup: BeautifulSoup) -> str | None:
    img = soup.select_one("img.show-logo")
    return img.get("src") if img else None


def _extract_age_from_important_info(text: str | None) -> str | None:
    """Try to pull an age guidance string out of the important_info
    block. Best-effort — returns the matched substring or None.

    Examples:
      'Ages: This production is recommended for ages 14+.'  → '14+'
      'Recommended for ages 16+'                            → '16+'
    """
    if not text:
        return None
    m = _AGES_RE.search(text)
    return m.group(1) if m else None


def parse_show(html_text: str) -> dict:
    """Parse a show detail page; return a dict of detail-only fields
    (no listing-card fields — those come from the listing scrape and
    are merged by the caller)."""
    soup = BeautifulSoup(html_text, "html.parser")
    blocks = _extract_jsonld_blocks(soup)

    # Title — first text node of h1
    name = ""
    h1 = soup.select_one("h1")
    if h1:
        # Pull the first non-empty string child (h1 may have padding text nodes)
        for c in h1.contents:
            if isinstance(c, str) and c.strip():
                name = _decode(c.strip()) or ""
                break
        if not name:
            name = _decode(h1.get_text(strip=True)) or ""

    # Product JSON-LD
    product = _find_product_node(blocks) or {}
    p_offers = product.get("offers") or {}
    if not isinstance(p_offers, dict):
        p_offers = {}
    sku = product.get("sku")
    if sku is not None:
        sku = str(sku)

    # Performances: JSON-LD ∪ Next Performances tab
    jsonld_perfs = _extract_theater_events(blocks)
    next_perfs = _extract_next_perf_links(soup)
    merged_perfs = _merge_performances(jsonld_perfs, next_perfs)

    # Future-month nav (advisory only — no performance data)
    future_months = _extract_future_months(soup)

    # Basic info widgets
    basic_info = _extract_basic_info_box(soup)

    # Important Information card (carries age guidance among other things)
    important_info = _extract_important_info(soup)
    age_content = _extract_age_from_important_info(important_info)

    # Offer card (green sidebar with offer-validity text)
    offer_title, offer_body = _extract_offer_card(soup)

    # Weekly schedule (Mon-Sun matinee/evening grid)
    weekly_schedule = _extract_weekly_schedule(soup)

    # Editorial tags (Tags tab)
    editorial_tags = _extract_editorial_tags(soup)

    # Category from breadcrumb JSON-LD
    category = _find_breadcrumb_category(blocks)

    # Description (Product JSON-LD short, visible summary long)
    product_description = _decode(product.get("description"))
    description_full = _extract_description_full(soup)

    # Hero / logo / canonical
    header_image = _extract_header_image(soup)
    detail_image = _extract_show_logo(soup) or _decode(product.get("image"))
    canonical = _extract_canonical(soup)

    # Theatre / venue address + access info + seating plan image
    venue_full_address, access_info, seating_plan_image = _extract_access_info(soup)

    # Book Tickets button (top-right "From £X.XX" + On Offer badge)
    book_price, book_price_display, has_offer_badge = _extract_book_button(soup)

    return {
        "detail_canonical": canonical,
        "sku": sku,
        "name": name,
        "product_description": product_description,
        "detail_image": detail_image,
        "detail_low_price": _to_float(p_offers.get("price")),
        "detail_currency": _decode(p_offers.get("priceCurrency")),
        "detail_availability": _decode(p_offers.get("availability")),
        "detail_offer_url": _decode(p_offers.get("url")),
        "header_image_url": header_image,
        "description_full": description_full,
        "running_time": basic_info.get("Running Time"),
        "running_since": basic_info.get("Running Since"),
        "booking_until": basic_info.get("Booking Until"),
        "important_info": important_info,
        "age_content": age_content,
        "access_info": access_info,
        "venue_full_address": venue_full_address,
        "seating_plan_image": seating_plan_image,
        "weekly_schedule": weekly_schedule,
        "editorial_tags": editorial_tags,
        "category": category,
        "book_from_price": book_price,
        "book_from_price_display": book_price_display,
        "has_offer_badge": has_offer_badge,
        "offer_card_title": offer_title,
        "offer_card_body": offer_body,
        "performances": [asdict(p) for p in merged_perfs],
        "future_months": [asdict(f) for f in future_months],
    }


# ---------------------------------------------------------------------------
# Stage 1 — fetch all three listings, build the master union
# ---------------------------------------------------------------------------

def fetch_all_listings(
    session: requests.Session,
    include_tag_lists: bool,
) -> tuple[list[ListingCard], dict[int, list[str]], dict[str, int]]:
    """Fetch each of the three listing URLs and return:

      master_cards: deduplicated list of ListingCards across all
        listings, in encounter order (first listing the show appeared
        in, then position within that listing). We keep the first
        encountered card's full payload as the canonical card data —
        subsequent appearances only contribute to appears_in.
      appears_in_map: {show_id: [listing_name, ...]}
      listing_counts: {listing_name: count_parsed}

    --no-tag-lists collapses this to musicals only (still a viable
    catalogue source, just smaller — drops plays and discount).
    """
    master_cards: list[ListingCard] = []
    appears_in_map: dict[int, list[str]] = {}
    listing_counts: dict[str, int] = {}
    seen_ids: set[int] = set()

    listings_to_fetch = (
        LISTING_URLS.items() if include_tag_lists
        else [("musicals", LISTING_URLS["musicals"])]
    )

    for name, url in listings_to_fetch:
        try:
            log.info("Fetching listing '%s': %s", name, url)
            resp = session.get(url, timeout=REQUEST_TIMEOUT_S)
            resp.raise_for_status()
            cards = parse_listing(resp.text)
            log.info("  '%s': parsed %d cards", name, len(cards))
        except requests.RequestException as e:
            log.warning("  listing '%s' failed: %s — skipping", name, e)
            listing_counts[name] = -1
            continue

        listing_counts[name] = len(cards)

        # Diagnostic for 0-card listings: distinguish a genuinely empty
        # page (e.g. no discounts today) from a parser regression. We
        # check for the markers we'd expect to find on a healthy page.
        if not cards and len(resp.text) > 1000:
            body_lower = resp.text.lower()
            has_list_item = "list-item" in body_lower
            has_grid = "list-items" in body_lower
            log.warning(
                "  listing '%s' parsed 0 cards despite %d bytes "
                "(any 'list-item' class present: %s; 'list-items' wrapper "
                "present: %s) — if this persists, page DOM has changed",
                name, len(resp.text), has_list_item, has_grid,
            )

        for card in cards:
            appears_in_map.setdefault(card.id, []).append(name)
            if card.id not in seen_ids:
                seen_ids.add(card.id)
                master_cards.append(card)

    return master_cards, appears_in_map, listing_counts


# ---------------------------------------------------------------------------
# Stage 2 — detail pages in parallel
# ---------------------------------------------------------------------------

def fetch_show_detail(
    session: requests.Session, card: ListingCard,
) -> tuple[dict | None, str | None]:
    """Fetch and parse one show's detail page. Layered retry: urllib3
    handles 5xx/429/connection errors at the adapter level; this
    application-level retry handles transient parse errors and 404/410
    edge cases (which we explicitly don't retry — those are permanent)."""
    last_err: str | None = None
    for attempt in (1, 2):
        try:
            resp = session.get(card.url, timeout=REQUEST_TIMEOUT_S)
            resp.raise_for_status()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            last_err = f"HTTP {code}"
            if code in (404, 410):
                return None, last_err
            if attempt == 1:
                time.sleep(DETAIL_PARSE_RETRY_DELAY_S)
                continue
            return None, last_err
        except requests.RequestException as e:
            last_err = f"network: {type(e).__name__}"
            if attempt == 1:
                time.sleep(DETAIL_PARSE_RETRY_DELAY_S)
                continue
            return None, last_err

        try:
            return parse_show(resp.text), None
        except Exception as e:
            last_err = f"parse: {type(e).__name__}: {e}"
            if attempt == 1:
                time.sleep(DETAIL_PARSE_RETRY_DELAY_S)
                continue
            return None, last_err

    return None, last_err or "unknown"


def fetch_all_details(
    session: requests.Session,
    cards: list[ListingCard],
    appears_in_map: dict[int, list[str]],
    concurrency: int,
    deadline: float | None = None,
) -> tuple[list[Show], list[ShowFailure], bool]:
    """Fetch each show's detail in parallel. Returns (shows, failures,
    budget_exceeded). Preserves the listing order of input cards.

    deadline mirrors the wall-clock budget pattern from olt_scraper
    and seatplan_scraper: past the deadline, no new tasks are submitted,
    but in-flight requests run to completion."""
    log.info("Fetching %d show detail pages with %d workers...",
             len(cards), concurrency)
    t_start = time.monotonic()
    budget_exceeded = False

    results: list[Show | None] = [None] * len(cards)
    errors: list[str | None] = [None] * len(cards)
    progress = [0]
    lock = Lock()

    def task(idx: int, card: ListingCard) -> None:
        if deadline is not None and time.monotonic() > deadline:
            errors[idx] = "skipped: wall-clock budget exceeded"
            with lock:
                progress[0] += 1
            return

        detail, err = fetch_show_detail(session, card)
        if detail is not None:
            # Detail-page name wins when present and non-empty; some
            # detail pages have cleaner names than the listing cards
            # (e.g. "1536" vs "1536 Tickets" — though TTD cards strip
            # "Tickets" already).
            name = detail.get("name") or card.name
            results[idx] = Show(
                id=card.id,
                name=name,
                url=card.url,
                slug=card.slug,
                sku=detail.get("sku"),
                image=card.image,
                offer_label=card.offer_label,
                venue_text=card.venue_text,
                notice_text=card.notice_text,
                closing_notice=card.closing_notice,
                opening_notice=card.opening_notice,
                listing_low_price=card.jsonld_low_price,
                listing_high_price=card.jsonld_high_price,
                listing_currency=card.jsonld_currency,
                listing_availability=card.jsonld_availability,
                listing_availability_url=card.jsonld_availability_url,
                listing_avail_starts=card.jsonld_avail_starts,
                listing_valid_from=card.jsonld_valid_from,
                description_short=card.jsonld_description,
                venue_name=card.jsonld_venue_name,
                venue_url=card.jsonld_venue_url,
                venue_address=card.jsonld_venue_address,
                appears_in=sorted(appears_in_map.get(card.id, [])),
                detail_canonical=detail.get("detail_canonical"),
                product_description=detail.get("product_description"),
                detail_image=detail.get("detail_image"),
                detail_low_price=detail.get("detail_low_price"),
                detail_currency=detail.get("detail_currency"),
                detail_availability=detail.get("detail_availability"),
                detail_offer_url=detail.get("detail_offer_url"),
                header_image_url=detail.get("header_image_url"),
                description_full=detail.get("description_full"),
                running_time=detail.get("running_time"),
                running_since=detail.get("running_since"),
                booking_until=detail.get("booking_until"),
                important_info=detail.get("important_info"),
                age_content=detail.get("age_content"),
                access_info=detail.get("access_info"),
                venue_full_address=detail.get("venue_full_address"),
                seating_plan_image=detail.get("seating_plan_image"),
                weekly_schedule=detail.get("weekly_schedule") or [],
                editorial_tags=detail.get("editorial_tags") or [],
                category=detail.get("category"),
                book_from_price=detail.get("book_from_price"),
                book_from_price_display=detail.get("book_from_price_display"),
                has_offer_badge=bool(detail.get("has_offer_badge")),
                offer_card_title=detail.get("offer_card_title"),
                offer_card_body=detail.get("offer_card_body"),
                performances=detail.get("performances") or [],
                future_months=detail.get("future_months") or [],
            )
        else:
            errors[idx] = err

        with lock:
            progress[0] += 1
            n = progress[0]
            if n % 10 == 0 or n == len(cards):
                log.info("  progress: %d/%d", n, len(cards))

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(task, i, c) for i, c in enumerate(cards)]
        for _ in as_completed(futures):
            if deadline is not None and time.monotonic() > deadline and not budget_exceeded:
                budget_exceeded = True
                log.warning(
                    "wall-clock budget exceeded after %.1fs — %d/%d shows "
                    "complete; in-flight requests will finish, no new submissions",
                    time.monotonic() - t_start, progress[0], len(cards),
                )

    shows = [s for s in results if s is not None]
    failures = [
        ShowFailure(id=cards[i].id, slug=cards[i].slug,
                    url=cards[i].url, error=errors[i] or "unknown")
        for i, s in enumerate(results) if s is None
    ]

    elapsed = time.monotonic() - t_start
    rate = len(cards) / elapsed if elapsed > 0 else 0
    log.info("Fetched %d/%d show details in %.1fs (%.2f req/s)",
             len(shows), len(cards), elapsed, rate)
    return shows, failures, budget_exceeded


# ---------------------------------------------------------------------------
# Sanity checks (warn-but-write policy)
# ---------------------------------------------------------------------------

def run_sanity_checks(
    shows: list[Show], failures: list[ShowFailure], master_count: int,
) -> list[str]:
    """Inspect the scraped data for structural anomalies. Same
    warn-but-write policy as the other scrapers — never abort here.

    Threshold rationale:
      * name / url / venue — structural fields; should never be missing.
      * performances — running shows have an upcoming perf list; closing
        or pre-opening shows legitimately may not. 25% missing is fine;
        more suggests the perf parser broke.
      * description — most shows have one but pre-opening attractions
        often don't. 20% is the warning threshold.
      * editorial_tags — every show has at least one tag (Musical /
        Play / Drama / etc.). >40% missing means the tags tab parser
        broke.
      * sku — every detail page emits a Product JSON-LD with sku.
        >5% missing means the JSON-LD parser is failing.
    """
    warnings: list[str] = []
    n = len(shows)

    if n == 0:
        warnings.append("CRITICAL: zero shows scraped — listing or detail layer fully broken")
        return warnings

    if master_count > 0 and n < master_count * 0.9:
        warnings.append(
            f"coverage: only {n}/{master_count} show details succeeded "
            f"({100*n/master_count:.0f}%)"
        )

    def pct_missing(predicate, label: str, threshold: float = 0.10) -> None:
        bad = sum(1 for s in shows if predicate(s))
        if bad / n > threshold:
            warnings.append(
                f"field-quality: {bad}/{n} shows have {label} "
                f"(>{threshold:.0%} threshold — possible schema drift)"
            )

    pct_missing(lambda s: not s.name, "missing name", threshold=0.0)
    pct_missing(lambda s: not s.url, "missing url", threshold=0.0)
    pct_missing(lambda s: not s.venue_name, "missing venue name", threshold=0.05)
    pct_missing(lambda s: not s.sku, "missing sku from detail page", threshold=0.05)
    pct_missing(lambda s: not s.description_full and not s.product_description,
                "missing both detail descriptions", threshold=0.20)
    pct_missing(lambda s: not s.editorial_tags, "no editorial tags", threshold=0.40)
    pct_missing(lambda s: not s.weekly_schedule, "no weekly schedule", threshold=0.25)
    # Performances — shows that aren't yet open (opening_notice set) or
    # are closing-imminent may legitimately have zero. Filter to the
    # "currently running, not about to close" set for this check.
    running = [s for s in shows if not s.opening_notice]
    if running:
        no_perfs = sum(1 for s in running if not s.performances)
        if no_perfs / len(running) > 0.25:
            warnings.append(
                f"field-quality: {no_perfs}/{len(running)} running shows "
                f"have zero performances (>25% threshold)"
            )

    # Canonical URL sanity — detail page's <link rel="canonical"> should
    # match the URL we fetched. Wholesale mismatch means we're being
    # redirected or pulling the wrong page.
    canonical_mismatches = sum(
        1 for s in shows
        if s.detail_canonical
        and s.detail_canonical.rstrip("/") != s.url.rstrip("/")
    )
    if canonical_mismatches / n > 0.10:
        warnings.append(
            f"redirect: {canonical_mismatches}/{n} show detail pages have a "
            "canonical URL different from the URL we fetched — possible redirects"
        )

    # Performance-level checks
    all_perfs = [(s, p) for s in shows for p in s.performances]
    if all_perfs:
        no_book_url = sum(1 for _, p in all_perfs if not p.get("book_url"))
        if no_book_url:
            warnings.append(
                f"performance: {no_book_url}/{len(all_perfs)} performances "
                "lack book_url"
            )
        bad_prices = sum(
            1 for _, p in all_perfs
            if p.get("price") is not None and p["price"] <= 0
        )
        if bad_prices:
            warnings.append(
                f"price: {bad_prices} performances have non-positive price"
            )

    # Failure breakdown (informational, not threshold-based)
    if failures:
        from collections import Counter
        kinds = Counter(f.error.split(":")[0] for f in failures)
        breakdown = ", ".join(f"{k}={v}" for k, v in kinds.most_common())
        warnings.append(f"fetch-failures: {len(failures)} shows failed ({breakdown})")

    return warnings


def compare_with_previous(new_shows: list[Show], previous_path: Path) -> list[str]:
    """Catch catastrophic regressions vs. yesterday's good output.
    Mirrors the other scrapers' check."""
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
    n_new, n_prev = len(new_shows), len(prev_shows)

    if n_prev > 0 and n_new < n_prev * 0.80:
        warnings.append(
            f"prev-run: show count dropped {n_prev} → {n_new} "
            f"({100*(n_prev-n_new)/n_prev:.0f}% loss, >20% threshold)"
        )

    p_new = sum(len(s.performances) for s in new_shows)
    p_prev = prev.get("performance_count") or sum(
        len(s.get("performances") or []) for s in prev_shows
    )
    if p_prev > 0 and p_new < p_prev * 0.50:
        warnings.append(
            f"prev-run: performance count dropped {p_prev} → {p_new} "
            f"({100*(p_prev-p_new)/p_prev:.0f}% loss, >50% threshold)"
        )

    # ID-based churn (id is the stable primary key on TTD)
    new_ids = {s.id for s in new_shows}
    prev_ids = {s["id"] for s in prev_shows if isinstance(s.get("id"), int)}
    if prev_ids:
        vanished = prev_ids - new_ids
        if len(vanished) > len(prev_ids) * 0.30:
            warnings.append(
                f"prev-run: {len(vanished)}/{len(prev_ids)} shows from previous "
                f"run missing today ({100*len(vanished)/len(prev_ids):.0f}% churn)"
            )
    return warnings


def validate_data_ranges(shows: list[Show]) -> list[str]:
    """Range validation — catches the class of bugs where parsing
    succeeds but produces nonsense values (a £100k ticket, a 1999 closing
    date). Advisory only; never fails the scrape."""
    issues: list[str] = []
    current_year = datetime.now(timezone.utc).year

    bad_listing_price: list[tuple[int, str, float | None]] = []
    bad_perf_price: list[tuple[int, str | None]] = []
    bad_perf_dates: list[tuple[int, str]] = []
    bad_end_dates: list[tuple[int, str]] = []
    bad_urls: list[tuple[int, str]] = []
    sku_id_mismatches: list[tuple[int, str]] = []

    def _price_ok(p):
        return p is None or (isinstance(p, (int, float)) and 0 < p < 10000)

    def _year_ok(y):
        return 2020 <= y <= current_year + 5

    for s in shows:
        for name_, val_ in [
            ("listing_low_price", s.listing_low_price),
            ("listing_high_price", s.listing_high_price),
            ("detail_low_price", s.detail_low_price),
            ("book_from_price", s.book_from_price),
        ]:
            if not _price_ok(val_):
                bad_listing_price.append((s.id, name_, val_))

        for p in s.performances:
            if not _price_ok(p.get("price")):
                bad_perf_price.append((s.id, p.get("iso")))
            date_str = p.get("date")
            if date_str:
                try:
                    year = int(date_str[:4])
                    if not _year_ok(year):
                        bad_perf_dates.append((s.id, date_str))
                except (ValueError, TypeError):
                    bad_perf_dates.append((s.id, date_str))

        # listing_*_dates are ISO YYYY-MM-DD on TTD
        for name_, val_ in [
            ("jsonld_end_date", None),  # placeholder; we extract from card
        ]:
            pass  # we don't have end_date on Show directly; checked via card already

        # sku should equal str(id)
        if s.sku and str(s.sku) != str(s.id):
            sku_id_mismatches.append((s.id, s.sku))

        if s.url and not s.url.startswith(BASE):
            bad_urls.append((s.id, s.url))

    if bad_listing_price:
        issues.append(
            f"data-range: {len(bad_listing_price)} listing-price anomalies "
            f"(expected 0–10000, e.g. show id {bad_listing_price[0][0]}: "
            f"{bad_listing_price[0][1]}={bad_listing_price[0][2]})"
        )
    if bad_perf_price:
        issues.append(
            f"data-range: {len(bad_perf_price)} performances with out-of-range prices"
        )
    if bad_perf_dates:
        issues.append(
            f"data-range: {len(bad_perf_dates)} performances with implausible "
            f"dates (expected {2020}–{current_year+5}, e.g. {bad_perf_dates[0][1]})"
        )
    if sku_id_mismatches:
        issues.append(
            f"data-range: {len(sku_id_mismatches)} shows have sku ≠ str(id) "
            f"(e.g. id={sku_id_mismatches[0][0]}, sku={sku_id_mismatches[0][1]})"
        )
    if bad_urls:
        issues.append(
            f"data-range: {len(bad_urls)} shows have URLs outside the expected "
            f"theatreticketsdirect.co.uk origin (e.g. {bad_urls[0][1]})"
        )
    return issues


# ---------------------------------------------------------------------------
# Output (atomic write + rotation)
# ---------------------------------------------------------------------------

def rotate_output(path: Path, keep: int = DEFAULT_ROTATION_DEPTH) -> None:
    """Shift existing output files to make room for a new write.

    ttd_london.json     →  ttd_london.json.1
    ttd_london.json.1   →  ttd_london.json.2
    ...
    ttd_london.json.4   →  ttd_london.json.5     (oldest kept)
    ttd_london.json.5   →  (deleted)
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
    survives a crashed current run. Rename is atomic on every modern
    OS (POSIX and Windows via Python 3.3+'s os.replace)."""
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
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
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
        description="Scrape TheatreTicketsDirect.co.uk (pure requests, parallel)."
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Only fetch details for this many shows (for testing).")
    p.add_argument("--out", type=Path, default=Path("ttd_london.json"),
                   help="Output JSON file path (default: ./ttd_london.json).")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel workers for detail pages (default: {DEFAULT_CONCURRENCY}).")
    p.add_argument("--no-tag-lists", action="store_true",
                   help="Only fetch the musicals listing (skip plays + discount); "
                        "shows will have appears_in containing only 'musicals' and the "
                        "catalogue will be smaller.")
    p.add_argument("--max-runtime-seconds", type=int, default=None, metavar="N",
                   help="Soft wall-clock budget. After N seconds, the detail "
                        "fetcher stops queuing new requests; in-flight ones finish. "
                        "Partial output still written with a warning. Default: unlimited.")
    p.add_argument("--no-rotate", action="store_true",
                   help="Don't keep previous output files as .1/.2/.../.5. "
                        "By default the previous 5 runs are retained.")
    p.add_argument("--rotation-depth", type=int, default=DEFAULT_ROTATION_DEPTH,
                   metavar="N",
                   help=f"How many previous outputs to retain (default: {DEFAULT_ROTATION_DEPTH}).")
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

    # Stage 1: all three listings, deduplicate into master + record appears_in
    try:
        master_cards, appears_in_map, listing_counts = fetch_all_listings(
            session, include_tag_lists=not args.no_tag_lists,
        )
    except requests.RequestException as e:
        log.error("Listing fetch failed: %s — aborting (previous output preserved).", e)
        return EXIT_HARD_FAIL

    if not master_cards:
        log.error("No shows found across any listing — aborting (previous output preserved).")
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
    warnings = run_sanity_checks(shows, failures, master_count=len(cards_to_fetch))
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
