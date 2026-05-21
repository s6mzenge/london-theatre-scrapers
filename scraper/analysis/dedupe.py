"""
London theatre scrape deduper, v2.
==================================

Reads the 7 per-source JSONs produced by the scrapers (or an all-scrapers
zip artifact), normalizes show titles and venues, groups records by
(canonical_title, canonical_venue), and emits a single unified JSON with
per-source attribution preserved on every value.

Five stages of matching are now layered:

  Stage 1  Aggressive normalization (unicode, mojibake, diacritics, articles,
           franchise prefixes, "theatre" suffix, parenthetical title suffixes,
           known generic subtitles).
  Stage 2  Canonical venue / title alias registry (VENUE_ALIASES below).
  Stage 3  Fuzzy matching within each canonical venue, using a gated
           token-set ratio. Catches typos and word-order variants that
           survive Stages 1+2.
  Stage 5  Manual overrides loaded from a JSON or YAML file. Always wins —
           use this for the genuinely-stubborn cases the algorithm can't
           handle (titles with no shared words, ticketing-variant splits).

(Stage 4 — performance-overlap corroboration for borderline fuzzy pairs —
is in the brainstorm but not yet implemented. The fuzzy gate plus the
override file cover the cases that matter on present data.)

Usage
-----
    python dedupe.py PATH                       # folder of *.json or an all-scrapers.zip
    python dedupe.py PATH --out OUT_DIR         # default: ./dedupe_output/
    python dedupe.py PATH --no-fuzzy            # skip Stage 3
    python dedupe.py PATH --overrides FILE      # apply Stage 5 from FILE (.json or .yaml)

Examples (Windows):
    python dedupe.py "C:\\Users\\morit\\Downloads\\all-scrapers (1).zip"
    python dedupe.py "C:\\Users\\morit\\Downloads\\all-scrapers (1).zip" --overrides overrides.yaml

Examples (macOS/Linux):
    python dedupe.py ~/Downloads/all-scrapers.zip
    python dedupe.py data/ --overrides overrides.yaml

Optional dependencies
---------------------
    rapidfuzz   — proper fuzzy matching (recommended); the script falls back
                  to a stdlib `difflib` implementation if not installed
    pyyaml      — required only if your overrides file is YAML; .json is fine
                  without any extra installs

Install with:
    pip install rapidfuzz pyyaml

Outputs (in OUT_DIR):
    unified.json     — the merged shows, one record per cluster
    review.json      — singletons, orphans, and fuzzy-pair review queue
    report.txt       — human-readable summary

Iterating
---------
The whole point of running this locally is to perfect the matching before
shipping it to CI. The workflow:

  1. Run the script. Read the printed report.
  2. Inspect `unified.json` → look at the `fuzzy_merges` section to verify
     the auto-merged pairs are correct.
  3. Inspect `review.json` → the `fuzzy_review_pairs` section lists borderline
     pairs (65–80 score). Promote the right ones via overrides.
  4. Inspect `singleton_clusters` for any genuinely-mergeable residuals; add
     them to your `overrides.yaml` under `force_merge`.
  5. Inspect `fuzzy_merges` for any false positives; add them to your
     `overrides.yaml` under `force_split`.
  6. Re-run. The override file always wins.

Notes on the design choices
---------------------------
* The match key is (norm_title, norm_venue). A play at two venues counts as two
  shows (correct: e.g. Jesus Christ Superstar runs concurrently at the Palladium
  and Theatre Royal Drury Lane — those are different productions).
* Performance-level unification: within a cluster, performances are joined on
  (date, time). Each performance keeps a per-source sub-object so prices and
  booking URLs from each seller are preserved AND linkable.
* Field-level reconciliation: top-level fields like `description` are picked
  by a configurable priority (longest non-null by default), with a
  `_field_provenance` map recording which source contributed each value.
* The venues registry (VENUE_ALIASES) is small on purpose — it grows as the
  review queue surfaces unmatched pairs that should be unified.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Canonical-name registry (Stage 2)
# ---------------------------------------------------------------------------
#
# Seeded with patterns observed in the actual scrape data. The dedupe pipeline
# normalizes venue strings, then looks them up here; if the normalized form
# matches an alias, the canonical form on the left becomes the cluster key.
#
# Add new aliases as you see them in `review.json` — they'll fold those records
# into existing clusters on the next run.

VENUE_ALIASES: dict[str, list[str]] = {
    # Add aliases of the SAME venue here. The key is the canonical
    # name we want to use; values are alternative spellings/abbreviations.
    # Note: case, smart quotes, "theatre" suffix, and trailing whitespace
    # are normalized out before lookup, so you only need to list aliases
    # that survive normalization (e.g. "Apollo Victoria" vs "Apollo Victoria Theatre"
    # both normalize to "apollo victoria" — no entry needed).

    # West End mainstream — seeded from observed patterns
    "Apollo Victoria Theatre": [],
    "His Majesty's Theatre": [],
    "Lyceum Theatre": [],
    "Novello Theatre": [],
    "Victoria Palace Theatre": [],

    # Renamed venue (Royal Opera House → Royal Ballet and Opera, 2024)
    "Royal Ballet and Opera": [
        "Royal Opera House",
    ],

    # Outdoor and unusual venues with multiple naming conventions
    "Regent's Park Open Air Theatre": [
        "Open Air Theatre",
        "Open Air Theatre, Regent's Park",
    ],
    "Troubadour Wembley Park Theatre": [
        "Wembley Park Theatre",
    ],
    "Royal Festival Hall": [
        "Royal Festival Hall - Southbank Centre",
    ],
    "St Paul's Church": [
        "St Pauls Church, Covent Garden",
        "St. Paul's Church",
    ],

    # Pop-up / production-specific venues. Be careful: aliasing 'Kit Kat Club'
    # → 'Kit Kat Club at the Playhouse Theatre' is correct only while the
    # Cabaret production runs. When Cabaret closes, REMOVE this entry.
    #
    # The 'Playhouse Theatre' alias below is ALSO production-specific: while
    # Cabaret runs, sources that list the venue as just "Playhouse Theatre"
    # (olt, seatplan) are referring to the same physical building. Once
    # Cabaret closes, any new production at the Playhouse will be a
    # different venue from a dedupe perspective, and this alias must be
    # removed alongside the Kit Kat Club ones.
    "Kit Kat Club at the Playhouse Theatre": [
        "Kit Kat Club",
        "Kit Kat Club At Playhouse Theatre",
        "Kit Kat Club at Playhouse Theatre",
        "Playhouse Theatre",
    ],

    # OLT uses "Drury Lane, Theatre Royal" / "Haymarket, Theatre Royal" with
    # the comma-reversed form. Other sources use natural word order.
    "Theatre Royal Drury Lane": [
        "Drury Lane, Theatre Royal",
    ],
    "Theatre Royal Haymarket": [
        "Haymarket, Theatre Royal",
    ],

    # Sadler's Wells — OLT shortens, others use "Theatre" suffix (which is
    # stripped by normalization anyway, but the entry makes provenance clear)
    "Sadler's Wells Theatre": [
        "Sadler's Wells",
    ],

    # Shakespeare's Globe is universally referred to as "Globe Theatre" in
    # London context — there's no other Globe Theatre.
    "Shakespeare's Globe": [
        "Globe Theatre",
    ],

    # Hippodrome Casino — Magic Mike Live's venue, with five distinct spellings
    "London Hippodrome": [
        "London Hippodrome (Over 18s Only)",
        "Hippodrome Casino",
        "Hippodrome Casino (Over 18s Only)",
        "The Hippodrome Casino",
        "The Theatre at Hippodrome Casino (over 18s only)",
    ],

    # Wilton's Music Hall — ttd drops the apostrophe-s
    "Wilton's Music Hall": [
        "Wilton Music Hall",
    ],

    # Young Vic — ttd uses bare name, others use "(Main House)" suffix
    "Young Vic": [
        "Young Vic (Main House)",
    ],

    # National Theatre auditoria — some sources prefix with "National Theatre"
    # (e.g. "National Theatre Lyttelton"); OLT/others use the bare auditorium name
    "Lyttelton Theatre": [
        "National Theatre Lyttelton",
    ],
    "Olivier Theatre": [
        "National Theatre Olivier",
    ],
    "Dorfman Theatre": [
        "National Theatre Dorfman",
    ],

    # Empress Museum, Earls Court (Come Alive! production)
    "Empress Museum": [
        "Empress Museum, Earls Court",
    ],

    # Menier Chocolate Factory — OLT prefixes with "Menier Theatre (...)"
    "Menier Chocolate Factory": [
        "Menier Theatre (Menier Chocolate Factory)",
    ],

    # The Arts at Marble Arch — variant with sponsor suffix
    "The Arts at Marble Arch": [
        "The Arts at Marble Arch powered by TodayTix",
    ],

    # Queen Elizabeth Hall — split by ", Southbank Centre" suffix in some sources
    "Queen Elizabeth Hall": [
        "Queen Elizabeth Hall, Southbank Centre",
        "Queen Elizabeth Hall - Southbank Centre",
    ],

    # Display-consistency aliases (added 2026-05-19 after audit revealed the
    # same venue_norm rendering with two different display strings across
    # clusters). Dedupe was already merging correctly — this just makes the
    # canonical display name stable instead of dependent on which source
    # happens to be first in the cluster.
    "The Old Vic": [
        "Old Vic",
        "Old Vic Theatre",
    ],
    "The Other Palace": [
        "Other Palace",
        # 'The Other Palace - Main Theatre' is the formal name of the larger
        # auditorium inside The Other Palace; aliasing collapses both onto
        # the building name. The Other Palace STUDIO is a separate smaller
        # space and is deliberately NOT aliased here.
        "The Other Palace - Main Theatre",
    ],
    "Underbelly Boulevard Theatre": [
        "Underbelly Boulevard",
    ],
    "The Vaults": [
        "Vaults",
        "The Vaults Theatre",
    ],
    "Royal Court Theatre": [
        "Royal Court",
        "The Royal Court Theatre",
    ],

    # Cross-source naming aliases (added 2026-05-19 after audit revealed
    # several missed-merge cases where the same physical venue was listed
    # under two distinct names by different sources, splitting one show
    # into two separate clusters at different venue_norms.)

    # @sohoplace is the marketing form (with leading '@' and no space) used
    # by olt, lovetheatre, ttd. Other sources spell it
    # "Soho Place". Same theatre near Tottenham Court Road. Affects: Boy
    # Who Harnessed the Wind, Tao of Glass — both jump 4→7 sources.
    "Soho Place": [
        "@sohoplace",
        "@sohoplace Theatre",
    ],

    # Barbican — five sources call the auditorium "Barbican" or "Barbican
    # Theatre", two call the whole complex "Barbican Centre". Treat as
    # synonymous in West End context. (The Pit and the Concert Hall are
    # also inside the Barbican Centre but neither appears in our data;
    # if they ever do, revisit.) Affects: Death Note, High Society —
    # both jump 5→7 sources.
    "Barbican Theatre": [
        "Barbican",
        "Barbican Centre",
    ],

    # Evolution London — temporary venue in Battersea Park hosting Grease:
    # The Immersive Movie Musical. Two sources call it "Evolution London -
    # Battersea", two call it "Battersea Park". Same physical pop-up;
    # production-specific alias that may need removing when the run ends.
    "Evolution London - Battersea": [
        "Battersea Park",
    ],
}

# Title prefix strings to strip when normalizing. The result of stripping is
# the canonical title for matching purposes; the original is still preserved
# on each source record. Keep this list tight — some prefixes ARE the title.
STRIPPABLE_TITLE_PREFIXES = [
    "disney's",
    "disneys",
    "rsc's",
    "rscs",
]

# Sources in their preferred order for tie-breaking when reconciling fields.
# Earlier = preferred. Reasoning: olt has the richest editorial content
# (descriptions, cast, FAQ); todaytix has the best price data;
# the rest fill in gaps.
SOURCE_PRIORITY = [
    "olt",
    "todaytix",
    "lovetheatre",
    "seatplan",
    "ttd",
]


# ---------------------------------------------------------------------------
# Per-source schema — how to extract fields from each source's show records
# ---------------------------------------------------------------------------

def _venue_str(v: Any) -> str | None:
    """Some sources have venue as a dict, others as a string."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, str):
        return v
    return None


