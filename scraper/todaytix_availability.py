#!/usr/bin/env python3
"""
todaytix_availability.py — full-catalogue live re-pricing (TodayTix JSON API)
=============================================================================

This is the second pass over the scraper's output. The first pass
(`todaytix_scraper.py`) records, per showtime, the cheapest *available*
band from the show page's __NEXT_DATA__ SSR snapshot in `low_price_value`
(it already sets it to None when sold out, using the rule
`showtime_seats == 0 or no available band`). That logic is correct — the
only defect is its **input**: the SSR snapshot is CloudFront-cached and lags
live inventory, so a showtime whose last cheap seat sold *after* the snapshot
still shows a price, and one that just went on sale may show None.

This pass closes that gap by running the *same* computation against the
**live** feed — one call per show to

    GET https://api.todaytix.com/api/v2/shows/{id}/showtimes?includeNoInventory=true

(cache-busted → CloudFront origin MISS, so it's as fresh as TodayTix exposes
without the per-seat /sections async map). Unlike the old browser chip pass it
is geo-robust (the API serves GBP from US CI runners — verified) and fast
(~200 small JSON calls, concurrent, a few seconds), and it re-prices the
**whole catalogue** every run rather than only "suspect" showtimes — closing
both the coverage gap (most showtimes were never checked) and the gating gap
(the old classifier relied on the stale SSR to decide what to check).

Per showtime it writes (consumed by analysis/dedupe.py):

    verified_min_price      float | None   cheapest band sellable as a block
                                           of MIN_CONTIGUOUS seats, live
    verified_max_price      float | None   most-expensive such band, live
    verified_candidates     list[float]    all available band prices, sorted
    verified_available      bool  | None   True / False / None(unknown)
    verified_price_source   str:
        "chips"        live API returned available priced bands → trust min/max
                       (kept named "chips" for dedupe back-compat; it means
                        "we have live prices")
        "sold_out"     live API checked it and it's unbuyable (every band zero,
                       or booking closed past availableUntil) → dedupe shows NO
                       price (drops it from the cross-source min, like SeatPlan
                       "no_seats") and marks it unavailable, instead of
                       advertising the stale SSR floor
        "fetch_failed" API errored / showtime absent / no per-band seat info
                       (schema drift) → dedupe falls back to the scraper's SSR
                       low_price_value
    verified_reason         str            "catalogue_recheck"
    verified_note           str            short diagnostic
    verified_url            str            the API URL that was checked
    verified_checked_at     str            UTC ISO timestamp

The critical correctness point: full coverage is only safe because "checked,
sold out" (→ show nothing) is distinguished from "couldn't check" (→ SSR
fallback). Without that split, every genuinely-sold-out performance on every
popular show would fall back to a stale cheap price — the very bug this avoids.

Quantity awareness: a band is only counted toward the displayed "from" price
if its maxContiguousSeats >= the booking link's quantity (MIN_CONTIGUOUS,
which mirrors todaytix_scraper's qt=2 book_url). This drops lone single seats
that the qt=2 seating-plan page cannot sell — e.g. a £23 restricted single
sitting under a £39 pair floor — so the quoted price is one the click-through
can actually honour.

Residual gaps (not closeable from this endpoint, by design):
  * up to ~15 min between-run staleness (any snapshot system has this)
  * held-in-cart seats counted as available
  * rush/lottery tickets that live outside regularTickets
These are documented, not silently ignored.

Usage:
    python todaytix_availability.py --in todaytix_london.json
    python todaytix_availability.py --in todaytix_london.json --workers 12
    python todaytix_availability.py --in todaytix_london.json --window-days 60
    python todaytix_availability.py --selftest        # offline, no TodayTix
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

try:                                              # urllib3 ships with requests
    from urllib3.util.retry import Retry
except Exception:                                 # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

log = logging.getLogger("todaytix_availability")

# --- constants --------------------------------------------------------------

SHOWTIMES_URL = "https://api.todaytix.com/api/v2/shows/{show_id}/showtimes"
REQUEST_TIMEOUT = 20.0
DEFAULT_WORKERS = 10
DEFAULT_WINDOW_DAYS = 0           # 0 / negative = re-price every date

# A price band only counts toward the displayed "from" price if it can seat a
# contiguous block of this size. It MUST match the quantity baked into
# todaytix_scraper._build_booking_url (qt=2): the seating-plan page we link to
# only sells seats in a contiguous run, so a band with seats but a smaller
# maxContiguousSeats (a lone single, e.g. BoM's £23) is real but unbuyable
# through our link, and quoting it advertises a floor the click-through can't
# honour. Set to 1 to revert to "cheapest single seat" semantics.
MIN_CONTIGUOUS = 2

# verified_price_source values — MUST match dedupe.py's checks.
SRC_LIVE = "chips"                # live prices available; trust verified_*
SRC_SOLD_OUT = "sold_out"         # checked live, unbuyable → no TT price
SRC_FETCH_FAILED = "fetch_failed" # couldn't check → SSR fallback

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.todaytix.com/",
    "Origin": "https://www.todaytix.com",
    # /showtimes varies on X-TT-Currency; US CI runners return GBP anyway
    # (verified) but pin it so a price can never silently arrive in USD.
    "X-TT-Currency": "GBP",
}


# --- helpers ----------------------------------------------------------------

def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}),
                  raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def evaluate_showtime(api_st: dict, now_utc: datetime) -> dict:
    """Run todaytix_scraper._build_showtime's price logic on a LIVE showtime.

    Returns {source, min, max, candidates, available, seats, note}.
    """
    regular = (api_st or {}).get("regularTickets") or {}
    showtime_seats = regular.get("numAssignedSeatsAvailable")
    bands = regular.get("priceBands") or []

    bands_with_seat_info = [b for b in bands
                            if b.get("numAssignedSeatsAvailable") is not None]
    if not bands_with_seat_info:
        # Same schema-drift guard the scraper uses: with no per-band seat
        # data we can't tell sold-out from not-yet-on-sale, so we don't
        # claim either — fall back to the scraper's SSR value.
        return {"source": SRC_FETCH_FAILED, "min": None, "max": None,
                "candidates": [], "available": None, "seats": showtime_seats,
                "note": "no per-band seat info in live API (schema fallback)"}

    # Bands with at least one seat AND a price. Used only to distinguish a
    # genuine sell-out from "sold out as a pair" below.
    available_any = [b for b in bands_with_seat_info
                     if (b.get("numAssignedSeatsAvailable") or 0) > 0
                     and (b.get("price") or {}).get("value") is not None]

    # Quantity-aware narrowing: keep only bands that can seat MIN_CONTIGUOUS
    # together (the qt baked into the book_url we hand users). A band with
    # seats but maxContiguousSeats < MIN_CONTIGUOUS is a lone single the qt=2
    # seating-plan page cannot sell — quoting its price would advertise a floor
    # the click-through can't honour (the £23-single-vs-£39-pair case).
    # maxContiguousSeats absent => treat as passing (schema fallback), so a TT
    # field rename can't silently zero out every showtime.
    available = [b for b in available_any
                 if b.get("maxContiguousSeats") is None
                 or (b.get("maxContiguousSeats") or 0) >= MIN_CONTIGUOUS]

    closed = False
    cutoff = _parse_iso(regular.get("availableUntil"))
    if cutoff is not None and now_utc > cutoff:
        closed = True

    if showtime_seats == 0 or not available_any or closed:
        if closed and available_any:
            note = "booking closed (availableUntil passed)"
        elif showtime_seats == 0:
            note = "sold out (showtime seats = 0)"
        else:
            note = "sold out (no available band)"
        return {"source": SRC_SOLD_OUT, "min": None, "max": None,
                "candidates": [], "available": False, "seats": showtime_seats,
                "note": note}

    if not available:
        # Seats exist, but none as a contiguous block of MIN_CONTIGUOUS, so the
        # qt=MIN_CONTIGUOUS link can't complete a purchase. Treat as sold out
        # for display rather than advertising a single-only price nobody
        # booking the default quantity can buy.
        return {"source": SRC_SOLD_OUT, "min": None, "max": None,
                "candidates": [], "available": False, "seats": showtime_seats,
                "note": f"only single seats (no {MIN_CONTIGUOUS}-seat block)"}

    prices = sorted({float((b.get("price") or {}).get("value")) for b in available})
    return {"source": SRC_LIVE, "min": prices[0], "max": prices[-1],
            "candidates": prices, "available": True, "seats": showtime_seats,
            "note": f"{len(available)} band(s) seating >= {MIN_CONTIGUOUS}"}


def fetch_show(session: requests.Session, show_id: int,
               cache_bust: bool, timeout: float) -> tuple:
    """Return (show_id, ok, {showtime_id: api_showtime}, note)."""
    params = {"includeNoInventory": "true"}
    headers = {}
    if cache_bust:
        params["_cb"] = str(random.randint(1, 10 ** 9))
        headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    try:
        r = session.get(SHOWTIMES_URL.format(show_id=show_id),
                        params=params, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return show_id, False, {}, f"HTTP {r.status_code}"
        data = (r.json() or {}).get("data") or []
        m = {st.get("id"): st for st in data if st.get("id") is not None}
        return show_id, True, m, f"{len(m)} showtime(s)"
    except Exception as e:                        # network / JSON / etc.
        return show_id, False, {}, f"{type(e).__name__}: {str(e)[:80]}"


# --- main pass --------------------------------------------------------------

def verify(payload: dict, *, workers: int, window_days: int, cache_bust: bool,
           dry_run: bool, fetch_fn=fetch_show, session: requests.Session | None = None
           ) -> dict:
    t0 = time.time()
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    shows = payload.get("shows") or []

    # one fetch per unique show id
    show_ids: list[int] = []
    seen: set[int] = set()
    for show in shows:
        sid = show.get("id")
        if isinstance(sid, int) and sid not in seen:
            seen.add(sid)
            show_ids.append(sid)

    if session is None:
        session = build_session()

    results: dict[int, tuple] = {}
    log.info("Re-pricing %d show(s) via live API (workers=%d, cache_bust=%s)…",
             len(show_ids), workers, cache_bust)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(fetch_fn, session, sid, cache_bust, REQUEST_TIMEOUT): sid
                for sid in show_ids}
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                _sid, ok, m, note = fut.result()
            except Exception as e:                # pragma: no cover
                ok, m, note = False, {}, f"{type(e).__name__}"
            results[sid] = (ok, m, note)

    shows_fetched = sum(1 for ok, _, _ in results.values() if ok)
    shows_failed = len(results) - shows_fetched

    upper: date | None = None
    if window_days and window_days > 0:
        upper = (now_utc + timedelta(days=window_days)).date()

    counts = {SRC_LIVE: 0, SRC_SOLD_OUT: 0, SRC_FETCH_FAILED: 0}
    checked = corrected = skipped_window = 0

    for show in shows:
        sid = show.get("id")
        ok, m, note = results.get(sid, (False, {}, "show not fetched"))
        for st in (show.get("showtimes") or []):
            if upper is not None:
                d = _parse_date(st.get("local_date"))
                if d is not None and d > upper:
                    skipped_window += 1
                    continue

            stid = st.get("showtime_id")
            if not ok:
                res = {"source": SRC_FETCH_FAILED, "min": None, "max": None,
                       "candidates": [], "available": None,
                       "seats": st.get("seats_available"),
                       "note": f"show fetch failed: {note}"}
            elif stid is None or stid not in m:
                res = {"source": SRC_FETCH_FAILED, "min": None, "max": None,
                       "candidates": [], "available": None,
                       "seats": st.get("seats_available"),
                       "note": "showtime absent from live API"}
            else:
                res = evaluate_showtime(m[stid], now_utc)

            counts[res["source"]] = counts.get(res["source"], 0) + 1
            checked += 1

            ssr = st.get("low_price_value")
            if res["source"] == SRC_LIVE and res["min"] is not None \
                    and ssr is not None and abs(res["min"] - ssr) > 1e-9:
                corrected += 1
            elif res["source"] == SRC_SOLD_OUT and ssr is not None:
                corrected += 1                    # SSR advertised a price; gone

            if not dry_run:
                st["verified_min_price"] = res["min"]
                st["verified_max_price"] = res["max"]
                st["verified_candidates"] = res["candidates"]
                st["verified_available"] = res["available"]
                st["verified_price_source"] = res["source"]
                st["verified_reason"] = "catalogue_recheck"
                st["verified_note"] = res["note"]
                st["verified_url"] = (SHOWTIMES_URL.format(show_id=sid)
                                      if sid is not None else None)
                st["verified_checked_at"] = now_iso

    elapsed = time.time() - t0
    log.info("Done in %.1fs — shows %d/%d fetched; showtimes: available=%d, "
             "sold_out=%d, fetch_failed=%d; corrected_vs_ssr=%d%s",
             elapsed, shows_fetched, len(show_ids), counts[SRC_LIVE],
             counts[SRC_SOLD_OUT], counts[SRC_FETCH_FAILED], corrected,
             (f"; skipped_outside_window={skipped_window}" if upper else ""))

    summary = {
        "verified_at": now_iso,
        "engine": "api-full-catalogue",
        "cache_busted": cache_bust,
        "window_days": window_days,
        "workers": workers,
        "shows_total": len(show_ids),
        "shows_fetched": shows_fetched,
        "shows_failed": shows_failed,
        "showtimes_checked": checked,
        "available": counts[SRC_LIVE],
        "sold_out": counts[SRC_SOLD_OUT],
        "fetch_failed": counts[SRC_FETCH_FAILED],
        "skipped_outside_window": skipped_window,
        "corrected_vs_ssr": corrected,
        "duration_seconds": round(elapsed, 1),
        # backward-compatible aliases (old summary keys)
        "ok": counts[SRC_LIVE],
        "no_chips": counts[SRC_SOLD_OUT],
    }
    report = payload.setdefault("report", {})
    if isinstance(report, dict):
        report["availability_verification"] = summary
    return summary


# --- offline selftest -------------------------------------------------------

def _band(price, seats, contig=None):
    b = {"numAssignedSeatsAvailable": seats}
    if contig is not None:
        b["maxContiguousSeats"] = contig
    if price is not None:
        b["price"] = {"value": price, "currency": "GBP", "display": f"£{price:g}"}
    return b


def selftest() -> int:
    print("SELFTEST: live-showtime evaluation + end-to-end re-price (no TodayTix)\n")
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # (a) available bands → chips, cheapest available wins
    r = evaluate_showtime({"regularTickets": {"numAssignedSeatsAvailable": 9,
        "availableUntil": future, "priceBands": [
            _band(45.0, 3), _band(25.0, 0), _band(60.0, 6)]}}, now)
    assert r["source"] == SRC_LIVE and r["min"] == 45.0 and r["max"] == 60.0, r
    assert r["available"] is True and r["candidates"] == [45.0, 60.0], r

    # (b) every band zero seats → sold_out
    r = evaluate_showtime({"regularTickets": {"numAssignedSeatsAvailable": 0,
        "priceBands": [_band(45.0, 0), _band(60.0, 0)]}}, now)
    assert r["source"] == SRC_SOLD_OUT and r["min"] is None and r["available"] is False, r

    # (c) bands have seats but booking window has closed → sold_out
    r = evaluate_showtime({"regularTickets": {"numAssignedSeatsAvailable": 5,
        "availableUntil": past, "priceBands": [_band(45.0, 5)]}}, now)
    assert r["source"] == SRC_SOLD_OUT and "closed" in r["note"], r

    # (d) no per-band seat info → schema fallback (NOT sold_out)
    r = evaluate_showtime({"regularTickets": {"numAssignedSeatsAvailable": None,
        "priceBands": [{"price": {"value": 30.0}}]}}, now)
    assert r["source"] == SRC_FETCH_FAILED, r

    # (e) empty regularTickets → schema fallback
    r = evaluate_showtime({}, now)
    assert r["source"] == SRC_FETCH_FAILED, r

    # (f) cheapest band is a lone single (maxContiguousSeats=1): excluded for a
    #     qty>=2 booking, so the floor rises to the cheapest pair-able band.
    #     This is the live BoM £23-single / £39-pair case.
    r = evaluate_showtime({"regularTickets": {"numAssignedSeatsAvailable": 30,
        "availableUntil": future, "priceBands": [
            _band(23.0, 1, contig=1), _band(39.0, 28, contig=8),
            _band(150.0, 14, contig=7)]}}, now)
    assert r["source"] == SRC_LIVE and r["min"] == 39.0 and r["max"] == 150.0, r
    assert 23.0 not in r["candidates"], r

    # (g) every priced band is single-only → nothing sellable as a pair → sold_out
    r = evaluate_showtime({"regularTickets": {"numAssignedSeatsAvailable": 3,
        "availableUntil": future, "priceBands": [
            _band(23.0, 1, contig=1), _band(30.0, 2, contig=1)]}}, now)
    assert r["source"] == SRC_SOLD_OUT and "single" in r["note"].lower(), r

    # (h) maxContiguousSeats absent on every band → schema fallback: don't
    #     narrow (a TT field rename must not zero out the catalogue).
    r = evaluate_showtime({"regularTickets": {"numAssignedSeatsAvailable": 9,
        "availableUntil": future, "priceBands": [
            _band(25.0, 3), _band(60.0, 6)]}}, now)
    assert r["source"] == SRC_LIVE and r["min"] == 25.0, r
    print("  evaluate_showtime: chips / sold_out / closed / schema-fallback / "
          "qty-contiguous OK")

    # end-to-end: a payload + a fake fetcher (no network)
    payload = {"shows": [{"id": 302, "slug": "x", "showtimes": [
        {"showtime_id": 1, "local_date": future[:10], "low_price_value": 23.0,
         "seats_available": 5},                       # live: available @ £19
        {"showtime_id": 2, "local_date": future[:10], "low_price_value": 30.0,
         "seats_available": 2},                       # live: sold out
        {"showtime_id": 3, "local_date": future[:10], "low_price_value": 40.0,
         "seats_available": 4},                       # absent from live API
    ]}]}
    fake_api = {
        1: {"regularTickets": {"numAssignedSeatsAvailable": 5,
            "priceBands": [_band(19.0, 2), _band(55.0, 3)]}},
        2: {"regularTickets": {"numAssignedSeatsAvailable": 0,
            "priceBands": [_band(30.0, 0)]}},
        # 3 deliberately omitted → "absent from live API"
    }

    def fake_fetch(_session, show_id, _bust, _timeout):
        return show_id, True, fake_api, "stub"

    s = verify(payload, workers=2, window_days=0, cache_bust=True,
               dry_run=False, fetch_fn=fake_fetch, session=object())
    sts = {st["showtime_id"]: st for st in payload["shows"][0]["showtimes"]}
    assert sts[1]["verified_price_source"] == SRC_LIVE and sts[1]["verified_min_price"] == 19.0
    assert sts[1]["verified_available"] is True
    assert sts[2]["verified_price_source"] == SRC_SOLD_OUT and sts[2]["verified_min_price"] is None
    assert sts[2]["verified_available"] is False
    assert sts[3]["verified_price_source"] == SRC_FETCH_FAILED and sts[3]["verified_available"] is None
    assert s["available"] == 1 and s["sold_out"] == 1 and s["fetch_failed"] == 1
    # corrected: #1 (23→19) and #2 (had £30, now sold out) = 2
    assert s["corrected_vs_ssr"] == 2, s
    assert payload["report"]["availability_verification"]["engine"] == "api-full-catalogue"
    print("  end-to-end verify(): available/sold_out/fetch_failed + corrected OK")

    # request mechanics (allowed domain), best-effort
    try:
        r = build_session().get("https://pypi.org/pypi/requests/json", timeout=15)
        print(f"  request mechanics OK (GET pypi -> {r.status_code})")
    except Exception as e:
        print(f"  (network unavailable here: {e}) — logic still validated")
    print("\nSELFTEST: PASS")
    return 0


# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Re-price the whole TodayTix catalogue against the live "
                     "JSON API, writing verified_* prices and an authoritative "
                     "sold-out flag onto every showtime."))
    p.add_argument("--selftest", action="store_true",
                   help="Run offline self-tests (no TodayTix access) and exit.")
    p.add_argument("--in", "-i", dest="in_path", type=Path,
                   help="Path to the scraper's JSON output (e.g. todaytix_london.json).")
    p.add_argument("--out", "-o", dest="out_path", type=Path, default=None,
                   help="Where to write the updated JSON (default: in place).")
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                   help="Only re-price showtimes within this many days "
                        "(default 0 = every date).")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"Concurrent API fetches (default {DEFAULT_WORKERS}).")
    p.add_argument("--no-cache-bust", action="store_true",
                   help="Allow CloudFront cache hits (≤~5 min stale) instead of "
                        "forcing origin-fresh fetches.")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and report, but don't write verified_* fields.")
    # Accepted-but-ignored, so the old browser-pass invocation still runs:
    p.add_argument("--chip-cache", type=Path, default=None,
                   help="(ignored) full-catalogue API pass re-prices fresh each run.")
    p.add_argument("--chip-cache-ttl-hours", type=int, default=24,
                   help="(ignored)")
    p.add_argument("--cheap-band-threshold", type=int, default=3,
                   help="(ignored in full-catalogue mode)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.selftest:
        return selftest()
    if not args.in_path:
        p.error("--in/-i is required (or use --selftest)")

    if args.chip_cache is not None:
        log.info("Note: --chip-cache is ignored — the full-catalogue API pass "
                 "re-prices every showtime fresh each run.")

    try:
        payload = json.loads(args.in_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("Could not read %s: %s", args.in_path, e)
        return 2
    if not isinstance(payload, dict) or not isinstance(payload.get("shows"), list):
        log.error("Input %s is not a TodayTix scrape (missing 'shows' list).", args.in_path)
        return 2

    verify(payload, workers=args.workers, window_days=args.window_days,
           cache_bust=not args.no_cache_bust, dry_run=args.dry_run)

    if args.dry_run:
        log.info("Dry run — no file written.")
        return 0

    out_path = args.out_path or args.in_path
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_path)
    log.info("Wrote %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
