"""
Official London Theatre scraper (pure requests, parallel)
=========================================================

Scrapes every show on https://officiallondontheatre.com/theatre-tickets/
(the master catalogue), then fetches each show's detail page for the rich
performance/calendar/cast data.

Why no Playwright?
------------------
Unlike TodayTix, OLT serves everything SSR. The master listing renders all
~127 shows in a single response (data-limit="200", data-total=~127) and each
show detail page embeds a complete `data-cal` JSON blob with every
performance, plus JSON-LD with cast/creative and an FAQPage. Plain HTTP
is faster and more reliable than a browser here.

The eight URLs we cover are not eight datasets — they're filtered views of
the same one. /theatre-tickets/ is the master; /todays-tickets/,
/special-offers/, /kids-week/, /london-musicals/, /plays-in-london/,
/family-friendly-shows/, and /todays-tickets/?tomorrow=1 are filter slices
of it. We use them as "tags" — for each show we record which of the seven
slices it appears in — but the real data depth comes from the show detail
page.

Setup
-----
    pip install requests beautifulsoup4

Usage
-----
    python olt_scraper.py                          # full scrape
    python olt_scraper.py --limit 5                # test with 5 shows
    python olt_scraper.py --out data/olt.json      # custom output path
    python olt_scraper.py --concurrency 24         # more parallel workers
    python olt_scraper.py --no-tag-lists           # skip the 7 filter slices

Output is a single JSON file:

    {
      "scraped_at": "2026-05-18T22:08:56+00:00",
      "source": "https://officiallondontheatre.com/theatre-tickets/",
      "show_count": 127,
      "performance_count": 4123,
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = "https://officiallondontheatre.com"
MASTER_URL = f"{BASE}/theatre-tickets/"

# The seven "tag" lists. We scrape each and record which shows appear in
# which, but the per-card payload is identical to the master so we don't
# re-extract fields from them — just the show IDs.
TAG_LISTS: dict[str, str] = {
    "today":           f"{BASE}/todays-tickets/?today=1&sort=name_asc",
    "tomorrow":        f"{BASE}/todays-tickets/?tomorrow=1&sort=name_asc",
    "special_offers":  f"{BASE}/special-offers/",
    "kids_week":       f"{BASE}/kids-week/",
    "musicals":        f"{BASE}/london-musicals/",
    "plays":           f"{BASE}/plays-in-london/",
    "family":          f"{BASE}/family-friendly-shows/",
}

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

# Application-level retry (parse errors, sparse HTML on first try). One
# retry only — anything more flaky than that is genuinely broken.
DETAIL_PARSE_RETRY_DELAY_S = 2.0

# Output file rotation depth — keeps olt_london.json, .1, .2, ..., .5
DEFAULT_ROTATION_DEPTH = 5

# Cap auto-pagination at this many API calls per stage so we never
# spin forever if the API gives bad totals.
MAX_API_PAGES = 10

# Exit codes — 0 clean, 1 hard failure (no output written), 2 wrote with warnings.
EXIT_CLEAN = 0
EXIT_HARD_FAIL = 1
EXIT_WARNINGS = 2

SHOW_URL_RE = re.compile(
    r"^https?://officiallondontheatre\.com/show/([a-z0-9\-]+)-(\d+)/?$"
)

# REST endpoint exposed on the filter bar (data-endpoint attribute).
# Used as a fallback when a tag list does no SSR (e.g. /todays-tickets/
# ships data-ssr-initial="0" and lazy-loads everything via this endpoint).
SHOWS_API_URL = f"{BASE}/wp-json/olt/v1/shows"

# data-query JSON template — what each tag list's filter looks like. We
# extract just the "behavioural" keys (the filter intent) and pass them
# to the API.
TAG_LIST_QUERY: dict[str, dict] = {
    "today":          {"today": "1"},
    "tomorrow":       {"tomorrow": "1"},
    "special_offers": {"offers": True, "list_base": "special-offers"},
    "kids_week":      {"list_base": "kids-week"},
    "musicals":       {"genre": ["Musical"], "list_base": "london-musicals"},
    "plays":          {"genre": ["Plays"], "list_base": "plays-in-london"},
    "family":         {"genre": ["Family"], "list_base": "family-friendly-shows"},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("olt")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ListingCard:
    """A card as it appears in any of the eight listing pages."""
    post_id: int
    name: str
    url: str
    image: str | None
    genres: list[str]
    min_price: float | None      # data-min-price (lowest actual price)
    max_price: float | None
    discount: float | None       # data-discount (always 0 in practice)
    price_label: str | None      # the visible "Tickets From £X" text
    badges: list[str]            # e.g. ["Show of the Week"]


@dataclass
class Performance:
    """One performance, sourced from the data-cal JSON on the detail page."""
    id: str | None
    date: str | None             # "2026-05-18"
    time: str | None             # "19:30"
    iso: str | None              # full ISO with offset
    min_price: float | None
    max_price: float | None
    available: bool
    labels: list[str]            # ["OLT"] or ["Special Offer"]
    save_pct: int | None         # 29 means 29% off
    bookable: bool
    book_url: str | None
    source: str | None           # "olt" or "tkts-online"


@dataclass
class TktsDeal:
    """A today's/tomorrow's-tickets editorial offer that may or may not appear
    in the show's regular calendar. The today's-tickets page surfaces TKTS-
    booth deals that sometimes pre-empt the show's own calendar — e.g. a
    last-minute seat released only via TKTS for tonight's performance, where
    the show's data-cal still shows the day as greyed out.

    Three layers of info:
      * card-level (always present): discount_pct, min_price, max_price —
        per-day snapshot that the regular /theatre-tickets/ listing doesn't
        surface (data-discount on that page is always 0). These come from
        data-* attrs on the article element.
      * row-level (best-effort): book_url, time, price_from — extracted from
        the Book Tickets row inside the card. The OLT frontend renders this
        row client-side, so it's often absent from the SSR HTML and stays
        None. Don't rely on it.
      * resolved (filled in post-fetch): date and matched_performances. The
        tag ("today"/"tomorrow") gets resolved to an ISO date in
        Europe/London time, and any performances on that date are copied
        in from show.performances[]. This is where the real booking URLs
        and times live."""
    tag: str                       # "today" or "tomorrow"
    # Card-level (always populated when the article has the attrs)
    discount_pct: float | None     # data-discount, e.g. 17.0 for "17% off"
    min_price: float | None        # data-min-price for the day
    max_price: float | None        # data-max-price for the day
    # Row-level (best-effort, often None because the row is JS-rendered)
    book_url: str | None
    time: str | None               # e.g. "7.30PM"
    price_from: float | None       # e.g. 163.0 from "from £163"
    raw_text: str | None           # short text excerpt for debugging
    # Resolved post-fetch from show.performances[]
    date: str | None = None        # ISO date (Europe/London) for this deal
    matched_performances: list[dict] = field(default_factory=list)


@dataclass
class CastMember:
    name: str | None
    role: str | None


@dataclass
class CreativeMember:
    role: str | None
    name: str | None


@dataclass
class FaqEntry:
    question: str | None
    answer: str | None


@dataclass
class Show:
    """Aggregate of listing card + show detail page."""
    # Identity
    id: int
    name: str
    url: str
    # From the master listing card
    image: str | None
    genres: list[str]
    listing_min_price: float | None       # data-min-price on card
    listing_max_price: float | None
    listing_price_label: str | None       # visible "Tickets From £X"
    badges: list[str]
    # Which of the seven filter slices this show appears in
    appears_in: list[str]
    # From the show detail page
    venue: dict | None                    # {"name": ..., "url": ...}
    description_text: str | None
    hero_image: str | None
    gallery: list[dict]                   # [{"src", "alt"}]
    closing: str | None             # value from "Closing" OR "Booking Until"
    closing_field_name: str | None  # which heading was present ("Closing" / "Booking Until")
    running_time: str | None
    genre: str | None
    age_content: str | None
    please_note: str | None
    category: str | None
    detail_low_price: float | None        # Product.lowPrice from JSON-LD
    detail_currency: str | None
    cast: list[dict]
    creative: list[dict]
    faq: list[dict]
    performances: list[dict]
    # Today's/tomorrow's-tickets editorial deals that may be missing from
    # the show's own calendar (TKTS booth-only seats etc). Keyed by tag.
    tkts_deals: list[dict] = field(default_factory=list)


@dataclass
class ShowFailure:
    id: int
    url: str
    error: str


@dataclass
class ScrapeReport:
    master_show_count: int
    succeeded_show_count: int
    failed_show_count: int
    tag_lists_scraped: list[str]
    tag_list_counts: dict[str, int]
    # How the master listing was obtained — "html" for the standard SSR
    # path, "api" if we fell back to the wp-json endpoint, "html+api" if
    # we used both (HTML first, then API to paginate past the page limit).
    master_source: str = "html"
    # Whether the wall-clock budget was hit (set via --max-runtime-seconds).
    # When True, the scrape proceeded with partial results.
    budget_exceeded: bool = False
    warnings: list[str] = field(default_factory=list)
    # Data-validation findings — distinct from operational warnings so
    # consumers can filter them separately (e.g. CI alert on warnings but
    # ignore validation noise).
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


def _strip_suffix(text: str, suffix: str) -> str:
    """Safe suffix strip — string-level, not character-set like rstrip()."""
    if text.endswith(suffix):
        return text[: -len(suffix)].strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Listing parser (used for all eight URLs)
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"£\s*(\d+(?:[.,]\d+)?)")
_TIME_RE = re.compile(
    r"\b(\d{1,2}[:.]\d{2}\s*(?:AM|PM|am|pm))\b"
    r"|\b(\d{1,2}\s*(?:AM|PM|am|pm))\b"
)
# Hosts known to host booking pages we'd want to keep as a TKTS deal URL.
# We intentionally allow any href except the show-detail page on the same
# origin — booking might be on seetickets, tktsonline, or a different
# subdomain. The exclusion list filters out the whole-card click target.
_NON_BOOKING_HREF_PATTERNS = (
    "officiallondontheatre.com/show/",   # whole-card click → show detail
    "officiallondontheatre.com/venue/",  # venue link
    "officiallondontheatre.com/category/",
)


def _looks_like_booking_href(href: str) -> bool:
    if not href or href.startswith("#") or href.startswith("javascript:"):
        return False
    href_l = href.lower()
    if any(p in href_l for p in _NON_BOOKING_HREF_PATTERNS):
        return False
    # Positive signals
    if "seetickets" in href_l or "/book" in href_l or "tkts" in href_l:
        return True
    # Allow any absolute http(s) URL that isn't on our excluded list
    return href_l.startswith("http")


def _parse_tkts_deals_from_card(article_soup, tag: str) -> list[dict]:
    """Extract today's/tomorrow's-tickets deal info from one card.

    These cards carry two layers of richer-than-master info:
      * Card-level attrs: data-discount (% off, often non-zero unlike the
        master listing where it's always 0), data-min-price, data-max-price
        — a per-day pricing snapshot.
      * Row-level info inside the card: each Book Tickets link plus its
        nearby time/price text.

    We always produce at least one deal entry per card (the card-level
    snapshot). If we find booking links, we produce one entry per link
    with row-level details filled in."""

    # Card-level snapshot (always extract these — they're cheap and useful)
    discount_pct = _to_float(article_soup.get("data-discount")) or None
    if discount_pct == 0.0:
        discount_pct = None
    card_min = _to_float(article_soup.get("data-min-price"))
    card_max = _to_float(article_soup.get("data-max-price"))

    # Row-level: find candidate booking links. Broad selector — anything
    # that isn't the whole-card click target. The whole-card <a> wraps
    # most of the card content and points to /show/{...}/.
    all_links = article_soup.find_all("a", href=True)
    booking_links = [a for a in all_links
                     if _looks_like_booking_href(a.get("href", ""))]

    # Card text (used to backfill time/price when not near a specific link)
    card_text = article_soup.get_text(" ", strip=True)
    card_price_match = _PRICE_RE.search(card_text)
    card_price = _to_float(card_price_match.group(1)) if card_price_match else None
    card_time_match = _TIME_RE.search(card_text)
    card_time = ((card_time_match.group(1) or card_time_match.group(2))
                 if card_time_match else None)

    deals: list[dict] = []

    if booking_links:
        # One deal per booking link, with row-local time/price
        for link in booking_links:
            nearby_text = ""
            node = link.parent
            for _ in range(4):
                if node is None:
                    break
                t = node.get_text(" ", strip=True)
                if t and len(t) >= 10:
                    nearby_text = t
                    break
                node = node.parent

            time_match = _TIME_RE.search(nearby_text) if nearby_text else None
            time_str = ((time_match.group(1) or time_match.group(2))
                        if time_match else card_time)
            price_match = _PRICE_RE.search(nearby_text) if nearby_text else None
            price = _to_float(price_match.group(1)) if price_match else card_price

            deals.append(asdict(TktsDeal(
                tag=tag,
                discount_pct=discount_pct,
                min_price=card_min,
                max_price=card_max,
                book_url=link.get("href"),
                time=time_str,
                price_from=price,
                raw_text=(nearby_text[:200] if nearby_text else None),
            )))
    elif (discount_pct is not None) or (card_min is not None) or card_time or card_price:
        # No booking link but we have at least some signal — emit one
        # card-level deal so the per-day pricing snapshot isn't lost.
        deals.append(asdict(TktsDeal(
            tag=tag,
            discount_pct=discount_pct,
            min_price=card_min,
            max_price=card_max,
            book_url=None,
            time=card_time,
            price_from=card_price,
            raw_text=(card_text[:200] if card_text else None),
        )))

    return deals


def _parse_cards_from_fragment(fragment_or_soup) -> list[ListingCard]:
    """Extract show cards from any HTML containing <article class='shows-grid-item'>
    elements. Accepts a raw HTML string or an already-parsed BeautifulSoup
    object. No wrapper-div required — used both by parse_listing (which scopes
    to .shows-grid__items first) and by the wp-json fallback (which returns
    a bare list of articles in an `html` field)."""
    if isinstance(fragment_or_soup, str):
        scope = BeautifulSoup(fragment_or_soup, "html.parser")
    else:
        scope = fragment_or_soup

    cards: list[ListingCard] = []
    for art in scope.select("article.shows-grid-item"):
        post_id_raw = art.get("data-post-id")
        if not post_id_raw or not str(post_id_raw).isdigit():
            continue

        link_el = art.select_one("a.shows-grid-item__link")
        url = link_el["href"] if link_el and link_el.has_attr("href") else ""
        if url and not url.startswith("http"):
            url = urljoin(BASE, url)

        img_el = art.select_one("img.shows-grid-item__img")
        image = img_el.get("src") if img_el else None

        price_el = art.select_one("[data-price-label]")
        price_label = price_el.get_text(strip=True) if price_el else None

        h_el = art.select_one(".shows-grid-item__details-heading")
        name = (h_el.get_text(strip=True) if h_el
                else _decode(art.get("data-name")) or "")

        genres_raw = _decode(art.get("data-genres")) or ""
        genres = [g.strip() for g in genres_raw.split(",") if g.strip()]

        badges = [b.get_text(strip=True)
                  for b in art.select(".shows-grid-item__badge")]

        cards.append(ListingCard(
            post_id=int(post_id_raw),
            name=name,
            url=url,
            image=image,
            genres=genres,
            min_price=_to_float(art.get("data-min-price")),
            max_price=_to_float(art.get("data-max-price")),
            discount=_to_float(art.get("data-discount")),
            price_label=price_label,
            badges=badges,
        ))
    return cards


def parse_listing(html_text: str) -> tuple[int, int, list[ListingCard]]:
    """Return (data_total, data_limit, cards). Works for any of the eight
    listing URLs — they all share one card template."""
    soup = BeautifulSoup(html_text, "html.parser")
    grid = soup.select_one("div.shows-grid__items[data-results]")
    if grid is None:
        return (0, 0, [])

    total = int(grid.get("data-total") or 0)
    limit = int(grid.get("data-limit") or 0)
    cards = _parse_cards_from_fragment(grid)
    return (total, limit, cards)


# ---------------------------------------------------------------------------
# Show detail parser
# ---------------------------------------------------------------------------

def _extract_data_cal(soup: BeautifulSoup) -> dict | None:
    cal = soup.select_one(".farlo-ui-ticket-calendar[data-cal]")
    if not cal:
        return None
    try:
        return json.loads(html.unescape(cal["data-cal"]))
    except (json.JSONDecodeError, KeyError):
        return None


def _extract_important_info(soup: BeautifulSoup) -> dict[str, str]:
    info: dict[str, str] = {}
    for item in soup.select(".show-important-information__item"):
        head = item.select_one("span.h4")
        body = item.select_one(
            ".show-important-information__item-content p, "
            ".notification-content p"
        )
        if head and body:
            info[head.get_text(strip=True)] = body.get_text(" ", strip=True)
    return info


def _extract_cast_creative(soup: BeautifulSoup) -> tuple[list[dict], list[dict]]:
    """Extract cast & creative from the show detail page.

    Layered fallback:
      1. Primary — the standard OLT markup with
         .cast-and-creative__cast / .cast-and-creative__creative
         containers and .cast-and-creative__content-item-name|role.
      2. Secondary (defensive) — if the primary selectors find nothing
         in a section that still has the wrapper, look for any <dl>/<dt>/<dd>
         pattern inside the section. Several WordPress themes
         have used this shape historically and the OLT theme might
         revert there during a redesign.

    JSON-LD-based recovery for creative team is in
    _extract_creative_from_jsonld; that's called separately in parse_show
    only when this returns empty creative.
    """
    cast: list[dict] = []
    creative: list[dict] = []

    cast_block = soup.select_one(".cast-and-creative__cast")
    if cast_block:
        for it in cast_block.select(".cast-and-creative__content-item"):
            name_el = it.select_one(".cast-and-creative__content-item-name")
            role_el = it.select_one(".cast-and-creative__content-item-role")
            # Format on the page: "<name> as" / "<role>"
            raw_name = name_el.get_text(strip=True) if name_el else ""
            raw_role = role_el.get_text(strip=True) if role_el else ""
            cast.append(asdict(CastMember(
                name=_strip_suffix(raw_name, " as") or None,
                role=raw_role or None,
            )))
        # Fallback: dl/dt/dd inside the same wrapper
        if not cast:
            cast.extend(_extract_dl_pairs(cast_block, "cast"))

    creative_block = soup.select_one(".cast-and-creative__creative")
    if creative_block:
        for it in creative_block.select(".cast-and-creative__content-item"):
            role_el = it.select_one(".cast-and-creative__content-item-role")
            name_el = it.select_one(".cast-and-creative__content-item-name")
            # Format on the page: "<role> -" / "<name>"
            raw_role = role_el.get_text(strip=True) if role_el else ""
            raw_name = name_el.get_text(strip=True) if name_el else ""
            creative.append(asdict(CreativeMember(
                role=_strip_suffix(raw_role, " -") or None,
                name=raw_name or None,
            )))
        if not creative:
            creative.extend(_extract_dl_pairs(creative_block, "creative"))

    return cast, creative


def _extract_dl_pairs(block, kind: str) -> list[dict]:
    """Fallback parser for cast/creative blocks that use a dl/dt/dd
    layout instead of the standard div-based structure. The pattern is:

        <dl>
          <dt>Role or Name</dt>
          <dd>Name or Role</dd>
          ...
        </dl>

    kind='cast' → emits CastMember dicts (name in dt, role in dd typical
    for casts). kind='creative' → emits CreativeMember dicts (role in dt,
    name in dd typical). If the order is reversed by the CMS, the data is
    still captured — just with field labels possibly swapped, which is
    less bad than missing the data entirely."""
    out: list[dict] = []
    for dl in block.select("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            dt_text = dt.get_text(strip=True)
            dd_text = dd.get_text(strip=True)
            if not dt_text and not dd_text:
                continue
            if kind == "cast":
                out.append(asdict(CastMember(
                    name=_strip_suffix(dt_text, " as") or None,
                    role=dd_text or None,
                )))
            else:
                out.append(asdict(CreativeMember(
                    role=_strip_suffix(dt_text, " -") or None,
                    name=dd_text or None,
                )))
    return out


def _extract_jsonld_blocks(soup: BeautifulSoup) -> list[dict]:
    out = []
    for s in soup.select("script[type='application/ld+json']"):
        if not s.string:
            continue
        try:
            out.append(json.loads(s.string))
        except json.JSONDecodeError:
            pass
    return out


def _find_product_node(jsonld_blocks: list[dict]) -> dict | None:
    """Find the schema.org Product node (carries category + lowPrice)."""
    for block in jsonld_blocks:
        if not isinstance(block, dict):
            continue
        graph = block.get("@graph") or [block]
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
                for q in block.get("mainEntity", [])
            ]
    return []


def _extract_creative_from_jsonld(jsonld_blocks: list[dict]) -> list[dict]:
    """Fallback: pull creative team out of schema.org Person nodes that have
    a jobTitle. Used when the visible Cast & Creative HTML block is empty
    (some shows publish their creatives via schema only, or not at all)."""
    out: list[dict] = []
    seen: set[tuple[str | None, str | None]] = set()
    for block in jsonld_blocks:
        if not isinstance(block, dict):
            continue
        graph = block.get("@graph") or [block]
        for node in graph:
            if not isinstance(node, dict) or node.get("@type") != "Person":
                continue
            job = node.get("jobTitle")
            name = node.get("name")
            if not job:
                continue   # cast members don't get jobTitle, only creatives do
            key = (job, name)
            if key in seen:
                continue
            seen.add(key)
            out.append(asdict(CreativeMember(role=job, name=name)))
    return out


def parse_show(html_text: str) -> dict:
    """Return a dict of all detail fields (no ID/listing fields — those come
    from the listing card)."""
    soup = BeautifulSoup(html_text, "html.parser")

    # Title — first text node of h1.show-details__title
    name = ""
    title_el = soup.select_one("h1.show-details__title")
    if title_el:
        for c in title_el.contents:
            if isinstance(c, str) and c.strip():
                name = c.strip()
                break

    # Hero artwork is inline-styled background-image
    hero = None
    artwork = soup.select_one(".show-details__artwork")
    if artwork and artwork.has_attr("style"):
        m = re.search(r"url\(['\"]?([^'\")]+)", artwork["style"])
        if m:
            hero = m.group(1)

    # Venue link
    venue_el = soup.select_one(".show-details__venue-content-venue-name a")
    venue = None
    if venue_el:
        venue = {
            "name": venue_el.get_text(strip=True),
            "url": venue_el["href"] if venue_el.has_attr("href") else None,
        }

    # Description (text body of the show)
    desc_el = soup.select_one(".show-details__text")
    description_text = desc_el.get_text("\n", strip=True) if desc_el else None

    # Gallery — the carousel images
    gallery: list[dict] = []
    seen_srcs: set[str] = set()
    for img in soup.select(".site-flexible-carousel__items img"):
        src = img.get("data-src") or img.get("src")
        if src and src not in seen_srcs:
            seen_srcs.add(src)
            gallery.append({"src": src, "alt": img.get("alt") or ""})

    # Important Info accordion
    info = _extract_important_info(soup)

    # The "end date" heading is sometimes "Closing" and sometimes "Booking Until"
    # (e.g. 1536 uses Booking Until). Same semantic field: the last date
    # tickets are on sale. Try both, record which one was present.
    closing_value: str | None = None
    closing_field_name: str | None = None
    for heading in ("Closing", "Booking Until"):
        if info.get(heading):
            closing_value = info[heading]
            closing_field_name = heading
            break

    # Cast & Creative accordion (visible HTML block)
    cast, creative = _extract_cast_creative(soup)

    # JSON-LD blocks → Product (for category, lowPrice), FAQPage, and Person
    # nodes (fallback for cast/creative when the visible block is sparse).
    jsonld_blocks = _extract_jsonld_blocks(soup)
    product = _find_product_node(jsonld_blocks) or {}
    product_offers = product.get("offers") or {}
    faq = _find_faq_entries(jsonld_blocks)

    # JSON-LD fallback: if the visible creative block was empty, sniff Person
    # nodes with a jobTitle out of the schema data. Some shows have richer
    # JSON-LD than visible markup (and vice versa).
    if not creative:
        creative = _extract_creative_from_jsonld(jsonld_blocks)

    # data-cal JSON → all performances
    cal = _extract_data_cal(soup)
    performances: list[dict] = []
    detail_currency = None
    if cal:
        detail_currency = cal.get("currency")
        for p in cal.get("performances", []):
            performances.append(asdict(Performance(
                id=p.get("id"),
                date=p.get("date"),
                time=p.get("time"),
                iso=p.get("iso"),
                min_price=p.get("min"),
                max_price=p.get("max"),
                available=bool(p.get("avail")),
                labels=p.get("labels") or [],
                save_pct=p.get("savePct"),
                bookable=bool(p.get("bookable")),
                book_url=p.get("bookUrl"),
                source=p.get("source"),
            )))

    return {
        "name": name,
        "venue": venue,
        "description_text": description_text,
        "hero_image": hero,
        "gallery": gallery,
        "closing": closing_value,
        "closing_field_name": closing_field_name,
        "running_time": info.get("Running Time"),
        "genre": info.get("Genre"),
        "age_content": info.get("Age & Content"),
        "please_note": info.get("Please Note:"),
        "category": product.get("category"),
        "detail_low_price": _to_float(product_offers.get("lowPrice")),
        "detail_currency": detail_currency,
        "cast": cast,
        "creative": creative,
        "faq": faq,
        "performances": performances,
    }


# ---------------------------------------------------------------------------
# Stage 1 — master listing + tag lists
# ---------------------------------------------------------------------------

def _fetch_master_via_api(
    session: requests.Session,
    offset: int = 0,
    page_size: int = 200,
    max_pages: int = MAX_API_PAGES,
) -> tuple[list[ListingCard], int | None]:
    """Fetch master-listing cards via the wp-json/olt/v1/shows endpoint.

    Used in two scenarios:
      * Fallback: when the SSR HTML on /theatre-tickets/ parses to 0
        cards (CMS redesign, layout change, partial render). The API
        returns the same card markup so existing parsers still apply.
      * Pagination: when SSR returns data-total > data-limit (catalogue
        grew past the page's hardcoded limit). We pick up where the SSR
        left off and merge.

    Returns (cards, api_total) — api_total is the total count the API
    reports (useful for sanity-checking how many more pages to fetch),
    or None if the response didn't include one.
    """
    cards: list[ListingCard] = []
    seen_ids: set[int] = set()
    api_total: int | None = None

    for page in range(max_pages):
        params = {"limit": page_size, "offset": offset + page * page_size}
        try:
            resp = session.get(SHOWS_API_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            log.warning("master API page %d failed: %s", page, e)
            break

        if not isinstance(data, dict):
            log.warning("master API page %d unexpected type %s",
                        page, type(data).__name__)
            break

        api_total = data.get("total") if api_total is None else api_total
        html_fragment = data.get("html") or ""
        if not html_fragment.strip():
            # No more cards — either we're past the end, or the page is empty
            break

        page_cards = _parse_cards_from_fragment(html_fragment)
        added = 0
        for c in page_cards:
            if c.post_id not in seen_ids:
                seen_ids.add(c.post_id)
                cards.append(c)
                added += 1
        log.info("  master API page %d: %d cards (%d new, %d total so far)",
                 page + 1, len(page_cards), added, len(cards))

        # Stop early if the API has nothing new (defensive — protects
        # against bad totals or repeating-page bugs in the endpoint).
        if added == 0:
            break
        # If we've passed the reported total, stop.
        if api_total is not None and len(cards) + offset >= api_total:
            break

    return cards, api_total


def fetch_master_and_tags(
    session: requests.Session, include_tag_lists: bool,
) -> tuple[list[ListingCard], dict[int, list[str]], dict[str, int],
           dict[int, list[dict]], str]:
    """Returns (master_cards, appears_in_map, tag_list_counts,
    deals_by_show_id, master_source).

    master_source is "html" / "api" / "html+api" depending on which paths
    were used to assemble the master list.
    """
    log.info("Fetching master listing: %s", MASTER_URL)
    master_source = "html"
    master_cards: list[ListingCard] = []
    total = 0
    limit = 0

    try:
        resp = session.get(MASTER_URL, timeout=30)
        resp.raise_for_status()
        total, limit, master_cards = parse_listing(resp.text)
        log.info("  master HTML: total=%d limit=%d parsed=%d",
                 total, limit, len(master_cards))
    except requests.RequestException as e:
        # HTML path failed entirely — try the API.
        log.warning("  master HTML fetch failed: %s — trying API fallback", e)
        master_cards = []

    # FALLBACK: HTML returned zero cards (CMS redesign, layout change).
    # The API returns the same card HTML, so existing parsers apply.
    if not master_cards:
        log.warning("  master HTML returned 0 cards — falling back to wp-json API")
        api_cards, api_total = _fetch_master_via_api(session)
        if api_cards:
            master_cards = api_cards
            master_source = "api"
            log.info("  master API fallback succeeded: %d cards (api_total=%s)",
                     len(master_cards), api_total)
        else:
            log.error("  master API fallback also returned 0 cards")

    # PAGINATION: SSR'd HTML had fewer cards than data-total reports
    # (catalogue grew past the hardcoded data-limit). Top up via API.
    elif total and total > len(master_cards):
        log.info("  master HTML under-rendered (%d/%d) — paginating via API",
                 len(master_cards), total)
        extra_cards, _ = _fetch_master_via_api(
            session, offset=len(master_cards),
        )
        seen = {c.post_id for c in master_cards}
        added = 0
        for c in extra_cards:
            if c.post_id not in seen:
                seen.add(c.post_id)
                master_cards.append(c)
                added += 1
        if added:
            log.info("  master API pagination added %d cards → %d total",
                     added, len(master_cards))
            master_source = "html+api"

    appears_in_map: dict[int, list[str]] = {}
    tag_list_counts: dict[str, int] = {}
    deals_by_show_id: dict[int, list[dict]] = {}

    if include_tag_lists:
        for tag, url in TAG_LISTS.items():
            try:
                resp = session.get(url, timeout=30)
                resp.raise_for_status()
                t, l, cards = parse_listing(resp.text)
                log.info("  tag %-16s total=%-3d parsed=%d  %s",
                         f"'{tag}'", t, len(cards), url)

                ids_in_tag: list[int] = [c.post_id for c in cards]

                # If the page reports more shows than it server-side-rendered,
                # try the JSON API endpoint to backfill (and capture
                # per-card deal info for today/tomorrow).
                if t > len(cards):
                    log.info("  tag '%s' under-rendered (%d/%d) — querying API",
                             tag, len(cards), t)
                    api_ids, api_deals = _fetch_tag_ids_via_api(session, tag, t)
                    if api_ids:
                        existing = set(ids_in_tag)
                        for vid in api_ids:
                            if vid not in existing:
                                existing.add(vid)
                                ids_in_tag.append(vid)
                        log.info("  tag '%s' API backfill: %d → %d IDs",
                                 tag, len(cards), len(ids_in_tag))
                    elif t > 0:
                        log.warning(
                            "  tag '%s' total=%d but only %d IDs obtained "
                            "(API fallback returned nothing) — tag membership "
                            "incomplete",
                            tag, t, len(ids_in_tag),
                        )
                    # Merge in any extracted deals
                    for sid, ds in api_deals.items():
                        deals_by_show_id.setdefault(sid, []).extend(ds)

                tag_list_counts[tag] = len(ids_in_tag)
                for show_id in ids_in_tag:
                    appears_in_map.setdefault(show_id, []).append(tag)
            except requests.RequestException as e:
                log.warning("  tag list '%s' failed: %s — skipping", tag, e)
                tag_list_counts[tag] = -1

    return (master_cards, appears_in_map, tag_list_counts,
            deals_by_show_id, master_source)


# ---------------------------------------------------------------------------
# Stage 2 — fetch each show detail page in parallel
# ---------------------------------------------------------------------------

def _fetch_tag_ids_via_api(
    session: requests.Session, tag: str, expected_total: int,
) -> tuple[list[int], dict[int, list[dict]]]:
    """Fallback for tag-list pages that don't SSR everything (today, tomorrow).

    The wp-json/olt/v1/shows endpoint returns a JSON object of shape
        {"html": "<article class='shows-grid-item' ...>...",
         "total": int, "offset": int, "limit": int, ... }
    — pre-rendered HTML containing the same card markup we parse on the
    regular listing pages. We reuse the card parser on data["html"]; the
    other fields (total, sotw_id, datalayer, structured_data) we ignore.

    For 'today' and 'tomorrow', we also extract per-card deal info
    (booking URL, time, price) because the today's-tickets page surfaces
    editorial TKTS-booth offers that may not appear in the show's own
    calendar.

    Returns (ids, deals_by_show_id) where deals_by_show_id is non-empty
    only for today/tomorrow.
    """
    query = TAG_LIST_QUERY.get(tag)
    if not query:
        return [], {}

    params = {**query, "limit": max(expected_total + 10, 100), "offset": 0}
    try:
        resp = session.get(SHOWS_API_URL, params=params, timeout=30)
    except requests.RequestException as e:
        log.warning("  tag '%s' API request failed: %s", tag, e)
        return [], {}

    log.info("  tag '%s' API → %d %s", tag, resp.status_code, resp.url)

    if resp.status_code >= 400:
        snippet = resp.text[:200].replace("\n", " ") if resp.text else "(empty)"
        log.warning("  tag '%s' API returned %d — body excerpt: %s",
                    tag, resp.status_code, snippet)
        return [], {}

    ct = (resp.headers.get("Content-Type") or "").lower()
    if "json" not in ct:
        snippet = resp.text[:200].replace("\n", " ") if resp.text else "(empty)"
        log.warning("  tag '%s' API returned non-JSON content-type %r — body excerpt: %s",
                    tag, ct, snippet)
        return [], {}

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        log.warning("  tag '%s' API JSON decode failed: %s", tag, e)
        return [], {}

    if not isinstance(data, dict):
        log.warning("  tag '%s' API returned unexpected type %s",
                    tag, type(data).__name__)
        return [], {}

    html_fragment = data.get("html")
    if not isinstance(html_fragment, str) or not html_fragment.strip():
        log.warning(
            "  tag '%s' API returned no 'html' field (or empty). "
            "Top-level keys: %s",
            tag, list(data.keys())[:10],
        )
        return [], {}

    # Parse the basic listing cards (gets IDs + the common fields).
    cards = _parse_cards_from_fragment(html_fragment)
    api_total = data.get("total")
    log.info("  tag '%s' API extracted %d IDs (API total=%s)",
             tag, len(cards), api_total)

    # For today/tomorrow, also extract per-card deal info (booking URL,
    # time, price). These are editorial offers that may not be in the
    # show's regular calendar.
    deals_by_show_id: dict[int, list[dict]] = {}
    if tag in ("today", "tomorrow"):
        soup = BeautifulSoup(html_fragment, "html.parser")
        articles = soup.select("article.shows-grid-item")
        cards_with_booking_url = 0
        cards_with_only_card_level = 0
        cards_with_nothing = 0
        first_nothing_html: str | None = None
        first_nothing_hrefs: list[str] = []
        first_nothing_text_len: int = 0
        for art in articles:
            pid_raw = art.get("data-post-id")
            if not pid_raw or not str(pid_raw).isdigit():
                continue
            pid = int(pid_raw)
            deals = _parse_tkts_deals_from_card(art, tag)
            if deals:
                if any(d.get("book_url") for d in deals):
                    cards_with_booking_url += 1
                else:
                    cards_with_only_card_level += 1
                deals_by_show_id.setdefault(pid, []).extend(deals)
            else:
                cards_with_nothing += 1
                if first_nothing_html is None:
                    first_nothing_html = str(art)[:2000]
                    first_nothing_hrefs = [
                        a.get("href", "") for a in art.find_all("a", href=True)
                    ]
                    first_nothing_text_len = len(art.get_text(" ", strip=True))

        total_cards = len(articles)
        log.info(
            "  tag '%s' card-level extraction: %d with booking URL, "
            "%d with card-level info only, %d empty (total %d)",
            tag, cards_with_booking_url, cards_with_only_card_level,
            cards_with_nothing, total_cards,
        )
        if cards_with_nothing and first_nothing_html:
            log.warning(
                "  tag '%s' first empty card — text_length=%d, hrefs=%s, "
                "HTML excerpt: %s",
                tag, first_nothing_text_len, first_nothing_hrefs[:6],
                first_nothing_html.replace("\n", " ")[:1500],
            )

    return [c.post_id for c in cards], deals_by_show_id


def fetch_show_detail(
    session: requests.Session, card: ListingCard,
) -> tuple[dict | None, str | None]:
    """Fetch and parse a single show's detail page.

    Two layers of retry above urllib3's network-level retries (which
    handle 429/5xx/connection errors): if either the request raises or
    parsing throws, we retry once after a short delay. Catches the
    transient cases — mid-deploy partial renders, momentary parse-time
    aberrations. More than one retry per show isn't worth it; that
    territory is "genuinely broken, log and move on".
    """
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
            return parse_show(resp.text), None
        except Exception as e:
            last_err = f"parse: {type(e).__name__}: {e}"
            if attempt == 1:
                # Parse errors on retry are usually structural, but
                # transient ones do happen (mid-deploy partial HTML).
                time.sleep(DETAIL_PARSE_RETRY_DELAY_S)
                continue
            return None, last_err

    return None, last_err or "unknown"


def fetch_all_details(
    session: requests.Session,
    cards: list[ListingCard],
    appears_in_map: dict[int, list[str]],
    deals_by_show_id: dict[int, list[dict]],
    concurrency: int,
    deadline: float | None = None,
) -> tuple[list[Show], list[ShowFailure], bool]:
    """Returns (shows, failures, budget_exceeded).

    deadline: a time.monotonic() value past which we stop submitting new
    work. In-flight requests run to completion, but no new ones get
    queued. The third return value reports whether the deadline was hit
    (used by main() to set budget_exceeded on the report and exit with
    a warning code rather than clean)."""
    log.info("Fetching %d show detail pages with %d workers...",
             len(cards), concurrency)
    t_start = time.monotonic()
    budget_exceeded = False

    results: list[Show | None] = [None] * len(cards)
    errors: list[str | None] = [None] * len(cards)
    progress = [0]
    lock = Lock()

    def task(idx: int, card: ListingCard) -> None:
        # If deadline passed before we even started this task, skip it
        # — the result will be a "skipped: budget" failure entry.
        if deadline is not None and time.monotonic() > deadline:
            errors[idx] = "skipped: wall-clock budget exceeded"
            with lock:
                progress[0] += 1
            return

        detail, err = fetch_show_detail(session, card)
        if detail is not None:
            results[idx] = Show(
                id=card.post_id,
                name=detail.get("name") or card.name,
                url=card.url,
                image=card.image,
                genres=card.genres,
                listing_min_price=card.min_price,
                listing_max_price=card.max_price,
                listing_price_label=card.price_label,
                badges=card.badges,
                appears_in=sorted(appears_in_map.get(card.post_id, [])),
                venue=detail.get("venue"),
                description_text=detail.get("description_text"),
                hero_image=detail.get("hero_image"),
                gallery=detail.get("gallery") or [],
                closing=detail.get("closing"),
                closing_field_name=detail.get("closing_field_name"),
                running_time=detail.get("running_time"),
                genre=detail.get("genre"),
                age_content=detail.get("age_content"),
                please_note=detail.get("please_note"),
                category=detail.get("category"),
                detail_low_price=detail.get("detail_low_price"),
                detail_currency=detail.get("detail_currency"),
                cast=detail.get("cast") or [],
                creative=detail.get("creative") or [],
                faq=detail.get("faq") or [],
                performances=detail.get("performances") or [],
                tkts_deals=deals_by_show_id.get(card.post_id, []),
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
        ShowFailure(id=cards[i].post_id, url=cards[i].url, error=errors[i] or "unknown")
        for i, s in enumerate(results) if s is None
    ]

    elapsed = time.monotonic() - t_start
    rate = len(cards) / elapsed if elapsed > 0 else 0
    log.info("Fetched %d/%d show details in %.1fs (%.2f req/s)",
             len(shows), len(cards), elapsed, rate)
    return shows, failures, budget_exceeded


# ---------------------------------------------------------------------------
# Sanity checks (warn-but-write policy, mirrors TodayTix scraper)
# ---------------------------------------------------------------------------

def run_sanity_checks(
    shows: list[Show], failures: list[ShowFailure], master_count: int,
) -> list[str]:
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
    pct_missing(lambda s: not (s.venue and s.venue.get("name")), "missing venue", threshold=0.05)
    pct_missing(lambda s: not s.running_time, "missing running time", threshold=0.20)
    pct_missing(lambda s: not s.category, "missing category", threshold=0.10)
    pct_missing(lambda s: not s.performances, "zero performances", threshold=0.20)
    pct_missing(lambda s: not s.creative, "no creative team", threshold=0.60)

    all_perfs = [(s, p) for s in shows for p in s.performances]
    if all_perfs:
        no_book_url = sum(1 for _, p in all_perfs if not p.get("book_url"))
        if no_book_url:
            warnings.append(
                f"performance: {no_book_url}/{len(all_perfs)} performances lack book_url"
            )
        bad_prices = sum(
            1 for _, p in all_perfs
            if p.get("min_price") is not None and p["min_price"] <= 0
        )
        if bad_prices:
            warnings.append(
                f"price: {bad_prices} performances have non-positive min_price"
            )

    if failures:
        from collections import Counter
        kinds = Counter(f.error.split(":")[0] for f in failures)
        breakdown = ", ".join(f"{k}={v}" for k, v in kinds.most_common())
        warnings.append(f"fetch-failures: {len(failures)} shows failed ({breakdown})")

    return warnings


def compare_with_previous(new_shows: list[Show], previous_path: Path) -> list[str]:
    """Catch catastrophic regressions vs. yesterday's good output."""
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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _enrich_tkts_deals(shows: list[Show], scraped_at: datetime) -> None:
    """Resolve each tkts_deal's tag ('today'/'tomorrow') to an ISO date in
    Europe/London time, then attach any performances on that date from
    the show's own data-cal.

    This is where the deals get their actual booking URLs and times: the
    OLT frontend renders the Book Tickets row client-side, so the SSR
    HTML we scrape on /todays-tickets/ doesn't include book_url or time
    in the card. But the same TKTS performances are also listed in each
    show's data-cal (sourced from a separate feed) with full booking
    info, so we can just join on date."""
    try:
        from zoneinfo import ZoneInfo
        london_tz = ZoneInfo("Europe/London")
    except Exception as e:
        # Windows without the tzdata package, or any other zoneinfo
        # failure — fall back to a fixed +01:00 offset which is correct
        # for British Summer Time (late March → late October). Will be
        # wrong by an hour in winter, but the runtime of midnight gives
        # a small enough window that this rarely flips the date.
        log.warning("zoneinfo unavailable (%s) — falling back to fixed +01:00 offset; "
                    "install 'tzdata' on Windows for correct GMT/BST handling", e)
        london_tz = timezone(timedelta(hours=1))

    london_now = scraped_at.astimezone(london_tz)
    today_iso = london_now.date().isoformat()
    tomorrow_iso = (london_now.date() + timedelta(days=1)).isoformat()
    tag_to_date = {"today": today_iso, "tomorrow": tomorrow_iso}

    # Slim view of a performance — drop full duplication; keep the fields
    # you'd actually want for a "what's on offer" view.
    def slim(p: dict) -> dict:
        return {
            "id": p.get("id"),
            "time": p.get("time"),
            "iso": p.get("iso"),
            "min_price": p.get("min_price"),
            "max_price": p.get("max_price"),
            "labels": p.get("labels") or [],
            "save_pct": p.get("save_pct"),
            "bookable": p.get("bookable"),
            "book_url": p.get("book_url"),
            "source": p.get("source"),
        }

    enriched = 0
    ghost = 0
    for show in shows:
        if not show.tkts_deals:
            continue
        for deal in show.tkts_deals:
            target_date = tag_to_date.get(deal.get("tag"))
            if not target_date:
                continue
            deal["date"] = target_date
            matches = [slim(p) for p in show.performances
                       if p.get("date") == target_date]
            deal["matched_performances"] = matches
            if matches:
                enriched += 1
            else:
                ghost += 1

    log.info(
        "Enriched %d tkts_deals with matched performances "
        "(%d ghost deals — tagged today/tomorrow but no matching perf in data-cal)",
        enriched, ghost,
    )


def validate_data_ranges(shows: list[Show]) -> list[str]:
    """Range validation across scraped data — catches the class of bugs
    where parsing succeeds but produces nonsense values (a £100,000
    ticket, a 1999 closing date, an empty cast member name).

    Returns a list of validation warning strings. Per-show issues get
    summarised (e.g. "12 shows have prices outside 0–10000") rather than
    listed per-show, to keep the warnings section readable.

    Validation runs are advisory — they never fail the scrape, just
    surface oddities for human review."""
    issues: list[str] = []
    current_year = datetime.now(timezone.utc).year

    bad_listing_price = []
    bad_perf_price = []
    bad_perf_dates = []
    bad_closing = []
    empty_cast_names = []
    empty_creative_names = []
    bad_urls = []

    def _price_ok(p):
        return p is None or (isinstance(p, (int, float)) and 0 < p < 10000)

    def _year_ok(y):
        return 2020 <= y <= current_year + 5

    for s in shows:
        # Listing prices
        for p_name, p_val in [("listing_min_price", s.listing_min_price),
                              ("listing_max_price", s.listing_max_price),
                              ("detail_low_price", s.detail_low_price)]:
            if not _price_ok(p_val):
                bad_listing_price.append((s.id, p_name, p_val))

        # Min vs max sanity
        if (s.listing_min_price is not None and s.listing_max_price is not None
                and s.listing_min_price > s.listing_max_price):
            bad_listing_price.append((s.id, "min>max",
                                      (s.listing_min_price, s.listing_max_price)))

        # Performance prices and dates
        for p in s.performances:
            if not _price_ok(p.get("min_price")) or not _price_ok(p.get("max_price")):
                bad_perf_price.append((s.id, p.get("id")))
            date_str = p.get("date")
            if date_str:
                try:
                    year = int(date_str[:4])
                    if not _year_ok(year):
                        bad_perf_dates.append((s.id, p.get("id"), date_str))
                except (ValueError, TypeError):
                    bad_perf_dates.append((s.id, p.get("id"), date_str))

        # Closing date — DD/MM/YYYY format from OLT
        if s.closing:
            try:
                parts = s.closing.split("/")
                if len(parts) == 3:
                    year = int(parts[2])
                    if not _year_ok(year):
                        bad_closing.append((s.id, s.closing))
            except (ValueError, IndexError):
                bad_closing.append((s.id, s.closing))

        # Cast/creative non-empty names
        for c in s.cast:
            if c.get("name") is not None and not str(c["name"]).strip():
                empty_cast_names.append(s.id)
                break
        for c in s.creative:
            if c.get("name") is not None and not str(c["name"]).strip():
                empty_creative_names.append(s.id)
                break

        # URLs look sane
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
            f"dates (expected {2020}–{current_year+5}, e.g. {bad_perf_dates[0][2]})"
        )
    if bad_closing:
        issues.append(
            f"data-range: {len(bad_closing)} shows with implausible closing dates "
            f"(e.g. {bad_closing[0][1]!r})"
        )
    if empty_cast_names:
        issues.append(f"data-range: {len(empty_cast_names)} shows have cast entries with empty names")
    if empty_creative_names:
        issues.append(f"data-range: {len(empty_creative_names)} shows have creative entries with empty names")
    if bad_urls:
        issues.append(
            f"data-range: {len(bad_urls)} shows have URLs outside the expected "
            f"officiallondontheatre.com origin (e.g. {bad_urls[0][1]})"
        )

    return issues


