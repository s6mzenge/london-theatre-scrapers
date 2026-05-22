"""
TodayTix per-performance availability verifier
==============================================

Second pass for `todaytix_scraper.py`. The main scraper reads each
showtime's price from the show listing page's `__NEXT_DATA__` SSR
snapshot — specifically `initialShowtimes[].regularTickets.priceBands`.
That snapshot lags live inventory: when the cheapest band's last few
seats sell out between snapshot generation and a real user clicking
through, the booking page shows a higher floor than what we recorded.

The smoking gun is in the snapshot itself: the cheapest available
band's own `seats_available` count. Every observed drift case shows
**1 seat remaining** on the cheapest band — i.e. the snapshot is one
click away from being wrong. So we don't need to verify all 16K
showtimes; we verify only the ~200 that are obviously about to flip,
within the next two weeks (where it matters most for users).

Suspect heuristic
-----------------
A showtime is verified iff ALL of:
  * `local_date` is within `--window-days` (default 14)
  * the cheapest band with seats has `seats_available < N`
    (default 3 — catches 1- and 2-seat cases, both flippable)
  * a `booking_url` is present (otherwise nothing to verify)

This produces ~200 suspect showtimes on a typical scrape, finishing
in ~13 min at 1 worker.

Pipeline
--------
1. Walk `payload["shows"][i]["showtimes"][j]`, classify each
2. Open each suspect's `booking_url` in headless Chromium
3. Wait for prices to render, scan rendered text for £-amounts,
   take min and max
4. Write `verified_*` fields back onto the showtime in place
5. `dedupe.py` prefers `verified_min_price` over the SSR `low_price_value`
   when the chip pass succeeded

Fields added per showtime (only the verified ones)
--------------------------------------------------
    verified_min_price          float | None     cheapest rendered chip
    verified_max_price          float | None     most-expensive rendered chip
    verified_candidates         list[float]      all plausible chips seen
    verified_price_source       str:
        "chips"          — chip min/max extracted, trust these
        "no_chips_found" — page loaded but no plausible £-amount
        "fetch_failed"   — browser navigation or render error
    verified_reason             str:             "thin_cheap_band"
    verified_note               str              short diagnostic
    verified_url                str              URL that was fetched
    verified_checked_at         str              UTC ISO timestamp

Why Playwright (and why not API)
--------------------------------
TT's booking page DOES NOT carry live pricing in its server-rendered
`__NEXT_DATA__`. It only has a marketing "from £X" floor. The actual
chip values are fetched client-side via XHR and rendered into the
page. We could reverse-engineer the XHR endpoint, but that's a
fragile contract; visible-text scan is robust to layout changes.

Bot-wall mitigations
--------------------
TT is sensitive to fingerprint-based detection. The verifier uses
the same mitigations as the proven batch verifier:
  * One long-lived context per worker (preserves Cloudflare clearance
    cookies — fresh-per-page made the wall worse, not better)
  * Basic stealth init script (navigator.webdriver, plugins, chrome,
    languages — no external dep)
  * Realistic Europe/London timezone matching the locale
  * `--disable-blink-features=AutomationControlled` launch flag
  * Homepage pre-warm to look like real navigation
  * `seats_available`-filtered request rate (~200 reqs/run not 16K)
  * Default 1 worker; bump to 2 cautiously if your network passes

Usage
-----
    python todaytix_availability.py --in scraper/data/todaytix.json
    python todaytix_availability.py --window-days 30         # broader
    python todaytix_availability.py --cheap-band-threshold 2 # tighter
    python todaytix_availability.py --workers 2              # faster
    python todaytix_availability.py --dry-run

Dependencies
------------
    pip install playwright
    python -m playwright install chromium

Exit codes
----------
    0  success (some or all suspect showtimes verified)
    1  bad input (file missing, malformed JSON)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_DAYS          = 14
DEFAULT_CHEAP_BAND_THRESHOLD = 3
# Two workers is the cautious bump. Earlier in this codebase's history
# we found that more workers + fresh-context-per-page made the
# Cloudflare bot wall worse (clearance cookies lost). Keeping the
# long-lived contexts and staggering worker startup (see _worker
# below — each worker waits worker_index * WORKER_START_STAGGER_S
# before its first request) lets us double throughput without
# punching Cloudflare in the face. If we see fetch_failed spike in
# practice, drop back to 1.
DEFAULT_WORKERS              = 2
WORKER_START_STAGGER_S       = 2.0   # second worker waits 2s before prewarm

NAV_TIMEOUT_MS               = 30_000
FIRST_PRICE_TIMEOUT_MS       = 12_000   # safety net; rarely hit
STABILITY_POLL_S             = 0.25
STABILITY_POLLS              = 2        # 2 × 250ms = 500ms unchanged
MAX_WAIT_S                   = 2.5      # hard cap; TT chips usually settle <1s
INTER_PAGE_SLEEP_S           = 2.0      # within-worker pacing (proven floor)
PREWARM_URL                  = "https://www.todaytix.com/london"

# Plausible per-ticket price range.
PRICE_MIN = 5.0
PRICE_MAX = 600.0

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
PRICE_RE = re.compile(r"£\s*(\d{1,4}(?:\.\d{1,2})?)")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Basic stealth — same patch as SP's chip pass. Manual rather than
# pulling in playwright-stealth as a dep.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
"""

