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
import random
import re
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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

# Throttle knobs. SeatPlan sits behind AWS WAF, which serves a JavaScript
# challenge (HTTP 202 + gokuProps) that our requests-based fetcher can't solve.
# IMPORTANT, learned the hard way: the clean window is a brief WALL-CLOCK grace
# (~20-40s from the start of the pass), NOT a request budget. Bursting fills
# that grace with ~400-600 requests; pacing wastes it (1.2 req/s got only ~46
# through). So pacing is OFF by default — we burst to capture the most per run,
# shuffle the order so a different slice lands in the grace each run, and lean
# on the price cache for the rest. --max-rate is retained only for experiments.
DEFAULT_MAX_RATE = 0.0          # requests/second; <=0 disables pacing (default)
DEFAULT_MAX_SECONDS = 0.0       # wall-clock budget; <=0 disables (CI sets this)

# Persistent verified-price cache. With pacing the full pass takes ~14 min; if
# a wall-clock budget cuts it short (or a stray 202 slips through), the perfs
# we didn't freshly verify carry their last real price forward instead of
# reverting to SeatPlan's catalogue lowPrice (a stale marketing headline).
PRICE_CACHE_VERSION = 1
DEFAULT_PRICE_CACHE_TTL_HOURS = 48
_PRICE_FIELDS = (
    "verified_min_price", "verified_max_price", "verified_price",
    "verified_is_discounted", "verified_is_no_booking_fee",
    "verified_price_source", "verified_status", "verified_url",
    "verified_checked_at",
)

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

# ---------------------------------------------------------------------------
# Browser-fetch config (fireCrmEvent pass, fetch-mode "browser")
# ---------------------------------------------------------------------------
# The fireCrmEvent pass has two fetch modes:
#
#   "requests" — a ThreadPool of requests.get() calls, optionally via the
#                proxy. Fast, but SeatPlan's AWS WAF serves a JavaScript
#                challenge (HTTP 202 + window.gokuProps) once an egress IP
#                bursts past its grace, and a plain HTTP client can't solve
#                JS — so only the first slice of each run verifies. Paired
#                with --price-cache + --shuffle, coverage rolls to full over
#                several runs (last-known prices carry forward in between).
#
#   "browser"  — fetch EVERY ticketing URL through one headless-Chromium
#                context that has solved the WAF challenge. A single warm-up
#                navigation lets Chromium run the AWS WAF challenge JS (the
#                very thing the chip pass relies on every run) and bank an
#                aws-waf-token cookie; subsequent *in-page* fetch() calls run
#                on the browser's real network stack, so they carry that token
#                and clear the WAF. Result: all performances verify each run,
#                direct from this host's IP — the proxy isn't used. If the
#                token's immunity window lapses mid-sweep (a 202 reappears),
#                we re-navigate to re-solve and retry the affected URLs.
#
# Why in-page fetch() and not Playwright's APIRequestContext or a cookie
# lifted into `requests`: AWS WAF tokens can be bound to the client that
# solved them (TLS/fingerprint + cookie), so the robust path is to fetch with
# the exact browser identity that earned the token — i.e. from inside the page.
BROWSER_MAX_CONC        = 12        # in-page fetch concurrency (JS promise pool)
BROWSER_CHUNK           = 120       # URLs handed to each page.evaluate() round-trip
BROWSER_NAV_TIMEOUT_MS  = 30_000
BROWSER_RESOLVE_SLEEP_S = 5.0       # let the challenge JS solve + set the cookie
BROWSER_MAX_ATTEMPTS    = 2         # per-URL tries (one retry after a re-solve)
BROWSER_MAX_EVALS       = 60        # hard safety cap on evaluate() round-trips

# Serial fetch pacing (fetch-mode "browser"). The pool model above bursts one
# WAF token across many concurrent in-page fetches; the token's short post-
# solve window then collapses and every later request 202s. seatplan_scraper.py
# never hits this because it fetches ONE URL at a time and re-solves on each
# 202. We mirror that: serial fetches paced near the scraper's proven ~1 req/s,
# re-solving the token on every challenge and retrying the same URL.
BROWSER_REQUEST_INTERVAL_S = 0.9    # min gap between serial fetches. Tested 0.2s:
                                    # total throughput rose (~479 vs ~440 fresh)
                                    # but the 202 challenge rate TREBLED (the WAF
                                    # has a request-RATE component, not just a time
                                    # window), and the extra failures fell on the
                                    # near-term performances that matter most
                                    # (first-100 freshness 90% -> 81%). 0.9 keeps the
                                    # soonest set freshest and is gentler on the WAF.
