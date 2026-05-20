"""
LoveTheatre booking-flow calendar API enrichment
=================================================

The SSR detail page on www.lovetheatre.com publishes JSON-LD
TheaterEvent blocks with `offers.price`, but that price is the
show-wide "Tickets From £X" sidebar value duplicated into every event
— not the per-performance currently-available minimum. Confirmed
across multiple shows: every show shows 1 unique JSON-LD price across
all 7 emitted perfs, while the calendar API shows real per-perf
variation that matches the on-site date-picker labels.

The booking SPA at secure.lovetheatre.com (an Ingresso Whitelabel
deployment) populates its date-picker via /api/calendar/{show_id}/,
which DOES expose per-performance accurate minimums. This module
wraps that API and merges its data into the existing per-show
Performance list emitted by lovetheatre_scraper.py.

Two wins from this integration:

  1. Price correctness. `min_combined` is the cheapest currently-
     available seat (face value + booking fee). Often differs from
     the JSON-LD value by tens of pounds, sometimes more.

  2. Coverage. JSON-LD typically caps at ~5–10 upcoming events; the
     calendar API covers the full booking window. For long-running
     shows like Wicked, this is 250+ extra perfs per show that the
     SSR-only path doesn't see at all.

Usage shape inside the main scraper:

    import lovetheatre_calendar as ltc

    # Once, after build_session:
    ltc.warm_session(session)

    # Per show, after parse_show() returns its dict:
    ltc.enrich_show(session, detail_dict)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

BASE = "https://secure.lovetheatre.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_S = 20

# Parse calendar API time display: "2.00 PM", "11:30 AM", etc. The
# separator can be either dot or colon — observed both in the wild.
TIME_RE = re.compile(r"^\s*(\d{1,2})[.:](\d{2})\s*(AM|PM)\s*$", re.I)

# Booking URLs we get from the JSON-LD look like:
#   https://secure.lovetheatre.com/book/1HM7N-a-midsummer-night-s-dream-26s/#perf=1HM7N-M
# We need both the show_id (1HM7N) and the slug to construct API URLs.
BOOK_URL_RE = re.compile(
    r"^https?://secure\.lovetheatre\.com/book/([A-Z0-9]+)-([a-z0-9\-]+)/",
    re.IGNORECASE,
)


@dataclass
class CalendarPerf:
    """One performance as returned by /api/calendar/{show_id}/."""
    perf_id: str
    date: str                       # "YYYY-MM-DD"
    time_display: str               # "2.00 PM" (as API returns it)
    time_hhmm: str | None           # "14:00" (normalized for joining)
    min_combined: float | None      # cheapest available, seat + fee
    no_singles_min_combined: float | None  # cheapest for pairs+
    special_offer: bool | None
    max_seats: int | None           # booking quantity cap; also a soft
                                    # availability signal (lower = closer
                                    # to sold-out at the cheap band)
    currency_code: str | None       # "gbp" — lowercase from API


# ---------------------------------------------------------------------------
# Session warmup
# ---------------------------------------------------------------------------

def warm_session(session: requests.Session) -> bool:
    """Set the sessionid cookie for secure.lovetheatre.com by GETting
    the booking root once. The cookie is shared across all subsequent
    API calls within the same session, so this is called once at
    scraper startup rather than per show.

    Returns False if warmup failed, in which case calendar calls will
    likely also fail — caller can choose to continue without enrichment
    or to abort the run."""
    url = f"{BASE}/"
    try:
        r = session.get(
            url,
            headers={"Accept": "text/html", "User-Agent": USER_AGENT},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        log.warning("Calendar session warmup failed: %s", e)
        return False
    if r.status_code >= 400:
        log.warning("Calendar warmup status %d", r.status_code)
        return False
    if "sessionid" not in session.cookies:
        log.warning("Calendar warmup didn't set sessionid cookie")
        return False
    log.info("Calendar session warmed (sessionid acquired)")
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_float(v) -> float | None:
    """Coerce JSON value to float. The calendar API ships numbers
    natively, but downstream merging also touches JSON-LD strings."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "."))
        except ValueError:
            return None
    return None


