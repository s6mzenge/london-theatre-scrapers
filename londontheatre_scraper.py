"""
LondonTheatre.co.uk scraper (hybrid Playwright + requests)
==========================================================

Scrapes every show on https://www.londontheatre.co.uk/tickets/all-shows
plus the two filter slices we care about as "tags":

  * /whats-on/last-minute-tickets  → "Tonight"         → tag: "last_minute"
  * /whats-on/theatre-ticket-offers → "Special offers" → tag: "offers"

Then fetches each show's detail page for showtimes and price bands.

The same-platform-as-TodayTix observation
-----------------------------------------
LondonTheatre.co.uk is the UK-branded sibling of TodayTix. Confirmed by:
  * Identical Next.js `__NEXT_DATA__` JSON shape on every listing
    (`props.pageProps.productList.results.shows` + `pagination`).
  * Same 54-card SSR cap with `infinite-scroll-component` lazy-loading
    the rest as the user scrolls.
  * Cross-references to todaytix.com URLs in the page body (the "Summer
    Theatre Sale" banner links straight to todaytix.com).

Practical consequence: the architecture mirrors `todaytix_scraper.py`
closely — Playwright for the listing discovery, then plain `requests`
in parallel for the show detail pages. The detail page also embeds a
`__NEXT_DATA__` with `props.pageProps.product` and `initialShowtimes`,
so a browser isn't needed there.

A note on the per-show schema
-----------------------------
The card schema is slightly richer than TodayTix's: each card carries
`avgRating`, `reviewSummary` (score, reviewsCount, adjectives, top
reviews), `salesMessage`, `savingsMessage`, `displayName`, `category`
(name + slug + id), `lowPriceForRegularTickets`, plus an
`images.productMedia` structure with a header image and a gallery of
`imagesAndVideos`. Showtime extraction (price bands, booking URLs)
remains the same shape as TodayTix's.

Setup
-----
    pip install requests playwright beautifulsoup4
    playwright install chromium

Usage
-----
    python londontheatre_scraper.py                          # full scrape
    python londontheatre_scraper.py --limit 5                # test
    python londontheatre_scraper.py --out data/lt.json       # custom path
    python londontheatre_scraper.py --headed                 # show browser
    python londontheatre_scraper.py --concurrency 32         # more workers
    python londontheatre_scraper.py --no-tag-lists           # skip filter slices

Output is a single JSON file:

    {
      "scraped_at": "2026-05-19T08:30:00+00:00",
      "source": "https://www.londontheatre.co.uk/tickets/all-shows",
      "show_count": 196,
      "showtime_count": 41234,
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
from urllib.parse import urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.londontheatre.co.uk"
LISTING_URL = f"{BASE_URL}/tickets/all-shows"

# The two filter slices we use as "tags". Their card schema is identical
# to the master listing — we scrape them only to learn which shows are
# in each slice (recorded in Show.appears_in).
TAG_LISTS: dict[str, str] = {
    "last_minute": f"{BASE_URL}/whats-on/last-minute-tickets",
    "offers":      f"{BASE_URL}/whats-on/theatre-ticket-offers",
}

SHOW_URL_TEMPLATE = f"{BASE_URL}/show/{{id}}-{{slug}}"
# LT booking URLs route through TodayTix's booking-seating-plan endpoint,
# matching the pattern used by todaytix_scraper.py. If LT later moves
# bookings to its own subdomain, only this template needs updating.
BOOKING_URL_BASE = "https://www.todaytix.com/booking/seating-plan"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

DEFAULT_CONCURRENCY = 32

RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

# Scroll behaviour. Mirrors todaytix_scraper.py because the lazy-loading
# component (infinite-scroll-component) is the same React library.
SCROLL_NETWORK_IDLE_MS = 600
SCROLL_HARD_TIMEOUT_MS = 4000
MAX_STALE_SCROLLS = 3
MAX_SCROLL_ATTEMPTS = 80

# Resource types to block in the browser. Same rationale as TodayTix:
# kills ~80% of page weight so the lazy loader fires faster.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_HOST_PATTERNS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.com", "facebook.net", "connect.facebook",
    "segment.com", "segment.io", "branch.io", "branch-cdn",
    "snowplowanalytics", "hotjar.com", "fullstory.com",
    "intercom.io", "intercomcdn.com", "zendesk.com",
    "adjust.com", "appsflyer.com", "amplitude.com",
    "optimizely.com", "launchdarkly.com",
    "/static/fonts/",
)

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

# Show detail URL pattern. The site renders some links as id-only stubs
# (e.g. /show/12201) which redirect to the full
# /show/12201-six-the-musical-tickets, so we match either form here and
# let the JSON give us the canonical slug afterwards. The id is required,
# the slug is optional. The character class allows the literal ampersand
# observed in slugs like "/show/18792-romeo-&-juliet".
SHOW_HREF_RE = re.compile(r'^/show/(\d+)(?:-([a-z0-9\-&]+))?/?$')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("londontheatre")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PriceBand:
    """One price band on one showtime. Same shape as todaytix_scraper.py
    because the underlying TodayTix API is shared."""
    price_value: float | None
    currency: str | None
    price_display: str | None
    face_value: float | None
    seats_available: int | None
    max_contiguous_seats: int | None


@dataclass
class Showtime:
    """One bookable performance with its price bands. Same shape as
    todaytix_scraper.py's Showtime."""
    showtime_id: int | None
    datetime: str | None
    local_date: str | None
    local_time: str | None
    day_of_week: str | None
    daypart: str | None
    booking_url: str | None
    low_price_value: float | None
    low_price_display: str | None
    currency: str | None
    has_promotion: bool
    promotion_label: str | None
    seats_available: int | None
    price_bands: list[PriceBand] = field(default_factory=list)


@dataclass
class TopReview:
    """A single editorial / customer top-review attached to a show's
    review summary on the listing card."""
    score: int | None
    five_star_score: float | None
    title: str | None
    body: str | None
    url: str | None
    publication: str | None
    stars: int | None


@dataclass
class ReviewSummary:
    """Aggregate review block. Score is 0–100; reviews_count is the
    count of customer reviews behind it."""
    score: int | None
    five_star_score: float | None
    score_description: str | None
    reviews_count: int | None
    short_reviews_count: str | None
    audience_acclaimed: bool
    adjectives: list[str]
    top_reviews: list[TopReview]