BROWSER_REFUSAL_STREAK     = 12     # consecutive unrecoverable 202s => the WAF
                                    # refused this runner IP; stop and let the
                                    # price cache carry the remainder

# Parallel WAF tokens. Each worker opens its OWN browser context (own cookie jar
# => own aws-waf-token) and fetches at the gentle per-token rate above. The pool
# model failed because it burst ONE token; this bursts N *independent* tokens, so
# a single token never sees more than ~1 req/s. HYPOTHESIS: the WAF budget is per
# token (the old pool cleared ~120 fetches on one token before the wall, which a
# strict per-IP rate limit wouldn't allow) — if so, N tokens ~= N× throughput at
# the same 4%-failure per-token rate, and the whole catalogue finishes inside the
# budget. If the limit turns out to be per-IP instead, the 202 rate spikes like
# the 0.2s test did — set BROWSER_WORKERS = 1 to revert to the proven serial pass.
BROWSER_WORKERS            = 3      # independent WAF-solved contexts run in parallel

# In-page fetch pool. Single arg [items, conc] where items = [{idx, url}].
# Returns [{idx, status, fire, challenge}] — `fire` is the matched
# fireCrmEvent('Viewed Performance', {...}) block substring (parsed in Python
# by parse_fire_crm, the single source of truth), and `challenge` flags an AWS
# WAF interstitial body. Returning only the block substring (not the whole
# ~30 KB page) keeps the CDP bridge light across ~1,100 URLs.
_BROWSER_POOL_JS = r"""
async ([items, conc]) => {
  const out = [];
  let i = 0;
  const reBlock = /fireCrmEvent\(\s*['"]Viewed Performance['"]\s*,\s*\{[^}]*\}/;
  async function worker() {
    while (i < items.length) {
      const it = items[i++];
      try {
        const r = await fetch(it.url, {credentials: 'include',
                                       headers: {'Accept': 'text/html'}});
        const t = await r.text();
        const m = t.match(reBlock);
        out.push({idx: it.idx, status: r.status, fire: m ? m[0] : "",
                  challenge: t.indexOf('gokuProps') !== -1});
      } catch (e) {
        out.push({idx: it.idx, status: -1, fire: "", challenge: false});
      }
    }
  }
  await Promise.all(Array.from({length: conc}, () => worker()));
  return out;
}
"""

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


# --- HTTP diagnostics (temporary instrumentation — safe to remove later) ----
# Same purpose as in seatplan_scraper.py: on a non-200 from seatplan.com, dump
# the body + key headers so we can tell WHO is blocking the egress —
#   * CloudFront / SeatPlan block (server: cloudflare? no — CloudFront; body
#     "Request blocked", a CloudFront request ID, or a 429): SeatPlan is
#     rate-limiting the Lambda's egress IP.
#   * {"Reason":"ConcurrentInvocationLimitExceeded",...} with content-type
#     application/json: it's the Lambda being throttled, not SeatPlan.
# Capped across worker threads so a sustained block can't flood the log.
_DIAG_HEADERS = (
    "server", "cf-ray", "cf-mitigated", "cf-cache-status",
    "content-type", "content-length", "retry-after",
    "x-proxy-error", "x-worker-error", "via",
)
_DIAG_MAX = 4
_diag_lock = Lock()
_diag_count = 0


def _diag_take() -> bool:
    global _diag_count
    with _diag_lock:
        if _diag_count >= _DIAG_MAX:
            return False
        _diag_count += 1
        return True


def _log_http_diagnostic(resp, *, context, head=900, tail=400):
    status = getattr(resp, "status_code", "?")
    hdrs = []
    try:
        for h in _DIAG_HEADERS:
            v = resp.headers.get(h)
            if v is not None:
                hdrs.append(f"{h}: {v}")
    except Exception:
        pass
    hdr_str = "; ".join(hdrs) if hdrs else "(no diagnostic headers present)"
    try:
        body = resp.text or ""
    except Exception as ex:
        body = f"(could not read body: {type(ex).__name__}: {ex})"
    collapsed = " ".join(body.split())
    n = len(collapsed)
    excerpt = collapsed if n <= head + tail else (
        f"{collapsed[:head]} …[+{n - head - tail} chars]… {collapsed[-tail:]}"
    )
    log.error("HTTP DIAGNOSTIC [%s]: status=%s body_len=%d", context, status, n)
    log.error("HTTP DIAGNOSTIC [%s]: headers=[%s]", context, hdr_str)
    log.error("HTTP DIAGNOSTIC [%s]: body=%s", context, excerpt)


