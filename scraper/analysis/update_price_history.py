"""
Price-history updater.
======================

Maintains a rolling, append-only record of how the price of every
upcoming performance has moved across successive scrapes. Run once per
scrape, immediately after dedupe, *before* the publish commit.

Identity
--------
A performance is identified by (show_id, date, time), where show_id is
the dedupe-assigned slug and time is canonicalised "HH:MM" 24h. These
are stable across scrapes as long as the dedupe rules don't change.

Storage shape (v2 — sharded + gzipped)
--------------------------------------
The history is split into one gzipped file *per performance month*, plus
a small plaintext manifest, all under a directory:

    public/data/price_history/
        index.json          # manifest (plaintext, tiny)
        2026-06.json.gz      # all buckets whose perf date is in 2026-06
        2026-07.json.gz
        ...

The single 100 MB+ `price_history.json` it replaces tripped GitHub's hard
100 MB-per-file push limit. Sharding by month keeps every committed blob
small, and committing each shard pre-gzipped (rather than gzipping at
build time, the way unified.json is handled) keeps the *repo* small too.
Crucially, only the current/near-future months change on a given run, so
past-month shards stay byte-identical and git never re-commits them.

Manifest (`index.json`):
    {
      "generated_at": "<ISO timestamp of this update>",
      "schema_version": 2,
      "months": ["2026-06", "2026-07", ...]   # sorted, only non-empty
    }

Each shard (`<YYYY-MM>.json.gz`, gunzips to):
    {
      "schema_version": 2,
      "month": "<YYYY-MM>",
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

NOTE: a shard payload deliberately carries NO run timestamp — only
schema_version, month, and shows. That keeps an unchanged month's bytes
identical from run to run (combined with gzip mtime=0), so git diffs stay
minimal and the publish workflow's "no changes to commit" short-circuit
keeps working. The run timestamp lives in the manifest only.

Snapshots are ordered oldest -> newest within each array. Keys are short
(`t`, `min`, `from`, ...) to keep the in-memory size down on the client,
which merges every shard back into one map at load time.

Append rules
------------
On each run, for every (show_id, date, time) in the fresh unified.json:
  - Build a snapshot from the dedupe-unified per-source data.
  - Compare to the immediately-previous entry for that perf, on every
    field except `t`. If equal, don't append (the prices haven't moved).
    If different (or no previous entry), append.

Prune rules
-----------
After appending, the history is trimmed:
  1. Drop any (show_id, date, time) bucket whose `date` is strictly
     before today in London local time. The day-of snapshot survives
     until the next run after the show date passes.
  2. Drop any show_id no longer present in the fresh unified.json.
  3. Drop show_id entries that became empty after the above.
  4. A month whose buckets are entirely gone has its shard file deleted.

There is no per-perf cap on snapshot count — we let it grow naturally,
per spec.

Migration
---------
On the first run after this version lands, the sharded directory won't
exist yet but a legacy `public/data/price_history.json` monolith will.
The updater seeds itself from that monolith, writes the shards, and then
deletes the monolith so the publish commit removes it from the tree.

CLI
---
    python update_price_history.py \\
        --unified public/data/unified.json \\
        --history-dir public/data/price_history

`--history-dir` doubles as input and output (default
`public/data/price_history`). `--legacy-in` (default
`public/data/price_history.json`) is the one-time migration source; it is
removed once shards have been written.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LONDON_TZ = ZoneInfo("Europe/London")
SCHEMA_VERSION = 2
MANIFEST_NAME = "index.json"
SHARD_SUFFIX = ".json.gz"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _empty_history() -> dict[str, Any]:
    return {
        "generated_at": None,
        "schema_version": SCHEMA_VERSION,
        "shows": {},
    }


def _month_of_key(key: str) -> str:
    """'YYYY-MM-DDTHH:MM' -> 'YYYY-MM'. Caller guarantees a valid key."""
    return key[:7]


def load_history(history_dir: Path, legacy_path: Path | None) -> tuple[dict[str, Any], str]:
    """Load existing history, merging every monthly shard into one map.

    Resolution order:
      1. Sharded store (manifest + per-month .json.gz) if the manifest
         exists.
      2. Legacy monolith `price_history.json` (one-time migration) if the
         sharded store is absent but the monolith is present.
      3. Empty skeleton (true first run).

    Returns (history, source) where source is one of
    'sharded' | 'legacy' | 'empty', so the caller can decide whether the
    legacy monolith needs deleting.
    """
    manifest_path = history_dir / MANIFEST_NAME

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            months = manifest.get("months") if isinstance(manifest, dict) else None
            months = months if isinstance(months, list) else []
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARNING: could not read manifest {manifest_path} ({e}); "
                  f"falling back to a directory scan")
            months = []

        # If the manifest was unreadable, recover by scanning the dir so we
        # never silently drop accumulated history.
        if not months:
            months = sorted(
                p.name[: -len(SHARD_SUFFIX)]
                for p in history_dir.glob(f"*{SHARD_SUFFIX}")
            )

        merged_shows: dict[str, dict[str, list[dict]]] = {}
        loaded = 0
        for month in months:
            shard = history_dir / f"{month}{SHARD_SUFFIX}"
            if not shard.exists():
                print(f"  WARNING: shard listed in manifest is missing: {shard}")
                continue
            try:
                obj = json.loads(gzip.decompress(shard.read_bytes()))
            except (OSError, json.JSONDecodeError, gzip.BadGzipFile) as e:
                print(f"  WARNING: could not read shard {shard} ({e}); skipping")
                continue
            for show_id, buckets in (obj.get("shows") or {}).items():
                if not isinstance(buckets, dict):
                    continue
                # Bucket keys are month-disjoint across shards, so a plain
                # per-show merge can't collide.
                merged_shows.setdefault(show_id, {}).update(buckets)
            loaded += 1
        print(f"  loaded {loaded} monthly shard(s) from {history_dir}")
        return ({"generated_at": None,
                 "schema_version": SCHEMA_VERSION,
                 "shows": merged_shows},
                "sharded")

    if legacy_path and legacy_path.exists():
        print(f"  no shards yet — migrating from legacy monolith {legacy_path}")
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARNING: could not read legacy {legacy_path} ({e}); "
                  f"starting fresh")
            return _empty_history(), "empty"
        if isinstance(data, dict) and isinstance(data.get("shows"), dict):
            return ({"generated_at": None,
                     "schema_version": SCHEMA_VERSION,
                     "shows": data["shows"]},
                    "legacy")
        print(f"  WARNING: legacy {legacy_path} has unexpected shape; starting fresh")
        return _empty_history(), "empty"

    print("  no existing history — starting fresh")
    return _empty_history(), "empty"


def load_unified(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"ERROR: --unified file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def write_history(history_dir: Path, new_history: dict[str, Any]) -> dict[str, int]:
    """Partition the merged history by month and write one gz shard each.

    Writes deterministically — keys are sorted (sort_keys=True) and gzip
    mtime is zeroed — so a month's bytes depend ONLY on its content, not on
    dict construction/merge order. That means an unchanged month is always
    byte-identical run to run (no git diff, no churn), and a freshly
    migrated store is byte-stable from its very first write. Shards are
    only rewritten when their bytes actually change; shards for months that
    no longer have any buckets are deleted. Returns a small stats dict.
    """
    history_dir.mkdir(parents=True, exist_ok=True)
    shows = new_history.get("shows") or {}

    # month -> {show_id -> {bucket_key -> [snapshots]}}
    by_month: dict[str, dict[str, dict[str, list[dict]]]] = {}
    for show_id, buckets in shows.items():
        for key, arr in buckets.items():
            month = _month_of_key(key)
            by_month.setdefault(month, {}).setdefault(show_id, {})[key] = arr

    months = sorted(by_month.keys())

    written = 0
    unchanged = 0
    for month in months:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "month": month,
            "shows": by_month[month],
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True).encode("utf-8")
        gz = gzip.compress(raw, compresslevel=9, mtime=0)
        target = history_dir / f"{month}{SHARD_SUFFIX}"
        if target.exists() and target.read_bytes() == gz:
            unchanged += 1
            continue
        target.write_bytes(gz)
        written += 1

    # Remove shards for months that have dropped out entirely (e.g. a month
    # fully pruned as past). Leaves the manifest as the source of truth.
    keep = set(months)
    removed = 0
    for f in history_dir.glob(f"*{SHARD_SUFFIX}"):
        month = f.name[: -len(SHARD_SUFFIX)]
        if month not in keep:
            f.unlink()
            removed += 1

    # Manifest. Plaintext + tiny. It's the only file that necessarily
    # changes every run (it carries the run timestamp), which is fine:
    # unified.json changes every run anyway, so the publish commit is never
    # empty in normal operation.
    manifest = {
        "generated_at": new_history.get("generated_at"),
        "schema_version": SCHEMA_VERSION,
        "months": months,
    }
    (history_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"),
                   sort_keys=True),
        encoding="utf-8",
    )

    return {
        "months": len(months),
        "shards_written": written,
        "shards_unchanged": unchanged,
        "shards_removed": removed,
    }


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
    ap.add_argument("--history-dir", type=Path,
                    default=Path("public/data/price_history"),
                    help="Sharded history directory (read + write). "
                         "Default: public/data/price_history")
    ap.add_argument("--legacy-in", type=Path,
                    default=Path("public/data/price_history.json"),
                    help="One-time migration source: the old monolithic "
                         "price_history.json. Removed once shards are "
                         "written. Pass an empty string to disable.")
    args = ap.parse_args(argv)

    legacy_path: Path | None = args.legacy_in
    if legacy_path is not None and str(legacy_path) == "":
        legacy_path = None

    print(f"Loading unified data from {args.unified}...")
    unified = load_unified(args.unified)
    print(f"  shows: {unified.get('show_count', '?')}, "
          f"performances: {unified.get('performance_count', '?')}")

    print(f"Loading existing history from {args.history_dir}...")
    history, source = load_history(args.history_dir, legacy_path)

    print("Updating history...")
    new_history = update_history(history, unified)

    stats = write_history(args.history_dir, new_history)
    print(f"  months:                    {stats['months']}")
    print(f"  shards written:            {stats['shards_written']}")
    print(f"  shards unchanged:          {stats['shards_unchanged']}")
    print(f"  shards removed:            {stats['shards_removed']}")

    # Migration cleanup: once shards exist, the legacy monolith must go so
    # the publish commit deletes it from the tree (and we never re-read it).
    if legacy_path and legacy_path.exists():
        legacy_path.unlink()
        print(f"  removed legacy monolith {legacy_path}"
              + ("" if source == "legacy" else " (stray)"))

    print(f"Wrote {args.history_dir}/ ({stats['months']} shard(s) + {MANIFEST_NAME})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
