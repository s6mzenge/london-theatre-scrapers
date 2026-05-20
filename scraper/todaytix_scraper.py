"""
TodayTix London scraper (hybrid Playwright + requests)
======================================================

Scrapes every show on https://www.todaytix.com/london/category/all-shows,
their performances, and the price bands for each performance.

Why hybrid?
-----------
TodayTix's all-shows listing caps the server-side response at 54 shows.
The remaining ~145 shows are loaded by client-side JavaScript as the user
scrolls. That bit requires a real browser, so we use Playwright for the
listing page (one page, one browser session).

Each individual show's detail page, by contrast, server-side-renders
everything we need — all performances and price bands sit in a
`<script id="__NEXT_DATA__">` JSON blob. So for the ~200 show detail
pages we use plain `requests` in parallel, which is 10× faster than
spinning up a browser per show.

Setup
-----
    pip install requests playwright
    playwright install chromium

Usage
-----
    python todaytix_scraper.py                          # scrape everything
    python todaytix_scraper.py --limit 5                # test with 5 shows
    python todaytix_scraper.py --out data/shows.json    # custom output path
    python todaytix_scraper.py --headed                 # show browser window
    python todaytix_scraper.py --concurrency 32         # more parallel detail fetches

Output is a single JSON file:

    {
      "scraped_at": "2026-05-18T22:08:56+00:00",
      "source": "https://www.todaytix.com/london/category/all-shows",
      "show_count": 199,
      "showtime_count": 40123,
      "shows": [ { ... }, ... ]
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

BASE_URL = "https://www.todaytix.com"
LISTING_URL = f"{BASE_URL}/london/category/all-shows"
SHOW_URL_TEMPLATE = f"{BASE_URL}/london/shows/{{id}}-{{slug}}"
BOOKING_URL_BASE = f"{BASE_URL}/booking/seating-plan"

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

# Retries handled at the urllib3 adapter level for connection-pool reuse.
RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

# Listing scroll behaviour. We scroll until the show count stays unchanged
# across MAX_STALE_SCROLLS attempts. Between scrolls we wait for network
# idle (the lazy loader's fetch to settle) rather than sleeping a fixed
# amount — much faster on good connections, still safe on slow ones.
SCROLL_NETWORK_IDLE_MS = 600   # how long the network must stay quiet
SCROLL_HARD_TIMEOUT_MS = 4000  # cap per scroll iteration
MAX_STALE_SCROLLS = 3
MAX_SCROLL_ATTEMPTS = 80       # hard ceiling, ~5 minutes worst case

# Resource types we never need for scraping. Blocking these cuts page
# weight by ~80% and makes scrolling noticeably snappier — the lazy
# loader fires faster because it's not competing with image downloads.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
# Third-party hosts we definitely don't need (analytics, ads, chat
# widgets, A/B testing). Matched as substrings of the request URL.
BLOCKED_HOST_PATTERNS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.com", "facebook.net", "connect.facebook",
    "segment.com", "segment.io", "branch.io", "branch-cdn",
    "snowplowanalytics", "hotjar.com", "fullstory.com",
    "intercom.io", "intercomcdn.com", "zendesk.com",
    "adjust.com", "appsflyer.com", "amplitude.com",
    "optimizely.com", "launchdarkly.com",
    "/static/fonts/",  # local font assets
)

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

# Show detail URLs look like /london/shows/{id}-{slug}. This regex picks
# them out of the rendered listing DOM.
SHOW_HREF_RE = re.compile(r'^/london/shows/(\d+)-([a-z0-9\-]+)$')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("todaytix")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PriceBand:
    price_value: float | None
    currency: str | None
    price_display: str | None
    face_value: float | None
    seats_available: int | None
    max_contiguous_seats: int | None


@dataclass
class Showtime:
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
class Show:
    id: int
    name: str
    slug: str
    url: str
    venue: str | None
    category: str | None
    start_date: str | None
    end_date: str | None
    low_price_value: float | None
    low_price_display: str | None
    currency: str | None
    has_promotion: bool
    promotion_label: str | None
    rating_score: int | None
    rating_count: int | None
    description: str | None
    showtimes: list[Showtime] = field(default_factory=list)


@dataclass
class ShowFailure:
    """A show that we expected to scrape (from the listing) but couldn't.

    Recorded so the consumer of the JSON can see which shows are missing
    from this run and why, instead of silently getting a shorter list.
    """
    id: int
    slug: str
    url: str
    error: str


@dataclass
class ScrapeReport:
    """Summary of a single scrape run.

    Embedded into the output JSON under "report" so downstream consumers
    can detect partial / degraded runs without parsing log files.
    Warnings are non-fatal anomalies (the run "warn-but-write" policy
    means we always produce output even if these fire).
    """
    expected_show_count: int | None      # from pagination.total, if readable
    discovered_show_count: int           # what we got from the listing
    succeeded_show_count: int            # detail-page fetches that worked
    failed_show_count: int
    warnings: list[str] = field(default_factory=list)
    failures: list[ShowFailure] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step 1 — Listing discovery via Playwright
# ---------------------------------------------------------------------------

def discover_show_links(
    listing_url: str,
    *,
    headless: bool = True,
    network_idle_ms: int = SCROLL_NETWORK_IDLE_MS,
    scroll_hard_timeout_ms: int = SCROLL_HARD_TIMEOUT_MS,
    max_stale: int = MAX_STALE_SCROLLS,
    max_attempts: int = MAX_SCROLL_ATTEMPTS,
) -> tuple[list[tuple[int, str]], int | None]:
    """Open the listing page in a real browser, scroll to bottom repeatedly
    until no new shows appear, then return every unique (show_id, slug) pair.

    Returns a (pairs, expected_total) tuple. expected_total is the count
    the site itself reports in pagination.total, or None if we couldn't
    read it. Pairs is in DOM order so the output stays stable.

    Speed optimisations:
      * Images, fonts, stylesheets, and known third-party trackers are
        blocked at the network layer. The listing page is ~80% smaller
        with these gone, and the lazy loader fires faster because it's
        not waiting behind image downloads.
      * Between scrolls we wait for *network idle* (the lazy fetch
        settling), not a fixed sleep. On a fast connection that's ~600ms
        instead of 1500ms; on a slow connection it adapts upward.
      * If we can read pagination.total from the page's __NEXT_DATA__,
        we stop scrolling the moment that target is reached — no
        "confirming" scrolls needed.
    """
    # Import here so users who only want to inspect the script don't need
    # Playwright installed.
    from playwright.sync_api import sync_playwright

    log.info("Launching browser to discover all shows on the listing")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=DEFAULT_HEADERS["User-Agent"],
            locale="en-GB",
            # JS still runs; only resource downloads are filtered.
        )

        # Install a route handler that aborts useless requests before
        # they're even fetched. This runs for every request the page
        # makes (HTML, XHR, images, scripts, …).
        def _route(route):
            req = route.request
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                return route.abort()
            url = req.url
            if any(p in url for p in BLOCKED_HOST_PATTERNS):
                return route.abort()
            route.continue_()
        ctx.route("**/*", _route)

        page = ctx.new_page()

        try:
            page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
            # Wait for at least one product card to be in the DOM. The
            # selector targets the show-detail link pattern that's present
            # on every card.
            page.wait_for_selector('a[href*="/london/shows/"]', timeout=30_000)

            # Read the expected total from the page's own SSR'd
            # __NEXT_DATA__ blob. TodayTix reports the true catalogue
            # size in pagination.total even though the initial render
            # only contains the first 54 shows. If we can read it, we
            # know exactly when to stop scrolling — no stale-streak
            # heuristic needed, no extra scrolls to "confirm" we're done.
            expected_total: int | None = None
            try:
                next_data_json = page.eval_on_selector(
                    '#__NEXT_DATA__', 'el => el.textContent',
                )
                next_data = json.loads(next_data_json)
                pag = (next_data.get('props', {}).get('pageProps', {})
                       .get('productList', {}).get('results', {})
                       .get('pagination', {}))
                if isinstance(pag.get('total'), int):
                    expected_total = pag['total']
                    log.info("  page reports total=%d shows in catalogue", expected_total)
            except Exception as e:
                log.info("  could not read expected total (%s) — falling back to stale-streak", e)

            # Dismiss cookie / consent banners that block scrolling. We try
            # a few common button texts; each is best-effort.
            for txt in ("Accept all", "Accept All", "I agree", "Got it", "Accept"):
                try:
                    btn = page.get_by_role("button", name=re.compile(txt, re.I))
                    if btn.count():
                        btn.first.click(timeout=2000)
                        log.info("  dismissed banner: %r", txt)
                        break
                except Exception:
                    pass

            ordered_pairs: list[tuple[int, str]] = []
            seen_ids: set[int] = set()
            stale_streak = 0
            last_count = 0

            for attempt in range(1, max_attempts + 1):
                # Pull every show link currently in the DOM.
                hrefs = page.eval_on_selector_all(
                    'a[href*="/london/shows/"]',
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
                    slug = m.group(2)
                    if show_id in seen_ids:
                        continue
                    seen_ids.add(show_id)
                    ordered_pairs.append((show_id, slug))

                count = len(ordered_pairs)

                # Primary exit: we know the expected total and we've hit it.
                # This is precise and avoids the ~8s of "confirming" scrolls.
                if expected_total is not None and count >= expected_total:
                    log.info(
                        "  scroll %d: %d shows — reached expected total, done",
                        attempt, count,
                    )
                    break

                # Safety-net exit: the count has stopped growing.
                # Always active, even when expected_total is known, because
                # the site sometimes reports a total that's slightly off
                # from what's actually lazy-loadable (e.g. total=199 but
                # only 198 shows are displayable). Without this, the loop
                # would run until max_attempts.
                if count == last_count:
                    stale_streak += 1
                else:
                    stale_streak = 0
                last_count = count

                # Fast-path early exit: if we've got nearly everything the
                # site claims exists (≥95%) AND the count is stable for
                # even one scroll, we're done. This handles the common
                # TodayTix off-by-one where total=199 but only 198 load:
                # without this, every run would waste ~12s on confirming
                # scrolls.
                FAST_PATH_THRESHOLD = 0.95
                if (
                    expected_total is not None
                    and stale_streak >= 1
                    and count >= expected_total * FAST_PATH_THRESHOLD
                ):
                    log.info(
                        "  scroll %d: %d/%d shows — count stable, accepting "
                        "(site likely reports more than it lazy-loads)",
                        attempt, count, expected_total,
                    )
                    break

                # When we have an expected total, log the gap so it's
                # obvious in the logs why we're still scrolling.
                if expected_total is not None:
                    missing = expected_total - count
                    gap_note = f" (target: {expected_total}, missing: {missing})"
                else:
                    gap_note = f" (stale streak: {stale_streak})"

                log.info("  scroll %d: %d unique shows%s", attempt, count, gap_note)

                # Conservative fallback exit: count has been stuck for the
                # full max_stale streak. This is the path for "we got
                # significantly less than expected and don't know why" —
                # we still stop, but log it as a concern.
                if stale_streak >= max_stale:
                    if expected_total is not None and count < expected_total:
                        log.info(
                            "  count stable at %d for %d scrolls (expected %d) — "
                            "site likely reports more shows than it actually lazy-loads",
                            count, max_stale, expected_total,
                        )
                    else:
                        log.info("  count stable for %d scrolls — done", max_stale)
                    break

                # Scroll to bottom and wait for the lazy loader's network
                # activity to settle. wait_for_load_state('networkidle')
                # returns once there are <=2 in-flight requests for
                # `network_idle_ms`. The hard timeout is a safety net so
                # one stuck analytics beacon can't hang us.
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=scroll_hard_timeout_ms,
                    )
                except Exception:
                    # Networkidle can time out on chatty sites; the
                    # stale-streak / expected-total exits handle this.
                    pass

            if expected_total is not None and len(ordered_pairs) < expected_total:
                missing = expected_total - len(ordered_pairs)
                # A 1-2 show gap is normal — TodayTix's pagination.total
                # occasionally counts a show that isn't actually displayed
                # (likely hidden via hideFromSearch). Only flag bigger gaps.
                if missing > 2:
                    log.warning(
                        "Only found %d of %d expected shows (%d missing) — "
                        "site may have changed or lazy loader misbehaved",
                        len(ordered_pairs), expected_total, missing,
                    )
                else:
                    log.info(
                        "Found %d of %d expected shows — %d show%s in catalogue "
                        "but not displayed (normal: hidden / not yet on sale)",
                        len(ordered_pairs), expected_total, missing,
                        "" if missing == 1 else "s",
                    )

            log.info("Discovered %d unique shows on the listing", len(ordered_pairs))
            return ordered_pairs, expected_total

        finally:
            ctx.close()
            browser.close()


# ---------------------------------------------------------------------------
# Step 2 — Per-show detail via plain HTTP (fast, parallel)
# ---------------------------------------------------------------------------

class DetailFetcher:
    """Fetches and parses show-detail pages over plain HTTP.

    Each show page is server-side-rendered and contains a complete
    `__NEXT_DATA__` JSON blob with product info plus `initialShowtimes`,
    so a real browser isn't needed here.
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

    def _fetch_next_data(self, url: str) -> dict:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        match = NEXT_DATA_RE.search(resp.text)
        if not match:
            raise RuntimeError(
                f"__NEXT_DATA__ not found on {resp.url} (page layout changed?)"
            )
        return json.loads(match.group(1))

    def fetch_show(self, show_id: int, slug: str) -> tuple[Show | None, str | None]:
        """Return (show, error_msg). On success: (Show, None). On failure:
        (None, "<one-line reason>"). The reason is propagated to the
        ScrapeReport so consumers know what went wrong.
        """
        url = SHOW_URL_TEMPLATE.format(id=show_id, slug=slug)
        try:
            data = self._fetch_next_data(url)
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
            elastic = pp.get("productElasticSearchData") or {}
            show = self._build_show(show_id, slug, product, showtimes_raw, elastic)
        except Exception as e:
            return None, f"shape: {type(e).__name__}: {e}"

        return show, None

    # -- shaping ------------------------------------------------------------

    @staticmethod
    def _build_booking_url(show_id: int, showtime_id: int | None,
                           date: str | None, time: str | None) -> str | None:
        if showtime_id is None:
            return None
        # qt (quantity) is required by TodayTix's seating-plan page —
        # without it, the page renders an empty "No seats available" state
        # even when seats *are* available. We default to 2 (the most common
        # booking size and TodayTix's own UI default); the user can adjust
        # the quantity on the page itself.
        params: dict[str, Any] = {
            "product_id": show_id,
            "qt": 2,
            "showtime_id": showtime_id,
        }
        if date:
            params["date"] = date
        if time:
            params["slot"] = time
        return f"{BOOKING_URL_BASE}?{urlencode(params)}"

    def _build_showtime(self, raw: dict, show_id: int) -> Showtime:
        regular = raw.get("regularTickets") or {}
        api_low_price = regular.get("lowPrice") or {}
        showtime_seats = regular.get("numAssignedSeatsAvailable")

        bands: list[PriceBand] = []
        for band in regular.get("priceBands") or []:
            price = band.get("price") or {}
            face = band.get("faceValue") or {}
            bands.append(PriceBand(
                price_value=price.get("value"),
                currency=price.get("currency"),
                price_display=price.get("display"),
                face_value=face.get("value"),
                seats_available=band.get("numAssignedSeatsAvailable"),
                max_contiguous_seats=band.get("maxContiguousSeats"),
            ))

        # Compute the from-price from AVAILABLE bands only.
        #
        # TodayTix's `regularTickets.lowPrice` is the cheapest band's price
        # *regardless of availability* — i.e. when the £81 band sells out
        # and only £150 seats remain, the API still advertises £81. Using
        # that value would mean our table shows prices users can't actually
        # buy, which is the whole bug this scraper is supposed to avoid.
        #
        # A band is considered available iff numAssignedSeatsAvailable > 0.
        # Bands with no seat info at all are treated as a schema-drift
        # signal and we fall back to the API's advertised price so we
        # don't silently zero out every showtime if TodayTix renames the
        # field.
        bands_with_seat_info = [b for b in bands if b.seats_available is not None]
        if not bands_with_seat_info:
            # Schema fallback — no per-band availability data anywhere on
            # this showtime. Trust the advertised price; flag via the
            # warnings pipeline if this becomes widespread.
            low_price_value = api_low_price.get("value")
            low_price_display = api_low_price.get("display")
            currency = api_low_price.get("currency")
        else:
            available = [
                b for b in bands_with_seat_info
                if (b.seats_available or 0) > 0 and b.price_value is not None
            ]
            if showtime_seats == 0 or not available:
                # Sold out, either at the showtime level or because every
                # band reports zero seats. Downstream consumers (dedupe,
                # display) already filter on `low_price_value is not None`.
                low_price_value = None
                low_price_display = None
                currency = api_low_price.get("currency")
            else:
                cheapest = min(available, key=lambda b: b.price_value)
                low_price_value = cheapest.price_value
                low_price_display = cheapest.price_display
                currency = cheapest.currency or api_low_price.get("currency")

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
            low_price_value=low_price_value,
            low_price_display=low_price_display,
            currency=currency,
            has_promotion=bool(regular.get("hasPromotion")),
            promotion_label=regular.get("promotionLabel"),
            seats_available=showtime_seats,
            price_bands=bands,
        )

    def _build_show(self, show_id: int, slug: str, product: dict,
                    showtimes_raw: list[dict], elastic: dict) -> Show:
        # The category/venue may live on the product, or fall back to
        # elastic-search summary data attached to the page.
        category_node = product.get("category") or elastic.get("category") or {}
        category = category_node.get("name") if isinstance(category_node, dict) else None
        venue_node = product.get("venue") or {}
        venue_name = venue_node.get("name") if isinstance(venue_node, dict) else None

        from_price = product.get("fromPrice") or {}
        promotion = product.get("promotion") or {}
        review = product.get("reviewSummary") or elastic.get("reviewSummary") or {}

        return Show(
            id=show_id,
            name=product.get("name") or product.get("displayName") or slug,
            slug=slug,
            url=SHOW_URL_TEMPLATE.format(id=show_id, slug=slug),
            venue=venue_name,
            category=category,
            start_date=product.get("startingDate") or product.get("previewsFrom"),
            end_date=product.get("closingDate") or product.get("bookingTo"),
            low_price_value=from_price.get("value"),
            low_price_display=from_price.get("display"),
            currency=from_price.get("currency"),
            has_promotion=bool(product.get("hasPromotion")),
            promotion_label=promotion.get("label") if isinstance(promotion, dict) else None,
            rating_score=review.get("score"),
            rating_count=review.get("reviewsCount"),
            description=product.get("shortDescription") or product.get("about"),
            showtimes=[self._build_showtime(st, show_id) for st in showtimes_raw],
        )

    # -- orchestrator -------------------------------------------------------

    def fetch_all(
        self, pairs: list[tuple[int, str]]
    ) -> tuple[list[Show], list[ShowFailure]]:
        """Fetch every (id, slug) pair concurrently. Returns (shows, failures)
        preserving the listing order for shows. Failures carry a short
        machine-readable error reason so the caller can summarise them.
        """
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
            show, err = self.fetch_show(show_id, slug)
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
                url=SHOW_URL_TEMPLATE.format(id=pairs[i][0], slug=pairs[i][1]),
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
# Previous-run comparison
# ---------------------------------------------------------------------------

