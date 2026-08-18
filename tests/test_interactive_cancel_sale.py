"""
Tier 2 -- Cancel a Sale (Op 8), driven through a real pty.

cancel_sale() is unusual among the operations tested so far: it opens with
a "pending refund sweep" that can surface and resolve an *unrelated* unit's
outstanding refund before the operator even picks a SKU to cancel. That
sweep is exercised directly below (TestPendingRefundSweep), not just the
core restore-to-Available path.

Run: pytest tests/test_interactive_cancel_sale.py -v -s
"""
from datetime import date

import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

TODAY_STR = date.today().strftime("%m-%d-%Y")


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _open_cancel_sale(child):
    """Enters option 8. A pending refund left permanently flagged by an
    *earlier* test (in this file or a prior run -- nothing here cleans up
    test data, and TestPartialPaymentRefund's decline case exists
    specifically to leave one behind) can make the pending-refund sweep
    show up before every other test's SKU prompt, not just
    TestPendingRefundSweep's own. Decline it here so every other test
    reaches "SKU:" regardless of that unrelated global state --
    TestPendingRefundSweep drives the "yes" path itself, separately."""
    child.sendline("8")
    index = child.expect(["Mark a refund as issued\\?", "SKU:"])
    if index == 0:
        child.sendline("no")
        child.expect("SKU:")


def _enter_cancel_sale(child, sku):
    _open_cancel_sale(child)
    child.sendline(sku)
    child.expect("Is this the correct unit\\?")
    child.sendline("yes")


class TestHappyPath:
    def test_cancelling_a_fully_paid_sale_restores_available(self):
        unit = fd.create_sold_unit("CANCEL-PAID-FULL")
        child = spawn_app()
        try:
            _enter_cancel_sale(child, unit["SKU"])
            child.expect("Reason for cancelling")
            child.sendline("pytest cancellation reason")

            text = expect_clean(child, "Cancel this sale and restore the unit to Available\\?")
            assert "CANCELLATION SUMMARY" in text
            assert "Sold" in text and "Available" in text
            # Fully paid -- the refund-acknowledgment question never fires.
            assert "refunded to the customer" not in text
            child.sendline("yes")
            text = expect_clean(child, "restored to Available")
            assert unit["SKU"] in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Available"
        assert updated["Customer Name"] == ""
        assert updated["Date Sold"] == ""
        assert "pytest cancellation reason" in updated["Transaction Notes"]


class TestPartialPaymentRefund:
    def test_refund_acknowledged_leaves_no_pending_flag(self):
        unit = fd.create_partial_payment_unit(
            "CANCEL-REFUNDED", customer_name="Pytest Refunded Customer",
            amount_received=75.0, amount_outstanding=50.0,
        )
        child = spawn_app()
        try:
            _enter_cancel_sale(child, unit["SKU"])
            child.expect("Reason for cancelling")
            child.sendline("pytest refund-acknowledged cancellation")
            child.expect("Was the partial payment of \\$75.00 refunded to the customer\\?")
            child.sendline("yes")

            text = expect_clean(child, "Cancel this sale and restore the unit to Available\\?")
            assert "Refunded $75.00 to customer." in text
            child.sendline("yes")
            expect_clean(child, "restored to Available")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Available"
        assert "· REFUND]" not in updated["Inventory Notes"]
        assert "refunded to customer" in updated["Transaction Notes"]

    def test_refund_declined_flags_a_pending_refund(self):
        unit = fd.create_partial_payment_unit(
            "CANCEL-REFUND-PENDING", customer_name="Pytest Refund Pending Customer",
            amount_received=75.0, amount_outstanding=50.0,
        )
        child = spawn_app()
        try:
            _enter_cancel_sale(child, unit["SKU"])
            child.expect("Reason for cancelling")
            child.sendline("pytest refund-pending cancellation")
            child.expect("Was the partial payment of \\$75.00 refunded to the customer\\?")
            child.sendline("no")

            text = expect_clean(child, "Cancel this sale and restore the unit to Available\\?")
            assert "$75.00 refund pending." in text
            child.sendline("yes")
            expect_clean(child, "restored to Available")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Available"
        assert "· REFUND]" in updated["Inventory Notes"]
        assert "Pytest Refund Pending Customer" in updated["Inventory Notes"]
        assert "not yet refunded" in updated["Transaction Notes"]


