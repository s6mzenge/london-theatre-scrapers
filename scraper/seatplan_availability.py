"""
SeatPlan per-performance availability verifier
==============================================

Second pass for `seatplan_scraper.py`. The main scraper reads each
performance's price from JSON-LD on the show's detail page, where
SeatPlan emits the **show-wide** minimum (e.g. £6 standing-yard tier
at the Globe) into every per-performance `offers.lowPrice` block —
even for performances where that tier isn't actually on sale. The
21 May 2026 2pm matinee of *A Midsummer Night's Dream* is the
canonical example: detail-page JSON-LD says £6, but the cheapest
seat you can actually buy for that performance is £12.

This script fixes that for the prices the dedupe layer relies on,
without touching the scraper's existing fields.

How it works
------------
For each performance in `seatplan_london.json`, build the ticketing-
page URL and GET it. The ticketing page is fully server-rendered and
includes, near the bottom, an inline tracking call:

    fireCrmEvent('Viewed Performance', {
        ...
        'price': 12, 'min_price': 12, 'max_price': 102,
        'is_discounted': false,
        'is_no_booking_fee': false,
        ...
    });

`min_price` / `max_price` here are the actual currently-available
range for that specific performance (matching the £12-£102 tier
buttons shown in the seat-map header). We regex these out and write
them back onto the performance record.

URL pattern
-----------
    {show.url}tickets/{D-mon-YYYY}/{H-MMam|pm}/

For example:
    /london/a-midsummer-nights-dream-tickets/tickets/21-may-2026/2-00pm/

Date: day with no leading zero, 3-letter lowercase month, 4-digit year.
Time: 12-hour clock, hour with no leading zero, minutes zero-padded,
lowercase am/pm (00:00 -> 12-00am, 12:00 -> 12-00pm).

Why not full JS parsing?
------------------------
The `fireCrmEvent` argument is a JavaScript object literal, not JSON:
single quotes, template literals (backticks) for strings that contain
apostrophes (`A Midsummer Night's Dream`), trailing commas. A full
parser would be fragile. The numeric and boolean fields we care about
are unquoted in source — straight field-name regexes against the
matched call body are robust to the messy parts.

Fields added to each Performance dict
-------------------------------------
    verified_min_price          float | None   actual cheapest-available
    verified_max_price          float | None   most-expensive-available
    verified_price              float | None   fireCrmEvent.price (usually == min_price)
    verified_is_discounted      bool  | None
    verified_is_no_booking_fee  bool  | None
    verified_price_source       str            one of:
        "ticketing_page"  — page loaded and min_price extracted
        "no_seats"        — page loaded but no usable price (sold out / off-sale)
        "fetch_failed"    — network error or non-200 status
        "skipped"         — perf has no date/time, no show URL, or date is past
    verified_status             int|str|None   HTTP status, or exception summary
    verified_url                str  | None    URL that was fetched
    verified_checked_at         str            UTC ISO timestamp

Chip-pass fields (added only on rows the chip pass ran on)
----------------------------------------------------------
The fireCrmEvent payload sometimes carries the wrong value: a show-
wide marketing floor (the £6/£8 Globe yard tier appears in every
per-performance event regardless of actual availability), or a
user-viewed tier when the page loads with a non-floor seat selected
(observed on Paddington: fireCrmEvent says £215, real floor is £60).
A targeted second pass opens such rows in headless Chromium and reads
the actually-rendered price chips. A row is flagged "suspect" via any
of three heuristics: verified_min_price ≤ £15 (catches yard-tier
phantoms), max/min ratio > 8 (catches phantom-floor shape), or
verified_min_price > 2× low_price (catches default-selected-tier
high-side outliers).

    verified_chip_min           float | None   cheapest chip on rendered page
    verified_chip_max           float | None   most-expensive chip on rendered page
    verified_chip_candidates    list[float]    full sorted list of plausible chips
    verified_chip_source        str            "chips" | "no_chips_found" | "fetch_failed"
    verified_chip_reason        str            "low_floor" | "wide_ratio" | "high_outlier"
    verified_chip_note          str            short diagnostic
    verified_chip_checked_at    str            UTC ISO timestamp

The existing `low_price` / `currency` / `availability` fields are
**not modified**, nor are the fireCrmEvent verified_* fields. The
dedupe layer prefers `verified_chip_min` when it's set, falls back to
`verified_min_price`, then `low_price`. Each tier is a stricter
extraction than the previous.

Usage
-----
    python seatplan_availability.py                       # in-place on default file
    python seatplan_availability.py --in input.json       # different input
    python seatplan_availability.py --out output.json     # write to different file
    python seatplan_availability.py --concurrency 24      # tune fireCrmEvent parallelism
    python seatplan_availability.py --limit 50            # smoke-test on N perfs
    python seatplan_availability.py --include-past        # also check past dates
    python seatplan_availability.py --dry-run             # don't write
    python seatplan_availability.py --skip-chips          # skip the chip second pass
    python seatplan_availability.py --chip-workers 5      # tune chip-pass parallelism

Dependencies
------------
The chip pass requires Playwright + Chromium:
    pip install playwright
    python -m playwright install chromium

Without those, the chip pass logs an error and is skipped; the
fireCrmEvent pass still runs and writes its results normally.

Exit codes
----------
    0  success (some or all perfs verified, partial fails are normal)
    1  bad input (file missing, malformed JSON)
    2  zero successes despite >0 attempts — likely URL pattern drift
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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
DEFAULT_TIMEOUT_S = 15

RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

# Sources we expect to see in `verified_price_source`. Keep aligned with
# the dedupe schema branch.
SOURCE_OK         = "ticketing_page"
SOURCE_NO_SEATS   = "no_seats"
SOURCE_FETCH_FAIL = "fetch_failed"
SOURCE_SKIPPED    = "skipped"

EXIT_CLEAN     = 0
EXIT_BAD_INPUT = 1
EXIT_DRIFT     = 2

# ---------------------------------------------------------------------------
# Chip pass config
# ---------------------------------------------------------------------------
# After the requests-based fireCrmEvent pass completes, a second pass
# runs in headless Chromium for performances whose `verified_min_price`
# looks suspect — typically because SeatPlan's fireCrmEvent payload
# emits the show-wide marketing floor (e.g. £6/£8 Globe yard tier) or a
# user-viewed tier (Paddington-style premium-default) instead of the
# actual cheapest-bookable chip. The chip pass reads the visible price
# chips rendered by SeatPlan's frontend, which are the source of truth
# for what the user can actually buy.
#
# Three independent suspicion heuristics — a perf is verified by chips
# if ANY fires:
#   1. SUSPECT_LOW_FLOOR — verified_min_price ≤ £15. Catches Globe
#      yard-tier phantoms (£6, £8, £13). Some false-positive cost on
#      genuinely cheap shows; the chip pass just re-confirms them.
#   2. SUSPECT_RATIO — verified_max/verified_min > 8. Most shows have
#      a 2-5x range; a 10x+ ratio (£8 → £102 Globe shape) is a strong
#      phantom-floor signal.
#   3. SUSPECT_OUTLIER — verified_min_price > 2 × low_price. Catches
#      Paddington-style cases where fireCrmEvent fired with a default-
#      selected premium tier instead of the floor.
SUSPECT_LOW_FLOOR        = 15.0
SUSPECT_RATIO_THRESHOLD  = 8.0
SUSPECT_OUTLIER_RATIO    = 2.0

# Performance budget for the chip pass. SP has no observed bot wall
# (we hit it 254× per scrape with the requests-based fireCrmEvent pass
# already and never trip detection), so we can run many parallel
# Playwright pages. Chips render fast once domcontentloaded fires —
# typically <1s — so the stability poll can be aggressive.
#
# Wallclock target: ~1 min on ~250 suspect rows. Math: 250 rows ÷ 10
# workers ≈ 25 rows per worker × ~2.5s mean per row ≈ 60s.
CHIP_WORKERS              = 10
CHIP_NAV_TIMEOUT_MS       = 30_000
CHIP_FIRST_PRICE_TIMEOUT  = 12_000   # safety net for slow loads; rarely hit
CHIP_STABILITY_POLL_S     = 0.25
CHIP_STABILITY_POLLS      = 2        # 2 × 250ms = 500ms of unchanged candidates
CHIP_MAX_WAIT_S           = 2.5      # hard cap; chips usually settle in <1s

# Plausible per-ticket range for chip extraction. Floor of £5 keeps the
# legitimate £6 Globe yard tier in range when it really is bookable —
# the chip pass's job is to find the truth, not to enforce our priors.
CHIP_PRICE_MIN = 5.0
CHIP_PRICE_MAX = 600.0

# Resources to drop — text scan needs none of these, and blocking them
# saves ~60% of page weight on commercial-template SPAs.
CHIP_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

CHIP_PRICE_RE = re.compile(r"£\s*(\d{1,4}(?:\.\d{1,2})?)")

CHIP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Stealth init script — basic fingerprint masking that helps clear the
# easy automation-detection checks. Manual rather than pulling in
# playwright-stealth; covers the common cases without a new dep.
CHIP_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
"""