@dataclass
class Show:
    # Identity
    id: int
    show_id: int | None         # the duplicate "showId" field on cards;
                                # normally == id but kept distinct in case
                                # LT ever uses one for product variants.
    name: str
    display_name: str | None
    slug: str
    url: str
    # Categorisation / housing
    venue: str | None
    venue_url_slug: str | None  # e.g. "his-majestys-theatre" (LT venue page slug)
    category: str | None
    category_slug: str | None
    location_id: int | None
    location_seo_name: str | None
    admission_type: str | None  # "TIMED" etc.
    product_type: str | None    # "SHOW" etc.
    # Dates
    start_date: str | None
    end_date: str | None
    # Pricing & promotion (listing card)
    low_price_value: float | None
    low_price_display: str | None
    low_price_display_rounded: str | None
    currency: str | None
    lottery_low_price_value: float | None
    lottery_low_price_display: str | None
    max_discount_percentage: int | None
    has_promotion: bool
    promotion_label: str | None
    promotion_description: str | None
    promotion_voucher_code: str | None
    sales_message: str | None
    savings_message: str | None
    # Ratings
    avg_rating: float | None
    review_summary: dict | None  # ReviewSummary (asdict'd) — None if no reviews
    # Editorial / descriptive
    description: str | None
    # Media
    header_image_url: str | None
    poster_image_url: str | None
    gallery: list[dict]   # [{"url","title","description","content_type"}, ...]
    # Filter-slice membership
    appears_in: list[str]
    # Showtimes (from detail page; empty if detail fetch failed)
    showtimes: list[Showtime] = field(default_factory=list)


@dataclass
class ShowFailure:
    """A show that we expected to fetch (from listing) but couldn't.
    Recorded so the consumer of the JSON can see which shows are missing
    and why. Same shape as the other scrapers."""
    id: int
    slug: str
    url: str
    error: str


@dataclass
class ScrapeReport:
    """Summary of a single scrape run. Embedded into the output JSON
    under "report" so downstream consumers can detect partial / degraded
    runs without parsing log files."""
    expected_show_count: int | None
    discovered_show_count: int
    succeeded_show_count: int
    failed_show_count: int
    tag_lists_scraped: list[str]
    tag_list_counts: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    failures: list[ShowFailure] = field(default_factory=list)
    # Structured diagnostic of showtime price/booking status. Lets
    # consumers distinguish "fully bookable" from "announced but not yet
    # on sale" (a category that's normal at ~4% on LT and not a failure).
    # See build_showtime_diagnostics().
    showtime_diagnostics: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Step 1 — listing discovery via Playwright
# ---------------------------------------------------------------------------

def _read_pagination_total(page) -> int | None:
    """Read pagination.total from the page's SSR'd __NEXT_DATA__. If
    successful, we know exactly how many shows to scroll to and can stop
    the moment we hit that target. Returns None if the read fails (the
    stale-streak heuristic kicks in as a fallback)."""
    try:
        next_data_json = page.eval_on_selector(
            '#__NEXT_DATA__', 'el => el.textContent',
        )
        next_data = json.loads(next_data_json)
        pag = (next_data.get('props', {}).get('pageProps', {})
               .get('productList', {}).get('results', {})
               .get('pagination', {}))
        if isinstance(pag.get('total'), int):
            return pag['total']
    except Exception:
        pass
    return None


def _read_initial_shows_from_next_data(page) -> list[dict]:
    """Pull the initial 54 shows out of the page's __NEXT_DATA__ blob.
    We use this for the master listing to get rich card data without
    having to re-fetch it from each detail page: the cards in
    __NEXT_DATA__ include rating, review summary, salesMessage, etc.

    Returns an empty list on any failure — caller falls back to DOM-only
    extraction which still gives us (id, slug) pairs."""
    try:
        next_data_json = page.eval_on_selector(
            '#__NEXT_DATA__', 'el => el.textContent',
        )
        next_data = json.loads(next_data_json)
        shows = (next_data.get('props', {}).get('pageProps', {})
                 .get('productList', {}).get('results', {})
                 .get('shows', []))
        return shows if isinstance(shows, list) else []
    except Exception as e:
        log.info("  couldn't extract initial shows from __NEXT_DATA__: %s", e)
        return []


def _looks_like_rich_show_card(item: Any) -> bool:
    """Sniff a single JSON object to see if it's a rich show card from
    a lazy-load batch. We avoid hardcoding a specific endpoint URL since
    the Next.js data route ("/_next/data/<buildId>/...") includes a build
    ID that changes on every deploy. Instead we recognise rich cards by
    their shape: a dict with a numeric show id AND at least one field
    that's only in the listing card schema (not the sparser detail-page
    product schema), like reviewSummary or salesMessage or avgRating."""
    if not isinstance(item, dict):
        return False
    has_id = isinstance(item.get("id"), int) or isinstance(item.get("showId"), int)
    if not has_id:
        return False
    # Distinctive listing-card fields. Any one of these means we've found
    # a card from the same source that populated initialShows in __NEXT_DATA__.
    return any(k in item for k in (
        "reviewSummary", "salesMessage", "avgRating",
        "lowPriceForRegularTickets", "savingsMessage",
    ))


def _find_show_cards_in_payload(obj: Any, out: list[dict],
                                _depth: int = 0, _max_depth: int = 6) -> None:
    """Walk a parsed JSON payload looking for arrays of rich show cards.

    Used to harvest lazy-loaded batches from the listing page. The
    expected shape is `{...: {shows: [{...rich...}, ...]}}` but we don't
    rely on the exact key path — we just walk the structure with a depth
    cap and collect anything that looks like a rich card. Bounded so a
    pathological response can't make us infinite-loop."""
    if _depth > _max_depth:
        return
    if isinstance(obj, list):
        # Whole-list shortcut: if the list itself looks like a list of
        # rich cards, harvest it and stop (don't dive into each card,
        # nothing useful below that level).
        if obj and _looks_like_rich_show_card(obj[0]):
            for item in obj:
                if _looks_like_rich_show_card(item):
                    out.append(item)
            return
        for item in obj:
            _find_show_cards_in_payload(item, out, _depth + 1, _max_depth)
    elif isinstance(obj, dict):
        for v in obj.values():
            _find_show_cards_in_payload(v, out, _depth + 1, _max_depth)


