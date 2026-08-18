"""
Tier 2 -- Discount Simulator (Op 4), driven through a real pty.

Chosen as the first scripted interactive flow because it's read-only (the
function's own docstring says so, and this session's audit confirmed it --
see Deliverables/session notes on Op 4), so repeated runs never accumulate
state in the test sheet the way Add/Sale/Reprice would. It also directly
exercises two real fixes made this session: the SKU-lookup scope was
narrowed to Available/Reserved only (Unassigned/Sold/Sold-Partial used to
be accepted and would silently simulate against fabricated numbers), and a
blank-cost warning was added.

Fixtures pull real SKUs from the live TEST sheet by status at collection
time rather than hardcoding specific SKUs, so the test doesn't silently
rot if that unit's status changes later.

Run: pytest tests/test_interactive_discount_simulator.py -v -s
(-s recommended the first few times so you can watch the real transcript)

Every test in this file carries pytest.mark.flaky: real live-API latency
(Google Sheets, the ECB rate service) occasionally pushes a single
pexpect.expect() past its timeout mid-test, on a run of many sequential
live-dependent tests, purely from network variance -- confirmed while
building this suite by re-running every observed timeout failure in
isolation, where it passed cleanly every time with no code changes. This is
scoped to Tier 2 only via this module-level marker, deliberately not a
global pytest.ini setting -- Tier 1's pure-function tests must stay 100%
deterministic, since a flake there would mean a real bug, not network noise.
"""
import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)


def _first_sku_with_status(*statuses):
    rows = inv.get_all_rows()
    for r in rows:
        if r.get("Status", "").strip() in statuses:
            return r
    return None


def _first_low_margin_unit():
    """An Available/Reserved unit priced above cost but under the 15%
    healthy-margin line -- distinct from the below-cost fixture, which
    tests the case one tier worse."""
    rows = inv.get_all_rows()
    for r in rows:
        if r.get("Status", "").strip() not in ("Available", "Reserved"):
            continue
        tc = inv._sheet_float(r.get("Total Cost (USD)"))
        sp = inv._sheet_float(r.get("Selling Price (USD)"))
        if tc > 0 and sp > tc:
            margin = (sp - tc) / sp * 100
            if margin < 15:
                return r
    return None


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


@pytest.fixture(scope="module")
def available_unit():
    row = _first_sku_with_status("Available")
    if row is None:
        pytest.skip("No Available-status unit on the test sheet to run this against.")
    return row


@pytest.fixture(scope="module")
def reserved_unit():
    row = _first_sku_with_status("Reserved")
    if row is None:
        pytest.skip("No Reserved-status unit on the test sheet to run this against.")
    return row


@pytest.fixture(scope="module")
def sold_unit():
    row = _first_sku_with_status("Sold", "Sold - Partial Payment")
    if row is None:
        pytest.skip("No Sold-status unit on the test sheet to run this against.")
    return row


@pytest.fixture(scope="module")
def unassigned_unit():
    row = _first_sku_with_status("Unassigned")
    if row is None:
        pytest.skip("No Unassigned-status unit on the test sheet to run this against.")
    return row


@pytest.fixture(scope="module")
def low_margin_unit():
    row = _first_low_margin_unit()
    if row is None:
        pytest.skip("No Available/Reserved unit under the 15% margin line on the test sheet.")
    return row


@pytest.fixture(scope="module")
def blank_cost_unit():
    """Synthetic fixture (tests/fixtures_data.py) -- a blank Total Cost
    cell doesn't occur via normal use, only hand-edited/legacy data, so
    this is created directly rather than hunted for on the live sheet."""
    return fd.get_or_create_blank_cost_unit()


@pytest.fixture(scope="module")
def below_cost_unit():
    """Synthetic fixture -- deliberately priced below its own cost."""
    return fd.get_or_create_below_cost_unit()


def _enter_discount_simulator(child):
    # spawn_app() already waits for the main menu prompt before returning.
    child.sendline("4")
    child.expect("Select an option:")
    child.send(ENTER)  # "Look up an existing unit" is the default first choice


