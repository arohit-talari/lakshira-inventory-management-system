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
    child.expect("How many units are part of this sale")
    child.sendline("")  # default 1
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
    child.expect("Customer Search")
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
            child.expect("Payment Status:")
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
            child.expect("Payment Status:")
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
            child.expect("Discount Percentage")
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
            child.expect("Discount Percentage")
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
            child.expect("How many units are part of this sale")
            child.sendline("")  # default 1
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
            child.expect("How many units are part of this sale")
            child.sendline("")  # default 1
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
            child.expect("How many units are part of this sale")
            child.sendline("")  # default 1
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
            child.expect("Payment Status:")
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


def _start_bulk_sale(child, batch_size):
    child.sendline("6")
    child.expect("How many units are part of this sale")
    child.sendline(str(batch_size))
    child.expect("Sales Channel:")
    child.send(ENTER)  # Exhibition/Popup (default)


def _enter_bulk_unit(child, sku, index, batch_size, skip=False):
    """Drives one pass of _record_bulk_sale()'s per-unit loop: SKU entry,
    the bulk-specific 'Is this the correct unit?' three-way select, no
    discount, and 'Add this unit to the batch?'. skip=True exits via the
    'Skip this unit' choice instead."""
    expect_clean(child, f"SALE ENTRY \\({index} of {batch_size}\\)")
    child.expect("SKU:")
    child.sendline(sku)
    expect_clean(child, "Is this the correct unit\\?")
    if skip:
        child.send(DOWN)
        child.send(DOWN)
        child.send(ENTER)  # "Skip this unit"
        return
    child.send(ENTER)  # "Yes, use this unit"
    child.expect(f"Was a discount applied to {sku}\\?")
    child.sendline("no")
    child.expect("Add this unit to the batch\\?")
    child.sendline("yes")


class TestBulkSaleHappyPath:
    def test_two_units_paid_in_full(self):
        unit_a = fd.create_fresh_available_unit("BULK-SALE-FULL-A")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-FULL-B")
        total_price = round(inv._sheet_float(unit_a["Selling Price (USD)"])
                             + inv._sheet_float(unit_b["Selling Price (USD)"]), 2)

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            customer_name = _new_customer_flow(child, "Bulk Buyer")
            _enter_bulk_unit(child, unit_a["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_b["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Write this sale \\(2 units\\) to the master sheet\\?")
            assert "BULK SALE SUMMARY" in text
            assert unit_a["SKU"] in text
            assert unit_b["SKU"] in text
            child.sendline("yes")
            text = expect_clean(child, "now has")  # follows the success line, so the
            # buffer by this point already contains the full "sold to ...: SKU, SKU" line
            assert unit_a["SKU"] in text and unit_b["SKU"] in text
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert row_a["Status"] == "Sold"
        assert row_b["Status"] == "Sold"
        assert row_a["Customer Name"] == customer_name
        assert row_b["Customer Name"] == customer_name
        assert row_a["Date Sold"] == TODAY_STR
        assert row_a["Amount Outstanding (USD)"] in ("", "0", 0)
        assert row_b["Amount Outstanding (USD)"] in ("", "0", 0)

    def test_uneven_prices_split_proportionally_not_evenly(self):
        # The exact scenario that drove this design: a cheap unit and a
        # pricier one in the same bulk sale. An even split would overpay
        # the cheap unit relative to its price; proportional allocation
        # must give each unit a share matching its own price ratio.
        unit_cheap = fd.create_fresh_available_unit("BULK-SALE-UNEVEN-CHEAP")
        unit_pricey = fd.create_fresh_available_unit("BULK-SALE-UNEVEN-PRICEY")
        cheap_price = 100.0
        pricey_price = 300.0
        inv.update_row(inv.find_row_index_by_sku(unit_cheap["SKU"]), {
            "Selling Price (USD)": cheap_price,
        })
        inv.update_row(inv.find_row_index_by_sku(unit_pricey["SKU"]), {
            "Selling Price (USD)": pricey_price,
        })
        total_price = cheap_price + pricey_price
        payment = 200.0  # 50% of the batch total

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Uneven Buyer")
            _enter_bulk_unit(child, unit_cheap["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_pricey["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("no")
            child.expect("Total Amount Received")
            child.sendline(str(payment))
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Write this sale \\(2 units\\) to the master sheet\\?")
            assert "Sold - Partial Payment" in text
            child.sendline("yes")
            expect_clean(child, "sold to")
        finally:
            close(child)

        row_cheap = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_cheap["SKU"]))
        row_pricey = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_pricey["SKU"]))
        # 50% of the total paid -> each unit should carry exactly 50% of its
        # own price as outstanding, not an equal dollar split (which would
        # have left the cheap unit at $0 outstanding and the pricey one at
        # $200 -- the exact failure mode this design replaced).
        assert inv._sheet_float(row_cheap["Amount Received (USD)"]) == round(cheap_price * 0.5, 2)
        assert inv._sheet_float(row_pricey["Amount Received (USD)"]) == round(pricey_price * 0.5, 2)
        assert row_cheap["Status"] == "Sold - Partial Payment"
        assert row_pricey["Status"] == "Sold - Partial Payment"


class TestBulkSaleSkipAndCancel:
    def test_skipping_a_unit_writes_only_the_rest(self):
        unit_keep = fd.create_fresh_available_unit("BULK-SALE-SKIP-KEEP")
        unit_skip = fd.create_fresh_available_unit("BULK-SALE-SKIP-DROP")

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Skip Buyer")
            _enter_bulk_unit(child, unit_keep["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_skip["SKU"], 2, 2, skip=True)
            text = expect_clean(child, "Date Sold")
            assert "skipped" in text.lower()
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Write this sale \\(1 unit\\) to the master sheet\\?")
            assert unit_skip["SKU"] not in text
            child.sendline("yes")
            expect_clean(child, "sold to")
        finally:
            close(child)

        row_keep = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_keep["SKU"]))
        row_skip = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_skip["SKU"]))
        assert row_keep["Status"] == "Sold"
        assert row_skip["Status"] == "Available"  # untouched

    def test_skipping_every_unit_writes_nothing(self):
        unit_a = fd.create_fresh_available_unit("BULK-SALE-ALLSKIP-A")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-ALLSKIP-B")

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "All Skip Buyer")
            _enter_bulk_unit(child, unit_a["SKU"], 1, 2, skip=True)
            _enter_bulk_unit(child, unit_b["SKU"], 2, 2, skip=True)
            text = expect_clean(child, "Returning to Main Menu")
            assert "No units were entered" in text
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert row_a["Status"] == "Available"
        assert row_b["Status"] == "Available"

    def test_declining_final_confirmation_writes_nothing(self):
        unit_a = fd.create_fresh_available_unit("BULK-SALE-DECLINE-A")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-DECLINE-B")

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Bulk Decliner")
            _enter_bulk_unit(child, unit_a["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_b["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale \\(2 units\\) to the master sheet\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Bulk sale cancelled" in text
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert row_a["Status"] == "Available"
        assert row_b["Status"] == "Available"
