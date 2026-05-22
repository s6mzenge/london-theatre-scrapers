"""
chip_pass_cache.py — opt-in result cache for the SP/TT chip passes
===================================================================

The chip pass classifier identifies ~250 suspect SP rows and ~125
suspect TT rows per scrape. At a 15-minute scrape cadence, that's
~24,000 chip extractions per day per source — but the underlying
data on most of those rows doesn't change run-to-run. A cache that
remembers "we already chip-verified this perf 10 minutes ago, the
catalogue input hasn't changed, the answer is still £X" eliminates
the redundant work.

What's cached
-------------
For each suspect perf the script ran the chip extractor on, the cache
records:

    key:    f"{date}|{time}|{book_url}"
    value:  {
        chip_min:    float | null,
        chip_max:    float | null,
        candidates:  [float, ...],
        source:      str   (chips | no_chips_found | fetch_failed)
        reason:      str   (suspect-classification reason)
        note:        str   (diagnostic)
        cached_at:   str   ISO UTC timestamp
        input_hash:  str   hash of catalogue inputs at verification time
    }

Cache hit conditions (all must be true to skip the chip extraction)
-------------------------------------------------------------------
1. Cache entry exists for the key.
2. Cache entry's `source` is one of the "successful" outcomes
   (chips / no_chips_found) — we never cache fetch_failed; a transient
   network error shouldn't poison future runs.
3. Cache entry is fresher than TTL (default 24h). Forces a periodic
   re-check even if the input hasn't changed, catching drift on the
   source side.
4. The current input_hash matches the cached input_hash. If the
   underlying catalogue value changed, the chip pass result is stale
   even if we still trust the source. Concretely: for SP this is
   `low_price` + `verified_min_price` + `verified_max_price`; for TT
   it's `low_price_value` + the cheapest band's price_value+seats.

Failure modes (all safe — fall through to full chip pass)
---------------------------------------------------------
* Cache file doesn't exist → empty cache, normal first run
* Cache file fails to parse → log warning, ignore, normal run
* Cache dir not writable → log warning, run without writing
* Stale entry → not a hit; chip pass runs and writes a fresh entry
* Module import failure → tools don't import this module silently;
  caller must explicitly opt in

Concurrency
-----------
The chip pass is single-process; this module isn't designed for
multi-writer access. If a future architecture runs multiple
processes against the same cache, add a file lock.

Atomic writes
-------------
`save()` writes to `<path>.tmp` then `os.replace()`. A crash mid-write
leaves the previous cache intact. A partial `.tmp` file is harmless
because it isn't referenced.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# Default TTL. Override per-source by passing ttl_hours to load().
DEFAULT_TTL_HOURS = 24

# Source outcomes we trust to cache. `fetch_failed` is excluded because
# transient browser errors shouldn't suppress retry on the next run.
CACHEABLE_SOURCES = frozenset({"chips", "no_chips_found"})


log = logging.getLogger("chip-cache")


# ---------------------------------------------------------------------------
# Key & input-hash helpers
# ---------------------------------------------------------------------------

def make_key(date: str | None, time: str | None,
             book_url: str | None) -> str | None:
    """Stable cache key for a single performance. Returns None when
    the perf is missing required fields — callers should skip caching
    such rows."""
    if not date or not time or not book_url:
        return None
    return f"{date}|{time}|{book_url}"


def hash_inputs(*values: Any) -> str:
    """Hash the catalogue input values that, when changed, should
    invalidate the cached chip-pass result. SP passes
    (low_price, verified_min_price, verified_max_price); TT passes
    (low_price_value, cheapest_band_price, cheapest_band_seats).

    None values are stable across runs (None != 0). Floats are
    formatted to 6dp to avoid spurious cache misses from
    floating-point representation noise."""
    parts = []
    for v in values:
        if v is None:
            parts.append("none")
        elif isinstance(v, float):
            parts.append(f"{v:.6f}")
        else:
            parts.append(str(v))
    raw = "|".join(parts).encode("utf-8")
    # Truncated SHA-1 is plenty for collision avoidance here; we're
    # not doing crypto, just keying ~thousands of values.
    return hashlib.sha1(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load(path: Path) -> dict[str, dict]:
    """Load the cache from disk. Returns an empty dict on any failure
    — missing file, malformed JSON, unexpected schema. NEVER raises;
    callers should be able to opt in to caching without try/except."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Cache at %s failed to load (%s); starting empty.",
                    path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("Cache at %s has wrong top-level type %s; starting empty.",
                    path, type(data).__name__)
        return {}
    # Light shape validation. We don't deeply validate every entry —
    # individual lookups are guarded by is_hit() — but catching a
    # completely wrong file shape here saves debugging time.
    entries = data.get("entries")
    if not isinstance(entries, dict):
        log.warning("Cache at %s missing 'entries' dict; starting empty.",
                    path)
        return {}
    return entries


def save(path: Path, entries: dict[str, dict]) -> bool:
    """Atomically persist `entries` to `path`. Returns True on success,
    False if the directory is unwritable or any I/O step fails. Never
    raises; caller doesn't need to handle exceptions."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Cache dir %s couldn't be created (%s); not persisting.",
                    path.parent, e)
        return False
    payload = {
        "schema_version": 1,
        "saved_at":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entry_count":    len(entries),
        "entries":        entries,
    }
    # Write to a temp file in the same directory (so os.replace stays
    # atomic on the same filesystem), then rename. Tempfile.NamedTemp
    # is created with mode 600; that's fine for a build artifact.
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            # Tempfile may already be gone if os.replace partly ran
            try: os.unlink(tmp_path)
            except OSError: pass
            raise
    except OSError as e:
        log.warning("Cache save to %s failed (%s); not persisting.", path, e)
        return False
    return True


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def is_hit(entry: dict | None, input_hash: str,
           ttl_hours: int = DEFAULT_TTL_HOURS) -> bool:
    """Decide whether a cache entry can be used in place of a fresh
    chip-pass run. Returns True iff:
       - entry exists
       - entry's source is cacheable (chips / no_chips_found)
       - entry's input_hash matches current input_hash
       - entry was written within ttl_hours

    All four conditions guard against serving stale or wrong data."""
    if entry is None:
        return False
    src = entry.get("source")
    if src not in CACHEABLE_SOURCES:
        return False
    if entry.get("input_hash") != input_hash:
        return False
    cached_at = entry.get("cached_at")
    if not cached_at:
        return False
    try:
        # Strip 'Z' suffix and parse; tolerate either +00:00 or 'Z' forms
        ts = datetime.fromisoformat(cached_at.rstrip("Z").replace("Z", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    if ts < datetime.now(timezone.utc) - timedelta(hours=ttl_hours):
        return False
    return True


def make_entry(chip_min: float | None,
               chip_max: float | None,
               candidates: list[float],
               source: str,
               reason: str,
               note: str,
               input_hash: str) -> dict:
    """Construct a cache entry dict from chip-pass output."""
    return {
        "chip_min":   chip_min,
        "chip_max":   chip_max,
        "candidates": candidates,
        "source":     source,
        "reason":     reason,
        "note":       note,
        "input_hash": input_hash,
        "cached_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def summarise(before: int, hits: int, misses: int,
              writes: int, ttl_hours: int) -> str:
    """Format a one-line summary suitable for the report block."""
    return (f"cache: {hits} hit / {misses} miss / {writes} write "
            f"(loaded {before} entries, TTL={ttl_hours}h)")