SHOW_SCHEMAS: dict[str, dict[str, Any]] = {
    "todaytix": {
        "title":        lambda s: s.get("name"),
        "venue":        lambda s: _venue_str(s.get("venue")),
        "id":           lambda s: s.get("id") or s.get("slug"),
        "url":          lambda s: s.get("url"),
        "performances": lambda s: s.get("showtimes") or [],
        "description":  lambda s: s.get("description"),
    },
    "olt": {
        "title":        lambda s: s.get("name"),
        "venue":        lambda s: _venue_str(s.get("venue")),
        "id":           lambda s: s.get("id"),
        "url":          lambda s: s.get("url"),
        "performances": lambda s: s.get("performances") or [],
        "description":  lambda s: s.get("description_text"),
    },
    "lovetheatre": {
        "title":        lambda s: s.get("name"),
        "venue":        lambda s: s.get("venue_name") or s.get("venue_text"),
        "id":           lambda s: s.get("post_id") or s.get("show_id") or s.get("slug"),
        "url":          lambda s: s.get("url") or s.get("detail_canonical"),
        "performances": lambda s: s.get("performances") or [],
        "description":  lambda s: s.get("description_full") or s.get("product_description"),
    },
    "seatplan": {
        "title":        lambda s: s.get("name"),
        "venue":        lambda s: s.get("venue_name"),
        "id":           lambda s: s.get("sku") or s.get("slug"),
        "url":          lambda s: s.get("url") or s.get("detail_canonical"),
        "performances": lambda s: s.get("performances") or [],
        "description":  lambda s: s.get("description_full") or s.get("description_short"),
    },
    "ttd": {
        "title":        lambda s: s.get("name"),
        "venue":        lambda s: s.get("venue_name") or s.get("venue_text"),
        "id":           lambda s: s.get("id") or s.get("sku") or s.get("slug"),
        "url":          lambda s: s.get("url") or s.get("detail_canonical"),
        "performances": lambda s: s.get("performances") or [],
        "description":  lambda s: s.get("description_full")
                                   or s.get("description_short")
                                   or s.get("product_description"),
    },
}


# SeatPlan's detail-page JSON-LD repeats the *show-wide* lowPrice on every
# performance (Globe yard-standing at £6 leaks onto matinees where the
# yard isn't on sale). When seatplan_availability.py has run, each
# performance carries the verified per-perf min/max scraped from the
# ticketing page's inline fireCrmEvent payload — that's the real
# currently-available range and we prefer it whenever it's present.
#
# verified_price_source semantics (set by seatplan_availability.py):
#   "ticketing_page" — verified successfully; trust verified_min_price
#   "no_seats"       — page loaded but no fireCrmEvent → not on sale
#                      → drop SeatPlan's price from the cross-source min
#                        (don't drag it down to a fake show-wide £6)
#   "fetch_failed"   — network/HTTP failure → fall back to low_price
#   "skipped"        — perf wasn't checked → fall back to low_price
#   (missing field)  — availability pass never ran → fall back to low_price
def _seatplan_price_from(p: dict) -> float | None:
    source = p.get("verified_price_source")
    if source == "no_seats":
        return None
    verified = p.get("verified_min_price")
    if verified is not None:
        return verified
    return p.get("low_price")


def _seatplan_price_to(p: dict) -> float | None:
    if p.get("verified_price_source") == "no_seats":
        return None
    return p.get("verified_max_price")


def _seatplan_available(p: dict) -> bool | None:
    source = p.get("verified_price_source")
    if source == "no_seats":
        return False
    if source == "ticketing_page" and p.get("verified_min_price") is not None:
        return True
    # No verified data (or fetch failed) — fall back to the JSON-LD
    # availability hint from the detail page.
    availability = p.get("availability")
    if availability:
        return "InStock" in availability
    return None


# Per-performance field extractors. Each source phrases dates/times/prices
# differently; this normalizes to a canonical shape so we can join across
# sources on (date, time).
# TTD's detail-page JSON-LD emits the show-wide minimum on every
# TheaterEvent.offers.price (e.g. £7 for *A Midsummer Night's Dream*
# at the Globe, even though most evening performances actually start
# at £13). When ttd_availability.py has run, each performance carries
# a verified_min_price scraped from TTD's bindcalendar response — the
# real per-perf "from £X.XX" the calendar widget displays. Prefer it
# whenever it's present.
#
# verified_price_source semantics (set by ttd_availability.py):
#   "ttd_calendar"     — verified successfully; trust verified_min_price
#   "not_in_calendar"  — month was fetched but this (date, time) wasn't
#                        returned → not on sale → drop TTD's price so the
#                        cross-source min isn't dragged down by JSON-LD bogosity
#   "fetch_failed"     — network/HTTP failure → fall back to raw price
#                        (still bogus, but better than nothing)
#   "skipped"          — perf wasn't checked → fall back to raw price
#   (missing field)    — availability pass never ran → fall back to raw price
def _ttd_price_from(p: dict) -> float | None:
    source = p.get("verified_price_source")
    if source == "not_in_calendar":
        return None
    verified = p.get("verified_min_price")
    if verified is not None:
        return verified
    # No verified data — fall back to raw JSON-LD price. This is the
    # show-wide leak (probably wrong) but we'd rather show a stale
    # number than no number at all when verification was never attempted.
    return p.get("price")


PERF_SCHEMAS: dict[str, dict[str, Any]] = {
    "todaytix": {
        "date":       lambda p: p.get("local_date") or p.get("date"),
        "time":       lambda p: p.get("local_time") or p.get("time"),
        "price_from": lambda p: p.get("low_price_value"),
        "price_to":   lambda p: None,
        "currency":   lambda p: p.get("currency"),
        "book_url":   lambda p: p.get("booking_url") or p.get("book_url"),
        "available":  lambda p: (p.get("seats_available", 0) > 0)
                                if isinstance(p.get("seats_available"), int) else None,
    },
    "olt": {
        "date":       lambda p: p.get("date"),
        "time":       lambda p: p.get("time"),
        "price_from": lambda p: p.get("min_price"),
        "price_to":   lambda p: p.get("max_price"),
        "currency":   lambda p: "GBP",
        "book_url":   lambda p: p.get("book_url"),
        "available":  lambda p: p.get("available"),
    },
    "lovetheatre": {
        "date":       lambda p: p.get("date"),
        "time":       lambda p: p.get("time"),
        "price_from": lambda p: p.get("min_combined_price") or p.get("price"),
        "price_to":   lambda p: None,
        "currency":   lambda p: p.get("currency"),
        "book_url":   lambda p: p.get("book_url") or p.get("offer_url"),
        "available":  lambda p: (
            "InStock" in (p.get("availability") or "")
            if p.get("availability")
            else (p["max_seats"] > 0
                  if isinstance(p.get("max_seats"), int)
                  else None)
        ),
    },
    "seatplan": {
        "date":       lambda p: p.get("date"),
        "time":       lambda p: p.get("time"),
        # Prefer the ticketing-page-verified min/max (set by
        # seatplan_availability.py) over the detail-page JSON-LD, which
        # is the show-wide minimum and misleads for performances where
        # the cheapest tier isn't actually on sale.
        "price_from": _seatplan_price_from,
        "price_to":   _seatplan_price_to,
        "currency":   lambda p: p.get("currency"),
        "book_url":   lambda p: p.get("book_url"),
        "available":  _seatplan_available,
    },
    "ttd": {
        "date":       lambda p: p.get("date"),
        "time":       lambda p: p.get("time"),
        # Prefer the bindcalendar-verified per-perf price (set by
        # ttd_availability.py) over the detail-page JSON-LD, which is
        # the show-wide minimum and misleads for performances where the
        # cheapest tier isn't actually on sale.
        "price_from": _ttd_price_from,
        "price_to":   lambda p: None,
        "currency":   lambda p: p.get("currency"),
        # verified_book_url has the real perf_id; raw book_url has /0
        # placeholders for any perf the existing scraper couldn't resolve.
        "book_url":   lambda p: p.get("verified_book_url") or p.get("book_url"),
        "available":  lambda p: ("InStock" in (p.get("availability") or ""))
                                if p.get("availability") else None,
    },
}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Pre-compiled regex helpers
_WS_RE = re.compile(r"\s+")
_VENUE_SUFFIX_RE = re.compile(r"\s*-\s*[^-]+$")   # for stripping "Show - Venue" titles


