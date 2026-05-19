"""
smoke_test.py — verify each scraper runs end-to-end on a tiny slice
===================================================================

Runs every scraper with `--limit 2` against a temporary output file and
prints a single summary table at the end.

Why this exists
---------------
After a fresh clone (or after dependency changes), you want a quick
"does anything work?" check that doesn't take 20 minutes. This script
runs each scraper sequentially with a small limit, parses the resulting
JSON, and reports show count, performance/showtime count, any warnings
embedded in the report, and any per-show failures.

Sequential, not parallel — Playwright scrapers spin up a browser and
shouldn't compete for resources, and a smoke test should be readable in
the logs rather than fast.

Usage
-----
    python smoke_test.py                # run all seven, --limit 2 each
    python smoke_test.py --limit 5      # bump the per-scraper limit
    python smoke_test.py --only olt     # run a single scraper by name
    python smoke_test.py --skip-browser # skip the two Playwright scrapers
    python smoke_test.py --timeout 600  # per-scraper timeout in seconds

Outputs land in data/smoke/<scraper>.json so they don't clash with real
scrape outputs, and the directory is wiped at the start of each run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SMOKE_DIR = REPO_ROOT / "data" / "smoke"

# (name, script filename, needs_playwright)
SCRAPERS: list[tuple[str, str, bool]] = [
    ("todaytix",            "todaytix_scraper.py",            True),
    ("londontheatre",       "londontheatre_scraper.py",       True),
    ("olt",                 "olt_scraper.py",                 False),
    ("lovetheatre",         "lovetheatre_scraper.py",         False),
    ("seatplan",            "seatplan_scraper.py",            False),
    ("londontheatredirect", "londontheatredirect_scraper.py", False),
    ("ttd",                 "ttd_scraper.py",                 False),
]


@dataclass
class Result:
    name: str
    ok: bool
    duration_s: float
    show_count: int | None = None
    perf_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    failures: int = 0
    error: str | None = None  # only set when ok is False


def run_one(
    name: str,
    script: str,
    limit: int,
    timeout: int,
) -> Result:
    out_path = SMOKE_DIR / f"{name}.json"
    cmd = [
        sys.executable,
        str(REPO_ROOT / script),
        "--limit", str(limit),
        "--out", str(out_path),
    ]

    print(f"\n→ {name}: {' '.join(cmd)}", flush=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return Result(
            name=name, ok=False,
            duration_s=time.monotonic() - t0,
            error=f"timed out after {timeout}s",
        )
    except FileNotFoundError as e:
        return Result(
            name=name, ok=False,
            duration_s=time.monotonic() - t0,
            error=f"could not launch: {e}",
        )

    duration = time.monotonic() - t0

    # Success is determined by "did a valid JSON file get written?",
    # not by exit code. Several scrapers (lovetheatre, olt, seatplan, ttd)
    # use a three-tier convention where exit code 2 means "wrote output,
    # but flagged warnings". The warnings already surface in
    # payload["report"]["warnings"]; no need to mark the run FAIL.
    if not out_path.exists():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        return Result(
            name=name, ok=False, duration_s=duration,
            error=f"no output file (exit code {proc.returncode})\n    "
                  + "\n    ".join(tail),
        )

    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return Result(
            name=name, ok=False, duration_s=duration,
            error=f"output is not valid JSON: {e}",
        )

    # Different scrapers use different secondary count keys; accept either.
    perf = payload.get("performance_count")
    if perf is None:
        perf = payload.get("showtime_count")

    report = payload.get("report") or {}
    warnings = list(report.get("warnings") or [])
    failures = list(report.get("failures") or [])

    return Result(
        name=name,
        ok=True,
        duration_s=duration,
        show_count=payload.get("show_count"),
        perf_count=perf,
        warnings=warnings,
        failures=len(failures),
    )


def print_summary(results: list[Result]) -> None:
    print("\n" + "=" * 78)
    print("SMOKE TEST SUMMARY")
    print("=" * 78)
    header = f"{'scraper':<22} {'status':<8} {'shows':>6} {'perfs':>7} {'time':>7}  notes"
    print(header)
    print("-" * 78)
    for r in results:
        status = "OK" if r.ok else "FAIL"
        shows = str(r.show_count) if r.show_count is not None else "-"
        perfs = str(r.perf_count) if r.perf_count is not None else "-"
        dur = f"{r.duration_s:.1f}s"
        notes_parts: list[str] = []
        if r.warnings:
            notes_parts.append(f"{len(r.warnings)} warn")
        if r.failures:
            notes_parts.append(f"{r.failures} fail")
        if r.error:
            # Keep the table line readable; full error printed below.
            notes_parts.append("see below")
        notes = ", ".join(notes_parts)
        print(f"{r.name:<22} {status:<8} {shows:>6} {perfs:>7} {dur:>7}  {notes}")

    # Detail blocks for anything that needs attention.
    bad = [r for r in results if not r.ok or r.warnings or r.failures]
    if bad:
        print("\n" + "-" * 78)
        print("DETAILS")
        print("-" * 78)
        for r in bad:
            print(f"\n[{r.name}]")
            if r.error:
                print(f"  error: {r.error}")
            for w in r.warnings:
                print(f"  warn:  {w}")
            if r.failures:
                print(f"  per-show failures: {r.failures}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--limit", type=int, default=2,
                   help="--limit value passed to each scraper (default: 2)")
    p.add_argument("--timeout", type=int, default=300,
                   help="Per-scraper timeout in seconds (default: 300)")
    p.add_argument("--only", action="append", default=None,
                   help="Run only this scraper by name (repeatable). "
                        f"Options: {', '.join(n for n, _, _ in SCRAPERS)}")
    p.add_argument("--skip-browser", action="store_true",
                   help="Skip the two Playwright-based scrapers "
                        "(todaytix, londontheatre)")
    args = p.parse_args(argv)

    # Fresh output dir.
    if SMOKE_DIR.exists():
        shutil.rmtree(SMOKE_DIR)
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)

    selected = SCRAPERS
    if args.only:
        names = set(args.only)
        unknown = names - {n for n, _, _ in SCRAPERS}
        if unknown:
            print(f"unknown scraper(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        selected = [s for s in selected if s[0] in names]
    if args.skip_browser:
        selected = [s for s in selected if not s[2]]

    print(f"Running {len(selected)} scraper(s) with --limit {args.limit}, "
          f"timeout {args.timeout}s each. Output → {SMOKE_DIR}/")

    results: list[Result] = []
    for name, script, _ in selected:
        results.append(run_one(name, script, args.limit, args.timeout))

    print_summary(results)

    failed = [r for r in results if not r.ok]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
