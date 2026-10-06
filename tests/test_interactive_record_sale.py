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
import os
import signal
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


class TestSingleUnitPreWriteConflict:
    """TC-353: the single-unit path's own write-time status/price-cost
    drift hard-abort -- pre-existing logic (lines ~4232-4249), unchanged
    by the Op 6 bulk build, but never had its own Tier 2 test; only the
    bulk extraction (TestBulkSalePreWriteConflicts) was covered."""

    def test_status_conflict_aborts_write(self):
        unit = fd.create_fresh_available_unit("SALE-STATUSCONF-SINGLE")
        child = spawn_app(timeout=45)
        try:
            _enter_record_sale(child, unit["SKU"])
            _new_customer_flow(child, "Single Status Conflict Buyer")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("no")
            child.expect("Payment Status:")
            child.send(ENTER)  # Paid in full
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale to the master sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit["SKU"]), {"Status": "Reserved"})
            child.sendline("yes")

            # Single-unit's own warning never names the SKU (only one unit
            # is in play, unlike bulk's per-unit message) -- match past the
            # "(no longer '...')" clause so it's actually captured in text.
            text = expect_clean(child, "Please re-check the SKU")
            assert "no longer 'Available'" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Status"] == "Reserved"  # the conflicting write, untouched by this aborted sale

    def test_price_drift_aborts_write(self):
        unit = fd.create_fresh_available_unit("SALE-PRICEDRIFT-SINGLE")
        child = spawn_app(timeout=45)
        try:
            _enter_record_sale(child, unit["SKU"])
            _new_customer_flow(child, "Single Price Drift Buyer")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("no")
            child.expect("Payment Status:")
            child.send(ENTER)  # Paid in full
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale to the master sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit["SKU"]), {"Selling Price (USD)": 9999.0})
            child.sendline("yes")

            expect_clean(child, "has changed since you started")
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Status"] == "Available"  # nothing written, sale never landed


class TestSingleUnitReservedSale:
    """TC-354: selling a Reserved unit through the single-unit path clears
    its Reserved Date and reservation note at write time. Pre-existing
    logic, unchanged by the Op 6 bulk build (only the bulk mixed-status
    case, TestBulkSaleMixedStatus, had automated coverage before this)."""

    def test_reserved_unit_sale_clears_reservation(self):
        unit = fd.create_fresh_available_unit("SALE-RESERVED-SINGLE", status="Reserved")
        reserved_date = TODAY_STR
        inv.update_row(inv.find_row_index_by_sku(unit["SKU"]), {
            "Reserved Date": reserved_date,
            "Inventory Notes": f"{unit['Inventory Notes']}\n[{reserved_date} · RESERVATION] Reserved by Test Reserver",
        })

        child = spawn_app(timeout=45)
        try:
            child.sendline("6")
            child.expect("How many units are part of this sale")
            child.sendline("")  # default 1
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "Is this the correct unit\\?")
            assert "RESERVATION" in text
            assert "Test Reserver" in text
            child.sendline("yes")
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("Sales Channel:")
            child.send(ENTER)
            _new_customer_flow(child, "Reserved Unit Buyer")
            child.expect("Was a discount applied to this sale\\?", timeout=20)
            child.sendline("no")
            child.expect("Payment Status:")
            child.send(ENTER)  # Paid in full
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale to the master sheet\\?")
            child.sendline("yes")
            expect_clean(child, "sold to")
        finally:
            close(child)

        row = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert row["Status"] == "Sold"
        assert row["Reserved Date"] == ""
        assert "· RESERVATION]" not in row["Inventory Notes"]


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


