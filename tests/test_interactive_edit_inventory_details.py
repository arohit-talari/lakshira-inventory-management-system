"""
Tier 2 -- Edit Inventory Details (Op 2), driven through a real pty.

Each mutating test creates its own fresh unit via
tests/fixtures_data.create_fresh_available_unit() rather than sharing one --
this operation exists to *change* a unit's data, so tests that mutate need
independent fixtures, unlike Discount Simulator's read-only lookups which
could safely share one.

Run: pytest tests/test_interactive_edit_inventory_details.py -v -s
"""
import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _enter_edit(child, sku):
    child.sendline("2")
    child.expect("SKU:")
    child.sendline(sku)
    child.expect("Is this the correct unit\\?")
    child.sendline("yes")
    child.expect("Which field would you like to update\\?")


class TestSimpleFieldEdit:
    def test_editing_supplier_updates_the_sheet(self):
        unit = fd.create_fresh_available_unit("EDIT-SUPPLIER")
        original_supplier = unit["Supplier"]

        child = spawn_app()
        try:
            _enter_edit(child, unit["SKU"])
            child.send(ENTER)  # "Supplier" is the first field
            child.expect("Select supplier:")
            child.send(DOWN)  # pick a different supplier than the current one
            child.send(ENTER)
            child.expect("Which field would you like to update\\?")
            child.send((DOWN * 7) + ENTER)  # "Done — review changes"

            text = expect_clean(child, "Apply these changes\\?")
            assert f"PROPOSED CHANGES — {unit['SKU']}" in text
            assert original_supplier in text

            child.send(ENTER)  # "Confirm and apply changes"
            text = expect_clean(child, "updated successfully")
            assert unit["SKU"] in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Supplier"] != original_supplier


class TestCostFieldEditRecalculatesPricing:
    def test_editing_base_price_recalculates_total_cost_and_margin(self):
        unit = fd.create_fresh_available_unit("EDIT-COST")
        original_total_cost_usd = inv._sheet_float(unit["Total Cost (USD)"])
        original_selling_price = inv._sheet_float(unit["Selling Price (USD)"])
        new_base_price_inr = 20000.0

        child = spawn_app()
        try:
            _enter_edit(child, unit["SKU"])
            child.send((DOWN * 4) + ENTER)  # "Base Price + GST Tax (INR)"
            child.expect("New Base Price")
            child.sendline(str(int(new_base_price_inr)))
            child.expect("review or update those too")
            child.sendline("no")  # don't also touch Shipping/Detailing
            child.expect("Confirm this rate\\?")
            child.sendline("yes")

            text = expect_clean(child, "How would you like to proceed\\?")
            assert "RECALCULATION PREVIEW" in text
            assert f"{new_base_price_inr:,.2f}" in text
            child.send(ENTER)  # "Proceed with this price"

            child.expect("Do you need to change any more details\\?")
            child.sendline("no")

            text = expect_clean(child, "Apply these changes\\?")
            assert "PROPOSED CHANGES" in text
            child.send(ENTER)  # "Confirm and apply changes"
            text = expect_clean(child, "updated successfully")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        new_total_cost_inr = new_base_price_inr + 750.0  # unchanged shipping, no detailing
        assert inv._sheet_float(updated["Total Cost (INR)"]) == new_total_cost_inr
        new_total_cost_usd = inv._sheet_float(updated["Total Cost (USD)"])
        assert new_total_cost_usd > original_total_cost_usd
        # Selling Price itself is untouched by this operation (owned by Reprice) --
        # only the derived markup/margin/gross-profit figures should move.
        assert inv._sheet_float(updated["Selling Price (USD)"]) == original_selling_price
        # Markup % is a formatted percentage string (e.g. "48.4%") -- _sheet_float()
        # doesn't strip "%", only commas, so parse it the same way the app's own
        # internal _pct() helpers do rather than reusing _sheet_float() here.
        def _pct(v):
            return float(str(v).replace("%", "").strip())
        assert _pct(updated["Markup %"]) != _pct(unit["Markup %"])


class TestCancellationPaths:
    def test_no_edits_then_cancel_writes_nothing(self):
        unit = fd.create_fresh_available_unit("EDIT-NOCHANGE")
        child = spawn_app()
        try:
            _enter_edit(child, unit["SKU"])
            child.send((DOWN * 7) + ENTER)  # "Cancel — no changes" (7 fields precede it here, no "Done" yet)
            text = expect_clean(child, "Returning to Main Menu")
            assert "No changes made" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Supplier"] == unit["Supplier"]

    def test_discard_at_final_review_writes_nothing(self):
        unit = fd.create_fresh_available_unit("EDIT-DISCARD")
        child = spawn_app()
        try:
            _enter_edit(child, unit["SKU"])
            child.send(ENTER)  # Supplier
            child.expect("Select supplier:")
            child.send(DOWN)
            child.send(ENTER)
            child.expect("Which field would you like to update\\?")
            child.send((DOWN * 7) + ENTER)  # Done — review changes
            child.expect("Apply these changes\\?")
            child.send((DOWN * 2) + ENTER)  # "Discard changes & continue"
            text = expect_clean(child, "Existing details retained")
            assert "Existing details retained" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Supplier"] == unit["Supplier"]


class TestStatusRejection:
    def test_sold_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("EDIT-SOLD-REJECT", status="Sold")
        child = spawn_app()
        try:
            child.sendline("2")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")  # re-prompted
            assert "already been sold" in text
        finally:
            close(child)

    def test_unassigned_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("EDIT-UNASSIGNED-REJECT", status="Unassigned")
        child = spawn_app()
        try:
            child.sendline("2")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "isn't eligible for this operation" in text
        finally:
            close(child)


class TestBlankCostWarning:
    def test_blank_cost_unit_shows_warning(self):
        unit = fd.get_or_create_blank_cost_unit()
        child = spawn_app()
        try:
            child.sendline("2")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "Is this the correct unit\\?")
            assert "blank or unreadable" in text
        finally:
            close(child)