# verified_chip_source values written onto each verified performance:
CHIP_SOURCE_OK         = "chips"           # extracted chip min/max, trust these
CHIP_SOURCE_NO_CHIPS   = "no_chips_found"  # page loaded but no plausible £-amount
CHIP_SOURCE_FETCH_FAIL = "fetch_failed"    # browser navigation or render error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seatplan-avail")


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

def build_session(
    pool_size: int,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
) -> requests.Session:
    if proxy_url:
        s: requests.Session = _ProxyingSession(proxy_url, proxy_token)
    else:
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


class _ProxyingSession(requests.Session):
    """requests.Session that tunnels every request through a reverse
    proxy via the X-Proxy-Target header. See seatplan_scraper.py for
    the full rationale — same class, repeated here to keep the verifier
    module self-contained (mirrors the OLT scraper/availability split,
    which uses the identical pattern).
    """

    def __init__(self, proxy_url: str, proxy_token: str | None) -> None:
        super().__init__()
        self._proxy_url = proxy_url.rstrip("/")
        self._proxy_token = proxy_token or ""

    def request(self, method, url, **kwargs):
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            caller_headers = kwargs.get("headers") or {}
            proxy_headers = {**caller_headers, "X-Proxy-Target": url}
            if self._proxy_token:
                proxy_headers["X-Proxy-Auth"] = self._proxy_token
            kwargs["headers"] = proxy_headers
            url = self._proxy_url
        return super().request(method, url, **kwargs)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

