#!/usr/bin/env python3
"""
coverage_gate.py — refuse to publish a unified.json that has collapsed.
=======================================================================

Compares a freshly-built unified.json (--new) against the currently-live one
(--live) and FAILS (exit 1) if coverage has regressed beyond tolerance, so a
flaky scrape — e.g. the TodayTix listing discovering 54/200 shows — can never
overwrite good production data. Meant to run in the publish job immediately
before `cp dedupe_output/unified.json public/data/unified.json`; a non-zero
exit fails the job so the copy never happens and the live file is left intact.

Guards (all use --min-fraction, default 0.70 → block on a >30% drop):
  * per-source show count — for every source with a meaningful presence in the
    live file (>= --min-live-count shows), block if its show count in the new
    file fell below live * min_fraction. This is the important one: when
    TodayTix collapses, the *total* unified show count barely moves (the other
    four sellers still carry those shows), so only a per-source check catches
    it. Also catches a source vanishing entirely (count 0) or being bot-blocked.
  * global show count and global performance count — block on a catastrophic
    overall collapse (e.g. dedupe itself broke).

Failure direction is deliberate:
  * Fails OPEN (exit 0, allow publish) only when there is genuinely nothing to
    compare against — a missing/unreadable live baseline or a live file with
    zero shows (first deploy). A gate must never brick the very first publish.
  * Fails CLOSED (exit 1, block) if the NEW file is missing/unreadable or has
    zero shows, because that itself means the build is broken.

Per-source counts are derived from shows[].sources[].source, which is stable
across schema versions — no dependence on any summary block that an older live
file might lack.

Tuning intuition: a genuine catalogue change (a show closing) moves a source
by well under 1%, so the 30% default never trips on normal churn; a discovery
race or a bot-block is a 50–100% drop, which always trips.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

DEFAULT_MIN_FRACTION = 0.70
DEFAULT_MIN_LIVE_COUNT = 20


def _load(path: str):
    p = Path(path)
    if not p.exists():
        return None, f"file does not exist: {path}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"could not parse {path}: {type(e).__name__}: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("shows"), list):
        return None, f"{path} is not a unified.json (missing 'shows' list)"
    return data, None


def _sources_of(show: dict) -> list[str]:
    out: list[str] = []
    v = show.get("sources")
    if isinstance(v, list):
        for x in v:
            s = x.get("source") if isinstance(x, dict) else x
            if s:
                out.append(s)
    elif isinstance(v, dict):
        out.extend(k for k in v.keys() if k)
    return out


def per_source_show_counts(data: dict) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for sh in data.get("shows") or []:
        for s in set(_sources_of(sh)):
            c[s] += 1
    return c


def perf_count(data: dict) -> int:
    pc = data.get("performance_count")
    if isinstance(pc, int):
        return pc
    return sum(len(sh.get("performances") or []) for sh in (data.get("shows") or []))


def evaluate(new: dict, live: dict, *, min_fraction: float, min_live_count: int):
    """Return (ok: bool, lines: list[str], blocks: list[str])."""
    lines: list[str] = []
    blocks: list[str] = []

    new_counts = per_source_show_counts(new)
    live_counts = per_source_show_counts(live)
    new_shows = len(new.get("shows") or [])
    live_shows = len(live.get("shows") or [])
    new_perfs, live_perfs = perf_count(new), perf_count(live)

    # global guards
    lines.append(f"  shows (total):        live={live_shows:6d}  new={new_shows:6d}")
    if live_shows and new_shows < live_shows * min_fraction:
        blocks.append(
            f"total shows {new_shows} < {min_fraction:.0%} of live {live_shows}")
    lines.append(f"  performances (total): live={live_perfs:6d}  new={new_perfs:6d}")
    if live_perfs and new_perfs < live_perfs * min_fraction:
        blocks.append(
            f"total performances {new_perfs} < {min_fraction:.0%} of live {live_perfs}")

    # per-source guards
    lines.append("  per-source show counts:")
    for src in sorted(set(live_counts) | set(new_counts)):
        lv, nv = live_counts.get(src, 0), new_counts.get(src, 0)
        guarded = lv >= min_live_count
        floor = lv * min_fraction
        bad = guarded and nv < floor
        flag = "BLOCK" if bad else ("ok" if guarded else "skip<min")
        pct = f"{(nv / lv * 100):5.1f}%" if lv else "  n/a"
        lines.append(
            f"    {src:14s} live={lv:5d}  new={nv:5d}  ({pct} of live)  [{flag}]")
        if bad:
            blocks.append(
                f"source '{src}' dropped to {nv} shows "
                f"({nv / lv:.0%} of live {lv}; floor {floor:.0f})")

    return (len(blocks) == 0), lines, blocks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Block publishing a collapsed/regressed unified.json.")
    ap.add_argument("--new", help="freshly-built unified.json (e.g. dedupe_output/unified.json)")
    ap.add_argument("--live", help="currently-live unified.json (e.g. public/data/unified.json)")
    ap.add_argument("--min-fraction", type=float, default=DEFAULT_MIN_FRACTION,
                    help=f"block if coverage falls below this fraction of live "
                         f"(default {DEFAULT_MIN_FRACTION})")
    ap.add_argument("--min-live-count", type=int, default=DEFAULT_MIN_LIVE_COUNT,
                    help=f"only guard sources with at least this many shows live "
                         f"(default {DEFAULT_MIN_LIVE_COUNT})")
    ap.add_argument("--selftest", action="store_true", help="run offline self-tests and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.new or not args.live:
        ap.error("--new and --live are required (or use --selftest)")

    new, err_new = _load(args.new)
    if err_new or not new.get("shows"):
        # New file broken/empty → fail CLOSED; do not publish a broken build.
        print(f"COVERAGE GATE: FAIL — new file unusable ({err_new or 'zero shows'}). "
              f"Refusing to publish.")
        return 1

    live, err_live = _load(args.live)
    if err_live or not live.get("shows"):
        # No baseline → fail OPEN (first deploy / file absent).
        print(f"COVERAGE GATE: PASS — no usable live baseline "
              f"({err_live or 'zero shows'}); nothing to compare, allowing publish.")
        return 0

    ok, lines, blocks = evaluate(
        new, live, min_fraction=args.min_fraction, min_live_count=args.min_live_count)
    print(f"COVERAGE GATE  (block below {args.min_fraction:.0%} of live; "
          f"sources guarded at >= {args.min_live_count} live shows)")
    for ln in lines:
        print(ln)
    if ok:
        print("COVERAGE GATE: PASS — publishing.")
        return 0
    print("COVERAGE GATE: FAIL — refusing to overwrite live data:")
    for b in blocks:
        print(f"   - {b}")
    print("Live unified.json left untouched. Investigate the source(s) above "
          "(most likely a flaky or blocked scrape) and re-run.")
    return 1


def selftest() -> int:
    def mk(counts, perfs_per=10):
        shows, sid = [], 0
        for src, n in counts.items():
            for _ in range(n):
                sid += 1
                shows.append({"id": sid, "sources": [{"source": src}],
                              "performances": [{} for _ in range(perfs_per)]})
        return {"shows": shows, "performance_count": len(shows) * perfs_per}

    base = {"olt": 135, "todaytix": 200, "lovetheatre": 166, "ttd": 216, "seatplan": 128}
    live = mk(base)
    F, M = 0.70, 20

    ok, _, _ = evaluate(mk(base), live, min_fraction=F, min_live_count=M)
    assert ok, "identical should pass"

    bad = dict(base); bad["todaytix"] = 54           # the 200->54 bug
    ok, _, blocks = evaluate(mk(bad), live, min_fraction=F, min_live_count=M)
    assert not ok and any("todaytix" in b for b in blocks), blocks

    gone = dict(base); gone["seatplan"] = 0          # source vanished
    ok, _, blocks = evaluate(mk(gone), live, min_fraction=F, min_live_count=M)
    assert not ok and any("seatplan" in b for b in blocks), blocks

    small = dict(base); small["ttd"] = 210           # a few shows closed
    ok, _, _ = evaluate(mk(small), live, min_fraction=F, min_live_count=M)
    assert ok, "small natural drop should pass"

    tiny_live = mk({**base, "fringe": 5})            # sub-threshold source...
    tiny_new = mk({**base, "fringe": 0})             # ...dropping to 0 is ignored
    ok, _, _ = evaluate(tiny_new, tiny_live, min_fraction=F, min_live_count=M)
    assert ok, "sub-min-count source should be skipped"

    half = {k: max(1, v // 3) for k, v in base.items()}   # global collapse
    ok, _, blocks = evaluate(mk(half), live, min_fraction=F, min_live_count=M)
    assert not ok, "global collapse should block"

    print("coverage_gate selftest: PASS "
          "(identical/pass, todaytix-collapse/block, vanish/block, "
          "small-drop/pass, tiny-skip/pass, global-collapse/block)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