def _install_listing_response_capture(page, sink: dict[int, dict]) -> None:
    """Install a page.on("response", ...) handler that intercepts JSON
    responses and harvests any rich show cards inside them. Writes to
    `sink` (id → card dict). Idempotent: cards already in `sink` win
    over a later, sparser duplicate.

    Why this exists: the master listing serves the first 54 shows
    server-side, then lazy-loads the remaining 142 as the user scrolls.
    The lazy-load responses contain the SAME rich card shape as the SSR
    initialShows — venue, salesMessage, reviewSummary, lowPriceForRegularTickets,
    images, etc. Without this capture, the per-show detail pages alone
    yield a sparser `product` object (no description, dates, prices, or
    reviews for ~140 shows), because the detail-page schema is the
    TodayTix sibling-platform's product schema rather than LT's
    listing-card schema."""

    def _on_response(response) -> None:
        try:
            # Cheap pre-filters: avoid parsing non-JSON or large media bodies.
            ct = (response.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            # Only look at first-party requests — third-party JSON (e.g.
            # analytics beacons) can't contain show cards.
            url = response.url
            if "londontheatre.co.uk" not in url and "todaytix.com" not in url:
                return
            body = response.json()
        except Exception:
            # response.json() raises on non-JSON or network errors; ignore.
            return
        found: list[dict] = []
        _find_show_cards_in_payload(body, found)
        if not found:
            return
        added = 0
        for card in found:
            cid = card.get("id") if isinstance(card.get("id"), int) else card.get("showId")
            if not isinstance(cid, int) or cid in sink:
                continue
            sink[cid] = card
            added += 1
        if added:
            log.info("  captured %d lazy-loaded cards from XHR (total: %d)",
                     added, len(sink))

    page.on("response", _on_response)


def _dismiss_consent_banners(page) -> None:
    """Best-effort consent / cookie banner dismissal — same approach as
    todaytix_scraper.py since the consent banner is shared infrastructure
    (TodayTix uses the same OneTrust-style modal)."""
    for txt in ("Accept all", "Accept All", "I agree", "Got it", "Accept"):
        try:
            btn = page.get_by_role("button", name=re.compile(txt, re.I))
            if btn.count():
                btn.first.click(timeout=2000)
                log.info("  dismissed banner: %r", txt)
                return
        except Exception:
            pass


def _scroll_and_collect_show_pairs(
    page,
    *,
    scroll_hard_timeout_ms: int,
    max_stale: int,
    max_attempts: int,
    label: str,
) -> tuple[list[tuple[int, str]], int | None]:
    """Generic scroll-to-load-all routine reused for master listing and
    tag-slice listings. Returns (ordered_pairs, expected_total).

    Speed optimisations mirror TodayTix:
      * Wait for network-idle (the lazy fetch settling), not a fixed sleep.
      * If pagination.total is readable, stop precisely at it.
      * Fast-path early exit: if count is stable for one scroll AND we've
        got ≥95% of the expected total, accept and move on.
    """
    expected_total = _read_pagination_total(page)
    if expected_total is not None:
        log.info("  [%s] page reports total=%d", label, expected_total)

    ordered_pairs: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    stale_streak = 0
    last_count = 0

    for attempt in range(1, max_attempts + 1):
        # Pull every show link currently in the DOM.
        hrefs = page.eval_on_selector_all(
            'a[href^="/show/"]',
            "els => els.map(e => e.getAttribute('href'))",
        )
        for href in hrefs:
            if not href:
                continue
            path = urlparse(href).path
            m = SHOW_HREF_RE.match(path)
            if not m:
                continue
            show_id = int(m.group(1))
            slug = m.group(2) or ""  # id-only stubs have no slug yet
            if show_id in seen_ids:
                continue
            seen_ids.add(show_id)
            ordered_pairs.append((show_id, slug))

        count = len(ordered_pairs)

        # Primary exit: reached the expected total.
        if expected_total is not None and count >= expected_total:
            log.info("  [%s] scroll %d: %d shows — reached expected total",
                     label, attempt, count)
            break

        # Safety-net exit: count stopped growing.
        if count == last_count:
            stale_streak += 1
        else:
            stale_streak = 0
        last_count = count

        # Fast-path exit: ≥95% of expected and stable for one scroll.
        FAST_PATH_THRESHOLD = 0.95
        if (
            expected_total is not None
            and stale_streak >= 1
            and count >= expected_total * FAST_PATH_THRESHOLD
        ):
            log.info(
                "  [%s] scroll %d: %d/%d shows — count stable, accepting",
                label, attempt, count, expected_total,
            )
            break

        if expected_total is not None:
            gap_note = f" (target: {expected_total}, missing: {expected_total - count})"
        else:
            gap_note = f" (stale streak: {stale_streak})"
        log.info("  [%s] scroll %d: %d unique shows%s",
                 label, attempt, count, gap_note)

        if stale_streak >= max_stale:
            if expected_total is not None and count < expected_total:
                log.info(
                    "  [%s] count stable at %d for %d scrolls (expected %d) — "
                    "site likely reports more than it lazy-loads",
                    label, count, max_stale, expected_total,
                )
            else:
                log.info("  [%s] count stable for %d scrolls — done", label, max_stale)
            break

        # Scroll and wait for the lazy loader's network activity to settle.
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            page.wait_for_load_state("networkidle", timeout=scroll_hard_timeout_ms)
        except Exception:
            # Networkidle timing out on chatty sites is normal; the
            # stale-streak / expected-total exits handle this.
            pass

    return ordered_pairs, expected_total


def discover_listings(
    *,
    headless: bool = True,
    scroll_hard_timeout_ms: int = SCROLL_HARD_TIMEOUT_MS,
    max_stale: int = MAX_STALE_SCROLLS,
    max_attempts: int = MAX_SCROLL_ATTEMPTS,
    include_tag_lists: bool = True,
) -> tuple[
    list[tuple[int, str]],
    list[dict],
    dict[int, list[str]],
    dict[str, int],
    int | None,
]:
    """Open the master listing (and optionally the two filter slices) in
    a single browser context, scroll each one until all shows are
    discovered, and return:

      master_pairs: list of (id, slug) for every show on the master
        listing, in DOM order.
      listing_cards_by_id: {show_id: rich_card_dict} — the rich card
        data harvested from (a) the SSR'd __NEXT_DATA__ initialShows
        (first 54) and (b) the lazy-load XHR responses intercepted
        during scroll (remaining ~140). Keyed by id so the caller can
        look up the listing card for each show in O(1) and merge it
        into the per-show records.
      appears_in_map: {show_id: [tag_name, ...]} — which filter slices
        each show appears in.
      tag_list_counts: {tag_name: count_discovered}, or -1 on failure.
      expected_total: pagination.total from the master listing, if
        readable, else None.

    Single browser process. Tag-slice pages share a context with the
    master, so route blocking and consent dismissal happen once.
    """
    from playwright.sync_api import sync_playwright

    log.info("Launching browser to discover all shows on the listing(s)")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=DEFAULT_HEADERS["User-Agent"],
            locale="en-GB",
        )

        def _route(route):
            req = route.request
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                return route.abort()
            url = req.url
            if any(pat in url for pat in BLOCKED_HOST_PATTERNS):
                return route.abort()
            route.continue_()
        ctx.route("**/*", _route)

        page = ctx.new_page()

        # Set up the lazy-load capture sink. We seed it from the SSR'd
        # __NEXT_DATA__ after navigation, then the response listener
        # accumulates the remaining cards as they're fetched during scroll.
        listing_cards_by_id: dict[int, dict] = {}
        _install_listing_response_capture(page, listing_cards_by_id)

        try:
            # --- master listing ---
            log.info("Navigating to master: %s", LISTING_URL)
            page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60_000)
            # state="attached" not the default "visible": the cards mount
            # into the DOM well before their entrance animation finishes
            # rendering them as "visible" in Playwright's sense (display:none
            # → opacity:0 → opacity:1 over a few hundred ms). Since we
            # extract hrefs via eval_on_selector_all (DOM-only, no
            # visibility check), waiting for visibility just makes us slow.
            page.wait_for_selector('a[href^="/show/"]', timeout=30_000, state="attached")
            _dismiss_consent_banners(page)

            # Seed the sink from the SSR'd initialShows (first 54). The
            # response listener has already been collecting since
            # navigation, so by now it may also have some cards from any
            # data calls fired during initial hydration.
            for s in _read_initial_shows_from_next_data(page):
                cid = s.get("id") if isinstance(s.get("id"), int) else s.get("showId")
                if isinstance(cid, int) and cid not in listing_cards_by_id:
                    listing_cards_by_id[cid] = s
            log.info("  seeded %d initial cards from __NEXT_DATA__", len(listing_cards_by_id))

            master_pairs, expected_total = _scroll_and_collect_show_pairs(
                page,
                scroll_hard_timeout_ms=scroll_hard_timeout_ms,
                max_stale=max_stale,
                max_attempts=max_attempts,
                label="all-shows",
            )

            if expected_total is not None and len(master_pairs) < expected_total:
                missing = expected_total - len(master_pairs)
                if missing > 2:
                    log.warning(
                        "Only found %d of %d expected shows (%d missing)",
                        len(master_pairs), expected_total, missing,
                    )
                else:
                    log.info(
                        "Found %d of %d shows — %d show%s in catalogue "
                        "but not displayed (normal)",
                        len(master_pairs), expected_total, missing,
                        "" if missing == 1 else "s",
                    )

            # --- tag-slice listings ---
            appears_in_map: dict[int, list[str]] = {}
            tag_list_counts: dict[str, int] = {}

            if include_tag_lists:
                for tag, tag_url in TAG_LISTS.items():
                    try:
                        log.info("Navigating to tag '%s': %s", tag, tag_url)
                        page.goto(tag_url, wait_until="domcontentloaded", timeout=60_000)
                        # Some tag slices may have zero shows (e.g. offers
                        # could legitimately be empty); allow the selector
                        # wait to time out softly rather than abort.
                        try:
                            page.wait_for_selector(
                                'a[href^="/show/"]', timeout=15_000, state="attached",
                            )
                        except Exception:
                            log.info("  [%s] no show links found within 15s — empty tag?", tag)
                        pairs, _ = _scroll_and_collect_show_pairs(
                            page,
                            scroll_hard_timeout_ms=scroll_hard_timeout_ms,
                            max_stale=max_stale,
                            max_attempts=max_attempts,
                            label=tag,
                        )
                        tag_list_counts[tag] = len(pairs)
                        for show_id, _slug in pairs:
                            appears_in_map.setdefault(show_id, []).append(tag)
                        log.info("Discovered %d shows on tag '%s'", len(pairs), tag)
                    except Exception as e:
                        log.warning("Tag list '%s' failed: %s — skipping", tag, e)
                        tag_list_counts[tag] = -1

            log.info("Discovery complete: %d master shows, %d tags scraped, "
                     "%d rich listing cards captured",
                     len(master_pairs),
                     sum(1 for v in tag_list_counts.values() if v >= 0),
                     len(listing_cards_by_id))
            return (master_pairs, listing_cards_by_id, appears_in_map,
                    tag_list_counts, expected_total)

        finally:
            ctx.close()
            browser.close()