def compare_with_previous(
    new_shows: list[Show],
    previous_path: Path,
) -> list[str]:
    """Compare today's scrape against the previous JSON output and return
    warnings about suspicious changes. Returns [] if no previous file
    exists, the file is unreadable, or the diff is within normal bounds.

    The idea: catalog drift of a few shows per day is normal (runs ending,
    new shows announced). A sudden 50% drop is almost certainly a scraper
    regression we should know about before the bad data gets committed.
    """
    if not previous_path.exists():
        return []

    try:
        with previous_path.open("r", encoding="utf-8") as f:
            prev = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # Old file unreadable — don't pretend we can compare. Log once
        # and move on; the new file will replace it.
        log.info("Could not read previous output for comparison (%s)", e)
        return []

    prev_shows = prev.get("shows") or []
    if not prev_shows:
        return []

    warnings: list[str] = []
    n_new = len(new_shows)
    n_prev = len(prev_shows)

    # 1. Catastrophic shrinkage. Yesterday had 199, today has 50 = something
    #    is very wrong. Threshold: more than 20% loss.
    if n_prev > 0 and n_new < n_prev * 0.80:
        warnings.append(
            f"prev-run: show count dropped {n_prev} → {n_new} "
            f"({100*(n_prev-n_new)/n_prev:.0f}% loss, >20% threshold)"
        )

    # 2. Showtime count likewise — catches the case where show count is
    #    stable but each show suddenly has way fewer performances.
    st_new = sum(len(s.showtimes) for s in new_shows)
    st_prev = prev.get("showtime_count") or sum(len(s.get("showtimes") or []) for s in prev_shows)
    if st_prev > 0 and st_new < st_prev * 0.50:
        warnings.append(
            f"prev-run: showtime count dropped {st_prev} → {st_new} "
            f"({100*(st_prev-st_new)/st_prev:.0f}% loss, >50% threshold)"
        )

    # 3. Wholesale ID churn. A small number of new/removed shows per day
    #    is normal. >30% of yesterday's IDs vanishing is not.
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