# 3-letter lowercase month abbreviations, as SeatPlan uses them in URLs.
_MONTH_ABBR = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def date_to_url_segment(date_iso: str | None) -> str | None:
    """'2026-05-21' -> '21-may-2026'. Returns None if input malformed."""
    if not date_iso or len(date_iso) < 10:
        return None
    try:
        y, m, d = date_iso[:10].split("-")
        month_idx = int(m) - 1
        if not (0 <= month_idx < 12):
            return None
        return f"{int(d)}-{_MONTH_ABBR[month_idx]}-{y}"
    except (ValueError, IndexError):
        return None


def time_to_url_segment(time_hhmm: str | None) -> str | None:
    """'14:00' -> '2-00pm'. '00:30' -> '12-30am'. '12:00' -> '12-00pm'.

    Returns None on malformed input.
    """
    if not time_hhmm or ":" not in time_hhmm:
        return None
    try:
        h_str, m_str = time_hhmm.split(":", 1)
        h = int(h_str)
        m = int(m_str[:2])
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
    except ValueError:
        return None

    if h == 0:
        hour_12, suffix = 12, "am"
    elif h < 12:
        hour_12, suffix = h, "am"
    elif h == 12:
        hour_12, suffix = 12, "pm"
    else:
        hour_12, suffix = h - 12, "pm"
    return f"{hour_12}-{m:02d}{suffix}"


def build_ticketing_url(
    show_url: str | None, perf_date: str | None, perf_time: str | None,
) -> str | None:
    """Compose the per-performance ticketing-page URL, or None if any
    component is unusable."""
    if not show_url:
        return None
    date_seg = date_to_url_segment(perf_date)
    time_seg = time_to_url_segment(perf_time)
    if not date_seg or not time_seg:
        return None
    base = show_url if show_url.endswith("/") else show_url + "/"
    return urljoin(base, f"tickets/{date_seg}/{time_seg}/")


# ---------------------------------------------------------------------------
# fireCrmEvent parsing
# ---------------------------------------------------------------------------
#
# We deliberately don't try to JSON-parse the full call payload — its
# argument is a JS object literal with single quotes, template literals,
# embedded apostrophes, and trailing commas. Instead:
#
#   1. Locate the `fireCrmEvent('Viewed Performance', { ... })` call and
#      grab the {...} body.
#   2. Within that body, run targeted field regexes for the unquoted
#      numeric/boolean values we care about.
#
# The body regex is non-greedy and bounded by the closing `}` of the
# argument object — which works because none of the values inside this
# specific call use `{...}` themselves. That's the only fragility worth
# flagging; if SeatPlan ever nests an object in this payload, the body
# regex would need rebalancing.

_FIRE_BLOCK_RE = re.compile(
    r"fireCrmEvent\(\s*['\"]Viewed Performance['\"]\s*,\s*\{(?P<body>[^}]*)\}",
    re.DOTALL,
)

# Field-level extractors. Keys are unquoted in the input, e.g.
# `'min_price': 12`. The leading `['\"]` requires the key to start
# at a quote boundary, which prevents `'min_price'` matching when we
# look for the bare `'price'` key (they don't overlap).
def _num(key: str) -> re.Pattern:
    return re.compile(rf"['\"]{key}['\"]\s*:\s*([0-9]+(?:\.[0-9]+)?)")


def _bool(key: str) -> re.Pattern:
    return re.compile(rf"['\"]{key}['\"]\s*:\s*(true|false)\b")


_RE_MIN_PRICE         = _num("min_price")
_RE_MAX_PRICE         = _num("max_price")
_RE_PRICE             = _num("price")
_RE_IS_DISCOUNTED     = _bool("is_discounted")
_RE_IS_NO_BOOKING_FEE = _bool("is_no_booking_fee")


def parse_fire_crm(html: str) -> dict | None:
    """Return the parsed numeric/boolean fields from the 'Viewed
    Performance' fireCrmEvent call, or None if the call is absent."""
    block = _FIRE_BLOCK_RE.search(html)
    if not block:
        return None
    body = block.group("body")

    def _f(rx: re.Pattern) -> float | None:
        m = rx.search(body)
        return float(m.group(1)) if m else None

    def _b(rx: re.Pattern) -> bool | None:
        m = rx.search(body)
        return None if not m else (m.group(1) == "true")

    return {
        "min_price":         _f(_RE_MIN_PRICE),
        "max_price":         _f(_RE_MAX_PRICE),
        "price":             _f(_RE_PRICE),
        "is_discounted":     _b(_RE_IS_DISCOUNTED),
        "is_no_booking_fee": _b(_RE_IS_NO_BOOKING_FEE),
    }


# ---------------------------------------------------------------------------
# Per-performance worker
# ---------------------------------------------------------------------------