# ---------------------------------------------------------------------------
# Step 2 — per-show detail via plain HTTP (fast, parallel)
# ---------------------------------------------------------------------------

def _norm_url(u: Any) -> str | None:
    """Normalise an asset URL: handle protocol-relative ("//host/path")
    and reject non-string inputs. Used for image URLs which come back
    protocol-relative from Contentful."""
    if not isinstance(u, str) or not u:
        return None
    if u.startswith("//"):
        return "https:" + u
    return u


def _none_if_sentinel(v: Any) -> Any:
    """Treat the literal strings "null" and "None" as actual None.

    The LT API emits `"endDate": "null"` (the four-character string!)
    for open-ended runs — observed on 30 of 54 master-listing shows.
    Without this, every consumer of the JSON would need its own
    special case. Empty strings get the same treatment for consistency."""
    if isinstance(v, str) and v.strip().lower() in ("null", "none", ""):
        return None
    return v


def _coerce_listing_card_review_summary(rs: Any) -> dict | None:
    """Convert a TodayTix-style reviewSummary blob into our ReviewSummary
    shape. Returns None for shows with no reviews (score absent and zero
    review count)."""
    if not isinstance(rs, dict) or not rs:
        return None
    if rs.get("score") is None and not rs.get("reviewsCount"):
        return None

    raw_top = rs.get("topReviews") or []
    top: list[TopReview] = []
    for r in raw_top:
        if not isinstance(r, dict):
            continue
        top.append(TopReview(
            score=r.get("score"),
            five_star_score=r.get("fiveStarScore"),
            title=r.get("title"),
            body=r.get("body") or r.get("text") or r.get("content"),
            url=r.get("url"),
            publication=r.get("publication") or r.get("source"),
            stars=r.get("stars"),
        ))

    summary = ReviewSummary(
        score=rs.get("score"),
        five_star_score=rs.get("fiveStarScore"),
        score_description=rs.get("scoreDescription"),
        reviews_count=rs.get("reviewsCount"),
        short_reviews_count=rs.get("shortReviewsCount"),
        audience_acclaimed=bool(rs.get("audienceAcclaimed")),
        adjectives=list(rs.get("adjectives") or []),
        top_reviews=top,
    )
    return asdict(summary)