def run_sanity_checks(
    shows: list[Show],
    failures: list[ShowFailure],
    expected_total: int | None,
) -> list[str]:
    """Inspect the scraped data for structural anomalies and return a list
    of human-readable warning strings. The policy is warn-but-write: we
    never abort here; the caller logs these and embeds them in the output.

    These thresholds are intentionally generous — they're tuned to catch
    "the site changed shape" failures, not "one show was sold out today".
    """
    warnings: list[str] = []
    n = len(shows)

    if n == 0:
        warnings.append("CRITICAL: zero shows scraped — listing or detail layer fully broken")
        return warnings

    # Coverage vs. site's own reported total. We accept a small gap
    # (≤2 shows) since TodayTix's pagination.total occasionally counts
    # shows that aren't actually displayed.
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

    # Required fields. If the page structure drifts, these are the first
    # things to silently null out. Tolerance is small — a couple of shows
    # with weird data is fine, half the catalogue isn't.
    def pct(predicate, label: str, threshold: float = 0.10) -> None:
        bad = sum(1 for s in shows if predicate(s))
        if bad / n > threshold:
            warnings.append(
                f"field-quality: {bad}/{n} shows have {label} "
                f"(>{threshold:.0%} threshold — possible schema drift)"
            )

    pct(lambda s: not s.name, "missing name")
    pct(lambda s: not s.venue, "missing venue", threshold=0.05)
    pct(lambda s: not s.category, "missing category")
    pct(lambda s: not s.url, "missing url", threshold=0.0)
    pct(lambda s: not s.showtimes, "zero showtimes", threshold=0.20)

    # Showtime-level integrity
    all_st = [(s, st) for s in shows for st in s.showtimes]
    if all_st:
        no_booking = sum(1 for _, st in all_st if not st.booking_url)
        if no_booking:
            warnings.append(
                f"showtime: {no_booking}/{len(all_st)} showtimes lack booking_url"
            )
        # Price bands with garbage values would indicate parsing drift
        bad_prices = sum(
            1 for _, st in all_st for b in st.price_bands
            if b.price_value is not None and b.price_value <= 0
        )
        if bad_prices:
            warnings.append(
                f"price: {bad_prices} price bands have non-positive values"
            )

    # Failure breakdown (informational, not threshold-based)
    if failures:
        # Group by error reason for a compact summary
        from collections import Counter
        kinds = Counter(f.error.split(":")[0] for f in failures)
        breakdown = ", ".join(f"{k}={v}" for k, v in kinds.most_common())
        warnings.append(f"fetch-failures: {len(failures)} shows failed ({breakdown})")

    return warnings


