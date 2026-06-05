#!/usr/bin/env python3
"""
todaytix_api_check.py — CI smoke test for the api.todaytix.com price feed
=========================================================================

todaytix_availability.py was validated from a UK IP, but the scrape runs on
US-based GitHub Actions runners — the same environment where the old browser
chip pass silently broke (it rendered a geo-degraded booking page and recorded
"no prices"). This script proves, from wherever it runs, that the JSON-API
approach the new checker uses works there too.

It checks four things and exits non-zero (red workflow) if the critical ones fail:

  1. REACHABILITY  — is api.todaytix.com reachable, or geo-gated (403/503)?
  2. LONDON SHOWS  — does /api/v2/shows?location=2 return live London shows?
  3. CURRENCY      — does /showtimes come back in GBP? Fetched twice: once with
                     NO currency header (what a US runner gets by default) and
                     once WITH X-TT-Currency: GBP (the pin the checker sends).
                     This is the decisive test of whether the pin works in CI.
  4. CACHE-BUST    — does the unique-param + no-cache request force a CloudFront
                     MISS (origin-fresh), as it does from a UK IP?

No todaytix.json needed: it discovers a currently-live London show via the API,
so it keeps working as shows come and go.

Exit codes:
    0  PASS  — reachable AND the GBP-pinned call returns GBP
    1  FAIL  — api.todaytix.com unreachable / geo-gated (route via proxy)
    2  FAIL  — reachable but the GBP pin did NOT yield GBP (pin needs fixing)
    3  WARN  — reachable but couldn't find a priced band to judge currency

Usage:
    python todaytix_api_check.py            # the CI check
    python todaytix_api_check.py --selftest # validate logic locally, no TT
"""

from __future__ import annotations

import argparse
import json
import random
import sys

import requests

LONDON_LOCATION_ID = 2          # TodayTix location id for London
SHOWS_URL     = "https://api.todaytix.com/api/v2/shows"
SHOWTIMES_URL = "https://api.todaytix.com/api/v2/shows/{show_id}/showtimes"
# Used only if live discovery fails; this is the show used throughout the
# debugging session. Discovery is the primary path, so a closed show here is
# harmless as long as the API is reachable.
FALLBACK_SHOW_IDS = [302]

TIMEOUT = 20.0

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.todaytix.com/",
    "Origin": "https://www.todaytix.com",
}
GBP_HEADERS = {**BASE_HEADERS, "X-TT-Currency": "GBP"}


# --- parsing helpers (pure, unit-tested) ------------------------------------

def parse_live_show_ids(shows_json: dict) -> list[tuple]:
    """From /api/v2/shows, return [(id, displayName), ...] for shows with
    regular tickets available."""
    out = []
    data = shows_json.get("data") if isinstance(shows_json, dict) else None
    for sh in (data or []):
        if not isinstance(sh, dict):
            continue
        if sh.get("id") and sh.get("areRegularTicketsAvailable"):
            out.append((sh["id"], sh.get("displayName") or "?"))
    # fall back to any show with an id, available or not
    if not out:
        for sh in (data or []):
            if isinstance(sh, dict) and sh.get("id"):
                out.append((sh["id"], sh.get("displayName") or "?"))
    return out


def first_priced_band(showtimes_json: dict):
    """Return (showtime_id, price_dict) for the first priceBand that has a
    value, else (None, None). price_dict = {value, currency, display}."""
    data = showtimes_json.get("data") if isinstance(showtimes_json, dict) else None
    for st in (data or []):
        if not isinstance(st, dict):
            continue
        reg = st.get("regularTickets") or {}
        for b in (reg.get("priceBands") or []):
            price = b.get("price") or {}
            if price.get("value") is not None:
                return st.get("id"), price
    return None, None


def looks_gbp(price: dict | None) -> bool:
    if not price:
        return False
    if (price.get("currency") or "").upper() == "GBP":
        return True
    return (price.get("display") or "").strip().startswith("£")


# --- HTTP -------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    return s


def get_showtimes(s, show_id, headers, bust):
    params = {"includeNoInventory": "true"}
    h = dict(headers)
    if bust:
        params["_cb"] = str(random.randint(1, 10 ** 9))
        h["Cache-Control"] = "no-cache"
        h["Pragma"] = "no-cache"
    return s.get(SHOWTIMES_URL.format(show_id=show_id), params=params,
                 headers=h, timeout=TIMEOUT)


def hdr(resp, name, default="-"):
    return resp.headers.get(name, default)


# --- main check -------------------------------------------------------------