def _parse_time(display: str | None) -> str | None:
    if not display:
        return None
    m = TIME_RE.match(display)
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    ampm = m.group(3).upper()
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mn:02d}"


def _extract_show_id_and_slug(performances: list[dict]) -> tuple[str, str] | None:
    """Find a perf with a book_url and parse show_id + slug from it.
    Returns None if no perf has a usable URL (e.g. show with no
    booking flow active — 'Tickets not currently available')."""
    for p in performances or []:
        url = p.get("book_url") or p.get("offer_url")
        if not isinstance(url, str):
            continue
        m = BOOK_URL_RE.match(url)
        if m:
            return m.group(1), m.group(2)
    return None


# ---------------------------------------------------------------------------
# Calendar API call + parse
# ---------------------------------------------------------------------------

def fetch_calendar(session: requests.Session, show_id: str) -> dict | None:
    """One GET against /api/calendar/{show_id}/. Returns parsed JSON
    or None on failure. Assumes warm_session() has been called."""
    url = f"{BASE}/api/calendar/{show_id}/"
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Referer": f"{BASE}/book/{show_id}/",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE,
    }
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        log.warning("Calendar API request failed for %s: %s", show_id, e)
        return None
    if r.status_code != 200:
        log.warning("Calendar API returned status %d for %s",
                    r.status_code, show_id)
        return None
    try:
        return r.json()
    except ValueError as e:
        log.warning("Calendar API returned non-JSON for %s: %s", show_id, e)
        return None


def flatten_calendar(cal: dict) -> list[CalendarPerf]:
    """Walk the nested years/months/days/perfs response into a flat
    list of CalendarPerf records.

    The response mixes shapes at each level: dict values hold real
    data, but some keys map to bools (meta flags — observed
    `years.special_offer: false` and `years.special_offer: true` at
    the year level). We filter to keys that look like dates and skip
    everything else."""
    out: list[CalendarPerf] = []
    if not isinstance(cal, dict):
        return out
    years = cal.get("years")
    if not isinstance(years, dict):
        return out

    for year_key, yo in years.items():
        # Real year keys are 4-digit numeric strings; meta keys are
        # things like "special_offer". Filter by shape, not by isinstance.
        if not (isinstance(year_key, str) and year_key.isdigit()
                and len(year_key) == 4):
            continue
        if not isinstance(yo, dict):
            continue
        months = yo.get("months")
        if not isinstance(months, dict):
            continue

        for month_key, mo in months.items():
            if not (isinstance(month_key, str) and month_key.isdigit()):
                continue
            if not isinstance(mo, dict):
                continue
            days = mo.get("days")
            if not isinstance(days, dict):
                continue

            for day_key, do in days.items():
                if not (isinstance(day_key, str) and day_key.isdigit()):
                    continue
                if not isinstance(do, dict):
                    continue
                date = (f"{year_key}-"
                        f"{int(month_key):02d}-"
                        f"{int(day_key):02d}")
                for perf in (do.get("perfs") or []):
                    if not isinstance(perf, dict):
                        continue
                    pid = perf.get("perf_id")
                    if not isinstance(pid, str) or not pid:
                        continue
                    display = perf.get("time") or ""
                    out.append(CalendarPerf(
                        perf_id=pid,
                        date=date,
                        time_display=display,
                        time_hhmm=_parse_time(display),
                        min_combined=_to_float(perf.get("min_combined")),
                        no_singles_min_combined=_to_float(
                            perf.get("no_singles_min_combined")),
                        special_offer=perf.get("special_offer"),
                        max_seats=perf.get("max_seats"),
                        currency_code=perf.get("currency_code"),
                    ))
    return out


# ---------------------------------------------------------------------------
# Merge into Performance dicts
# ---------------------------------------------------------------------------

# Fields we always add to every Performance dict, even if calendar data
# is unavailable — keeps the output schema uniform.
_NEW_FIELDS_NULL = {
    "min_combined_price": None,
    "no_singles_min_combined_price": None,
    "special_offer": None,
    "max_seats": None,
    "price_source": "jsonld_only",
}