# ---------------------------------------------------------------------------
# Output writer (atomic)
# ---------------------------------------------------------------------------

def write_output(
    shows: list[Show],
    path: Path,
    report: ScrapeReport,
) -> None:
    """Atomically write the JSON output and embedded scrape report.

    The atomic part matters: a previous run's good JSON should survive a
    crashed or partial current run. We write to a sibling .tmp file and
    rename it into place — rename is atomic on every modern OS (POSIX
    and on Windows since Python 3.3's os.replace).
    """
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
    # os.replace is atomic on POSIX and Windows; pathlib.Path.replace
    # delegates to it. If we crash before this line, `path` is untouched.
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
        description="Scrape TodayTix London shows, showtimes and prices (hybrid Playwright + requests)."
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Only scrape this many shows after listing discovery (for testing).")
    p.add_argument("--out", type=Path, default=Path("todaytix_london.json"),
                   help="Output JSON file path (default: ./todaytix_london.json).")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel HTTP workers for show details (default: {DEFAULT_CONCURRENCY}).")
    p.add_argument("--headed", action="store_true",
                   help="Show the Playwright browser window (default: headless).")
    p.add_argument("--network-idle-ms", type=int, default=SCROLL_NETWORK_IDLE_MS,
                   help=f"Network-idle window between scrolls in ms (default: {SCROLL_NETWORK_IDLE_MS}).")
    args = p.parse_args(argv)

    # Step 1: discover all show URLs via real browser
    try:
        pairs, expected_total = discover_show_links(
            LISTING_URL,
            headless=not args.headed,
            network_idle_ms=args.network_idle_ms,
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

    if not pairs:
        log.error("No shows discovered on listing — aborting (preserving any previous output).")
        return 1

    if args.limit is not None:
        pairs = pairs[: args.limit]
        log.info("--limit applied: scraping details for %d shows", len(pairs))
        # When limiting, the expected_total comparison would be misleading
        expected_total = None

    # Step 2: fetch each show page in parallel via plain HTTP
    fetcher = DetailFetcher(concurrency=args.concurrency)
    shows, failures = fetcher.fetch_all(pairs)

    # Step 3: build the run report and log warnings (warn-but-write policy)
    warnings = run_sanity_checks(shows, failures, expected_total)

    # Compare against the previous output file (if any). Skipped when
    # --limit is used because today's truncated run isn't comparable to
    # yesterday's full run, and would always trip the shrinkage check.
    if args.limit is None:
        prev_warnings = compare_with_previous(shows, args.out)
        warnings.extend(prev_warnings)

    for w in warnings:
        log.warning("anomaly: %s", w)

    report = ScrapeReport(
        expected_show_count=expected_total,
        discovered_show_count=len(pairs),
        succeeded_show_count=len(shows),
        failed_show_count=len(failures),
        warnings=warnings,
        failures=failures,
    )

    # Always write, even with warnings — but only if we got *something*.
    # An empty result would clobber yesterday's good JSON for no reason.
    if not shows:
        log.error("No shows successfully scraped — preserving previous output.")
        return 1

    write_output(shows, args.out, report)

    # Exit 0 even with warnings, per the warn-but-write policy. Workflow
    # logs will surface them. (If you ever want fail-loud, return 1 here
    # when warnings is non-empty.)
    return 0


if __name__ == "__main__":
    sys.exit(main())
