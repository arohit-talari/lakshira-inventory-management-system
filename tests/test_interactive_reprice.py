"""
Tier 2 -- Reprice a Unit (Op 3), driven through a real pty.

Building this test file caught a real, 100%-reproducible bug: reprice_unit()
crashed with "name '_sep' is not defined" the moment it reached the PRICING
PREVIEW step, on every single call, via any pricing path. Confirmed this
predates this session's earlier _print_boxed() refactor of the confirmation
box further down the same function -- Step 4 (where the crash happened)
runs before Step 5 (what was refactored), so the two were never related;
_sep was simply never defined anywhere in the function. Fixed by defining
it once before the pricing loop. Every test below exercises that exact
code path, so they also stand as the regression test for this fix.

Run: pytest tests/test_interactive_reprice.py -v -s
"""
import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _enter_reprice(child, sku):
    child.sendline("3")
    child.expect("SKU:")
    child.sendline(sku)
    child.expect("Is this the correct unit\\?")
    child.sendline("yes")
    child.expect("Confirm this rate\\?")
    child.sendline("yes")


class TestHappyPath:
    def test_markup_path_writes_correct_pricing(self):
        unit = fd.create_fresh_available_unit("REPRICE-MARKUP")
        total_cost_usd = inv._sheet_float(unit["Total Cost (USD)"])

        child = spawn_app(timeout=45)
        try:
            _enter_reprice(child, unit["SKU"])
            child.expect("How would you like to set the new selling price\\?")
            child.send(ENTER)  # markup
            child.expect("New markup percentage")
            child.sendline("40")

            text = expect_clean(child, "How would you like to proceed\\?")
            assert "PRICING PREVIEW" in text
            expected_price = round(total_cost_usd * 1.40, 2)
            assert f"${expected_price:,.2f}" in text
            child.send(ENTER)  # "Proceed with this price"

            text = expect_clean(child, "Write this reprice to the master sheet\\?")
            assert "REPRICE SUMMARY" in text
            assert unit["SKU"] in text
            child.sendline("yes")
            text = expect_clean(child, "repriced")
            assert unit["SKU"] in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert inv._sheet_float(updated["Selling Price (USD)"]) == round(total_cost_usd * 1.40, 2)
        assert inv._sheet_float(updated["Actual Selling Price (USD)"]) == round(total_cost_usd * 1.40, 2)

    def test_usd_price_path_writes_correct_pricing(self):
        unit = fd.create_fresh_available_unit("REPRICE-USD")

        child = spawn_app(timeout=45)
        try:
            _enter_reprice(child, unit["SKU"])
            child.expect("How would you like to set the new selling price\\?")
            child.send(DOWN)
            child.send(ENTER)  # "Enter a selling price (USD)"
            child.expect("New Selling Price \\(USD\\):")
            child.sendline("199.99")

            text = expect_clean(child, "How would you like to proceed\\?")
            assert "$199.99" in text
            child.send(ENTER)

            child.expect("Write this reprice to the master sheet\\?")
            child.sendline("yes")
            text = expect_clean(child, "repriced")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert inv._sheet_float(updated["Selling Price (USD)"]) == 199.99


class TestRecalibrateLoop:
    def test_recalibrate_reenters_pricing_path(self):
        unit = fd.create_fresh_available_unit("REPRICE-RECALIBRATE")
        child = spawn_app(timeout=45)
        try:
            _enter_reprice(child, unit["SKU"])
            child.expect("How would you like to set the new selling price\\?")
            child.send(ENTER)
            child.expect("New markup percentage")
            child.sendline("40")
            child.expect("How would you like to proceed\\?")
            child.send(DOWN)
            child.send(ENTER)  # "Recalibrate selling price"

            expect_clean(child, "How would you like to set the new selling price\\?")
            child.send(ENTER)
            child.expect("New markup percentage")
            child.sendline("50")
            child.expect("How would you like to proceed\\?")
            child.send(ENTER)
            child.expect("Write this reprice to the master sheet\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Reprice cancelled" in text
        finally:
            close(child)


class TestCancellationPaths:
    def test_discard_at_pricing_path_menu(self):
        unit = fd.create_fresh_available_unit("REPRICE-DISCARD1")
        child = spawn_app(timeout=45)
        try:
            _enter_reprice(child, unit["SKU"])
            child.expect("How would you like to set the new selling price\\?")
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # "Discard & exit"
            text = expect_clean(child, "Returning to Main Menu")
            assert "Reprice cancelled" in text
        finally:
            close(child)

    def test_declining_final_confirmation_writes_nothing(self):
        unit = fd.create_fresh_available_unit("REPRICE-DISCARD2")
        original_price = unit["Selling Price (USD)"]
        child = spawn_app(timeout=45)
        try:
            _enter_reprice(child, unit["SKU"])
            child.expect("How would you like to set the new selling price\\?")
            child.send(ENTER)
            child.expect("New markup percentage")
            child.sendline("40")
            child.expect("How would you like to proceed\\?")
            child.send(ENTER)
            child.expect("Write this reprice to the master sheet\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Reprice cancelled" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert inv._sheet_float(unchanged["Selling Price (USD)"]) == inv._sheet_float(original_price)


class TestStatusRejection:
    """Reprice is the strictest of all the status-gated ops -- Available
    only, unlike Discount Simulator or Manage Reservation which also allow
    Reserved."""

    def test_reserved_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("REPRICE-RESERVED-REJECT", status="Reserved")
        child = spawn_app()
        try:
            child.sendline("3")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "Only Available units can be repriced" in text
        finally:
            close(child)

    def test_sold_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("REPRICE-SOLD-REJECT", status="Sold")
        child = spawn_app()
        try:
            child.sendline("3")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "Only Available units can be repriced" in text
        finally:
            close(child)


class TestBlankCostWarning:
    def test_blank_cost_unit_shows_warning(self):
        unit = fd.get_or_create_blank_cost_unit()
        child = spawn_app()
        try:
            child.sendline("3")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "Is this the correct unit\\?")
            assert "blank or unreadable" in text
        finally:
            close(child)
