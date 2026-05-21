"""
TTD per-performance price verifier
==================================

Second pass for `ttd_scraper.py`. TTD's detail-page JSON-LD emits the
show-wide minimum (e.g. £7 for *A Midsummer Night's Dream* at the
Globe) in every `TheaterEvent.offers.price`, regardless of whether
that tier is actually on sale for the specific performance. The
21 May 2026 19:30 evening of AMND is the canonical example: JSON-LD
says £7, but the cheapest ticket on sale is £13.

The TTD show page's calendar widget exposes the real per-performance
prices and the real `perf_id` (the existing scraper writes `/0`
placeholder URLs) via:

    POST /show/bindcalendar
    Content-Type: application/x-www-form-urlencoded
    X-Requested-With: XMLHttpRequest
    Body: id={show_id}&monthyear={M},{YYYY}&tickets=2&loadMonths=true

A session warmup (one GET to the show URL on the same Client) is
required — without it the endpoint returns 404 or an empty body.

The existing scraper's `_bindcal_form` builds `{id, month, year}`
which is the WRONG shape. That call silently 404s in CI (see the
`bindcalendar.*HTTP 404` warnings the existing scraper emits but
swallows). Both perf-ID resolution and per-perf prices are lost.

This verifier makes the correct call and writes back:

    verified_min_price          float | None   real per-perf min from "from £X.XX"
    verified_perf_id            int | None     non-zero perf ID (the one the existing
                                               scraper tries and fails to resolve)
    verified_book_url           str | None     full booking URL with the real perf_id
    verified_price_source       str            one of:
        "ttd_calendar"     — found in calendar response; trust verified_min_price
        "not_in_calendar"  — month was fetched but this (date, time) wasn't returned
                             (sold out / off-sale / cancelled — drop price downstream)
        "fetch_failed"     — HTTP error on this perf's month
        "skipped"          — perf has no date/time, no show URL, or all-past
    verified_status             int | str | None  HTTP status of the month fetch
    verified_url                str | None        the calendar URL we queried
    verified_checked_at         str               UTC ISO timestamp

Response shape we parse (HTML fragment from bindcalendar):

    <a href="...shows/seats/{show_id}/{Y}/{M}/{D}/{qty}/{HH-MM}/{perf_id}"
       class="withloader time two-columns">
       {HH:MM}
       <span class="price-from">from &#163;{X.XX}</span>
    </a>

The existing `price` / `book_url` fields on the performance are
**not modified**. The dedupe layer's TTD schema is updated separately
to prefer `verified_min_price` when present.

Usage
-----
    python ttd_availability.py                  # in-place on default file
    python ttd_availability.py --in ttd.json    # different input
    python ttd_availability.py --concurrency 8  # tune parallelism
    python ttd_availability.py --limit 5        # smoke-test on N shows
    python ttd_availability.py --include-past   # also check past dates
    python ttd_availability.py --dry-run        # don't write

Exit codes
----------
    0  success (some or all perfs verified, partial fails are normal)
    1  bad input (file missing, malformed JSON)
    2  zero successes despite >0 attempts — likely site changed
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
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://www.theatreticketsdirect.co.uk"
BINDCAL_URL = f"{BASE}/show/bindcalendar"

# Headers matched to what the browser sends (per HAR capture). Without
# the X-Requested-With + Origin combo, TTD returns 404 or empty body.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

POST_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE,
    "Accept": "text/html, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

DEFAULT_CONCURRENCY = 8      # 8 workers × ~3 months × ~210 shows = ~30s total
DEFAULT_TIMEOUT_S = 20

RETRY_TOTAL = 3
RETRY_BACKOFF = 0.6

SOURCE_OK         = "ttd_calendar"
SOURCE_NOT_FOUND  = "not_in_calendar"
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
log = logging.getLogger("ttd-avail")


# ---------------------------------------------------------------------------
# Session: per-worker, since the .NET ASP.NET_SessionId cookie is required
# and the warmup binds it to the show URL.
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=4,
        max_retries=retry,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
#
# The bindcalendar response is an HTML fragment containing many <a> tags,
# each looking like:
#
#    <a href="...shows/seats/{sid}/{Y}/{M}/{D}/{qty}/{HH-MM}/{pid}"
#       class="withloader time two-columns">
#        14:00
#        <span class="price-from">from &#163;7.00</span>
#    </a>
#
# "Tickets not available" rows are NOT <a> tags — they don't carry seats
# URLs or prices, so they naturally don't match the anchor regex.

_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?'
    r'href="[^"]*?/shows/seats/(?P<sid>\d+)/(?P<y>\d{4})/(?P<mo>\d{1,2})/'
    r'(?P<d>\d{1,2})/\d+/(?P<hhmm>\d{1,2}-\d{2})/(?P<pid>\d+)"[^>]*?'
    r'class="[^"]*\bwithloader\b[^"]*"[^>]*>'
    r'(?P<inner>.*?)'
    r'</a>',
    re.DOTALL,
)
_PRICE_RE = re.compile(
    r'price-from[^>]*>\s*from\s*&#163;\s*(\d+(?:\.\d{2})?)',
    re.IGNORECASE,
)


def parse_calendar_html(html: str, show_id: int) -> dict[tuple[str, str], dict]:
    """Parse a bindcalendar response into a per-perf lookup map.

    Returns: ``{(date_iso, time_hhmm): {"perf_id": int, "price": float, "book_url": str}}``

    Anchors for *other* shows (shouldn't appear, but the page sometimes
    inlines cross-promo content) are dropped via the show_id check.
    Anchors with `/0` placeholder perf IDs are also dropped — they
    carry no real per-perf info.
    """
    out: dict[tuple[str, str], dict] = {}
    for m in _ANCHOR_RE.finditer(html):
        try:
            sid = int(m.group("sid"))
            if sid != show_id:
                continue
            pid = int(m.group("pid"))
            if pid == 0:
                continue
            y = int(m.group("y"))
            mo = int(m.group("mo"))
            d = int(m.group("d"))
            hhmm = m.group("hhmm")  # e.g. "14-00"
            hh, mi = hhmm.split("-")
            date_iso = f"{y:04d}-{mo:02d}-{d:02d}"
            time_str = f"{int(hh):02d}:{mi}"
        except (ValueError, AttributeError):
            continue

        price_match = _PRICE_RE.search(m.group("inner"))
        if not price_match:
            # Anchor without a price-from span — shouldn't happen for
            # bookable perfs, but be defensive.
            continue
        try:
            price = float(price_match.group(1))
        except ValueError:
            continue

        book_url = (
            f"{BASE}/shows/seats/{sid}/{y}/{mo}/{d}/2/{hhmm}/{pid}"
        )
        out[(date_iso, time_str)] = {
            "perf_id": pid,
            "price": price,
            "book_url": book_url,
        }
    return out


# ---------------------------------------------------------------------------
# Per-show worker
# ---------------------------------------------------------------------------

def _empty_verified_fields(source: str, *, url: str | None = None,
                           status=None) -> dict:
    """Build the verified_* fields for a performance we couldn't price."""
    return {
        "verified_min_price": None,
        "verified_perf_id": None,
        "verified_book_url": None,
        "verified_price_source": source,
        "verified_status": status,
        "verified_url": url,
        "verified_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _months_needed(perfs: list[dict], today_iso: str,
                   include_past: bool) -> set[tuple[int, int]]:
    """Distinct (year, month) pairs across a show's performances."""
    out: set[tuple[int, int]] = set()
    for p in perfs:
        date = p.get("date")
        if not date or len(date) < 10:
            continue
        if not include_past and date < today_iso:
            continue
        try:
            y = int(date[:4]); mo = int(date[5:7])
            out.add((y, mo))
        except ValueError:
            continue
    return out