def _build_card_record_from_listing(card: dict) -> dict:
    """Distil a raw listing-card JSON object (from the SSR __NEXT_DATA__)
    into the subset of fields that go on the final Show record. Detail
    pages will overwrite/augment showtimes; everything else here is
    treated as candidate data (caller merges with detail-page values).

    Works for both listing-card dicts (from productList.results.shows)
    and detail-page product dicts (props.pageProps.product) — they have
    the same shape on this platform."""
    if not isinstance(card, dict):
        card = {}

    cat = card.get("category") or {}
    if not isinstance(cat, dict):
        cat = {}
    low = card.get("lowPriceForRegularTickets") or {}
    if not isinstance(low, dict):
        low = {}
    lottery = card.get("lowPriceForLotteryTickets") or {}
    if not isinstance(lottery, dict):
        lottery = {}
    promo = card.get("promotion") or {}
    if not isinstance(promo, dict):
        promo = {}

    # Media. URLs come back protocol-relative ("//images.ctfassets.net/...")
    # from Contentful so we normalise to https.
    media = (card.get("images") or {}).get("productMedia") or {}
    if not isinstance(media, dict):
        media = {}

    header_image_node = media.get("headerImage") or {}
    if isinstance(header_image_node, dict):
        header_image = _norm_url(((header_image_node.get("file") or {}).get("url")))
    else:
        header_image = None

    poster_node = media.get("posterImageLandscape") or {}
    if isinstance(poster_node, dict):
        poster_image = _norm_url(((poster_node.get("file") or {}).get("url")))
    else:
        poster_image = None

    gallery: list[dict] = []
    for item in (media.get("imagesAndVideos") or []):
        if not isinstance(item, dict):
            continue
        media_node = item.get("media") or {}
        if not isinstance(media_node, dict):
            continue
        mfile = media_node.get("file") or {}
        if not isinstance(mfile, dict):
            mfile = {}
        gallery.append({
            "url": _norm_url(mfile.get("url")),
            "title": media_node.get("title"),
            "description": media_node.get("description"),
            "content_type": mfile.get("contentType"),
        })

    return {
        "show_id": card.get("showId"),
        "name": card.get("name") or card.get("displayName") or "",
        "display_name": card.get("displayName"),
        "venue": card.get("venue"),
        "venue_url_slug": card.get("venueUrl"),
        "category": cat.get("name"),
        "category_slug": cat.get("slug"),
        "location_id": card.get("locationId"),
        "location_seo_name": card.get("locationSeoName"),
        "admission_type": card.get("admissionType"),
        "product_type": card.get("productType"),
        "start_date": _none_if_sentinel(card.get("startDate")),
        "end_date": _none_if_sentinel(card.get("endDate")),
        "low_price_value": low.get("value"),
        "low_price_display": low.get("display"),
        "low_price_display_rounded": low.get("displayRounded"),
        "currency": low.get("currency"),
        "lottery_low_price_value": lottery.get("value"),
        "lottery_low_price_display": lottery.get("display"),
        "max_discount_percentage": card.get("maxDiscountPercentage"),
        "has_promotion": bool(card.get("hasPromotion")),
        "promotion_label": promo.get("label"),
        "promotion_description": promo.get("description"),
        "promotion_voucher_code": promo.get("voucherCode"),
        "sales_message": _none_if_sentinel(card.get("salesMessage")),
        "savings_message": _none_if_sentinel(card.get("savingsMessage")),
        "avg_rating": card.get("avgRating"),
        "review_summary": _coerce_listing_card_review_summary(card.get("reviewSummary")),
        "description": _none_if_sentinel(card.get("description")),
        "header_image_url": header_image,
        "poster_image_url": poster_image,
        "gallery": gallery,
    }


