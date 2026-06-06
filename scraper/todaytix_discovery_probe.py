#!/usr/bin/env python3
"""
todaytix_discovery_probe.py  (v2.1 — scope & reliability, hardened)
===================================================================

READ-ONLY DIAGNOSTIC. Makes no repo changes. Run on a US GitHub runner.

v1 proved the big thing: GET /api/v2/shows?location=2 paginates via offset/limit,
reports total=236, returns the whole catalogue in one call with limit=500, and
every show carries id + slug. v2 drills into the three details needed to switch
discovery onto the API correctly:

  [1] DUP / ID-LESS analysis + offset-paging reliability (236 raw vs 225 unique).
  [2] CATEGORY / TYPE distribution — the API over-returns vs the ~200-show
      theatre listing (includes attractions like "st-pauls-cathedral"), so we
      need the category / productType / isPyos / isGa split to define a
      "theatre only" filter that matches the current scrape's scope.
  [3] SLUG GAPS — which shows lack a slug and what they are.

HARDENING (the prior run hit a transient EMPTY response and aborted): every
request now retries empty / non-JSON bodies itself — urllib3's Retry only
catches 5xx, not a 200 with an empty body — prints the HTTP status + a body
snippet when a response is unusable, makes offset-paging the primary path with
the single bulk call as a cross-check, and only gives up if everything fails.

Usage:
    python scraper/todaytix_discovery_probe.py
    python scraper/todaytix_discovery_probe.py --selftest   # offline
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

SHOWS_URL = "https://api.todaytix.com/api/v2/shows"
LONDON = 2
TIMEOUT = 30.0
TRIES = 4  # per-call attempts on empty / non-JSON bodies

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


def fetch_json(session, *, tries: int = TRIES, **params) -> dict:
    """GET /shows with self-retry on empty / non-JSON / unexpected bodies.

    A parsed dict carrying 'data' and/or 'pagination' (even with data == [])
    is a valid response and ends the retry loop — that is how end-of-pages
    looks. Anything else (empty body, HTML challenge, connection error) is
    retried with backoff. Returns the last attempt's diagnostics on failure.
    """
    p = {"location": LONDON}
    p.update(params)
    last = {"ok": False, "status": None, "shows": [], "pag": None,
            "body": "", "error": "no attempt"}
    for attempt in range(tries):
        try:
            r = session.get(SHOWS_URL, params=p, timeout=TIMEOUT)
        except Exception as e:
            last = {"ok": False, "status": None, "shows": [], "pag": None,
                    "body": "", "error": f"{type(e).__name__}: {str(e)[:120]}"}
            time.sleep(0.8 * (attempt + 1))
            continue
        body = r.text or ""
        try:
            j = r.json()
        except Exception:
            j = None
        if isinstance(j, dict) and ("data" in j or "pagination" in j):
            return {"ok": True, "status": r.status_code,
                    "shows": extract_shows(j),
                    "pag": j.get("pagination"), "body": "", "error": None}
        last = {"ok": False, "status": r.status_code, "shows": [], "pag": None,
                "body": body[:200].replace("\n", " "),
                "error": "empty/non-JSON/unexpected body"}
        time.sleep(0.8 * (attempt + 1))
    return last


def label(v):
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


def dedup(shows: list[dict]):
    by_id: dict = {}
    dups = idless = 0
    for sh in shows:
        i = sh.get("id")
        if i is None:
            idless += 1
            continue
        if i in by_id:
            dups += 1
        else:
            by_id[i] = sh
    return by_id, dups, idless


def probe() -> int:
    s = build_session()
    print("=" * 74)
    print("TodayTix discovery probe v2.1 — /api/v2/shows?location=2 scope & reliability")
    print("=" * 74)

    # --- [1] retrieval -----------------------------------------------------
    print("\n[1] RETRIEVAL & DUP / ID-LESS ANALYSIS")

    big = fetch_json(s, limit=500)
    if big["ok"]:
        by_id, dups, idless = dedup(big["shows"])
        total = (big["pag"] or {}).get("total")
        print(f"    single call (limit=500): status={big['status']}, "
              f"raw entries={len(big['shows'])}, unique ids={len(by_id)}, "
              f"dup-id rows={dups}, id-less rows={idless}  (pagination.total={total})")
        big_unique = by_id
    else:
        print(f"    single call (limit=500): FAILED status={big['status']} "
              f"error={big['error']!r} body[:200]={big['body']!r}")
        print("    (continuing with offset paging, which is the path we'd ship anyway)")
        big_unique = {}
        total = None

    # offset paging in pages of 50 (primary, robust path)
    paged: dict = {}
    pages = empty_pages = failed_pages = 0
    offset, limit = 0, 50
    cap = ((total or 300) // limit) + 4
    while pages < cap:
        chunk = fetch_json(s, limit=limit, offset=offset)
        pages += 1
        if not chunk["ok"]:
            failed_pages += 1
            print(f"    [page offset={offset}] FAILED status={chunk['status']} "
                  f"error={chunk['error']!r} body[:120]={chunk['body'][:120]!r}")
            break
        if total is None and chunk["pag"]:
            total = (chunk["pag"] or {}).get("total")
            cap = ((total or 300) // limit) + 4
        if not chunk["shows"]:
            empty_pages += 1
            break  # past the end
        before = len(paged)
        for sh in chunk["shows"]:
            i = sh.get("id")
            if i is not None:
                paged.setdefault(i, sh)
        offset += limit
        if total and offset >= total:
            break
    print(f"    offset paging (limit=50): pages={pages}, unique ids collected={len(paged)}, "
          f"empty_pages={empty_pages}, failed_pages={failed_pages}  (pagination.total={total})")

    # canonical = union of whatever succeeded
    canon: dict = dict(paged)
    for i, sh in big_unique.items():
        canon.setdefault(i, sh)
    shows = list(canon.values())
    print(f"    CANONICAL unique shows (union of both methods): {len(shows)}")
    if big["ok"] and len(big_unique) != len(paged):
        print(f"    NOTE: single-call unique ({len(big_unique)}) != paged unique "
              f"({len(paged)}) — result set is mildly non-deterministic; paging+dedup "
              f"is the robust path.")

    if not shows:
        print("\nVERDICT: could not retrieve any shows this run (transient API/WAF "
              "response — see status/body above). The API is reachable in principle "
              "(v1 succeeded); just re-run the Action. Discovery stays on the scroll "
              "until a clean pull.")
        return 1

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

    print("\n    cross-tab by category.name  (count | isPyos=T | isGa=T | regAvail=T | slug-less):")
    cats = collections.Counter(cat_name(sh) for sh in shows)
    for cn, _ in cats.most_common(40):
        grp = [sh for sh in shows if cat_name(sh) == cn]
        n_pyos = sum(1 for sh in grp if sh.get("isPyos") is True)
        n_ga = sum(1 for sh in grp if sh.get("isGa") is True)
        n_reg = sum(1 for sh in grp if sh.get("areRegularTicketsAvailable") is True)
        n_noslug = sum(1 for sh in grp if not sh.get("slug"))
        ex = next((sh.get("slug") or sh.get("name") for sh in grp), "")
        print(f"        {cn:22.22} {len(grp):4d} | {n_pyos:4d} | {n_ga:4d} | "
              f"{n_reg:4d} | {n_noslug:4d}   e.g. {ex}")

    # --- [3] slug gaps ------------------------------------------------------
    print("\n[3] SLUG GAPS  (cannot build /london/shows/{id}-{slug} for these)")
    noslug = [sh for sh in shows if not sh.get("slug")]
    print(f"    shows with NO slug: {len(noslug)} of {len(shows)}")
    for sh in noslug[:30]:
        print(f"        id={sh.get('id')!s:>8}  cat={cat_name(sh):20.20}  "
              f"name={str(sh.get('name') or sh.get('displayName'))[:40]}")

    # --- SUMMARY ------------------------------------------------------------
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

class _FakeResp:
    def __init__(self, status, payload, raw_text=None):
        self.status_code = status
        self._payload = payload
        self.text = raw_text if raw_text is not None else (
            json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON")
        return self._payload


class _FakeSession:
    """Returns queued responses in order, to exercise the retry loop."""
    def __init__(self, queue):
        self.queue = list(queue)

    def get(self, url, params=None, timeout=None):
        return self.queue.pop(0)


def selftest() -> int:
    print("SELFTEST (offline)\n")

    # retry loop: two empty bodies, then a good one
    good = {"code": 200, "data": [{"id": 1, "slug": "a"}],
            "pagination": {"total": 1, "offset": 0, "limit": 50}}
    sess = _FakeSession([_FakeResp(200, None, ""),        # empty body -> retry
                         _FakeResp(200, None, "<html>"),  # non-JSON -> retry
                         _FakeResp(200, good)])           # good -> stop
    # patch sleep to no-op
    orig_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        res = fetch_json(sess, tries=4, limit=50)
    finally:
        time.sleep = orig_sleep
    assert res["ok"] and len(res["shows"]) == 1 and res["pag"]["total"] == 1, res

    # all-empty -> failure with diagnostics
    sess2 = _FakeSession([_FakeResp(403, None, "blocked")] * 4)
    time.sleep = lambda *_: None
    try:
        res2 = fetch_json(sess2, tries=4, limit=50)
    finally:
        time.sleep = orig_sleep
    assert not res2["ok"] and res2["status"] == 403 and "blocked" in res2["body"], res2

    # end-of-pages: empty data list is a VALID response (not retried)
    sess3 = _FakeSession([_FakeResp(200, {"data": [], "pagination": {"total": 1}})])
    res3 = fetch_json(sess3, tries=4, limit=50, offset=999)
    assert res3["ok"] and res3["shows"] == [], res3

    # dedup + labels
    by_id, dups, idless = dedup([{"id": 1}, {"id": 1}, {"slug": "x"}, {"id": 2}])
    assert len(by_id) == 2 and dups == 1 and idless == 1
    assert label({"name": "Musicals"}) == "Musicals"
    assert label(["a", {"name": "b"}]) == "a+b"
    assert cat_name({"category": {"name": "Plays"}}) == "Plays"

    print("  fetch_json retry-on-empty / fail-with-diagnostics / end-of-pages: OK")
    print("  dedup + label + cat_name: OK")
    print("\nSELFTEST: PASS")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe v2.1: scope & reliability of /api/v2/shows.")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    return selftest() if args.selftest else probe()


if __name__ == "__main__":
    sys.exit(main())
