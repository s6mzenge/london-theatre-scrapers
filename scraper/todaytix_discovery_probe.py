#!/usr/bin/env python3
"""
todaytix_discovery_probe.py — can the Playwright listing-scroll be replaced
with deterministic API pagination?
===========================================================================

READ-ONLY DIAGNOSTIC. Makes no changes to any repo file; writes nothing.
Intended to run on a US GitHub runner (same place todaytix_api_check passes),
because api.todaytix.com is geo-aware. It hits

    GET https://api.todaytix.com/api/v2/shows?location=2     (2 = London)

and reports EVERYTHING needed to decide whether discovery can move off the
flaky browser scroll onto the API:

  1. response shape + a sample show's fields
  2. the default page size (how many shows one call returns)
  3. which pagination mechanism actually ADVANCES the result set —
     page-number / offset / limit-bump / cursor — vs is silently ignored
  4. a full walk with the working mechanism: how many unique show ids we can
     collect, vs the total the API reports, vs the listing page's own
     pagination.total (~200) when reachable
  5. whether each show carries the id + slug/url we need to rebuild the
     /london/shows/{id}-{slug} pairs the current scraper produces

Then it prints a VERDICT. Paste the whole output back and the discovery switch
can be designed from measured facts.

Usage:
    python scraper/todaytix_discovery_probe.py            # the real probe
    python scraper/todaytix_discovery_probe.py --selftest # offline, no network
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path  # noqa: F401  (kept for parity / future use)

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

SHOWS_URL = "https://api.todaytix.com/api/v2/shows"
LISTING_URL = "https://www.todaytix.com/london/shows"
LONDON_LOCATION = 2
TIMEOUT = 25.0

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.todaytix.com/",
    "Origin": "https://www.todaytix.com",
    "X-TT-Currency": "GBP",
}

# key-name heuristics --------------------------------------------------------
ID_KEYS = ("id", "showId", "show_id")
SLUG_KEYS = ("slug", "showSlug", "seoSlug", "urlSlug")
URL_KEYS = ("url", "webUrl", "shareUrl", "canonicalUrl", "href", "deeplink")
NAME_KEYS = ("name", "showName", "title", "displayName")
CATEGORY_KEYS = ("category", "categories", "type", "showType", "genre", "tags")
AVAIL_KEYS = ("isAvailable", "available", "status", "saleStatus", "onSale")
PAGINATION_HINT = ("pag", "total", "count", "page", "next", "cursor",
                   "offset", "limit", "hasmore", "has_more")


# --- generic helpers --------------------------------------------------------

def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    retry = Retry(total=3, backoff_factor=0.6,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}), raise_on_status=False)
    ad = HTTPAdapter(max_retries=retry)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def _first(d: dict, keys) -> tuple[str | None, object]:
    """Return (key, value) for the first present, non-empty key in `keys`."""
    if not isinstance(d, dict):
        return None, None
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return k, d[k]
    return None, None


def extract_shows(payload) -> list[dict]:
    """Find the list of show-like dicts in a response of unknown shape."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        # e.g. {"data": {"shows": [...]}} or {"data": {"results": [...]}}
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    # last resort: the longest top-level list of dicts
    best: list[dict] = []
    for v in payload.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and len(v) > len(best):
            best = v
    return best


def extract_pagination(payload) -> dict | None:
    """Find a dict whose keys look like pagination metadata."""
    if not isinstance(payload, dict):
        return None
    # direct, common spots
    for key in ("pagination", "meta", "_meta", "page", "paging"):
        v = payload.get(key)
        if isinstance(v, dict) and any(
                any(h in str(k).lower() for h in PAGINATION_HINT) for k in v):
            return {key: v} if key != "pagination" else v
    # any nested dict that looks paginationy
    for k, v in payload.items():
        if isinstance(v, dict) and sum(
                1 for kk in v if any(h in str(kk).lower() for h in PAGINATION_HINT)) >= 2:
            return {k: v}
    # scalar pagination fields sitting at the top level
    flat = {k: v for k, v in payload.items()
            if not isinstance(v, (list, dict))
            and any(h in str(k).lower() for h in PAGINATION_HINT)}
    return flat or None