class TestBulkSaleSkuReentry:
    """TC-357: 'No, try a different SKU' re-prompts without dropping the unit."""

    def test_no_try_a_different_sku_reprompts(self):
        unit_decoy = fd.create_fresh_available_unit("BULK-SALE-REENTRY-DECOY")
        unit_real = fd.create_fresh_available_unit("BULK-SALE-REENTRY-REAL")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-REENTRY-B")

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Reentry Buyer")
            expect_clean(child, "SALE ENTRY \\(1 of 2\\)")
            child.expect("SKU:")
            child.sendline(unit_decoy["SKU"])
            expect_clean(child, "Is this the correct unit\\?")
            child.send(DOWN)
            child.send(ENTER)  # "No, try a different SKU"
            child.expect("SKU:")
            child.sendline(unit_real["SKU"])
            expect_clean(child, "Is this the correct unit\\?")
            child.send(ENTER)  # "Yes, use this unit"
            child.expect(f"Was a discount applied to {unit_real['SKU']}\\?")
            child.sendline("no")
            child.expect("Add this unit to the batch\\?")
            child.sendline("yes")
            _enter_bulk_unit(child, unit_b["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            text = expect_clean(child, "Write this sale \\(2 units\\) to the master sheet\\?")
            assert unit_real["SKU"] in text and unit_b["SKU"] in text
            assert unit_decoy["SKU"] not in text
            child.sendline("no")
        finally:
            close(child)


class TestBulkSaleMarginAlert:
    """TC-358: the margin-alert discard option is relabeled 'Skip this unit'
    inside a bulk batch and drops only that unit."""

    def test_margin_alert_skip_this_unit_drops_only_that_unit(self):
        unit_problem = fd.get_or_create_below_cost_unit()
        unit_fine = fd.create_fresh_available_unit("BULK-SALE-MARGIN-FINE")

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Margin Alert Buyer")
            expect_clean(child, "SALE ENTRY \\(1 of 2\\)")
            child.expect("SKU:")
            child.sendline(unit_problem["SKU"])
            expect_clean(child, "Is this the correct unit\\?")
            child.send(ENTER)  # "Yes, use this unit"
            child.expect(f"Was a discount applied to {unit_problem['SKU']}\\?")
            child.sendline("yes")
            child.expect("Discount Percentage")
            child.sendline("10")
            text = expect_clean(child, "How would you like to proceed\\?")
            assert "BELOW COST" in text
            # Choices: Re-enter / Remove discount / Proceed with current / Skip this unit
            # (the choice labels themselves render after this qmark match, so
            # the "Skip this unit" vs. single-unit's "Discard & exit" wording
            # is confirmed behaviorally below by the unit actually being
            # dropped, not asserted on captured text here)
            child.send(DOWN)
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # "Skip this unit"
            _enter_bulk_unit(child, unit_fine["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            text = expect_clean(child, "Write this sale \\(1 unit\\) to the master sheet\\?")
            assert unit_problem["SKU"] not in text
            assert unit_fine["SKU"] in text
            child.sendline("no")
        finally:
            close(child)


class TestBulkSaleDeclineAddToBatch:
    """TC-359: declining 'Add this unit to the batch?' is its own skip path,
    distinct from skipping at the SKU-confirmation stage."""

    def test_declining_add_to_batch_skips_that_unit_only(self):
        unit_declined = fd.create_fresh_available_unit("BULK-SALE-DECLINEADD")
        unit_kept = fd.create_fresh_available_unit("BULK-SALE-DECLINEADD-KEEP")

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Decline Add Buyer")
            expect_clean(child, "SALE ENTRY \\(1 of 2\\)")
            child.expect("SKU:")
            child.sendline(unit_declined["SKU"])
            expect_clean(child, "Is this the correct unit\\?")
            child.send(ENTER)
            child.expect(f"Was a discount applied to {unit_declined['SKU']}\\?")
            child.sendline("no")
            child.expect("Add this unit to the batch\\?")
            child.sendline("no")
            _enter_bulk_unit(child, unit_kept["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            text = expect_clean(child, "Write this sale \\(1 unit\\) to the master sheet\\?")
            assert unit_declined["SKU"] not in text
            child.sendline("no")
        finally:
            close(child)


class TestBulkSaleMixedStatus:
    """TC-361: a bulk batch can mix Available and Reserved units; the
    Reserved unit's reservation gets cleared correctly at write time."""

    def test_mixed_available_and_reserved_units(self):
        unit_available = fd.create_fresh_available_unit("BULK-SALE-MIXED-AVAIL")
        unit_reserved = fd.create_fresh_available_unit("BULK-SALE-MIXED-RESV", status="Reserved")
        reserved_date = TODAY_STR
        inv.update_row(inv.find_row_index_by_sku(unit_reserved["SKU"]), {
            "Reserved Date": reserved_date,
            "Inventory Notes": f"{unit_reserved['Inventory Notes']}\n[{reserved_date} · RESERVATION] Reserved by Test Reserver",
        })

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Mixed Status Buyer")
            _enter_bulk_unit(child, unit_available["SKU"], 1, 2)
            expect_clean(child, "SALE ENTRY \\(2 of 2\\)")
            child.expect("SKU:")
            child.sendline(unit_reserved["SKU"])
            text = expect_clean(child, "Is this the correct unit\\?")
            assert "RESERVATION" in text
            assert "Test Reserver" in text
            child.send(ENTER)
            child.expect(f"Was a discount applied to {unit_reserved['SKU']}\\?")
            child.sendline("no")
            child.expect("Add this unit to the batch\\?")
            child.sendline("yes")
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale \\(2 units\\) to the master sheet\\?")
            child.sendline("yes")
            expect_clean(child, "now has")
        finally:
            close(child)

        row_reserved = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_reserved["SKU"]))
        assert row_reserved["Status"] == "Sold"
        assert row_reserved["Reserved Date"] == ""
        assert "· RESERVATION]" not in row_reserved["Inventory Notes"]


class TestBulkSaleDateSoldValidation:
    """TC-362: Date Sold must be on or after the LATEST Date Acquired in the
    batch, not just any single unit's."""

    def test_date_sold_validated_against_latest_date_acquired(self):
        unit_old = fd.create_fresh_available_unit("BULK-SALE-DATE-OLD")
        unit_new = fd.create_fresh_available_unit("BULK-SALE-DATE-NEW")
        inv.update_row(inv.find_row_index_by_sku(unit_new["SKU"]), {"Date Acquired": "09-01-2026"})

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Date Validation Buyer")
            _enter_bulk_unit(child, unit_old["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_new["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline("07-15-2026")  # after unit_old's acquisition, before unit_new's
            text = expect_clean(child, "09-01-2026")  # the batch's latest Date Acquired, named in the rejection
            assert "cannot be before the most recent Date Acquired in this batch" in text
            child.sendline("09-05-2026")
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            text = expect_clean(child, "Write this sale \\(2 units\\) to the master sheet\\?")
            assert "09-05-2026" in text
            child.sendline("no")
        finally:
            close(child)


class TestBulkSaleThreeUnitAllocation:
    """TC-364: proportional allocation with 3+ meaningfully different prices."""

    def test_three_units_meaningfully_different_prices(self):
        unit_a = fd.create_fresh_available_unit("BULK-SALE-3U-A")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-3U-B")
        unit_c = fd.create_fresh_available_unit("BULK-SALE-3U-C")
        prices = {unit_a["SKU"]: 100.0, unit_b["SKU"]: 250.0, unit_c["SKU"]: 400.0}
        for sku, price in prices.items():
            inv.update_row(inv.find_row_index_by_sku(sku), {"Selling Price (USD)": price})
        payment = 375.0  # 50% of the 750 total

        child = spawn_app(timeout=60)
        try:
            _start_bulk_sale(child, 3)
            _new_customer_flow(child, "Three Unit Buyer")
            _enter_bulk_unit(child, unit_a["SKU"], 1, 3)
            _enter_bulk_unit(child, unit_b["SKU"], 2, 3)
            _enter_bulk_unit(child, unit_c["SKU"], 3, 3)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("no")
            child.expect("Total Amount Received")
            child.sendline(str(payment))
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale \\(3 units\\) to the master sheet\\?")
            child.sendline("yes")
            expect_clean(child, "now has")
        finally:
            close(child)

        for sku, price in prices.items():
            row = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(sku))
            assert inv._sheet_float(row["Amount Received (USD)"]) == round(price * 0.5, 2)
            assert row["Status"] == "Sold - Partial Payment"


def _create_unit_retrying_sku_collision(label, status="Available"):
    """Calling create_fresh_available_unit() many times in a tight loop
    (the allocation-at-scale tests below) occasionally hits a genuine SKU
    collision -- _next_sku() reads the sheet fresh each call, but Op 1's
    own bulk intake needed a same-batch reserved-SKU set to avoid this
    exact race (see resolve_sku()'s batch_reserved_skus), and this fixture
    helper has no equivalent for rapid sequential calls outside a real
    batch. A short retry is a pragmatic, test-only workaround -- not a
    product bug, create_fresh_available_unit() itself is unchanged."""
    for attempt in range(5):
        try:
            return fd.create_fresh_available_unit(label, status=status)
        except ValueError as e:
            if "already exists in the sheet" not in str(e) or attempt == 4:
                raise
    raise AssertionError("unreachable")


class TestBulkSaleAllocationAtScale:
    """TC-365 (D-391): the largest-remainder allocation fix holds at
    realistic scale, with a small-price unit placed mid-batch, not last."""

    def test_allocation_holds_at_realistic_scale(self):
        n = 10
        units = [_create_unit_retrying_sku_collision(f"BULK-SALE-SCALE-{i}") for i in range(n)]
        prices = [round(50 + i * 23.37, 2) for i in range(n)]
        prices[n // 2] = 0.42  # small, mid-list, not last
        for u, price in zip(units, prices):
            inv.update_row(inv.find_row_index_by_sku(u["SKU"]), {"Selling Price (USD)": price})
        total_price = round(sum(prices), 2)
        payment = round(total_price - 0.07, 2)  # a few cents short of full payoff

        child = spawn_app(timeout=120)
        try:
            _start_bulk_sale(child, n)
            _new_customer_flow(child, "Scale Buyer")
            for i, u in enumerate(units, start=1):
                _enter_bulk_unit(child, u["SKU"], i, n)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("no")
            child.expect("Total Amount Received")
            child.sendline(str(payment))
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect(f"Write this sale \\({n} units\\) to the master sheet\\?", timeout=60)
            child.sendline("yes")
            expect_clean(child, "now has", timeout=60)
        finally:
            close(child)

        rows = [inv.get_row_by_sheet_index(inv.find_row_index_by_sku(u["SKU"])) for u in units]
        # Amount Received (USD) is correctly blank for any unit that became
        # fully "Sold" (the same convention record_sale()'s single-unit
        # path already uses) -- its effective received is the unit's own
        # price, not $0. Reading the column directly here would undercount
        # every Sold unit and look like a shortfall that was never real.
        effective_received = [
            inv._sheet_float(r["Selling Price (USD)"]) if r["Status"] == "Sold"
            else inv._sheet_float(r["Amount Received (USD)"])
            for r in rows
        ]
        total_received = round(sum(effective_received), 2)
        assert total_received == payment  # no silent shortfall
        for received, price in zip(effective_received, prices):
            assert received <= price + 1e-9  # no overflow


class TestBulkSalePreWriteConflicts:
    """TC-367/TC-368: a status or price/cost change on any one unit between
    confirmation and write aborts the WHOLE batch, nothing written."""

    def test_status_conflict_aborts_whole_batch(self):
        unit_a = fd.create_fresh_available_unit("BULK-SALE-STATUSCONF-A")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-STATUSCONF-B")
        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Status Conflict Buyer")
            _enter_bulk_unit(child, unit_a["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_b["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale \\(2 units\\) to the master sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {"Status": "Reserved"})
            child.sendline("yes")

            text = expect_clean(child, "status has changed since you started")
            assert unit_a["SKU"] in text
        finally:
            close(child)

        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert row_b["Status"] == "Available"  # whole batch aborted, b untouched too

    def test_price_drift_aborts_whole_batch(self):
        unit_a = fd.create_fresh_available_unit("BULK-SALE-PRICEDRIFT-A")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-PRICEDRIFT-B")
        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Price Drift Buyer")
            _enter_bulk_unit(child, unit_a["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_b["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale \\(2 units\\) to the master sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {"Selling Price (USD)": 9999.0})
            child.sendline("yes")

            text = expect_clean(child, "has changed since you started")
            assert unit_a["SKU"] in text
        finally:
            close(child)

        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert row_b["Status"] == "Available"


class TestHardKillMidBulkWrite:
    """TC-402/TC-400: a hard kill (SIGKILL, no Python exception ever
    raised) after confirming a bulk sale but before the write loop
    finishes must leave the batch-intent log behind, and the NEXT launch
    of either operation -- not just Op 6 again -- must surface it with
    the correct SKUs and amount. _write_batch_intent_log() runs as a
    local file write immediately after the final confirmation, before
    the write loop even starts (inventory.py L4676), so killing shortly
    after sending "yes" reliably catches it on disk regardless of
    exactly how many units' sheet writes land before the signal does --
    this test doesn't need to (and can't reliably) pin down an exact
    partial-write count."""

    def test_sigkill_after_confirm_surfaces_on_next_op7_launch(self):
        unit_a = fd.create_fresh_available_unit("BULK-SALE-KILL-A")
        unit_b = fd.create_fresh_available_unit("BULK-SALE-KILL-B")

        child = spawn_app(timeout=45)
        try:
            _start_bulk_sale(child, 2)
            _new_customer_flow(child, "Hard Kill Buyer")
            _enter_bulk_unit(child, unit_a["SKU"], 1, 2)
            _enter_bulk_unit(child, unit_b["SKU"], 2, 2)
            child.expect("Date Sold")
            child.sendline(TODAY_STR)
            child.expect("paid in full\\?")
            child.sendline("yes")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this sale \\(2 units\\) to the master sheet\\?")
            child.sendline("yes")
            # Sending "yes" doesn't go straight to the intent-log write --
            # the pre-write conflict check runs first. Poll for the log
            # file itself rather than guess a fixed sleep. Critically,
            # this must also actively drain the child's pty while it
            # waits: confirmed by direct measurement that polling
            # os.path.exists() alone, without ever reading from `child`,
            # left the child genuinely stuck (not slow) -- its own stdout
            # fills the pty's kernel buffer and the write() syscall blocks
            # until something reads the other end. Interleaving a
            # non-blocking read fixed it: the file appeared in well under
            # a second once the pty was actually being drained.
            _intent_log_path = inv._BULK_INTENT_LOG_PATH
            for _ in range(100):
                if os.path.exists(_intent_log_path):
                    break
                try:
                    child.read_nonblocking(size=4000, timeout=0.1)
                except Exception:
                    pass
            assert os.path.exists(_intent_log_path), "intent log never appeared -- kill would be meaningless"
            os.kill(child.pid, signal.SIGKILL)
            # isalive() doesn't reflect a SIGKILL instantly while the
            # child is mid-syscall -- poll rather than assume a single
            # fixed sleep is enough, draining here too for the same reason.
            for _ in range(20):
                if not child.isalive():
                    break
                try:
                    child.read_nonblocking(size=4000, timeout=0.1)
                except Exception:
                    pass
            assert not child.isalive()
        finally:
            close(child)

        verify = spawn_app(timeout=30)
        try:
            verify.sendline("7")  # Op 7, not Op 6 -- proves TC-400's
            # cross-operation claim in the same pass
            text = expect_clean(verify, "Acknowledge and clear this notice\\?")
            assert "UNFINISHED BULK RECORD_SALE_BULK DETECTED" in text
            assert unit_a["SKU"] in text
            assert unit_b["SKU"] in text
            verify.sendline("yes")  # clear it so it doesn't bleed into later tests
            expect_clean(verify, "How would you like to select a unit\\?")
        finally:
            close(verify)
