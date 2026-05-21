"""
TTD seat-plan verifier (third pass)
====================================

Third pass for the TTD pipeline. Where ``ttd_availability.py`` confirms
what TTD's calendar widget *displays*, this script confirms what the
actual seat-plan page *offers* — the ground truth of bookable price
tiers, as a buyer would see them.

Why this exists
---------------
TTD's listing card, detail-page JSON-LD, and ``/show/bindcalendar`` AJAX
response all share the same database-driven "from £X" minimum. When
that minimum includes a tier that isn't actually configured in the
seat-plan engine — observed pattern: every King's Head Theatre show
advertises "from £13" on all three of those surfaces, but the actual
seat-plan page only has £25 / £31 / £40 tiers — the three upstream
surfaces lie consistently. ``ttd_availability.py`` parses the calendar
widget's ``<span class="price-from">`` and so faithfully reports the
same £13. It cannot catch the disagreement because it never sees the
seat plan.

The seat-plan page at::

    /shows/seats/{sid}/{Y}/{M}/{D}/{qty}/{HH-MM}/{pid}

renders the actual price-tier legend the buyer chooses from. Parsing
that legend is the only place we can verify whether the "from £X"
claim is real or phantom.

This pass runs AFTER ``ttd_availability.py``. It needs the real perf_id
that bindcalendar resolves; without it the ``/0`` placeholder URLs from
the base scraper don't address a real seat plan from outside TTD's own
internal navigation. Perfs with ``verified_price_source`` other than
``"ttd_calendar"`` are therefore skipped.

Fields written
--------------
Each performance dict gets the following keys (mutated in place):

    seat_min_price          float | None   minimum price-tier in the legend
    seat_max_price          float | None   maximum price-tier in the legend
    seat_price_tiers        list[float]    sorted, deduped tier values
    seat_price_source       str            one of:
        "seat_plan"        — legend parsed; trust seat_min_price
        "no_legend"        — page fetched but no legend extractable
                             (off-sale / unusual layout — drop price downstream)
        "fetch_failed"     — HTTP error
        "skipped"          — no perf_id, no resolvable URL, or past date
    seat_status             int | str | None  HTTP status of the fetch
    seat_url                str | None        the seat-plan URL we queried
    seat_checked_at         str               UTC ISO timestamp
    seat_parse_strategy     str | None        which fallback strategy hit
                                              ("a_json", "b_legend", "c_sweep")
    seat_agrees_with_calendar  bool | None    True if min(tiers) matches
                                              verified_min_price within
                                              £0.50; False when calendar
                                              over-reports or under-reports;
                                              None on parse failure /
                                              skipped perf

The existing ``verified_*`` fields on the performance are NOT modified.
The dedupe layer's TTD schema should be updated separately to prefer
``seat_min_price`` over ``verified_min_price`` when present — see the
companion patch notes alongside this script.

Usage
-----
    python ttd_seat_verification.py                  # in-place on ttd.json
    python ttd_seat_verification.py --in ttd.json    # different input
    python ttd_seat_verification.py --concurrency 4  # tune parallelism
    python ttd_seat_verification.py --limit 5        # smoke-test on N shows
    python ttd_seat_verification.py --include-past   # also check past dates
    python ttd_seat_verification.py --dry-run        # don't write

Single-perf debug (run once per parser change to confirm it still hits):

    python ttd_seat_verification.py \\
        --smoke-show 7219 --save-html /tmp/urinal-seat.html

Exit codes
----------
    0  success (some or all perfs verified, partial fails are normal)
    1  bad input (file missing, malformed JSON, unresolved smoke-show)
    2  zero successes despite >0 attempts — likely the page structure
       has changed; rerun with --smoke-show to inspect what we got
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests
from requests.adapters import HTTPAdapter
from urllib.parse import quote, urlsplit, urlunsplit
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _ascii_safe_url(url: str | None) -> str | None:
    """Re-encode URL path/query to be ASCII-safe for HTTP transmission.

    Show URLs and seat-plan URLs alike can contain U+2019 (right single
    quotation mark) and U+2013 (en-dash) — e.g. *Churchill's Urinal*'s
    slug includes the curly apostrophe. ``requests`` raises
    ``UnicodeEncodeError: 'latin-1' codec can't encode character`` when
    handed these directly; percent-encoding the path before the request
    line is built fixes it. Identical helper to ``ttd_availability.py``;
    duplicated to keep this script standalone.
    """
    if not url:
        return url
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        quote(parts.path, safe="/-_.~%"),
        quote(parts.query, safe="=&%"),
        parts.fragment,
    ))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://www.theatreticketsdirect.co.uk"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# Seat-plan pages are full HTML documents (50–500 KB) — heavier than
# bindcalendar's small HTML fragments. Default concurrency lower than
# ttd_availability.py to be a polite citizen on TTD's origin.
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_S = 25

RETRY_TOTAL = 3
RETRY_BACKOFF = 0.6

SOURCE_OK         = "seat_plan"
SOURCE_NO_LEGEND  = "no_legend"
SOURCE_FETCH_FAIL = "fetch_failed"
SOURCE_SKIPPED    = "skipped"

# Calendar-cross-check tolerance: if the seat-plan minimum matches the
# calendar's verified_min_price within this many pence, count them as
# in agreement. The legend renders penny-precise so the tolerance only
# guards against rounding, not real disagreement.
AGREE_TOLERANCE_GBP = 0.50

# Common-sense bounds for a real ticket-tier price in GBP. A West End
# top tier rarely exceeds £350; £5 concessions are plausible; anything
# outside this band is almost certainly noise (booking fees, decoy
# zeros) and excluded by every strategy.
PRICE_MIN_GBP = 5.0
PRICE_MAX_GBP = 500.0

# A real legend has between 1 and ~10 tiers. >12 is almost certainly a
# parse where we accidentally swept in fees, gift-voucher amounts, or
# other non-tier prices; we treat that as a parse miss and fall through.
MAX_PLAUSIBLE_TIERS = 12

EXIT_CLEAN     = 0
EXIT_BAD_INPUT = 1
EXIT_DRIFT     = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ttd-seats")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
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
        pool_connections=2,
        pool_maxsize=2,
        max_retries=retry,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
#
# Three strategies, tried in order. Each takes the raw page HTML and
# returns a sorted list of unique tier prices (floats). The first
# strategy that returns a sensible 1..MAX_PLAUSIBLE_TIERS result wins.
#
# Why three? TTD is a legacy ASP.NET MVC site and we can't be certain
# which way it renders the legend without looking at every page. The
# strategies overlap defensively:
#
#   A. Inline JSON. .NET MVC views often embed a chunk of state into a
#      <script> block — e.g. `var priceBands = [{price: 25.00, ...}]`.
#      If we find a hint token (priceBands / priceCategories / etc.)
#      we extract numeric prices from a tight window around it.
#   B. Legend container. Visual legend rendered as <ul>/<div>/<span>
#      with class/id hints like "legend", "price-band", "seat-key".
#      We extract £-prefixed prices from windows around those hints.
#   C. Defensive sweep. Last-resort scan of the first 60 KB for every
#      distinct £XX.XX, filtering out tokens whose immediate context
#      contains "booking fee" / "from £" / etc. — see _NOISE_CONTEXT_RE.
#
# The bounds-check (PRICE_MIN_GBP / PRICE_MAX_GBP) is applied in all
# three; the tier-count sanity check happens in the dispatcher.

# Strategy A: embedded JSON price bands.
_JSON_PRICE_BAND_RE = re.compile(
    r'"(?:price|amount|value|fareValue|priceValue)"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
    re.IGNORECASE,
)
_JSON_BLOCK_HINT_RE = re.compile(
    r'(?:priceBands?|priceTypes?|priceList|priceCategories|seatPrices?|'
    r'ticketTypes?|fareTypes?|seatTypes?)',
    re.IGNORECASE,
)

# Strategy B: legend-container £XX.XX patterns.
_LEGEND_PRICE_RE = re.compile(r'£\s*(\d{1,3}(?:\.\d{2})?)')
_LEGEND_CONTAINER_HINTS = (
    "legend", "price-band", "priceband", "price-tier", "pricetier",
    "seat-key", "seatkey", "price-key", "pricekey", "price-list",
    "seat-legend", "seatlegend", "price-legend", "seat-types",
    "seattypes", "ticket-types", "tickettypes",
)

# Strategy C: defensive head-of-page sweep window (60 KB).
_HEAD_KB = 60 * 1024

# Noise patterns. If a £XX.XX appears within ~80 chars of any of these
# tokens, we skip it. "from £X" specifically excludes the upstream
# database-driven minimum (e.g. the same £13 the calendar reports);
# the legend tiers are bare £XX.XX without "from".
_NOISE_CONTEXT_RE = re.compile(
    r'(?:booking\s*fee|service\s*fee|transaction\s*fee|gift\s*voucher|'
    r'restoration\s*levy|delivery|postage|handling\s*fee|'
    r'from\s*£|starting\s*from|prices?\s*from)',
    re.IGNORECASE,
)


def _extract_prices_strategy_a(html: str) -> list[float]:
    """Strategy A: embedded JSON price bands.

    Find each occurrence of a price-band-like hint token; within a 4 KB
    window around each, extract every numeric ``price``/``amount``/
    ``value`` field. Returns sorted unique tier values within
    PRICE_MIN_GBP..PRICE_MAX_GBP.
    """
    found: set[float] = set()
    for hint in _JSON_BLOCK_HINT_RE.finditer(html):
        lo = max(0, hint.start() - 200)
        hi = min(len(html), hint.start() + 4096)
        for m in _JSON_PRICE_BAND_RE.finditer(html, lo, hi):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if PRICE_MIN_GBP <= v <= PRICE_MAX_GBP:
                found.add(v)
    return sorted(found)


def _extract_prices_strategy_b(html: str) -> list[float]:
    """Strategy B: legend-container HTML.

    Find substrings whose class/id contains a legend-related hint, then
    extract £-prices from a 3 KB window around each. The hint match is
    case-insensitive but we keep the original case for the £ scan so
    byte offsets are stable.
    """
    found: set[float] = set()
    html_lower = html.lower()
    for hint in _LEGEND_CONTAINER_HINTS:
        idx = 0
        while True:
            i = html_lower.find(hint, idx)
            if i == -1:
                break
            lo = max(0, i - 200)
            hi = min(len(html), i + 3072)
            window = html[lo:hi]
            for m in _LEGEND_PRICE_RE.finditer(window):
                # Check immediate context for noise tokens. We use the
                # narrower ~80 char window from Strategy C, since legend
                # containers can be near "from £X" headers in some
                # layouts.
                ctx_lo = max(0, m.start() - 60)
                ctx_hi = min(len(window), m.end() + 20)
                if _NOISE_CONTEXT_RE.search(window[ctx_lo:ctx_hi]):
                    continue
                try:
                    v = float(m.group(1))
                except ValueError:
                    continue
                if PRICE_MIN_GBP <= v <= PRICE_MAX_GBP:
                    found.add(v)
            idx = i + len(hint)
    return sorted(found)


def _extract_prices_strategy_c(html: str) -> list[float]:
    """Strategy C: defensive head-of-page sweep.

    Scan the first 60 KB of the document for every distinct £XX.XX whose
    immediate ~80 char context doesn't contain a noise token. Last-
    resort fallback — the loose bounds and the noise filter together
    catch most false positives, but we still validate the tier count in
    the dispatcher before trusting this strategy's output.
    """
    head = html[:_HEAD_KB]
    found: set[float] = set()
    for m in _LEGEND_PRICE_RE.finditer(head):
        ctx_lo = max(0, m.start() - 60)
        ctx_hi = min(len(head), m.end() + 20)
        if _NOISE_CONTEXT_RE.search(head[ctx_lo:ctx_hi]):
            continue
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if PRICE_MIN_GBP <= v <= PRICE_MAX_GBP:
            found.add(v)
    return sorted(found)


_STRATEGIES = (
    ("a_json",   _extract_prices_strategy_a),
    ("b_legend", _extract_prices_strategy_b),
    ("c_sweep",  _extract_prices_strategy_c),
)


def parse_seat_plan_prices(html: str) -> tuple[list[float], str]:
    """Run each strategy in order; return the first sensible result.

    "Sensible" means 1..MAX_PLAUSIBLE_TIERS distinct tiers. Returns
    ``([], "none")`` if every strategy comes up empty or with too many
    tiers — that's the parse-failed signal the dispatcher will surface
    as ``seat_price_source = "no_legend"``.
    """
    for name, fn in _STRATEGIES:
        tiers = fn(html)
        if 1 <= len(tiers) <= MAX_PLAUSIBLE_TIERS:
            return tiers, name
    return [], "none"


# ---------------------------------------------------------------------------
# Per-show worker
# ---------------------------------------------------------------------------

def _empty_seat_fields(source: str, *, url: str | None = None,
                        status=None) -> dict:
    """Build the seat_* fields for a performance we couldn't verify."""
    return {
        "seat_min_price": None,
        "seat_max_price": None,
        "seat_price_tiers": [],
        "seat_price_source": source,
        "seat_status": status,
        "seat_url": url,
        "seat_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seat_parse_strategy": None,
        "seat_agrees_with_calendar": None,
    }


