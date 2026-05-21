"""
Parser fixtures and tests for ttd_seat_verification.py.

The live seat-plan HTML can't be fetched from this sandbox, so the test
fixtures here are hand-crafted to mirror the three shapes we expect:

  A. ASP.NET MVC view with an inline JSON state block
  B. Visual legend rendered as HTML elements
  C. Bare prices with no structured legend — the C strategy fallback

Each fixture also contains noise (booking fees, "from £X" headers,
voucher amounts) to exercise the noise filter. Run this with:

    python test_parser.py

The Churchill's Urinal fixture deliberately puts a phantom "from £13.00"
in the header and a real £25/£31/£40 legend below it — the bug we're
fixing. The parser must return [25.0, 31.0, 40.0], not include £13.
"""

import sys
sys.path.insert(0, ".")

from ttd_seat_verification import (
    parse_seat_plan_prices,
    _extract_prices_strategy_a,
    _extract_prices_strategy_b,
    _extract_prices_strategy_c,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A: typical ASP.NET MVC inline-JSON shape
FIXTURE_A_JSON_BLOCK = """
<!DOCTYPE html>
<html><head><title>Some show</title></head><body>
<script>
var seatPlanData = {
    "priceBands": [
        {"id": 1, "price": 40.00, "label": "Stalls A-D"},
        {"id": 2, "price": 31.00, "label": "Stalls E-J"},
        {"id": 3, "price": 25.00, "label": "Side / Restricted"}
    ],
    "bookingFee": 2.50,
    "showId": 7219
};
</script>
<div>Some random £13.00 from £ marketing text we should ignore</div>
</body></html>
"""

# B: visual legend in HTML — the most common "legacy" rendering
FIXTURE_B_LEGEND_HTML = """
<!DOCTYPE html>
<html><head><title>Churchill's Urinal</title></head><body>
<div class="show-header">
    <h1>Churchill's Urinal - King's Head Theatre</h1>
    <p class="header-from-price">from £13.00</p>  <!-- phantom -->
</div>
<div class="price-legend">
    <ul class="legend-list">
        <li><span class="legend-dot color1"></span><span>£40.00</span></li>
        <li><span class="legend-dot color2"></span><span>£31.00</span></li>
        <li><span class="legend-dot color3"></span><span>£25.00</span></li>
        <li><span class="legend-dot empty"></span><span>N/A</span></li>
    </ul>
</div>
<div class="seat-map">... lots of dots ...</div>
<div class="footer">Booking fee £2.50 applies. Restoration levy £1.00.</div>
</body></html>
"""

# C: bare prices, no structured container — Strategy C only
FIXTURE_C_BARE = """
<!DOCTYPE html>
<html><head><title>Some show</title></head><body>
<div>
    <h1>Pick your seats</h1>
    <p>£28.50</p>
    <p>£35.00</p>
    <p>£45.00</p>
    <p>Booking fee £2.50</p>
    <p>Gift voucher £10.00</p>
</div>
</body></html>
"""

# Phantom-tier case — the actual bug. Phantom "from £13" in the header,
# real legend below. We must NOT pick up the £13.
FIXTURE_PHANTOM = """
<!DOCTYPE html>
<html><head><title>Churchill's Urinal</title></head><body>
<header>
    <div class="show-title">Churchill's Urinal - King's Head Theatre</div>
    <div class="from-price-banner">From £13.00</div>
</header>
<section class="price-legend">
    <div class="price-band"><span class="dot purple"></span>£40.00</div>
    <div class="price-band"><span class="dot maroon"></span>£31.00</div>
    <div class="price-band"><span class="dot red"></span>£25.00</div>
    <div class="price-band"><span class="dot grey"></span>N/A</div>
</section>
<div class="footer">Includes £2.50 booking fee. Postage £1.95.</div>
</body></html>
"""

# Empty / unstyled fallback — what we'd get on a sold-out perf
FIXTURE_NO_LEGEND = """
<!DOCTYPE html>
<html><head><title>Tickets not available</title></head><body>
<h1>Tickets not available</h1>
<p>Sorry, this performance is no longer on sale.</p>
<p>Browse other shows from £15.00 here.</p>
</body></html>
"""

# Sold-out but page renders legend — a tier that's all-grey on the map.
# We still want to return the tiers (the user knows they're sold out
# from other signals like availability="OutOfStock").
FIXTURE_SOLD_OUT_BUT_LEGEND = """
<!DOCTYPE html>
<html><body>
<div class="price-legend">
    <span>£50.00</span>
    <span>£35.00</span>
    <span>£20.00</span>
</div>
<div class="seat-map">all seats grey here</div>
</body></html>
"""

# Many price bands — National Theatre style
FIXTURE_MANY_TIERS = """
<!DOCTYPE html>
<html><body>
<div class="price-legend">
    <ul>
        <li>£89.00</li>
        <li>£75.00</li>
        <li>£59.00</li>
        <li>£45.00</li>
        <li>£35.00</li>
        <li>£25.00</li>
        <li>£18.00</li>
    </ul>
</div>
</body></html>
"""

# Out-of-bounds noise: a "voucher £600" should NOT make the cap.
FIXTURE_OUT_OF_BOUNDS = """
<!DOCTYPE html>
<html><body>
<div class="price-legend">
    <span>£25.00</span>
    <span>£40.00</span>
</div>
<div>Gift voucher worth £600.00 available!</div>
<div>Free upgrade: was £0.01</div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _expect(name: str, got, want):
    if got == want:
        print(f"  PASS  {name}: {got}")
        return True
    print(f"  FAIL  {name}: got {got}, want {want}")
    return False


def test_strategy_a_json():
    print("\n[A] inline JSON")
    tiers = _extract_prices_strategy_a(FIXTURE_A_JSON_BLOCK)
    return _expect("strategy a picks band prices, skips booking fee",
                   tiers, [25.0, 31.0, 40.0])


def test_strategy_b_legend():
    print("\n[B] legend HTML")
    tiers = _extract_prices_strategy_b(FIXTURE_B_LEGEND_HTML)
    # Phantom £13.00 is in a `.header-from-price` element preceded by
    # "from £" — Strategy B should NOT capture it.
    return _expect("strategy b picks legend, drops 'from £13.00' header",
                   tiers, [25.0, 31.0, 40.0])


def test_strategy_c_sweep():
    print("\n[C] head sweep")
    tiers = _extract_prices_strategy_c(FIXTURE_C_BARE)
    return _expect("strategy c picks prices, drops booking/voucher fees",
                   tiers, [28.5, 35.0, 45.0])


def test_phantom_tier_case():
    """The actual bug we're fixing. The 'from £13.00' must NOT bleed
    into the result.
    """
    print("\n[!] phantom-tier (Churchill's Urinal pattern)")
    tiers, strategy = parse_seat_plan_prices(FIXTURE_PHANTOM)
    ok1 = _expect("dispatcher picks real tiers",
                  tiers, [25.0, 31.0, 40.0])
    ok2 = _expect("dispatcher used a non-fallback strategy",
                  strategy in ("a_json", "b_legend"), True)
    ok3 = _expect("13.0 is NOT in the result",
                  13.0 not in tiers, True)
    return ok1 and ok2 and ok3


def test_no_legend_returns_empty():
    print("\n[!] no legend on the page")
    tiers, strategy = parse_seat_plan_prices(FIXTURE_NO_LEGEND)
    # Strategy C may catch the "from £15.00" if the noise filter misses
    # it — that's actually fine in this fixture because the £15.00 is
    # explicitly preceded by "from £". So we expect [].
    return _expect("dispatcher returns empty on a not-available page",
                   tiers, [])


def test_sold_out_still_parses():
    print("\n[!] sold-out perf with legend still visible")
    tiers, strategy = parse_seat_plan_prices(FIXTURE_SOLD_OUT_BUT_LEGEND)
    return _expect("legend parses even when all seats are grey",
                   tiers, [20.0, 35.0, 50.0])


def test_many_tiers():
    print("\n[!] many tiers (NT-style)")
    tiers, strategy = parse_seat_plan_prices(FIXTURE_MANY_TIERS)
    return _expect("up to MAX_PLAUSIBLE_TIERS=12 tiers parsed",
                   tiers, [18.0, 25.0, 35.0, 45.0, 59.0, 75.0, 89.0])


def test_out_of_bounds():
    print("\n[!] out-of-bounds prices excluded")
    tiers, strategy = parse_seat_plan_prices(FIXTURE_OUT_OF_BOUNDS)
    ok1 = _expect("real tiers parsed", tiers, [25.0, 40.0])
    ok2 = _expect("£600 voucher excluded by PRICE_MAX_GBP",
                  600.0 not in tiers, True)
    ok3 = _expect("£0.01 decoy excluded by PRICE_MIN_GBP",
                  0.01 not in tiers, True)
    return ok1 and ok2 and ok3


def test_empty_html():
    print("\n[!] empty input")
    tiers, strategy = parse_seat_plan_prices("")
    return _expect("empty input returns empty",
                   (tiers, strategy), ([], "none"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_strategy_a_json,
        test_strategy_b_legend,
        test_strategy_c_sweep,
        test_phantom_tier_case,
        test_no_legend_returns_empty,
        test_sold_out_still_parses,
        test_many_tiers,
        test_out_of_bounds,
        test_empty_html,
    ]
    results = [t() for t in tests]
    n_pass = sum(results)
    n_total = len(results)
    print(f"\n{'=' * 50}")
    print(f"{n_pass}/{n_total} tests passed")
    sys.exit(0 if n_pass == n_total else 1)
