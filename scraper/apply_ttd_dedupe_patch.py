"""
Surgical, idempotent patch for dedupe.py (TTD edits)
====================================================

Adds the TTD availability-aware helper and rewires the TTD entry in
PERF_SCHEMAS to consume verified_min_price / verified_price_source
from ttd_availability.py.

Why a separate patcher instead of extending the SeatPlan one?
-------------------------------------------------------------
The SeatPlan patcher has already run on this repo. Re-running it
detects its sentinel and no-ops — which would skip new TTD edits.
Keeping the TTD patch in its own file means each patcher does one
thing and is idempotent in its own right.

Two targeted edits:
  1. Inserts _ttd_price_from() immediately above the PERF_SCHEMAS
     declaration (just below any existing SeatPlan helpers).
  2. Replaces the existing "ttd" entry within PERF_SCHEMAS with
     one that calls _ttd_price_from.

Idempotency: detects the presence of `_ttd_price_from` and exits
cleanly without rewriting if the patch is already applied.

Failure mode: if either anchor (PERF_SCHEMAS declaration line or the
ttd block within it) can't be found, the script exits non-zero
without writing.

Usage
-----
    python apply_ttd_dedupe_patch.py
        # patches scraper/analysis/dedupe.py in place

    python apply_ttd_dedupe_patch.py --target /path/to/dedupe.py
    python apply_ttd_dedupe_patch.py --dry-run
    python apply_ttd_dedupe_patch.py --no-backup

Exit codes
----------
    0   patch applied OR already present
    1   anchors not found
    2   I/O error
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path


# Anchors and sentinels
INSERT_BEFORE = "PERF_SCHEMAS: dict[str, dict[str, Any]] = {"
SENTINEL = "_ttd_price_from"


HELPER_BLOCK = '''\
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


'''


NEW_TTD_BLOCK = '''    "ttd": {
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
'''


class PatchError(Exception):
    pass


def _find_ttd_block(content: str, search_from: int = 0) -> tuple[int, int]:
    """Return (start, end) byte offsets of the TTD PERF_SCHEMAS entry,
    searching only after PERF_SCHEMAS so we don't match the same-shape
    entry in SHOW_SCHEMAS earlier."""
    marker = '    "ttd": {'
    window = content[search_from:]
    occurrences = [m.start() for m in re.finditer(re.escape(marker), window)]
    if not occurrences:
        raise PatchError(
            f"Could not find a line matching {marker!r} after the PERF_SCHEMAS "
            "declaration. The file structure has drifted; manual edit required."
        )
    if len(occurrences) > 1:
        raise PatchError(
            f"Found {len(occurrences)} candidate 'ttd' blocks after PERF_SCHEMAS — "
            "ambiguous; manual edit required."
        )
    start = search_from + occurrences[0]
    close_match = re.search(r"\n    \},\n", content[start:])
    if not close_match:
        raise PatchError(
            "Found the ttd block opening but not its closing '    },' "
            "line — manual edit required."
        )
    return start, start + close_match.end()


def apply_patch(content: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if SENTINEL in content:
        notes.append("Already patched — found '_ttd_price_from' in file")
        return content, notes

    # --- Edit 1: insert helper above PERF_SCHEMAS -------------------------
    idx = content.find(INSERT_BEFORE)
    if idx == -1:
        raise PatchError(
            f"Could not find anchor {INSERT_BEFORE!r} in dedupe.py — "
            "the file structure has drifted; manual edit required."
        )
    line_start = content.rfind("\n", 0, idx) + 1
    content = content[:line_start] + HELPER_BLOCK + content[line_start:]
    notes.append("Inserted _ttd_price_from above PERF_SCHEMAS")

    # --- Edit 2: replace the ttd PERF_SCHEMAS entry -----------------------
    perf_idx = content.find(INSERT_BEFORE)
    if perf_idx == -1:
        raise PatchError(
            "Lost the PERF_SCHEMAS anchor after Edit 1 — bug in patcher."
        )
    perf_line_end = content.find("\n", perf_idx) + 1
    start, end = _find_ttd_block(content, search_from=perf_line_end)
    old_block = content[start:end]
    content = content[:start] + NEW_TTD_BLOCK + content[end:]
    notes.append(
        f"Rewrote 'ttd' PERF_SCHEMAS entry "
        f"({len(old_block.splitlines())} lines -> "
        f"{len(NEW_TTD_BLOCK.splitlines())} lines)"
    )

    # Belt-and-braces: confirm result still parses
    try:
        compile(content, "<patched dedupe.py>", "exec")
    except SyntaxError as e:
        raise PatchError(
            f"Patched dedupe.py would have a syntax error at line {e.lineno}: "
            f"{e.msg}. Refusing to write."
        )
    return content, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Surgically patch dedupe.py to consume verified_min_price from "
            "ttd_availability.py. Idempotent."
        ),
    )
    ap.add_argument(
        "--target", "-t", type=Path,
        default=Path("scraper/analysis/dedupe.py"),
        help="Path to dedupe.py (default: ./scraper/analysis/dedupe.py).",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without writing.")
    ap.add_argument("--no-backup", action="store_true",
                    help="Don't write a timestamped .bak file.")
    args = ap.parse_args(argv)

    target: Path = args.target
    if not target.exists():
        print(f"ERROR: {target} not found", file=sys.stderr)
        return 2
    try:
        original = target.read_text(encoding="utf-8")
    except OSError as e:
        print(f"ERROR: could not read {target}: {e}", file=sys.stderr)
        return 2

    try:
        patched, notes = apply_patch(original)
    except PatchError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for n in notes:
        print(f"  - {n}")

    if patched == original:
        print("No changes written.")
        return 0

    if args.dry_run:
        added = patched.count("\n") - original.count("\n")
        print(f"--dry-run: would write {len(patched)} bytes ({added:+d} lines)")
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = target.with_name(f"{target.name}.bak.{stamp}")
        try:
            bak.write_text(original, encoding="utf-8")
            print(f"  - Backup: {bak.name}")
        except OSError as e:
            print(f"ERROR: could not write backup {bak}: {e}", file=sys.stderr)
            return 2

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(patched, encoding="utf-8")
        tmp.replace(target)
    except OSError as e:
        print(f"ERROR: could not write {target}: {e}", file=sys.stderr)
        return 2

    print(f"Patched {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
