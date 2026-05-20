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

The existing `low_price` / `currency` / `availability` fields are
**not modified**. The dedupe layer's SeatPlan schema is updated
separately to prefer `verified_min_price` when present.

Usage
-----
    python seatplan_availability.py                    # in-place on default file
    python seatplan_availability.py --in input.json    # different input
    python seatplan_availability.py --out output.json  # write to different file
    python seatplan_availability.py --concurrency 24   # tune parallelism
    python seatplan_availability.py --limit 50         # smoke-test on N perfs
    python seatplan_availability.py --include-past     # also check past dates
    python seatplan_availability.py --dry-run          # don't write

Exit codes
----------
    0  success (some or all perfs verified, partial fails are normal)
    1  bad input (file missing, malformed JSON)
    2  zero successes despite >0 attempts — likely URL pattern drift
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seatplan-avail")


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
) -> dict:
    """Run verification in place on payload['shows'][i]['performances'][j].

    Returns a summary dict suitable for embedding under
    payload['report']['availability_verification'].
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

    session = build_session(pool_size=max(concurrency, 8))

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
        log.error("Input %s does not look like seatplan scraper output "
                  "(expected top-level dict with 'shows' list)", args.in_path)
        return EXIT_BAD_INPUT

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