# Failure-reason tally — populated across worker threads, logged once at the
# end of the run. This is the diagnostic that cannot miss: whatever the 610
# failures actually are (an HTTP status from SeatPlan/CloudFront, or a
# network-level exception like ConnectionError / ReadTimeout from the Lambda
# being throttled or dropping the connection), they show up here bucketed.
_fail_tally: dict[str, int] = {}
_fail_lock = Lock()


def _tally_fail(reason: str) -> None:
    with _fail_lock:
        _fail_tally[reason] = _fail_tally.get(reason, 0) + 1


def _result_from_html(url: str | None, status_code, html: str) -> dict:
    """Build the verified-price dict from one fetched ticketing page.

    Shared by both fetch modes (the requests `verify_one` and the browser
    pass), so the parse→result mapping — and therefore the downstream
    price-cache merge — is identical no matter how the HTML was obtained.
    `status_code` is a real HTTP code (or a sentinel like -1); anything other
    than 200 is treated as a fetch failure and tallied. Never raises.
    """
    if status_code != 200:
        _tally_fail(f"HTTP {status_code}")
        return _empty_result(url, SOURCE_FETCH_FAIL, status=status_code)

    parsed = parse_fire_crm(html)
    if parsed is None:
        # Page loaded but no Viewed-Performance tracking call. Most
        # commonly: sold-out redirect, off-sale page, or layout change.
        # Treat as no-seats; price_source surfaces the distinction.
        return _empty_result(url, SOURCE_NO_SEATS, status=status_code)

    result = _empty_result(url, SOURCE_OK, status=status_code)
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