class DetailFetcher:
    """Fetches and parses show-detail pages over plain HTTP.

    Each show page server-side-renders a `__NEXT_DATA__` JSON blob with
    `props.pageProps.product` (the canonical show record) and
    `props.pageProps.initialShowtimes` (showtimes with price bands).
    Same schema as the TodayTix parent platform.
    """

    def __init__(self, concurrency: int = DEFAULT_CONCURRENCY):
        self.concurrency = concurrency
        self.session = self._build_session(concurrency)

    @staticmethod
    def _build_session(pool_size: int) -> requests.Session:
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

    def _fetch_next_data(self, url: str) -> tuple[dict, str]:
        """Fetch a show page and return (next_data, final_url). final_url
        reflects any redirects (used to resolve id-only stub URLs)."""
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        match = NEXT_DATA_RE.search(resp.text)
        if not match:
            raise RuntimeError(
                f"__NEXT_DATA__ not found on {resp.url} (page layout changed?)"
            )
        return json.loads(match.group(1)), resp.url

    def fetch_show(
        self,
        show_id: int,
        slug: str,
        listing_card: dict | None,
        appears_in: list[str],
    ) -> tuple[Show | None, str | None]:
        """Return (show, error_msg). On success: (Show, None). On failure:
        (None, "<one-line reason>") — the reason is propagated to the
        ScrapeReport so consumers know what went wrong.

        Strategy:
          1. Hit /show/{id}-{slug}; parse __NEXT_DATA__.
          2. Pull rich product info + initialShowtimes from there.
          3. Merge with whatever rich card data we already have from the
             listing's SSR'd JSON (for the first 54 shows). Detail-page
             values win when both exist.
          4. Build a Show record with showtimes + price bands.

        If the slug is empty (id-only stub link), we use the no-slug URL
        and let the server redirect to the canonical."""
        if slug:
            url = SHOW_URL_TEMPLATE.format(id=show_id, slug=slug)
        else:
            url = f"{BASE_URL}/show/{show_id}"

        try:
            data, final_url = self._fetch_next_data(url)
        except requests.HTTPError as e:
            return None, f"HTTP {e.response.status_code if e.response is not None else '?'}"
        except requests.RequestException as e:
            return None, f"network: {type(e).__name__}"
        except Exception as e:
            return None, f"parse: {type(e).__name__}: {e}"

        try:
            pp = data["props"]["pageProps"]
            product = pp.get("product") or {}
            showtimes_raw = pp.get("initialShowtimes") or []

            # Defensive fallback: if the page shape ever changes such
            # that "product" lives under a different key, look there.
            if not product and isinstance(pp.get("show"), dict):
                product = pp["show"]

            # If we have a listing card with the rich SSR data, build the
            # base record from that; the detail page then *overrides*
            # any fields it has updated values for. (For shows past the
            # initial 54, listing_card is None and only detail wins.)
            base = _build_card_record_from_listing(listing_card) if listing_card else {}
            detail_fields = _build_card_record_from_listing(product) if product else {}

            # Merge: detail-page fields win where present and non-empty;
            # listing-card fields fill the gaps.
            merged: dict[str, Any] = {}
            for k in set(base) | set(detail_fields):
                base_v = base.get(k)
                det_v = detail_fields.get(k)
                if det_v not in (None, "", [], {}):
                    merged[k] = det_v
                else:
                    merged[k] = base_v

            # Resolve the canonical slug. Prefer (in order):
            #   1. product.slug from the detail page (authoritative);
            #   2. the slug we already have from the listing;
            #   3. parsing the slug out of the final URL we landed on.
            resolved_slug = (
                product.get("slug") if isinstance(product, dict) else None
            ) or slug
            if not resolved_slug:
                m = SHOW_HREF_RE.match(urlparse(final_url).path)
                if m and m.group(2):
                    resolved_slug = m.group(2)
            resolved_slug = resolved_slug or ""

            canonical_url = (
                SHOW_URL_TEMPLATE.format(id=show_id, slug=resolved_slug)
                if resolved_slug else final_url
            )

            show = Show(
                id=show_id,
                show_id=merged.get("show_id"),
                name=merged.get("name") or "",
                display_name=merged.get("display_name"),
                slug=resolved_slug,
                url=canonical_url,
                venue=merged.get("venue"),
                venue_url_slug=merged.get("venue_url_slug"),
                category=merged.get("category"),
                category_slug=merged.get("category_slug"),
                location_id=merged.get("location_id"),
                location_seo_name=merged.get("location_seo_name"),
                admission_type=merged.get("admission_type"),
                product_type=merged.get("product_type"),
                start_date=merged.get("start_date"),
                end_date=merged.get("end_date"),
                low_price_value=merged.get("low_price_value"),
                low_price_display=merged.get("low_price_display"),
                low_price_display_rounded=merged.get("low_price_display_rounded"),
                currency=merged.get("currency"),
                lottery_low_price_value=merged.get("lottery_low_price_value"),
                lottery_low_price_display=merged.get("lottery_low_price_display"),
                max_discount_percentage=merged.get("max_discount_percentage"),
                has_promotion=bool(merged.get("has_promotion")),
                promotion_label=merged.get("promotion_label"),
                promotion_description=merged.get("promotion_description"),
                promotion_voucher_code=merged.get("promotion_voucher_code"),
                sales_message=merged.get("sales_message"),
                savings_message=merged.get("savings_message"),
                avg_rating=merged.get("avg_rating"),
                review_summary=merged.get("review_summary"),
                description=merged.get("description"),
                header_image_url=merged.get("header_image_url"),
                poster_image_url=merged.get("poster_image_url"),
                gallery=merged.get("gallery") or [],
                appears_in=sorted(appears_in),
                showtimes=[self._build_showtime(st, show_id) for st in showtimes_raw],
            )
        except Exception as e:
            return None, f"shape: {type(e).__name__}: {e}"

        return show, None

    # -- showtime shaping ---------------------------------------------------

    @staticmethod
    def _build_booking_url(show_id: int, showtime_id: int | None,
                           date: str | None, time: str | None) -> str | None:
        """Build a booking URL. LT routes bookings through TodayTix's
        seating-plan endpoint; the parameter names match TodayTix's
        because the booking backend is shared."""
        if showtime_id is None:
            return None
        params: dict[str, Any] = {"product_id": show_id, "showtime_id": showtime_id}
        if date:
            params["date"] = date
        if time:
            params["slot"] = time
        return f"{BOOKING_URL_BASE}?{urlencode(params)}"

    def _build_showtime(self, raw: dict, show_id: int) -> Showtime:
        if not isinstance(raw, dict):
            raw = {}
        regular = raw.get("regularTickets") or {}
        if not isinstance(regular, dict):
            regular = {}
        low_price = regular.get("lowPrice") or {}
        if not isinstance(low_price, dict):
            low_price = {}

        bands: list[PriceBand] = []
        for band in regular.get("priceBands") or []:
            if not isinstance(band, dict):
                continue
            price = band.get("price") or {}
            if not isinstance(price, dict):
                price = {}
            face = band.get("faceValue") or {}
            if not isinstance(face, dict):
                face = {}
            bands.append(PriceBand(
                price_value=price.get("value"),
                currency=price.get("currency"),
                price_display=price.get("display"),
                face_value=face.get("value"),
                seats_available=band.get("numAssignedSeatsAvailable"),
                max_contiguous_seats=band.get("maxContiguousSeats"),
            ))

        showtime_id = raw.get("id")
        local_date = raw.get("localDate")
        local_time = raw.get("localTime")

        return Showtime(
            showtime_id=showtime_id,
            datetime=raw.get("datetime"),
            local_date=local_date,
            local_time=local_time,
            day_of_week=raw.get("dayOfWeek"),
            daypart=raw.get("daypart"),
            booking_url=self._build_booking_url(show_id, showtime_id, local_date, local_time),
            low_price_value=low_price.get("value"),
            low_price_display=low_price.get("display"),
            currency=low_price.get("currency"),
            has_promotion=bool(regular.get("hasPromotion")),
            promotion_label=regular.get("promotionLabel"),
            seats_available=regular.get("numAssignedSeatsAvailable"),
            price_bands=bands,
        )

    # -- orchestrator -------------------------------------------------------

    def fetch_all(
        self,
        pairs: list[tuple[int, str]],
        listing_cards_by_id: dict[int, dict],
        appears_in_map: dict[int, list[str]],
    ) -> tuple[list[Show], list[ShowFailure]]:
        """Fetch every (id, slug) pair concurrently. Returns (shows, failures)
        preserving the listing order. Failures carry a short
        machine-readable error reason so the caller can summarise them."""
        t_start = time.monotonic()
        log.info(
            "Fetching %d show pages with %d workers...",
            len(pairs), self.concurrency,
        )

        results: list[Show | None] = [None] * len(pairs)
        errors: list[str | None] = [None] * len(pairs)
        progress = [0]
        progress_lock = Lock()

        def task(idx: int, show_id: int, slug: str) -> None:
            listing_card = listing_cards_by_id.get(show_id)
            appears = appears_in_map.get(show_id, [])
            show, err = self.fetch_show(show_id, slug, listing_card, appears)
            results[idx] = show
            errors[idx] = err
            with progress_lock:
                progress[0] += 1
                n = progress[0]
                if n % 10 == 0 or n == len(pairs):
                    log.info("  progress: %d/%d", n, len(pairs))

        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futures = [
                ex.submit(task, i, sid, slug)
                for i, (sid, slug) in enumerate(pairs)
            ]
            for _ in as_completed(futures):
                pass

        shows = [s for s in results if s is not None]
        failures = [
            ShowFailure(
                id=pairs[i][0],
                slug=pairs[i][1],
                url=(SHOW_URL_TEMPLATE.format(id=pairs[i][0], slug=pairs[i][1])
                     if pairs[i][1] else f"{BASE_URL}/show/{pairs[i][0]}"),
                error=errors[i] or "unknown",
            )
            for i, s in enumerate(results) if s is None
        ]

        elapsed = time.monotonic() - t_start
        log.info(
            "Fetched %d/%d shows in %.1fs (%.2f req/s)",
            len(shows), len(pairs), elapsed,
            len(pairs) / elapsed if elapsed > 0 else 0,
        )
        return shows, failures


