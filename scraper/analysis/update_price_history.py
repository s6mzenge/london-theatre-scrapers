"""
Price-history updater.
======================

Maintains a single rolling JSON file that records how the price of every
upcoming performance has moved across successive scrapes. Run once per
scrape, immediately after dedupe, *before* the publish commit.

Identity
--------
A performance is identified by (show_id, date, time), where show_id is
the dedupe-assigned slug and time is canonicalised "HH:MM" 24h. These
are stable across scrapes as long as the dedupe rules don't change.

Storage shape
-------------
    {
      "generated_at": "<ISO timestamp of this update>",
      "schema_version": 1,
      "shows": {
        "<show_id>": {
          "<YYYY-MM-DD>T<HH:MM>": [
            {
              "t": "<scrape ISO timestamp>",
              "min": <float|null>,
              "max": <float|null>,
              "currency": "GBP",
              "any_available": <bool|null>,
              "sources": {
                "todaytix": {"from": <float|null>, "to": <float|null>, "available": <bool|null>},
                ...
              }
            },
            ...
          ]
        }
      }
    }

Snapshots are ordered oldest -> newest within each array. Keys are short
(`t`, `min`, `from`, ...) to keep the in-memory size down on the client,
since the file is eager-loaded alongside unified.json.

Append rules
------------
On each run, for every (show_id, date, time) in the fresh unified.json:
  - Build a snapshot from the dedupe-unified per-source data.
  - Compare to the immediately-previous entry for that perf, on every
    field except `t`. If equal, don't append (the prices haven't moved).
    If different (or no previous entry), append.

Prune rules
-----------
After appending, the file is trimmed in two passes:
  1. Drop any (show_id, date, time) bucket whose `date` is strictly
     before today in London local time. The day-of snapshot survives
     until the next run after the show date passes.
  2. Drop any show_id no longer present in the fresh unified.json.
  3. Drop show_id entries that became empty after the above.

There is no per-perf cap on snapshot count — we let it grow naturally,
per spec.

CLI
---
    python update_price_history.py \\
        --unified public/data/unified.json \\
        --history-in public/data/price_history.json \\
        --out public/data/price_history.json

`--history-in` is optional; if missing or unreadable, the script starts
from an empty history (first-run behaviour). `--out` is required.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LONDON_TZ = ZoneInfo("Europe/London")
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_history(path: Path) -> dict[str, Any]:
    """Load existing history file. Return empty skeleton if absent/unreadable."""
    if not path.exists():
        print(f"  no existing history at {path} — starting fresh")
        return _empty_history()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: could not read {path} ({e}); starting fresh")
        return _empty_history()
    # Defensive: validate the bits we depend on.
    if not isinstance(data, dict) or not isinstance(data.get("shows"), dict):
        print(f"  WARNING: {path} has unexpected shape; starting fresh")
        return _empty_history()
    return data


def _empty_history() -> dict[str, Any]:
    return {
        "generated_at": None,
        "schema_version": SCHEMA_VERSION,
        "shows": {},
    }


def load_unified(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"ERROR: --unified file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------

def _build_snapshot(perf: dict, scrape_t: str) -> dict[str, Any]:
    """Turn one unified performance dict into a compact history snapshot."""
    sources_compact: dict[str, dict[str, Any]] = {}
    for src_name, src in (perf.get("sources") or {}).items():
        if not src:
            continue
        sources_compact[src_name] = {
            "from": src.get("price_from"),
            "to": src.get("price_to"),
            "available": src.get("available"),
        }
    return {
        "t": scrape_t,
        "min": perf.get("min_price"),
        "max": perf.get("max_price"),
        "currency": perf.get("currency"),
        "any_available": perf.get("any_available"),
        "sources": sources_compact,
    }


def _payloads_equal(a: dict, b: dict) -> bool:
    """Compare two snapshots on everything except `t`.

    If equal, the prices haven't moved and we suppress the append.
    """
    return (
        a.get("min") == b.get("min")
        and a.get("max") == b.get("max")
        and a.get("currency") == b.get("currency")
        and a.get("any_available") == b.get("any_available")
        and a.get("sources") == b.get("sources")
    )


# ---------------------------------------------------------------------------
# Main update logic
# ---------------------------------------------------------------------------

def update_history(history: dict[str, Any], unified: dict[str, Any]) -> dict[str, Any]:
    """Append new snapshots and prune, in place semantics on a copy.

    Returns a fresh dict; does not mutate the input.
    """
    shows_history: dict[str, dict[str, list[dict]]] = dict(history.get("shows") or {})
    # Deep copy the per-show dicts we touch, to keep callers' input intact.
    shows_history = {sid: dict(buckets) for sid, buckets in shows_history.items()}

    # Use the unified.json's own generated_at as the snapshot timestamp.
    # That ties each history entry to the scrape it came from, and is
    # naturally idempotent if you re-run on the same unified file.
    scrape_t = unified.get("generated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Track which show_ids exist in the fresh data, for the prune step.
    live_show_ids: set[str] = set()

    appended = 0
    suppressed_nochange = 0

    for show in unified.get("shows") or []:
        show_id = show.get("id")
        if not show_id:
            continue
        live_show_ids.add(show_id)

        buckets = shows_history.setdefault(show_id, {})

        for perf in show.get("performances") or []:
            date = perf.get("date")
            time = perf.get("time")
            if not date or not time:
                continue
            key = f"{date}T{time}"

            snapshot = _build_snapshot(perf, scrape_t)
            arr = buckets.get(key)

            if not arr:
                # First time we've seen this performance.
                buckets[key] = [snapshot]
                appended += 1
                continue

            # Idempotency: same scrape timestamp as last entry → no-op,
            # regardless of payload (we don't want duplicate `t`s).
            if arr[-1].get("t") == scrape_t:
                continue

            if _payloads_equal(arr[-1], snapshot):
                suppressed_nochange += 1
                continue

            arr.append(snapshot)
            appended += 1

    # --- Prune step 1: drop buckets where the perf date is strictly past. ---
    today_london: date_cls = datetime.now(LONDON_TZ).date()
    pruned_past = 0
    for show_id, buckets in shows_history.items():
        keys_to_drop = []
        for key in buckets.keys():
            # Key shape: "YYYY-MM-DD T HH:MM"
            try:
                perf_date = date_cls.fromisoformat(key.split("T", 1)[0])
            except ValueError:
                # Malformed key — drop it defensively
                keys_to_drop.append(key)
                continue
            if perf_date < today_london:
                keys_to_drop.append(key)
        for k in keys_to_drop:
            del buckets[k]
        pruned_past += len(keys_to_drop)

    # --- Prune step 2: drop show_ids no longer present in unified. ---
    pruned_missing_shows = 0
    for show_id in list(shows_history.keys()):
        if show_id not in live_show_ids:
            del shows_history[show_id]
            pruned_missing_shows += 1

    # --- Prune step 3: drop show_ids that became empty after step 1. ---
    emptied = 0
    for show_id in list(shows_history.keys()):
        if not shows_history[show_id]:
            del shows_history[show_id]
            emptied += 1

    # Stats
    total_buckets = sum(len(b) for b in shows_history.values())
    total_snapshots = sum(len(arr) for b in shows_history.values() for arr in b.values())
    print(f"  appended snapshots:        {appended}")
    print(f"  suppressed (no change):    {suppressed_nochange}")
    print(f"  pruned past performances:  {pruned_past}")
    print(f"  pruned missing shows:      {pruned_missing_shows}")
    print(f"  pruned emptied shows:      {emptied}")
    print(f"  tracked performances:      {total_buckets}")
    print(f"  total snapshots in file:   {total_snapshots}")

    return {
        "generated_at": scrape_t,
        "schema_version": SCHEMA_VERSION,
        "shows": shows_history,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--unified", type=Path, required=True,
                    help="Path to freshly-deduped unified.json")
    ap.add_argument("--history-in", type=Path, default=None,
                    help="Path to existing price_history.json (optional; "
                         "starts fresh if missing)")
    ap.add_argument("--out", type=Path, required=True,
                    help="Path to write updated price_history.json")
    args = ap.parse_args(argv)

    print(f"Loading unified data from {args.unified}...")
    unified = load_unified(args.unified)
    print(f"  shows: {unified.get('show_count', '?')}, "
          f"performances: {unified.get('performance_count', '?')}")

    history_in = args.history_in or args.out  # default: read & write same file
    print(f"Loading existing history from {history_in}...")
    history = load_history(history_in)

    print("Updating history...")
    new_history = update_history(history, unified)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Compact JSON; the file is gzipped by scripts/gzip-data.mjs at build
    # time, so the on-disk indented form would just waste git diff space.
    args.out.write_text(
        json.dumps(new_history, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