def verify_one(
    session: requests.Session,
    url: str | None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Fetch one ticketing page (requests mode) and return the verified-price
    dict. Never raises — failures surface in verified_price_source/status.
    """
    if not url:
        return _empty_result(url, SOURCE_SKIPPED)

    try:
        r = session.get(url, timeout=timeout_s)
    except requests.RequestException as e:
        # Network-level failure (no HTTP status). Bucket by exception class and
        # capture the first few full repr strings so we can see exactly what
        # the connection did (reset, read timeout, etc.).
        _tally_fail(f"EXC {type(e).__name__}")
        if _diag_take():
            log.error("HTTP DIAGNOSTIC [fireCrmEvent GET %s]: exception %r", url, e)
        return _empty_result(url, SOURCE_FETCH_FAIL, status=str(e)[:160])

    # Capture the body/headers on ANY non-200 (capped) before delegating to the
    # shared result builder, which does the tally + result construction.
    if r.status_code != 200 and _diag_take():
        _log_http_diagnostic(r, context=f"fireCrmEvent GET {url}")

    return _result_from_html(url, r.status_code, r.text)


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


class _RateLimiter:
    """Thread-safe global pacer. Hands out request slots no closer together
    than `interval` seconds, so the combined rate across all worker threads
    stays at or below `rate_per_s`. interval<=0 disables pacing.
    """

    def __init__(self, rate_per_s: float) -> None:
        self._interval = (1.0 / rate_per_s) if rate_per_s and rate_per_s > 0 else 0.0
        self._lock = Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = self._next if self._next > now else now
            self._next = start + self._interval
            delay = start - now
        if delay > 0:
            time.sleep(delay)


def _load_price_cache(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
        return raw["entries"]
    return {}


def _save_price_cache(path: Path | None, entries: dict) -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(
            json.dumps({"version": PRICE_CACHE_VERSION, "entries": entries},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as e:
        log.warning("could not write price cache %s: %s", path, e)


def _price_entry_fresh(entry: dict, now: datetime, ttl: timedelta) -> bool:
    ts = entry.get("verified_checked_at")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt) <= ttl


# ---------------------------------------------------------------------------
# Browser fetch pass (fireCrmEvent pass, fetch-mode "browser")
# ---------------------------------------------------------------------------

def _apply_result(payload: dict, si: int, pi: int, out: dict,
                  counts: dict, counts_lock: Lock,
                  progress: dict, progress_lock: Lock, total: int) -> None:
    """Write one verified result into payload and update the shared counters —
    the same bookkeeping the requests ThreadPool loop does, factored out so the
    browser pass yields identical counts/progress and the downstream
    price-cache merge + summary are reached unchanged."""
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
                counts[SOURCE_OK], counts[SOURCE_NO_SEATS],
                counts[SOURCE_FETCH_FAIL],
            )


async def _browser_fetch_async(
    tasks: list[tuple[int, int, str | None]], payload: dict, *,
    concurrency: int, max_seconds: float,
    counts: dict, counts_lock: Lock, progress: dict, progress_lock: Lock,
    budget_skipped: dict, total: int,
) -> bool:
    """Verify every task by fetching its ticketing URL through one
    WAF-solved headless-Chromium context (in-page fetch). Writes results into
    payload exactly like the requests loop, so run()'s cache merge is shared.

    Returns True if the browser handled the work, or False if it's unusable
    this run (Chromium launch failed, or the runner IP is hard-blocked at the
    WAF edge) — in which case NOTHING is written and the caller falls back to
    the requests-via-proxy path over the full task list."""
    from playwright.async_api import async_playwright

    # Separate fetchable tasks from url==None (instant SKIPPED). Defer applying
    # the SKIPPED rows until we know the browser is viable — if the runner IP
    # is hard-blocked we write NOTHING and signal a fallback, so the caller can
    # run the requests path over the full task list without double-counting.
    none_tasks = [(si, pi) for (si, pi, url) in tasks if not url]
    usable: list[dict] = [
        {"si": si, "pi": pi, "url": url, "attempt": 1}
        for (si, pi, url) in tasks if url
    ]

    def _apply_skipped() -> None:
        for (si, pi) in none_tasks:
            _apply_result(payload, si, pi, _empty_result(None, SOURCE_SKIPPED),
                          counts, counts_lock, progress, progress_lock, total)

    if not usable:
        _apply_skipped()
        return True

    n_workers = max(1, min(BROWSER_WORKERS, len(usable)))
    warm_url = usable[0]["url"]
    loop = asyncio.get_running_loop()
    deadline = (loop.time() + max_seconds) if max_seconds and max_seconds > 0 else None

    # Soonest-first: verify the nearest performances first (the cheapest-available
    # floor is almost always near-term), so the user-visible price stays fresh
    # every run even when the budget can't reach the whole catalogue.
    def _perf_when(it: dict) -> tuple:
        perf = payload["shows"][it["si"]]["performances"][it["pi"]]
        return (perf.get("date") or "9999-12-31", perf.get("time") or "99:99")
    usable.sort(key=_perf_when)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            # Chromium unavailable — signal a fallback to the requests path
            # (write nothing here so the caller can process all tasks).
            log.error("browser pass: could not launch Chromium — %s", e)
            log.error("  run: python -m playwright install chromium")
            return False

        async def _new_solved_page():
            """Open a fresh context (its OWN cookie jar => its OWN aws-waf-token)
            and a page, plus a solve() that navigates a ticketing page so Chromium
            runs the WAF challenge JS and banks that context's token. Each worker
            owns one of these, so the workers hold INDEPENDENT tokens — the whole
            point of the pool."""
            ctx = await browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent=CHIP_UA, locale="en-GB", timezone_id="Europe/London",
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            )
            await ctx.route("**/*", _chip_block_heavy)
            await ctx.add_init_script(CHIP_STEALTH_JS)
            pg = await ctx.new_page()

            async def solve() -> None:
                try:
                    await pg.goto(warm_url, wait_until="domcontentloaded",
                                  timeout=BROWSER_NAV_TIMEOUT_MS)
                except Exception as e:
                    log.warning("browser pass: token nav error — %s",
                                type(e).__name__)
                await asyncio.sleep(BROWSER_RESOLVE_SLEEP_S)

            return ctx, pg, solve

        # --- Viability probe (one throwaway context) -------------------------
        # The browser can solve a *challenge* (202) but not an IP *block*. If the
        # warm-up fetch is 403/-1 the runner IP is hard-blocked: write NOTHING and
        # signal a requests-fallback so the caller handles the full task list.
        probe_ctx, probe_pg, probe_solve = await _new_solved_page()
        await probe_solve()

        async def _probe_status() -> int:
            try:
                return await probe_pg.evaluate(
                    """async (u) => { try {
                         const r = await fetch(u, {credentials:'include',
                                                   headers:{'Accept':'text/html'}});
                         return r.status;
                       } catch (e) { return -1; } }""",
                    warm_url,
                )
            except Exception:
                return -1

        probe = await _probe_status()
        if probe in (403, -1):
            await probe_solve()
            probe = await _probe_status()
        try:
            names = sorted({c["name"] for c in await probe_ctx.cookies()})
            log.info("browser pass: token primed; warm-up probe HTTP %s; "
                     "context cookies: %s", probe, names or "(none)")
        except Exception:
            pass
        try:
            await probe_ctx.close()
        except Exception:
            pass
        if probe in (403, -1):
            log.warning("browser pass: runner IP appears hard-blocked at the WAF "
                        "(warm-up probe HTTP %s) — signalling requests fallback",
                        probe)
            try:
                await browser.close()
            except Exception:
                pass
            return False

        # Browser is viable → apply the deferred SKIPPED rows, then run the pool.
        _apply_skipped()

        # Shared soonest-first work queue + a couple of shared flags. Everything
        # below runs in ONE event loop, so plain ints/bools are race-free across
        # workers (there is no await between reading and writing a shared field).
        queue: deque = deque(usable)
        state = {"challenge_logged": False, "budget_hit": False}
        log.info("browser pass: %d worker(s), each with its own WAF token, "
                 "soonest-first over %d performance(s)", n_workers, len(usable))

        async def worker(wid: int) -> None:
            try:
                ctx, pg, solve = await _new_solved_page()
            except Exception as e:
                log.warning("browser pass: worker %d could not start — %s",
                            wid, type(e).__name__)
                return
            await solve()                       # mint this worker's own token
            fail_streak = 0
            try:
                while queue:
                    if deadline is not None and loop.time() >= deadline:
                        state["budget_hit"] = True
                        break
                    try:
                        it = queue.popleft()
                    except IndexError:
                        break
                    # Per-item fetch with re-solve-on-202 + retry, on THIS worker's
                    # own token/page — isolated from the other workers' tokens.
                    while True:
                        try:
                            out = await pg.evaluate(
                                _BROWSER_POOL_JS,
                                [[{"idx": 0, "url": it["url"]}], 1])
                            r = (out or [{}])[0] or {}
                        except Exception:
                            r = {"status": -1, "fire": "", "challenge": False}
                        status = r.get("status", -1)
                        fire = r.get("fire") or ""
                        is_challenge = bool(r.get("challenge")) or status == 202
                        is_transient = status == -1  # fetch threw; not a verdict
                        if is_challenge or is_transient:
                            if is_challenge and not state["challenge_logged"]:
                                log.info("browser pass: WAF challenge seen (HTTP "
                                         "202) — re-solving per worker on each hit")
                                state["challenge_logged"] = True
                            if it["attempt"] < BROWSER_MAX_ATTEMPTS:
                                # Token died — re-solve THIS context and retry the
                                # same URL on the fresh token.
                                it["attempt"] += 1
                                await solve()
                                await asyncio.sleep(BROWSER_REQUEST_INTERVAL_S)
                                continue
                            _tally_fail("HTTP 202" if is_challenge else "HTTP -1")
                            _apply_result(payload, it["si"], it["pi"],
                                          _empty_result(it["url"], SOURCE_FETCH_FAIL,
                                                        status=(202 if is_challenge
                                                                else -1)),
                                          counts, counts_lock, progress,
                                          progress_lock, total)
                            fail_streak += 1
                            break
                        # Clean response — build the result the shared way.
                        _apply_result(payload, it["si"], it["pi"],
                                      _result_from_html(it["url"], status, fire),
                                      counts, counts_lock, progress, progress_lock,
                                      total)
                        fail_streak = 0
                        break
                    if fail_streak >= BROWSER_REFUSAL_STREAK:
                        log.warning("browser pass: worker %d hit %d consecutive "
                                    "challenges despite re-solving — stopping it "
                                    "(remaining work falls to other workers / "
                                    "cache)", wid, fail_streak)
                        break
                    await asyncio.sleep(BROWSER_REQUEST_INTERVAL_S)
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass

        await asyncio.gather(*(worker(w) for w in range(n_workers)))

        # Anything still queued (budget reached, or every worker bailed on a
        # refusal) carries forward from the price cache, exactly as before.
        n_left = 0
        while queue:
            it = queue.popleft()
            with progress_lock:
                budget_skipped["n"] += 1
            _apply_result(payload, it["si"], it["pi"],
                          _empty_result(it["url"], SOURCE_SKIPPED,
                                        status=("budget" if state["budget_hit"]
                                                else "refused")),
                          counts, counts_lock, progress, progress_lock, total)
            n_left += 1
        if n_left:
            if state["budget_hit"]:
                log.info("browser pass: wall-clock budget reached — %d "
                         "performance(s) carried from price cache", n_left)
            else:
                log.warning("browser pass: %d performance(s) carried from cache "
                            "(workers stopped early)", n_left)

        try:
            await browser.close()
        except Exception:
            pass
    return True


def _run_browser_fetch(tasks: list[tuple[int, int, str | None]],
                       payload: dict, **kw) -> bool:
    """Sync entry point for the browser pass.

    Returns True if the browser handled the work (results written; the
    price-cache carry-forward covers anything it couldn't fetch), or False if
    the browser is unusable this run (Chromium launch failed, or the runner IP
    is hard-blocked at the WAF) and the caller should fall back to the
    requests-via-proxy path. A mid-sweep crash still returns True — partial
    results plus cache carry-forward are never worse than the requests path."""
    try:
        return bool(asyncio.run(_browser_fetch_async(tasks, payload, **kw)))
    except Exception as e:  # noqa: BLE001 — defend the whole pipeline
        log.error("browser pass crashed (%s) — marking unfetched perfs as "
                  "fetch_failed so cached prices carry forward", e)
        counts, counts_lock = kw["counts"], kw["counts_lock"]
        progress, progress_lock = kw["progress"], kw["progress_lock"]
        total = kw["total"]
        for (si, pi, url) in tasks:
            perf = payload["shows"][si]["performances"][pi]
            if perf.get("verified_price_source"):
                continue  # already recorded before the crash
            if url:
                _tally_fail("EXC BrowserPass")
                out = _empty_result(url, SOURCE_FETCH_FAIL, status="crash")
            else:
                out = _empty_result(url, SOURCE_SKIPPED)
            _apply_result(payload, si, pi, out, counts, counts_lock,
                          progress, progress_lock, total)
        return True


def _run_requests_fetch(tasks: list[tuple[int, int, str | None]], payload: dict, *,
                        concurrency: int, proxy_url: str | None,
                        proxy_token: str | None, max_rate: float,
                        max_seconds: float, t0: float, counts: dict,
                        counts_lock: Lock, progress: dict, progress_lock: Lock,
                        budget_skipped: dict, total: int) -> None:
    """The requests-based fireCrmEvent fetch loop — a ThreadPool of HTTP gets,
    optionally via the proxy. Used both as the primary path in --fetch-mode
    requests and as the fallback when the browser pass can't run (runner IP
    blocked / Chromium unavailable). Writes results via _apply_result so the
    downstream cache merge is identical to the browser path."""
    session = build_session(
        pool_size=max(concurrency, 8),
        proxy_url=proxy_url, proxy_token=proxy_token,
    )
    limiter = _RateLimiter(max_rate)
    deadline = (t0 + max_seconds) if max_seconds and max_seconds > 0 else None
    if max_rate and max_rate > 0:
        log.info(
            "Pacing at <=%.2f req/s%s", max_rate,
            f"; wall-clock budget {max_seconds:.0f}s" if deadline else "",
        )

    def _job(task: tuple[int, int, str | None]) -> tuple[int, int, dict]:
        si, pi, url = task
        if not url:
            # Unusable date/time — instant SKIPPED, no fetch, no pacing.
            return si, pi, verify_one(session, url)
        if deadline is not None and time.monotonic() >= deadline:
            # Out of wall-clock budget: short-circuit without fetching. The
            # price-cache merge carries this perf's last real price.
            with progress_lock:
                budget_skipped["n"] += 1
            return si, pi, _empty_result(url, SOURCE_SKIPPED, status="budget")
        limiter.wait()
        return si, pi, verify_one(session, url)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_job, t) for t in tasks]
        for fut in as_completed(futures):
            try:
                si, pi, out = fut.result()
            except Exception as e:  # noqa: BLE001 — worker shouldn't raise, but defend
                log.warning("worker exception: %s", e)
                continue
            _apply_result(payload, si, pi, out, counts, counts_lock,
                          progress, progress_lock, total)


def run(
    payload: dict,
    *,
    concurrency: int,
    limit: int | None,
    include_past: bool,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
    max_rate: float = DEFAULT_MAX_RATE,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    price_cache_path: Path | None = None,
    price_cache_ttl_hours: int = DEFAULT_PRICE_CACHE_TTL_HOURS,
    shuffle: bool = False,
    fetch_mode: str = "requests",
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
    if shuffle:
        # The WAF grace window covers only the first chunk of requests by
        # wall-clock; randomising submission order spreads which performances
        # land inside it, so successive runs refresh different slices and the
        # catalogue rolls to full coverage (combined with the price cache).
        random.shuffle(tasks)
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

    t0 = time.monotonic()
    budget_skipped = {"n": 0}

    handled_by_browser = False
    if fetch_mode == "browser":
        log.info(
            "Fetch mode: browser — one WAF-solved Chromium context, in-page "
            "fetch over all %d performance(s) (direct from host; proxy unused)",
            total,
        )
        handled_by_browser = _run_browser_fetch(
            tasks, payload,
            concurrency=concurrency, max_seconds=max_seconds,
            counts=counts, counts_lock=counts_lock,
            progress=progress, progress_lock=progress_lock,
            budget_skipped=budget_skipped, total=total,
        )
        if not handled_by_browser:
            # Runner IP hard-blocked at the WAF (or Chromium unavailable).
            # Nothing was written; fall back to the requests-via-proxy burst.
            # That traffic exits via the Lambda's clean IP — it can't solve the
            # WAF *challenge*, so only the grace-window slice verifies, but
            # that's a few hundred fresh prices and the cache carries the rest.
            # Shuffle so a different slice lands in the grace window each run.
            log.warning("browser pass unavailable this run — falling back to "
                        "requests via proxy (grace-window slice + price cache)")
            random.shuffle(tasks)

    if fetch_mode != "browser" or not handled_by_browser:
        _run_requests_fetch(
            tasks, payload,
            concurrency=concurrency, proxy_url=proxy_url, proxy_token=proxy_token,
            max_rate=max_rate, max_seconds=max_seconds, t0=t0,
            counts=counts, counts_lock=counts_lock,
            progress=progress, progress_lock=progress_lock,
            budget_skipped=budget_skipped, total=total,
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
    if _fail_tally:
        breakdown = ", ".join(
            f"{reason}×{n}"
            for reason, n in sorted(_fail_tally.items(), key=lambda kv: -kv[1])
        )
        log.info("fetch_failed breakdown: %s", breakdown)
    if budget_skipped["n"]:
        log.info(
            "Budget reached — %d performance(s) not fetched this run "
            "(carried forward from price cache where available)",
            budget_skipped["n"],
        )

    # --- Price-cache merge ------------------------------------------------
    # For perfs verified OK this run, refresh the cache. For perfs we could
    # NOT fetch (fetch_failed, or budget-skipped), overlay the last real
    # price from the cache so they don't fall back to SeatPlan's catalogue
    # lowPrice. no_seats is left untouched (respect a genuine sold-out), and
    # its stale entry is allowed to age out. Entries are pruned to URLs seen
    # this run and to within the TTL, which bounds the file size.
    carried = fresh = 0
    if price_cache_path:
        prev = _load_price_cache(price_cache_path)
        now = datetime.now(timezone.utc)
        ttl = timedelta(hours=price_cache_ttl_hours)
        new_entries: dict = {}
        for si, pi, url in tasks:
            if not url:
                continue
            perf = payload["shows"][si]["performances"][pi]
            src = perf.get("verified_price_source")
            if src == SOURCE_OK:
                new_entries[url] = {k: perf.get(k) for k in _PRICE_FIELDS}
                fresh += 1
            elif src in (SOURCE_FETCH_FAIL, SOURCE_SKIPPED):
                entry = prev.get(url)
                if entry and _price_entry_fresh(entry, now, ttl):
                    for k in _PRICE_FIELDS:
                        if k in entry:
                            perf[k] = entry[k]
                    perf["verified_price_cached"] = True
                    new_entries[url] = entry
                    carried += 1
        _save_price_cache(price_cache_path, new_entries)
        log.info(
            "Price cache: %d fresh, %d carried-forward, %d entries saved",
            fresh, carried, len(new_entries),
        )

    summary = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_checked": total,
        "ok": counts[SOURCE_OK],
        "no_seats": counts[SOURCE_NO_SEATS],
        "fetch_failed": counts[SOURCE_FETCH_FAIL],
        "skipped": counts[SOURCE_SKIPPED],
        "carried_forward": carried,
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
  const t = (document.body && document.body.innerText) || '';
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
        except Exception:
            # Continue anyway — the scan below reports no-price if needed. This
            # catches the predicate *throwing* (e.g. document.body momentarily
            # null during a WAF-challenge reload), not just PWTimeout; either
            # way we fall through to the stability poll.
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
            try:
                chip_min, chip_max, candidates, note = await _chip_extract_one(
                    context, item["url"]
                )
            except Exception as e:  # noqa: BLE001 — one bad row must not kill the pass
                chip_min = chip_max = None
                candidates, note = [], f"chip error — {type(e).__name__}: {e}"
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
        # return_exceptions=True so a single worker failing can't cancel its
        # siblings mid-flight — that cascade is what produced the wall of
        # TargetClosedError tracebacks.
        await asyncio.gather(*tasks, return_exceptions=True)
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
    p.add_argument(
        "--max-rate", type=float, default=DEFAULT_MAX_RATE, metavar="RPS",
        help=f"Cap the fireCrmEvent pass at this many requests/second "
             f"across all workers (default: {DEFAULT_MAX_RATE}). SeatPlan's "
             f"AWS WAF challenges an egress IP that bursts too fast; pacing "
             f"below the threshold keeps every request answered. Set <=0 to "
             f"disable pacing.",
    )
    p.add_argument(
        "--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, metavar="SECS",
        help="Wall-clock budget for the fireCrmEvent pass. Once exceeded, "
             "remaining performances are skipped without fetching (and keep "
             "their cached price). Use this in CI to guarantee the job "
             "finishes before the next scheduled run. Default 0 = no budget.",
    )
    p.add_argument(
        "--price-cache", type=Path, default=None,
        help="Path to a JSON cache of last-known verified prices. When set, "
             "performances that couldn't be fetched this run (budget-skipped "
             "or WAF-challenged) carry their last real price forward instead "
             "of reverting to SeatPlan's catalogue lowPrice. Omit to disable.",
    )
    p.add_argument(
        "--price-cache-ttl-hours", type=int,
        default=DEFAULT_PRICE_CACHE_TTL_HOURS,
        help=f"How long a cached price stays usable for carry-forward "
             f"(default: {DEFAULT_PRICE_CACHE_TTL_HOURS}h). Older entries are "
             f"dropped rather than shown.",
    )
    p.add_argument(
        "--shuffle", action="store_true",
        help="Randomise the order performances are fetched. SeatPlan's AWS "
             "WAF only lets a brief burst through before challenging, so "
             "shuffling spreads which performances land in that window — "
             "successive runs refresh different slices and (with --price-cache) "
             "the catalogue rolls to full coverage over a few runs. "
             "(requests mode only; browser mode verifies everything regardless.)",
    )
    p.add_argument(
        "--fetch-mode", choices=("requests", "browser"), default="requests",
        help="How to fetch ticketing pages for the fireCrmEvent pass. "
             "'requests' (default): a ThreadPool of HTTP gets, optionally via "
             "--proxy-url; SeatPlan's AWS WAF JS-challenges the egress after a "
             "burst (HTTP 202), so only a slice verifies per run — pair with "
             "--price-cache / --shuffle. 'browser': fetch every URL through one "
             "WAF-solved headless-Chromium context (in-page fetch carries the "
             "aws-waf-token), verifying ALL performances each run, direct from "
             "this host — the proxy is not used.",
    )
    args = p.parse_args(argv)

    if args.fetch_mode == "browser":
        log.info("fireCrmEvent pass: browser fetch mode "
                 "(WAF-solved Chromium; proxy not used)")
    elif args.proxy_url:
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
        max_rate=args.max_rate,
        max_seconds=args.max_seconds,
        price_cache_path=args.price_cache,
        price_cache_ttl_hours=args.price_cache_ttl_hours,
        shuffle=args.shuffle,
        fetch_mode=args.fetch_mode,
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

    # Zero successful verifications can mean two very different things:
    #   (a) genuine drift — the ticketing URL pattern moved or fireCrmEvent was
    #       removed, so reachable pages parse to nothing (404 / no_seats); or
    #   (b) an infrastructure block — the WAF returned 403/202 (or the in-page
    #       fetch threw, -1) for everything, which is transient and runner-IP
    #       dependent. In case (b) the price cache has already carried last-known
    #       prices forward, so the site is fine and this is NOT drift.
    # Only (a) should raise the drift signal; (b) exits clean.
    attempted_real = summary["total_checked"] - summary["skipped"]
    if attempted_real > 0 and summary["ok"] == 0:
        fails = dict(_fail_tally)
        infra = sum(
            v for k, v in fails.items()
            if k in ("HTTP 403", "HTTP 202", "HTTP 429", "HTTP -1",
                     "eval-cap", "chromium-launch", "crash")
            or k.startswith("EXC ")
        )
        content = sum(fails.values()) - infra
        if infra >= max(content, 1):
            breakdown = ", ".join(
                f"{k}×{v}" for k, v in sorted(fails.items(), key=lambda kv: -kv[1])
            ) or "n/a"
            log.warning(
                "No fresh verifications this run — infrastructure block (%s); "
                "served prices carried from cache. Not treating as drift.",
                breakdown,
            )
            return EXIT_CLEAN
        log.error(
            "No performances verified successfully out of %d attempts — "
            "possible ticketing-page URL pattern drift or fireCrmEvent removal",
            attempted_real,
        )
        return EXIT_DRIFT
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