# ---------------------------------------------------------------------------
# Sanity checks (warn-but-write policy)
# ---------------------------------------------------------------------------

def run_sanity_checks(
    shows: list[Show],
    failures: list[ShowFailure],
    expected_total: int | None,
) -> list[str]:
    """Inspect the scraped data for structural anomalies. Mirrors the
    same warn-but-write policy as the other scrapers — never abort here;
    just surface issues for downstream consumers.

    Thresholds are intentionally generous — they're tuned to catch
    "the site changed shape" failures, not "one show was sold out today".

    Threshold rationale (empirical from real runs):
      * description / header_image / start_date — these come from the
        rich listing card (SSR + lazy-load XHR capture). Healthy runs
        have ≤2% missing (gift cards, pre-opening shows). >5% missing
        means the XHR capture has stopped working — exactly the failure
        mode we want to catch.
      * end_date — open-ended runs legitimately have no end_date (~25%
        baseline). NO threshold; we'd just be alerting on Wicked.
      * review_summary / avg_rating — most shows simply don't have
        reviews on this platform (~80% missing baseline). NO threshold.
      * low_price_value on shows — pre-opening shows lack a from-price
        (~11% baseline). Threshold at 25% catches schema drift without
        firing on a few new announcements.
      * showtime price coverage — see below."""
    warnings: list[str] = []
    n = len(shows)

    if n == 0:
        warnings.append("CRITICAL: zero shows scraped — listing or detail layer fully broken")
        return warnings

    # Coverage vs. site's own reported total. Small gaps (≤2 shows) are
    # normal — pagination.total occasionally counts shows that aren't
    # actually displayed (matches TodayTix's behaviour).
    if expected_total is not None:
        if n < expected_total * 0.8:
            warnings.append(
                f"coverage: only {n}/{expected_total} shows succeeded "
                f"({100*n/expected_total:.0f}% < 80% threshold)"
            )
        elif n < expected_total - 2:
            warnings.append(
                f"coverage: {n}/{expected_total} shows succeeded "
                f"({expected_total - n} missing)"
            )

    # Required-field checks. If the page structure drifts, these are
    # the first things to silently null out.
    def pct(predicate, label: str, threshold: float = 0.10) -> None:
        bad = sum(1 for s in shows if predicate(s))
        if bad / n > threshold:
            warnings.append(
                f"field-quality: {bad}/{n} shows have {label} "
                f"(>{threshold:.0%} threshold — possible schema drift)"
            )

    # Structural fields — these should never be missing.
    pct(lambda s: not s.name, "missing name")
    pct(lambda s: not s.url, "missing url", threshold=0.0)
    pct(lambda s: not s.venue, "missing venue", threshold=0.05)
    pct(lambda s: not s.category, "missing category")

    # Listing-card enrichments. Tight thresholds (5%) because the XHR
    # capture should give us near-100% coverage; >5% missing means it's
    # broken and we've silently regressed to detail-page-only data.
    pct(lambda s: not s.description, "missing description", threshold=0.05)
    pct(lambda s: not s.header_image_url, "missing header image", threshold=0.05)
    pct(lambda s: not s.start_date, "missing start_date", threshold=0.05)
    # Low-price has a higher legitimate-absence rate (pre-opening shows).
    pct(lambda s: s.low_price_value is None, "missing low_price_value", threshold=0.25)
    # Showtime presence — some shows legitimately have none (closing /
    # pre-sale), so keep this loose.
    pct(lambda s: not s.showtimes, "zero showtimes", threshold=0.20)

    # ---- Showtime-level integrity ----
    all_st = [(s, st) for s in shows for st in s.showtimes]
    if all_st:
        # booking_url must always be present — it's deterministically
        # constructible from (show_id, showtime_id), so absence indicates
        # a parser failure.
        no_booking = sum(1 for _, st in all_st if not st.booking_url)
        if no_booking:
            warnings.append(
                f"showtime: {no_booking}/{len(all_st)} showtimes lack booking_url"
            )

        # Garbage prices (negative / zero) — would indicate parsing drift.
        bad_prices = sum(
            1 for _, st in all_st for b in st.price_bands
            if b.price_value is not None and b.price_value <= 0
        )
        if bad_prices:
            warnings.append(
                f"price: {bad_prices} price bands have non-positive values"
            )

        # Bandless showtimes. Empirically ~4% of showtimes are "announced
        # but not yet on sale" (no low_price, no bands, no seats info,
        # frequently 6+ months out). That's normal. >30% would suggest
        # the price-band parser broke.
        no_bands = sum(1 for _, st in all_st if not st.price_bands)
        if no_bands / len(all_st) > 0.30:
            warnings.append(
                f"price: {no_bands}/{len(all_st)} showtimes lack price bands "
                f"({100*no_bands/len(all_st):.0f}% > 30% threshold — "
                "possible parser regression, or the platform has changed how it "
                "exposes priceBands)"
            )

    # Failure breakdown (informational, not threshold-based)
    if failures:
        from collections import Counter
        kinds = Counter(f.error.split(":")[0] for f in failures)
        breakdown = ", ".join(f"{k}={v}" for k, v in kinds.most_common())
        warnings.append(f"fetch-failures: {len(failures)} shows failed ({breakdown})")

    return warnings


def build_showtime_diagnostics(shows: list[Show]) -> dict[str, Any]:
    """Compute a structured snapshot of showtime quality — what fraction
    are fully on-sale vs announced-but-not-yet-bookable vs genuinely
    anomalous. Embedded under report.showtime_diagnostics so downstream
    consumers can filter or alert on these without parsing log lines.

    The taxonomy reflects an empirical observation: ~4% of showtimes on
    LT are scheduled (booking_url present, real date) but carry zero
    price info — no low_price_value, no price_bands, no seats info. This
    is the platform's signal for "performance is on the calendar but
    seats haven't been released for sale yet" (typically 6+ months out).
    These are not failures — they're the natural state of far-future
    announcements.

    Anything else (booking URL missing, or some price info present but
    not all) is a real anomaly and gets surfaced separately."""
    all_st = [(s, st) for s in shows for st in s.showtimes]
    total = len(all_st)
    if total == 0:
        return {"total": 0}

    n_priced = 0          # has price_bands populated → fully on sale
    n_lowprice_no_bands = 0  # has low_price_value but no bands — partial data
    n_announced_only = 0     # has booking_url + no price info at all
    n_no_booking = 0         # missing booking_url (real anomaly)

    for _, st in all_st:
        has_url = bool(st.booking_url)
        has_bands = bool(st.price_bands)
        has_lowprice = st.low_price_value is not None
        if not has_url:
            n_no_booking += 1
        elif has_bands:
            n_priced += 1
        elif has_lowprice:
            n_lowprice_no_bands += 1
        else:
            n_announced_only += 1

    return {
        "total": total,
        "priced": n_priced,
        "priced_pct": round(100.0 * n_priced / total, 1),
        # Announced + scheduled but not on sale yet. Far-future dates
        # typical. Normal at low single-digit percentages.
        "announced_only": n_announced_only,
        "announced_only_pct": round(100.0 * n_announced_only / total, 1),
        # Partial price info (low_price but no bands). Rare; usually a
        # quirk of a specific promo or limited-release seat type.
        "lowprice_no_bands": n_lowprice_no_bands,
        # Real anomaly — booking URL absent. Should be 0 in healthy runs.
        "no_booking_url": n_no_booking,
    }


