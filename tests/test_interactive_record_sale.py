"""
Tier 2 -- Record a Sale (Op 6), driven through a real pty.

This is the longest, most complex flow in the app and got the heaviest set
of fixes this session (stale Total Cost/Selling Price guard, blank-cost
warning, the discount-loop re-check restructure, the 100%-discount dead-end
fix). The tests below prioritize exercising those fixes specifically, not
exhaustive coverage of every customer-entry sub-path (country/phone/email/
city/state validation is its own large surface, shared with Manage
Reservation's new-customer flow and not re-tested field-by-field here).

Run: pytest tests/test_interactive_record_sale.py -v -s
"""
import itertools
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


def _enter_record_sale(child, sku):
    child.sendline("6")
    child.expect("SKU:")
    child.sendline(sku)
    child.expect("Is this the correct unit\\?")
    child.sendline("yes")
    child.expect("Date Sold")
    child.sendline(TODAY_STR)
    child.expect("Sales Channel:")
    child.send(ENTER)  # Exhibition/Popup (default)


_phone_counter = itertools.count()
_RUN_ID = int(time.time()) % 10_000  # unique per process run, not just per call --
                                       # a bare in-process counter would collide with
                                       # phone numbers left on the sheet by a *previous*
                                       # run, since nothing here cleans customers up


def _unique_phone():
    """Phone numbers are enforced-unique per customer -- every test that
    registers a new customer needs its own, or the app correctly (and
    otherwise unhelpfully, for a test run reusing one fake number) rejects
    the second one as a duplicate. 10 digits total (555 + 4-digit run id +
    3-digit counter), a valid-length fake US number."""
    return f"555{_RUN_ID:04d}{next(_phone_counter):03d}"


_DIGIT_TO_LETTER = str.maketrans("0123456789", "ABCDEFGHIJ")


def _unique_name(label):
    """A fixed name like "Pytest Buyer" collides with whatever a *previous*
    run already left on the sheet (confirmed while building this suite --
    a second run found "Pytest Buyer" as an existing customer and took a
    completely different code path, one this script wasn't written to
    handle). Needs real per-run uniqueness, but customer names are
    validated letters/spaces/hyphens/apostrophes/periods only -- a raw
    digit suffix gets silently rejected and re-prompted forever (looks
    exactly like a hang from the outside). Encoding _RUN_ID as letters
    (0->A .. 9->J) keeps it unique while staying valid."""
    suffix = str(_RUN_ID).translate(_DIGIT_TO_LETTER)
    return f"Pytest {label} {suffix}"


def _new_customer_flow(child, label, phone=None):
    """Drives the full enter_new_customer() sub-flow with a real (fake)
    phone/city/state -- shared by every test that needs a fresh customer."""
    phone = phone or _unique_phone()
    name = _unique_name(label)
    child.expect("Customer search")
    child.sendline(name)
    # Wording differs depending on whether the search found any partial
    # matches ("+ Register a new customer") or none ("+ Register as a new
    # customer") -- the unique name above should always mean zero matches,
    # but match both so this doesn't silently hang if that ever changes.
    child.expect("Register as a new customer|Register a new customer")
    child.send(ENTER)
    child.expect("Customer Name")
    child.sendline("")  # accept prefilled name
    # Fuzzy name matching can offer an existing customer here if this name
    # is close to one already on the sheet -- decline it so this always
    # proceeds as genuinely new.
    index = child.expect(["Proceed with an existing customer\\?", "search by country name"])
    if index == 0:
        child.sendline("no")
        child.expect("search by country name")
    child.sendline("United States")
    child.expect("Enter the number of your choice")
    child.sendline("1")
    child.expect("Customer Phone")
    child.sendline(phone)
    child.expect("Customer Email")
    child.sendline("")
    child.expect("Customer City")
    child.sendline("New York")
    child.expect("Customer State")
    child.sendline("New York")
    child.expect("Apply these changes\\?")
    child.send(ENTER)  # Confirm and apply changes
    return name