_MOJIBAKE_SEQUENCE_RE = re.compile(r"[\xC2\xC3\xE2][\x80-\xBF]+")


def _fix_mojibake(s: str) -> str:
    """Recover from UTF-8/CP1252 double-encoding.

    LoveTheatre's scraper emits strings like 'A Midsummer Nightâ\\x80\\x99s Dream'
    (the bytes of U+2019 right single quote, decoded as latin-1, then re-encoded
    as UTF-8 and finally decoded as UTF-8 again). The recovery is to round-trip
    the affected sub-strings back through latin-1 → utf-8.

    We have to be surgical: lovetheatre's descriptions mix mojibake-encoded smart
    quotes with already-correctly-decoded ones (U+2019 appears in the same string
    as ‘â\\x80\\x99’), so a whole-string round-trip would die on the first valid
    U+2019 (which is outside latin-1). Instead, we identify each mojibake run by
    its fingerprint — a leading U+00C2/C3/E2 followed by one or more U+0080-00BF
    continuation bytes, never legitimate in natural text — and decode just those
    runs individually, leaving valid Unicode untouched.
    """
    if not s:
        return s

    def _repl(m: "re.Match[str]") -> str:
        chunk = m.group(0)
        try:
            return chunk.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return chunk

    s = _MOJIBAKE_SEQUENCE_RE.sub(_repl, s)

    # Residue cleanup: 'Â ' (U+00C2 + regular space) is the corruption pattern
    # left when an upstream HTML parser converted the trailing U+00A0 of a
    # mojibake'd NBSP (Â\xA0) into a regular space, breaking the regex's two-
    # byte fingerprint. The intent was clearly a single space, so we collapse
    # the orphaned 'Â' to a space here. Conservative: only matches the exact
    # 'Â<space>' sequence, never bare 'Â' (which is a real character in French
    # names like 'Châtelet' that we don't want to corrupt).
    s = s.replace("\xC2 ", " ")
    return s


def _ascii_fold(s: str) -> str:
    """Unicode-normalize: fix mojibake, then smart quotes → ', dashes → -, NFC."""
    if s is None:
        return ""
    s = _fix_mojibake(s)
    s = unicodedata.normalize("NFC", s)
    # Replace common typography
    return (
        s
        .replace("\u2019", "'")  # right single quote → apostrophe
        .replace("\u2018", "'")  # left single quote
        .replace("\u201C", '"')  # left double quote
        .replace("\u201D", '"')  # right double quote
        .replace("\u2013", "-")  # en dash
        .replace("\u2014", "-")  # em dash
        .replace("\u00A0", " ")  # non-breaking space
    )


def _strip_diacritics(s: str) -> str:
    """Remove accents/diacritics for matching purposes only.

    "La bohème" → "la boheme", "Léon" → "leon". Used inside normalize_title
    and normalize_venue so accented and unaccented variants cluster together.
    The original display string is preserved separately.
    """
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_title(raw: str | None, venue_norm: str | None = None) -> str:
    """Lower-case, strip articles/punctuation/prefixes/diacritics, and strip
    any '- <Venue>' suffix when it matches the record's own venue.

    Examples:
        "Mamma Mia!"               → "mamma mia"
        "Disney's The Lion King"   → "lion king"
        "La bohème"                → "la boheme"
        "Jesus Christ Superstar - The Palladium" (at London Palladium)
                                   → "jesus christ superstar"
    """
    if not raw:
        return ""

    s = _ascii_fold(raw).lower().strip()
    s = _strip_diacritics(s)

    # Strip "- <venue>" or "- the <venue>" suffix when it matches this
    # record's own venue, INCLUDING the venue's aliases. This handles things
    # like "A Midsummer Night's Dream - Globe Theatre" at "Shakespeare's
    # Globe" (alias) by checking each alias spelling normalizes to the same
    # canonical as the record's venue.
    if venue_norm:
        m = re.search(r"^(.+?)\s*-\s*(?:the\s+)?([^-]+)\s*$", s)
        if m:
            rest, suffix = m.group(1), m.group(2).strip()
            suffix_norm, _ = normalize_venue(suffix)
            if suffix_norm == venue_norm:
                s = rest.strip()

    # Strip "(<anything>)" and "[<anything>]" suffix from the title.
    # The clustering key is venue-anchored, so this can't accidentally merge
    # productions at different venues.
    m = re.search(r"\s*[\(\[][^)\]]+[\)\]]\s*$", s)
    if m:
        s = s[: m.start()].strip()

    # Strip known generic subtitles like "Cinderella: The Pantomime" or
    # "Six the Musical". This list is conservative on purpose — only
    # subtitles that universally just classify the show rather than identify
    # a specific variant. Adding "singalong" or "karaoke" here would be
    # incorrect (those denote distinct ticketed events).
    KNOWN_SUBTITLES = (
        ": the musical", ": a musical", ": a new musical",
        ": the pantomime", ": a pantomime",
        ": a comedy", ": a tragedy", ": a play", ": the play",
        " the musical", " the pantomime",
    )
    for sub in KNOWN_SUBTITLES:
        if s.endswith(sub):
            s = s[: -len(sub)].strip()
            break

    # Strip strippable franchise prefixes
    for prefix in STRIPPABLE_TITLE_PREFIXES:
        if s.startswith(prefix + " "):
            s = s[len(prefix) + 1:].strip()

    # Strip leading article
    for article in ("the ", "a ", "an "):
        if s.startswith(article):
            s = s[len(article):].strip()
            break

    # Strip terminal punctuation that's purely decorative
    s = s.rstrip("!?.,")

    # Strip ALL punctuation and collapse whitespace for the final key
    s = re.sub(r"[^\w\s]", "", s)
    s = _WS_RE.sub(" ", s).strip()

    return s