# verified_price_source values — must match dedupe.py's checks.
PRICE_SOURCE_OK         = "chips"
PRICE_SOURCE_NO_CHIPS   = "no_chips_found"
PRICE_SOURCE_FETCH_FAIL = "fetch_failed"

EXIT_CLEAN     = 0
EXIT_BAD_INPUT = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("todaytix-avail")


# ---------------------------------------------------------------------------
# Suspect classifier
# ---------------------------------------------------------------------------

def _classify_suspect(st: dict, today: date, window_days: int,
                      threshold: int) -> str | None:
    """Return 'thin_cheap_band' if suspect, else None. Verified at scale
    against every known TT drift case: all have seats_available = 1 on
    the cheapest band per the SSR snapshot."""
    ds = st.get("local_date")
    if not ds:
        return None
    try:
        pd = date.fromisoformat(ds)
    except ValueError:
        return None
    if pd < today:
        return None
    if pd > today + timedelta(days=window_days):
        return None
    bands = st.get("price_bands") or []
    avail = [b for b in bands
             if (b.get("seats_available") or 0) > 0
             and b.get("price_value") is not None]
    if not avail:
        return None
    cheapest = min(avail, key=lambda b: b["price_value"])
    if (cheapest.get("seats_available") or 0) < threshold:
        return "thin_cheap_band"
    return None


# ---------------------------------------------------------------------------
# Playwright extraction
# ---------------------------------------------------------------------------

async def _block_heavy(route) -> None:
    try:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


# JS predicate — "page has a plausible £-amount in body text".
# %-formatted to avoid clashing with the JS curly braces.
_FIRST_PRICE_JS = """() => {
  const re = /£\\s*(\\d{1,4}(?:\\.\\d{1,2})?)/g;
  const t = document.body.innerText || '';
  let m;
  while ((m = re.exec(t)) !== null) {
    const v = parseFloat(m[1]);
    if (v >= %d && v <= %d) return true;
  }
  return false;
}""" % (int(PRICE_MIN), int(PRICE_MAX))


