"""
TTD seat-plan verifier (third pass) — BindSeatPlan AJAX edition.

What this is
============
Third pass for the TTD pipeline. Calls ``POST /Show/BindSeatPlan`` —
the AJAX endpoint TTD's own front-end JS fires to populate the seat
plan on page load — and reads the ground-truth ``PriceBands`` array
from the JSON response. That array is the legend the buyer actually
sees in their browser: the currently-bookable price tiers for the
specific performance.

History (worth knowing if this file breaks again)
-------------------------------------------------
The first cut of this script (21 May 2026) fetched the *static* seat-
plan HTML page and tried to regex out a legend using three fallback
strategies (inline JSON / legend-container HTML / head-of-page sweep).
That was the wrong approach: the static page is an empty shell for
every venue except the London Coliseum (which inlines all 983 seat
tooltips). For Churchill's Urinal, Beetlejuice, the Globe, etc. the
shipped HTML carries zero price data — JS populates everything client-
side via ``BindSeatPlan``. The first cut therefore returned no_legend
on 321/332 perfs and false-positive Coliseum hits on the other 11
(every one returning the same bogus tier set [110, 134.5, 150, 182.5]).

Diagnosis came from two local recon scripts (ttd_recon.py /
ttd_recon2.py) that drained the static page, found the inline
``loadUrl = "/Show/BindSeatPlan"`` script tag, and confirmed that POSTing
the inline ``params = {…}`` block to that endpoint returns clean JSON
with a ``PriceBands`` array. This file is the result: a four-line
parser around that AJAX call.

Why we hit BindSeatPlan directly (no static-page fetch)
-------------------------------------------------------
All seven parameters BindSeatPlan needs can be constructed from data
already in ttd.json:

    performanceId    → perf["verified_perf_id"]    (set by ttd_availability.py)
    showId           → show["id"]
    VenueId          → parse "/venue/{N}/" from show["venue_url"]
    tickets          → constant "2"
    PDate            → convert perf["date"] (YYYY-MM-DD) to DD/MM/YYYY
    Time             → convert perf["time"] (HH:MM) to HH-MM
    PerformancesFor  → constant "SeatPlan"

So we skip the static-page GET entirely and go straight to the AJAX
POST. Halves the HTTP calls and removes the dependency on the inline
``params`` script block (which would silently break if TTD's template
ever drops it).

Fields written
--------------
Each performance dict gets the following keys (mutated in place):

    seat_min_price          float | None   minimum buyer-facing tier
                                           (PriceBands[i].Price, the
                                           selling price not face)
    seat_max_price          float | None   maximum buyer-facing tier
    seat_price_tiers        list[float]    sorted, deduped tier values
    seat_face_min_price     float | None   minimum face value (no fees)
    seat_face_max_price     float | None   maximum face value
    seat_seats_available    int | None     number of seats in Performances
    seat_price_source       str            one of:
        "seat_plan"        — JSON parsed, PriceBands non-empty
        "no_legend"        — ResultCode=0 but PriceBands empty
                             (= off-sale / no inventory)
        "fetch_failed"     — HTTP error, redirect, or ResultCode != 0
        "skipped"          — no verified_perf_id, no VenueId, or past
    seat_status             int | str | None  HTTP status (or error tag)
    seat_url                str               the BindSeatPlan URL queried
    seat_checked_at         str               UTC ISO timestamp
    seat_agrees_with_calendar  bool | None    True if seat_min_price
                                              matches verified_min_price
                                              within £0.50, False if not,
                                              None if we couldn't check

The existing ``verified_*`` fields (from ttd_availability.py) are NOT
modified. The dedupe layer prefers seat_min_price over verified_min_price
when both are present — see ``_ttd_price_from`` in
scraper/analysis/dedupe.py.

Usage
-----
    python ttd_seat_verification.py
    python ttd_seat_verification.py --in ttd.json
    python ttd_seat_verification.py --concurrency 6
    python ttd_seat_verification.py --limit 5
    python ttd_seat_verification.py --include-past
    python ttd_seat_verification.py --dry-run

Single-perf debug (prints the parsed JSON, useful when the
shape changes):

    python ttd_seat_verification.py \\
        --smoke-show 7219 --save-html /tmp/urinal-bsp.json

Exit codes
----------
    0  success (some or all perfs verified, partial fails are normal)
    1  bad input (file missing, malformed JSON, unknown smoke-show)
    2  zero successes despite >0 attempts — likely the endpoint
       changed; rerun with --smoke-show to inspect what it returned
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

def _ascii_safe_url(url):
    """Percent-encode non-ASCII characters in URL path/query (TTD show
    slugs contain curly apostrophes that requests can't put on the HTTP
    request line as-is)."""
    if not url:
        return url
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme, parts.netloc,
        quote(parts.path, safe="/-_.~%"),
        quote(parts.query, safe="=&%"),
        parts.fragment,
    ))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://www.theatreticketsdirect.co.uk"
BINDSEATPLAN_URL = f"{BASE}/Show/BindSeatPlan"

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
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# One HTTP call per perf (just the POST — we construct params locally)
# means we can push concurrency higher than the previous version, which
# was bounded by static-page fetch heaviness.
DEFAULT_CONCURRENCY = 8
DEFAULT_TIMEOUT_S = 25

RETRY_TOTAL = 3
RETRY_BACKOFF = 0.6

SOURCE_OK = "seat_plan"
SOURCE_NO_LEGEND = "no_legend"
SOURCE_FETCH_FAIL = "fetch_failed"
SOURCE_SKIPPED = "skipped"

AGREE_TOLERANCE_GBP = 0.50

# Plausibility bounds. £4 (some matinee concessions) up to £500
# (premium box seats). Anything outside is rejected as malformed
# rather than silently propagated.
PRICE_MIN_GBP = 4.0
PRICE_MAX_GBP = 500.0

EXIT_CLEAN = 0
EXIT_BAD_INPUT = 1
EXIT_DRIFT = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ttd-seats")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def build_session():
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
# Params construction
# ---------------------------------------------------------------------------

# venue_url looks like:
#   https://www.theatreticketsdirect.co.uk/venue/134/king's-head-theatre
_VENUE_ID_RE = re.compile(r"/venue/(\d+)/")


def extract_venue_id(venue_url):
    if not venue_url:
        return None
    m = _VENUE_ID_RE.search(venue_url)
    return m.group(1) if m else None


def build_params(show, perf):
    """Construct the BindSeatPlan POST body for one perf. Returns None
    if any required field is missing — caller treats that as SKIPPED."""
    pid = perf.get("verified_perf_id")
    sid = show.get("id")
    venue_id = extract_venue_id(show.get("venue_url"))
    date = perf.get("date")       # "YYYY-MM-DD"
    time_str = perf.get("time")   # "HH:MM"
    if not (pid and sid and venue_id and date and time_str):
        return None
    # YYYY-MM-DD → DD/MM/YYYY
    try:
        y, mo, d = date.split("-")
        pdate = f"{int(d):02d}/{int(mo):02d}/{int(y):04d}"
    except (ValueError, AttributeError):
        return None
    # HH:MM → HH-MM
    if ":" not in time_str:
        return None
    return {
        "performanceId":   str(pid),
        "showId":          str(sid),
        "VenueId":         str(venue_id),
        "tickets":         "2",
        "PDate":           pdate,
        "Time":            time_str.replace(":", "-"),
        "PerformancesFor": "SeatPlan",
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
#
# BindSeatPlan response shape (verified 21 May 2026 across five venues —
# King's Head, Prince Edward, London Coliseum, Shakespeare's Globe):
#
#   {
#     "ResultCode": "0",
#     "RedirectUrl": null,
#     "data": {
#       "Performances": [
#         {
#           "SeatIdOnMap": "Stalls-G-20",
#           "SeatCategory": "Stalls",
#           "Row": "G",
#           "SeatNumber": "20",
#           "CssClass": "pln1",                ← tier id
#           "FaceValueFormatted": "£125.00",   ← without fees
#           "SellingPriceFormatted": "£150.00",← buyer pays this
#           "IsDiscountAvailable": false,
#           "SaveFormatted": "£0.00",
#           "RestrictedViewDescription": null,
#           ...
#         },
#         ...
#       ],
#       "PriceBands": [
#         {"CssClass":"pln1","Price":"£150.00","FaceValue":"£125.00", ...},
#         {"CssClass":"pln2","Price":"£180.00","FaceValue":"£150.00", ...}
#       ],
#       ...
#     }
#   }
#
# We use PriceBands directly — it's the legend the buyer sees. The
# Performances array confirms which tiers actually have seats, but
# PriceBands is already filtered to those by the server.
#
# ResultCode != "0" means TTD wants the user to take some action
# (relog, retry, etc) — we treat as fetch_failed.

# Captures: optional £, optional whitespace, digit run with optional
# comma thousands separators and optional decimal part. We rely on
# replace(",", "") downstream to canonicalise — the regex just needs
# to grab the whole numeric span without splitting at a comma.
_MONEY_RE = re.compile(r"£?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def _parse_money(s):
    if not s:
        return None
    m = _MONEY_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_bindseatplan(body):
    """Decode a BindSeatPlan response and pull out the bits we care
    about. Returns ``None`` on any unrecoverable parse error.

    Output shape:
        {
            "result_code":  str,
            "tiers":        list[float]   (selling prices, sorted)
            "face_tiers":   list[float]   (face values, sorted)
            "n_seats":      int           (count in Performances array)
        }

    Empty PriceBands → tiers=[], which the dispatcher treats as no_legend.
    """
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    result_code = str(doc.get("ResultCode", ""))
    data = doc.get("data") or {}
    if not isinstance(data, dict):
        return {
            "result_code": result_code,
            "tiers":       [],
            "face_tiers":  [],
            "n_seats":     0,
        }

    bands_raw = data.get("PriceBands") or []
    perfs_raw = data.get("Performances") or []
    sell_set = set()
    face_set = set()
    for b in bands_raw:
        if not isinstance(b, dict):
            continue
        sp = _parse_money(b.get("Price"))
        fv = _parse_money(b.get("FaceValue"))
        if sp is not None and PRICE_MIN_GBP <= sp <= PRICE_MAX_GBP:
            sell_set.add(sp)
        if fv is not None and PRICE_MIN_GBP <= fv <= PRICE_MAX_GBP:
            face_set.add(fv)
    n_seats = len(perfs_raw) if isinstance(perfs_raw, list) else 0
    return {
        "result_code": result_code,
        "tiers":       sorted(sell_set),
        "face_tiers":  sorted(face_set),
        "n_seats":     n_seats,
    }


# ---------------------------------------------------------------------------
# Per-show worker
# ---------------------------------------------------------------------------

def _empty_seat_fields(source, url=None, status=None):
    return {
        "seat_min_price":            None,
        "seat_max_price":            None,
        "seat_price_tiers":          [],
        "seat_face_min_price":       None,
        "seat_face_max_price":       None,
        "seat_seats_available":      None,
        "seat_price_source":         source,
        "seat_status":               status,
        "seat_url":                  url,
        "seat_checked_at":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seat_agrees_with_calendar": None,
    }


def verify_one_show(show, today_iso, include_past, timeout_s=DEFAULT_TIMEOUT_S):
    """Verify all bookable performances of one show. Mutates
    ``show['performances']`` in place. Returns ``(perfs_touched, counts)``."""
    counts = {SOURCE_OK: 0, SOURCE_NO_LEGEND: 0,
              SOURCE_FETCH_FAIL: 0, SOURCE_SKIPPED: 0}

    show_id = show.get("id")
    show_url = _ascii_safe_url(show.get("url") or show.get("detail_canonical"))
    venue_id = extract_venue_id(show.get("venue_url"))
    perfs = show.get("performances") or []

    if not isinstance(show_id, int) or not show_url or not venue_id or not perfs:
        # Anything wrong at the show level: skip everything cleanly.
        # Missing venue_id is common enough that we don't treat it as
        # a hard error — just classify all perfs as skipped.
        for p in perfs:
            p.update(_empty_seat_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
        return len(perfs), counts

    # Build the list of perfs we can actually verify. Skip past dates,
    # perfs without a verified_perf_id (those have no resolvable URL),
    # and perfs the calendar pass marked anything other than
    # "ttd_calendar" (off-sale / fetch-failed → seat plan won't load).
    to_check = []
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
        if not p.get("verified_perf_id"):
            p.update(_empty_seat_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue
        to_check.append(p)

    if not to_check:
        return len(perfs), counts

    session = build_session()

    # Warmup: seed ASP.NET_SessionId. Without this, BindSeatPlan can
    # return 200 with ResultCode != "0" (an inert "please retry" payload).
    try:
        wr = session.get(show_url, timeout=timeout_s)
        if wr.status_code >= 400:
            log.debug("warmup HTTP %d for show %d", wr.status_code, show_id)
    except requests.RequestException as e:
        log.debug("warmup exception for show %d: %s", show_id, e)

    headers = {**POST_HEADERS, "Referer": show_url}
    for p in to_check:
        params = build_params(show, p)
        if params is None:
            p.update(_empty_seat_fields(SOURCE_SKIPPED))
            counts[SOURCE_SKIPPED] += 1
            continue

        try:
            r = session.post(BINDSEATPLAN_URL, data=params, headers=headers,
                             timeout=timeout_s)
        except requests.RequestException as e:
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL, url=BINDSEATPLAN_URL, status=str(e)[:160],
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue
        if r.status_code != 200:
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL, url=BINDSEATPLAN_URL, status=r.status_code,
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue
        if not r.text:
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL, url=BINDSEATPLAN_URL, status="empty_body",
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue

        parsed = parse_bindseatplan(r.text)
        if parsed is None:
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL, url=BINDSEATPLAN_URL, status="json_parse",
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue

        if parsed["result_code"] != "0":
            # TTD returned a non-zero result code (redirect / error).
            # Don't write seat fields — fall through to calendar.
            p.update(_empty_seat_fields(
                SOURCE_FETCH_FAIL,
                url=BINDSEATPLAN_URL,
                status=f"result_code={parsed['result_code']}",
            ))
            counts[SOURCE_FETCH_FAIL] += 1
            continue

        tiers = parsed["tiers"]
        if not tiers:
            # ResultCode=0 but no price bands → genuinely off-sale.
            p.update(_empty_seat_fields(
                SOURCE_NO_LEGEND, url=BINDSEATPLAN_URL, status=200,
            ))
            counts[SOURCE_NO_LEGEND] += 1
            continue

        # Success — write seat_* fields.
        cal_min = p.get("verified_min_price")
        if cal_min is None:
            agrees = None
        else:
            agrees = abs(min(tiers) - cal_min) <= AGREE_TOLERANCE_GBP

        p["seat_min_price"]            = min(tiers)
        p["seat_max_price"]            = max(tiers)
        p["seat_price_tiers"]          = tiers
        p["seat_face_min_price"]       = min(parsed["face_tiers"]) if parsed["face_tiers"] else None
        p["seat_face_max_price"]       = max(parsed["face_tiers"]) if parsed["face_tiers"] else None
        p["seat_seats_available"]      = parsed["n_seats"]
        p["seat_price_source"]         = SOURCE_OK
        p["seat_status"]               = 200
        p["seat_url"]                  = BINDSEATPLAN_URL
        p["seat_checked_at"]           = datetime.now(timezone.utc).isoformat(timespec="seconds")
        p["seat_agrees_with_calendar"] = agrees
        counts[SOURCE_OK] += 1

    return len(perfs), counts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(payload, concurrency, limit, include_past):
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
        return _empty_summary()

    totals = {SOURCE_OK: 0, SOURCE_NO_LEGEND: 0,
              SOURCE_FETCH_FAIL: 0, SOURCE_SKIPPED: 0}
    totals_lock = Lock()
    progress = {"n": 0}
    progress_lock = Lock()

    def _job(show):
        _, counts = verify_one_show(show, today_iso, include_past)
        return counts

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_job, s): s for s in shows}
        for fut in as_completed(futures):
            try:
                counts = fut.result()
            except Exception as e:  # noqa: BLE001
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

    agreed = disagreed = 0
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
            "%d perfs where the seat plan disagrees with the calendar "
            "by more than £%.2f — the seat plan wins.",
            disagreed, AGREE_TOLERANCE_GBP,
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


def _empty_summary():
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shows_checked": 0, "total_perfs": 0,
        "ok": 0, "no_legend": 0, "fetch_failed": 0, "skipped": 0,
        "calendar_agreed": 0, "calendar_disagreed": 0,
        "duration_seconds": 0.0,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description=(
            "Verify TTD per-performance prices by POSTing to "
            "/Show/BindSeatPlan and reading the JSON PriceBands array "
            "— the ground truth for what a buyer can pick from."
        ),
    )
    p.add_argument("--in", "-i", dest="in_path", type=Path,
                   default=Path("ttd.json"),
                   help="Input JSON (default: ttd.json). Must have been "
                        "processed by ttd_availability.py first — we rely "
                        "on verified_perf_id and verified_price_source.")
    p.add_argument("--out", "-o", dest="out_path", type=Path, default=None,
                   help="Output JSON path. Default: overwrite input in place.")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel workers (default: {DEFAULT_CONCURRENCY}). "
                        "One HTTP POST per perf, so this can be higher than "
                        "ttd_availability without hammering anything.")
    p.add_argument("--limit", type=int, default=None,
                   help="Verify only the first N shows (smoke-test).")
    p.add_argument("--include-past", action="store_true",
                   help="Also verify past-dated performances "
                        "(skipped by default — they're off-sale).")
    p.add_argument("--dry-run", action="store_true",
                   help="Do everything except write the output file.")
    p.add_argument("--smoke-show", type=int, default=None,
                   help="Single-show debug mode: hit BindSeatPlan for the "
                        "first verified perf of this show, dump the parsed "
                        "JSON to stderr, and exit. Use to inspect the live "
                        "response shape after a TTD-side change.")
    p.add_argument("--save-html", type=Path, default=None,
                   help="With --smoke-show, save the raw response body "
                        "(JSON) to this path for offline inspection.")
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

    has_verified = any(
        p.get("verified_perf_id")
        for s in payload["shows"]
        for p in (s.get("performances") or [])
    )
    if not has_verified:
        log.warning(
            "Input has no verified_perf_id fields — ttd_availability.py "
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

    attempted_real = (summary["total_perfs"]
                      - summary["skipped"]
                      - summary["fetch_failed"])
    if attempted_real > 0 and summary["ok"] == 0:
        log.error(
            "No PriceBands successfully parsed out of %d attempts — "
            "BindSeatPlan may have changed shape. Run with "
            "--smoke-show {id} --save-html /tmp/bsp.json to inspect.",
            attempted_real,
        )
        return EXIT_DRIFT
    return EXIT_CLEAN


def _smoke_one(payload, show_id, save_html):
    shows = payload.get("shows") or []
    show = next((s for s in shows if s.get("id") == show_id), None)
    if show is None:
        log.error("Show ID %d not found in input", show_id)
        return EXIT_BAD_INPUT

    perf = next(
        (p for p in show.get("performances") or []
         if p.get("verified_price_source") == "ttd_calendar"
         and p.get("verified_perf_id")),
        None,
    )
    if perf is None:
        log.error("Show %d has no verified-calendar perf to test. "
                  "Run ttd_availability.py first.", show_id)
        return EXIT_BAD_INPUT

    params = build_params(show, perf)
    if not params:
        log.error("Could not build params (missing show.venue_url maybe?). "
                  "Show fields: id=%s venue_url=%s",
                  show.get("id"), show.get("venue_url"))
        return EXIT_BAD_INPUT

    show_url = _ascii_safe_url(show.get("url") or show.get("detail_canonical"))
    session = build_session()

    log.info("Smoke-test: show=%d  perf=%s %s  cal=£%s",
             show_id, perf.get("date"), perf.get("time"),
             perf.get("verified_min_price"))
    log.info("  params: %s", params)
    log.info("  show URL (warmup): %s", show_url)

    try:
        wr = session.get(show_url, timeout=DEFAULT_TIMEOUT_S)
        log.info("  warmup → %d, %d bytes", wr.status_code, len(wr.text))
    except requests.RequestException as e:
        log.warning("warmup failed: %s", e)

    log.info("POST %s", BINDSEATPLAN_URL)
    try:
        r = session.post(BINDSEATPLAN_URL, data=params,
                         headers={**POST_HEADERS, "Referer": show_url or ""},
                         timeout=DEFAULT_TIMEOUT_S)
    except requests.RequestException as e:
        log.error("BindSeatPlan failed: %s", e)
        return EXIT_DRIFT
    log.info("  → %d, %d bytes, content-type=%s",
             r.status_code, len(r.text),
             r.headers.get("content-type", "?"))

    if save_html is not None:
        save_html.parent.mkdir(parents=True, exist_ok=True)
        save_html.write_text(r.text, encoding="utf-8")
        log.info("Saved raw response to %s", save_html)

    parsed = parse_bindseatplan(r.text)
    if parsed is None:
        log.error("Response did not parse as JSON.")
        log.error("First 400 bytes: %r", r.text[:400])
        return EXIT_DRIFT

    log.info("Parsed:")
    log.info("  ResultCode:   %s", parsed["result_code"])
    log.info("  selling tiers (PriceBands):  %s", parsed["tiers"])
    log.info("  face tiers:                  %s", parsed["face_tiers"])
    log.info("  seats in Performances:       %d", parsed["n_seats"])
    if parsed["tiers"] and perf.get("verified_min_price") is not None:
        agrees = abs(min(parsed["tiers"]) - perf["verified_min_price"]) <= AGREE_TOLERANCE_GBP
        log.info("  agrees with calendar:        %s%s",
                 agrees,
                 "" if agrees else "   <-- phantom-tier case (seat plan wins)")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