def find_total(obj) -> int | None:
    """Recursively find a plausible 'total count' integer."""
    found: list[int] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if isinstance(v, int) and ("total" in kl or kl in
                                           ("count", "totalcount", "numresults", "resultcount")):
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(obj)
    # the largest such integer is the most likely catalogue total
    return max(found) if found else None


def show_ids(shows: list[dict]) -> list:
    out = []
    for sh in shows:
        _, v = _first(sh, ID_KEYS)
        if v is not None:
            out.append(v)
    return out


def fetch(session, params: dict):
    """Return a dict describing one /shows call."""
    p = {"location": LONDON_LOCATION}
    p.update(params)
    try:
        r = session.get(SHOWS_URL, params=p, timeout=TIMEOUT)
    except Exception as e:
        return {"params": p, "ok": False, "status": None,
                "error": f"{type(e).__name__}: {str(e)[:120]}",
                "shows": [], "ids": [], "pagination": None, "json": None}
    try:
        j = r.json()
    except Exception:
        j = None
    shows = extract_shows(j) if j is not None else []
    return {
        "params": p, "ok": r.status_code == 200, "status": r.status_code,
        "error": None, "shows": shows, "ids": show_ids(shows),
        "pagination": extract_pagination(j) if j is not None else None,
        "top_keys": list(j.keys()) if isinstance(j, dict) else f"(type {type(j).__name__})",
        "json": j,
    }


def compare(base_ids, other_ids):
    b, o = set(map(str, base_ids)), set(map(str, other_ids))
    new = o - b
    return {"n": len(other_ids), "n_new_vs_base": len(new),
            "identical_to_base": (b == o and len(o) > 0)}


# --- the probe --------------------------------------------------------------