async def _extract_one(context, url: str
                       ) -> tuple[float | None, float | None,
                                  list[float], str]:
    """Open a new page in `context`, navigate, wait smartly, scan.
    Returns (chip_min, chip_max, all_plausible_candidates, note)."""
    from playwright.async_api import TimeoutError as PWTimeout

    page = await context.new_page()
    text = ""
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            return None, None, [], f"nav error — {type(e).__name__}: {e}"

        try:
            await page.wait_for_function(_FIRST_PRICE_JS,
                                          timeout=FIRST_PRICE_TIMEOUT_MS)
        except PWTimeout:
            # Continue anyway — the post-scan will report no-price.
            pass

        # Stability poll: finish when the candidate set has been
        # unchanged for STABILITY_POLLS consecutive polls.
        prev: frozenset[float] | None = None
        stable = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MAX_WAIT_S
        while loop.time() < deadline:
            try:
                text = await page.evaluate("document.body.innerText") or ""
            except Exception:
                break
            cands = frozenset(
                float(m.group(1)) for m in PRICE_RE.finditer(text)
                if PRICE_MIN <= float(m.group(1)) <= PRICE_MAX
            )
            if cands == prev and len(cands) > 0:
                stable += 1
                if stable >= STABILITY_POLLS:
                    break
            else:
                stable = 0
                prev = cands
            await asyncio.sleep(STABILITY_POLL_S)
    finally:
        try: await page.close()
        except Exception: pass

    raw = [float(m.group(1)) for m in PRICE_RE.finditer(text)]
    valid = sorted({p for p in raw if PRICE_MIN <= p <= PRICE_MAX})
    if not valid:
        return None, None, [], (
            f"no prices in plausible range "
            f"({PRICE_MIN:.0f}-{PRICE_MAX:.0f}); "
            f"saw {len(raw)} raw match(es)"
        )
    return valid[0], valid[-1], valid, "chips"


async def _prewarm(context) -> None:
    """Visit the public homepage to establish Cloudflare clearance
    cookies before any booking-page request. Non-fatal on failure."""
    page = await context.new_page()
    try:
        await page.goto(PREWARM_URL, wait_until="domcontentloaded",
                        timeout=20_000)
        await page.wait_for_timeout(2000)
    except Exception:
        pass
    finally:
        try: await page.close()
        except Exception: pass


async def _worker(name: str, worker_index: int, browser,
                  queue: asyncio.Queue,
                  results: dict, lock: asyncio.Lock,
                  counter: list, total: int) -> None:
    """One long-lived context per worker. Pre-warms once, then pulls
    items from the shared queue until empty.

    Workers stagger their startup by `worker_index * WORKER_START_STAGGER_S`
    seconds — without this, N workers prewarm in parallel and N
    simultaneous fresh Chrome fingerprints hitting Cloudflare's
    challenge in the same instant looks more bot-like than one. The
    stagger lets the first worker clear the challenge cleanly before
    the second one starts, then both run concurrently from then on."""
    if worker_index > 0:
        await asyncio.sleep(worker_index * WORKER_START_STAGGER_S)
    context = await browser.new_context(
        viewport={"width": 1400, "height": 900},
        user_agent=UA,
        locale="en-GB",
        timezone_id="Europe/London",
        extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
    )
    await context.route("**/*", _block_heavy)
    await context.add_init_script(STEALTH_JS)
    await _prewarm(context)
    try:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            min_p, max_p, candidates, note = await _extract_one(
                context, item["url"]
            )
            async with lock:
                counter[0] += 1
                results[item["key"]] = (min_p, max_p, candidates, note)
                if counter[0] % 10 == 0 or counter[0] == total:
                    log.info("  progress: %d/%d", counter[0], total)
            await asyncio.sleep(INTER_PAGE_SLEEP_S)
    finally:
        try: await context.close()
        except Exception: pass