def rotate_output(path: Path, keep: int = DEFAULT_ROTATION_DEPTH) -> None:
    """Shift existing output files to make room for a new write.

    olt_london.json     →  olt_london.json.1
    olt_london.json.1   →  olt_london.json.2
    ...
    olt_london.json.4   →  olt_london.json.5     (oldest kept)
    olt_london.json.5   →  (deleted)

    Allows recovery from accidentally-degraded runs — a bad scrape
    overwriting a good output is bounded to one ring slot, not total loss.
    """
    if not path.exists():
        return  # nothing to rotate

    # Walk from oldest to youngest so each shift's destination is free.
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
                # Continue with remaining slots rather than aborting


def write_output(shows: list[Show], path: Path, report: ScrapeReport) -> None:
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
        description="Scrape Official London Theatre (pure requests, parallel)."
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Only fetch details for this many shows (for testing).")
    p.add_argument("--out", type=Path, default=Path("olt_london.json"),
                   help="Output JSON file path (default: ./olt_london.json).")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel workers for detail pages (default: {DEFAULT_CONCURRENCY}).")
    p.add_argument("--no-tag-lists", action="store_true",
                   help="Skip scraping the 7 filter slices (today/offers/musicals/etc.); "
                        "shows will have empty appears_in arrays.")
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
                   help="Do everything except write the output file. "
                        "Useful for testing parser changes against live data.")
    args = p.parse_args(argv)

    # Exit codes:
    #   0  clean success (no warnings, output written)
    #   1  hard failure (no output written, previous preserved)
    #   2  success but with warnings (output written, but with anomalies)

    session = build_session(pool_size=max(args.concurrency, 8))
    t_start = time.monotonic()
    deadline = (t_start + args.max_runtime_seconds
                if args.max_runtime_seconds is not None else None)

    def _budget_remaining() -> str:
        if deadline is None:
            return "unlimited"
        remaining = deadline - time.monotonic()
        return f"{max(0, int(remaining))}s remaining"

    if args.dry_run:
        log.info("--dry-run: no output file will be written")
    if deadline is not None:
        log.info("Wall-clock budget: %ds", args.max_runtime_seconds)

    # Stage 1: master + tag lists
    try:
        (master_cards, appears_in_map, tag_counts, deals_by_show_id,
         master_source) = fetch_master_and_tags(
            session, include_tag_lists=not args.no_tag_lists,
        )
    except requests.RequestException as e:
        log.error("Master listing fetch failed: %s — aborting (previous output preserved).", e)
        return EXIT_HARD_FAIL

    if not master_cards:
        log.error("No shows found on master listing (HTML or API) — "
                  "aborting (previous output preserved).")
        return EXIT_HARD_FAIL

    log.info("Master assembled from '%s' source: %d shows (%s)",
             master_source, len(master_cards), _budget_remaining())

    cards_to_fetch = master_cards
    if args.limit is not None:
        cards_to_fetch = master_cards[: args.limit]
        log.info("--limit applied: fetching details for %d/%d shows",
                 len(cards_to_fetch), len(master_cards))

    # Stage 2: detail pages — with the deadline if set
    shows, failures, budget_exceeded = fetch_all_details(
        session, cards_to_fetch, appears_in_map, deals_by_show_id,
        concurrency=args.concurrency, deadline=deadline,
    )

    # Stage 2b: link tkts_deals to performances. The OLT frontend renders
    # the Book Tickets row on /todays-tickets/ client-side, so we can't
    # get booking URLs from that listing. But the same TKTS perfs are
    # also in each show's data-cal — we join on date and attach them.
    scrape_now = datetime.now(timezone.utc)
    _enrich_tkts_deals(shows, scrape_now)

    # Stage 3: sanity checks + previous-run compare + data validation
    warnings = run_sanity_checks(shows, failures, master_count=len(cards_to_fetch))

    if budget_exceeded:
        warnings.append(
            f"budget: wall-clock budget ({args.max_runtime_seconds}s) "
            f"exceeded — only {len(shows)}/{len(cards_to_fetch)} shows complete"
        )

    # Surface "ghost" deals — shows tagged today/tomorrow whose data-cal
    # has no performance on the resolved date.
    ghost_today = sum(
        1 for s in shows for d_ in s.tkts_deals
        if d_.get("tag") == "today" and not d_.get("matched_performances")
    )
    ghost_tomorrow = sum(
        1 for s in shows for d_ in s.tkts_deals
        if d_.get("tag") == "tomorrow" and not d_.get("matched_performances")
    )
    if ghost_today or ghost_tomorrow:
        warnings.append(
            f"info: ghost deals — {ghost_today} today and {ghost_tomorrow} "
            "tomorrow tagged but with no matching performance in their data-cal "
            "(usually transient — calendar refresh lag behind TKTS releases)"
        )

    # Data-range validation (separate list — these are advisory, not regressions)
    validation_warnings = validate_data_ranges(shows)

    # Previous-run compare (skipped when --limit is set, since shrinkage is expected)
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
        master_source=master_source,
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