def _empty_result(url: str | None, source: str, status=None) -> dict:
    return {
        "verified_min_price": None,
        "verified_max_price": None,
        "verified_price": None,
        "verified_is_discounted": None,
        "verified_is_no_booking_fee": None,
        "verified_price_source": source,
        "verified_status": status,
        "verified_url": url,
        "verified_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def verify_one(
    session: requests.Session,
    url: str | None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Fetch one ticketing page and return the verified-price dict.

    Never raises — failures surface in verified_price_source / verified_status.
    """
    if not url:
        return _empty_result(url, SOURCE_SKIPPED)

    try:
        r = session.get(url, timeout=timeout_s)
    except requests.RequestException as e:
        return _empty_result(url, SOURCE_FETCH_FAIL, status=str(e)[:160])

    if r.status_code != 200:
        return _empty_result(url, SOURCE_FETCH_FAIL, status=r.status_code)

    parsed = parse_fire_crm(r.text)
    if parsed is None:
        # Page loaded but no Viewed-Performance tracking call. Most
        # commonly: sold-out redirect, off-sale page, or layout change.
        # Treat as no-seats; price_source surfaces the distinction.
        return _empty_result(url, SOURCE_NO_SEATS, status=r.status_code)

    result = _empty_result(url, SOURCE_OK, status=r.status_code)
    result["verified_min_price"]         = parsed["min_price"]
    result["verified_max_price"]         = parsed["max_price"]
    result["verified_price"]             = parsed["price"]
    result["verified_is_discounted"]     = parsed["is_discounted"]
    result["verified_is_no_booking_fee"] = parsed["is_no_booking_fee"]

    if parsed["min_price"] is None:
        # Tracking call present but missing the price field. Unusual —
        # mark as no_seats so the dedupe layer drops it cleanly.
        result["verified_price_source"] = SOURCE_NO_SEATS

    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def iter_perfs_to_check(
    payload: dict, today_iso: str, include_past: bool,
) -> list[tuple[int, int, str | None]]:
    """Yield (show_idx, perf_idx, url) tuples for every performance we
    should verify.

    Performances with unusable date/time get a None url and are marked
    SKIPPED in the result so the report counts stay honest. Past dates
    are dropped entirely unless --include-past is set.
    """
    out: list[tuple[int, int, str | None]] = []
    for si, show in enumerate(payload.get("shows") or []):
        show_url = show.get("url")
        for pi, perf in enumerate(show.get("performances") or []):
            date = perf.get("date")
            time_ = perf.get("time")
            if not include_past and date and date < today_iso:
                continue
            url = build_ticketing_url(show_url, date, time_)
            out.append((si, pi, url))
    return out


def run(
    payload: dict,
    *,
    concurrency: int,
    limit: int | None,
    include_past: bool,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
) -> dict:
    """Run verification in place on payload['shows'][i]['performances'][j].

    Returns a summary dict suitable for embedding under
    payload['report']['availability_verification'].

    If proxy_url is set, all requests are routed through that URL
    (typically a Cloudflare Worker forwarding to seatplan.com) with
    proxy_token sent as the X-Proxy-Auth header. See seatplan_scraper.py
    docstring for why this exists.
    """
    today_iso = datetime.now(timezone.utc).date().isoformat()
    tasks = iter_perfs_to_check(payload, today_iso, include_past=include_past)
    if limit is not None:
        tasks = tasks[:limit]
        log.info("--limit %d applied", limit)

    total = len(tasks)
    log.info(
        "Verifying %d performance(s) across %d show(s) with %d worker(s)",
        total, len(payload.get("shows") or []), concurrency,
    )
    if total == 0:
        return {
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_checked": 0, "ok": 0, "no_seats": 0,
            "fetch_failed": 0, "skipped": 0, "duration_seconds": 0.0,
        }

    counts = {SOURCE_OK: 0, SOURCE_NO_SEATS: 0,
              SOURCE_FETCH_FAIL: 0, SOURCE_SKIPPED: 0}
    counts_lock = Lock()
    progress = {"n": 0}
    progress_lock = Lock()

    session = build_session(
        pool_size=max(concurrency, 8),
        proxy_url=proxy_url,
        proxy_token=proxy_token,
    )

    def _job(task: tuple[int, int, str | None]) -> tuple[int, int, dict]:
        si, pi, url = task
        return si, pi, verify_one(session, url)

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_job, t) for t in tasks]
        for fut in as_completed(futures):
            try:
                si, pi, out = fut.result()
            except Exception as e:  # noqa: BLE001 — worker shouldn't raise, but defend
                log.warning("worker exception: %s", e)
                continue
            payload["shows"][si]["performances"][pi].update(out)
            src = out["verified_price_source"]
            with counts_lock:
                counts[src] = counts.get(src, 0) + 1
            with progress_lock:
                progress["n"] += 1
                if progress["n"] % 100 == 0:
                    log.info(
                        "  progress: %d/%d  (ok=%d, no_seats=%d, fail=%d)",
                        progress["n"], total,
                        counts[SOURCE_OK],
                        counts[SOURCE_NO_SEATS],
                        counts[SOURCE_FETCH_FAIL],
                    )

    elapsed = time.monotonic() - t0
    log.info(
        "Done in %.1fs — ok=%d, no_seats=%d, fetch_failed=%d, skipped=%d",
        elapsed,
        counts[SOURCE_OK],
        counts[SOURCE_NO_SEATS],
        counts[SOURCE_FETCH_FAIL],
        counts[SOURCE_SKIPPED],
    )

    summary = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_checked": total,
        "ok": counts[SOURCE_OK],
        "no_seats": counts[SOURCE_NO_SEATS],
        "fetch_failed": counts[SOURCE_FETCH_FAIL],
        "skipped": counts[SOURCE_SKIPPED],
        "duration_seconds": round(elapsed, 1),
    }

    # Embed in the existing report block so consumers (incl. dedupe)
    # can detect a stale/partial verification run.
    report = payload.setdefault("report", {})
    if isinstance(report, dict):
        report["availability_verification"] = summary

    return summary


# ---------------------------------------------------------------------------
# Chip pass — headless-browser re-verification for suspect performances
# ---------------------------------------------------------------------------
# The fireCrmEvent pass above catches the common case but mis-reports
# a small fraction of perfs because SeatPlan populates that payload
# inconsistently. The chip pass is a targeted re-extraction over only
# those rows that look suspicious, using rendered DOM text from the
# user-facing booking page. It writes new fields, never overwrites the
# fireCrmEvent ones — the dedupe layer decides which wins.

def _classify_suspect(p: dict) -> str | None:
    """Return a non-empty reason string if `p` looks suspect, else None.
    Each branch is documented in the chip-pass config comment above."""
    vmin = p.get("verified_min_price")
    if vmin is None:
        return None
    vmax = p.get("verified_max_price")
    low  = p.get("low_price")
    if vmin <= SUSPECT_LOW_FLOOR:
        return "low_floor"
    if vmax is not None and vmin > 0 and (vmax / vmin) > SUSPECT_RATIO_THRESHOLD:
        return "wide_ratio"
    if low is not None and low > 0 and (vmin / low) > SUSPECT_OUTLIER_RATIO:
        return "high_outlier"
    return None


async def _chip_block_heavy(route) -> None:
    """Drop image/media/font requests at the network layer."""
    try:
        if route.request.resource_type in CHIP_BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        # Route may already be resolved if the page closed mid-request.
        pass


# JS predicate: "page contains at least one £-amount in plausible range".
# Used to wait for hydration before the stability poll begins.
_CHIP_FIRST_PRICE_JS = """() => {
  const re = /£\\s*(\\d{1,4}(?:\\.\\d{1,2})?)/g;
  const t = document.body.innerText || '';
  let m;
  while ((m = re.exec(t)) !== null) {
    const v = parseFloat(m[1]);
    if (v >= %d && v <= %d) return true;
  }
  return false;
}""" % (int(CHIP_PRICE_MIN), int(CHIP_PRICE_MAX))


async def _chip_extract_one(context, url: str
                            ) -> tuple[float | None, float | None,
                                       list[float], str]:
    """Open `url` in a fresh page, wait for prices to stabilise, return
    (chip_min, chip_max, all_plausible_candidates, note).

    Strategy mirrors the proven verify_live_prices_batch.py extractor:
    domcontentloaded → wait for first plausible £ → stability poll
    (finish when candidate set has been unchanged for 3 consecutive
    250ms polls, or after 4s max) → scan final body innerText."""
    from playwright.async_api import TimeoutError as PWTimeout

    page = await context.new_page()
    text = ""
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=CHIP_NAV_TIMEOUT_MS)
        except Exception as e:
            return None, None, [], f"nav error — {type(e).__name__}: {e}"

        try:
            await page.wait_for_function(
                _CHIP_FIRST_PRICE_JS, timeout=CHIP_FIRST_PRICE_TIMEOUT,
            )
        except PWTimeout:
            # Continue anyway — scan below will report no-price if so.
            pass

        prev: frozenset[float] | None = None
        stable = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CHIP_MAX_WAIT_S
        while loop.time() < deadline:
            try:
                text = await page.evaluate("document.body.innerText") or ""
            except Exception:
                break
            cands = frozenset(
                float(m.group(1)) for m in CHIP_PRICE_RE.finditer(text)
                if CHIP_PRICE_MIN <= float(m.group(1)) <= CHIP_PRICE_MAX
            )
            if cands == prev and len(cands) > 0:
                stable += 1
                if stable >= CHIP_STABILITY_POLLS:
                    break
            else:
                stable = 0
                prev = cands
            await asyncio.sleep(CHIP_STABILITY_POLL_S)
    finally:
        try: await page.close()
        except Exception: pass

    raw = [float(m.group(1)) for m in CHIP_PRICE_RE.finditer(text)]
    valid = sorted({p for p in raw if CHIP_PRICE_MIN <= p <= CHIP_PRICE_MAX})
    if not valid:
        return None, None, [], (
            f"no chips in plausible range "
            f"({CHIP_PRICE_MIN:.0f}-{CHIP_PRICE_MAX:.0f}); "
            f"saw {len(raw)} raw match(es)"
        )
    return valid[0], valid[-1], valid, "chips"


async def _chip_worker(name: str, browser, queue: asyncio.Queue,
                       results: dict, lock: asyncio.Lock,
                       counter: list, total: int) -> None:
    """One long-lived context per worker. Pulls suspect items from the
    shared queue until empty."""
    context = await browser.new_context(
        viewport={"width": 1400, "height": 900},
        user_agent=CHIP_UA,
        locale="en-GB",
        timezone_id="Europe/London",
        extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
    )
    await context.route("**/*", _chip_block_heavy)
    await context.add_init_script(CHIP_STEALTH_JS)
    try:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            chip_min, chip_max, candidates, note = await _chip_extract_one(
                context, item["url"]
            )
            async with lock:
                counter[0] += 1
                results[item["key"]] = (chip_min, chip_max, candidates, note)
                if counter[0] % 10 == 0 or counter[0] == total:
                    log.info("  chip progress: %d/%d", counter[0], total)
    finally:
        try: await context.close()
        except Exception: pass


async def _chip_pass_async(suspect_items: list[dict],
                           workers: int = CHIP_WORKERS) -> dict:
    from playwright.async_api import async_playwright
    results: dict = {}
    counter = [0]
    total = len(suspect_items)
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            log.error("chip pass: could not launch Chromium — %s", e)
            log.error("  run: python -m playwright install chromium")
            return results

        queue: asyncio.Queue = asyncio.Queue()
        for item in suspect_items:
            queue.put_nowait(item)
        lock = asyncio.Lock()
        tasks = [
            asyncio.create_task(
                _chip_worker(f"w{i+1}", browser, queue, results,
                             lock, counter, total)
            )
            for i in range(workers)
        ]
        await asyncio.gather(*tasks, return_exceptions=False)
        try: await browser.close()
        except Exception: pass
    return results


def run_chip_pass(payload: dict, workers: int = CHIP_WORKERS,
                  cache_path: Path | None = None,
                  cache_ttl_hours: int = 24) -> dict:
    """Identify suspect performances across the payload, run the
    Playwright chip extractor over them, and write
    verified_chip_min / verified_chip_max / verified_chip_source onto
    each suspect performance in place. Returns a summary dict for the
    report block.

    Does NOT modify the existing verified_* fields. Dedupe consumes
    chip_min/max when verified_chip_source == 'chips', else falls back
    to the fireCrmEvent values, else the JSON-LD low_price. Each tier
    is a stricter extraction than the previous.

    Cache (optional): when `cache_path` is given, suspect rows are
    looked up in a JSON cache before the Playwright extractor runs.
    Hits skip extraction and reuse the cached chip values. Misses run
    the extractor and write back to the cache. The cache invalidates
    on TTL (default 24h) and on input change (catalogue values that
    changed since last verification). See chip_pass_cache.py for the
    contract."""
    if not isinstance(payload, dict):
        return {"ok": 0, "no_chips": 0, "fetch_failed": 0, "suspect_count": 0,
                "duration_seconds": 0.0, "cache_hits": 0, "cache_misses": 0}

    # Cache setup — fully optional. If chip_pass_cache can't be
    # imported (e.g. running against an older checkout), or the cache
    # path isn't given, the chip pass behaves exactly as before.
    cache_entries: dict = {}
    use_cache = cache_path is not None
    cache_mod = None
    if use_cache:
        try:
            import chip_pass_cache as cache_mod
            cache_entries = cache_mod.load(cache_path)
            log.info("Chip cache loaded from %s — %d entries",
                     cache_path, len(cache_entries))
        except ImportError:
            log.warning("chip_pass_cache module not importable; "
                        "running without cache.")
            use_cache = False
            cache_mod = None

    # Collect suspect performances along with mutable references.
    suspects = []
    by_reason: dict[str, int] = {}
    for s_idx, show in enumerate(payload.get("shows", []) or []):
        for p_idx, perf in enumerate(show.get("performances", []) or []):
            reason = _classify_suspect(perf)
            if reason is None:
                continue
            book_url = perf.get("book_url")
            if not book_url:
                continue
            suspects.append({
                "key":      (s_idx, p_idx),
                "url":      book_url,
                "reason":   reason,
                "show_url": show.get("url") or "?",
                "date":     perf.get("date"),
                "time":     perf.get("time"),
                # Catalogue values that the chip pass acts on. If these
                # change between runs, the cached chip result is stale.
                # For SP: low_price (the JSON-LD floor) + verified
                # min/max from the fireCrmEvent pass.
                "_input_low_price":         perf.get("low_price"),
                "_input_verified_min":      perf.get("verified_min_price"),
                "_input_verified_max":      perf.get("verified_max_price"),
            })
            by_reason[reason] = by_reason.get(reason, 0) + 1

    log.info("Chip pass: %d suspect performances identified  (%s)",
             len(suspects),
             ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())) or "none")

    if not suspects:
        return {"ok": 0, "no_chips": 0, "fetch_failed": 0,
                "suspect_count": 0, "duration_seconds": 0.0,
                "cache_hits": 0, "cache_misses": 0}

    # Cache lookup: partition suspects into hits (reuse) and misses
    # (run the extractor). We resolve hits into the same `results`
    # dict shape the async runner produces so the post-processing
    # loop below is uniform regardless of source.
    results: dict = {}
    cache_hits = 0
    cache_misses = 0
    suspects_to_run = []
    if use_cache and cache_mod is not None:
        for s in suspects:
            ckey = cache_mod.make_key(s["date"], s["time"], s["url"])
            if ckey is None:
                suspects_to_run.append(s)
                cache_misses += 1
                continue
            input_hash = cache_mod.hash_inputs(
                s["_input_low_price"],
                s["_input_verified_min"],
                s["_input_verified_max"],
            )
            entry = cache_entries.get(ckey)
            if cache_mod.is_hit(entry, input_hash, ttl_hours=cache_ttl_hours):
                # Reuse the cached chip result without running Playwright.
                results[s["key"]] = (
                    entry["chip_min"], entry["chip_max"],
                    entry["candidates"], entry["note"],
                )
                # Remember the source so the post-processing loop
                # writes the right verified_chip_source value.
                s["_cached_source"] = entry["source"]
                s["_cache_key"]     = ckey
                s["_input_hash"]    = input_hash
                cache_hits += 1
            else:
                s["_cache_key"]  = ckey
                s["_input_hash"] = input_hash
                suspects_to_run.append(s)
                cache_misses += 1
    else:
        # No cache → run everything.
        suspects_to_run = suspects
        cache_misses = len(suspects)

    log.info("Chip cache: %d hits, %d misses (running extractor on %d rows)",
             cache_hits, cache_misses, len(suspects_to_run))

    # Run the Playwright extractor on the cache-miss subset only.
    t0 = time.monotonic()
    if suspects_to_run:
        miss_results = asyncio.run(
            _chip_pass_async(suspects_to_run, workers=workers)
        )
        results.update(miss_results)
    elapsed = time.monotonic() - t0

    # Post-processing: write back to the payload and the cache.
    # Same loop walks both cache-hit rows (where results came from
    # the cache) and cache-miss rows (where results came from the
    # extractor), so the write logic is uniform.
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok = no_chips = fetch_failed = 0
    cache_writes = 0
    for s in suspects:
        chip_min, chip_max, candidates, note = results.get(
            s["key"], (None, None, [], "missed"),
        )
        s_idx, p_idx = s["key"]
        perf = payload["shows"][s_idx]["performances"][p_idx]
        # Determine the source label. Cache hits already know which
        # cacheable outcome they were; cache misses derive it from the
        # extractor output.
        cached_source = s.get("_cached_source")
        if cached_source is not None:
            source = cached_source
            if source == CHIP_SOURCE_OK:
                ok += 1
            elif source == CHIP_SOURCE_NO_CHIPS:
                no_chips += 1
            else:
                # Defensive: cache should never store fetch_failed, but
                # if a hand-edited cache or schema mismatch slipped one
                # through, treat as fetch_failed in stats.
                fetch_failed += 1
        else:
            if chip_min is not None:
                source = CHIP_SOURCE_OK
                ok += 1
            elif note.startswith("no chips"):
                source = CHIP_SOURCE_NO_CHIPS
                no_chips += 1
            else:
                source = CHIP_SOURCE_FETCH_FAIL
                fetch_failed += 1
        perf["verified_chip_min"]        = chip_min
        perf["verified_chip_max"]        = chip_max
        perf["verified_chip_candidates"] = candidates
        perf["verified_chip_source"]     = source
        perf["verified_chip_reason"]     = s["reason"]
        perf["verified_chip_note"]       = note
        perf["verified_chip_checked_at"] = now_iso

        # Persist freshly-extracted entries to the cache. Cache hits
        # don't need rewriting; only NEW successful extractions get
        # written, so the cache grows incrementally without churning
        # untouched rows. Fetch failures are deliberately not cached.
        if (use_cache and cache_mod is not None
                and cached_source is None  # i.e. this was a cache miss
                and source in cache_mod.CACHEABLE_SOURCES
                and s.get("_cache_key") is not None):
            cache_entries[s["_cache_key"]] = cache_mod.make_entry(
                chip_min=chip_min,
                chip_max=chip_max,
                candidates=candidates,
                source=source,
                reason=s["reason"],
                note=note,
                input_hash=s["_input_hash"],
            )
            cache_writes += 1

    if use_cache and cache_mod is not None and cache_writes > 0:
        cache_mod.save(cache_path, cache_entries)
        log.info("Chip cache: wrote %d new entries → %s",
                 cache_writes, cache_path)

    log.info(
        "Chip pass done in %.1fs — ok=%d, no_chips=%d, fetch_failed=%d"
        " (cache: %d hits / %d misses / %d writes)",
        elapsed, ok, no_chips, fetch_failed,
        cache_hits, cache_misses, cache_writes,
    )

    summary = {
        "verified_at":      now_iso,
        "suspect_count":    len(suspects),
        "ok":               ok,
        "no_chips":         no_chips,
        "fetch_failed":     fetch_failed,
        "duration_seconds": round(elapsed, 1),
        "by_reason":        by_reason,
        "cache_hits":       cache_hits,
        "cache_misses":     cache_misses,
        "cache_writes":     cache_writes,
    }
    report = payload.setdefault("report", {})
    if isinstance(report, dict):
        report["chip_verification"] = summary
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Verify SeatPlan per-performance prices by GETting each "
            "ticketing-page URL and parsing the inline fireCrmEvent payload."
        ),
    )
    p.add_argument(
        "--in", "-i", dest="in_path", type=Path,
        default=Path("seatplan_london.json"),
        help="Input JSON from seatplan_scraper.py (default: seatplan_london.json).",
    )
    p.add_argument(
        "--out", "-o", dest="out_path", type=Path, default=None,
        help="Output JSON path. Default: overwrite the input in place.",
    )
    p.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Parallel workers (default: {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Verify only the first N performances (smoke-test).",
    )
    p.add_argument(
        "--include-past", action="store_true",
        help="Also verify performances whose date is in the past "
             "(skipped by default since they typically 404 or redirect).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Do everything except write the output file.",
    )
    p.add_argument(
        "--skip-chips", action="store_true",
        help="Skip the chip-verification second pass "
             "(by default it runs over suspect performances after the "
             "main fireCrmEvent pass).",
    )
    p.add_argument(
        "--chip-workers", type=int, default=CHIP_WORKERS,
        help=f"Concurrent Playwright pages in the chip pass "
             f"(default: {CHIP_WORKERS}).",
    )
    p.add_argument(
        "--chip-cache", type=Path, default=None,
        help="Path to a JSON cache file for chip-pass results. When "
             "provided, suspect performances whose catalogue inputs "
             "haven't changed since the last run skip the Playwright "
             "extraction and reuse cached chip values. Cache entries "
             "expire after --chip-cache-ttl-hours (default 24). "
             "Omit this flag to run without caching.",
    )
    p.add_argument(
        "--chip-cache-ttl-hours", type=int, default=24,
        help="How long a cache entry stays valid (default: 24h). "
             "After expiry the entry is re-verified.",
    )
    p.add_argument(
        "--proxy-url",
        default=os.environ.get("OLT_PROXY_URL"),
        metavar="URL",
        help="If set, route all fireCrmEvent fetches through this "
             "proxy URL (a Cloudflare Worker forwarding to "
             "seatplan.com, authenticated via X-Proxy-Auth). The "
             "chip pass is unaffected — it uses Playwright and runs "
             "from the host's IP regardless. Defaults to "
             "$OLT_PROXY_URL.",
    )
    p.add_argument(
        "--proxy-token",
        default=os.environ.get("OLT_PROXY_TOKEN"),
        metavar="TOKEN",
        help="Shared secret for the proxy. Defaults to "
             "$OLT_PROXY_TOKEN. Must match the worker's bound "
             "PROXY_TOKEN secret.",
    )
    args = p.parse_args(argv)

    if args.proxy_url:
        log.info("Routing fireCrmEvent fetches via proxy: %s", args.proxy_url)
        if not args.proxy_token:
            log.warning("--proxy-url set but --proxy-token is empty — "
                        "the worker will reject the request with 401")

    if not args.in_path.exists():
        log.error("Input file %s not found", args.in_path)
        return EXIT_BAD_INPUT

    try:
        payload = json.loads(args.in_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("Could not load %s: %s", args.in_path, e)
        return EXIT_BAD_INPUT

    if not isinstance(payload, dict) or not isinstance(payload.get("shows"), list):
        log.error("Input %s does not look like seatplan scraper output "
                  "(expected top-level dict with 'shows' list)", args.in_path)
        return EXIT_BAD_INPUT

    summary = run(
        payload,
        concurrency=args.concurrency,
        limit=args.limit,
        include_past=args.include_past,
        proxy_url=args.proxy_url,
        proxy_token=args.proxy_token,
    )

    # Second pass: chip re-verification for suspect performances.
    if not args.skip_chips:
        try:
            run_chip_pass(
                payload,
                workers=args.chip_workers,
                cache_path=args.chip_cache,
                cache_ttl_hours=args.chip_cache_ttl_hours,
            )
        except Exception as e:
            # Chip pass is best-effort — never block the main verifier's
            # output. Failures here just mean the suspect rows keep
            # their fireCrmEvent values and dedupe falls back accordingly.
            log.error("Chip pass failed: %s", e)
    else:
        log.info("Chip pass skipped (--skip-chips)")

    if args.dry_run:
        log.info("--dry-run: not writing output")
        return EXIT_CLEAN

    out_path = args.out_path or args.in_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    tmp.replace(out_path)
    log.info("Wrote %s", out_path)

    # Hard signal that the URL pattern (or upstream layout) has drifted:
    # we attempted real verifications but none came back with a price.
    attempted_real = summary["total_checked"] - summary["skipped"]
    if attempted_real > 0 and summary["ok"] == 0:
        log.error(
            "No performances verified successfully out of %d attempts — "
            "possible ticketing-page URL pattern drift or fireCrmEvent removal",
            attempted_real,
        )
        return EXIT_DRIFT
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