def run_check() -> int:
    s = _session()
    print("=" * 70)
    print("TodayTix API check — running from this GitHub runner")
    print("=" * 70)

    # 1. reachability + London discovery
    print("\n[1] reachability + London show discovery (/api/v2/shows?location=2)")
    candidates: list[tuple] = []
    try:
        rd = s.get(SHOWS_URL, params={
            "fieldset": "SHOW_SUMMARY", "sortBy": "RECENT_TRANSACTION_COUNT",
            "sortOrder": "DESC", "location": LONDON_LOCATION_ID,
            "context": "MERCHANDISING", "limit": 12, "offset": 0,
            "includeAggregations": "false"}, headers=GBP_HEADERS, timeout=TIMEOUT)
        print(f"    status: {rd.status_code}   x-amz-cf-pop: {hdr(rd,'x-amz-cf-pop')}"
              f"   x-cache: {hdr(rd,'x-cache')}")
        if rd.status_code == 200:
            candidates = parse_live_show_ids(rd.json())
            print(f"    discovered {len(candidates)} live London show(s); "
                  f"e.g. {[n for _, n in candidates[:4]]}")
        else:
            print(f"    non-200 from /shows — will try fallback show id(s).")
    except requests.RequestException as e:
        print(f"    REACHABILITY ERROR: {type(e).__name__}: {str(e)[:160]}")
        print("\nRESULT: FAIL (1) — api.todaytix.com is not reachable from this "
              "runner.\n  This is the geo-gated case: route the checker through "
              "your Cloudflare Worker (UK egress).")
        return 1

    show_ids = [sid for sid, _ in candidates] + FALLBACK_SHOW_IDS

    # 2/3. find a show with a priced band; compare currency without vs with pin
    print("\n[2/3] /showtimes currency check (no header  vs  X-TT-Currency: GBP)")
    chosen = None
    for sid in show_ids:
        try:
            r_gbp = get_showtimes(s, sid, GBP_HEADERS, bust=True)
        except requests.RequestException as e:
            print(f"    show {sid}: request error {type(e).__name__}: {str(e)[:100]}")
            continue
        if r_gbp.status_code != 200:
            print(f"    show {sid}: /showtimes HTTP {r_gbp.status_code} "
                  f"(x-cache={hdr(r_gbp,'x-cache')}) — trying next")
            continue
        st_id, price_gbp = first_priced_band(r_gbp.json())
        if price_gbp is None:
            print(f"    show {sid}: 200 but no priced band right now — trying next")
            continue
        chosen = (sid, st_id, r_gbp, price_gbp)
        break

    if chosen is None:
        print("\nRESULT: WARN (3) — API reachable, but no show returned a priced "
              "band to judge currency. Re-run later.")
        return 3

    sid, st_id, r_gbp, price_gbp = chosen
    # default-currency call (no X-TT-Currency) on the same show
    try:
        r_def = get_showtimes(s, sid, BASE_HEADERS, bust=True)
        _, price_def = first_priced_band(r_def.json()) if r_def.status_code == 200 else (None, None)
    except requests.RequestException:
        price_def = None

    def fmt(p):
        if not p:
            return "(no band)"
        return f"{p.get('currency','?')}  {p.get('display','?')}  value={p.get('value')}"

    print(f"    using show id {sid}, showtime {st_id}")
    print(f"    NO currency header : {fmt(price_def)}")
    print(f"    X-TT-Currency: GBP : {fmt(price_gbp)}")
    print(f"    /showtimes cache-bust: x-cache={hdr(r_gbp,'x-cache')} "
          f"age={hdr(r_gbp,'age')} pop={hdr(r_gbp,'x-amz-cf-pop')}")

    # 4. interpret
    print("\n[4] verdict")
    default_gbp = looks_gbp(price_def)
    pinned_gbp = looks_gbp(price_gbp)
    bust_ok = "MISS" in (hdr(r_gbp, "x-cache") or "").upper()

    if not pinned_gbp:
        print("    ✗ The X-TT-Currency: GBP call did NOT return GBP.")
        print(f"      It returned: {fmt(price_gbp)}")
        print("\nRESULT: FAIL (2) — the GBP pin does not work from this runner.")
        print("  Fix options: confirm the header name/value, or route the checker")
        print("  through your Cloudflare Worker so requests egress from the UK.")
        return 2

    print("    ✓ GBP confirmed with the X-TT-Currency: GBP pin.")
    if default_gbp:
        print("    • Note: the no-header call ALSO returned GBP — this runner gets "
              "GBP by default; the pin is belt-and-suspenders here.")
    else:
        print("    • The no-header call returned NON-GBP — so the pin is doing real "
              "work on this runner (confirms it was needed).")
    print(f"    • Cache-bust: {'origin MISS (fresh)' if bust_ok else 'did not MISS this call'}.")
    print("\nRESULT: PASS (0) — api.todaytix.com is reachable from this runner and "
          "returns GBP with the pin.\n  The API availability checker will work in "
          "CI; no proxy needed.")
    return 0


# --- selftest (no TT) -------------------------------------------------------

def selftest() -> int:
    print("SELFTEST: parsing + request mechanics (no TodayTix access)\n")
    shows = {"data": [
        {"id": 1, "displayName": "Sold Out Show", "areRegularTicketsAvailable": False},
        {"id": 302, "displayName": "The Producers", "areRegularTicketsAvailable": True},
    ]}
    assert parse_live_show_ids(shows) == [(302, "The Producers")], parse_live_show_ids(shows)
    st = {"data": [
        {"id": 99, "regularTickets": {"priceBands": [{"price": {"value": None}}]}},
        {"id": 2365307, "regularTickets": {"priceBands": [
            {"price": {"value": 51.0, "currency": "GBP", "display": "£51.00"},
             "numAssignedSeatsAvailable": 11}]}},
    ]}
    sid, price = first_priced_band(st)
    assert sid == 2365307 and price["currency"] == "GBP", (sid, price)
    assert looks_gbp({"currency": "GBP"}) and looks_gbp({"display": "£9"})
    assert not looks_gbp({"currency": "USD", "display": "$9"})
    print("  parsing helpers OK (discovery, first priced band, GBP detection)")
    try:
        s = _session()
        r = s.get("https://pypi.org/pypi/requests/json", timeout=15)
        print(f"  requests mechanics OK (GET pypi -> {r.status_code}, "
              f"headers sent incl. {list(BASE_HEADERS)[:2]}…)")
        ok = r.status_code == 200
    except Exception as e:
        print(f"  (network unavailable here: {e}) — logic still validated")
        ok = True
    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="CI smoke test for api.todaytix.com")
    p.add_argument("--selftest", action="store_true",
                   help="Validate parsing + mechanics locally, no TT access.")
    args = p.parse_args(argv)
    return selftest() if args.selftest else run_check()


if __name__ == "__main__":
    sys.exit(main())