class TestPendingRefundSweep:
    """The refund flagged by TestPartialPaymentRefund's decline case above
    must resurface at the top of the *next* cancel_sale() call and be
    resolvable there, independent of a second sale's cancellation."""

    def test_sweep_surfaces_and_resolves_a_pending_refund(self):
        flagged_unit = fd.create_partial_payment_unit(
            "CANCEL-SWEEP-SOURCE", customer_name="Pytest Sweep Customer",
            amount_received=40.0, amount_outstanding=60.0,
        )
        setup_child = spawn_app()
        try:
            _enter_cancel_sale(setup_child, flagged_unit["SKU"])
            setup_child.expect("Reason for cancelling")
            setup_child.sendline("pytest sweep-source cancellation")
            setup_child.expect("Was the partial payment of \\$40.00 refunded to the customer\\?")
            setup_child.sendline("no")
            setup_child.expect("Cancel this sale and restore the unit to Available\\?")
            setup_child.sendline("yes")
            expect_clean(setup_child, "restored to Available")
        finally:
            close(setup_child)

        # A second, unrelated unit -- just needs to exist so the sweep's
        # own "SELECT SALE TO CANCEL" step has a real SKU to decline on.
        other_unit = fd.create_sold_unit("CANCEL-SWEEP-BYSTANDER")

        child = spawn_app()
        try:
            child.sendline("8")
            text = expect_clean(child, "Mark a refund as issued\\?")
            assert "PENDING REFUNDS" in text
            assert flagged_unit["SKU"] in text
            child.sendline("yes")

            # More than one pending refund can accumulate across runs since
            # nothing here cleans up test data -- handle both the
            # single-match (auto-selected) and multi-match (asks which SKU)
            # cases rather than assuming a count.
            index = child.expect(["SKU to clear:", "confirm as issued\\?"])
            if index == 0:
                child.sendline(flagged_unit["SKU"])
                child.expect("confirm as issued\\?")
            child.sendline("yes")
            text = expect_clean(child, "SELECT SALE TO CANCEL")
            assert "refund of $40.00" in text.lower() or "marked as issued" in text.lower()

            # Decline the follow-on cancellation step -- already covered by
            # TestHappyPath, not the point of this test.
            child.sendline(other_unit["SKU"])
            child.expect("Is this the correct unit\\?")
            child.sendline("yes")
            child.expect("Reason for cancelling")
            child.sendline("pytest sweep-bystander decline")
            child.expect("Cancel this sale and restore the unit to Available\\?")
            child.sendline("no")
            expect_clean(child, "Returning to Main Menu")
        finally:
            close(child)

        cleared = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(flagged_unit["SKU"]))
        assert "· REFUND]" not in cleared["Inventory Notes"]
        assert "REFUND-CLEARED" in cleared["Transaction Notes"]

        unchanged_bystander = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(other_unit["SKU"]))
        assert unchanged_bystander["Status"] == "Sold"


class TestStatusRejection:
    def test_available_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("CANCEL-AVAILABLE-REJECT")
        child = spawn_app()
        try:
            _open_cancel_sale(child)
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "Only units recorded as Sold or Sold - Partial Payment can have a sale cancelled" in text
        finally:
            close(child)


class TestBlankCostWarning:
    def test_blank_total_cost_unit_shows_warning(self):
        unit = fd.create_sold_unit_blank_total_cost("CANCEL-BLANK-COST")
        child = spawn_app()
        try:
            _open_cancel_sale(child)
            child.sendline(unit["SKU"])
            text = expect_clean(child, "Is this the correct unit\\?")
            assert "blank or unreadable" in text
        finally:
            close(child)


class TestCancellation:
    def test_declining_final_confirmation_writes_nothing(self):
        unit = fd.create_sold_unit("CANCEL-DECLINE")
        child = spawn_app()
        try:
            _enter_cancel_sale(child, unit["SKU"])
            child.expect("Reason for cancelling")
            child.sendline("pytest decline reason")
            child.expect("Cancel this sale and restore the unit to Available\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Cancellation discarded" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Status"] == "Sold"
