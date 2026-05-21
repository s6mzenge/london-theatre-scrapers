"""
Official London Theatre per-performance availability verifier
=============================================================

Second pass for `olt_scraper.py`. The main scraper reads each show's
embedded ``data-cal`` calendar JSON, which lists every performance
with ``min``, ``max``, ``avail``, ``bookable``. That calendar refreshes
on a schedule and lags real-time bookability on the underlying
SeeTickets backend — performances the calendar reports as
``avail:true, min:83`` may already be sold out when you visit the
actual ticketing page.

The 22 May 2026 7:30pm *Beetlejuice* perf at the Prince Edward is the
canonical case: data-cal said £83 available, the live event page at
``…/n/event/beetlejuice/…/118409`` was marked "Tickets not available"
with the only ticket type (Standard) flagged ``SoldOut`` in the
embedded JSON-LD.

How it works
------------
Each performance in ``olt.json`` already carries a ``book_url``
pointing at its SeeTickets event-detail page. We GET that page and
walk the live ticket-type table:

    <tr class=" ticket" ...>
        <td>Standard</td>
        <td class="price-info-cell" data-cost="83.00">£83.00 (£80.00)</td>
        <td class="note quantity">Tickets not available</td>
    </tr>

For each row we pull ``data-cost="X.XX"`` and check whether the row
contains the literal "Tickets not available" message. A row is
*bookable* iff that string is absent.

A perf is then:
- ``verified_available=True`` if at least one tier is bookable
- ``verified_min_price`` / ``verified_max_price`` reflect only the
  bookable tiers (a £15 sold-out tier and a £85 available tier yields
  min=85 max=85)
- ``verified_available=False`` (all tiers sold out) is recorded with
  ``verified_price_source="no_seats"`` so dedupe drops OLT from the
  cross-source minimum cleanly

Show-wide signals (the ``<meta property="product:availability">`` tag
and JSON-LD ``offers[…].availability``) are consulted only when the
ticket table can't be located — they're a robustness fallback against
template drift, not the primary signal.

Fields added to each Performance dict
-------------------------------------
    verified_min_price             float | None
    verified_max_price             float | None
    verified_available             bool  | None
    verified_tier_count            int   | None   total ticket-type rows seen
    verified_available_tier_count  int   | None   tiers with stock
    verified_price_source          str            one of:
        "ticketing_page" - ticket table parsed; verified_* are usable
        "no_seats"       - page loaded, all tiers sold out
        "fetch_failed"   - HTTP error or non-200 status
        "skipped"        - no book_url, or perf date is in the past
    verified_status                int|str|None   HTTP status, or exception summary
    verified_url                   str  | None
    verified_checked_at            str            UTC ISO timestamp

The existing ``min_price`` / ``max_price`` / ``available`` / ``bookable``
fields are NOT modified. The dedupe layer's OLT schema is updated
separately to prefer ``verified_min_price`` when present.

Usage
-----
    python olt_availability.py --in olt.json
    python olt_availability.py --concurrency 16
    python olt_availability.py --limit 50
    python olt_availability.py --include-past
    python olt_availability.py --dry-run

Exit codes
----------
    0  success (partial fails are normal)
    1  bad input (file missing, malformed JSON)
    2  zero successes despite >0 attempts — likely template drift
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

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

DEFAULT_CONCURRENCY = 6
DEFAULT_TIMEOUT_S = 15

# Per-request jitter (seconds, uniform). Each worker sleeps this much
# before its fetch. With concurrency=6 and ~150ms mean jitter, aggregate
# request rate stays around 4-5 RPS — well under what Akamai/SeeTickets
# tolerates from a single egress IP. Local testing through the same
# proxy with sequential calls returned 200; the CI run with 16-way
# concurrency tripped Akamai's burst protection (1906×403, 95×429).
JITTER_S_MIN = 0.05
JITTER_S_MAX = 0.20

# Slightly higher retry budget. urllib3 retries on the statuses below
# with exponential backoff (RETRY_BACKOFF * 2^N seconds). With 5
# retries and 0.5s backoff, total wait can reach ~15s per request —
# slow on a real outage but rare; mostly we just need to ride out
# transient Akamai pushback.
RETRY_TOTAL = 5
RETRY_BACKOFF = 0.5

# verified_price_source values. Keep aligned with the dedupe OLT schema.
SOURCE_OK         = "ticketing_page"
SOURCE_NO_SEATS   = "no_seats"
SOURCE_FETCH_FAIL = "fetch_failed"
SOURCE_SKIPPED    = "skipped"

# Plausibility bounds — anything outside this range is treated as a
# parse glitch and dropped silently from min/max. The OLT calendar
# itself has 4–500 GBP as its realistic span.
PRICE_MIN_GBP = 4.0
PRICE_MAX_GBP = 500.0

EXIT_CLEAN     = 0
EXIT_BAD_INPUT = 1
EXIT_DRIFT     = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("olt-avail")


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
#
# SeeTickets event pages sit behind Akamai bot mitigation that 403s
# requests from cloud-CI IP ranges (Azure-hosted GitHub Actions runners
# are blocklisted). To get past it, we route every request through the
# same Cloudflare Worker that olt_scraper.py uses for the parent
# officiallondontheatre.com site — a fetch-and-forward proxy living on
# clean IPs that Akamai trusts.
#
# Wire protocol: instead of the path-rewriting trick olt_scraper.py uses
# (only good for one fixed origin), we pass the full target URL as a
# request header. The Worker reads `X-Proxy-Target`, fetches that URL,
# and streams the response back. The auth header is the same
# `X-Proxy-Auth: <token>` the existing proxy expects.
#
# If proxy-url isn't configured we fall back to direct fetches — fine
# for local development on a residential IP, broken in CI.

class _ProxyingSession(requests.Session):
    """A session that tunnels every GET through a reverse proxy.

    Each request is rewritten so the URL points at the proxy, with the
    original target URL passed via `X-Proxy-Target`. Auth is a static
    shared-secret in `X-Proxy-Auth`. Both headers are added per-request
    so the secret only leaves the process on proxied calls, never on
    any other host that might share the session.
    """

    def __init__(self, proxy_url: str, proxy_token: str | None) -> None:
        super().__init__()
        # Strip trailing slash so the URL is well-formed regardless of
        # whether the user supplied "https://w.dev" or "https://w.dev/".
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
        # 403 is added because Akamai uses it as a soft "slow down"
        # signal alongside 429. A real "you're not allowed here" 403
        # will exhaust retries quickly and surface as fetch_failed,
        # which is fine.
        status_forcelist=(403, 429, 500, 502, 503, 504),
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
# Ticket-table parsing
# ---------------------------------------------------------------------------
#
# We rely on three signals from the SeeTickets template, ranked by
# trustworthiness:
#
#   1. The price-list table itself: each ``<tr class="…ticket…">`` row
#      has a ``data-cost="X.XX"`` attribute, and an unavailable row
#      contains the literal text "Tickets not available". Multiple
#      tiers can mix availability; this is the only signal that
#      preserves that detail.
#
#   2. The ``<meta property="product:availability">`` tag — show-wide
#      "in stock" / "out of stock". Used as a fallback when the table
#      can't be located.
#
#   3. JSON-LD ``offers[…].availability`` — also show-wide. Final
#      fallback if the meta tag is missing.

# Match each <tr ...> whose class attribute lists 'ticket'. The class
# attribute frequently has surrounding whitespace (e.g. ``class=" ticket"``)
# and may carry extra classes, so we match the word boundary inside.
_ROW_RE = re.compile(
    r'<tr\b[^>]*\bclass="[^"]*\bticket\b[^"]*"[^>]*>(?P<body>[\s\S]*?)</tr>',
    re.IGNORECASE,
)

_DATA_COST_RE = re.compile(
    r'\bdata-cost\s*=\s*"(?P<cost>\d+(?:\.\d+)?)"',
    re.IGNORECASE,
)

# Both "Tickets not available" and the older "Tickets unavailable" form
# have been observed across SeeTickets-hosted partner sites. Case
# insensitive and tolerant of whitespace between words.
_UNAVAIL_RE = re.compile(
    r"Tickets\s+(?:not\s+available|unavailable)",
    re.IGNORECASE,
)

_META_AVAIL_RE = re.compile(
    r'<meta\b[^>]*\bproperty="product:availability"[^>]*\bcontent="(?P<v>[^"]*)"',
    re.IGNORECASE,
)

# JSON-LD availability — appears inside @type:Offer blocks as
# "availability":"http://schema.org/SoldOut" or just "SoldOut" / "InStock".
_JSONLD_AVAIL_RE = re.compile(
    r'"availability"\s*:\s*"(?:https?://schema\.org/)?(?P<v>[A-Za-z]+)"',
    re.IGNORECASE,
)


def _in_bounds(price: float) -> bool:
    return PRICE_MIN_GBP <= price <= PRICE_MAX_GBP


def parse_ticket_table(html: str) -> dict | None:
    """Walk every <tr class="ticket"> row.

    Returns a dict with tier counts and price extrema, or None if no
    rows could be located (signal to fall back to meta/JSON-LD).
    """
    rows = _ROW_RE.findall(html)
    if not rows:
        return None

    tier_count = 0
    available_count = 0
    available_costs: list[float] = []
    for body in rows:
        tier_count += 1
        cost_m = _DATA_COST_RE.search(body)
        if not cost_m:
            # Row without a data-cost — most often a multi-row offer
            # header or a hidden template row. Don't count it as a
            # tier; skip.
            tier_count -= 1
            continue
        try:
            cost = float(cost_m.group("cost"))
        except ValueError:
            continue
        if not _in_bounds(cost):
            continue
        if _UNAVAIL_RE.search(body):
            continue
        available_count += 1
        available_costs.append(cost)

    return {
        "tier_count": tier_count,
        "available_tier_count": available_count,
        "min_price": min(available_costs) if available_costs else None,
        "max_price": max(available_costs) if available_costs else None,
    }


def parse_meta_availability(html: str) -> bool | None:
    """Return True/False if the meta tag is parseable, else None."""
    m = _META_AVAIL_RE.search(html)
    if not m:
        return None
    v = m.group("v").strip().lower()
    if v in ("in stock", "instock"):
        return True
    if v in ("out of stock", "outofstock", "soldout", "sold out"):
        return False
    return None  # unrecognised value — don't gamble


def parse_jsonld_availability(html: str) -> bool | None:
    """Sniff JSON-LD offer availability without parsing the full block.

    Returns True if at least one offer is InStock-equivalent, False if
    every offer is SoldOut-equivalent, None if no parseable signal.
    """
    matches = _JSONLD_AVAIL_RE.findall(html)
    if not matches:
        return None
    saw_in_stock = False
    saw_sold_out = False
    for raw in matches:
        v = raw.strip().lower()
        if v in ("instock", "limitedavailability", "preorder", "presale"):
            saw_in_stock = True
        elif v in ("soldout", "outofstock"):
            saw_sold_out = True
    if saw_in_stock:
        return True
    if saw_sold_out:
        return False
    return None


# ---------------------------------------------------------------------------
# Per-performance worker
# ---------------------------------------------------------------------------

def _empty_result(url: str | None, source: str, status=None) -> dict:
    return {
        "verified_min_price": None,
        "verified_max_price": None,
        "verified_available": None,
        "verified_tier_count": None,
        "verified_available_tier_count": None,
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
    """Fetch one event page and return the verified-price dict.

    Never raises — failures surface in verified_price_source / verified_status.
    """
    if not url:
        return _empty_result(url, SOURCE_SKIPPED)

    # Per-request jitter. Without this, all N workers can launch their
    # first fetches within a few milliseconds of each other — a clean
    # burst pattern Akamai will flag. The randomised delay desynchronises
    # workers and keeps aggregate RPS in a sustainable range.
    time.sleep(random.uniform(JITTER_S_MIN, JITTER_S_MAX))

    try:
        r = session.get(url, timeout=timeout_s)
    except requests.RequestException as e:
        return _empty_result(url, SOURCE_FETCH_FAIL, status=str(e)[:160])

    if r.status_code != 200:
        return _empty_result(url, SOURCE_FETCH_FAIL, status=r.status_code)

    html = r.text

    # Primary: walk the ticket-type table.
    table = parse_ticket_table(html)
    if table is not None and table["tier_count"] > 0:
        result = _empty_result(url, SOURCE_OK, status=r.status_code)
        result["verified_tier_count"]           = table["tier_count"]
        result["verified_available_tier_count"] = table["available_tier_count"]
        result["verified_min_price"]            = table["min_price"]
        result["verified_max_price"]            = table["max_price"]
        result["verified_available"]            = table["available_tier_count"] > 0
        if not result["verified_available"]:
            # Page loaded fine, every tier is sold out. Surface as
            # no_seats so dedupe drops OLT from the cross-source min.
            result["verified_price_source"] = SOURCE_NO_SEATS
        return result

    # Fallback 1: show-wide meta tag.
    meta = parse_meta_availability(html)
    if meta is not None:
        result = _empty_result(
            url,
            SOURCE_OK if meta else SOURCE_NO_SEATS,
            status=r.status_code,
        )
        result["verified_available"] = meta
        # No tier-level data — leave prices None and let dedupe fall
        # back to the unverified data-cal min_price/max_price.
        return result

    # Fallback 2: JSON-LD availability.
    jsonld = parse_jsonld_availability(html)
    if jsonld is not None:
        result = _empty_result(
            url,
            SOURCE_OK if jsonld else SOURCE_NO_SEATS,
            status=r.status_code,
        )
        result["verified_available"] = jsonld
        return result

    # Page loaded but none of our signals fired — almost certainly a
    # template change. Mark no_seats so dedupe is conservative; the
    # drift check at the run() level will flag it.
    return _empty_result(url, SOURCE_NO_SEATS, status=r.status_code)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def iter_perfs_to_check(
    payload: dict,
    today_iso: str,
    include_past: bool,
    within_days: int | None,
) -> list[tuple[int, int, str | None]]:
    """(show_idx, perf_idx, book_url) for every perf to verify.

    Past-dated perfs are dropped unless --include-past. With
    within_days=N, perfs more than N calendar days in the future are
    also dropped (catches the user-visible bug window without paying
    for every long-tail booking). Perfs without a book_url get a None
    and are recorded as SKIPPED.
    """
    if within_days is not None:
        end_date = (
            datetime.fromisoformat(today_iso).date()
            + timedelta(days=within_days)
        ).isoformat()
    else:
        end_date = None

    out: list[tuple[int, int, str | None]] = []
    for si, show in enumerate(payload.get("shows") or []):
        for pi, perf in enumerate(show.get("performances") or []):
            date = perf.get("date")
            if not include_past and date and date < today_iso:
                continue
            if end_date is not None and date and date > end_date:
                continue
            out.append((si, pi, perf.get("book_url")))
    return out


def run(
    payload: dict,
    *,
    concurrency: int,
    limit: int | None,
    include_past: bool,
    within_days: int | None,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
) -> dict:
    """Verify in-place on payload['shows'][i]['performances'][j].

    Returns a summary dict, also embedded under
    payload['report']['availability_verification'].
    """
    today_iso = datetime.now(timezone.utc).date().isoformat()
    tasks = iter_perfs_to_check(
        payload, today_iso,
        include_past=include_past,
        within_days=within_days,
    )
    if limit is not None:
        tasks = tasks[:limit]
        log.info("--limit %d applied", limit)

    total = len(tasks)
    log.info(
        "Verifying %d performance(s) across %d show(s) with %d worker(s)"
        "%s%s",
        total,
        len(payload.get("shows") or []),
        concurrency,
        f" (within {within_days}d window)" if within_days is not None else "",
        " (via proxy)" if proxy_url else "",
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
            except Exception as e:  # noqa: BLE001
                log.warning("worker exception: %s", e)
                continue
            payload["shows"][si]["performances"][pi].update(out)
            src = out["verified_price_source"]
            with counts_lock:
                counts[src] = counts.get(src, 0) + 1
            with progress_lock:
                progress["n"] += 1
                if progress["n"] % 500 == 0:
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
            "Verify OLT per-performance availability by GETting each "
            "SeeTickets event page and parsing the live ticket-type table."
        ),
    )
    p.add_argument(
        "--in", "-i", dest="in_path", type=Path,
        default=Path("olt.json"),
        help="Input JSON from olt_scraper.py (default: olt.json).",
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
        "--within-days", type=int, default=30, metavar="N",
        help="Only verify performances scheduled within the next N "
             "calendar days (default: 30). Pass 0 to disable the "
             "window and verify every future perf. The user-visible "
             "bug window is near-term anyway: nobody books 9 months "
             "out and gets burned by a sold-out tier.",
    )
    p.add_argument(
        "--include-past", action="store_true",
        help="Also verify performances whose date is in the past "
             "(skipped by default since they typically 404 or redirect).",
    )
    p.add_argument(
        "--proxy-url", default=os.environ.get("OLT_PROXY_URL"),
        metavar="URL",
        help="Route every SeeTickets request through this reverse-proxy "
             "URL (e.g. the same Cloudflare Worker olt_scraper.py uses). "
             "The proxy must accept an X-Proxy-Target header carrying "
             "the original URL and fetch+forward. Defaults to "
             "$OLT_PROXY_URL if set. Required when running from cloud "
             "CI; SeeTickets 403s Azure/GitHub Actions runner IPs.",
    )
    p.add_argument(
        "--proxy-token", default=os.environ.get("OLT_PROXY_TOKEN"),
        metavar="TOKEN",
        help="Shared-secret value sent as X-Proxy-Auth on every "
             "request to --proxy-url. Defaults to $OLT_PROXY_TOKEN.",
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
        log.error("Input %s does not look like olt scraper output "
                  "(expected top-level dict with 'shows' list)", args.in_path)
        return EXIT_BAD_INPUT

    summary = run(
        payload,
        concurrency=args.concurrency,
        limit=args.limit,
        include_past=args.include_past,
        within_days=None if args.within_days <= 0 else args.within_days,
        proxy_url=args.proxy_url,
        proxy_token=args.proxy_token,
    )

    if args.proxy_url:
        log.info("Using proxy: %s", args.proxy_url)
    elif summary["fetch_failed"] > 0 and summary["ok"] == 0:
        log.warning(
            "No --proxy-url set and all fetches failed. SeeTickets 403s "
            "cloud-CI IPs; set OLT_PROXY_URL/OLT_PROXY_TOKEN to route "
            "through the Cloudflare Worker."
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

    attempted_real = summary["total_checked"] - summary["skipped"]
    if attempted_real > 0 and summary["ok"] == 0:
        # Zero successes despite real attempts. Two possible causes,
        # very different in severity:
        #   - All fetches blocked at the network layer (403, 429, or
        #     connection errors). The parser never ran. NOT drift —
        #     just IP reputation. Partial output is already written
        #     with verified_price_source="fetch_failed" on every perf;
        #     dedupe correctly falls back to data-cal. Don't fail the
        #     pipeline; warn loudly so the operator sees it.
        #   - Fetches succeeded (200) but the parser found nothing.
        #     That IS template drift — exit 2 so CI flags it red.
        if summary["fetch_failed"] >= attempted_real:
            log.warning(
                "All %d fetches failed (no 200 responses). Almost certainly "
                "the SeeTickets IP-block on cloud-CI runners. Set "
                "--proxy-url / OLT_PROXY_URL to route through the worker. "
                "Output is still written; dedupe will fall back to data-cal.",
                attempted_real,
            )
            return EXIT_CLEAN
        log.error(
            "No performances verified successfully out of %d attempts — "
            "possible event-page template drift",
            attempted_real,
        )
        return EXIT_DRIFT
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
