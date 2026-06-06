#!/usr/bin/env python3
"""
todaytix_discovery_probe.py  (v2 — scope & reliability drill-down)
==================================================================

READ-ONLY DIAGNOSTIC. Makes no repo changes. Run on a US GitHub runner.

v1 already proved the big thing: GET /api/v2/shows?location=2 paginates via
offset/limit, reports total=236, returns the whole catalogue in one call with
limit=500, and every show carries id + slug. v2 drills into the three details
needed to switch discovery onto the API *correctly* rather than by guesswork:

  [1] DUP / ID-LESS analysis — why a 236-entry response yielded only 225 unique
      ids, and whether offset-paging reliably reaches the same full set.
  [2] CATEGORY / TYPE distribution — the API over-returns vs the ~200-show
      theatre listing (it includes attractions like "st-pauls-cathedral"), so
      we need to see the category / productType / isPyos / isGa split to define
      a "theatre only" filter that matches the current scrape's scope.
  [3] SLUG GAPS — which shows lack a slug (so /london/shows/{id}-{slug} can't be
      built for them), and what they are (experiences? real theatre?).

Prints a SUMMARY with the likely theatre-show count after filtering. Paste the
whole output back.

Usage:
    python scraper/todaytix_discovery_probe.py
    python scraper/todaytix_discovery_probe.py --selftest   # offline
"""

from __future__ import annotations

import argparse
import collections
import json
import sys

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

