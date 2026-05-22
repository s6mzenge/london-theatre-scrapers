"""
LoveTheatre per-performance availability refresher
==================================================

Second pass for `lovetheatre_scraper.py`. The main scraper already
calls `lovetheatre_calendar.enrich_show()` once during the full scrape
to attach per-perf prices and `max_seats` from the booking-flow
calendar API (`secure.lovetheatre.com/api/calendar/{show_id}/`). That
data is accurate at scrape time but goes stale within hours: the
calendar API removes today's performances around the time the box
office stops accepting new bookings (typically ~30–90 min before
showtime, sometimes earlier). The 22 May 2026 14:00 *Cursed Child*
matinee was the canonical case: at scrape time the calendar still
listed it at £17.50; an hour later the calendar response no longer
contained it at all, but the unified.json still said `available: true`
because the JSON-LD `availability` field on the perf was untouched.

How it works
------------
For every show in `lovetheatre.json` with a known booking flow, we:

1. Snapshot `price_source` on each existing performance (this tells
   us whether the perf was previously matched to a calendar entry).
2. Re-call `lovetheatre_calendar.enrich_show()`, which re-queries the
   calendar API and overwrites the per-perf calendar fields
   (`min_combined_price`, `no_singles_min_combined_price`,
   `special_offer`, `max_seats`, `price_source`).
3. For each perf within `--within-days` of today: if it was previously
   `calendar_api` or `calendar_only` but the fresh call returned
   `jsonld_only` (i.e. the calendar no longer lists it), treat that as
   a removal: set `max_seats = 0` and clear `availability`, so the
   dedupe layer's lovetheatre `available` lambda reports False instead
   of falling back to the now-stale JSON-LD InStock string.

Fields written / overwritten per perf
-------------------------------------
    min_combined_price             float | None    fresh from calendar API
    no_singles_min_combined_price  float | None
    special_offer                  bool  | None
    max_seats                      int   | None
    price_source                   str             one of:
        "calendar_api"             — fresh calendar entry matched this perf
        "calendar_only"            — fresh calendar entry, no JSON-LD twin
        "jsonld_only"              — calendar didn't return this perf
        "no_booking_flow"          — show has no bookable URL
    availability                   str   | None    set to "" when we detect
                                                   a calendar-removal in window
    verified_checked_at            str             UTC ISO timestamp (added)
    verified_calendar_removed      bool            True only if this run detected
                                                   the perf disappeared from
                                                   calendar within the window

The existing `price` / `book_url` fields are NOT modified. Dedupe's
lovetheatre `available` lambda already prefers `availability` when set
and falls back to `max_seats > 0` otherwise — clearing availability +
setting max_seats=0 produces a clean `available: false`.

Usage
-----
    python lovetheatre_availability.py                                # default file
    python lovetheatre_availability.py --in lovetheatre.json          # explicit input
    python lovetheatre_availability.py --within-days 2                # near-term only
    python lovetheatre_availability.py --concurrency 8                # parallel shows
    python lovetheatre_availability.py --limit 5                      # smoke-test
    python lovetheatre_availability.py --dry-run                      # don't write

Exit codes
----------
    0  success (partial fails are normal)
    1  bad input (file missing, malformed JSON)
    2  zero shows successfully re-queried despite >0 attempts — likely
       session warmup failed or the calendar API endpoint changed
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Reuse the calendar-API plumbing from the main scraper's enrichment
# module. This script is a thin orchestrator on top of it; all the HTTP
# parsing lives there and stays in one place.
import lovetheatre_calendar as ltc


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONCURRENCY = 8
DEFAULT_WITHIN_DAYS = 2
TIMEOUT_S = 15

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Outcome buckets — mirror the convention used in ttd_availability /
# olt_availability so the embedded report shape is consistent.
SOURCE_OK         = "calendar_api"
SOURCE_REMOVED    = "calendar_removed"
SOURCE_NOT_FOUND  = "not_in_calendar"   # perf has always been jsonld_only
SOURCE_FETCH_FAIL = "fetch_failed"
SOURCE_NO_FLOW    = "no_booking_flow"
SOURCE_SKIPPED    = "skipped"           # outside window or no date

EXIT_CLEAN     = 0
EXIT_BAD_INPUT = 1
EXIT_DRIFT     = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lovetheatre_availability")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class ShowOutcome:
    """Per-show summary, aggregated into the run summary at the end."""
    show_name: str
    enriched_count: int = 0       # perfs newly matched to calendar this run
    removed_count: int = 0        # perfs detected as calendar-removed (within window)
    untouched_count: int = 0      # perfs outside window or no booking flow
    status: str = SOURCE_OK       # SOURCE_OK / SOURCE_FETCH_FAIL / SOURCE_NO_FLOW
    error: str | None = None


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    """Build a requests.Session with sensible retries + UA. The
    `lovetheatre_calendar.warm_session()` call below installs the
    sessionid cookie that every /api/calendar/{id}/ call needs."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return s


