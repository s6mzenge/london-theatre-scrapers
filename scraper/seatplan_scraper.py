"""
SeatPlan London scraper (pure requests, parallel)
=================================================

Scrapes every show on https://seatplan.com/london/ (the master catalogue)
plus five filtered "tag list" views and the global Last Minute table,
then fetches each show's detail page for the rich performance, cast, FAQ
and rating data.

A word on what we DON'T scrape
------------------------------
Two URLs that look like listings but aren't:

  * /london/deals/ — looks like a "best value" tag list, but is actually
    a search form ("Best Seats for Your Money / Find the best London
    theatre deals in one easy search") with no show data on it. Excluded.
  * /london/whats-on/last-minute/ — uses a completely different DOM (a
    Today/Tomorrow `<table class="last-minute__table">`) rather than the
    standard card grid. We parse it separately (see
    `_parse_last_minute_listing`) because it carries per-day pricing,
    booking URLs, and "Price Drops on selected seats" badges that aren't
    surfaced anywhere else.

Why no Playwright?
------------------
SeatPlan is fully server-side rendered. The master listing emits all
~125 production cards in a single response, and — critically — emits a
companion `<script type="application/ld+json">` TheaterEvent block for
each card, in the same DOM order, with rich data: venue, geographic
coordinates, run dates, AggregateOffer price range, description and
typical age range. So one HTTP GET gives us:

  * 125 card HTML chunks (image, title, "from £X.XX", optional rating
    avg, optional "Opens DATE" footer)
  * 125 TheaterEvent JSON-LD blocks paired 1:1 by index

For each show, the detail page (e.g. /london/les-miserables-tickets/)
also SSRs everything:

  * A Product JSON-LD with sku, AggregateRating (ratingCount,
    reviewCount, ratingValue), Offer (priceValidUntil, currency) and
    review samples — the only place full rating counts live.
  * ~9 TheaterEvent JSON-LD blocks, one per upcoming performance, with
    ISO datetimes (e.g. "2026-05-19T19:30:00+01:00") and per-performance
    lowPrice.
  * A FAQPage JSON-LD with the on-page FAQ.
  * "Last Minute Tickets" HTML table — 5-day rolling window with the
    actual booking URLs and showtime IDs (data-id) we need to deep-link
    into the checkout.
  * "Show Times" HTML table — weekly schedule (Mon-Sun, Matinee/Evening).
  * A long description with the cast embedded as
    `<ul><li><strong>Role</strong> - Name</li></ul>`.

The detail-page TheaterEvents and the Last Minute table overlap but
neither subsumes the other. JSON-LD has dates the table doesn't show
(it caps at 5 days); the table has booking URLs and showtime IDs the
JSON-LD doesn't include. We extract both and join on (date, time) to
build a unified performance list.

A note on identity
------------------
SeatPlan does not expose a stable numeric ID on listing cards. The
Product JSON-LD on the detail page does (`sku`, e.g. "44"), but only
appears once we've fetched the detail. The URL slug is the only field
present from listing through detail and is stable across runs, so we
key on it. The SKU is kept as a secondary identifier when available.

A note on duplicates
--------------------
The same show can appear multiple times on the listing if it plays at
multiple venues — e.g. *Jesus Christ Superstar* has separate URLs for
the London Palladium and the Theatre Royal Drury Lane. These are
genuinely different productions (different runs, casts, prices) and
each has its own JSON-LD, so we treat them as distinct shows.

Setup
-----
    pip install requests beautifulsoup4

Usage
-----
    python seatplan_scraper.py                          # full scrape
    python seatplan_scraper.py --limit 5                # test with 5 shows
    python seatplan_scraper.py --out data/seatplan.json # custom output
    python seatplan_scraper.py --concurrency 24         # more workers
    python seatplan_scraper.py --no-tag-lists           # skip tag/last-minute fetches
    python seatplan_scraper.py --dry-run                # don't write

Output is a single JSON file:

    {
      "scraped_at": "2026-05-18T22:08:56+00:00",
      "source": "https://seatplan.com/london/",
      "show_count": 125,
      "performance_count": 870,
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

BASE = "https://seatplan.com"
MASTER_URL = f"{BASE}/london/"

# Five filter slices that reuse the master card template. We only pull
# URLs from them for "appears_in" — the real data depth comes from the
# master + detail pages.
#
# Two URLs you might expect to see here but are deliberately absent:
#   * /london/deals/ — a search form (not a listing); no show cards.
#   * /london/whats-on/last-minute/ — uses a different DOM (a table
#     with per-day pricing). Handled separately via LAST_MINUTE_URL
#     and `_parse_last_minute_listing`, not parse_listing.
TAG_LISTS: dict[str, str] = {
    "discounts":    f"{BASE}/london/whats-on/discounts/",
    "musicals":     f"{BASE}/london/whats-on/musicals/",
    "plays":        f"{BASE}/london/whats-on/plays/",
    "kids":         f"{BASE}/london/whats-on/kids/",
    "opera":        f"{BASE}/london/whats-on/opera/",
}

# Dedicated URL for the Today/Tomorrow last-minute table. Parsed
# separately because its DOM (sp-table + last-minute__* classes) is
# nothing like the regular sp-card listing template.
LAST_MINUTE_URL = f"{BASE}/london/whats-on/last-minute/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

DEFAULT_CONCURRENCY = 16

# urllib3-level retries (network errors, 5xx, 429)
RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

# Application-level retry for parse errors (mid-deploy partial HTML etc).
# One retry only — anything flakier than that is genuinely broken.
DETAIL_PARSE_RETRY_DELAY_S = 2.0

# Output file rotation depth — keeps seatplan_london.json, .1, .2, ..., .5
DEFAULT_ROTATION_DEPTH = 5

# Exit codes — 0 clean, 1 hard fail (no output), 2 wrote with warnings.
EXIT_CLEAN = 0
EXIT_HARD_FAIL = 1
EXIT_WARNINGS = 2

# Show detail URLs end with `-tickets/` or a venue-qualified variant like
# `-tickets-p10482-theatre-royal-drury-lane-venue/`. Either is fine; we
# only need to detect that a link is a show-detail link (not a venue
# page, news article, etc).
SHOW_HREF_RE = re.compile(r"^/london/[a-z0-9\-]+-tickets(?:-p\d+-[a-z0-9\-]+-venue)?/?$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seatplan")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ListingCard:
    """A card as it appears on any of the eight listing pages, paired with
    its TheaterEvent JSON-LD."""
    url: str                       # full URL — primary key
    slug: str                      # path slug (used as a stable identifier)
    name: str
    image: str | None
    description_short: str | None
    # Card-level visible-on-listing values
    listing_price_from: float | None    # parsed from "from £X.XX" text
    rating_value: float | None          # avg from sp-rating-headline (only ~25% of cards)
    opens_text: str | None              # "Opens DD MMM YYYY" if present
    bordered: bool                      # bordered cards seem to be promotional
    # From the companion JSON-LD TheaterEvent
    jsonld_low_price: float | None
    jsonld_high_price: float | None
    jsonld_currency: str | None
    jsonld_availability: str | None     # "https://schema.org/InStock" etc.
    start_date: str | None
    end_date: str | None
    duration_iso: str | None            # ISO 8601 duration (PT2H30M etc.) on some events
    typical_age_range: str | None
    venue_name: str | None
    venue_url: str | None
    venue_address: dict | None          # street, locality, postal, country
    venue_geo: dict | None              # {latitude, longitude}
    work_performed_name: str | None
    work_performed_wikipedia: str | None  # sameAs URL


@dataclass
class Performance:
    """One upcoming performance, sourced from the detail page's
    TheaterEvent JSON-LD and (where overlapping) the Last Minute table.

    Five fields at the end are filled by the availability-enrichment step
    (see `_enrich_with_availability`), which calls SeatPlan's internal
    /api/performance/normal-prices/ endpoint to get the actually-bookable
    price. `low_price` is kept untouched — it carries the marketing
    "from £X" price from JSON-LD, which SeatPlan themselves know is
    stale (their new-quotes endpoint fires an `sp_ticketing_calendar_error`
    event when this diverges from the real quote). Consumers should
    prefer `available_from` and fall back to `low_price` only when
    enrichment couldn't run (e.g. show outside the 30-perf cap, network
    error during enrichment, or production has no sku)."""
    iso: str | None            # full ISO with offset, e.g. "2026-05-19T19:30:00+01:00"
    date: str | None           # "2026-05-19"
    time: str | None           # "19:30"
    low_price: float | None    # JSON-LD lowPrice — MARKETING price, often stale
    currency: str | None
    availability: str | None
    # Filled from the Last Minute table where dates overlap
    showtime_id: str | None    # data-id on the booking link
    book_url: str | None       # absolute URL for checkout
    has_deals: bool            # whether the row carried a "Deals" badge
    # Filled by _enrich_with_availability. All None when enrichment
    # didn't run for this perf.
    performance_id: int | None = None       # SeatPlan's numeric perf ID
    available_seats: int | None = None      # count from /normal-prices/ (0 = sold out)
    available_from: float | None = None     # min normalMinPrice — the REAL cheapest
    available_to: float | None = None       # max normalMaxPrice
    price_source: str | None = None         # "normal_prices" | "sold_out" | None


@dataclass
class LastMinuteEntry:
    """One performance from the global Last Minute Tickets table at
    /london/whats-on/last-minute/ — the cross-show Today/Tomorrow
    calendar. Distinct provenance from `Performance` (which is sourced
    from a show's own detail page); kept in its own field so consumers
    can tell where the data came from. The unique payload here is
    the per-day "Price Drops on selected seats" badge that the global
    listing surfaces but isn't visible in the per-show last-minute
    HTML table.

    A single show can have multiple entries for the same date when
    there are matinee + evening performances (e.g. Cabaret's Tuesday
    2pm + 7:30pm)."""
    date: str                  # ISO from the cell's data-date attr, e.g. "2026-05-19"
    time_display: str | None   # raw page text, e.g. "7:30pm" — kept verbatim
    time_24h: str | None       # normalised "HH:MM" derived from time_display
    book_url: str              # absolute URL for checkout
    showtime_id: str | None    # data-id on the perf link
    from_price: float | None   # parsed numeric, e.g. 93.0
    from_price_display: str | None  # original text, e.g. "from £93.00"
    has_price_drops: bool      # presence of a per-perf Price Drops badge
    price_drops_label: str | None   # exact badge text, typically "Price Drops on selected seats"


@dataclass
class CastMember:
    role: str | None
    name: str | None


@dataclass
class FaqEntry:
    question: str | None
    answer: str | None


@dataclass
class WeeklyScheduleRow:
    """One day's matinee/evening pattern from the Show Times table."""
    day: str            # "Monday" .. "Sunday"
    matinee: str | None  # display text e.g. "2.30pm" or None when "-"
    evening: str | None


@dataclass
class Show:
    """Combined master-card + detail-page record."""
    # Identity
    url: str
    slug: str
    sku: str | None                  # from Product JSON-LD (detail page)
    # From listing card / paired JSON-LD
    name: str
    image: str | None
    description_short: str | None
    listing_price_from: float | None
    listing_rating_value: float | None
    opens_text: str | None
    bordered: bool
    jsonld_low_price: float | None
    jsonld_high_price: float | None
    jsonld_currency: str | None
    jsonld_availability: str | None
    start_date: str | None
    end_date: str | None
    duration_iso: str | None
    typical_age_range: str | None
    venue_name: str | None
    venue_url: str | None
    venue_address: dict | None
    venue_geo: dict | None
    work_performed_name: str | None
    work_performed_wikipedia: str | None
    # Which filter slices this show appears in
    appears_in: list[str]
    # From detail page Product JSON-LD
    detail_rating_value: float | None
    detail_rating_count: int | None
    detail_review_count: int | None
    detail_low_price: float | None
    detail_price_valid_until: str | None
    detail_currency: str | None
    # From detail page HTML
    description_full: str | None     # synopsis paragraphs (joined)
    running_time_text: str | None    # "2 hours and 50 minutes including an interval"
    matinee_text: str | None         # "Performances start at 2.30pm..."
    evening_text: str | None
    weekly_schedule: list[dict]      # WeeklyScheduleRow entries
    cast: list[dict]
    faq: list[dict]
    # Unified performance list (JSON-LD ∪ last-minute table)
    performances: list[dict]
    # Entries from the cross-show global Last Minute table (different
    # provenance from `performances` — kept separate; consumers can
    # join on date+time if they want a merged view).
    last_minute_listing: list[dict]
    # Sanity: the canonical URL from the detail page (should == url)
    detail_canonical: str | None


@dataclass
class ShowFailure:
    """A show in the listing that we couldn't fetch/parse a detail page for."""
    url: str
    slug: str
    error: str


@dataclass
class ScrapeReport:
    """Embedded in the output JSON under "report" so downstream consumers
    can detect partial / degraded runs without parsing log files."""
    master_show_count: int
    succeeded_show_count: int
    failed_show_count: int
    tag_lists_scraped: list[str]
    tag_list_counts: dict[str, int]
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
# "Tue 19 May" style date headers in the Last Minute table.
_LAST_MIN_HEADER_RE = re.compile(
    r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+([A-Z][a-z]{2})$"
)
_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _decode(s: str | None) -> str | None:
    if s is None:
        return None
    return html.unescape(s).strip()


def _to_float(s) -> float | None:
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _slug_from_url(url: str) -> str:
    """Extract the slug from a show URL. Used as the stable primary key
    since SeatPlan doesn't expose a numeric ID on listing cards. The slug
    is "les-miserables-tickets" for /london/les-miserables-tickets/, and
    "jesus-christ-superstar-tickets-p10482-theatre-royal-drury-lane-venue"
    for the venue-qualified variant — distinct slugs for distinct
    productions, which is what we want."""
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    # Path looks like "london/<slug>" so the slug is the last segment.
    return parts[-1] if parts else ""


def _parse_time_label_to_24h(label: str) -> str | None:
    """Convert a time label from the Last Minute table ("7:30pm", "2:30pm",
    "7.30pm") to 24h format ("19:30", "14:30"). Returns None if unparseable.

    Used to join Last Minute booking URLs back to the JSON-LD TheaterEvent
    list (which uses 24h ISO times). Lenient about colon vs dot separators
    and the am/pm casing the OLT theme inconsistently emits."""
    s = label.strip().lower().replace(".", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", s)
    if not m:
        # Some entries are "7pm" with no minutes
        m2 = re.match(r"^(\d{1,2})\s*(am|pm)$", s)
        if not m2:
            return None
        hour = int(m2.group(1))
        minute = 0
        meridiem = m2.group(2)
    else:
        hour = int(m.group(1))
        minute = int(m.group(2))
        meridiem = m.group(3)
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _parse_lastminute_header_date(header: str, scraped_at: datetime) -> str | None:
    """Resolve a "Tue 19 May" header to an ISO date, picking the year so
    the date is in the future or within the last few days. The page never
    shows the year, so we have to infer it: the table is always for "next
    5 days", so the resolved date should be within ±2 days of scrape time
    (rolling-window guarantee).

    Picking the wrong year happens at year boundaries — "Tue 30 Dec" could
    be either the current year or next year. We try both and pick the
    one closest to today."""
    m = _LAST_MIN_HEADER_RE.match(header.strip())
    if not m:
        return None
    _dow, day_str, month_str = m.groups()
    month = _MONTH_MAP.get(month_str)
    if not month:
        return None
    try:
        day = int(day_str)
    except ValueError:
        return None
    today = scraped_at.date()
    # Candidate dates this year and adjacent years; pick the one closest
    # to today (which will be within ~2 days for the rolling window).
    candidates = []
    for year_off in (-1, 0, 1):
        try:
            candidates.append(today.replace(
                year=today.year + year_off, month=month, day=day,
            ))
        except ValueError:
            # E.g. day=29 in a non-leap February
            pass
    if not candidates:
        return None
    chosen = min(candidates, key=lambda d: abs((d - today).days))
    return chosen.isoformat()


# ---------------------------------------------------------------------------
# Listing parser
# ---------------------------------------------------------------------------

def _extract_jsonld_blocks(soup: BeautifulSoup) -> list[dict]:
    out: list[dict] = []
    for s in soup.select("script[type='application/ld+json']"):
        if not s.string:
            continue
        try:
            out.append(json.loads(s.string))
        except json.JSONDecodeError:
            # SeatPlan emits valid JSON-LD as of this writing; if a block
            # breaks, log and skip — the others are still usable.
            pass
    return out


def _theater_events_in_doc_order(blocks: list[dict]) -> list[dict]:
    """Return only TheaterEvent JSON-LD blocks, in their document order.
    Listing pairing relies on this matching the card order — confirmed
    empirically on the master listing (125/125)."""
    return [b for b in blocks
            if isinstance(b, dict) and b.get("@type") == "TheaterEvent"]


def _flatten_venue(event: dict) -> tuple[str | None, str | None, dict | None, dict | None]:
    """Pull (venue_name, venue_url, address, geo) out of a TheaterEvent's
    `location` node. Returns None for any missing piece."""
    loc = event.get("location") or {}
    if not isinstance(loc, dict):
        return None, None, None, None
    venue_name = loc.get("name")
    venue_url = loc.get("sameAs")
    addr = loc.get("address") or {}
    address = None
    if isinstance(addr, dict) and addr:
        country = addr.get("addressCountry") or {}
        address = {
            "street": addr.get("streetAddress"),
            "locality": addr.get("addressLocality"),
            "postal_code": addr.get("postalCode"),
            "country": country.get("name") if isinstance(country, dict) else country,
        }
    geo = loc.get("geo") or {}
    geo_out = None
    if isinstance(geo, dict) and ("latitude" in geo or "longitude" in geo):
        geo_out = {
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
        }
    return venue_name, venue_url, address, geo_out


def _build_listing_card(card_div, event: dict | None) -> ListingCard | None:
    """Combine a card's HTML chunk with its paired TheaterEvent JSON-LD."""
    link = card_div.select_one("a.sp-card__link")
    if not link or not link.has_attr("href"):
        return None
    href = link["href"]
    url = urljoin(BASE, href)
    slug = _slug_from_url(url)
    if not slug:
        return None

    # Name: prefer h3 text, fall back to JSON-LD workPerformed name.
    title_el = card_div.select_one("h3.sp-card__title")
    name = title_el.get_text(strip=True) if title_el else ""

    img = card_div.select_one("img.sp-card__header-image")
    image = img.get("src") if img else None
    if image and image.startswith("/"):
        image = urljoin(BASE, image)

    desc_el = card_div.select_one("p.sp-card__description")
    description_short = desc_el.get_text(strip=True) if desc_el else None

    # Card price ("from £X.XX")
    price_el = card_div.select_one(".sp-from-price__value")
    listing_price_from = None
    if price_el:
        m = _PRICE_RE.search(price_el.get_text())
        if m:
            listing_price_from = _to_float(m.group(1))

    # Card rating (only ~25% of cards display this)
    rating_el = card_div.select_one(".sp-rating-headline__avg")
    rating_value = _to_float(rating_el.get_text(strip=True)) if rating_el else None

    # "Opens DATE" footer (only on shows not yet open)
    opens_el = card_div.select_one(".sp-card__small-text-bolded")
    opens_text = None
    if opens_el:
        t = opens_el.get_text(strip=True)
        if t.lower().startswith("opens"):
            opens_text = t

    bordered = "sp-card--bordered" in (card_div.get("class") or [])

    # Pull JSON-LD fields. event may be None if listing/jsonld count drifts.
    if event is None:
        event = {}
    offers = event.get("offers") or {}
    if not isinstance(offers, dict):
        offers = {}
    venue_name, venue_url, address, geo = _flatten_venue(event)
    work = event.get("workPerformed") or {}
    if not isinstance(work, dict):
        work = {}

    # Fallback for name from JSON-LD if the card had no h3 (defensive).
    if not name:
        name = work.get("name") or event.get("name") or slug

    return ListingCard(
        url=url,
        slug=slug,
        name=name,
        image=image,
        description_short=description_short,
        listing_price_from=listing_price_from,
        rating_value=rating_value,
        opens_text=opens_text,
        bordered=bordered,
        jsonld_low_price=_to_float(offers.get("lowPrice")),
        jsonld_high_price=_to_float(offers.get("highPrice")),
        jsonld_currency=offers.get("priceCurrency"),
        jsonld_availability=offers.get("availability"),
        start_date=event.get("startDate"),
        end_date=event.get("endDate"),
        duration_iso=event.get("duration"),
        typical_age_range=event.get("typicalAgeRange"),
        venue_name=venue_name,
        venue_url=venue_url,
        venue_address=address,
        venue_geo=geo,
        work_performed_name=work.get("name"),
        work_performed_wikipedia=work.get("sameAs"),
    )


def parse_listing(html_text: str) -> list[ListingCard]:
    """Parse any of the eight listing pages.

    Master listing returns ~125 cards. Tag-list pages return a subset.
    Either way the markup is the same:
      * `<div class="sp-card sp-card--linked sp-card-list__item ...">`
        elements for each show card.
      * One `<script type="application/ld+json">` TheaterEvent per card,
        in document order, matching the card order 1:1.

    If the JSON-LD count diverges from the card count (defensive), we
    log it and still emit cards — the JSON-LD-derived fields will be
    None for the extras.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    cards_html = soup.select("div.sp-card.sp-card--linked.sp-card-list__item")
    if not cards_html:
        return []

    events = _theater_events_in_doc_order(_extract_jsonld_blocks(soup))
    if len(events) != len(cards_html):
        log.warning(
            "  card/JSON-LD count mismatch: %d cards vs %d TheaterEvents "
            "— some JSON-LD-derived fields will be empty",
            len(cards_html), len(events),
        )

    out: list[ListingCard] = []
    seen_urls: set[str] = set()
    for i, card_div in enumerate(cards_html):
        event = events[i] if i < len(events) else None
        card = _build_listing_card(card_div, event)
        if card is None:
            continue
        if card.url in seen_urls:
            # Defensive — if the page ever emits a duplicate (e.g. a
            # "featured" repeat), skip silently.
            continue
        seen_urls.add(card.url)
        out.append(card)
    return out


# ---------------------------------------------------------------------------
# Global Last Minute Tickets table parser
# (separate page, /london/whats-on/last-minute/ — different DOM from
# the regular tag-list pages, so a dedicated parser. Its unique value
# is per-day "Price Drops on selected seats" badges that aren't
# visible on per-show pages.)
# ---------------------------------------------------------------------------

# Selector + helper constants. Defined as constants up top so the
# structure assumptions are visible in one place if the page DOM
# changes.
_LM_TABLE_SELECTOR = "table.last-minute__table"
_LM_ROW_INFO_CELL = "th.last-minute__production-info-cell"
_LM_DAY_CELL = "td.last-minute__day-col"
_LM_EMPTY_MARKER = "last-minute__day-col-inner--empty"
_LM_PERF_ITEM = "div.last-minute__performance-item"
_LM_TIME_WRAP = "last-minute__performance-link-wrap"
_LM_PERF_LINK = "a.last-minute__performance-link"
_LM_FROM_PRICE = "last-minute__performance-select-seats-wrap-from-price"
_LM_OFFERS_TEXT = "last-minute__performance-offers-text"

_LM_FROM_PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d+)?)")


def _parse_last_minute_entry(item, date_iso: str) -> LastMinuteEntry | None:
    """Build one LastMinuteEntry from a `.last-minute__performance-item`
    div. Returns None if the item is missing the essentials (booking URL
    or time) — defensive against malformed cells.

    The cell carries `data-date` already; the perf-link href also encodes
    the date (e.g. /tickets/19-may-2026/7-30pm/) which is more brittle
    to parse, so we trust the cell attr."""
    # Time — the link wrapper contains just the display string ("7:30pm")
    time_wrap = item.find(class_=_LM_TIME_WRAP)
    time_display = time_wrap.get_text(strip=True) if time_wrap else None

    # Booking URL — there are two anchors per item (time link + "Select
    # Seats" button) with the same href. Either works.
    link = item.find("a", class_="last-minute__performance-link", href=True)
    if link is None:
        # Fall back to any anchor inside the item
        link = item.find("a", href=True)
    if link is None or not link.get("href"):
        return None
    href = link["href"]
    if href.startswith("/"):
        href = urljoin(BASE, href)
    showtime_id = link.get("data-id")

    # From-price — the visible "from £93.00" inside the Select Seats button
    price_el = item.find(class_=_LM_FROM_PRICE)
    from_price_display = None
    from_price = None
    if price_el:
        from_price_display = price_el.get_text(" ", strip=True) or None
        if from_price_display:
            m = _LM_FROM_PRICE_RE.search(from_price_display)
            if m:
                from_price = _to_float(m.group(1))

    # Price Drops badge — its presence + the exact label
    offers_text_el = item.find(class_=_LM_OFFERS_TEXT)
    has_price_drops = offers_text_el is not None
    price_drops_label = None
    if offers_text_el:
        # The button text is like "Price Drops on selected seats" with a
        # trailing chevron icon — strip whitespace and keep only the
        # primary span's text.
        first_span = offers_text_el.find("span")
        if first_span:
            price_drops_label = first_span.get_text(" ", strip=True) or None
        else:
            price_drops_label = offers_text_el.get_text(" ", strip=True) or None

    return LastMinuteEntry(
        date=date_iso,
        time_display=time_display,
        time_24h=_parse_time_label_to_24h(time_display) if time_display else None,
        book_url=href,
        showtime_id=showtime_id,
        from_price=from_price,
        from_price_display=from_price_display,
        has_price_drops=has_price_drops,
        price_drops_label=price_drops_label,
    )


def _parse_last_minute_listing(
    html_text: str,
) -> tuple[dict[str, list[LastMinuteEntry]], dict[str, str]]:
    """Parse /london/whats-on/last-minute/ into two parallel maps:

      entries_by_url: {show_url: [LastMinuteEntry, ...]} — one entry per
        performance (matinee + evening on the same day produce two
        entries for that url+date).

      production_id_by_url: {show_url: production_id_str} — captured from
        each `<tr data-production-id="...">`. Identical to the Product
        JSON-LD `sku` on shows that publish one; for shows whose detail
        page doesn't expose `sku` (some operas, pre-opening shows), this
        is our only source of that ID. The caller uses it to backfill
        `sku` where missing.

    The page's DOM is a `<table class="last-minute__table">` with one
    row per show and one cell per day (currently Today + Tomorrow). Each
    day cell has `data-date="YYYY-MM-DD"` and contains zero or more
    `.last-minute__performance-item` blocks (handles multi-perf days
    like Cabaret's matinee + evening).

    Show URL is taken from the production-info cell's anchor; that URL
    matches the master listing exactly (verified across all 47 rows in
    the wild — no multi-venue resolution needed).

    Returns ({}, {}) if the table is absent (which would itself be
    a signal the page DOM has changed — the caller logs it)."""
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.select_one(_LM_TABLE_SELECTOR)
    if table is None:
        return {}, {}

    rows = table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")
    entries_by_url: dict[str, list[LastMinuteEntry]] = {}
    production_id_by_url: dict[str, str] = {}

    for row in rows:
        info_cell = row.select_one(_LM_ROW_INFO_CELL)
        if info_cell is None:
            continue
        show_anchor = info_cell.find("a", href=True)
        if show_anchor is None:
            continue
        show_url = show_anchor["href"]
        if show_url.startswith("/"):
            show_url = urljoin(BASE, show_url)

        # Capture production_id from the row's data attribute — same
        # value as the show's Product.sku where available, and a useful
        # fallback when the detail page doesn't carry one.
        prod_id = row.get("data-production-id")
        if prod_id:
            production_id_by_url[show_url] = str(prod_id)

        entries: list[LastMinuteEntry] = []
        for day_cell in row.select(_LM_DAY_CELL):
            date_iso = day_cell.get("data-date")
            if not date_iso:
                # Defensive — without data-date we can't anchor the perf
                # to a real date. The cell is useless to us.
                continue
            # Skip empty-marker cells fast (avoids walking perf-item
            # selectors that would find nothing anyway).
            if day_cell.find(class_=_LM_EMPTY_MARKER) is not None:
                continue
            for item in day_cell.select(_LM_PERF_ITEM):
                entry = _parse_last_minute_entry(item, date_iso)
                if entry is not None:
                    entries.append(entry)

        if entries:
            entries_by_url.setdefault(show_url, []).extend(entries)

    return entries_by_url, production_id_by_url


# ---------------------------------------------------------------------------
# Show detail parser
# ---------------------------------------------------------------------------

def _find_product_node(jsonld_blocks: list[dict]) -> dict | None:
    """The Product node holds aggregateRating (with the full ratingCount
    and reviewCount the listing rating doesn't carry) and offers."""
    for block in jsonld_blocks:
        if isinstance(block, dict) and block.get("@type") == "Product":
            return block
        # SeatPlan emits flat blocks, but defensively handle @graph too
        graph = block.get("@graph") if isinstance(block, dict) else None
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
    return None


def _find_faq_entries(jsonld_blocks: list[dict]) -> list[dict]:
    for block in jsonld_blocks:
        if isinstance(block, dict) and block.get("@type") == "FAQPage":
            return [
                asdict(FaqEntry(
                    question=q.get("name"),
                    answer=(q.get("acceptedAnswer") or {}).get("text"),
                ))
                for q in (block.get("mainEntity") or [])
                if isinstance(q, dict)
            ]
    return []


def _extract_performances_from_jsonld(jsonld_blocks: list[dict]) -> list[Performance]:
    """Each upcoming performance is its own TheaterEvent JSON-LD block on
    the detail page. Each one has a full ISO startDate (with timezone
    offset) and an Offer.lowPrice for that specific performance.

    Returns Performance records without book_url / showtime_id filled
    in — those come from the Last Minute HTML table where date+time
    overlap, and get merged later in _merge_lastminute()."""
    out: list[Performance] = []
    for block in jsonld_blocks:
        if not isinstance(block, dict) or block.get("@type") != "TheaterEvent":
            continue
        iso = block.get("startDate")
        if not iso:
            continue
        # SeatPlan serves two flavours of TheaterEvent on a detail page:
        #   * Show-level "wrapper" — startDate is a *date only*, like
        #     "2016-05-30" (the show's original opening night). This is
        #     the same JSON-LD that's paired with each listing card.
        #     There's only one of these per show.
        #   * Per-performance — startDate is a *full ISO datetime* with
        #     a time portion and timezone offset, like
        #     "2026-05-19T19:30:00+01:00". There are ~9 of these per
        #     show, one per upcoming performance.
        # Only the per-performance ones belong in the performances list;
        # the wrapper polluted earlier runs with implausible-date entries
        # (e.g. Harry Potter's 2016-05-30 opening night).
        # We discriminate on the presence of "T" in the startDate.
        if "T" not in iso:
            continue
        # Split the ISO timestamp into date + time portions. We keep the
        # raw `iso` field too in case consumers want the offset.
        date_part = None
        time_part = None
        if "T" in iso:
            date_part, rest = iso.split("T", 1)
            # rest is like "19:30:00+01:00" — keep HH:MM only
            hhmm = rest[:5] if len(rest) >= 5 else None
            time_part = hhmm
        else:
            date_part = iso[:10] if len(iso) >= 10 else None

        offers = block.get("offers") or {}
        if not isinstance(offers, dict):
            offers = {}
        out.append(Performance(
            iso=iso,
            date=date_part,
            time=time_part,
            low_price=_to_float(offers.get("lowPrice")),
            currency=offers.get("priceCurrency"),
            availability=offers.get("availability"),
            showtime_id=None,
            book_url=None,
            has_deals=False,
        ))
    return out


def _extract_lastminute_table(
    soup: BeautifulSoup, scraped_at: datetime,
) -> list[tuple[str | None, str | None, str | None, str | None, bool]]:
    """Walk the "Last Minute Tickets" table and return a list of tuples:

        (date_iso, time_24h, time_label, book_url, has_deals_badge)

    Returns an empty list if the section is absent (some shows have no
    bookable performances right now — pre-opening shows, sold-out
    runs).

    Why we keep time_label too: we use time_24h for joining against the
    JSON-LD performance list, but the label (the original "7:30pm"
    text) is useful for debugging mismatches."""
    section = soup.select_one("section#last-minute")
    if section is None:
        return []
    table = section.select_one("table")
    if table is None:
        return []

    # First row: date headers ("Tue 19 May" etc.)
    headers_row = table.select_one("tr")
    if headers_row is None:
        return []
    header_dates: list[str | None] = []
    for th in headers_row.select("th"):
        txt = th.get_text(strip=True)
        header_dates.append(_parse_lastminute_header_date(txt, scraped_at))

    # Second row: cells with performance lists
    body_rows = table.select("tr")[1:]
    if not body_rows:
        return []
    # The Last Minute table has one body row with one td per day column.
    body_row = body_rows[0]
    tds = body_row.select("td")

    out: list[tuple[str | None, str | None, str | None, str | None, bool]] = []
    for col_idx, td in enumerate(tds):
        if col_idx >= len(header_dates):
            break
        date_iso = header_dates[col_idx]
        for item in td.select(".last-minute__performance-item"):
            link = item.select_one("a.last-minute__performance-link")
            if not link:
                continue
            time_label = link.get_text(strip=True)
            time_24h = _parse_time_label_to_24h(time_label)
            href = link.get("href")
            book_url = urljoin(BASE, href) if href else None
            has_deals = item.select_one(".last-minute__deals-badge-wrap") is not None
            out.append((date_iso, time_24h, time_label, book_url, has_deals))

    return out


def _merge_lastminute(
    perfs: list[Performance],
    lastmin: list[tuple[str | None, str | None, str | None, str | None, bool]],
) -> list[Performance]:
    """Join the JSON-LD performance list with the Last Minute table on
    (date, time). The JSON-LD list is authoritative for which performances
    exist (it has 9 entries vs the table's 5-day cap); the table contributes
    the booking URL and `data-id` showtime ID we can't get elsewhere.

    Any Last Minute entry whose (date, time) doesn't match a JSON-LD
    performance is appended as a standalone performance — this happens
    rarely but can if the JSON-LD lags the calendar by a day."""
    # Index JSON-LD perfs by (date, time) for O(1) lookup
    by_key: dict[tuple[str | None, str | None], Performance] = {}
    for p in perfs:
        by_key[(p.date, p.time)] = p

    # We also need to look up the showtime_id, which we don't have until
    # _extract_lastminute_table is called with the soup — see fetch path.
    # Here we just match on (date, time) and merge what's already there.
    matched_keys: set[tuple[str | None, str | None]] = set()
    for date_iso, time_24h, _time_label, book_url, has_deals in lastmin:
        key = (date_iso, time_24h)
        target = by_key.get(key)
        if target is not None:
            target.book_url = book_url
            target.has_deals = has_deals
            matched_keys.add(key)
        else:
            # Stand-alone last-minute entry — usually a calendar/JSON-LD
            # sync lag. Emit a placeholder Performance with what we have.
            perfs.append(Performance(
                iso=None,
                date=date_iso,
                time=time_24h,
                low_price=None,
                currency=None,
                availability=None,
                showtime_id=None,
                book_url=book_url,
                has_deals=has_deals,
            ))
    return perfs


def _merge_global_lastminute_into_perfs(
    perfs: list[dict],
    global_lm: list[dict],
) -> list[dict]:
    """Fill in book_url / showtime_id / has_deals from the cross-show
    global Last Minute Tickets table for performances whose per-show
    `section#last-minute` was missing or didn't cover them.

    The per-show section (merged earlier in `_merge_lastminute`) is the
    preferred source; the global table — parsed from
    `/london/whats-on/last-minute/`, which carries the same booking
    deep-link URLs — is the fallback. We only fill fields that aren't
    already populated, so per-show data always wins.

    This matters for shows whose detail page omits the per-show widget
    entirely. Typical cases:
      * New productions in their first few days (no per-show widget
        yet, but the show is bookable today/tomorrow via the global
        table — e.g. Beetlejuice on opening week).
      * Sold-out shows where the per-show table is empty.
      * Productions where SeatPlan simply hasn't enabled the widget.

    Without this merge, ~17 currently-bookable shows surface 0 book_urls
    on their performances even though the global table carries them.

    Returns the (possibly extended) list of performance dicts. Operates
    on dicts (not Performance instances) because `_parse_detail_page`
    has already done `asdict(p)` by the time we receive these.
    """
    if not global_lm:
        return perfs

    by_key: dict[tuple, dict] = {
        (p.get("date"), p.get("time")): p for p in perfs
    }

    for e in global_lm:
        key = (e.get("date"), e.get("time_24h"))
        target = by_key.get(key)
        if target is not None:
            # Fill gaps only — don't overwrite per-show data
            if not target.get("book_url") and e.get("book_url"):
                target["book_url"] = e["book_url"]
            if not target.get("showtime_id") and e.get("showtime_id"):
                target["showtime_id"] = e["showtime_id"]
            if not target.get("has_deals") and e.get("has_price_drops"):
                target["has_deals"] = True
        elif e.get("book_url"):
            # No JSON-LD performance matches — append as a standalone
            # entry, mirroring the Performance dataclass shape. Same
            # fallback strategy `_merge_lastminute` uses for unmatched
            # per-show entries.
            perfs.append({
                "iso": None,
                "date": e.get("date"),
                "time": e.get("time_24h"),
                "low_price": e.get("from_price"),
                "currency": "GBP",  # SeatPlan London is consistently GBP
                "availability": None,
                "showtime_id": e.get("showtime_id"),
                "book_url": e["book_url"],
                "has_deals": bool(e.get("has_price_drops")),
            })

    return perfs


# ---------------------------------------------------------------------------
# Constructed booking URLs (for performances beyond the rolling window)
# ---------------------------------------------------------------------------

# Lower-case 3-letter month abbreviations, indexed 1..12. SeatPlan's
# URL slug uses these (e.g. "13-jun-2026"), so we match that exactly.
_MONTH_ABBR: tuple[str, ...] = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def _construct_book_url(
    show_url: str, date_iso: str | None, time_24h: str | None,
) -> str | None:
    """Build a SeatPlan booking deep-link URL from a (date, time) pair.

    SeatPlan only renders deep-link booking URLs inline in the page DOM
    for the rolling ~5-day "Last Minute" window, but their server
    validates leaf URLs against the full performance calendar:

      * Valid (date, time) with a real performance → serves a dedicated
        "Select Seat" page (title "Select Seat | <time> <date> | …").
      * (date, time) with no performance, or a past date → redirects
        to the show page.

    Verified empirically across +1 week, +3 weeks, +10 weeks, +20 weeks
    and end-of-run for Wicked: all returned Select Seat pages. A Monday
    probe (no performance) and a past-date probe both bounced. The
    pattern works for the entire published run, not just the window.

    Returns None on malformed inputs (missing date/time, non-numeric
    components, out-of-range values). Since we feed this from JSON-LD
    performance dates+times, malformed inputs should be rare; we still
    guard against them so a single bad row doesn't crash a whole show.

    URL format (matches what the per-show widget emits):
        {show_url}tickets/{D-mon-YYYY}/{h-mm}{am|pm}/

    Where D is the day of month with no leading zero, mon is the
    lowercase 3-letter month, h is the 12-hour hour with no leading
    zero, mm is the 2-digit minute, and am/pm is the meridian. Examples:
        "2026-06-13" + "14:30" → ".../tickets/13-jun-2026/2-30pm/"
        "2026-08-01" + "19:30" → ".../tickets/1-aug-2026/7-30pm/"
        "2026-10-10" + "12:00" → ".../tickets/10-oct-2026/12-00pm/"
        "2026-10-10" + "00:30" → ".../tickets/10-oct-2026/12-30am/"
    """
    if not show_url or not date_iso or not time_24h:
        return None

    try:
        y_str, m_str, d_str = date_iso.split("-")
        year, month, day = int(y_str), int(m_str), int(d_str)
    except (ValueError, AttributeError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None

    try:
        hh_str, mm_str = time_24h.split(":", 1)
        hour, minute = int(hh_str), int(mm_str[:2])
    except (ValueError, AttributeError, IndexError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    # 24h → 12h with am/pm
    if hour == 0:
        h12, ampm = 12, "am"
    elif hour < 12:
        h12, ampm = hour, "am"
    elif hour == 12:
        h12, ampm = 12, "pm"
    else:
        h12, ampm = hour - 12, "pm"

    base = show_url if show_url.endswith("/") else show_url + "/"
    return f"{base}tickets/{day}-{_MONTH_ABBR[month - 1]}-{year}/{h12}-{minute:02d}{ampm}/"


def _fill_constructed_book_urls(
    perfs: list[dict], show_url: str,
) -> int:
    """Fill `book_url` on any performance dict that's still missing one,
    using the deterministic SeatPlan deep-link URL pattern.

    Runs after the per-show widget merge (`_merge_lastminute`) and the
    cross-show global table merge (`_merge_global_lastminute_into_perfs`)
    have done what they can. Whatever's left is beyond the ~5-day
    window the page DOM exposes; we construct URLs for those from the
    JSON-LD date+time directly.

    Mutates perfs in place. Returns the number of URLs constructed —
    surfaced in the scrape report so anomalies (a sudden drop in
    constructed count, or a sudden surge in construction failures) are
    visible run-to-run.

    No HTTP requests are made — this is a pure transform. If SeatPlan
    ever changes the URL pattern, broken links will be emitted without
    a runtime signal; the right tripwire is the cross-source
    consistency check downstream, which would notice book_urls that
    don't resolve.
    """
    constructed = 0
    for p in perfs:
        if p.get("book_url"):
            continue
        url = _construct_book_url(show_url, p.get("date"), p.get("time"))
        if url is not None:
            p["book_url"] = url
            constructed += 1
    return constructed


def _extract_lastminute_showtime_ids(soup: BeautifulSoup) -> dict[str, str]:
    """Build a map from booking URL → data-id (showtime ID). We attach
    these in a separate pass so the merge step doesn't have to thread the
    soup through."""
    out: dict[str, str] = {}
    section = soup.select_one("section#last-minute")
    if section is None:
        return out
    for link in section.select("a.last-minute__performance-link"):
        href = link.get("href")
        data_id = link.get("data-id")
        if href and data_id:
            absurl = urljoin(BASE, href)
            out[absurl] = data_id
    return out

# ---------------------------------------------------------------------------
# Availability enrichment — actual bookable prices, not marketing headlines
# ---------------------------------------------------------------------------
#
# The JSON-LD lowPrice carried on each TheaterEvent block is the show's
# headline "from £X" price (e.g. £12 for A Midsummer Night's Dream),
# repeated identically across every performance. It's the marketing-tier
# anchor price, not the actually-bookable cheapest. SeatPlan's own
# new-quotes endpoint logs a `sp_ticketing_calendar_error` event whenever
# the JSON-LD "from" diverges from the real quote, e.g.:
#
#     {"errorDescription": "From Price Too Low",
#      "state": "Performance: 12.00; Quote: 30.00"}
#
# We work around it by calling two undocumented but stable endpoints:
#
#   1. GET /ajax/tickets/performances/{productionId}/{date}/
#      Returns [{id, date, weekDay, time, link}, ...] for every upcoming
#      performance from `date` onward. One call per show.
#
#   2. GET /api/performance/normal-prices/{performanceId}
#      Returns [{seatId, normalMinPrice, normalMaxPrice}, ...] — one
#      record per currently-available seat. min(normalMinPrice) is the
#      real cheapest price; len() is the count of bookable seats;
#      empty array means sold out.
#
# Both endpoints require only a browser-shaped User-Agent (already on
# the session) and a Referer pointing at a seatplan booking page.
# Responses are server-side cached (~2h per new-quotes'
# quoteSearchRemainingLife) so load is mild.

PERFORMANCES_URL = "/ajax/tickets/performances/{production_id}/{date}/"
NORMAL_PRICES_URL = "/api/performance/normal-prices/{performance_id}"

# Don't enrich more than this many performances per show — the discovery
# endpoint typically returns ~15-20 future perfs and we don't need
# accurate prices for the ones 3+ months out (price column on a
# comparison table is about near-term decisions). Far-future perfs keep
# their JSON-LD low_price as a fallback, clearly tagged via
# price_source=None so consumers can tell they weren't verified.
AVAILABILITY_PERF_CAP = 30

# Lowercase month abbreviations for the performances endpoint, which
# wants e.g. "21-may-2026". %b is locale-dependent so we format manually.
_MONTH_ABBR_LC = ["jan", "feb", "mar", "apr", "may", "jun",
                  "jul", "aug", "sep", "oct", "nov", "dec"]


def _format_date_for_performances_endpoint(d) -> str:
    """date(2026, 5, 21) -> '21-may-2026'."""
    return f"{d.day}-{_MONTH_ABBR_LC[d.month - 1]}-{d.year}"


def _parse_performances_endpoint_date(s: str) -> str | None:
    """'21-May-2026' -> '2026-05-21'. None if unparseable."""
    if not s:
        return None
    parts = s.split("-")
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        mon = _MONTH_MAP.get(parts[1].title())
        year = int(parts[2])
        if mon is None:
            return None
        return f"{year:04d}-{mon:02d}-{day:02d}"
    except (ValueError, TypeError):
        return None


def _fetch_performance_ids(
    session: requests.Session, sku: str, slug: str, scraped_at: datetime,
) -> dict[tuple[str, str], int]:
    """Map (date_iso, time_24h) -> performance_id for every future
    performance of this show. Empty dict on any error — caller falls
    back to JSON-LD prices for all perfs."""
    date_str = _format_date_for_performances_endpoint(scraped_at.date())
    url = BASE + PERFORMANCES_URL.format(production_id=sku, date=date_str)
    referer = f"{BASE}/london/{slug}/"
    try:
        resp = session.get(
            url,
            headers={"Referer": referer,
                     "Accept": "application/json, text/plain, */*"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.debug("performances endpoint failed for sku=%s: %s", sku, e)
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[tuple[str, str], int] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        date_iso = _parse_performances_endpoint_date(item.get("date") or "")
        time_24h = _parse_time_label_to_24h(item.get("time") or "")
        pid = item.get("id")
        if date_iso and time_24h and isinstance(pid, int):
            out[(date_iso, time_24h)] = pid
    return out


def _fetch_normal_prices(
    session: requests.Session, performance_id: int, slug: str,
) -> list[dict] | None:
    """Raw list from /api/performance/normal-prices/{id}. None on error;
    empty list means genuinely sold out."""
    url = BASE + NORMAL_PRICES_URL.format(performance_id=performance_id)
    referer = f"{BASE}/london/{slug}/"
    try:
        resp = session.get(
            url,
            headers={"Referer": referer,
                     "Accept": "application/json, text/plain, */*"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.debug("normal-prices failed for perf=%s: %s", performance_id, e)
        return None
    return data if isinstance(data, list) else None


def _enrich_with_availability(
    session: requests.Session, sku: str | None, slug: str,
    performances: list[dict], scraped_at: datetime,
) -> int:
    """Populate performance_id / available_seats / available_from /
    available_to / price_source on each perf dict in place.

    Returns the number of performances that got `price_source =
    "normal_prices"` (i.e. genuinely enriched with a verified bookable
    price). No-op if sku is missing or the discovery call fails — perfs
    keep their JSON-LD `low_price` as fallback and `price_source` stays
    None to signal "unverified"."""
    if not sku or not performances:
        return 0
    id_map = _fetch_performance_ids(session, sku, slug, scraped_at)
    if not id_map:
        return 0

    enriched = 0
    for p in performances[:AVAILABILITY_PERF_CAP]:
        key = (p.get("date"), p.get("time"))
        pid = id_map.get(key)
        if pid is None:
            continue
        p["performance_id"] = pid
        prices = _fetch_normal_prices(session, pid, slug)
        if prices is None:
            # network/parse error — keep low_price, leave new fields None
            continue
        if not prices:
            # endpoint returned [] -- genuinely sold out
            p["available_seats"] = 0
            p["price_source"] = "sold_out"
            continue
        mins = [_to_float(r.get("normalMinPrice")) for r in prices
                if isinstance(r, dict)]
        maxs = [_to_float(r.get("normalMaxPrice")) for r in prices
                if isinstance(r, dict)]
        mins = [m for m in mins if m is not None]
        maxs = [m for m in maxs if m is not None]
        if mins:
            p["available_from"] = min(mins)
            p["available_to"] = max(maxs) if maxs else min(mins)
            p["available_seats"] = len(prices)
            p["price_source"] = "normal_prices"
            enriched += 1
    return enriched


def _extract_weekly_schedule(soup: BeautifulSoup) -> tuple[list[dict], str | None, str | None, str | None]:
    """Extract:
      * The weekly Mon-Sun matinee/evening grid as a list of dicts.
      * "How long is X?" running-time text.
      * "Matinee Shows" text.
      * "Evening Shows" text.

    The section is at #show-time. Cells are "-" for no performance,
    otherwise a display string like "7.30pm"."""
    section = soup.select_one("section#show-time")
    schedule: list[dict] = []
    running_time = None
    matinee_text = None
    evening_text = None

    if section is None:
        return schedule, running_time, matinee_text, evening_text

    table = section.select_one("table")
    if table is not None:
        for row in table.select("tbody tr"):
            day_cell = row.select_one("th")
            tds = row.select("td")
            if not day_cell or len(tds) < 2:
                continue
            day_name = day_cell.get_text(strip=True)
            mat = tds[0].get_text(strip=True)
            eve = tds[1].get_text(strip=True)
            schedule.append(asdict(WeeklyScheduleRow(
                day=day_name,
                matinee=mat if mat and mat != "-" else None,
                evening=eve if eve and eve != "-" else None,
            )))

    # Feature blocks for running-time / matinee / evening text. Keyed by
    # header text which is stable across shows.
    for blk in section.select(".sp-feature-block"):
        header_el = blk.select_one(".sp-feature-block__header")
        text_el = blk.select_one(".sp-feature-block__text")
        if not header_el or not text_el:
            continue
        header = header_el.get_text(strip=True).lower()
        text = text_el.get_text(" ", strip=True)
        if "how long" in header or "running time" in header:
            running_time = text
        elif "matinee" in header:
            matinee_text = text
        elif "evening" in header:
            evening_text = text

    return schedule, running_time, matinee_text, evening_text


def _extract_cast(soup: BeautifulSoup) -> list[dict]:
    """Pull the cast list out of the long-description editorials.

    Pattern observed:
        <h2>The Les Miserables London Cast</h2>
        <p>Main cast:</p>
        <ul>
          <li><strong>Jean Valjean</strong> - Ian McIntosh</li>
          ...
        </ul>

    We look for any <ul> that directly follows a heading mentioning
    "cast" (case-insensitive) and whose first <li> contains both a
    <strong> and a hyphen separator. Best-effort: some shows have no
    cast list, others list cast differently (e.g. prose paragraphs),
    and we just skip those rather than guess.
    """
    out: list[dict] = []
    editorials = soup.select_one(".sp-editorials")
    if editorials is None:
        return out

    # Walk children looking for a "Cast" heading followed by a <ul>.
    found_cast_heading = False
    for el in editorials.find_all(True):
        if el.name in ("h2", "h3", "h4"):
            txt = el.get_text(strip=True).lower()
            found_cast_heading = "cast" in txt
            continue
        if not found_cast_heading:
            continue
        if el.name == "ul":
            for li in el.select("li"):
                strong = li.find("strong")
                if strong is None:
                    continue
                role = strong.get_text(strip=True).rstrip(":").strip()
                # The name follows the </strong>, optionally after " - ".
                # Cleanest extraction: take li text, remove role + leading
                # separator chars.
                full_text = li.get_text(" ", strip=True)
                # full_text e.g. "Jean Valjean - Ian McIntosh" (role first)
                # or "Cosette - Izzi Levine"
                if full_text.lower().startswith(role.lower()):
                    rest = full_text[len(role):].lstrip(" -–—:").strip()
                else:
                    rest = full_text
                name_clean = rest or None
                role_clean = role or None
                if name_clean or role_clean:
                    out.append(asdict(CastMember(role=role_clean, name=name_clean)))
            # Only take the first ul after the heading — subsequent ones
            # are usually unrelated lists (e.g. "ways to save").
            break
    return out


def _extract_description_full(soup: BeautifulSoup) -> str | None:
    """Concatenate the paragraph text from the long-description editorials,
    skipping H2 sub-headings (which often duplicate FAQ-style content).

    This gives a clean show synopsis usable as a blurb. We deliberately
    don't try to preserve structure — consumers wanting structure should
    consume the raw HTML themselves."""
    editorials = soup.select_one(".production-overview__description .sp-editorials")
    if editorials is None:
        # Fallback to any sp-editorials block on the page
        editorials = soup.select_one(".sp-editorials")
        if editorials is None:
            return None
    paragraphs: list[str] = []
    for p in editorials.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if txt:
            paragraphs.append(txt)
    if not paragraphs:
        return None
    return "\n\n".join(paragraphs)


def _extract_canonical(soup: BeautifulSoup) -> str | None:
    link = soup.select_one('link[rel="canonical"]')
    return link.get("href") if link else None


def parse_show(html_text: str, scraped_at: datetime) -> dict:
    """Return a dict of all detail-page fields. Caller merges with the
    listing card."""
    soup = BeautifulSoup(html_text, "html.parser")
    blocks = _extract_jsonld_blocks(soup)

    # Product JSON-LD → aggregateRating + offers + sku
    product = _find_product_node(blocks) or {}
    agg = product.get("aggregateRating") or {}
    if not isinstance(agg, dict):
        agg = {}
    p_offers = product.get("offers") or {}
    if not isinstance(p_offers, dict):
        p_offers = {}

    sku = product.get("sku")
    if sku is not None:
        sku = str(sku)

    # Performance list from TheaterEvent JSON-LD blocks
    perfs = _extract_performances_from_jsonld(blocks)

    # Last Minute table → (date, time, label, book_url, has_deals)
    lastmin = _extract_lastminute_table(soup, scraped_at)
    perfs = _merge_lastminute(perfs, lastmin)

    # Showtime IDs map (book_url → data-id) — attached in a second pass
    # so the merge step doesn't have to know about the soup.
    showtime_ids = _extract_lastminute_showtime_ids(soup)
    for p in perfs:
        if p.book_url and not p.showtime_id:
            p.showtime_id = showtime_ids.get(p.book_url)

    # Weekly schedule + running-time / matinee / evening text
    weekly, running_time, matinee_text, evening_text = _extract_weekly_schedule(soup)

    # Cast + long description
    cast = _extract_cast(soup)
    description_full = _extract_description_full(soup)

    # FAQ
    faq = _find_faq_entries(blocks)

    # Canonical URL (verifies we hit the page we expected)
    canonical = _extract_canonical(soup)

    return {
        "sku": sku,
        "detail_rating_value": _to_float(agg.get("ratingValue")),
        "detail_rating_count": agg.get("ratingCount") if isinstance(agg.get("ratingCount"), int) else None,
        "detail_review_count": agg.get("reviewCount") if isinstance(agg.get("reviewCount"), int) else None,
        "detail_low_price": _to_float(p_offers.get("price") or p_offers.get("lowPrice")),
        "detail_price_valid_until": p_offers.get("priceValidUntil"),
        "detail_currency": p_offers.get("priceCurrency"),
        "description_full": description_full,
        "running_time_text": running_time,
        "matinee_text": matinee_text,
        "evening_text": evening_text,
        "weekly_schedule": weekly,
        "cast": cast,
        "faq": faq,
        "performances": [asdict(p) for p in perfs],
        "detail_canonical": canonical,
    }


# ---------------------------------------------------------------------------
# Stage 1 — master + tag lists
# ---------------------------------------------------------------------------

def fetch_master_and_tags(
    session: requests.Session, include_tag_lists: bool,
) -> tuple[list[ListingCard], dict[str, list[str]], dict[str, int],
           dict[str, list[dict]], dict[str, str]]:
    """Returns:
        master_cards: ListingCards from /london/
        appears_in_map: slug → list of tag names the show appears in
        tag_list_counts: tag → count (or -1 if the fetch failed)
        last_minute_by_url: show_url → list of LastMinuteEntry dicts from
            the global Last Minute Tickets table (empty if not included).
            Keyed by URL not slug so multi-venue shows (sharing a slug
            root but having distinct URLs) can be disambiguated.
        production_id_by_url: show_url → numeric production ID string
            from the last-minute table's `data-production-id`. Same
            value as Product.sku where the detail page has one; used to
            backfill sku for shows whose detail page omits it.
    """
    log.info("Fetching master listing: %s", MASTER_URL)
    try:
        resp = session.get(MASTER_URL, timeout=30)
        resp.raise_for_status()
        master_cards = parse_listing(resp.text)
        log.info("  master listing: parsed=%d cards", len(master_cards))
    except requests.RequestException as e:
        log.error("Master listing fetch failed: %s", e)
        return [], {}, {}, {}, {}

    appears_in_map: dict[str, list[str]] = {}
    tag_list_counts: dict[str, int] = {}
    last_minute_by_url: dict[str, list[dict]] = {}
    production_id_by_url: dict[str, str] = {}

    if not include_tag_lists:
        return (master_cards, appears_in_map, tag_list_counts,
                last_minute_by_url, production_id_by_url)

    for tag, url in TAG_LISTS.items():
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            cards = parse_listing(resp.text)
            tag_list_counts[tag] = len(cards)
            for c in cards:
                appears_in_map.setdefault(c.slug, []).append(tag)
            log.info("  tag %-12s parsed=%-3d  %s", f"'{tag}'", len(cards), url)

            # Diagnostic for 0-card tags: distinguish "page is genuinely
            # empty" (e.g. no last-minute deals tonight) from "DOM uses
            # a card class we don't recognise". We probe for three things:
            #   * response size — a tiny page suggests "no results" panel
            #   * any `sp-card` class at all — if the page uses sp-card
            #     but a different variant suffix, our selector is too
            #     specific
            #   * common "no results" copy — would confirm a genuinely
            #     empty curated list
            # Without this, an operator looking at `parsed=0` can't tell
            # which case applies; both look the same in the log.
            if len(cards) == 0 and len(resp.text) > 1000:
                body_lower = resp.text.lower()
                any_sp_card = 'sp-card' in body_lower
                empty_marker = any(m in body_lower for m in (
                    'no shows', 'no results', 'no matches found',
                    "nothing's on", "no events", "no offers available",
                ))
                log.warning(
                    "  tag '%s' returned 0 cards despite %d bytes of HTML "
                    "(any 'sp-card' class present: %s; 'no results' "
                    "marker found: %s) — if this persists, page DOM "
                    "may differ from the standard listing template",
                    tag, len(resp.text), any_sp_card, empty_marker,
                )
        except requests.RequestException as e:
            log.warning("  tag list '%s' failed: %s — skipping", tag, e)
            tag_list_counts[tag] = -1

    # Global Last Minute Tickets table — different DOM from the tag-list
    # pages, so a dedicated parser. We treat it like a tag for the
    # appears_in field, and additionally record the per-show entries
    # (date + time + book_url + Price Drops flag) which are unique to
    # this page.
    try:
        log.info("Fetching global last-minute table: %s", LAST_MINUTE_URL)
        resp = session.get(LAST_MINUTE_URL, timeout=30)
        resp.raise_for_status()
        entries_by_url, production_id_by_url = _parse_last_minute_listing(resp.text)
        # Convert dataclasses to dicts here (the value type in the
        # return signature is list[dict] so downstream code doesn't have
        # to know about the LastMinuteEntry class).
        for url, entries in entries_by_url.items():
            last_minute_by_url[url] = [asdict(e) for e in entries]
        n_shows = len(last_minute_by_url)
        n_entries = sum(len(v) for v in last_minute_by_url.values())
        log.info(
            "  last-minute table: %d shows, %d performance entries  %s",
            n_shows, n_entries, LAST_MINUTE_URL,
        )
        if production_id_by_url:
            log.info(
                "  last-minute table: captured production_id for %d shows "
                "(used to backfill sku where the detail page omits it)",
                len(production_id_by_url),
            )
        tag_list_counts["last_minute"] = n_shows
        # Wire into appears_in. We match on URL because the URL is
        # unique per multi-venue production (the slug isn't — same root
        # slug can belong to two URLs). The caller already keys
        # appears_in_map by slug, so we look up the slug from each URL.
        for url in last_minute_by_url:
            slug = _slug_from_url(url)
            appears_in_map.setdefault(slug, []).append("last_minute")

        # Diagnostic for the empty-table case — distinguishes a
        # legitimately quiet day from a DOM breakage. We expect at
        # least one show with deals on most days, so 0 here is
        # suspicious.
        if n_shows == 0 and len(resp.text) > 1000:
            has_table = "<table" in resp.text
            has_class = "last-minute__table" in resp.text
            log.warning(
                "  last-minute table parsed 0 shows despite %d bytes of HTML "
                "(any '<table' element present: %s; 'last-minute__table' "
                "class present: %s) — if this persists, page DOM may have changed",
                len(resp.text), has_table, has_class,
            )
    except requests.RequestException as e:
        log.warning("  last-minute fetch failed: %s — skipping", e)
        tag_list_counts["last_minute"] = -1

    return (master_cards, appears_in_map, tag_list_counts,
            last_minute_by_url, production_id_by_url)


# ---------------------------------------------------------------------------
# Stage 2 — detail pages in parallel
# ---------------------------------------------------------------------------

def fetch_show_detail(
    session: requests.Session, card: ListingCard, scraped_at: datetime,
) -> tuple[dict | None, str | None]:
    """Fetch and parse a single show's detail page. Returns (detail, error).
    One application-level retry on transient parse / network errors,
    layered on top of urllib3's 429/5xx retries."""
    last_err: str | None = None
    for attempt in (1, 2):
        try:
            resp = session.get(card.url, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            last_err = f"HTTP {code}"
            if code in (404, 410):
                # Permanent — don't waste a retry
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
            return parse_show(resp.text, scraped_at), None
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
    appears_in_map: dict[str, list[str]],
    last_minute_by_url: dict[str, list[dict]],
    production_id_by_url: dict[str, str],
    concurrency: int,
    scraped_at: datetime,
    deadline: float | None = None,
) -> tuple[list[Show], list[ShowFailure], bool]:
    """Returns (shows, failures, budget_exceeded).

    last_minute_by_url: {show_url: [LastMinuteEntry dicts]} from the
    global Last Minute Tickets table — populated onto each Show's
    last_minute_listing field. Empty for shows not on that table.

    production_id_by_url: {show_url: production_id_str} from the same
    last-minute table. Used as a fallback source of `sku` for shows
    whose detail page Product JSON-LD doesn't carry one (some operas
    and pre-opening shows). When the detail page does have sku, it
    takes precedence — the production_id is only consulted as backfill.

    deadline: a time.monotonic() value past which we stop submitting new
    work. In-flight requests run to completion but no new ones get
    queued. Mirrors the OLT scraper's wall-clock budget behaviour."""
    log.info("Fetching %d show detail pages with %d workers...",
             len(cards), concurrency)
    t_start = time.monotonic()
    budget_exceeded = False
    sku_backfills = [0]  # mutable counter for closure; logged at end
    constructed_urls = [0]  # how many book_urls we built from the URL pattern

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

        detail, err = fetch_show_detail(session, card, scraped_at)
        if detail is not None:
            # SKU backfill: prefer the detail-page Product.sku; fall
            # back to the last-minute table's data-production-id when
            # the detail page didn't expose one.
            sku = detail.get("sku")
            if sku is None:
                backfill = production_id_by_url.get(card.url)
                if backfill is not None:
                    sku = backfill
                    with lock:
                        sku_backfills[0] += 1
            # Fill book_url / showtime_id gaps from the cross-show
            # global last-minute table. Critical for shows whose
            # detail page has no `section#last-minute` widget — without
            # this, ~17 currently-bookable shows surface 0 book_urls
            # on their performances even though the data is available.
            global_lm = last_minute_by_url.get(card.url, [])
            performances = _merge_global_lastminute_into_perfs(
                detail.get("performances") or [], global_lm,
            )
            # Final fallback: for performances still missing a book_url
            # (i.e. anything beyond the rolling ~5-day window SeatPlan
            # surfaces inline), construct the deep-link URL from the
            # show URL + JSON-LD date/time. The server validates these
            # against the real performance calendar — invalid (date,
            # time) combos bounce to the show page rather than 404, so
            # the worst-case fallback is graceful.
            n_built = _fill_constructed_book_urls(performances, card.url)
            if n_built:
                with lock:
                    constructed_urls[0] += n_built
            results[idx] = Show(
                url=card.url,
                slug=card.slug,
                sku=sku,
                name=card.name,
                image=card.image,
                description_short=card.description_short,
                listing_price_from=card.listing_price_from,
                listing_rating_value=card.rating_value,
                opens_text=card.opens_text,
                bordered=card.bordered,
                jsonld_low_price=card.jsonld_low_price,
                jsonld_high_price=card.jsonld_high_price,
                jsonld_currency=card.jsonld_currency,
                jsonld_availability=card.jsonld_availability,
                start_date=card.start_date,
                end_date=card.end_date,
                duration_iso=card.duration_iso,
                typical_age_range=card.typical_age_range,
                venue_name=card.venue_name,
                venue_url=card.venue_url,
                venue_address=card.venue_address,
                venue_geo=card.venue_geo,
                work_performed_name=card.work_performed_name,
                work_performed_wikipedia=card.work_performed_wikipedia,
                appears_in=sorted(appears_in_map.get(card.slug, [])),
                detail_rating_value=detail.get("detail_rating_value"),
                detail_rating_count=detail.get("detail_rating_count"),
                detail_review_count=detail.get("detail_review_count"),
                detail_low_price=detail.get("detail_low_price"),
                detail_price_valid_until=detail.get("detail_price_valid_until"),
                detail_currency=detail.get("detail_currency"),
                description_full=detail.get("description_full"),
                running_time_text=detail.get("running_time_text"),
                matinee_text=detail.get("matinee_text"),
                evening_text=detail.get("evening_text"),
                weekly_schedule=detail.get("weekly_schedule") or [],
                cast=detail.get("cast") or [],
                faq=detail.get("faq") or [],
                performances=performances,
                last_minute_listing=global_lm,
                detail_canonical=detail.get("detail_canonical"),
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
        ShowFailure(url=cards[i].url, slug=cards[i].slug,
                    error=errors[i] or "unknown")
        for i, s in enumerate(results) if s is None
    ]

    elapsed = time.monotonic() - t_start
    rate = len(cards) / elapsed if elapsed > 0 else 0
    log.info("Fetched %d/%d show details in %.1fs (%.2f req/s)",
             len(shows), len(cards), elapsed, rate)
    if sku_backfills[0]:
        log.info(
            "  sku backfilled from last-minute production_id for %d show(s) "
            "where the detail page didn't expose Product.sku",
            sku_backfills[0],
        )
    if constructed_urls[0]:
        log.info(
            "  book_url constructed from URL pattern for %d performance(s) "
            "beyond the rolling ~5-day window SeatPlan surfaces inline",
            constructed_urls[0],
        )
    return shows, failures, budget_exceeded


# ---------------------------------------------------------------------------
# Sanity checks (warn-but-write policy)
# ---------------------------------------------------------------------------

def run_sanity_checks(
    shows: list[Show], failures: list[ShowFailure], master_count: int,
) -> list[str]:
    """Inspect the scraped data for structural anomalies. Returns a list
    of human-readable warning strings; the policy is warn-but-write."""
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
    pct_missing(lambda s: not s.venue_name, "missing venue name", threshold=0.05)
    pct_missing(lambda s: not s.url, "missing url", threshold=0.0)
    # Most shows have a description but pre-opening shows may not
    pct_missing(lambda s: not s.description_short, "missing short description", threshold=0.10)
    # Performances: shows that are currently running should have at least
    # one. Shows not-yet-open (with opens_text) legitimately have none —
    # exclude those from the count.
    running_shows = [s for s in shows if not s.opens_text]
    if running_shows:
        no_perfs = sum(1 for s in running_shows if not s.performances)
        if no_perfs / len(running_shows) > 0.20:
            warnings.append(
                f"field-quality: {no_perfs}/{len(running_shows)} running "
                "shows have zero performances (>20% threshold)"
            )

    # Canonical URL sanity — the detail page's <link rel="canonical">
    # should match the URL we fetched. A wholesale mismatch means we're
    # being redirected or pulling the wrong page.
    canonical_mismatches = sum(
        1 for s in shows
        if s.detail_canonical and s.detail_canonical.rstrip("/") != s.url.rstrip("/")
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
        # After the URL-construction fallback, every performance with a
        # valid (date, time) should carry a book_url — the only legitimate
        # misses are perfs with malformed date/time fields. >10% missing
        # therefore indicates URL construction is silently failing or the
        # JSON-LD time format has shifted.
        if no_book_url / len(all_perfs) > 0.10:
            warnings.append(
                f"performance: {no_book_url}/{len(all_perfs)} performances "
                "lack book_url (>10% threshold) — URL construction may be "
                "failing; check time format in JSON-LD"
            )
        bad_prices = sum(
            1 for _, p in all_perfs
            if p.get("low_price") is not None and p["low_price"] <= 0
        )
        if bad_prices:
            warnings.append(
                f"price: {bad_prices} performances have non-positive low_price"
            )

    if failures:
        from collections import Counter
        kinds = Counter(f.error.split(":")[0] for f in failures)
        breakdown = ", ".join(f"{k}={v}" for k, v in kinds.most_common())
        warnings.append(f"fetch-failures: {len(failures)} shows failed ({breakdown})")

    return warnings


def compare_with_previous(new_shows: list[Show], previous_path: Path) -> list[str]:
    """Catch catastrophic regressions vs. yesterday's good output. Mirrors
    the OLT scraper's check."""
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

    # Use slug as the join key — it's the stable identifier across runs.
    new_slugs = {s.slug for s in new_shows}
    prev_slugs = {s["slug"] for s in prev_shows
                  if isinstance(s.get("slug"), str)}
    if prev_slugs:
        vanished = prev_slugs - new_slugs
        if len(vanished) > len(prev_slugs) * 0.30:
            warnings.append(
                f"prev-run: {len(vanished)}/{len(prev_slugs)} shows from previous "
                f"run missing today ({100*len(vanished)/len(prev_slugs):.0f}% churn)"
            )
    return warnings


def validate_data_ranges(shows: list[Show]) -> list[str]:
    """Range validation — catches the class of bugs where parsing succeeds
    but produces nonsense values (a £100,000 ticket, a 1999 closing date).
    Advisory only, never fails the scrape."""
    issues: list[str] = []
    current_year = datetime.now(timezone.utc).year

    bad_listing_price: list[tuple[str, str, float | None]] = []
    bad_perf_price: list[tuple[str, str | None]] = []
    bad_perf_dates: list[tuple[str, str]] = []
    bad_end_dates: list[tuple[str, str]] = []
    bad_ratings: list[tuple[str, float]] = []
    bad_urls: list[tuple[str, str]] = []

    def _price_ok(p):
        return p is None or (isinstance(p, (int, float)) and 0 < p < 10000)

    def _year_ok(y):
        return 2020 <= y <= current_year + 5

    for s in shows:
        for p_name, p_val in [
            ("listing_price_from", s.listing_price_from),
            ("jsonld_low_price", s.jsonld_low_price),
            ("jsonld_high_price", s.jsonld_high_price),
            ("detail_low_price", s.detail_low_price),
        ]:
            if not _price_ok(p_val):
                bad_listing_price.append((s.slug, p_name, p_val))

        # Min vs max sanity
        if (s.jsonld_low_price is not None and s.jsonld_high_price is not None
                and s.jsonld_low_price > s.jsonld_high_price):
            bad_listing_price.append(
                (s.slug, "low>high", (s.jsonld_low_price, s.jsonld_high_price))
            )

        # Performance prices and dates
        for p in s.performances:
            if not _price_ok(p.get("low_price")):
                bad_perf_price.append((s.slug, p.get("iso")))
            date_str = p.get("date")
            if date_str:
                try:
                    year = int(date_str[:4])
                    if not _year_ok(year):
                        bad_perf_dates.append((s.slug, date_str))
                except (ValueError, TypeError):
                    bad_perf_dates.append((s.slug, date_str))

        # end_date — ISO date or datetime
        if s.end_date:
            try:
                year = int(s.end_date[:4])
                if not _year_ok(year):
                    bad_end_dates.append((s.slug, s.end_date))
            except (ValueError, TypeError):
                bad_end_dates.append((s.slug, s.end_date))

        # Rating sanity (0-5 scale)
        for r_name, r_val in [
            ("listing_rating_value", s.listing_rating_value),
            ("detail_rating_value", s.detail_rating_value),
        ]:
            if r_val is not None and not (0 <= r_val <= 5):
                bad_ratings.append((s.slug, r_val))

        if s.url and not s.url.startswith(BASE):
            bad_urls.append((s.slug, s.url))

    if bad_listing_price:
        issues.append(
            f"data-range: {len(bad_listing_price)} listing-price anomalies "
            f"(expected 0–10000, e.g. show {bad_listing_price[0][0]}: "
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
    if bad_end_dates:
        issues.append(
            f"data-range: {len(bad_end_dates)} shows with implausible end_date"
        )
    if bad_ratings:
        issues.append(
            f"data-range: {len(bad_ratings)} ratings outside 0–5 scale"
        )
    if bad_urls:
        issues.append(
            f"data-range: {len(bad_urls)} shows have URLs outside the expected "
            f"seatplan.com origin (e.g. {bad_urls[0][1]})"
        )

    return issues


# ---------------------------------------------------------------------------
# Output (atomic write + rotation)
# ---------------------------------------------------------------------------

def rotate_output(path: Path, keep: int = DEFAULT_ROTATION_DEPTH) -> None:
    """Shift existing output files to make room for a new write.

    seatplan_london.json     →  seatplan_london.json.1
    seatplan_london.json.1   →  seatplan_london.json.2
    ...
    seatplan_london.json.4   →  seatplan_london.json.5     (oldest kept)
    seatplan_london.json.5   →  (deleted)
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
    """Atomically write the JSON output and embedded scrape report."""
    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": MASTER_URL,
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
        description="Scrape SeatPlan London (pure requests, parallel)."
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Only fetch details for this many shows (for testing).")
    p.add_argument("--out", type=Path, default=Path("seatplan_london.json"),
                   help="Output JSON file path (default: ./seatplan_london.json).")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel workers for detail pages (default: {DEFAULT_CONCURRENCY}).")
    p.add_argument("--no-tag-lists", action="store_true",
                   help="Skip the 5 filter slices + the global last-minute table; "
                        "shows will have empty appears_in arrays and no last_minute_listing entries.")
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
    scraped_at = datetime.now(timezone.utc)
    deadline = (t_start + args.max_runtime_seconds
                if args.max_runtime_seconds is not None else None)

    if args.dry_run:
        log.info("--dry-run: no output file will be written")
    if deadline is not None:
        log.info("Wall-clock budget: %ds", args.max_runtime_seconds)

    # Stage 1: master + tag lists + global last-minute table
    try:
        (master_cards, appears_in_map, tag_counts,
         last_minute_by_url, production_id_by_url) = fetch_master_and_tags(
            session, include_tag_lists=not args.no_tag_lists,
        )
    except requests.RequestException as e:
        log.error("Master listing fetch failed: %s — aborting (previous output preserved).", e)
        return EXIT_HARD_FAIL

    if not master_cards:
        log.error("No shows found on master listing — aborting (previous output preserved).")
        return EXIT_HARD_FAIL

    cards_to_fetch = master_cards
    if args.limit is not None:
        cards_to_fetch = master_cards[: args.limit]
        log.info("--limit applied: fetching details for %d/%d shows",
                 len(cards_to_fetch), len(master_cards))

    # Stage 2: detail pages
    shows, failures, budget_exceeded = fetch_all_details(
        session, cards_to_fetch, appears_in_map, last_minute_by_url,
        production_id_by_url,
        concurrency=args.concurrency,
        scraped_at=scraped_at,
        deadline=deadline,
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
        tag_lists_scraped=sorted(k for k, v in tag_counts.items() if v >= 0),
        tag_list_counts=tag_counts,
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