SHOWS_URL = "https://api.todaytix.com/api/v2/shows"
LONDON = 2
TIMEOUT = 25.0

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.todaytix.com/",
    "Origin": "https://www.todaytix.com",
    "X-TT-Currency": "GBP",
}


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=0.6,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}), raise_on_status=False)
    ad = HTTPAdapter(max_retries=retry)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def extract_shows(payload) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [x for x in payload["data"] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def get(session, **params):
    p = {"location": LONDON}
    p.update(params)
    try:
        r = session.get(SHOWS_URL, params=p, timeout=TIMEOUT)
        j = r.json()
    except Exception as e:
        return [], None, f"{type(e).__name__}: {str(e)[:100]}"
    pag = j.get("pagination") if isinstance(j, dict) else None
    return extract_shows(j), pag, None


def label(v):
    """Compact label for a category/productType-ish value."""
    if isinstance(v, dict):
        return v.get("name") or v.get("slug") or v.get("type") or "(dict)"
    if isinstance(v, list):
        names = [label(x) for x in v]
        return "+".join(n for n in names if n) or "(empty)"
    if v is None:
        return "(none)"
    return str(v)


def cat_name(sh: dict) -> str:
    return label(sh.get("category"))


def analyse(shows: list[dict]):
    """Dedup by id; return (unique_shows, stats)."""
    by_id: dict = {}
    dups = 0
    idless = 0
    for sh in shows:
        i = sh.get("id")
        if i is None:
            idless += 1
            continue
        if i in by_id:
            dups += 1
        else:
            by_id[i] = sh
    return list(by_id.values()), {"raw": len(shows), "unique": len(by_id),
                                  "dups": dups, "idless": idless}


def probe() -> int:
    s = build_session()
    print("=" * 74)
    print("TodayTix discovery probe v2 — /api/v2/shows?location=2 scope & reliability")
    print("=" * 74)

    # --- [1] retrieval: single big call vs offset paging -------------------
    print("\n[1] RETRIEVAL & DUP / ID-LESS ANALYSIS")
    big_shows, big_pag, err = get(s, limit=500)
    if err:
        print(f"    limit=500 call failed: {err}")
        return 1
    total = (big_pag or {}).get("total")
    big_unique, big_stats = analyse(big_shows)
    print(f"    single call (limit=500): raw entries={big_stats['raw']}, "
          f"unique ids={big_stats['unique']}, duplicate-id rows={big_stats['dups']}, "
          f"id-less rows={big_stats['idless']}   (pagination.total={total})")

    # offset paging in pages of 50
    paged: dict = {}
    pages = 0
    offset = 0
    limit = 50
    cap = ((total or 300) // limit) + 3
    while pages < cap:
        chunk, _pag, e2 = get(s, limit=limit, offset=offset)
        pages += 1
        if e2 or not chunk:
            break
        before = len(paged)
        for sh in chunk:
            i = sh.get("id")
            if i is not None:
                paged.setdefault(i, sh)
        if len(chunk) < limit and len(paged) == before:
            break
        if len(paged) == before and len(chunk) < limit:
            break
        offset += limit
        if total and offset >= total:
            # one more page to be safe, then stop
            chunk, _p, e3 = get(s, limit=limit, offset=offset)
            pages += 1
            if not e3 and chunk:
                for sh in chunk:
                    i = sh.get("id")
                    if i is not None:
                        paged.setdefault(i, sh)
            break
    print(f"    offset paging (limit=50): pages={pages}, unique ids collected={len(paged)}")

    # canonical set = union of both methods (most complete)
    canon: dict = dict(paged)
    for sh in big_unique:
        canon.setdefault(sh["id"], sh)
    shows = list(canon.values())
    print(f"    CANONICAL unique shows (union of both methods): {len(shows)}")
    if big_stats["unique"] != len(paged):
        print(f"    NOTE: single-call unique ({big_stats['unique']}) != paged unique "
              f"({len(paged)}) — result set is mildly non-deterministic; paging+dedup "
              f"is the robust path.")

    # --- [2] category / type distribution ----------------------------------
    print("\n[2] CATEGORY / TYPE DISTRIBUTION  (theatre vs experience?)")

    def dist(fn, title, topn=30):
        c = collections.Counter(fn(sh) for sh in shows)
        print(f"    {title}:")
        for k, n in c.most_common(topn):
            print(f"        {n:4d}  {k}")

    dist(cat_name, "category.name")
    dist(lambda sh: label(sh.get("productType")), "productType")
    dist(lambda sh: label(sh.get("admissionType")), "admissionType")
    dist(lambda sh: label(sh.get("inventorySelectionMode")), "inventorySelectionMode")

    # cross-tab category × seat-selection signals
    print("\n    cross-tab by category.name  (count | isPyos=T | isGa=T | regAvail=T | slug-less):")
    cats = collections.Counter(cat_name(sh) for sh in shows)
    rows = []
    for cn, _ in cats.most_common(40):
        grp = [sh for sh in shows if cat_name(sh) == cn]
        n_pyos = sum(1 for sh in grp if sh.get("isPyos") is True)
        n_ga = sum(1 for sh in grp if sh.get("isGa") is True)
        n_reg = sum(1 for sh in grp if sh.get("areRegularTicketsAvailable") is True)
        n_noslug = sum(1 for sh in grp if not sh.get("slug"))
        ex = next((sh.get("slug") or sh.get("name") for sh in grp), "")
        rows.append((cn, len(grp), n_pyos, n_ga, n_reg, n_noslug, ex))
    for cn, n, p, g, rg, ns, ex in rows:
        print(f"        {cn:22.22} {n:4d} | {p:4d} | {g:4d} | {rg:4d} | {ns:4d}   e.g. {ex}")

    # --- [3] slug gaps ------------------------------------------------------
    print("\n[3] SLUG GAPS  (shows we can't build /london/shows/{id}-{slug} for)")
    noslug = [sh for sh in shows if not sh.get("slug")]
    print(f"    shows with NO slug: {len(noslug)} of {len(shows)}")
    for sh in noslug[:30]:
        print(f"        id={sh.get('id')!s:>8}  cat={cat_name(sh):20.20}  "
              f"name={str(sh.get('name') or sh.get('displayName'))[:40]}")

    # --- SUMMARY ------------------------------------------------------------
    # crude theatre vs non-theatre guess: theatre ~ has a slug AND isPyos (or
    # reg tickets) — just an estimate for the log; final filter decided from
    # the tables above.
    theatre_est = [sh for sh in shows if sh.get("slug") and (
        sh.get("isPyos") is True or sh.get("areRegularTicketsAvailable") is True)]
    print("\n" + "=" * 74)
    print("SUMMARY")
    print("=" * 74)
    print(f"    canonical unique shows           : {len(shows)}")
    print(f"    with a usable slug               : {sum(1 for sh in shows if sh.get('slug'))}")
    print(f"    rough 'theatre' estimate         : {len(theatre_est)} "
          f"(has slug AND isPyos|regTickets)  [final filter TBD from tables]")
    print(f"    pagination.total reported        : {total}")
    print("    -> If the theatre estimate lands near the listing's ~200, the filter is")
    print("       'has slug + isPyos/regTickets'; the category table confirms which")
    print("       category names are attractions/experiences to drop.")
    return 0


# --- offline selftest -------------------------------------------------------

def selftest() -> int:
    print("SELFTEST (offline)\n")
    sample = [
        {"id": 1, "slug": "ham", "name": "Hamilton",
         "category": {"name": "Musicals"}, "productType": "SHOW",
         "isPyos": True, "isGa": False, "areRegularTicketsAvailable": True,
         "admissionType": "TIMED", "inventorySelectionMode": "PYOS"},
        {"id": 1, "slug": "ham", "name": "Hamilton dup", "category": {"name": "Musicals"}},  # dup id
        {"id": 2, "slug": None, "name": "St Paul's", "category": {"name": "Attractions"},
         "productType": "EXPERIENCE", "isPyos": False, "isGa": True,
         "areRegularTicketsAvailable": True},
        {"slug": "x", "name": "no-id"},  # id-less
    ]
    uniq, st = analyse(sample)
    assert st["raw"] == 4 and st["unique"] == 2 and st["dups"] == 1 and st["idless"] == 1, st
    assert label({"name": "Musicals"}) == "Musicals"
    assert label(["a", {"name": "b"}]) == "a+b"
    assert label(None) == "(none)"
    assert cat_name(sample[0]) == "Musicals"
    assert cat_name(sample[2]) == "Attractions"
    noslug = [sh for sh in uniq if not sh.get("slug")]
    assert len(noslug) == 1 and noslug[0]["name"] == "St Paul's"
    print("  analyse (raw/unique/dups/idless), label, cat_name, slug-gap: OK")
    print("\nSELFTEST: PASS")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe v2: scope & reliability of /api/v2/shows.")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    return selftest() if args.selftest else probe()


if __name__ == "__main__":
    sys.exit(main())