def _merge(
    jsonld_perfs: list[dict],
    cal_perfs: list[CalendarPerf],
    show_id: str,
    slug: str,
) -> list[dict]:
    """Match calendar perfs to JSON-LD perfs by perf_id. JSON-LD perfs
    gain calendar fields; calendar perfs not in JSON-LD are appended
    as stub Performance records."""
    cal_by_pid = {c.perf_id: c for c in cal_perfs}
    out: list[dict] = []
    matched: set[str] = set()

    for jp in jsonld_perfs:
        new = dict(jp)
        new.update(_NEW_FIELDS_NULL)
        pid = jp.get("perf_id")
        cal = cal_by_pid.get(pid) if pid else None
        if cal is not None:
            new["min_combined_price"] = cal.min_combined
            new["no_singles_min_combined_price"] = cal.no_singles_min_combined
            new["special_offer"] = cal.special_offer
            new["max_seats"] = cal.max_seats
            new["price_source"] = "calendar_api"
            matched.add(pid)
        out.append(new)

    # Calendar-only perfs → stub Performance records.
    # The book_url pattern is fixed; reconstruct it so downstream
    # consumers still get a clickable booking link.
    book_root = f"{BASE}/book/{show_id}-{slug}/"
    for cal in cal_perfs:
        if cal.perf_id in matched:
            continue
        currency = (cal.currency_code or "").upper() or None
        out.append({
            # Standard Performance fields (mostly None — no JSON-LD source)
            "perf_id": cal.perf_id,
            "iso": None,
            "date": cal.date,
            "time": cal.time_hhmm,
            "end_date": None,
            "status": None,
            "price": None,
            "currency": currency,
            "availability": None,
            "valid_from": None,
            "book_url": f"{book_root}#perf={cal.perf_id}",
            "offer_url": None,
            "venue_name": None,
            # Calendar fields
            "min_combined_price": cal.min_combined,
            "no_singles_min_combined_price": cal.no_singles_min_combined,
            "special_offer": cal.special_offer,
            "max_seats": cal.max_seats,
            "price_source": "calendar_only",
        })

    # Sort by (date, time) — JSON-LD perfs come first (~5–10), calendar-
    # only stubs follow. Without an explicit sort the calendar-only tail
    # ends up at the bottom regardless of date, which is confusing.
    out.sort(key=lambda p: (p.get("date") or "", p.get("time") or ""))
    return out


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------

def enrich_show(
    session: requests.Session,
    show_detail: dict,
) -> tuple[int, int, int] | None:
    """Fetch the calendar API for this show and merge into
    show_detail['performances'] in place.

    Returns (matched, jsonld_only, calendar_only) counts on success,
    or None if the show has no booking flow (e.g. 'Tickets not
    currently available' — no usable URL in any JSON-LD perf).

    On API failure (vs no-booking-flow), we still attach the new
    fields set to None on every JSON-LD perf so the output schema
    stays uniform — downstream consumers see consistent keys.
    """
    perfs = show_detail.get("performances") or []
    ids = _extract_show_id_and_slug(perfs)
    if ids is None:
        # No booking flow — leave perfs alone but stamp price_source
        # so downstream code knows enrichment was skipped, not failed.
        for p in perfs:
            p.update(_NEW_FIELDS_NULL)
            p["price_source"] = "no_booking_flow"
        return None
    show_id, slug = ids

    cal = fetch_calendar(session, show_id)
    if cal is None:
        # API failure — stamp jsonld_only on every perf
        for p in perfs:
            p.update(_NEW_FIELDS_NULL)
        return 0, len(perfs), 0

    cal_perfs = flatten_calendar(cal)
    merged = _merge(perfs, cal_perfs, show_id=show_id, slug=slug)
    show_detail["performances"] = merged

    matched = sum(1 for p in merged if p["price_source"] == "calendar_api")
    jsonld_only = sum(1 for p in merged if p["price_source"] == "jsonld_only")
    cal_only = sum(1 for p in merged if p["price_source"] == "calendar_only")
    return matched, jsonld_only, cal_only
