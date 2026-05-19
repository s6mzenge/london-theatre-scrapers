"""
londontheatredirect_scraper.py
==============================

Scraper for https://www.londontheatredirect.com — pulls the full show
catalogue from `/all-events`, tags shows that also appear on `/discounts`,
then visits each show's detail page to fetch the full performance calendar
plus editorial fields (description, running time, age guidance, gallery,
tags, badges, reviews, related shows).

Why no Playwright?
------------------
The site is React-hydrated, but the React props blob is embedded inline in
the HTML — `ReactDOM.hydrate(React.createElement(LTD.EventList, {...}))`
for listings and `LTD.EventDetail` for show pages. The /all-events page
ships all 164 shows in a single response, the /discounts page does the same
for its filtered subset, and each detail page embeds a 264-performance
calendar. Plain `requests` is faster and more reliable than a browser here.

The two URLs the user listed are not two datasets — they're filtered views
of the same one. /all-events is the master; /discounts is a filter slice
(`offersOnly: true`). We use /discounts as a tag for each show ("appears in
discounts") but the real data depth comes from the show detail page.

Setup
-----
    pip install requests beautifulsoup4

Usage
-----
    python londontheatredirect_scraper.py                       # full scrape
    python londontheatredirect_scraper.py --limit 5             # test with 5 shows
    python londontheatredirect_scraper.py --out data/ltd.json   # custom output path
    python londontheatredirect_scraper.py --concurrency 24      # more parallel workers
    python londontheatredirect_scraper.py --no-tag-lists        # skip /discounts
    python londontheatredirect_scraper.py --no-detail           # listing only, no detail fetch

Output is a single JSON file:

    {
      "scraped_at": "2026-05-19T08:30:00+00:00",
      "source": "https://www.londontheatredirect.com/all-events",
      "show_count": 164,
      "performance_count": 41234,
      "report": { ... warnings, failures ... },
      "shows": [ { ... } ... ]
    }
"""

from __future__ import annotations

import argparse
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
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = "https://www.londontheatredirect.com"
MASTER_URL = f"{BASE}/all-events"