def probe() -> int:
    s = build_session()
    print("=" * 72)
    print("TodayTix discovery probe — GET /api/v2/shows?location=2 (London)")
    print("=" * 72)

    # 1) BASE CALL ----------------------------------------------------------
    base = fetch(s, {})
    print("\n[1] BASE CALL")
    print(f"    status        : {base['status']}  ok={base['ok']}  error={base['error']}")
    print(f"    top-level keys: {base['top_keys']}")
    print(f"    shows returned: {len(base['shows'])}   (this is the default page size)")
    print(f"    pagination    : {json.dumps(base['pagination'], default=str)[:300]}")
    if not base["ok"] or not base["shows"]:
        print("\nVERDICT: could not read a shows list from the base call — the API may be "
              "geo-gated here, rate-limited, or shaped differently than expected. "
              "Re-run on a US runner; if it still fails, discovery stays on the scroll.")
        if base["json"] is not None:
            print("    raw (trimmed):", json.dumps(base["json"], default=str)[:600])
        return 1

    page_size = len(base["shows"])
    sample = base["shows"][0]
    print(f"\n    sample show — all keys: {sorted(sample.keys())}")
    print("    sample show (trimmed):")
    print("      " + json.dumps(sample, default=str)[:700])

    idk, idv = _first(sample, ID_KEYS)
    slugk, slugv = _first(sample, SLUG_KEYS)
    urlk, urlv = _first(sample, URL_KEYS)
    namek, namev = _first(sample, NAME_KEYS)
    catk, catv = _first(sample, CATEGORY_KEYS)
    availk, availv = _first(sample, AVAIL_KEYS)
    print("\n    field mapping (key -> sample value):")
    print(f"      id       : {idk!r:>14} -> {idv!r}")
    print(f"      slug     : {slugk!r:>14} -> {str(slugv)[:60]!r}")
    print(f"      url      : {urlk!r:>14} -> {str(urlv)[:80]!r}")
    print(f"      name     : {namek!r:>14} -> {str(namev)[:60]!r}")
    print(f"      category : {catk!r:>14} -> {str(catv)[:80]!r}")
    print(f"      available: {availk!r:>14} -> {availv!r}")

    reported_total = find_total(base["json"])
    print(f"\n    API-reported total (best guess): {reported_total}")

    # 2) PAGINATION EXPERIMENTS --------------------------------------------
    print("\n[2] PAGINATION EXPERIMENTS  (does the param ADVANCE, or get ignored?)")
    experiments = {}

    e_page2 = fetch(s, {"page": 2})
    experiments["page=2"] = e_page2
    c = compare(base["ids"], e_page2["ids"])
    print(f"    page=2            : n={c['n']:4d}  new_vs_base={c['n_new_vs_base']:4d}  "
          f"identical_to_base={c['identical_to_base']}  "
          f"-> {'ADVANCES' if c['n_new_vs_base'] else ('IGNORED' if c['identical_to_base'] else 'empty/odd')}")

    e_off = fetch(s, {"offset": page_size, "limit": page_size})
    experiments[f"offset={page_size}&limit={page_size}"] = e_off
    c = compare(base["ids"], e_off["ids"])
    print(f"    offset={page_size}&limit={page_size}: n={c['n']:4d}  new_vs_base={c['n_new_vs_base']:4d}  "
          f"identical_to_base={c['identical_to_base']}  "
          f"-> {'ADVANCES' if c['n_new_vs_base'] else ('IGNORED' if c['identical_to_base'] else 'empty/odd')}")

    bump_param = None
    for pname in ("limit", "pageSize", "perPage", "per_page"):
        e_bump = fetch(s, {pname: 500})
        n = len(e_bump["shows"])
        grew = n > page_size
        print(f"    {pname}=500        : n={n:4d}  "
              f"-> {'GROWS (single-call bulk!)' if grew else 'no effect'}")
        if grew and bump_param is None:
            bump_param = pname
            experiments[f"{pname}=500"] = e_bump

    # cursor?
    cursor_note = "none found"
    pg = base["pagination"] or {}
    pj = json.dumps(pg, default=str).lower()
    if any(t in pj for t in ("cursor", "next", "after", "hasmore", "has_more")):
        cursor_note = json.dumps(pg, default=str)[:200]
    print(f"    cursor/next token : {cursor_note}")

    # 3) DECIDE MECHANISM + FULL WALK --------------------------------------
    print("\n[3] FULL WALK with the best working mechanism")
    mechanism = None
    if bump_param:
        mechanism = f"limit-bump ({bump_param})"
    elif compare(base["ids"], e_page2["ids"])["n_new_vs_base"] > 0:
        mechanism = "page"
    elif compare(base["ids"], e_off["ids"])["n_new_vs_base"] > 0:
        mechanism = "offset"
    print(f"    chosen mechanism  : {mechanism or 'NONE (only one page retrievable)'}")

    all_ids: list = []
    seen: set = set()

    def add(ids):
        added = 0
        for i in ids:
            k = str(i)
            if k not in seen:
                seen.add(k)
                all_ids.append(i)
                added += 1
        return added

    add(base["ids"])
    pages_fetched = 1

    if mechanism and mechanism.startswith("limit-bump"):
        big = fetch(s, {bump_param: 1000})
        pages_fetched += 1
        add(big["ids"])
    elif mechanism == "page":
        for pg_no in range(2, 41):  # cap 40 pages
            e = fetch(s, {"page": pg_no})
            pages_fetched += 1
            if not e["shows"] or add(e["ids"]) == 0:
                break
    elif mechanism == "offset":
        off = page_size
        for _ in range(40):  # cap
            e = fetch(s, {"offset": off, "limit": page_size})
            pages_fetched += 1
            if not e["shows"] or add(e["ids"]) == 0:
                break
            off += page_size

    print(f"    pages fetched     : {pages_fetched}")
    print(f"    unique shows found: {len(all_ids)}")
    print(f"    API-reported total: {reported_total}")
    if all_ids:
        print(f"    first 5 ids       : {all_ids[:5]}")
        print(f"    last 5 ids        : {all_ids[-5:]}")

    # 4) LISTING CROSS-CHECK (best-effort) ---------------------------------
    print("\n[4] LISTING CROSS-CHECK  (what does the website itself report?)")
    listing_total = None
    try:
        r = s.get(LISTING_URL, timeout=TIMEOUT,
                  headers={"Accept": "text/html,application/xhtml+xml"})
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                      r.text, re.S)
        if m:
            nd = json.loads(m.group(1))
            listing_total = find_total(nd)
        print(f"    listing page status: {r.status_code}; "
              f"pagination.total (best guess): {listing_total}")
    except Exception as e:
        print(f"    (could not read listing page here: {type(e).__name__}: {str(e)[:80]})")

    # 5) (id, slug) BUILDABILITY -------------------------------------------
    print("\n[5] CAN WE BUILD /london/shows/{id}-{slug} PAIRS?")
    have_id = idk is not None
    have_slug = slugk is not None
    have_url = urlk is not None
    print(f"    id present: {have_id} ({idk})   slug present: {have_slug} ({slugk})   "
          f"url present: {have_url} ({urlk})")
    print("    samples (id | slug | url):")
    for sh in base["shows"][:5]:
        _, i = _first(sh, ID_KEYS)
        _, sl = _first(sh, SLUG_KEYS)
        _, u = _first(sh, URL_KEYS)
        print(f"      {str(i):>8} | {str(sl)[:40]:<40} | {str(u)[:70]}")

    # VERDICT ---------------------------------------------------------------
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)
    enough = False
    if listing_total and all_ids:
        enough = len(all_ids) >= listing_total * 0.98
    elif reported_total and all_ids:
        enough = len(all_ids) >= reported_total * 0.98
    can_build = have_id and (have_slug or have_url)

    if mechanism and enough and can_build:
        print(f"  VIABLE ✅  API discovery can replace the scroll.")
        print(f"    mechanism      : {mechanism}")
        print(f"    page size      : {page_size}")
        print(f"    collected      : {len(all_ids)} unique shows "
              f"(reported total {reported_total}, listing {listing_total})")
        print(f"    id+slug/url    : yes — pairs are buildable from "
              f"{idk!r} + {slugk or urlk!r}")
        print("  Next: I'll wire API discovery into todaytix_scraper.py behind a flag,")
        print("  with the scroll kept as automatic fallback.")
    else:
        print("  NEEDS A LOOK ⚠️  — one or more prerequisites unmet:")
        print(f"    pagination mechanism found: {bool(mechanism)} ({mechanism})")
        print(f"    reached full catalogue   : {enough} "
              f"(collected {len(all_ids)} vs listing {listing_total} / reported {reported_total})")
        print(f"    id + slug/url buildable  : {can_build}")
        print("  Paste this whole output back — the field names / shape above tell us")
        print("  exactly what to adjust (different param names, a cursor walk, or a")
        print("  filter if the API returns more than the listing's theatre shows).")
    return 0


