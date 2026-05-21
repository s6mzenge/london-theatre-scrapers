"""
Unit tests for ttd_seat_verification.py — JSON / BindSeatPlan edition.

Replaces the original test_parser.py, which exercised three HTML-regex
strategies that turned out to be the wrong approach (the seat-plan
page is JS-rendered for everything except Coliseum). This file tests
the JSON parser against:

  1. Five live BindSeatPlan responses saved to ./fixtures/ — Churchill's
     Urinal, Derrière, Beetlejuice, Kinky Boots, AMND Globe. These are
     the ground truth.
  2. A handful of synthetic edge cases — malformed JSON, ResultCode
     non-zero, empty PriceBands, prices outside the plausibility band,
     £ formatting variants.

Run from the directory containing this file plus the fixtures/ subdir:

    python test_ttd_seat_verification.py

Exits 0 if every test passes, 1 otherwise. No third-party test runner
required.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# Make the module importable when the test is run directly from out/.
sys.path.insert(0, str(Path(__file__).parent))
import ttd_seat_verification as mod


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Test infrastructure: tiny assert / collect / report so we don't need pytest
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def assert_eq(self, name, actual, expected):
        if actual == expected:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            self.failures.append((name, actual, expected))
            print(f"  FAIL  {name}")
            print(f"          expected: {expected!r}")
            print(f"          actual:   {actual!r}")

    def assert_true(self, name, value):
        self.assert_eq(name, bool(value), True)


# ---------------------------------------------------------------------------
# Fixture-driven tests (the ground truth)
# ---------------------------------------------------------------------------
#
# For each saved BindSeatPlan response, the parser must extract exactly
# these tier sets. The expected values come from manually inspecting the
# JSON earlier in the conversation.

FIXTURE_EXPECTATIONS = {
    7219: {
        "label":           "Churchill's Urinal @ King's Head",
        "expected_tiers":  [25.0, 31.0, 40.0],
        "expected_face":   [20.0, 25.0, 32.5],
        "expected_seats":  53,
        "calendar":        13.0,
        "agrees":          False,   # calendar £13 vs real £25 = phantom tier
    },
    7077: {
        "label":           "Derrière on a G String @ King's Head",
        "expected_tiers":  [18.5, 22.5, 25.0],
        "expected_face":   [22.5, 27.5, 32.5],
        "expected_seats":  46,
        "calendar":        13.0,
        "agrees":          False,
    },
    7015: {
        "label":           "Beetlejuice @ Prince Edward",
        "expected_tiers":  [150.0, 180.0],
        "expected_face":   [125.0, 150.0],
        "expected_seats":  6,
        "calendar":        84.0,
        "agrees":          False,   # mostly sold out, premium only
    },
    6920: {
        "label":           "Kinky Boots @ Coliseum",
        "expected_tiers":  [15.9, 26.5, 37.1, 47.7, 63.6, 79.5, 95.4, 165.0],
        "expected_face":   [16.25, 26.25, 46.25, 61.25, 71.25, 86.25, 101.25, 136.25],
        "expected_seats":  320,
        "calendar":        17.0,
        "agrees":          False,   # 15.9 vs 17.0 diff > £0.50 tolerance
    },
    7158: {
        "label":           "AMND @ Globe",
        "expected_tiers":  [13.0, 31.0, 43.0, 55.0, 67.0],
        "expected_face":   [10.0, 25.0, 35.0, 45.0, 55.0],
        "expected_seats":  27,
        "calendar":        13.0,
        "agrees":          True,
    },
}


def test_fixtures(r: TestResult) -> None:
    """Every saved BindSeatPlan response parses to the exact tier set
    we observed manually. This is the most important test in the file
    — if these fail, the production data will be wrong."""
    for sid, exp in FIXTURE_EXPECTATIONS.items():
        fp = FIXTURES_DIR / f"show_{sid}.html"
        if not fp.exists():
            print(f"  SKIP  fixture {fp} missing — drop the saved "
                  f"BindSeatPlan response there and rerun")
            continue
        body = fp.read_text(encoding="utf-8")
        print(f"\n[fixture] {exp['label']} (show {sid})")

        parsed = mod.parse_bindseatplan(body)
        if parsed is None:
            r.failed += 1
            print(f"  FAIL  parser returned None for show {sid}")
            continue

        r.assert_eq(f"show {sid}: ResultCode=='0'", parsed["result_code"], "0")
        r.assert_eq(f"show {sid}: tiers", parsed["tiers"], exp["expected_tiers"])
        r.assert_eq(f"show {sid}: face_tiers", parsed["face_tiers"], exp["expected_face"])
        r.assert_eq(f"show {sid}: n_seats", parsed["n_seats"], exp["expected_seats"])

        # Calendar agreement: re-derive to verify the tolerance logic
        agrees = abs(min(parsed["tiers"]) - exp["calendar"]) <= mod.AGREE_TOLERANCE_GBP
        r.assert_eq(f"show {sid}: agrees_with_calendar", agrees, exp["agrees"])


# ---------------------------------------------------------------------------
# Synthetic edge cases
# ---------------------------------------------------------------------------

def test_empty_input(r: TestResult) -> None:
    print("\n[synthetic] empty input")
    r.assert_eq("empty string → None", mod.parse_bindseatplan(""), None)
    r.assert_eq("whitespace only → None", mod.parse_bindseatplan("   "), None)


def test_malformed_json(r: TestResult) -> None:
    print("\n[synthetic] malformed JSON")
    r.assert_eq("plain HTML → None",
                mod.parse_bindseatplan("<html><body>oops</body></html>"),
                None)
    r.assert_eq("truncated JSON → None",
                mod.parse_bindseatplan('{"ResultCode":"0","data":'),
                None)
    r.assert_eq("non-object root (array) → None",
                mod.parse_bindseatplan('[]'),
                None)


def test_result_code_nonzero(r: TestResult) -> None:
    print("\n[synthetic] ResultCode != '0'")
    body = json.dumps({
        "ResultCode": "1",
        "RedirectUrl": "/login",
        "data": None,
    })
    p = mod.parse_bindseatplan(body)
    r.assert_eq("ResultCode preserved", p["result_code"], "1")
    r.assert_eq("tiers empty when data is null", p["tiers"], [])


def test_empty_price_bands(r: TestResult) -> None:
    print("\n[synthetic] ResultCode=0 but PriceBands empty (off-sale)")
    body = json.dumps({
        "ResultCode": "0",
        "data": {"Performances": [], "PriceBands": []},
    })
    p = mod.parse_bindseatplan(body)
    r.assert_eq("ResultCode is '0'", p["result_code"], "0")
    r.assert_eq("tiers empty", p["tiers"], [])
    r.assert_eq("face_tiers empty", p["face_tiers"], [])
    r.assert_eq("n_seats is 0", p["n_seats"], 0)


def test_plausibility_bounds(r: TestResult) -> None:
    print("\n[synthetic] prices outside plausibility band are dropped")
    body = json.dumps({
        "ResultCode": "0",
        "data": {
            "Performances": [{"x": 1}],
            "PriceBands": [
                {"Price": "£0.01",   "FaceValue": "£0.01"},      # too cheap
                {"Price": "£25.00",  "FaceValue": "£20.00"},     # legit
                {"Price": "£999.00", "FaceValue": "£800.00"},    # too expensive
                {"Price": "£40.00",  "FaceValue": "£32.50"},     # legit
            ],
        },
    })
    p = mod.parse_bindseatplan(body)
    r.assert_eq("only in-bounds tiers kept", p["tiers"], [25.0, 40.0])
    r.assert_eq("only in-bounds face values kept", p["face_tiers"], [20.0, 32.5])


def test_money_parsing(r: TestResult) -> None:
    print("\n[synthetic] money string parsing variants")
    cases = [
        ("£25.00",       25.0),
        ("£25",          25.0),
        ("25.00",        25.0),
        ("£1,234.50",    1234.5),     # comma thousands — out of plausibility but parses
        ("  £42.50  ",   42.5),       # whitespace
        ("£0.01",        0.01),
        ("",             None),
        (None,           None),
        ("not a price",  None),
    ]
    for input_s, expected in cases:
        r.assert_eq(f"_parse_money({input_s!r})",
                    mod._parse_money(input_s),
                    expected)


def test_venue_id_extraction(r: TestResult) -> None:
    print("\n[synthetic] VenueId extraction from venue_url")
    cases = [
        ("https://www.theatreticketsdirect.co.uk/venue/134/king's-head-theatre", "134"),
        ("https://www.theatreticketsdirect.co.uk/venue/26/prince-edward",       "26"),
        ("https://www.theatreticketsdirect.co.uk/venue/45/globe",               "45"),
        ("https://example.com/foo/bar",                                          None),
        ("",                                                                     None),
        (None,                                                                   None),
    ]
    for url, expected in cases:
        r.assert_eq(f"extract_venue_id({url!r})",
                    mod.extract_venue_id(url),
                    expected)


def test_build_params_happy_path(r: TestResult) -> None:
    print("\n[synthetic] build_params constructs the right POST body")
    show = {
        "id": 7219,
        "venue_url": "https://www.theatreticketsdirect.co.uk/venue/134/kings-head",
    }
    perf = {
        "verified_perf_id": 773752,
        "date": "2026-05-21",
        "time": "19:00",
    }
    params = mod.build_params(show, perf)
    r.assert_eq("performanceId",    params["performanceId"],   "773752")
    r.assert_eq("showId",           params["showId"],          "7219")
    r.assert_eq("VenueId",          params["VenueId"],         "134")
    r.assert_eq("tickets",          params["tickets"],         "2")
    r.assert_eq("PDate (DD/MM/YYYY)", params["PDate"],         "21/05/2026")
    r.assert_eq("Time (HH-MM)",     params["Time"],            "19-00")
    r.assert_eq("PerformancesFor",  params["PerformancesFor"], "SeatPlan")


def test_build_params_skipped_cases(r: TestResult) -> None:
    print("\n[synthetic] build_params returns None when fields missing")
    base_show = {"id": 7219,
                 "venue_url": "https://x.co.uk/venue/134/foo"}
    base_perf = {"verified_perf_id": 773752,
                 "date": "2026-05-21",
                 "time": "19:00"}

    # No verified_perf_id
    p = dict(base_perf); p["verified_perf_id"] = None
    r.assert_eq("missing verified_perf_id → None",
                mod.build_params(base_show, p), None)

    # Missing venue_url
    s = dict(base_show); s["venue_url"] = None
    r.assert_eq("missing venue_url → None",
                mod.build_params(s, base_perf), None)

    # Malformed venue_url (no /venue/{N}/)
    s = dict(base_show); s["venue_url"] = "https://x.co.uk/about"
    r.assert_eq("malformed venue_url → None",
                mod.build_params(s, base_perf), None)

    # Malformed date
    p = dict(base_perf); p["date"] = "21 May 2026"
    r.assert_eq("malformed date → None",
                mod.build_params(base_show, p), None)

    # Time without colon
    p = dict(base_perf); p["time"] = "1900"
    r.assert_eq("time without colon → None",
                mod.build_params(base_show, p), None)


def test_url_ascii_safe(r: TestResult) -> None:
    print("\n[synthetic] non-ASCII URL handling")
    # Real-world TTD show slugs contain U+2019 RIGHT SINGLE QUOTATION MARK
    # (e.g. Churchill's-urinal-tickets). This char fails to encode on the
    # HTTP/1.1 request line as-is; _ascii_safe_url must percent-encode it.
    url = "https://www.theatreticketsdirect.co.uk/shows/7219/churchill\u2019s-urinal"
    safe = mod._ascii_safe_url(url)
    r.assert_true("curly apostrophe encoded", "%E2%80%99" in safe)
    r.assert_eq("None passthrough", mod._ascii_safe_url(None), None)
    r.assert_eq("empty passthrough", mod._ascii_safe_url(""), "")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 78)
    print("ttd_seat_verification.py — JSON parser tests")
    print("=" * 78)
    r = TestResult()
    for fn in [
        test_fixtures,
        test_empty_input,
        test_malformed_json,
        test_result_code_nonzero,
        test_empty_price_bands,
        test_plausibility_bounds,
        test_money_parsing,
        test_venue_id_extraction,
        test_build_params_happy_path,
        test_build_params_skipped_cases,
        test_url_ascii_safe,
    ]:
        try:
            fn(r)
        except Exception:  # noqa: BLE001
            print(f"  ERROR in {fn.__name__}:")
            traceback.print_exc()
            r.failed += 1
    print()
    print("=" * 78)
    total = r.passed + r.failed
    if r.failed:
        print(f"{r.failed}/{total} TESTS FAILED")
        return 1
    print(f"{r.passed}/{total} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