class TestHappyPath:
    def test_paid_in_full_writes_correct_sale_record(self):
        unit = fd.create_fresh_available_unit("SALE-PAID-FULL")
        total_cost_usd = inv._sheet_float(unit["Total Cost (USD)"])
        selling_price = inv._sheet_float(unit["Selling Price (USD)"])

        child = spawn_app(timeout=45)
        try:
            _enter_record_sale(child, unit["SKU"])
            customer_name = _new_customer_flow(child, "Buyer")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("no")
            child.expect("Payment status:")
            child.send(ENTER)  # Paid in full
            child.expect("Method of Payment:")
            child.send(ENTER)  # Cash

            text = expect_clean(child, "Write this sale to the master sheet\\?")
            assert "SALE SUMMARY" in text
            assert unit["SKU"] in text
            child.sendline("yes")
            text = expect_clean(child, "sold to")
            assert unit["SKU"] in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Sold"
        assert updated["Customer Name"] == customer_name
        assert inv._sheet_float(updated["Actual Selling Price (USD)"]) == selling_price
        assert inv._sheet_float(updated["Gross Profit (USD)"]) == round(selling_price - total_cost_usd, 2)
        assert updated["Amount Outstanding (USD)"] in ("", "0", 0)

    def test_partial_payment_sets_correct_status_and_outstanding(self):
        unit = fd.create_fresh_available_unit("SALE-PARTIAL")
        selling_price = inv._sheet_float(unit["Selling Price (USD)"])
        amount_paid = round(selling_price / 2, 2)

        child = spawn_app(timeout=45)
        try:
            _enter_record_sale(child, unit["SKU"])
            customer_name = _new_customer_flow(child, "Partial Payer")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("no")
            child.expect("Payment status:")
            child.send(DOWN)
            child.send(ENTER)  # Partial payment
            child.expect("Amount Received")
            child.sendline(str(amount_paid))
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Write this sale to the master sheet\\?")
            assert "Sold - Partial Payment" in text
            child.sendline("yes")
            expect_clean(child, "sold to")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Sold - Partial Payment"
        assert inv._sheet_float(updated["Amount Received (USD)"]) == amount_paid
        assert inv._sheet_float(updated["Amount Outstanding (USD)"]) == round(selling_price - amount_paid, 2)


class TestDiscountRemovalRecheck:
    """Pins this session's Finding 3 fix: picking 'Remove discount and
    proceed at full price' must re-check whether the *full* price is
    itself below cost / low-margin, not silently exit the loop. The
    below-cost fixture's full price is already below its own cost, so
    removing the discount must still show a warning (previously it
    wouldn't have, and the operator could complete a below-cost sale
    without ever being told)."""

    def test_removing_discount_on_an_already_below_cost_unit_still_warns(self):
        unit = fd.get_or_create_below_cost_unit()
        child = spawn_app(timeout=45)
        try:
            _enter_record_sale(child, unit["SKU"])
            customer_name = _new_customer_flow(child, "Discount Remover")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("yes")
            child.expect("Discount percentage")
            child.sendline("10")

            text = expect_clean(child, "How would you like to proceed\\?")
            assert "BELOW COST" in text
            assert "This discount puts" in text
            child.send(DOWN)
            child.send(ENTER)  # "Remove discount and proceed at full price"

            # Full price is ALSO below cost -- must re-warn, using wording
            # that doesn't falsely say "this discount" since there isn't one.
            # Not "How would you like to proceed?" -- that's the select()
            # prompt's own title, printed at the top of *every* iteration of
            # this menu, so it matches too early (before the new warning
            # text) the same way it did on the very first iteration too.
            text = expect_clean(child, "This unit's full price \\(no discount\\) puts")
            assert "BELOW COST" in text

            # Only two choices should remain now -- no "Remove discount"
            # option left to offer, since there's nothing left to remove.
            text = expect_clean(child, "Discard & exit")
            assert "Remove discount" not in text
        finally:
            close(child)


class TestFullDiscountPaymentFix:
    """Pins Finding 4: a 100% discount must not offer 'Partial payment' --
    that combination is an inescapable prompt (amount must be > 0 but
    can't exceed a $0 full price)."""

    def test_hundred_percent_discount_skips_payment_status_prompt(self):
        unit = fd.create_fresh_available_unit("SALE-100PCT-DISCOUNT")
        child = spawn_app(timeout=45)
        try:
            _enter_record_sale(child, unit["SKU"])
            customer_name = _new_customer_flow(child, "Comp Recipient")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("yes")
            child.expect("Discount percentage")
            child.sendline("100")

            text = expect_clean(child, "How would you like to proceed\\?")
            assert "BELOW COST" in text
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # "Proceed with current discount"

            # Payment status prompt must NOT appear -- straight to payment method.
            text = expect_clean(child, "Method of Payment:")
            assert "Payment status" not in text

            child.send(ENTER)
            text = expect_clean(child, "Write this sale to the master sheet\\?")
            assert "$0.00" in text
        finally:
            close(child)


class TestStatusRejection:
    def test_unassigned_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("SALE-UNASSIGNED-REJECT", status="Unassigned")
        child = spawn_app()
        try:
            child.sendline("6")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "cannot be sold" in text
        finally:
            close(child)

    def test_already_sold_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("SALE-ALREADY-SOLD-REJECT", status="Sold")
        child = spawn_app()
        try:
            child.sendline("6")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "already been recorded" in text
        finally:
            close(child)


class TestBlankCostWarning:
    def test_blank_cost_unit_shows_warning(self):
        unit = fd.get_or_create_blank_cost_unit()
        child = spawn_app()
        try:
            child.sendline("6")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "Is this the correct unit\\?")
            assert "blank or unreadable" in text
        finally:
            close(child)


class TestCancellation:
    def test_declining_final_confirmation_writes_nothing(self):
        unit = fd.create_fresh_available_unit("SALE-DECLINE")
        child = spawn_app(timeout=45)
        try:
            _enter_record_sale(child, unit["SKU"])
            customer_name = _new_customer_flow(child, "Sale Decliner")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("no")
            child.expect("Payment status:")
            child.send(ENTER)
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale to the master sheet\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Sale cancelled" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Status"] == "Available"