# --- offline selftest -------------------------------------------------------

def selftest() -> int:
    print("SELFTEST: parsing + decision helpers (offline, no TodayTix)\n")

    # extract_shows across shapes
    assert len(extract_shows({"data": [{"id": 1}, {"id": 2}]})) == 2
    assert len(extract_shows({"data": {"shows": [{"id": 1}]}})) == 1
    assert len(extract_shows([{"id": 9}])) == 1
    assert extract_shows({"x": 5}) == []

    # pagination + total
    pg = extract_pagination({"data": [], "pagination": {"page": 1, "totalCount": 200}})
    assert pg and "totalCount" in json.dumps(pg)
    assert find_total({"pagination": {"totalCount": 200, "page": 1}}) == 200
    assert find_total({"a": {"total": 50}, "b": {"resultCount": 200}}) == 200

    # field detection
    sh = {"id": 43861, "slug": "arcadia", "showName": "Arcadia",
          "isAvailable": True, "categories": ["Play"]}
    assert _first(sh, ID_KEYS)[1] == 43861
    assert _first(sh, SLUG_KEYS)[1] == "arcadia"
    assert _first(sh, NAME_KEYS)[1] == "Arcadia"
    assert _first(sh, AVAIL_KEYS)[0] == "isAvailable"

    # compare / mechanism logic
    base_ids = list(range(0, 20))
    page2_ids = list(range(20, 40))         # disjoint -> advances
    same_ids = list(range(0, 20))           # identical -> ignored
    assert compare(base_ids, page2_ids)["n_new_vs_base"] == 20
    assert compare(base_ids, same_ids)["identical_to_base"] is True
    assert compare(base_ids, same_ids)["n_new_vs_base"] == 0

    # find_total ignores non-total ints
    assert find_total({"pageSize": 20, "page": 3}) is None

    print("  extract_shows / pagination / find_total / field-detection / compare: OK")
    print("\nSELFTEST: PASS")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe TodayTix /api/v2/shows for API-based discovery.")
    ap.add_argument("--selftest", action="store_true", help="run offline self-tests and exit")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    return probe()


if __name__ == "__main__":
    sys.exit(main())