def verify_one_show(
    show: dict,
    today_iso: str,
    include_past: bool,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, dict[str, int]]:
    """Fetch the seat-plan page for each verified perf of one show and
    parse its legend. Mutates ``show['performances']`` in place. Returns
    ``(perfs_touched, counts_by_source)``.
    """
    counts = {SOURCE_OK: 0, SOURCE_NO_LEGEND: 0,
              SOURCE_FETCH_FAIL: 0, SOURCE_SKIPPED: 0}

    show_id = show.get("id")
    show_url = _ascii_safe_url(show.get("url") or show.get("detail_canonical"))
    perfs = show.get("performances") or []

    if not isinstance(show_id, int) or not show_url or not perfs:
        for p in perfs:
            p.update(_empty_seat_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
        return len(perfs), counts

    # Filter to perfs we can actually verify. Reasons to skip:
    #   - past dated (calendar widget doesn't return past months either)
    #   - no resolved perf_id (calendar didn't return this date/time)
    #   - no verified_book_url (same root cause)
    to_check: list[tuple[dict, str]] = []
    for p in perfs:
        date = p.get("date")
        if not date or (not include_past and date < today_iso):
            p.update(_empty_seat_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue
        if p.get("verified_price_source") != "ttd_calendar":
            p.update(_empty_seat_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue
        url = p.get("verified_book_url")
        if not url:
            p.update(_empty_seat_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue
        to_check.append((p, url))

    if not to_check:
        return len(perfs), counts

    session = build_session()

    # Warmup: seed ASP.NET_SessionId etc. on this client. Without the
    # warmup, the seat-plan page sometimes returns an unstyled fallback
    # page that lacks the legend. If the warmup itself fails we still
    # try the seat plans — they'll likely fail too but we'll classify
    # cleanly as fetch_failed rather than no_legend.
    try:
        wr = session.get(show_url, timeout=timeout_s)
        if wr.status_code >= 400:
            log.debug("warmup HTTP %d for show %d", wr.status_code, show_id)
    except requests.RequestException as e:
        log.debug("warmup exception for show %d: %s", show_id, e)

    referer = show_url
    for p, url in to_check:
        safe_url = _ascii_safe_url(url) or url
        try:
            r = session.get(
                safe_url,
                headers={"Referer": referer,
                         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                timeout=timeout_s,
            )
        except requests.RequestException as e:
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL, url=safe_url, status=str(e)[:160],
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue
        if r.status_code != 200:
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL, url=safe_url, status=r.status_code,
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue
        if not r.text:
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL, url=safe_url, status="empty_body",
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue

        try:
            tiers, strategy = parse_seat_plan_prices(r.text)
        except Exception as e:  # noqa: BLE001 — defensive; never break the scrape
            log.warning("parse exception for show %d %s %s: %s",
                        show_id, p.get("date"), p.get("time"), e)
            tiers, strategy = [], "exception"

        if not tiers:
            p.update(_empty_seat_fields(
                SOURCE_NO_LEGEND, url=safe_url, status=200,
            ))
            counts[SOURCE_NO_LEGEND] += 1
            continue

        # Compute agreement vs calendar
        cal_min = p.get("verified_min_price")
        agrees: bool | None
        if cal_min is None:
            agrees = None
        else:
            agrees = abs(min(tiers) - cal_min) <= AGREE_TOLERANCE_GBP

        p["seat_min_price"]            = min(tiers)
        p["seat_max_price"]            = max(tiers)
        p["seat_price_tiers"]          = tiers
        p["seat_price_source"]         = SOURCE_OK
        p["seat_status"]               = 200
        p["seat_url"]                  = safe_url
        p["seat_checked_at"]           = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        p["seat_parse_strategy"]       = strategy
        p["seat_agrees_with_calendar"] = agrees
        counts[SOURCE_OK] += 1

    return len(perfs), counts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(payload: dict, *, concurrency: int, limit: int | None,
        include_past: bool) -> dict:
    today_iso = datetime.now(timezone.utc).date().isoformat()
    shows = payload.get("shows") or []
    if limit is not None:
        shows = shows[:limit]
        log.info("--limit %d applied", limit)

    total_shows = len(shows)
    total_perfs = sum(len(s.get("performances") or []) for s in shows)
    log.info(
        "Verifying %d performance(s) across %d show(s) with %d worker(s)",
        total_perfs, total_shows, concurrency,
    )
    if total_shows == 0:
        return {
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "shows_checked": 0, "total_perfs": 0,
            "ok": 0, "no_legend": 0, "fetch_failed": 0, "skipped": 0,
            "calendar_agreed": 0, "calendar_disagreed": 0,
            "duration_seconds": 0.0,
        }

    totals = {SOURCE_OK: 0, SOURCE_NO_LEGEND: 0,
              SOURCE_FETCH_FAIL: 0, SOURCE_SKIPPED: 0}
    totals_lock = Lock()
    progress = {"n": 0}
    progress_lock = Lock()

    def _job(show: dict) -> dict[str, int]:
        _, counts = verify_one_show(show, today_iso, include_past)
        return counts

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_job, s): s for s in shows}
        for fut in as_completed(futures):
            try:
                counts = fut.result()
            except Exception as e:  # noqa: BLE001 — workers shouldn't raise
                log.warning("worker exception: %s", e)
                continue
            with totals_lock:
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + v
            with progress_lock:
                progress["n"] += 1
                if progress["n"] % 25 == 0 or progress["n"] == total_shows:
                    log.info(
                        "  progress: %d/%d shows  (ok=%d, no_legend=%d, fail=%d)",
                        progress["n"], total_shows,
                        totals[SOURCE_OK],
                        totals[SOURCE_NO_LEGEND],
                        totals[SOURCE_FETCH_FAIL],
                    )

    elapsed = time.monotonic() - t0

    # Post-pass: agreement counts across all perfs touched.
    agreed = 0
    disagreed = 0
    for s in shows:
        for p in s.get("performances", []):
            a = p.get("seat_agrees_with_calendar")
            if a is True:
                agreed += 1
            elif a is False:
                disagreed += 1

    log.info(
        "Done in %.1fs — ok=%d (calendar agreed=%d, disagreed=%d), "
        "no_legend=%d, fetch_failed=%d, skipped=%d",
        elapsed,
        totals[SOURCE_OK], agreed, disagreed,
        totals[SOURCE_NO_LEGEND],
        totals[SOURCE_FETCH_FAIL],
        totals[SOURCE_SKIPPED],
    )
    if disagreed:
        log.info(
            "%d perfs where the seat plan disagrees with the calendar — "
            "the seat plan wins. (This is the King's Head / phantom-tier case.)",
            disagreed,
        )

    summary = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shows_checked": total_shows,
        "total_perfs": total_perfs,
        "ok": totals[SOURCE_OK],
        "no_legend": totals[SOURCE_NO_LEGEND],
        "fetch_failed": totals[SOURCE_FETCH_FAIL],
        "skipped": totals[SOURCE_SKIPPED],
        "calendar_agreed": agreed,
        "calendar_disagreed": disagreed,
        "duration_seconds": round(elapsed, 1),
    }
    report = payload.setdefault("report", {})
    if isinstance(report, dict):
        report["seat_verification"] = summary
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Verify TTD per-performance prices by fetching the actual "
            "seat-plan page and parsing its price-tier legend — the "
            "ground truth for what a buyer can pick from."
        ),
    )
    p.add_argument("--in", "-i", dest="in_path", type=Path,
                   default=Path("ttd.json"),
                   help="Input JSON (default: ttd.json). Must have already "
                        "been processed by ttd_availability.py — we rely on "
                        "verified_book_url and verified_min_price.")
    p.add_argument("--out", "-o", dest="out_path", type=Path, default=None,
                   help="Output JSON path. Default: overwrite input in place.")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel workers (default: {DEFAULT_CONCURRENCY}). "
                        "Lower than ttd_availability since seat-plan pages "
                        "are heavier.")
    p.add_argument("--limit", type=int, default=None,
                   help="Verify only the first N shows (smoke-test the "
                        "whole pipeline cheaply).")
    p.add_argument("--include-past", action="store_true",
                   help="Also fetch past-dated performances (skipped by "
                        "default — they 404 on the seat-plan endpoint).")
    p.add_argument("--dry-run", action="store_true",
                   help="Do everything except write the output file.")
    p.add_argument("--smoke-show", type=int, default=None,
                   help="Single-show debug mode: fetch the first verified "
                        "perf for this show ID, dump each strategy's output "
                        "to stderr, and exit. Use when adding support for a "
                        "new venue or after a parse failure.")
    p.add_argument("--save-html", type=Path, default=None,
                   help="With --smoke-show, save the raw seat-plan HTML to "
                        "this path for offline inspection.")
    args = p.parse_args(argv)

    if not args.in_path.exists():
        log.error("Input file %s not found", args.in_path)
        return EXIT_BAD_INPUT

    try:
        payload = json.loads(args.in_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("Could not load %s: %s", args.in_path, e)
        return EXIT_BAD_INPUT

    if not isinstance(payload, dict) or not isinstance(payload.get("shows"), list):
        log.error("Input %s does not look like ttd scraper output "
                  "(expected top-level dict with 'shows' list)", args.in_path)
        return EXIT_BAD_INPUT

    # Defensive guard: if the input has zero verified_book_urls,
    # ttd_availability.py probably didn't run. Warn but don't refuse —
    # the script will produce all-skipped output which is at least honest.
    has_verified = any(
        p.get("verified_book_url")
        for s in payload["shows"]
        for p in (s.get("performances") or [])
    )
    if not has_verified:
        log.warning(
            "Input has no verified_book_url fields — ttd_availability.py "
            "doesn't look like it has run. Every perf will be skipped.",
        )

    if args.smoke_show is not None:
        return _smoke_one(payload, args.smoke_show, args.save_html)

    summary = run(
        payload,
        concurrency=args.concurrency,
        limit=args.limit,
        include_past=args.include_past,
    )

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

    # Drift sentinel: if we attempted real fetches but got zero legends,
    # the page structure has probably changed. Surface a hard error so
    # CI flags it instead of silently shipping a TTD pipeline with no
    # ground-truth verification.
    attempted_real = (summary["total_perfs"]
                      - summary["skipped"]
                      - summary["fetch_failed"])
    if attempted_real > 0 and summary["ok"] == 0:
        log.error(
            "No seat-plan pages parsed successfully out of %d attempts — "
            "the page structure may have changed. Run with "
            "--smoke-show {id} --save-html /tmp/seat.html to inspect.",
            attempted_real,
        )
        return EXIT_DRIFT
    return EXIT_CLEAN


def _smoke_one(payload: dict, show_id: int, save_html: Path | None) -> int:
    """Single-show debug pass. Fetches the first verified perf for this
    show and prints what each parser strategy extracted, separately.
    Run this once on a known show after any parser tweak to confirm
    the legend is still being picked up.
    """
    shows = payload.get("shows") or []
    show = next((s for s in shows if s.get("id") == show_id), None)
    if show is None:
        log.error("Show ID %d not found in input", show_id)
        return EXIT_BAD_INPUT

    perf = next(
        (p for p in show.get("performances") or []
         if p.get("verified_price_source") == "ttd_calendar"
         and p.get("verified_book_url")),
        None,
    )
    if perf is None:
        log.error("Show %d has no verified-calendar perf to test against. "
                  "Run ttd_availability.py first.", show_id)
        return EXIT_BAD_INPUT

    url = _ascii_safe_url(perf["verified_book_url"]) or perf["verified_book_url"]
    show_url = _ascii_safe_url(show.get("url") or show.get("detail_canonical"))
    session = build_session()

    log.info("Smoke-test: show=%d  perf=%s %s",
             show_id, perf.get("date"), perf.get("time"))
    log.info("  show URL: %s", show_url)
    log.info("  seat URL: %s", url)
    log.info("  calendar said: from £%s", perf.get("verified_min_price"))

    log.info("Warmup GET...")
    try:
        wr = session.get(show_url, timeout=DEFAULT_TIMEOUT_S)
        log.info("  warmup status %d, %d bytes", wr.status_code, len(wr.text))
    except requests.RequestException as e:
        log.warning("warmup failed: %s", e)

    log.info("Fetching seat plan...")
    try:
        r = session.get(url, headers={"Referer": show_url or ""},
                        timeout=DEFAULT_TIMEOUT_S)
    except requests.RequestException as e:
        log.error("fetch failed: %s", e)
        return EXIT_DRIFT
    log.info("  status %d, %d bytes, content-type=%s",
             r.status_code, len(r.text),
             r.headers.get("content-type", "?"))

    if save_html is not None:
        save_html.parent.mkdir(parents=True, exist_ok=True)
        save_html.write_text(r.text, encoding="utf-8")
        log.info("Saved raw HTML to %s", save_html)

    for name, fn in _STRATEGIES:
        try:
            tiers = fn(r.text)
        except Exception as e:  # noqa: BLE001 — surface parser bugs here
            log.warning("strategy %s raised: %s", name, e)
            continue
        log.info("  strategy %-9s -> %s", name, tiers)

    final, strategy = parse_seat_plan_prices(r.text)
    log.info("FINAL: tiers=%s  strategy=%s  calendar_said=£%s",
             final, strategy, perf.get("verified_min_price"))
    if final and perf.get("verified_min_price") is not None:
        agrees = abs(min(final) - perf["verified_min_price"]) <= AGREE_TOLERANCE_GBP
        log.info("  agrees with calendar: %s%s",
                 agrees,
                 "" if agrees else "  <-- phantom-tier case!")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