async def _run_async(suspect_items: list[dict], workers: int) -> dict:
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
            log.error("could not launch Chromium — %s", e)
            log.error("  run: python -m playwright install chromium")
            return results

        queue: asyncio.Queue = asyncio.Queue()
        for item in suspect_items:
            queue.put_nowait(item)
        lock = asyncio.Lock()
        tasks = [
            asyncio.create_task(
                _worker(f"w{i+1}", i, browser, queue, results,
                        lock, counter, total)
            )
            for i in range(workers)
        ]
        await asyncio.gather(*tasks, return_exceptions=False)
        try: await browser.close()
        except Exception: pass
    return results


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(payload: dict, window_days: int, threshold: int,
        workers: int,
        cache_path: Path | None = None,
        cache_ttl_hours: int = 24) -> dict:
    """Walk all shows × showtimes, classify suspects, run the chip
    pass, write verified_* fields. Returns a summary for the report
    block.

    Cache: when `cache_path` is given, suspect showtimes whose
    catalogue inputs haven't changed since the last run skip the
    Playwright extraction and reuse cached chip values. See
    chip_pass_cache.py for the cache contract. The catalogue inputs
    hashed for TT are `low_price_value` (the SSR snapshot's cheapest)
    and the cheapest available band's price + seats_available — these
    are exactly the fields that drive the suspect classifier, so any
    change to them invalidates the cached result."""
    if not isinstance(payload, dict):
        return {"suspect_count": 0, "ok": 0, "no_chips": 0,
                "fetch_failed": 0, "duration_seconds": 0.0,
                "cache_hits": 0, "cache_misses": 0}

    # Cache setup — fully optional.
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

    today = date.today()
    suspects = []
    for s_idx, show in enumerate(payload.get("shows", []) or []):
        for st_idx, st in enumerate(show.get("showtimes") or []):
            reason = _classify_suspect(st, today, window_days, threshold)
            if reason is None:
                continue
            url = st.get("booking_url")
            if not url:
                continue
            # Extract the cheapest-available band's price/seats for
            # input hashing. If the band data changes, the chip pass
            # would re-classify and re-extract — so invalidate.
            bands = st.get("price_bands") or []
            avail = [b for b in bands
                     if (b.get("seats_available") or 0) > 0
                     and b.get("price_value") is not None]
            cheapest = min(avail, key=lambda b: b["price_value"]) if avail else None
            suspects.append({
                "key":    (s_idx, st_idx),
                "url":    url,
                "reason": reason,
                "show":   show.get("slug") or "?",
                "date":   st.get("local_date"),
                "time":   st.get("local_time"),
                "_input_low_price":    st.get("low_price_value"),
                "_input_cheap_price":  cheapest.get("price_value") if cheapest else None,
                "_input_cheap_seats":  cheapest.get("seats_available") if cheapest else None,
            })

    log.info(
        "Identified %d suspect showtimes (cheap band <%d seats, within %dd)",
        len(suspects), threshold, window_days,
    )

    if not suspects:
        return {"suspect_count": 0, "ok": 0, "no_chips": 0,
                "fetch_failed": 0, "duration_seconds": 0.0,
                "cache_hits": 0, "cache_misses": 0}

    # Cache lookup: partition into hits and misses.
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
                s["_input_cheap_price"],
                s["_input_cheap_seats"],
            )
            entry = cache_entries.get(ckey)
            if cache_mod.is_hit(entry, input_hash, ttl_hours=cache_ttl_hours):
                results[s["key"]] = (
                    entry["chip_min"], entry["chip_max"],
                    entry["candidates"], entry["note"],
                )
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
        suspects_to_run = suspects
        cache_misses = len(suspects)

    log.info("Chip cache: %d hits, %d misses (running extractor on %d rows)",
             cache_hits, cache_misses, len(suspects_to_run))

    est_min = max(1, len(suspects_to_run) * 4 // (max(workers, 1) * 60))
    log.info("Starting chip pass: %d showtime(s), %d worker(s), "
             "est. ~%d min", len(suspects_to_run), workers, est_min)

    t0 = time.monotonic()
    if suspects_to_run:
        miss_results = asyncio.run(_run_async(suspects_to_run, workers))
        results.update(miss_results)
    elapsed = time.monotonic() - t0

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok = no_chips = fetch_failed = 0
    cache_writes = 0
    for s in suspects:
        min_p, max_p, candidates, note = results.get(
            s["key"], (None, None, [], "missed"),
        )
        cached_source = s.get("_cached_source")
        if cached_source is not None:
            source = cached_source
            if source == PRICE_SOURCE_OK:        ok += 1
            elif source == PRICE_SOURCE_NO_CHIPS: no_chips += 1
            else:                                 fetch_failed += 1
        else:
            if min_p is not None:
                source = PRICE_SOURCE_OK; ok += 1
            elif note.startswith("no prices"):
                source = PRICE_SOURCE_NO_CHIPS; no_chips += 1
            else:
                source = PRICE_SOURCE_FETCH_FAIL; fetch_failed += 1
        s_idx, st_idx = s["key"]
        st = payload["shows"][s_idx]["showtimes"][st_idx]
        st["verified_min_price"]    = min_p
        st["verified_max_price"]    = max_p
        st["verified_candidates"]   = candidates
        st["verified_price_source"] = source
        st["verified_reason"]       = s["reason"]
        st["verified_note"]         = note
        st["verified_url"]          = s["url"]
        st["verified_checked_at"]   = now_iso

        if (use_cache and cache_mod is not None
                and cached_source is None
                and source in cache_mod.CACHEABLE_SOURCES
                and s.get("_cache_key") is not None):
            cache_entries[s["_cache_key"]] = cache_mod.make_entry(
                chip_min=min_p,
                chip_max=max_p,
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
        "Done in %.1fs — ok=%d, no_chips=%d, fetch_failed=%d"
        " (cache: %d hits / %d misses / %d writes)",
        elapsed, ok, no_chips, fetch_failed,
        cache_hits, cache_misses, cache_writes,
    )

    summary = {
        "verified_at":      now_iso,
        "suspect_count":    len(suspects),
        "window_days":      window_days,
        "threshold":        threshold,
        "workers":          workers,
        "ok":               ok,
        "no_chips":         no_chips,
        "fetch_failed":     fetch_failed,
        "duration_seconds": round(elapsed, 1),
        "cache_hits":       cache_hits,
        "cache_misses":     cache_misses,
        "cache_writes":     cache_writes,
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
            "Verify TodayTix per-performance prices against live booking "
            "pages. Only verifies showtimes whose cheapest band has thin "
            "seat inventory (likely-stale snapshot) within a date window."
        ),
    )
    p.add_argument("--in", "-i", dest="in_path", type=Path,
        default=Path("todaytix.json"),
        help="Input JSON from todaytix_scraper.py (default: todaytix.json).")
    p.add_argument("--out", "-o", dest="out_path", type=Path, default=None,
        help="Output JSON path. Default: overwrite the input in place.")
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
        help=f"Only verify showtimes within N days from today "
             f"(default: {DEFAULT_WINDOW_DAYS}).")
    p.add_argument("--cheap-band-threshold", type=int,
        default=DEFAULT_CHEAP_BAND_THRESHOLD,
        help=f"Verify if cheapest available band has fewer than N seats "
             f"(default: {DEFAULT_CHEAP_BAND_THRESHOLD}).")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Concurrent Playwright pages (default: {DEFAULT_WORKERS}; "
             "TT is sensitive to bot detection — increase cautiously).")
    p.add_argument("--chip-cache", type=Path, default=None,
        help="Path to a JSON cache file for chip-pass results. When "
             "provided, suspect showtimes whose catalogue inputs "
             "haven't changed since the last run skip the Playwright "
             "extraction and reuse cached chip values. Cache entries "
             "expire after --chip-cache-ttl-hours (default 24). Omit "
             "this flag to run without caching.")
    p.add_argument("--chip-cache-ttl-hours", type=int, default=24,
        help="How long a cache entry stays valid (default: 24h).")
    p.add_argument("--dry-run", action="store_true",
        help="Do everything except write the output file.")
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
        log.error("Input %s does not look like todaytix scraper output",
                  args.in_path)
        return EXIT_BAD_INPUT

    run(payload, window_days=args.window_days,
        threshold=args.cheap_band_threshold, workers=args.workers,
        cache_path=args.chip_cache,
        cache_ttl_hours=args.chip_cache_ttl_hours)

    if args.dry_run:
        log.info("--dry-run: not writing output")
        return EXIT_CLEAN

    out_path = args.out_path or args.in_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(out_path)
    log.info("Wrote %s", out_path)
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