def verify_one_show(
    show: dict,
    today_iso: str,
    include_past: bool,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, dict[str, int]]:
    """Verify all performances of one show. Mutates show['performances']
    in place. Returns (perfs_touched, counts_by_source)."""
    counts = {SOURCE_OK: 0, SOURCE_NOT_FOUND: 0,
              SOURCE_FETCH_FAIL: 0, SOURCE_SKIPPED: 0}

    show_id = show.get("id")
    show_url = show.get("url") or show.get("detail_canonical")
    perfs = show.get("performances") or []

    if not isinstance(show_id, int) or not show_url or not perfs:
        for p in perfs:
            p.update(_empty_verified_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
        return len(perfs), counts

    needed = _months_needed(perfs, today_iso, include_past)
    if not needed:
        for p in perfs:
            p.update(_empty_verified_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
        return len(perfs), counts

    session = build_session()

    # Warmup: seed ASP.NET_SessionId and any other cookies on this client.
    # If the warmup itself fails we still try the calendar — it'll most
    # likely fail too but we'll classify cleanly.
    warmup_ok = True
    try:
        wr = session.get(show_url, timeout=timeout_s)
        if wr.status_code >= 400:
            warmup_ok = False
            log.debug("warmup HTTP %d for show %d", wr.status_code, show_id)
    except requests.RequestException as e:
        warmup_ok = False
        log.debug("warmup exception for show %d: %s", show_id, e)

    # Fetch each needed month. month_map: (date, time) -> {perf_id, price, book_url}
    month_map: dict[tuple[str, str], dict] = {}
    fetch_errors: list[tuple[int, int, str | int]] = []

    headers = {**POST_HEADERS, "Referer": show_url}
    for (y, mo) in sorted(needed):
        body = {
            "id": str(show_id),
            "monthyear": f"{mo},{y}",
            "tickets": "2",
            "loadMonths": "true",
        }
        try:
            r = session.post(BINDCAL_URL, data=body, headers=headers,
                             timeout=timeout_s)
        except requests.RequestException as e:
            fetch_errors.append((y, mo, str(e)[:160]))
            continue
        if r.status_code != 200:
            fetch_errors.append((y, mo, r.status_code))
            continue
        if not r.text:
            fetch_errors.append((y, mo, "empty_body"))
            continue
        try:
            parsed = parse_calendar_html(r.text, show_id)
        except Exception as e:  # noqa: BLE001 — defensive
            fetch_errors.append((y, mo, f"parse:{type(e).__name__}"))
            continue
        month_map.update(parsed)

    # Apply to each performance
    failed_months = {(y, mo) for (y, mo, _) in fetch_errors}
    for p in perfs:
        date = p.get("date")
        time_str = p.get("time")
        if not date or not time_str:
            p.update(_empty_verified_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue
        if not include_past and date < today_iso:
            p.update(_empty_verified_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue
        try:
            y = int(date[:4]); mo = int(date[5:7])
        except (ValueError, IndexError):
            p.update(_empty_verified_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue

        key = (date, time_str)
        if key in month_map:
            hit = month_map[key]
            p["verified_min_price"]    = hit["price"]
            p["verified_perf_id"]      = hit["perf_id"]
            p["verified_book_url"]     = hit["book_url"]
            p["verified_price_source"] = SOURCE_OK
            p["verified_status"]       = 200
            p["verified_url"]          = (
                f"{BINDCAL_URL}?id={show_id}&monthyear={mo},{y}"
            )
            p["verified_checked_at"]   = (
                datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            counts[SOURCE_OK] += 1
        elif (y, mo) in failed_months:
            # This month's fetch failed — don't claim "not in calendar"
            err = next(e for (yy, mm, e) in fetch_errors if (yy, mm) == (y, mo))
            p.update(_empty_verified_fields(SOURCE_FETCH_FAIL, status=err))
            counts[SOURCE_FETCH_FAIL] += 1
        else:
            # Month was fetched, this (date, time) just wasn't present.
            # Most likely sold out / off-sale / cancelled.
            p.update(_empty_verified_fields(SOURCE_NOT_FOUND, status=200))
            counts[SOURCE_NOT_FOUND] += 1

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
            "shows_checked": 0, "total_perfs": 0, "ok": 0, "not_in_calendar": 0,
            "fetch_failed": 0, "skipped": 0, "duration_seconds": 0.0,
        }

    totals = {SOURCE_OK: 0, SOURCE_NOT_FOUND: 0,
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
                        "  progress: %d/%d shows  (ok=%d, not_in_cal=%d, fail=%d)",
                        progress["n"], total_shows,
                        totals[SOURCE_OK],
                        totals[SOURCE_NOT_FOUND],
                        totals[SOURCE_FETCH_FAIL],
                    )

    elapsed = time.monotonic() - t0
    log.info(
        "Done in %.1fs — ok=%d, not_in_calendar=%d, fetch_failed=%d, skipped=%d",
        elapsed,
        totals[SOURCE_OK],
        totals[SOURCE_NOT_FOUND],
        totals[SOURCE_FETCH_FAIL],
        totals[SOURCE_SKIPPED],
    )

    summary = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shows_checked": total_shows,
        "total_perfs": total_perfs,
        "ok": totals[SOURCE_OK],
        "not_in_calendar": totals[SOURCE_NOT_FOUND],
        "fetch_failed": totals[SOURCE_FETCH_FAIL],
        "skipped": totals[SOURCE_SKIPPED],
        "duration_seconds": round(elapsed, 1),
    }
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
            "Verify TTD per-performance prices by POSTing to /show/bindcalendar "
            "with the correct form fields (id, monthyear, tickets, loadMonths) "
            "and parsing the response's price-from spans."
        ),
    )
    p.add_argument(
        "--in", "-i", dest="in_path", type=Path,
        default=Path("ttd.json"),
        help="Input JSON from ttd_scraper.py (default: ttd.json).",
    )
    p.add_argument(
        "--out", "-o", dest="out_path", type=Path, default=None,
        help="Output JSON path. Default: overwrite the input in place.",
    )
    p.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Parallel workers, one show per worker (default: {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Verify only the first N shows (smoke-test).",
    )
    p.add_argument(
        "--include-past", action="store_true",
        help="Also verify past-dated performances "
             "(skipped by default — calendar doesn't return them).",
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
        log.error("Input %s does not look like ttd scraper output "
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

    attempted_real = summary["total_perfs"] - summary["skipped"]
    if attempted_real > 0 and summary["ok"] == 0:
        log.error(
            "No performances verified successfully out of %d attempts — "
            "possible bindcalendar form/endpoint drift",
            attempted_real,
        )
        return EXIT_DRIFT
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