def _normalize_venue_string(raw: str) -> str:
    """Helper: normalize a venue string to its lookup form."""
    s = _ascii_fold(raw).lower().strip()
    s = _strip_diacritics(s)
    # Strip leading "the "
    if s.startswith("the "):
        s = s[4:]
    # Strip trailing "theatre" / "theater"
    for suffix in (" theatre", " theater"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Strip trailing ", london" / " london"
    for suffix in (", london", " london"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Strip all punctuation, collapse whitespace
    s = re.sub(r"[^\w\s]", "", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# Build lookup map at import time: normalized form → canonical name
_VENUE_LOOKUP: dict[str, str] = {}
for canonical, aliases in VENUE_ALIASES.items():
    _VENUE_LOOKUP[_normalize_venue_string(canonical)] = canonical
    for alias in aliases:
        _VENUE_LOOKUP[_normalize_venue_string(alias)] = canonical


def normalize_venue(raw: str | None) -> tuple[str | None, str | None]:
    """Return (cluster_key, canonical_display_name).

    The cluster_key is what we use for grouping records. The display_name is
    what we show in unified.json. If the venue is in the registry we use the
    canonical display, AND we set the cluster_key to the normalization of
    that canonical name — so all aliases collapse to the same cluster key.
    Otherwise we use the original (cleaned) name and its own normalization.
    """
    if not raw:
        return (None, None)
    norm = _normalize_venue_string(raw)
    if not norm:
        return (None, None)
    canonical = _VENUE_LOOKUP.get(norm)
    if canonical:
        # Use the canonical name's normalized form as the cluster key, so all
        # aliases of one venue end up in the same cluster.
        return (_normalize_venue_string(canonical), canonical)
    # Not in registry — use the input cleaned of whitespace/smart quotes
    return (norm, _ascii_fold(raw).strip())


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SourceRecord:
    """One show as it appeared in one source."""
    source: str
    source_id: str | None
    title_raw: str
    venue_raw: str | None
    url: str | None
    description: str | None
    performances: list[dict]                       # raw, source-shaped
    # Computed during normalization
    title_norm: str = ""
    venue_norm: str | None = None
    venue_canonical: str | None = None             # display name for output
    # How this record landed in its final cluster. Default is the normal
    # Stage 1+2 path ("exact_normalized"). Stage 3 fuzzy merges keep this
    # default since the cluster as a whole is exact-matched at its key.
    # Stage 4 sets "orphan_rescued_by_title" on rescued records so consumers
    # can tell they were attached via title-only matching, not venue+title.
    matched_via: str = "exact_normalized"
    # Stash the raw show dict for any field we want to look up later
    raw: dict = field(default_factory=dict)


@dataclass
class UnifiedPerformance:
    date: str
    time: str
    min_price: float | None
    max_price: float | None
    currency: str | None
    any_available: bool | None
    sources: dict[str, dict]    # {source_name: {price_from, price_to, book_url, available}}


@dataclass
class UnifiedShow:
    id: str                                        # slug derived from canonical title+venue
    title: str
    venue: str
    title_norm: str
    venue_norm: str
    source_count: int
    sources: list[dict]                            # provenance per source
    description: str | None
    min_price_gbp: float | None
    max_price_gbp: float | None
    performance_count: int
    performances: list[dict]
    field_provenance: dict[str, str]
    match_confidence: int                           # 100 for exact-normalized; lower in future


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _slugify(title: str, venue: str) -> str:
    s = f"{title}__{venue}".lower()
    s = re.sub(r"[^\w]+", "-", s).strip("-")
    return s[:120]


def load_sources(path: Path) -> dict[str, dict]:
    """Load all source JSONs from a directory or all-scrapers.zip."""
    out: dict[str, dict] = {}
    expected = set(SHOW_SCHEMAS.keys())

    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                name = Path(info.filename).name
                if not name.endswith(".json"):
                    continue
                stem = name[:-5]
                if stem in expected:
                    with z.open(info) as f:
                        out[stem] = json.load(f)
    elif path.is_dir():
        for src in expected:
            p = path / f"{src}.json"
            if p.exists():
                out[src] = json.loads(p.read_text(encoding="utf-8"))
    else:
        raise SystemExit(f"path is neither a directory nor a zip: {path}")

    missing = expected - set(out.keys())
    if missing:
        print(f"  WARNING: missing source files: {sorted(missing)}", file=sys.stderr)

    return out


def extract_records(sources_data: dict[str, dict]) -> list[SourceRecord]:
    """Flatten the 7 source JSONs into a list of SourceRecord."""
    records: list[SourceRecord] = []
    for src, payload in sources_data.items():
        schema = SHOW_SCHEMAS[src]
        for show in payload.get("shows", []):
            # Apply mojibake recovery at extraction time so both clustering
            # AND display see the corrected text. (Without this, the singleton
            # entries in review.json still appear with mangled bytes even
            # though their normalized form matches correctly.)
            title_raw = _fix_mojibake(schema["title"](show) or "")
            venue_raw = schema["venue"](show)
            if venue_raw:
                venue_raw = _fix_mojibake(venue_raw)
            description_raw = schema["description"](show)
            if description_raw:
                description_raw = _fix_mojibake(description_raw)
            records.append(SourceRecord(
                source=src,
                source_id=str(schema["id"](show)) if schema["id"](show) is not None else None,
                title_raw=title_raw,
                venue_raw=venue_raw,
                url=schema["url"](show),
                description=description_raw,
                performances=schema["performances"](show),
                raw=show,
            ))
    return records


# ---------------------------------------------------------------------------
# Matching pipeline (Stages 1 + 2)
# ---------------------------------------------------------------------------

def normalize_all(records: list[SourceRecord]) -> None:
    """In-place: fill in title_norm / venue_norm / venue_canonical."""
    for r in records:
        venue_norm, venue_canonical = normalize_venue(r.venue_raw)
        r.venue_norm = venue_norm
        r.venue_canonical = venue_canonical
        r.title_norm = normalize_title(r.title_raw, venue_norm)


def cluster_by_key(records: list[SourceRecord]) -> tuple[
        dict[tuple[str, str], list[SourceRecord]],
        list[SourceRecord]]:
    """Return (clusters_by_key, records_without_venue)."""
    clusters: dict[tuple[str, str], list[SourceRecord]] = defaultdict(list)
    no_venue: list[SourceRecord] = []
    for r in records:
        if not r.venue_norm:
            no_venue.append(r)
            continue
        if not r.title_norm:
            no_venue.append(r)
            continue
        clusters[(r.title_norm, r.venue_norm)].append(r)
    return clusters, no_venue


# ---------------------------------------------------------------------------
# Stage 3: Fuzzy matching within venues
# ---------------------------------------------------------------------------
#
# After Stage 1+2 exact clustering, some clusters at the same venue are
# really the same show under different naming. We catch those with a gated
# token-set ratio: two titles match if their tokens overlap enough AND the
# overlapping substance is significant.
#
# The "gate" prevents the classic false positive of two titles sharing one
# short common token (e.g. "Six" vs "Six - Singalong" — both contain "six"
# but they're different events). The gate requires either two shared tokens
# OR six characters of shared content before any fuzzy score is computed.
#
# rapidfuzz is preferred (fast, well-tested); we fall back to stdlib difflib
# if it's not installed. The user-facing scores are 0-100 in both cases.

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False


def _difflib_token_set_ratio(a: str, b: str) -> int:
    """Hand-rolled token_set_ratio using stdlib SequenceMatcher.
    Used only if rapidfuzz is not installed."""
    from difflib import SequenceMatcher
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0
    inter = set_a & set_b
    sorted_inter = " ".join(sorted(inter))
    combined1 = " ".join(sorted(inter | (set_a - set_b)))
    combined2 = " ".join(sorted(inter | (set_b - set_a)))
    r1 = SequenceMatcher(None, sorted_inter, combined1).ratio() if sorted_inter else 0.0
    r2 = SequenceMatcher(None, sorted_inter, combined2).ratio() if sorted_inter else 0.0
    r3 = SequenceMatcher(None, combined1, combined2).ratio()
    return int(round(max(r1, r2, r3) * 100))


# Thresholds — empirically calibrated against the residual cases in the
# London theatre data. AUTO ≥ 75 merges; 60 ≤ score < 75 surfaces for review.
FUZZY_AUTO_THRESHOLD = 75
FUZZY_REVIEW_THRESHOLD = 60

# Gate: minimum shared substance to even consider a fuzzy match
MIN_INTER_TOKENS = 2
MIN_INTER_CHARS = 6


def fuzzy_score(t1: str, t2: str) -> int:
    """Gated token-set ratio. Returns 0 if the overlap is too thin, otherwise
    the rapidfuzz/difflib token_set_ratio (0-100)."""
    if not t1 or not t2:
        return 0
    tokens1 = set(t1.split())
    tokens2 = set(t2.split())
    if not tokens1 or not tokens2:
        return 0
    inter = tokens1 & tokens2
    inter_chars = sum(len(t) for t in inter)
    if len(inter) < MIN_INTER_TOKENS and inter_chars < MIN_INTER_CHARS:
        return 0
    if _RAPIDFUZZ_AVAILABLE:
        return int(_rf_fuzz.token_set_ratio(t1, t2))
    return _difflib_token_set_ratio(t1, t2)


def fuzzy_merge_within_venues(
    clusters: dict[tuple[str, str], list[SourceRecord]],
    auto_threshold: int = FUZZY_AUTO_THRESHOLD,
    review_threshold: int = FUZZY_REVIEW_THRESHOLD,
) -> tuple[
        dict[tuple[str, str], list[SourceRecord]],
        list[dict],
        list[dict]]:
    """Find clusters with similar titles at the same venue and merge them.

    Key safety invariant: never merge two clusters if the union would contain
    multiple records from the same source. Two different titles from the same
    source are almost always two different shows. This single rule prevents
    almost all false-positive fuzzy merges — e.g. at London Coliseum many
    operas share "- English National Opera" suffix (so token-set ratio is
    high across unrelated operas), but every source that lists both Tosca
    and La Traviata as separate records would block their merger.

    Edges are processed in descending-score order so the most confident merges
    happen first, preventing order-dependence pathologies.

    Returns (new_clusters, fuzzy_merges, review_pairs). review_pairs contains
    both genuine borderline cases (score in [review, auto)) and high-scoring
    edges that were *blocked* by the within-source-duplicate guard — the
    latter are flagged with `blocked: "within_source_duplicate"`.
    """
    # Group cluster keys by venue
    venue_to_keys: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key in clusters:
        venue_to_keys[key[1]].append(key)

    # Union-Find on cluster keys
    parent: dict[tuple[str, str], tuple[str, str]] = {key: key for key in clusters}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # Records per Union-Find root, kept current so we can detect within-source
    # duplicates BEFORE committing a union.
    root_records: dict[tuple[str, str], list[SourceRecord]] = {
        key: list(records) for key, records in clusters.items()
    }

    # Collect all candidate edges first, then sort highest-score-first
    candidate_edges: list[tuple[int, tuple[str, str], tuple[str, str], str]] = []
    for venue, keys in venue_to_keys.items():
        if len(keys) < 2:
            continue
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                ka, kb = keys[i], keys[j]
                t_a, t_b = ka[0], kb[0]
                if t_a == t_b:
                    continue
                score = fuzzy_score(t_a, t_b)
                if score >= review_threshold:
                    candidate_edges.append((score, ka, kb, venue))

    candidate_edges.sort(key=lambda x: -x[0])

    fuzzy_merges: list[dict] = []
    review_pairs: list[dict] = []

    for score, ka, kb, venue in candidate_edges:
        if score < auto_threshold:
            # Only report as a borderline pair if the two haven't already
            # been merged transitively by higher-scoring edges
            if find(ka) != find(kb):
                review_pairs.append({
                    "venue": venue,
                    "title_a": ka[0],
                    "title_b": kb[0],
                    "score": score,
                })
            continue

        ra, rb = find(ka), find(kb)
        if ra == rb:
            continue  # already merged transitively

        combined = root_records[ra] + root_records[rb]
        # Guard: within-source duplicates after merge?
        seen_sources: set[str] = set()
        has_dup = False
        for r in combined:
            if r.source in seen_sources:
                has_dup = True
                break
            seen_sources.add(r.source)

        if has_dup:
            review_pairs.append({
                "venue": venue,
                "title_a": ka[0],
                "title_b": kb[0],
                "score": score,
                "blocked": "within_source_duplicate",
            })
            continue

        # Safe to merge: rb absorbs ra
        parent[ra] = rb
        root_records[rb] = combined
        del root_records[ra]
        fuzzy_merges.append({
            "venue": venue,
            "title_a": ka[0],
            "title_b": kb[0],
            "score": score,
        })

    # Rebuild clusters according to final Union-Find roots
    new_clusters: dict[tuple[str, str], list[SourceRecord]] = defaultdict(list)
    for key, records in clusters.items():
        root = find(key)
        new_clusters[root].extend(records)

    return dict(new_clusters), fuzzy_merges, review_pairs


# ---------------------------------------------------------------------------
# Stage 4: Orphan rescue (title-only match for records missing venue)
# ---------------------------------------------------------------------------
#
# Some source records arrive with a venue we can't parse — most commonly
# because lovetheatre's scraper occasionally fails to extract `venue_name`
# even when it's plainly in the page URL. Those records get dropped to the
# orphan list in Stage 1+2 (the cluster key is (title_norm, venue_norm) and
# venue_norm is empty).
#
# Stage 4 takes one last pass at attaching them: for each orphan, find the
# best title match across all existing clusters and attach if the score is
# high enough. This is title-only matching, so the threshold is set MUCH
# higher than Stage 3's within-venue threshold (default 90 vs 75) — without
# venue as a discriminator, "Romeo and Juliet" at Globe and at Harold Pinter
# would otherwise wrongly merge.
#
# Safety invariants:
#   1. Score >= ORPHAN_RESCUE_THRESHOLD (default 90, conservative).
#   2. Gated token overlap (same gate as Stage 3's fuzzy_score).
#   3. Tie-breaker on equal scores: prefer the cluster with the most sources
#      (the one that's most likely to be the canonical entry).
#   4. Within-source-duplicate guard: never attach an orphan to a cluster
#      that already contains a record from the orphan's source.
#   5. Ambiguity guard: if two or more distinct clusters score within
#      ORPHAN_RESCUE_AMBIGUITY_MARGIN of the best, refuse to pick. Without
#      this, an orphan titled 'Romeo and Juliet' (no venue) would attach
#      to whichever ENB-Coliseum or Harold-Pinter R&J cluster happens to
#      have more sources — flipping at random as data changes. Refusing
#      to rescue keeps the orphan visible for human review.

ORPHAN_RESCUE_THRESHOLD = 90
ORPHAN_RESCUE_AMBIGUITY_MARGIN = 5


def rescue_orphans_by_title(
    clusters: dict[tuple[str, str], list[SourceRecord]],
    orphans: list[SourceRecord],
    threshold: int = ORPHAN_RESCUE_THRESHOLD,
) -> tuple[
        dict[tuple[str, str], list[SourceRecord]],
        list[SourceRecord],
        list[dict]]:
    """Attempt to attach each orphan to its best-matching cluster by title.

    The orphan inherits the cluster's venue (venue_norm, venue_raw,
    venue_canonical) so downstream output is consistent.

    Returns (clusters, remaining_orphans, rescue_log). The rescue_log
    records every decision — attached, blocked, or score-too-low — so the
    user can audit what happened.
    """
    remaining: list[SourceRecord] = []
    rescue_log: list[dict] = []

    # Snapshot cluster keys so we iterate against a stable list even as
    # we mutate `clusters` by appending orphans
    cluster_keys = list(clusters.keys())

    for orphan in orphans:
        if not orphan.title_norm:
            rescue_log.append({
                "orphan_title": orphan.title_raw,
                "orphan_source": orphan.source,
                "orphan_source_id": orphan.source_id,
                "status": "no_title_norm",
            })
            remaining.append(orphan)
            continue

        # Score against every cluster, keep all that meet the threshold so
        # we can tie-break by cluster size
        candidates: list[tuple[int, int, tuple[str, str]]] = []
        for key in cluster_keys:
            score = fuzzy_score(orphan.title_norm, key[0])
            if score >= threshold:
                # Sort key: (score desc, then source_count desc as tiebreaker)
                candidates.append((score, len(clusters[key]), key))

        if not candidates:
            rescue_log.append({
                "orphan_title": orphan.title_raw,
                "orphan_source": orphan.source,
                "orphan_source_id": orphan.source_id,
                "status": "no_match_above_threshold",
            })
            remaining.append(orphan)
            continue

        # Best match: highest score, then most-sourced cluster
        candidates.sort(key=lambda x: (-x[0], -x[1]))
        best_score, _, best_key = candidates[0]

        # Ambiguity guard: if a second distinct cluster scores within
        # ORPHAN_RESCUE_AMBIGUITY_MARGIN of the best, refuse to pick. The
        # orphan title alone isn't enough signal to disambiguate between
        # two near-equally-good clusters (e.g. 'Romeo and Juliet' could
        # match a Globe production AND a Harold Pinter production both at
        # score 100). Falling back to "most sources wins" would give a
        # plausible-sounding wrong answer; safer to leave it as an orphan.
        if len(candidates) > 1:
            runner_up_score, _, runner_up_key = candidates[1]
            if runner_up_score >= best_score - ORPHAN_RESCUE_AMBIGUITY_MARGIN:
                rescue_log.append({
                    "orphan_title": orphan.title_raw,
                    "orphan_source": orphan.source,
                    "orphan_source_id": orphan.source_id,
                    "matched_cluster_title": best_key[0],
                    "matched_cluster_venue": best_key[1],
                    "score": best_score,
                    "alternative_match": {
                        "title": runner_up_key[0],
                        "venue": runner_up_key[1],
                        "score": runner_up_score,
                    },
                    "status": "blocked_ambiguous_match",
                })
                remaining.append(orphan)
                continue

        # Within-source-duplicate guard
        target_sources = {r.source for r in clusters[best_key]}
        if orphan.source in target_sources:
            rescue_log.append({
                "orphan_title": orphan.title_raw,
                "orphan_source": orphan.source,
                "orphan_source_id": orphan.source_id,
                "matched_cluster_title": best_key[0],
                "matched_cluster_venue": best_key[1],
                "score": best_score,
                "status": "blocked_within_source_duplicate",
            })
            remaining.append(orphan)
            continue

        # Attach: orphan inherits the cluster's NORMALIZED venue so downstream
        # code that groups by venue_norm or looks up venue_canonical sees a
        # consistent record. We deliberately preserve `venue_raw` as-is (None
        # for missing extractions) so the unified output's `venue_as_listed`
        # tells the truth: this source didn't list a venue — we attached the
        # record to the cluster by title match alone.
        donor = clusters[best_key][0]
        orphan.venue_norm = donor.venue_norm
        orphan.venue_canonical = donor.venue_canonical
        orphan.matched_via = "orphan_rescued_by_title"
        # NOTE: orphan.venue_raw intentionally NOT modified

        clusters[best_key].append(orphan)

        rescue_log.append({
            "orphan_title": orphan.title_raw,
            "orphan_source": orphan.source,
            "orphan_source_id": orphan.source_id,
            "matched_cluster_title": best_key[0],
            "matched_cluster_venue": best_key[1],
            "score": best_score,
            "status": "attached",
        })

    return clusters, remaining, rescue_log


# ---------------------------------------------------------------------------
# Stage 5: Manual overrides
# ---------------------------------------------------------------------------
#
# Last-mile lever for cases the algorithm can't handle. Two operations:
#
#   force_merge : these records belong in one cluster, period
#   force_split : this record is its own cluster, period
#
# Format (JSON or YAML — both supported):
#
#   {
#     "force_merge": [
#       {
#         "reason": "Yamato Drummers of Japan -- same production, different titles",
#         "canonical_title": "Yamato: The Drummers of Japan",
#         "canonical_venue": "Peacock Theatre",
#         "records": [
#           {"source": "olt",         "source_id": "..."},
#           {"source": "ttd",         "source_id": "..."},
#           {"source": "lovetheatre", "source_id": "..."}
#         ]
#       }
#     ],
#     "force_split": [
#       {
#         "reason": "Six - Singalong is a different ticketed event from Six",
#         "record": {"source": "lovetheatre", "source_id": "..."}
#       }
#     ]
#   }

def load_overrides(path: Path | None) -> dict:
    """Load overrides from a JSON or YAML file. Empty if path is None or absent."""
    if path is None:
        return {"force_merge": [], "force_split": []}
    if not path.exists():
        print(f"  WARNING: overrides file not found: {path}", file=sys.stderr)
        return {"force_merge": [], "force_split": []}

    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise SystemExit(
                f"overrides file is YAML ({path}) but PyYAML is not installed.\n"
                f"Install with:  pip install pyyaml\n"
                f"Or convert {path} to JSON."
            )
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}

    data.setdefault("force_merge", [])
    data.setdefault("force_split", [])
    # YAML returns None for empty sections — coerce to empty list
    if data.get("force_merge") is None:
        data["force_merge"] = []
    if data.get("force_split") is None:
        data["force_split"] = []
    return data


def apply_overrides(
    clusters: dict[tuple[str, str], list[SourceRecord]],
    overrides: dict,
) -> tuple[dict[tuple[str, str], list[SourceRecord]], list[dict]]:
    """Apply force_split first (more conservative), then force_merge.

    Returns (new_clusters, applied_actions). applied_actions records what
    was actually done so it can be reported.
    """
    applied: list[dict] = []

    # Index: (source, source_id) → cluster_key
    record_index: dict[tuple[str, str], tuple[str, str]] = {}
    for key, records in clusters.items():
        for r in records:
            if r.source_id is not None:
                record_index[(r.source, str(r.source_id))] = key

    # Make a mutable copy
    new_clusters: dict[tuple[str, str], list[SourceRecord]] = {
        k: list(v) for k, v in clusters.items()
    }

    # --- force_split first: isolate each named record into a singleton ---
    for split in overrides.get("force_split", []):
        rec = split.get("record")
        if not rec:
            continue
        rid = (rec.get("source"), str(rec.get("source_id", "")))
        cur_key = record_index.get(rid)
        if cur_key is None:
            applied.append({"action": "force_split", "status": "record_not_found", **rec})
            continue

        cur_records = new_clusters.get(cur_key, [])
        target = next((r for r in cur_records if (r.source, str(r.source_id)) == rid), None)
        if target is None:
            applied.append({"action": "force_split", "status": "record_not_in_cluster", **rec})
            continue

        cur_records.remove(target)
        if not cur_records:
            del new_clusters[cur_key]

        # Singleton key — synthesized so it can't collide with anything
        new_key = (f"__split__:{target.title_norm or 'untitled'}", target.venue_norm or "__no_venue__")
        # Disambiguate if needed
        suffix = 1
        while new_key in new_clusters:
            new_key = (f"__split__:{target.title_norm}#{suffix}", target.venue_norm or "__no_venue__")
            suffix += 1
        new_clusters[new_key] = [target]
        record_index[rid] = new_key

        applied.append({
            "action": "force_split",
            "status": "applied",
            "record": rec,
            "reason": split.get("reason"),
        })

    # --- force_merge: gather records into one cluster ---
    for merge in overrides.get("force_merge", []):
        rec_specs = merge.get("records", [])
        if len(rec_specs) < 1:
            applied.append({
                "action": "force_merge",
                "status": "needs_at_least_1_record",
                "reason": merge.get("reason"),
            })
            continue

        rec_ids = [(r.get("source"), str(r.get("source_id", ""))) for r in rec_specs]

        # Look up the current cluster for each named record
        rec_to_cluster: dict[tuple[str, str], tuple[str, str]] = {}
        missing: list[tuple[str, str]] = []
        for rid in rec_ids:
            ck = record_index.get(rid)
            if ck is None or ck not in new_clusters:
                missing.append(rid)
                continue
            if not any((r.source, str(r.source_id)) == rid for r in new_clusters[ck]):
                missing.append(rid)
                continue
            rec_to_cluster[rid] = ck

        if not rec_to_cluster:
            applied.append({
                "action": "force_merge",
                "status": "no_records_found",
                "missing": [{"source": s, "source_id": i} for s, i in missing],
                "reason": merge.get("reason"),
            })
            continue

        # Pick destination cluster. Three cases:
        #
        # (A) 2+ records named: pick the cluster currently holding the most
        #     of them, tie-broken by largest existing cluster size.
        # (B) 1 record named + canonical_title/venue specified: try to find
        #     an existing cluster matching that canonical (so we can move
        #     the record into the right pre-existing cluster, not create
        #     a parallel one).
        # (C) 1 record named, no canonical match: the record stays where
        #     it is (no-op, since force_merge requires somewhere to merge TO).
        from collections import Counter as _Counter
        merge_counts: _Counter = _Counter(rec_to_cluster.values())

        dest_key: tuple[str, str] | None = None
        if len(rec_to_cluster) >= 2:
            dest_key = max(
                merge_counts.keys(),
                key=lambda k: (merge_counts[k], len(new_clusters[k])),
            )
        else:
            # Single-record case: try to find target cluster via canonical fields
            canon_title = merge.get("canonical_title")
            canon_venue = merge.get("canonical_venue")
            if canon_title and canon_venue:
                v_norm, _ = normalize_venue(canon_venue)
                t_norm = normalize_title(canon_title, v_norm)
                # Look for an existing cluster containing a record whose
                # normalized title+venue match the canonical. This handles
                # the case where the cluster's current key is some other
                # variant (because union-find picked a different root).
                for ck, records in new_clusters.items():
                    if any(r.title_norm == t_norm and r.venue_norm == v_norm for r in records):
                        # Skip if this is just the single record's own cluster
                        if ck in rec_to_cluster.values() and len(records) == 1:
                            continue
                        dest_key = ck
                        break

        if dest_key is None:
            applied.append({
                "action": "force_merge",
                "status": "no_destination_cluster_found",
                "missing": [{"source": s, "source_id": i} for s, i in missing] if missing else None,
                "reason": merge.get("reason"),
                "hint": ("for a single-record merge, set canonical_title and "
                         "canonical_venue to match an existing cluster"),
            })
            continue

        # Move records (those not already in dest_key) into dest_key
        moved_count = 0
        for rid, source_ck in list(rec_to_cluster.items()):
            if source_ck == dest_key:
                continue
            cur = new_clusters.get(source_ck)
            if cur is None:
                continue
            target = next((r for r in cur if (r.source, str(r.source_id)) == rid), None)
            if target is None:
                continue
            cur.remove(target)
            if not cur:
                del new_clusters[source_ck]
            new_clusters[dest_key].append(target)
            record_index[rid] = dest_key
            moved_count += 1

        applied.append({
            "action": "force_merge",
            "status": "applied" if moved_count > 0 else "no_op_already_merged",
            "destination_cluster": list(dest_key),
            "records_moved": moved_count,
            "records": [{"source": s, "source_id": i} for s, i in rec_to_cluster.keys()],
            "missing": [{"source": s, "source_id": i} for s, i in missing] if missing else None,
            "reason": merge.get("reason"),
        })

    return new_clusters, applied


# ---------------------------------------------------------------------------
# Performance unification
# ---------------------------------------------------------------------------

def _canonical_time(t: Any) -> str | None:
    """Normalize a time string to HH:MM."""
    if not t:
        return None
    s = str(t).strip()
    # Some sources include seconds — trim. Some lack leading zero — pad.
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return s


def unify_performances(records: list[SourceRecord]) -> list[dict]:
    """Join performances across sources on (date, time)."""
    # Build per-source perf list with normalized shape
    by_dt: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)

    for r in records:
        sch = PERF_SCHEMAS[r.source]
        for p in r.performances:
            date = sch["date"](p)
            time = _canonical_time(sch["time"](p))
            if not date or not time:
                continue
            entry = {
                "price_from": sch["price_from"](p),
                "price_to":   sch["price_to"](p),
                "currency":   sch["currency"](p),
                "book_url":   sch["book_url"](p),
                "available":  sch["available"](p),
            }
            # Drop all-null entries (defensive)
            if all(v is None for v in entry.values()):
                continue
            by_dt[(date, time)][r.source] = entry

    out: list[dict] = []
    for (date, time), per_src in sorted(by_dt.items()):
        prices = [e["price_from"] for e in per_src.values() if e["price_from"] is not None]
        prices_to = [e["price_to"] for e in per_src.values() if e["price_to"] is not None]
        avails = [e["available"] for e in per_src.values() if e["available"] is not None]
        currs = [e["currency"] for e in per_src.values() if e["currency"]]

        out.append({
            "date": date,
            "time": time,
            "min_price": min(prices) if prices else None,
            "max_price": max(prices_to) if prices_to else (max(prices) if prices else None),
            "currency": currs[0] if currs else None,
            "any_available": (any(avails) if avails else None),
            "sources": per_src,
        })
    return out


# ---------------------------------------------------------------------------
# Field reconciliation
# ---------------------------------------------------------------------------

def _by_priority(records: list[SourceRecord]) -> list[SourceRecord]:
    """Sort records by SOURCE_PRIORITY."""
    pri = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    return sorted(records, key=lambda r: pri.get(r.source, 999))


def _pick_description(records: list[SourceRecord]) -> tuple[str | None, str | None]:
    """Pick the longest non-null description, breaking ties by source priority.
    Returns (description, contributing_source)."""
    candidates = [(r, r.description) for r in records if r.description and r.description.strip()]
    if not candidates:
        return (None, None)
    # Sort by length desc, then by source priority
    pri = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    candidates.sort(key=lambda x: (-len(x[1] or ""), pri.get(x[0].source, 999)))
    return (candidates[0][1], candidates[0][0].source)


def _pick_title(records: list[SourceRecord]) -> tuple[str, str]:
    """Choose the cleanest title variant.

    Heuristic, in priority order:
      1. Most-voted variant wins (so 5 sources saying "The Lion King" beat
         2 sources saying "Disney's The Lion King").
      2. Non-shouty preferred (lowercased "Mamma Mia!" over "MAMMA MIA!").
      3. Shorter preferred (cleaner display).
      4. Earliest in SOURCE_PRIORITY.

    Also strips any " - <Venue>" or " - The <Venue>" suffix from each
    candidate before voting, so titles like "Jesus Christ Superstar - The
    Palladium" don't beat the bare "Jesus Christ Superstar".
    """
    def is_shouty(s: str) -> bool:
        letters = [c for c in s if c.isalpha()]
        if not letters:
            return False
        return sum(1 for c in letters if c.isupper()) / len(letters) > 0.6

    def strip_venue_suffix(title: str, venue_canonical: str | None) -> str:
        if not venue_canonical:
            return title
        for suffix in (f" - {venue_canonical}", f" - The {venue_canonical}"):
            if title.lower().endswith(suffix.lower()):
                return title[: -len(suffix)].strip()
        return title

    variants: dict[str, list[SourceRecord]] = defaultdict(list)
    for r in records:
        if not r.title_raw:
            continue
        title = strip_venue_suffix(r.title_raw.strip(), r.venue_canonical)
        if title:
            variants[title].append(r)

    if not variants:
        return ("", "")

    pri = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    scored = []
    for title, recs in variants.items():
        best_src = min(recs, key=lambda rr: pri.get(rr.source, 999))
        scored.append((
            -len(recs),                    # 1: more votes better
            is_shouty(title),              # 2: non-shouty better
            len(title),                    # 3: shorter better
            pri.get(best_src.source, 999), # 4: source priority
            title,
            best_src.source,
        ))
    scored.sort()
    return (scored[0][4], scored[0][5])


def build_unified(records: list[SourceRecord]) -> UnifiedShow:
    """Build a UnifiedShow from records that all share a cluster key."""
    # Prefer a venue name that's in our canonical registry; fall back to
    # the first cleaned input. This matters when an override merges
    # records that resolved to different venue_canonical values (e.g.
    # "Playhouse Theatre" vs "Kit Kat Club at the Playhouse Theatre" —
    # the registry-canonical form should win the display.)
    registry_canonicals = [r.venue_canonical for r in records
                           if r.venue_canonical and r.venue_canonical in VENUE_ALIASES]
    if registry_canonicals:
        venue_canonical = registry_canonicals[0]
    else:
        venue_canonical = next((r.venue_canonical for r in records if r.venue_canonical),
                               "Unknown Venue")
    title, title_src = _pick_title(records)
    description, desc_src = _pick_description(records)

    performances = unify_performances(records)

    # Show-level min/max from the unified performances
    perf_mins = [p["min_price"] for p in performances if p["min_price"] is not None]
    perf_maxes = [p["max_price"] for p in performances if p["max_price"] is not None]

    sources_list = []
    for r in _by_priority(records):
        sources_list.append({
            "source": r.source,
            "source_id": r.source_id,
            "title_as_listed": r.title_raw,
            "venue_as_listed": r.venue_raw,
            "url": r.url,
            "performance_count": len(r.performances),
            "matched_via": r.matched_via,
            "confidence": 100 if r.matched_via == "exact_normalized" else 90,
        })

    return UnifiedShow(
        id=_slugify(records[0].title_norm, records[0].venue_norm),
        title=title,
        venue=venue_canonical,
        title_norm=records[0].title_norm,
        venue_norm=records[0].venue_norm,
        source_count=len(records),
        sources=sources_list,
        description=description,
        min_price_gbp=min(perf_mins) if perf_mins else None,
        max_price_gbp=max(perf_maxes) if perf_maxes else None,
        performance_count=len(performances),
        performances=performances,
        field_provenance={
            "title": title_src,
            "description": desc_src,
            "venue": "registry" if venue_canonical in VENUE_ALIASES else "first_source",
            "min_price_gbp": "computed_from_performances",
            "max_price_gbp": "computed_from_performances",
        },
        match_confidence=100,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("path", type=Path,
                    help="Folder containing the 7 *.json files, OR an all-scrapers.zip")
    ap.add_argument("--out", type=Path, default=Path("dedupe_output"),
                    help="Output directory (default: ./dedupe_output/)")
    ap.add_argument("--no-fuzzy", action="store_true",
                    help="Skip Stage 3 (fuzzy matching within venues)")
    ap.add_argument("--overrides", type=Path, default=None,
                    help="Path to a JSON or YAML overrides file (Stage 5)")
    ap.add_argument("--fuzzy-auto", type=int, default=FUZZY_AUTO_THRESHOLD,
                    help=f"Stage 3 auto-merge threshold 0-100 (default {FUZZY_AUTO_THRESHOLD})")
    ap.add_argument("--fuzzy-review", type=int, default=FUZZY_REVIEW_THRESHOLD,
                    help=f"Stage 3 review threshold 0-100 (default {FUZZY_REVIEW_THRESHOLD})")
    ap.add_argument("--no-orphan-rescue", action="store_true",
                    help="Skip Stage 4 (title-only attach for records missing venue)")
    ap.add_argument("--orphan-rescue-threshold", type=int,
                    default=ORPHAN_RESCUE_THRESHOLD,
                    help=f"Stage 4 minimum title-similarity score 0-100 "
                         f"(default {ORPHAN_RESCUE_THRESHOLD}; conservative because "
                         f"venue is not used as a discriminator)")
    ap.add_argument("--min-performances", type=int, default=0,
                    help="Drop clusters with fewer than N total performances "
                         "(default: 0 = keep everything). Set to 1 to filter "
                         "out 'ghost' listings — show pages that exist on a "
                         "source but have no current schedule data, like the "
                         "stale Miss Saigon entry on todaytix or lovetheatre's "
                         "no-date New Wimbledon touring listings.")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Loading sources from {args.path}...")
    sources_data = load_sources(args.path)
    print(f"  loaded {len(sources_data)} sources: {sorted(sources_data.keys())}")

    records = extract_records(sources_data)
    print(f"  total source records: {len(records)}")

    print("Stage 1+2: normalizing titles and venues...")
    normalize_all(records)

    # Diagnostic: how many records hit the venue registry?
    in_registry = sum(1 for r in records if r.venue_canonical in VENUE_ALIASES)
    no_venue = sum(1 for r in records if not r.venue_norm)
    print(f"  venue resolved to registry: {in_registry}")
    print(f"  venue missing/unparseable: {no_venue}")

    print("  clustering by (title_norm, venue_norm)...")
    clusters, orphans = cluster_by_key(records)
    stage12_count = len(clusters)
    print(f"  Stage 1+2 cluster count: {stage12_count}")

    # ----- Stage 3: fuzzy matching within venues -----
    fuzzy_merges: list[dict] = []
    fuzzy_review_pairs: list[dict] = []
    if args.no_fuzzy:
        print("Stage 3: SKIPPED (--no-fuzzy)")
    else:
        if not _RAPIDFUZZ_AVAILABLE:
            print("Stage 3: fuzzy matching within venues (rapidfuzz NOT installed — "
                  "falling back to difflib; install rapidfuzz for better speed and quality)")
        else:
            print("Stage 3: fuzzy matching within venues...")
        clusters, fuzzy_merges, fuzzy_review_pairs = fuzzy_merge_within_venues(
            clusters,
            auto_threshold=args.fuzzy_auto,
            review_threshold=args.fuzzy_review,
        )
        print(f"  fuzzy auto-merges:        {len(fuzzy_merges)}")
        print(f"  fuzzy pairs in review:    {len(fuzzy_review_pairs)}")
        print(f"  cluster count after fuzzy: {len(clusters)}")

    # ----- Stage 4: orphan rescue (title-only match) -----
    orphan_rescues: list[dict] = []
    if args.no_orphan_rescue:
        print("Stage 4: SKIPPED (--no-orphan-rescue)")
    elif not orphans:
        print("Stage 4: SKIPPED (no orphans to rescue)")
    else:
        print(f"Stage 4: orphan rescue (threshold {args.orphan_rescue_threshold}, "
              f"{len(orphans)} orphans)...")
        clusters, orphans, orphan_rescues = rescue_orphans_by_title(
            clusters, orphans, threshold=args.orphan_rescue_threshold,
        )
        attached = sum(1 for r in orphan_rescues if r["status"] == "attached")
        blocked_dup = sum(1 for r in orphan_rescues if r["status"] == "blocked_within_source_duplicate")
        blocked_amb = sum(1 for r in orphan_rescues if r["status"] == "blocked_ambiguous_match")
        nomatch = sum(1 for r in orphan_rescues if r["status"] == "no_match_above_threshold")
        print(f"  attached: {attached}")
        if blocked_dup:
            print(f"  blocked:  {blocked_dup} (within-source duplicate)")
        if blocked_amb:
            print(f"  blocked:  {blocked_amb} (ambiguous — multiple plausible clusters)")
        if nomatch:
            print(f"  no match: {nomatch}")
        print(f"  remaining orphans: {len(orphans)}")

    # ----- Stage 5: manual overrides -----
    overrides_applied: list[dict] = []
    if args.overrides:
        print(f"Stage 5: applying overrides from {args.overrides}...")
        overrides = load_overrides(args.overrides)
        n_merges = len(overrides.get("force_merge", []))
        n_splits = len(overrides.get("force_split", []))
        print(f"  declared:  {n_splits} splits, {n_merges} merges")
        clusters, overrides_applied = apply_overrides(clusters, overrides)
        applied_merges = sum(1 for a in overrides_applied if a["action"] == "force_merge" and a["status"] == "applied")
        applied_splits = sum(1 for a in overrides_applied if a["action"] == "force_split" and a["status"] == "applied")
        skipped = sum(1 for a in overrides_applied if a["status"] != "applied")
        print(f"  applied:   {applied_splits} splits, {applied_merges} merges")
        if skipped:
            print(f"  skipped:   {skipped} (records not found or other issues — see report.txt)")
        print(f"  cluster count after overrides: {len(clusters)}")
    else:
        print("Stage 5: SKIPPED (no --overrides file)")

    # Final stats
    # Multi-source / singleton split — use UNIQUE source count, not record
    # count. Stage 5 overrides can intentionally create clusters with two
    # records from the same source (e.g. HPCC "One Part" + "Two Parts" both
    # from lovetheatre); those still count as one source for coverage.
    multi = [c for c in clusters.values() if len({r.source for r in c}) >= 2]
    single = [c for c in clusters.values() if len({r.source for r in c}) == 1]
    print(f"\nFinal: {len(clusters)} clusters ({len(multi)} multi-source, {len(single)} singletons)")
    print(f"  records without venue (orphans):    {len(orphans)}")

    # Distribution by unique source count (same reasoning as above)
    distrib: dict[int, int] = defaultdict(int)
    for c in clusters.values():
        distrib[len({r.source for r in c})] += 1
    print("\nCoverage distribution (cluster size → count):")
    for n in sorted(distrib.keys(), reverse=True):
        print(f"  {n} sources: {distrib[n]:>4} shows")

    # Build unified records for ALL clusters
    print("\nBuilding unified records...")
    unified = [build_unified(c) for c in clusters.values()]

    # Optional ghost-listing filter — drop clusters whose total performance
    # union is below the threshold. Applied AFTER build_unified so the
    # threshold is checked against the merged perf set, not any one source.
    if args.min_performances > 0:
        before = len(unified)
        dropped = [u for u in unified if u.performance_count < args.min_performances]
        unified = [u for u in unified if u.performance_count >= args.min_performances]
        print(f"  --min-performances={args.min_performances}: dropped {before - len(unified)} ghost listings")
        if dropped:
            # Show a sample so the user can verify nothing important was lost
            for u in dropped[:5]:
                print(f"    drop: '{u.title}' @ '{u.venue}' "
                      f"(sources={[s['source'] for s in u.sources]}, perfs={u.performance_count})")
            if len(dropped) > 5:
                print(f"    ... and {len(dropped) - 5} more (see report.txt)")

    # Sort: most-sourced first, then by title
    unified.sort(key=lambda u: (-u.source_count, u.title.lower()))

    # Total perf union
    total_perfs = sum(u.performance_count for u in unified)
    print(f"  unified shows: {len(unified)}")
    print(f"  unified performances: {total_perfs}")

    # ----- Write outputs -----
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Recompute coverage_distribution from the (possibly filtered) unified
    # list so the JSON reflects what was actually shipped. Bucket by unique
    # source count to stay consistent with the pre-build distribution (and
    # to correctly reflect that an override-merged cluster with two records
    # from one source still has the same coverage as before the override).
    final_distrib: dict[int, int] = defaultdict(int)
    for u in unified:
        final_distrib[len({s["source"] for s in u.sources})] += 1

    unified_payload = {
        "generated_at": now,
        "source_summary": {
            src: {
                "show_count": payload.get("show_count"),
                "scraped_at": payload.get("scraped_at"),
            }
            for src, payload in sources_data.items()
        },
        "show_count": len(unified),
        "performance_count": total_perfs,
        "coverage_distribution": dict(sorted(final_distrib.items(), reverse=True)),
        "stages": {
            "stage_1_2_normalization_and_registry_clusters": stage12_count,
            "stage_3_fuzzy_merges": fuzzy_merges,
            "stage_4_orphan_rescues": orphan_rescues,
            "stage_5_overrides_applied": overrides_applied,
        },
        "shows": [asdict(u) for u in unified],
    }
    out_unified = args.out / "unified.json"
    out_unified.write_text(json.dumps(unified_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_unified}")

    # Review file
    review_payload = {
        "generated_at": now,
        "records_without_venue": [
            {
                "source": r.source,
                "source_id": r.source_id,
                "title": r.title_raw,
                "venue_raw": r.venue_raw,
                "url": r.url,
            }
            for r in orphans
        ],
        "singleton_clusters": [
            {
                "title_norm": k[0],
                "venue_norm": k[1],
                "source": c[0].source,
                "source_id": c[0].source_id,
                "title": c[0].title_raw,
                "venue": c[0].venue_raw,
                "url": c[0].url,
            }
            for k, c in clusters.items() if len(c) == 1
        ],
        # Stage 3 borderline pairs — between review and auto thresholds.
        # Human inspects these and promotes good ones via overrides.
        "fuzzy_review_pairs": fuzzy_review_pairs,
    }
    out_review = args.out / "review.json"
    out_review.write_text(json.dumps(review_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out_review}")

    # Human-readable report
    lines: list[str] = []
    lines.append(f"Deduper v2 report — generated {now}")
    lines.append(f"")
    lines.append(f"Sources loaded: {len(sources_data)}")
    for src in SOURCE_PRIORITY:
        if src in sources_data:
            d = sources_data[src]
            lines.append(f"  {src:<22}  {d.get('show_count', '?'):>4} shows   scraped {d.get('scraped_at', '?')}")
    lines.append(f"")
    lines.append(f"Total source records:           {len(records)}")
    lines.append(f"Total unified shows:            {len(unified)}")
    lines.append(f"Total unified performances:     {total_perfs}")
    lines.append(f"")
    lines.append(f"Stages:")
    lines.append(f"  1+2 (norm + registry)         {stage12_count} clusters")
    if not args.no_fuzzy:
        lines.append(f"  3 (fuzzy within venues)       {len(fuzzy_merges)} auto-merges, "
                     f"{len(fuzzy_review_pairs)} pairs in review")
    if args.overrides:
        applied_merges = sum(1 for a in overrides_applied if a["action"] == "force_merge" and a["status"] == "applied")
        applied_splits = sum(1 for a in overrides_applied if a["action"] == "force_split" and a["status"] == "applied")
        lines.append(f"  5 (manual overrides)          {applied_splits} splits, {applied_merges} merges")
    lines.append(f"")
    lines.append(f"Cluster size distribution:")
    for n in sorted(distrib.keys(), reverse=True):
        lines.append(f"  {n} sources:  {distrib[n]:>4} shows")
    lines.append(f"")
    lines.append(f"Orphans (no venue, dropped):    {len(orphans)}")
    lines.append(f"")
    if fuzzy_merges:
        lines.append(f"Stage 3 fuzzy auto-merges ({len(fuzzy_merges)}):")
        for fm in fuzzy_merges:
            lines.append(f"  [{fm['score']}]  {fm['title_a']!r}  +  {fm['title_b']!r}  @  {fm['venue']!r}")
        lines.append(f"")
    if fuzzy_review_pairs:
        lines.append(f"Stage 3 review-queue pairs ({len(fuzzy_review_pairs)} — see review.json):")
        for fp in fuzzy_review_pairs[:10]:
            lines.append(f"  [{fp['score']}]  {fp['title_a']!r}  ~  {fp['title_b']!r}  @  {fp['venue']!r}")
        if len(fuzzy_review_pairs) > 10:
            lines.append(f"  ... and {len(fuzzy_review_pairs) - 10} more")
        lines.append(f"")
    if orphan_rescues:
        attached = [r for r in orphan_rescues if r["status"] == "attached"]
        blocked = [r for r in orphan_rescues if r["status"] == "blocked_within_source_duplicate"]
        ambiguous = [r for r in orphan_rescues if r["status"] == "blocked_ambiguous_match"]
        if attached:
            lines.append(f"Stage 4 orphans attached ({len(attached)}):")
            for r in attached:
                lines.append(f"  [{r['score']}]  [{r['orphan_source']}] {r['orphan_title']!r}")
                lines.append(f"          → matched cluster {r['matched_cluster_title']!r} @ {r['matched_cluster_venue']!r}")
            lines.append(f"")
        if blocked:
            lines.append(f"Stage 4 orphans blocked by within-source guard ({len(blocked)}):")
            for r in blocked:
                lines.append(f"  [{r['score']}]  [{r['orphan_source']}] {r['orphan_title']!r}")
                lines.append(f"          ✗ would create duplicate in {r['matched_cluster_title']!r}")
            lines.append(f"")
        if ambiguous:
            lines.append(f"Stage 4 orphans blocked by ambiguity guard ({len(ambiguous)}):")
            for r in ambiguous:
                lines.append(f"  [{r['score']}]  [{r['orphan_source']}] {r['orphan_title']!r}")
                alt = r.get("alternative_match", {})
                lines.append(f"          ✗ best: {r['matched_cluster_title']!r} @ {r['matched_cluster_venue']!r}")
                lines.append(f"            alt:  {alt.get('title')!r} @ {alt.get('venue')!r} (score {alt.get('score')})")
            lines.append(f"")
    if overrides_applied:
        not_applied = [a for a in overrides_applied if a["status"] != "applied"]
        if not_applied:
            lines.append(f"Stage 5 overrides NOT applied ({len(not_applied)}):")
            for a in not_applied:
                lines.append(f"  {a['action']}  status={a['status']}  reason={a.get('reason', '?')!r}")
            lines.append(f"")
    lines.append("Top 20 shows by source count:")
    for u in unified[:20]:
        lines.append(f"  {u.source_count}× {u.title!r:<45} @ {u.venue!r}  ({u.performance_count} perfs)")
    lines.append(f"")
    lines.append(f"To improve match rate further:")
    lines.append(f"  - Inspect review.json fuzzy_review_pairs section; promote good ones")
    lines.append(f"    via an overrides.yaml force_merge entry.")
    lines.append(f"  - Inspect unified.json stages.stage_3_fuzzy_merges; demote bad ones")
    lines.append(f"    via an overrides.yaml force_split entry.")
    lines.append(f"  - Add venue aliases to VENUE_ALIASES in dedupe.py for any")
    lines.append(f"    singletons that should clearly be in an existing cluster.")

    out_report = args.out / "report.txt"
    out_report.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_report}")
    print(f"\nReport:\n")
    print("\n".join(lines))

    return 0


if __name__ == "__main__":
    sys.exit(main())