# ---------------------------------------------------------------------------
# Per-show refresh
# ---------------------------------------------------------------------------

def _within_window(perf_date: str | None, today: datetime, within_days: int) -> bool:
    """True iff perf_date is between today and today+within_days inclusive.
    Returns False on missing/unparseable dates (we don't touch those)."""
    if not perf_date:
        return False
    try:
        d = datetime.strptime(perf_date, "%Y-%m-%d").date()
    except ValueError:
        return False
    today_d = today.date()
    return today_d <= d <= today_d + timedelta(days=within_days)


def refresh_show(
    session: requests.Session,
    show: dict,
    today: datetime,
    within_days: int,
) -> ShowOutcome:
    """Refresh per-perf calendar fields for a single show.

    Modifies `show["performances"]` in place. Returns a ShowOutcome
    that's aggregated into the run summary."""
    name = show.get("title") or show.get("name") or "<unknown>"
    outcome = ShowOutcome(show_name=name)
    perfs = show.get("performances") or []
    if not perfs:
        outcome.status = SOURCE_NO_FLOW
        return outcome

    # Snapshot old price_source per perf_id so we can detect transitions
    # from calendar_api/calendar_only -> jsonld_only after the re-enrichment
    # (= calendar dropped this perf).
    old_price_source: dict[str, str | None] = {}
    for p in perfs:
        pid = p.get("perf_id")
        if pid:
            old_price_source[pid] = p.get("price_source")

    # Run the same enrichment the main scraper uses. This overwrites the
    # calendar-derived fields on every perf and may append calendar-only
    # stubs at the tail. On API failure it stamps jsonld_only on every
    # perf (still safe — we just won't transition anyone to "removed").
    try:
        result = ltc.enrich_show(session, show)
    except requests.RequestException as e:
        outcome.status = SOURCE_FETCH_FAIL
        outcome.error = f"{type(e).__name__}: {e}"
        return outcome
    except Exception as e:  # noqa: BLE001
        outcome.status = SOURCE_FETCH_FAIL
        outcome.error = f"unexpected {type(e).__name__}: {e}"
        return outcome

    if result is None:
        # enrich_show returns None when the show has no booking flow at
        # all (no usable URL in any JSON-LD perf). Nothing to refresh.
        outcome.status = SOURCE_NO_FLOW
        outcome.untouched_count = len(perfs)
        return outcome

    matched, jsonld_only, cal_only = result
    outcome.enriched_count = matched

    # Walk the post-enrichment perfs. For each one within the window
    # that USED to be in the calendar but isn't anymore, mark it
    # explicitly unavailable so dedupe stops trusting the stale JSON-LD
    # InStock string.
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p in show.get("performances") or []:
        pid = p.get("perf_id")
        date = p.get("date")
        if not pid or not date:
            continue
        if not _within_window(date, today, within_days):
            outcome.untouched_count += 1
            continue

        new_source = p.get("price_source")
        old_source = old_price_source.get(pid)

        if new_source == "calendar_api":
            # Still in calendar — fields refreshed, nothing else to do.
            # (Counted into enriched_count above.)
            p["verified_calendar_removed"] = False
            p["verified_checked_at"] = now_iso
            continue

        if old_source in ("calendar_api", "calendar_only") and new_source == "jsonld_only":
            # WAS in the calendar at the last check, isn't anymore.
            # That's the staleness signal we exist to catch.
            p["max_seats"] = 0
            # Clear availability so dedupe's `_lovetheatre_available`
            # falls through to the max_seats path. Empty string is
            # falsy on `p.get("availability")` checks.
            p["availability"] = ""
            p["verified_calendar_removed"] = True
            p["verified_checked_at"] = now_iso
            outcome.removed_count += 1
        else:
            # Was already jsonld_only before, still jsonld_only now.
            # No transition — don't touch availability. (Could be a
            # perf the calendar API has never exposed; we have no
            # signal one way or the other.)
            p["verified_calendar_removed"] = False
            p["verified_checked_at"] = now_iso

    return outcome


# ---------------------------------------------------------------------------
# Run / aggregate
# ---------------------------------------------------------------------------

