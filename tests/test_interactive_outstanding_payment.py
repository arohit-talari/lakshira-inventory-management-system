"""
Tier 2 -- Record Outstanding Payment (Op 7), driven through a real pty.

Directly exercises this session's highest-severity finding: six raw
float(x or 0) call sites that crash on any comma-formatted balance >=
$1,000 (Python's float() can't parse "1,250.00", unlike the app's own
_sheet_float() helper). One test below uses a four-figure outstanding
balance specifically to confirm the fix holds against the real listing
and transaction-summary screens, not just the isolated _sheet_float() unit
tests already covered in Tier 1.

Run: pytest tests/test_interactive_outstanding_payment.py -v -s
"""
import time
from datetime import date

import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

TODAY_STR = date.today().strftime("%m-%d-%Y")


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _enter_by_sku(child, sku):
    child.sendline("7")
    child.expect("How would you like to select a unit\\?")
    child.send(ENTER)  # Enter by SKU
    child.expect("Search SKU")
    child.sendline(sku)


class TestPartialPayment:
    def test_payment_less_than_outstanding_stays_partial(self):
        unit = fd.create_partial_payment_unit("OUT-STILL-PARTIAL", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app()
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment date")
            child.sendline(TODAY_STR)
            child.expect("Payment received")
            child.sendline("50")
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Confirm and write to sheet\\?")
            assert "PAYMENT SUMMARY" in text
            assert "Sold - Partial Payment" in text
            assert "$64.18" in text  # 114.18 - 50
            child.sendline("yes")
            text = expect_clean(child, "still outstanding")
            assert "$64.18" in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Sold - Partial Payment"
        assert inv._sheet_float(updated["Amount Received (USD)"]) == 150.0
        assert inv._sheet_float(updated["Amount Outstanding (USD)"]) == 64.18

    def test_payment_equal_to_outstanding_fully_settles(self):
        unit = fd.create_partial_payment_unit("OUT-FULL-SETTLE", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app()
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment date")
            child.sendline(TODAY_STR)
            child.expect("Payment received")
            child.sendline("114.18")
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Confirm and write to sheet\\?")
            assert "PAYMENT SUMMARY" in text
            assert "Sold - Partial Payment" not in text
            assert "Amount Outstanding:   $0.00" in text
            child.sendline("yes")
            text = expect_clean(child, "fully settled")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Sold"
        # Matches record_sale()'s own convention: once fully settled, these
        # columns go blank rather than showing a stale/zeroed value -- only
        # meaningful for a unit still in "Sold - Partial Payment".
        assert updated["Amount Received (USD)"] == ""
        assert updated["Amount Outstanding (USD)"] == ""

    def test_overpayment_is_rejected(self):
        unit = fd.create_partial_payment_unit("OUT-OVERPAY-REJECT", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app()
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment date")
            child.sendline(TODAY_STR)
            child.expect("Payment received")
            child.sendline("200")
            text = expect_clean(child, "Payment received")  # re-prompted
            assert "exceeds the outstanding balance" in text
        finally:
            close(child)


class TestFourFigureBalanceDoesNotCrash:
    """Pins this session's fix: a comma-formatted balance >= $1,000 used to
    crash this entire screen via raw float(x or 0) -- confirmed reproducible
    before the fix, at the very first listing loop, before an operator could
    even select a unit."""

    def test_listing_and_transaction_summary_handle_four_figure_balance(self):
        unit = fd.create_partial_payment_unit("OUT-FOUR-FIGURE", amount_received=500.0,
                                               amount_outstanding=1250.75)
        child = spawn_app()
        try:
            child.sendline("7")
            text = expect_clean(child, "How would you like to select a unit\\?")
            assert "CURRENT OUTSTANDING PAYMENTS" in text
            assert "$1,250.75" in text  # would have thrown before the fix

            child.send(ENTER)
            child.expect("Search SKU")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "Payment date")
            assert "$1,250.75" in text
        finally:
            close(child)


class TestCancellation:
    def test_declining_final_confirmation_writes_nothing(self):
        unit = fd.create_partial_payment_unit("OUT-DECLINE", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app()
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment date")
            child.sendline(TODAY_STR)
            child.expect("Payment received")
            child.sendline("50")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Confirm and write to sheet\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Payment cancelled" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert inv._sheet_float(unchanged["Amount Received (USD)"]) == 100.0
        assert inv._sheet_float(unchanged["Amount Outstanding (USD)"]) == 114.18


class TestFilterByCustomer:
    def test_selecting_via_customer_filter(self):
        # A fixed name here would collide with every customer this same
        # test created on every *previous* run -- nothing cleans up test
        # data, and Filter-by-customer groups by identity (phone), not
        # name, so those past runs (now each with a distinct phone since
        # the fixture's own uniqueness fix) show up as separate customers
        # who all happen to share this literal name. Two-plus matches
        # makes the app show a "Select a customer:" picker instead of
        # jumping straight to "Payment date" -- this test only handles a
        # single, unambiguous match, so the name needs real per-run
        # uniqueness the same way phone numbers already have it.
        customer_name = f"Pytest Filterable Customer {int(time.time())}"
        unit = fd.create_partial_payment_unit(
            "OUT-CUSTOMER-FILTER", customer_name=customer_name,
            amount_received=75.0, amount_outstanding=50.0,
        )
        child = spawn_app()
        try:
            child.sendline("7")
            child.expect("How would you like to select a unit\\?")
            child.send(DOWN)
            child.send(ENTER)  # "Filter by customer"
            child.expect("Customer search")
            child.sendline(customer_name)
            text = expect_clean(child, "Payment date")
            assert unit["SKU"] in text
        finally:
            close(child)
