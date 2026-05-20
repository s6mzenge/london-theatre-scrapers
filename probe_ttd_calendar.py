"""
Probe TTD's calendar AJAX endpoint to discover the shape that returns
real performance IDs (the 6-digit numbers like 767461 in URLs such as
/shows/seats/7158/2026/5/21/2/14-00/767461).

Why this script exists
----------------------
TTD's seat-plan URL has the shape:
    /shows/seats/{show_id}/{Y}/{M}/{D}/{qty}/{HH-MM}/{perf_id}

When `{perf_id}` is the literal `0` (a placeholder TTD's own HTML emits
in its "Next Performances" tab), the seat-plan page only renders for
*internal* TTD navigation. From outside, TTD's JS appends ?m=cmst and
the server then returns "Tickets not available" because /0 can't be
resolved without the internal session context.

The real perf_id lives in TTD's calendar widget, which loads via an
AJAX call we have not yet verified. This script probes the candidate
endpoints/parameter shapes and prints whichever returns the perf IDs.
Run it ONCE; paste the output back so the scraper integration knows
which call to make.

Usage
-----
    pip install httpx[http2] beautifulsoup4 lxml
    python probe_ttd_calendar.py
    # optional: probe a different show
    python probe_ttd_calendar.py --show-id 7158 --year 2026 --month 5
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter

import httpx

BASE = "https://www.theatreticketsdirect.co.uk"

# The pattern we're hunting for in any response body. We capture the
# trailing path segment which is the perf_id we want.
PERF_URL_RE = re.compile(
    r"/shows/seats/(\d+)/(\d{4})/(\d{1,2})/(\d{1,2})/(\d+)/(\d{1,2})-(\d{2})/(\d+)"
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _summarise(text: str, show_id: int) -> tuple[int, int, list[tuple[str, str, int]]]:
    """Pull out (date, time, perf_id) triples from a response body.

    Returns (total_url_matches, non_zero_perf_id_count, samples_first_5).
    """
    matches = PERF_URL_RE.findall(text)
    same_show = [m for m in matches if int(m[0]) == show_id]
    non_zero = [m for m in same_show if int(m[7]) != 0]
    samples = []
    for sid, y, mo, d, qty, hh, mm, pid in non_zero[:5]:
        date = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        time = f"{int(hh):02d}:{mm}"
        samples.append((date, time, int(pid)))
    return len(same_show), len(non_zero), samples


def _try(client: httpx.Client, label: str, method: str, url: str,
         params=None, data=None, json=None, headers=None, show_id: int = 7158) -> bool:
    """Run one variant and print a one-line verdict.

    Returns True if the variant looks promising (non-zero perf IDs found).
    """
    print(f"\n--- {label} ---")
    print(f"    {method} {url}")
    if params:
        print(f"    params={params}")
    if data:
        print(f"    data={data}")
    if json:
        print(f"    json={json}")
    try:
        r = client.request(method, url, params=params, data=data,
                           json=json, headers=headers or {}, timeout=20)
    except httpx.HTTPError as e:
        print(f"    ERROR: {type(e).__name__}: {e}")
        return False
    print(f"    -> HTTP {r.status_code}, "
          f"Content-Type={r.headers.get('content-type', '?')}, "
          f"length={len(r.text)}")
    if r.status_code >= 400:
        # Show the first ~200 chars for debugging
        body_preview = r.text[:200].replace("\n", " ")
        print(f"    body preview: {body_preview!r}")
        return False
    total, non_zero, samples = _summarise(r.text, show_id)
    print(f"    URL matches for show {show_id}: {total} total, "
          f"{non_zero} with real perf IDs")
    if non_zero:
        print(f"    SAMPLES (date, time, perf_id):")
        for s in samples:
            print(f"      {s}")
        print(f"    *** PROMISING ***")
        return True
    elif total:
        print(f"    All matches use the /0 placeholder — same as the show page")
    else:
        # Maybe the response uses a different URL shape — show a snippet so
        # we can spot patterns we haven't seen yet
        body_preview = r.text[:400].replace("\n", " ")
        print(f"    no /shows/seats/ URLs found. body preview: {body_preview!r}")
    return False


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--show-id", type=int, default=7158,
                   help="Show ID to probe (default: 7158 = Globe Midsummer)")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--month", type=int, default=5)
    args = p.parse_args(argv)

    sid = args.show_id
    y = args.year
    m = args.month
    show_slug_path = f"/shows/{sid}/a-midsummer-night%E2%80%99s-dream---globe-theatre-tickets"
    referer = f"{BASE}{show_slug_path}"

    # First warm a session by visiting the show page (in case the
    # endpoint requires session cookies)
    client = httpx.Client(
        http2=True,
        headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
        follow_redirects=True,
    )
    print(f"Warming session with {referer}")
    try:
        warm = client.get(referer, timeout=20)
        print(f"  -> HTTP {warm.status_code}, cookies={list(client.cookies.keys())}")
    except httpx.HTTPError as e:
        print(f"  WARNING: warmup failed: {e}")

    # Variants to try, ordered from most-likely to least-likely. The
    # first that yields real perf IDs wins.
    ajax_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Accept": "text/html, */*; q=0.01",
    }

    hits = []
    if _try(client, "POST /show/bindcalendar (form, lowercase keys)",
            "POST", f"{BASE}/show/bindcalendar",
            data={"id": str(sid), "month": str(m), "year": str(y)},
            headers=ajax_headers, show_id=sid):
        hits.append("POST /show/bindcalendar form lowercase")
    if _try(client, "POST /Show/BindCalendar (form, PascalCase URL)",
            "POST", f"{BASE}/Show/BindCalendar",
            data={"id": str(sid), "month": str(m), "year": str(y)},
            headers=ajax_headers, show_id=sid):
        hits.append("POST /Show/BindCalendar form lowercase")
    if _try(client, "POST /show/bindcalendar (form, PascalCase keys)",
            "POST", f"{BASE}/show/bindcalendar",
            data={"Id": str(sid), "Month": str(m), "Year": str(y)},
            headers=ajax_headers, show_id=sid):
        hits.append("POST /show/bindcalendar form PascalCase")
    if _try(client, "POST /show/bindcalendar (form, ShowId key)",
            "POST", f"{BASE}/show/bindcalendar",
            data={"ShowId": str(sid), "Month": str(m), "Year": str(y)},
            headers=ajax_headers, show_id=sid):
        hits.append("POST /show/bindcalendar form ShowId")
    if _try(client, "GET /show/bindcalendar (query params)",
            "GET", f"{BASE}/show/bindcalendar",
            params={"id": str(sid), "month": str(m), "year": str(y)},
            headers=ajax_headers, show_id=sid):
        hits.append("GET /show/bindcalendar query")
    if _try(client, "GET /calendar/{id}/{month},{year} (the visible URL)",
            "GET", f"{BASE}/calendar/{sid}/{m},{y}",
            headers=ajax_headers, show_id=sid):
        hits.append("GET /calendar/{id}/{m},{y} as AJAX")
    if _try(client, "POST /shows/Calendar/{id}/{slug} (canonical from earlier)",
            "POST", f"{BASE}/shows/Calendar/{sid}/the-show",
            data={"id": str(sid), "month": str(m), "year": str(y)},
            headers=ajax_headers, show_id=sid):
        hits.append("POST /shows/Calendar/{id}/{slug}")

    print("\n" + "=" * 60)
    if hits:
        print(f"Promising variants ({len(hits)}):")
        for h in hits:
            print(f"  ✓ {h}")
        print("\nNext step: tell the scraper integration which variant to use.")
        return 0
    else:
        print("No variant returned real perf IDs.")
        print("If you can capture the AJAX call from Chrome devtools "
              "(F12 → Network → click a date on the calendar widget), "
              "paste the request URL + method + body and we can extend "
              "the probe to match.")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