def compare_with_previous(new_shows: list[Show], previous_path: Path) -> list[str]:
    """Compare today's scrape against the previous JSON output and flag
    catastrophic regressions. Returns [] if no previous file exists or
    the diff is within normal bounds. Same thresholds as the other
    scrapers — small daily drift (shows ending, new shows announced) is
    fine; a 50% drop is almost certainly a scraper regression."""
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

    st_new = sum(len(s.showtimes) for s in new_shows)
    st_prev = prev.get("showtime_count") or sum(
        len(s.get("showtimes") or []) for s in prev_shows
    )
    if st_prev > 0 and st_new < st_prev * 0.50:
        warnings.append(
            f"prev-run: showtime count dropped {st_prev} → {st_new} "
            f"({100*(st_prev-st_new)/st_prev:.0f}% loss, >50% threshold)"
        )

    new_ids = {s.id for s in new_shows}
    prev_ids = {s["id"] for s in prev_shows if isinstance(s.get("id"), int)}
    if prev_ids:
        vanished = prev_ids - new_ids
        if len(vanished) > len(prev_ids) * 0.30:
            warnings.append(
                f"prev-run: {len(vanished)}/{len(prev_ids)} shows from previous "
                f"run are missing today ({100*len(vanished)/len(prev_ids):.0f}% "
                "churn, >30% threshold)"
            )

    return warnings


# ---------------------------------------------------------------------------
# Output writer (atomic)
# ---------------------------------------------------------------------------

def write_output(shows: list[Show], path: Path, report: ScrapeReport) -> None:
    """Atomically write the JSON output and embedded scrape report.

    Atomic via write-to-tmp + rename: a previous run's good JSON survives
    a crashed current run. Rename is atomic on every modern OS (POSIX
    and Windows via Python 3.3+'s os.replace)."""
    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": LISTING_URL,
        "show_count": len(shows),
        "showtime_count": sum(len(s.showtimes) for s in shows),
        "report": asdict(report),
        "shows": [asdict(s) for s in shows],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    log.info(
        "Wrote %s — %d shows, %d showtimes, %d warning(s)",
        path, payload["show_count"], payload["showtime_count"],
        len(report.warnings),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Scrape londontheatre.co.uk shows, showtimes and prices "
                    "(hybrid Playwright + requests)."
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Only scrape this many shows after listing discovery (for testing).")
    p.add_argument("--out", type=Path, default=Path("londontheatre_london.json"),
                   help="Output JSON file path (default: ./londontheatre_london.json).")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel HTTP workers for show details (default: {DEFAULT_CONCURRENCY}).")
    p.add_argument("--headed", action="store_true",
                   help="Show the Playwright browser window (default: headless).")
    p.add_argument("--no-tag-lists", action="store_true",
                   help="Skip the 2 filter slices (last-minute, offers); "
                        "shows will have empty appears_in arrays.")
    args = p.parse_args(argv)

    # Step 1: discover all show URLs via real browser (master + tag slices)
    try:
        (master_pairs, listing_cards_by_id, appears_in_map,
         tag_list_counts, expected_total) = discover_listings(
            headless=not args.headed,
            include_tag_lists=not args.no_tag_lists,
        )
    except ImportError:
        log.error(
            "Playwright is required. Install with:  pip install playwright "
            "&& playwright install chromium"
        )
        return 2
    except Exception as e:
        log.error("Listing discovery failed: %s", e)
        return 1

    if not master_pairs:
        log.error("No shows discovered on listing — aborting (preserving any previous output).")
        return 1

    # Log coverage of the rich card data so it's visible in run logs
    # whether the XHR-interception path is working.
    covered = sum(1 for sid, _ in master_pairs if sid in listing_cards_by_id)
    log.info("Rich listing-card coverage: %d/%d master shows (%.0f%%)",
             covered, len(master_pairs),
             100.0 * covered / len(master_pairs) if master_pairs else 0)

    if args.limit is not None:
        master_pairs = master_pairs[: args.limit]
        log.info("--limit applied: scraping details for %d shows", len(master_pairs))
        expected_total = None  # comparison would be misleading after slicing

    # Step 2: fetch each show page in parallel via plain HTTP
    fetcher = DetailFetcher(concurrency=args.concurrency)
    shows, failures = fetcher.fetch_all(
        master_pairs, listing_cards_by_id, appears_in_map,
    )

    # Step 3: sanity checks + previous-run comparison
    warnings = run_sanity_checks(shows, failures, expected_total)
    if args.limit is None:
        warnings.extend(compare_with_previous(shows, args.out))
    for w in warnings:
        log.warning("anomaly: %s", w)

    # Structured showtime-quality snapshot (separate from warnings so a
    # healthy "4% of perfs are far-future announcements" doesn't look
    # like a problem in the warnings list).
    showtime_diagnostics = build_showtime_diagnostics(shows)
    if showtime_diagnostics.get("total"):
        log.info(
            "Showtime status: %d priced (%.1f%%), %d announced-only "
            "(%.1f%%, normal at low single digits), %d no booking_url",
            showtime_diagnostics["priced"], showtime_diagnostics["priced_pct"],
            showtime_diagnostics["announced_only"],
            showtime_diagnostics["announced_only_pct"],
            showtime_diagnostics["no_booking_url"],
        )

    report = ScrapeReport(
        expected_show_count=expected_total,
        discovered_show_count=len(master_pairs),
        succeeded_show_count=len(shows),
        failed_show_count=len(failures),
        tag_lists_scraped=sorted(k for k, v in tag_list_counts.items() if v >= 0),
        tag_list_counts=tag_list_counts,
        warnings=warnings,
        failures=failures,
        showtime_diagnostics=showtime_diagnostics,
    )

    # Always write if we have something. Empty result would clobber
    # yesterday's good JSON for no reason.
    if not shows:
        log.error("No shows successfully scraped — preserving previous output.")
        return 1

    write_output(shows, args.out, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