class TestSoldAndUnassignedUnitsAreRejected:
    """Pins the scope-restriction fix: Sold / Sold - Partial Payment /
    Unassigned units must be rejected before any pricing math runs."""

    def test_sold_unit_rejected(self, sold_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(sold_unit["SKU"])
            text = expect_clean(child, "SKU:")  # re-prompted, since the lookup was rejected
            assert "Only Available or Reserved units can be simulated" in text
            assert "UNIT SUMMARY" not in text
        finally:
            close(child)

    def test_unassigned_unit_rejected(self, unassigned_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(unassigned_unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "Only Available or Reserved units can be simulated" in text
        finally:
            close(child)


def _proceed_past_below_cost_gate_if_shown(child):
    """After a unit lookup, the app goes straight to 'Discount to
    simulate:' unless the unit is already below cost at full price, in
    which case it asks 'Simulate a discount anyway?' first. Handles both
    so callers don't need to know which one a given fixture will hit."""
    child.expect("Simulate a discount anyway\\?|Discount to simulate:")
    if "anyway" in child.after:
        child.sendline("yes")
        child.expect("Discount to simulate:")


class TestAvailableUnitShowsPricing:
    def test_lookup_shows_unit_summary_with_correct_figures(self, available_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(available_unit["SKU"])
            # ask_percent's own prompt text is what terminates the UNIT
            # SUMMARY box -- entering the discount loop is mandatory (there's
            # no "skip" option once a unit is found), so this is the actual
            # boundary of the summary screen, not "Look up a different unit?".
            text = expect_clean(child, "Simulate a discount anyway\\?|Discount to simulate:")

            assert "UNIT SUMMARY" in text
            assert available_unit["SKU"] in text

            expected_cost = inv._sheet_float(available_unit.get("Total Cost (USD)"))
            expected_price = inv._sheet_float(available_unit.get("Selling Price (USD)"))
            assert f"${expected_cost:,.2f}" in text
            assert f"${expected_price:,.2f}" in text
        finally:
            close(child)

    def test_discount_math_matches_manual_calculation(self, available_unit):
        total_cost = inv._sheet_float(available_unit.get("Total Cost (USD)"))
        selling_price = inv._sheet_float(available_unit.get("Selling Price (USD)"))
        if total_cost <= 0 or selling_price <= 0:
            pytest.skip("Fixture unit has no usable cost/price to simulate a discount against.")

        discount_pct = 20.0
        expected_discount_amount = round(selling_price * discount_pct / 100, 2)
        expected_actual_price = round(selling_price - expected_discount_amount, 2)
        expected_gross_profit = round(expected_actual_price - total_cost, 2)

        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(available_unit["SKU"])
            _proceed_past_below_cost_gate_if_shown(child)
            child.sendline(str(discount_pct))
            text = expect_clean(child, "Try a different discount\\?")

            assert "DISCOUNT SIMULATION" in text
            assert f"${expected_discount_amount:,.2f}" in text
            assert f"${expected_actual_price:,.2f}" in text
            gp_sign = "+" if expected_gross_profit >= 0 else "-"
            assert f"{gp_sign}${abs(expected_gross_profit):,.2f}" in text

            child.sendline("no")  # "Try a different discount?"
            child.expect("Look up a different unit\\?")
            child.sendline("no")
            child.expect("Return to Main Menu")
            child.send(ENTER)
        finally:
            close(child)


class TestReservedUnitAccepted:
    """The scope-restriction fix allows Available OR Reserved -- this
    fixture proves Reserved specifically, not just Available, since both
    share the same gate condition but only Available was covered above."""

    def test_reserved_unit_lookup_succeeds(self, reserved_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(reserved_unit["SKU"])
            text = expect_clean(child, "Simulate a discount anyway\\?|Discount to simulate:")
            assert "UNIT SUMMARY" in text
            assert "Only Available or Reserved" not in text
            expected_cost = inv._sheet_float(reserved_unit.get("Total Cost (USD)"))
            assert f"${expected_cost:,.2f}" in text
        finally:
            close(child)


class TestLowMarginWarning:
    """margin_pct < 15 at full price (before any discount) must show the
    LOW MARGIN alert in the UNIT SUMMARY screen itself."""

    def test_low_margin_unit_shows_warning(self, low_margin_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(low_margin_unit["SKU"])
            text = expect_clean(child, "Discount to simulate:")
            assert "LOW MARGIN" in text
            assert "below the 15% threshold" in text
        finally:
            close(child)


class TestBelowCostGate:
    """A unit already selling below cost at full price (before any
    discount) must show BELOW COST and gate the discount loop behind
    'Simulate a discount anyway?' -- declining must skip straight to
    'Look up a different unit?' without ever asking for a discount %."""

    def test_below_cost_unit_shows_warning(self, below_cost_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(below_cost_unit["SKU"])
            text = expect_clean(child, "Simulate a discount anyway\\?")
            assert "BELOW COST" in text
            assert "already at a loss" in text
        finally:
            close(child)

    def test_declining_the_gate_skips_the_discount_loop_entirely(self, below_cost_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(below_cost_unit["SKU"])
            child.expect("Simulate a discount anyway\\?")
            child.sendline("no")
            text = expect_clean(child, "Look up a different unit\\?")
            assert "Discount to simulate" not in text
        finally:
            close(child)

    def test_accepting_the_gate_proceeds_to_discount_loop(self, below_cost_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(below_cost_unit["SKU"])
            child.expect("Simulate a discount anyway\\?")
            child.sendline("yes")
            child.expect("Discount to simulate:")
            child.sendline("10")
            text = expect_clean(child, "Try a different discount\\?")
            assert "DISCOUNT SIMULATION" in text
            assert "pushing the unit" in text or "below cost" in text.lower()
        finally:
            close(child)


class TestBlankCostWarning:
    """This session's fix: a blank Total Cost (USD) cell must print an
    explicit warning rather than silently rendering every dependent figure
    as a fabricated $0.00."""

    def test_blank_cost_unit_shows_warning(self, blank_cost_unit):
        child = spawn_app()
        try:
            _enter_discount_simulator(child)
            child.expect("SKU:")
            child.sendline(blank_cost_unit["SKU"])
            text = expect_clean(child, "Simulate a discount anyway\\?|Discount to simulate:")
            assert "blank or unreadable" in text
            assert "$0.00" in text  # the fabricated total cost, shown as part of the warned display
        finally:
            close(child)


class TestMode2CustomFigures:
    """Mode 2 ('Simulate with custom figures') is a completely separate
    code path from the SKU-lookup mode covered above -- hypothetical
    total cost, a real ECB rate fetch, markup-or-USD pricing entry, then
    the same mandatory discount loop and a final 'adjust and redo?' loop."""

    def test_markup_path_full_cycle(self):
        child = spawn_app(timeout=35)  # allow for the real ECB API round-trip
        try:
            child.sendline("4")
            child.expect("Select an option:")
            child.send(DOWN)  # "Simulate with custom figures"
            child.send(ENTER)

            child.expect("Total Cost \\(INR\\):")
            child.sendline("25000")
            child.expect("Exchange Rate Date")
            child.sendline("01-15-2026")
            child.expect("Confirm this rate\\?")
            child.sendline("yes")

            text = expect_clean(child, "How would you like to set the selling price\\?")
            assert "COST BASIS" in text
            assert "$25,000" in text or "25,000" in text  # INR total cost display

            child.send(ENTER)  # "Enter a markup percentage"
            child.expect("Markup percentage:")
            child.sendline("50")

            text = expect_clean(child, "Discount to simulate:")
            assert "BASE PRICING" in text
            assert "50.0%" in text

            child.sendline("15")
            text = expect_clean(child, "Try a different discount\\?")
            assert "DISCOUNT SIMULATION" in text
            assert "15.0%" in text

            child.sendline("no")
            child.expect("Adjust selling price or markup\\?")
            child.sendline("no")
            child.expect("Returning to Main Menu")
        finally:
            close(child)

    def test_usd_price_path_and_discard(self):
        """Covers the second pricing sub-path (direct USD entry instead of
        markup %) and the 'Discard & exit' escape from the pricing-path
        menu, neither exercised by the markup-path test above."""
        child = spawn_app(timeout=35)
        try:
            child.sendline("4")
            child.expect("Select an option:")
            child.send(DOWN)
            child.send(ENTER)

            child.expect("Total Cost \\(INR\\):")
            child.sendline("10000")
            child.expect("Exchange Rate Date")
            child.sendline("01-15-2026")
            child.expect("Confirm this rate\\?")
            child.sendline("yes")

            child.expect("How would you like to set the selling price\\?")
            child.send(DOWN)  # "Enter a selling price (USD)"
            child.send(ENTER)
            child.expect("Selling Price \\(USD\\):")
            child.sendline("200")

            text = expect_clean(child, "Discount to simulate:")
            assert "BASE PRICING" in text
            assert "$200.00" in text

            child.sendline("5")
            child.expect("Try a different discount\\?")
            child.sendline("no")
            child.expect("Adjust selling price or markup\\?")
            child.sendline("yes")

            # Back at the pricing-path menu -- this time exit via Discard.
            child.expect("How would you like to set the selling price\\?")
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # "Discard & exit"
            child.expect("Returning to Main Menu")
        finally:
            close(child)