def run(
    payload: dict,
    concurrency: int,
    within_days: int,
    limit: int | None,
) -> dict:
    """Refresh every show in `payload` in place. Returns a summary dict
    that gets stamped onto `payload['report']['availability_verification']`."""
    t_start = time.monotonic()
    session = build_session()
    if not ltc.warm_session(session):
        # Calendar API requires a sessionid cookie obtained from a GET
        # of /. Without it every API call 403s. Bail loudly — the run
        # is useless and dedupe would just see stale data.
        log.error("Calendar session warmup failed — aborting (no shows refreshed)")
        return {
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "shows_attempted": 0,
            "shows_ok": 0,
            "shows_failed": 0,
            "perfs_enriched": 0,
            "perfs_removed": 0,
            "perfs_untouched": 0,
            "duration_seconds": round(time.monotonic() - t_start, 1),
            "error": "session_warmup_failed",
        }

    shows = payload.get("shows") or []
    if limit is not None:
        shows = shows[:limit]
        log.info("Limit set: only refreshing first %d shows", len(shows))

    today = datetime.now(timezone.utc)
    log.info(
        "Refreshing %d shows within %d days of %s (concurrency=%d)",
        len(shows), within_days, today.date().isoformat(), concurrency,
    )

    outcomes: list[ShowOutcome] = []
    lock = Lock()
    progress = [0]
    total = len(shows)

    def task(show: dict) -> ShowOutcome:
        outcome = refresh_show(session, show, today, within_days)
        with lock:
            progress[0] += 1
            if progress[0] % 25 == 0 or progress[0] == total:
                log.info("  progress: %d / %d shows", progress[0], total)
        return outcome

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(task, s) for s in shows]
        for f in as_completed(futures):
            outcomes.append(f.result())

    elapsed = time.monotonic() - t_start

    # Aggregate
    ok       = sum(1 for o in outcomes if o.status == SOURCE_OK)
    failed   = sum(1 for o in outcomes if o.status == SOURCE_FETCH_FAIL)
    no_flow  = sum(1 for o in outcomes if o.status == SOURCE_NO_FLOW)
    enriched = sum(o.enriched_count for o in outcomes)
    removed  = sum(o.removed_count  for o in outcomes)
    untouched= sum(o.untouched_count for o in outcomes)

    summary = {
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "within_days": within_days,
        "shows_attempted": len(outcomes),
        "shows_ok": ok,
        "shows_failed": failed,
        "shows_no_booking_flow": no_flow,
        "perfs_enriched": enriched,
        "perfs_removed": removed,
        "perfs_untouched": untouched,
        "duration_seconds": round(elapsed, 1),
    }
    log.info(
        "Done in %.1fs — %d shows OK, %d failed, %d no-flow; "
        "%d perfs enriched, %d perfs detected as calendar-removed",
        elapsed, ok, failed, no_flow, enriched, removed,
    )

    # Surface failed shows in the report so the next operator can see
    # which ones drifted. Cap at 20 to keep the embedded report small.
    failed_samples = [
        {"show": o.show_name, "error": o.error}
        for o in outcomes if o.status == SOURCE_FETCH_FAIL
    ][:20]
    if failed_samples:
        summary["failed_samples"] = failed_samples

    # Stamp the summary onto the existing report dict if present
    report = payload.setdefault("report", {})
    if isinstance(report, dict):
        report["availability_verification"] = summary

    # Also bump the top-level scraped_at so consumers can tell the file
    # was touched. We keep the original scraped_at under another key
    # for traceability.
    if "scraped_at" in payload and "scraped_at_full" not in payload:
        payload["scraped_at_full"] = payload["scraped_at"]
    payload["scraped_at"] = summary["scraped_at"]

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Refresh LoveTheatre per-performance availability by re-querying "
            "the booking-flow calendar API. Performances that have been "
            "removed from the calendar within the window are marked "
            "unavailable so the unified output stops sending users to "
            "now-closed booking pages."
        ),
    )
    p.add_argument(
        "--in", "-i", dest="in_path", type=Path,
        default=Path("lovetheatre.json"),
        help="Input JSON from lovetheatre_scraper.py (default: lovetheatre.json).",
    )
    p.add_argument(
        "--out", "-o", dest="out_path", type=Path, default=None,
        help="Output JSON path. Default: overwrite the input in place.",
    )
    p.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Parallel shows (default: {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "--within-days", type=int, default=DEFAULT_WITHIN_DAYS,
        help=(
            f"Only mark calendar-removals on performances scheduled within "
            f"the next N days (default: {DEFAULT_WITHIN_DAYS}). Calendar fields "
            "for ALL perfs are still refreshed; the window only governs which "
            "removals get flagged as availability changes. Near-term "
            "performances are the bug zone — the booking-flow calendar can "
            "transiently drop perfs months out and we don't want to mark "
            "those unavailable on transient blips."
        ),
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Refresh only the first N shows (smoke-test).",
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
        log.error("Input %s does not look like lovetheatre scraper output "
                  "(expected top-level dict with 'shows' list)", args.in_path)
        return EXIT_BAD_INPUT

    summary = run(
        payload,
        concurrency=args.concurrency,
        within_days=args.within_days,
        limit=args.limit,
    )

    # Drift detection: if we attempted any shows and ALL of them failed,
    # something systemic is wrong (calendar API down, IP blocked, schema
    # change). Exit non-zero so the workflow surfaces an alert without
    # also overwriting the input file with degraded data.
    if (summary["shows_attempted"] > 0
            and summary["shows_ok"] == 0
            and summary.get("error") != "session_warmup_failed"):
        log.error("Drift: 0 shows successfully refreshed — refusing to overwrite input")
        return EXIT_DRIFT

    if args.dry_run:
        log.info("--dry-run: not writing output")
        return EXIT_CLEAN

    out_path = args.out_path or args.in_path
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(out_path)
    log.info("Wrote %s", out_path)
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