# Filter-slice URLs we record as tags. /all-events already returns the full
# catalogue; /discounts is /all-events filtered by `offersOnly: true`. We
# scrape /discounts only to learn which shows currently have an offer (the
# per-card payload is identical, so we don't re-extract fields from it).
TAG_LISTS: dict[str, str] = {
    "discounts": f"{BASE}/discounts",
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

RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

# Map LTD's numeric eventType to a human-readable string. Derived from
# tile.subtitle pairings observed in the listing payload.
EVENT_TYPE_MAP: dict[int, str] = {
    1: "Musical",
    2: "Play",
    3: "Attraction",
    4: "Dance",
    5: "Opera",
    13: "Concert",
    15: "Experience",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ltd")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ListingCard:
    """A single show as it appears in the LTD.EventList React props blob."""

    event_id: int
    name: str
    subtitle: str | None         # display label e.g. "Musical", "Play"
    event_type: int | None       # numeric LTD category code
    event_type_label: str | None # human-readable from EVENT_TYPE_MAP
    slug: str                    # canonical URL slug, e.g. "wicked-tickets"
    url: str                     # absolute canonical detail URL
    tagline: str | None
    image: str | None            # tileImage src
    image_alt: str | None
    social_image: str | None
    venue_name: str | None
    venue_url: str | None
    venue_address: dict | None   # {street, postCode, city, geoLocation}
    star_rating: dict | None     # {score, reviews_count} or None
    price_from: float | None
    currency: str | None
    has_offer: bool
    minimal_offer_selling_price: float | None
    minimal_offer_price_before: float | None
    promo_text: str | None
    campaign_label: str | None
    special_offer_date_range: str | None
    advance_pick: dict | None    # {date, label} or None
    child_policy: str | None
    event_start_date: str | None
    event_end_date: str | None
    book_tickets_link: dict | None   # {title, url}


@dataclass
class Performance:
    """A single performance from bookingCalendarData.performances."""

    performance_id: int | None
    iso: str | None              # full ISO datetime
    date: str | None             # "YYYY-MM-DD"
    time: str | None             # "HH:MM"
    price_from: float | None
    label: str | None            # extra label override (e.g. "Press Night")
    special_offer: bool
    offer_description: str | None
    offer_display_text: str | None
    tickets_availability: int | None   # 1 = available, 0 = sold out
    is_master: bool
    tickets_count: int | None
    tickets_count_before: int | None
    book_url: str | None         # built from bookingUrlPattern


@dataclass
class Show:
    """Aggregate of listing tile + show detail page."""

    # --- Listing tile fields ---
    event_id: int
    name: str
    url: str
    slug: str
    subtitle: str | None
    event_type: int | None
    event_type_label: str | None
    tagline: str | None
    image: str | None
    image_alt: str | None
    social_image: str | None
    venue: dict | None           # {name, url, address}
    star_rating: dict | None
    price_from: float | None
    currency: str | None
    has_offer: bool
    minimal_offer_selling_price: float | None
    minimal_offer_price_before: float | None
    promo_text: str | None
    campaign_label: str | None
    special_offer_date_range: str | None
    advance_pick: dict | None
    child_policy: str | None
    event_start_date: str | None
    event_end_date: str | None
    book_tickets_link: dict | None
    appears_in: list[str]        # ["all_events", "discounts", ...]

    # --- Detail-page fields (None if not fetched / not available) ---
    description: str | None = None
    running_time: str | None = None
    running_time_includes_interval: bool | None = None
    age_restriction: str | None = None
    performance_dates: str | None = None
    content_info: str | None = None
    special_notice: str | None = None
    access_info: str | None = None
    tags: list[dict] = field(default_factory=list)
    badges: list[dict] = field(default_factory=list)
    gallery: list[dict] = field(default_factory=list)
    actors: list[dict] = field(default_factory=list)
    critics_reviews: list[dict] = field(default_factory=list)
    customer_reviews: list[dict] = field(default_factory=list)
    related_events: list[dict] = field(default_factory=list)
    special_offer_info: dict | None = None
    safety_notice: str | None = None
    visitors_policy: str | None = None
    group_info: dict | None = None
    book_tickets_enabled: bool | None = None
    book_tickets_absolute_link: str | None = None
    booking_url_pattern: str | None = None
    event_offer_label: str | None = None
    performances: list[dict] = field(default_factory=list)
    next_available_performances: list[dict] = field(default_factory=list)


@dataclass
class ShowFailure:
    event_id: int | None
    url: str
    error: str


@dataclass
class ScrapeReport:
    """Embedded in the output JSON for downstream pipelines / CI to read."""

    master_show_count: int = 0
    succeeded_show_count: int = 0
    failed_show_count: int = 0
    no_detail: bool = False
    tag_lists_scraped: list[str] = field(default_factory=list)
    tag_list_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


def build_session() -> requests.Session:
    """Session with sensible retries on 5xx / 429."""
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(500, 502, 503, 504, 429),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch_url(session: requests.Session, url: str) -> str:
    """Fetch a URL; raise on non-2xx."""
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# React props extraction
# ---------------------------------------------------------------------------

# Match the hydration call header. We capture nothing here — we use the
# match's `end()` as the cursor for raw_decode below.
_HYDRATE_HEADER = re.compile(
    r"ReactDOM\.hydrate\(\s*React\.createElement\(\s*LTD\.(\w+)\s*,\s*",
)


def _extract_react_props(text: str, component: str) -> dict | None:
    """Find `LTD.{component}` hydration and return its props dict.

    The props object can span hundreds of KB on a single line and contain
    nested braces, so we anchor on the header regex then use
    `json.JSONDecoder.raw_decode` to consume exactly one JSON value.
    """
    for m in _HYDRATE_HEADER.finditer(text):
        if m.group(1) != component:
            continue
        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(text, m.end())
        except json.JSONDecodeError as e:
            log.warning("raw_decode failed for LTD.%s: %s", component, e)
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ---------------------------------------------------------------------------
# Listing parsing
# ---------------------------------------------------------------------------


def _absolute(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(BASE + "/", url.lstrip("/"))


def _slug_from_detail_link(detail_link: dict | None) -> str | None:
    if not detail_link:
        return None
    url = detail_link.get("url") or ""
    # canonical pattern: /{category}/{slug}-tickets
    m = re.match(r"^/[^/]+/(.+)$", url)
    if m:
        return m.group(1)
    return None


def _parse_tile(tile: dict) -> ListingCard | None:
    """Map one raw React tile dict to our ListingCard dataclass."""
    detail_link = tile.get("detailLink") or {}
    book_link = tile.get("bookTicketsLink") or {}
    promo = tile.get("promoInfo") or {}
    addl = tile.get("additionalInfo") or {}
    tile_img = tile.get("tileImage") or {}
    star = tile.get("starRating") or None

    url = _absolute(detail_link.get("url"))
    if not url:
        return None

    slug = _slug_from_detail_link(detail_link) or ""
    event_type = addl.get("eventType")

    # Star rating: keep both keys for downstream consumers
    star_rating = None
    if isinstance(star, dict):
        score = star.get("score")
        reviews = star.get("reviewsCount")
        if score is not None or reviews:
            star_rating = {"score": score, "reviews_count": reviews}

    # Advance pick / special offer date range — pass through but normalise
    # to plain dicts so they survive asdict serialisation
    advance_pick = promo.get("advancePick")
    if advance_pick and isinstance(advance_pick, dict):
        advance_pick = {
            "date": advance_pick.get("date"),
            "label": advance_pick.get("label"),
        }
    sodr = promo.get("specialOfferDateRange")
    if sodr and not isinstance(sodr, (str, type(None))):
        # in case it's a dict — stringify defensively
        sodr = str(sodr)

    venue_addr = addl.get("venueAddress") or None
    if isinstance(venue_addr, dict):
        # Pass through but lowercase the keys for consistency
        venue_addr = {
            "street": venue_addr.get("street"),
            "post_code": venue_addr.get("postCode"),
            "city": venue_addr.get("city"),
            "geo_location": venue_addr.get("geoLocation"),
        }

    venue_url = (
        f"{BASE}/venue/{addl['venueUrlId']}"
        if addl.get("venueUrlId") else None
    )

    return ListingCard(
        event_id=int(tile["eventId"]) if tile.get("eventId") is not None else 0,
        name=tile.get("title") or detail_link.get("title") or "",
        subtitle=tile.get("subtitle"),
        event_type=event_type,
        event_type_label=EVENT_TYPE_MAP.get(event_type) if event_type is not None else None,
        slug=slug,
        url=url,
        tagline=tile.get("tagline"),
        image=tile_img.get("src"),
        image_alt=tile_img.get("altText"),
        social_image=tile.get("socialMediaImageAbsoluteUrl"),
        venue_name=addl.get("venueName"),
        venue_url=venue_url,
        venue_address=venue_addr,
        star_rating=star_rating,
        price_from=promo.get("priceFrom"),
        currency=promo.get("currency"),
        has_offer=bool(promo.get("hasOffer")),
        minimal_offer_selling_price=promo.get("minimalOfferSellingPrice"),
        minimal_offer_price_before=promo.get("minimalOfferPriceBefore"),
        promo_text=promo.get("promoText") or None,
        campaign_label=promo.get("campaignLabel"),
        special_offer_date_range=sodr,
        advance_pick=advance_pick,
        child_policy=addl.get("childPolicy"),
        event_start_date=addl.get("eventStartDate"),
        event_end_date=addl.get("eventEndDate"),
        book_tickets_link={
            "title": book_link.get("title"),
            "url": _absolute(book_link.get("url")),
        } if book_link.get("url") else None,
    )


def parse_listing(html_text: str) -> list[ListingCard]:
    """Parse an /all-events or /discounts page and return a list of cards.

    Both pages use the same LTD.EventList component, so the same extractor
    works for both — the only difference is the `filter` field (which we
    don't care about; we just want the tiles).
    """
    props = _extract_react_props(html_text, "EventList")
    if not props:
        log.error("could not find LTD.EventList props on page")
        return []
    tiles = props.get("tiles") or []
    cards: list[ListingCard] = []
    for tile in tiles:
        try:
            card = _parse_tile(tile)
            if card is not None:
                cards.append(card)
        except Exception as e:
            log.warning("failed to parse tile (eventId=%s): %s",
                        tile.get("eventId"), e)
    return cards


# ---------------------------------------------------------------------------
# Detail-page parsing
# ---------------------------------------------------------------------------


def _parse_iso(s: str | None) -> tuple[str | None, str | None, str | None]:
    """Split an ISO datetime like '2026-05-19T19:30:00' into iso/date/time.

    LTD's `performanceDate` is naive (no timezone), and they're UK times
    written as local time. We don't add a timezone; we mirror exactly what
    the API returns (`iso` keeps the full string, `date` and `time` are
    the obvious slices).
    """
    if not s:
        return None, None, None
    iso = s
    date_part: str | None = None
    time_part: str | None = None
    if "T" in s:
        date_part, time_part = s.split("T", 1)
        # truncate seconds: "19:30:00" -> "19:30"
        if len(time_part) >= 5:
            time_part = time_part[:5]
    return iso, date_part, time_part


def _build_book_url(pattern: str | None, perf_id: int | None,
                    perf_date_iso: str | None) -> str | None:
    """LTD's bookingUrlPattern looks like:
        /booking/wicked-tickets/{date}/{performanceId}
    where {date} is MM-YYYY (e.g. '05-2026') and {performanceId} is the int.
    """
    if not pattern or perf_id is None or not perf_date_iso:
        return None
    date_part = perf_date_iso.split("T", 1)[0]
    try:
        y, m, _ = date_part.split("-")
    except ValueError:
        return None
    date_token = f"{m}-{y}"
    url = (pattern
           .replace("{date}", date_token)
           .replace("{performanceId}", str(perf_id)))
    return _absolute(url)


def _parse_performance(raw: dict, booking_url_pattern: str | None) -> Performance:
    iso, date_part, time_part = _parse_iso(raw.get("performanceDate"))
    perf_id = raw.get("performanceId")
    return Performance(
        performance_id=int(perf_id) if perf_id is not None else None,
        iso=iso,
        date=date_part,
        time=time_part,
        price_from=raw.get("priceFrom"),
        label=raw.get("label"),
        special_offer=bool(raw.get("specialOffer")),
        offer_description=raw.get("offerDescription"),
        offer_display_text=raw.get("offerDisplayText"),
        tickets_availability=raw.get("ticketsAvailability"),
        is_master=bool(raw.get("isMaster")),
        tickets_count=raw.get("ticketsCount"),
        tickets_count_before=raw.get("ticketsCountBefore"),
        book_url=_build_book_url(booking_url_pattern, perf_id,
                                 raw.get("performanceDate")),
    )


def _normalize_gallery_item(raw: dict) -> dict:
    return {
        "src": raw.get("src"),
        "alt_text": raw.get("altText"),
        "description": raw.get("description"),
        "dimensions": raw.get("dimensions"),
    }


def _normalize_tag(raw: dict) -> dict:
    return {
        "title": raw.get("title"),
        "url": _absolute(raw.get("url")),
    }


def _normalize_badge(raw: dict) -> dict:
    icon = raw.get("icon") or {}
    return {
        "title": raw.get("title"),
        "description": raw.get("description"),
        "icon_src": icon.get("src") if isinstance(icon, dict) else None,
        "contains_visitor_policy": bool(raw.get("containsVisitorPolicy")),
    }


def _normalize_related_event(tile: dict) -> dict:
    """Slim down a related-events tile to the bits worth keeping."""
    detail_link = tile.get("detailLink") or {}
    return {
        "event_id": tile.get("eventId"),
        "name": tile.get("title") or detail_link.get("title"),
        "url": _absolute(detail_link.get("url")),
        "subtitle": tile.get("subtitle"),
        "venue_name": (tile.get("additionalInfo") or {}).get("venueName"),
        "image": ((tile.get("tileImage") or {}).get("src")),
    }


def _normalize_review(raw: dict) -> dict:
    return {
        "customer_name": raw.get("customerName"),
        "content": raw.get("content"),
        "date_created": raw.get("dateCreated"),
        "score": raw.get("score"),
    }


def _normalize_critic_review(raw: dict) -> dict:
    return {
        "publication": raw.get("publication") or raw.get("source"),
        "quote": raw.get("quote") or raw.get("content"),
        "score": raw.get("score") or raw.get("stars"),
        "author": raw.get("author"),
        "url": _absolute(raw.get("url")) if raw.get("url") else None,
    }


def parse_detail(html_text: str) -> dict | None:
    """Parse a show detail page and return a dict of extracted fields.

    Returns None if the LTD.EventDetail props can't be found (which would
    be a hard scrape failure for that show).
    """
    props = _extract_react_props(html_text, "EventDetail")
    if not props:
        return None

    addl = props.get("additionalInfo") or {}
    bcd = props.get("bookingCalendarData") or {}
    booking_url_pattern = bcd.get("bookingUrlPattern")

    performances = [
        asdict(_parse_performance(p, booking_url_pattern))
        for p in (bcd.get("performances") or [])
    ]

    # nextAvailablePerformances has a different shape (per-event slot info
    # rather than per-performance), so we keep it as a slim list
    nap_raw = props.get("nextAvailablePerformances") or []
    nap: list[dict] = []
    for n in nap_raw:
        if not isinstance(n, dict):
            continue
        dl = n.get("detailLink") or {}
        nap.append({
            "performance_date": n.get("performanceDate"),
            "url": _absolute(dl.get("url")),
            "price_from": n.get("eventMinimumPrice"),
            "minimal_offer_selling_price": n.get("minimalOfferSellingPrice"),
            "minimal_offer_price_before": n.get("minimalOfferPriceBefore"),
        })

    gallery = [_normalize_gallery_item(g)
               for g in (props.get("gallery") or [])
               if isinstance(g, dict)]
    tags = [_normalize_tag(t)
            for t in (props.get("tags") or [])
            if isinstance(t, dict)]
    badges = [_normalize_badge(b)
              for b in (props.get("badges") or [])
              if isinstance(b, dict)]

    # actors / cast — usually empty in samples, but pass through verbatim
    actors_raw = props.get("actors") or []
    actors: list[dict] = []
    for a in actors_raw:
        if not isinstance(a, dict):
            continue
        actors.append({
            "name": a.get("name"),
            "role": a.get("role") or a.get("character"),
            "image": (a.get("image") or {}).get("src") if isinstance(a.get("image"), dict) else a.get("image"),
        })

    critics_reviews = [_normalize_critic_review(r)
                       for r in (props.get("criticsReviews") or [])
                       if isinstance(r, dict)]
    customer_reviews = [_normalize_review(r)
                        for r in (props.get("customerReviews") or [])
                        if isinstance(r, dict)]

    # relatedEvents may be {tiles: [...]} or a list — handle both
    related_raw = props.get("relatedEvents") or {}
    if isinstance(related_raw, dict):
        related_tiles = related_raw.get("tiles") or []
    elif isinstance(related_raw, list):
        related_tiles = related_raw
    else:
        related_tiles = []
    related_events = [_normalize_related_event(t)
                      for t in related_tiles
                      if isinstance(t, dict)]

    return {
        "description": props.get("eventDescription"),
        "running_time": addl.get("runningTime"),
        "running_time_includes_interval": addl.get("runningTimeIncludesInterval"),
        "age_restriction": addl.get("ageRestriction"),
        "performance_dates": addl.get("performanceDates"),
        "content_info": addl.get("contentInfo"),
        "special_notice": addl.get("specialNotice"),
        "access_info": addl.get("accessInfo"),
        "tags": tags,
        "badges": badges,
        "gallery": gallery,
        "actors": actors,
        "critics_reviews": critics_reviews,
        "customer_reviews": customer_reviews,
        "related_events": related_events,
        "special_offer_info": props.get("specialOfferInfo"),
        "safety_notice": props.get("safetyNotice"),
        "visitors_policy": props.get("visitorsPolicy"),
        "group_info": props.get("groupInfo"),
        "book_tickets_enabled": props.get("bookTicketsEnabled"),
        "book_tickets_absolute_link": props.get("bookTicketsAbsoluteLink"),
        "booking_url_pattern": booking_url_pattern,
        "event_offer_label": bcd.get("eventOfferLabel"),
        "performances": performances,
        "next_available_performances": nap,
    }


# ---------------------------------------------------------------------------
# Scraping pipeline
# ---------------------------------------------------------------------------


def card_to_show(card: ListingCard) -> Show:
    """Convert a ListingCard into a Show with detail fields blank."""
    venue: dict | None = None
    if card.venue_name or card.venue_url or card.venue_address:
        venue = {
            "name": card.venue_name,
            "url": card.venue_url,
            "address": card.venue_address,
        }
    return Show(
        event_id=card.event_id,
        name=card.name,
        url=card.url,
        slug=card.slug,
        subtitle=card.subtitle,
        event_type=card.event_type,
        event_type_label=card.event_type_label,
        tagline=card.tagline,
        image=card.image,
        image_alt=card.image_alt,
        social_image=card.social_image,
        venue=venue,
        star_rating=card.star_rating,
        price_from=card.price_from,
        currency=card.currency,
        has_offer=card.has_offer,
        minimal_offer_selling_price=card.minimal_offer_selling_price,
        minimal_offer_price_before=card.minimal_offer_price_before,
        promo_text=card.promo_text,
        campaign_label=card.campaign_label,
        special_offer_date_range=card.special_offer_date_range,
        advance_pick=card.advance_pick,
        child_policy=card.child_policy,
        event_start_date=card.event_start_date,
        event_end_date=card.event_end_date,
        book_tickets_link=card.book_tickets_link,
        appears_in=["all_events"],
    )


def scrape_master(session: requests.Session) -> list[ListingCard]:
    log.info("fetching master listing: %s", MASTER_URL)
    html_text = fetch_url(session, MASTER_URL)
    cards = parse_listing(html_text)
    log.info("master listing: %d shows", len(cards))
    return cards


def scrape_tag_lists(session: requests.Session) -> dict[str, set[int]]:
    """For each tag URL, return the set of event_ids appearing in it."""
    out: dict[str, set[int]] = {}
    for tag, url in TAG_LISTS.items():
        try:
            html_text = fetch_url(session, url)
        except Exception as e:
            log.warning("tag list %s (%s) failed: %s", tag, url, e)
            out[tag] = set()
            continue
        cards = parse_listing(html_text)
        ids = {c.event_id for c in cards if c.event_id}
        log.info("tag list %s: %d shows", tag, len(ids))
        out[tag] = ids
    return out


def fetch_show_detail(
    session: requests.Session,
    show: Show,
) -> tuple[Show, ShowFailure | None]:
    try:
        html_text = fetch_url(session, show.url)
    except Exception as e:
        return show, ShowFailure(event_id=show.event_id, url=show.url, error=str(e))
    parsed = parse_detail(html_text)
    if parsed is None:
        return show, ShowFailure(
            event_id=show.event_id, url=show.url,
            error="LTD.EventDetail props not found in HTML",
        )
    for k, v in parsed.items():
        setattr(show, k, v)
    return show, None


def scrape_all(
    session: requests.Session,
    *,
    limit: int | None,
    concurrency: int,
    skip_tag_lists: bool,
    skip_detail: bool,
) -> tuple[list[Show], list[ShowFailure], dict[str, set[int]], int]:
    """Run the full scrape.

    Returns (shows, failures, tag_membership, master_count) where master_count
    is the number of shows the /all-events listing advertised (independent
    of --limit or detail-fetch outcomes).
    """
    cards = scrape_master(session)
    master_count = len(cards)

    tag_membership: dict[str, set[int]] = {}
    if not skip_tag_lists:
        tag_membership = scrape_tag_lists(session)

    shows = [card_to_show(c) for c in cards]
    # Apply tag membership before any limit so the report counts are accurate
    for s in shows:
        for tag, ids in tag_membership.items():
            if s.event_id in ids:
                s.appears_in.append(tag)

    if limit is not None:
        shows = shows[:limit]
        log.info("limited to %d shows", len(shows))

    failures: list[ShowFailure] = []
    if skip_detail:
        log.info("--no-detail set; skipping per-show detail fetch")
        return shows, failures, tag_membership, master_count

    log.info("fetching %d show detail pages with concurrency=%d",
             len(shows), concurrency)
    completed = 0
    lock = Lock()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fetch_show_detail, session, s): s for s in shows}
        for fut in as_completed(futures):
            show, err = fut.result()
            if err:
                failures.append(err)
            with lock:
                completed += 1
                if completed % 20 == 0 or completed == len(shows):
                    log.info("  %d / %d done", completed, len(shows))

    log.info("detail fetch: %d ok, %d failed",
             len(shows) - len(failures), len(failures))
    return shows, failures, tag_membership, master_count


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------


def run_sanity_checks(
    shows: list[Show],
    failures: list[ShowFailure],
    skipped_detail: bool,
) -> list[str]:
    """Field-quality and detail-fetch success-rate checks.

    These run on the `shows` we actually built — so when --limit is in
    effect the rates are computed over the limited subset, which is what
    you want for a "did this run go well" check.
    """
    warnings: list[str] = []

    if not shows:
        warnings.append("CRITICAL: no shows produced (master listing empty?)")
        return warnings

    succeeded = len(shows) - len(failures)
    if not skipped_detail:
        success_rate = succeeded / len(shows)
        if success_rate < 0.95:
            warnings.append(
                f"detail success rate {success_rate:.1%} below 95% "
                f"({succeeded}/{len(shows)})"
            )

    # Field-quality thresholds: how many shows are missing key fields?
    no_venue = sum(1 for s in shows if not s.venue or not s.venue.get("name"))
    if no_venue / len(shows) > 0.05:
        warnings.append(
            f"{no_venue}/{len(shows)} shows ({no_venue/len(shows):.1%}) "
            f"have no venue name (threshold 5%)"
        )
    no_price = sum(1 for s in shows if s.price_from is None)
    if no_price / len(shows) > 0.10:
        warnings.append(
            f"{no_price}/{len(shows)} shows ({no_price/len(shows):.1%}) "
            f"have no priceFrom (threshold 10%)"
        )

    if not skipped_detail:
        no_perfs = sum(1 for s in shows if not s.performances)
        # Some shows might genuinely have no upcoming performances
        # (closing or pre-sale); only flag if it's > 15%.
        if no_perfs / len(shows) > 0.15:
            warnings.append(
                f"{no_perfs}/{len(shows)} shows ({no_perfs/len(shows):.1%}) "
                f"have zero performances (threshold 15%)"
            )

    return warnings


def compare_with_previous(
    new_shows: list[Show],
    new_perf_count: int,
    out_path: Path,
) -> list[str]:
    """If a previous output exists at out_path, flag catastrophic regressions.

    Warn-but-write: we still write the new output, but the warnings get
    embedded in the report so a downstream CI step can decide whether to
    fail the build.
    """
    if not out_path.exists():
        return []
    try:
        prev = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"could not read previous output {out_path}: {e}"]

    warnings: list[str] = []
    prev_shows = prev.get("shows") or []
    prev_count = len(prev_shows)
    if prev_count and len(new_shows) / prev_count < 0.8:
        warnings.append(
            f"show count dropped: {prev_count} -> {len(new_shows)} "
            f"({len(new_shows)/prev_count:.0%})"
        )

    prev_ids = {s.get("event_id") for s in prev_shows}
    new_ids = {s.event_id for s in new_shows}
    if prev_ids:
        churn = len(prev_ids ^ new_ids) / len(prev_ids | new_ids)
        if churn > 0.5:
            warnings.append(
                f"event_id churn {churn:.0%} between runs "
                f"(possible upstream schema break)"
            )

    prev_perfs = prev.get("performance_count") or 0
    if prev_perfs and new_perf_count / prev_perfs < 0.5:
        warnings.append(
            f"performance count dropped: {prev_perfs} -> {new_perf_count} "
            f"({new_perf_count/prev_perfs:.0%})"
        )

    return warnings


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_output(
    shows: list[Show],
    failures: list[ShowFailure],
    tag_membership: dict[str, set[int]],
    skipped_detail: bool,
    out_path: Path,
    master_count: int,
) -> None:
    perf_count = sum(len(s.performances) for s in shows)

    sanity_warnings = run_sanity_checks(shows, failures, skipped_detail)
    regression_warnings = compare_with_previous(shows, perf_count, out_path)
    all_warnings = sanity_warnings + regression_warnings
    if all_warnings:
        log.warning("scrape produced %d warning(s):", len(all_warnings))
        for w in all_warnings:
            log.warning("  - %s", w)

    report = ScrapeReport(
        master_show_count=master_count,
        succeeded_show_count=len(shows) - len(failures),
        failed_show_count=len(failures),
        no_detail=skipped_detail,
        tag_lists_scraped=sorted(tag_membership.keys()),
        tag_list_counts={k: len(v) for k, v in tag_membership.items()},
        warnings=all_warnings,
        failures=[asdict(f) for f in failures],
    )

    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": MASTER_URL,
        "show_count": len(shows),
        "performance_count": perf_count,
        "report": asdict(report),
        "shows": [asdict(s) for s in shows],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(out_path)
    log.info("wrote %s (%d shows, %d performances)",
             out_path, len(shows), perf_count)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape londontheatredirect.com",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("ltd_london.json"),
        help="output JSON path (default: ltd_london.json)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="limit to N shows (for testing)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"detail-fetch worker count (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--no-tag-lists", action="store_true",
        help="skip /discounts tag list",
    )
    parser.add_argument(
        "--no-detail", action="store_true",
        help="skip per-show detail fetch (listing only)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="don't write the output file",
    )
    args = parser.parse_args(argv)

    session = build_session()
    start = time.time()
    try:
        shows, failures, tag_membership, master_count = scrape_all(
            session,
            limit=args.limit,
            concurrency=args.concurrency,
            skip_tag_lists=args.no_tag_lists,
            skip_detail=args.no_detail,
        )
    except requests.HTTPError as e:
        log.error("master fetch failed: %s", e)
        return 2
    except Exception as e:
        log.exception("unhandled error: %s", e)
        return 3

    if args.dry_run:
        log.info("--dry-run set; not writing output")
    else:
        write_output(
            shows, failures, tag_membership,
            args.no_detail, args.out, master_count,
        )

    log.info("done in %.1fs", time.time() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
