"""
Tier 2 -- Record an Outstanding Payment (Op 7), driven through a real pty.

Directly exercises this session's highest-severity finding: six raw
float(x or 0) call sites that crash on any comma-formatted balance >=
$1,000 (Python's float() can't parse "1,250.00", unlike the app's own
_sheet_float() helper). One test below uses a four-figure outstanding
balance specifically to confirm the fix holds against the real listing
and transaction-summary screens, not just the isolated _sheet_float() unit
tests already covered in Tier 1.

Run: pytest tests/test_interactive_outstanding_payment.py -v -s
"""
import os
import signal
import time
from datetime import date

import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, SPACE, close, expect_clean, require_test_mode, spawn_app

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
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
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
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
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
        # D-137/D-395 (2026-10-05): full settlement must write the final
        # values, not blank them -- the prior "blank on Sold" behavior
        # silently erased a real, accumulated Amount Received that exists
        # nowhere else in the row (unlike record_sale()'s immediate-full-
        # payment case, where these columns are never written at all
        # because there's no distinct payment history to begin with --
        # "Actual Selling Price (USD)" already covers it). No design
        # decision anywhere documents a deliberate reversal of D-137; the
        # only record on file (D-137 itself) requires final values.
        assert inv._sheet_float(updated["Amount Received (USD)"]) == 214.18  # 100 (prior) + 114.18 (this payment)
        assert inv._sheet_float(updated["Amount Outstanding (USD)"]) == 0.0

    def test_overpayment_is_rejected(self):
        unit = fd.create_partial_payment_unit("OUT-OVERPAY-REJECT", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app()
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
            child.sendline("200")
            text = expect_clean(child, "Payment Received")  # re-prompted
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
            text = expect_clean(child, "Payment Date")
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
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
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
        # jumping straight to "Payment Date" -- this test only handles a
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
            child.expect("Customer Search")
            child.sendline(customer_name)
            text = expect_clean(child, "Payment Date")
            assert unit["SKU"] in text
        finally:
            close(child)


def _create_partial_payment_unit_retrying_sku_collision(label, **kwargs):
    """Same rationale as test_interactive_record_sale.py's
    _create_unit_retrying_sku_collision() -- rapid sequential fixture
    creation (the allocation-at-scale test below) occasionally hits a
    genuine SKU collision against a leftover row from a previous run.
    Test-only workaround, not a product bug."""
    for attempt in range(5):
        try:
            return fd.create_partial_payment_unit(label, **kwargs)
        except ValueError as e:
            if "already exists in the sheet" not in str(e) or attempt == 4:
                raise
    raise AssertionError("unreachable")


def _enter_bulk_by_customer(child, customer_name, count):
    """Navigates to Op 7, filters by customer, and checks the first `count`
    units in the resulting checkbox list (space to toggle, arrow down to
    move, enter to confirm) -- see docs/Bulk_Payment_Scoping_Op6_Op7.md §8.1."""
    child.sendline("7")
    child.expect("How would you like to select a unit\\?")
    child.send(DOWN)
    child.send(ENTER)  # "Filter by customer"
    child.expect("Customer Search")
    child.sendline(customer_name)
    expect_clean(child, f"Outstanding units for {customer_name}")
    for i in range(count):
        child.send(SPACE)
        if i < count - 1:
            child.send(DOWN)
    child.send(ENTER)


class TestBulkPayment:
    def test_two_units_proportional_split_not_even(self):
        # Mirrors Op 6's identical allocation flaw scenario: a payment that
        # covers less than the full batch must give each unit a share
        # matching its own outstanding balance's ratio, not an equal split.
        customer_name = f"Pytest Bulk Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_cheap = fd.create_partial_payment_unit(
            "OUT-BULK-CHEAP", customer_name=customer_name, phone=phone,
            amount_received=50.0, amount_outstanding=100.0,
        )
        unit_pricey = fd.create_partial_payment_unit(
            "OUT-BULK-PRICEY", customer_name=customer_name, phone=phone,
            amount_received=50.0, amount_outstanding=300.0,
        )
        payment = 200.0  # 50% of the combined $400 outstanding

        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            expect_clean(child, "UNITS SELECTED \\(2\\)")
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline(str(payment))
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Write this payment \\(2 units\\) to the master sheet\\?")
            assert "BULK PAYMENT SUMMARY" in text
            assert "Sold - Partial Payment" in text
            child.sendline("yes")
            expect_clean(child, "recorded across 2")
        finally:
            close(child)

        row_cheap = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_cheap["SKU"]))
        row_pricey = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_pricey["SKU"]))
        # 50% of the total paid -> each unit carries exactly 50% of its own
        # original outstanding balance afterward, not an equal dollar split.
        assert inv._sheet_float(row_cheap["Amount Outstanding (USD)"]) == 50.0
        assert inv._sheet_float(row_pricey["Amount Outstanding (USD)"]) == 150.0
        assert row_cheap["Status"] == "Sold - Partial Payment"
        assert row_pricey["Status"] == "Sold - Partial Payment"

    def test_full_payoff_clears_every_unit(self):
        customer_name = f"Pytest Bulk Full Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit(
            "OUT-BULK-FULL-A", customer_name=customer_name, phone=phone,
            amount_received=50.0, amount_outstanding=75.0,
        )
        unit_b = fd.create_partial_payment_unit(
            "OUT-BULK-FULL-B", customer_name=customer_name, phone=phone,
            amount_received=50.0, amount_outstanding=125.0,
        )

        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("200")  # exactly the combined $200 outstanding
            child.expect("Method of Payment:")
            child.send(ENTER)

            text = expect_clean(child, "Write this payment \\(2 units\\) to the master sheet\\?")
            assert "Sold - Partial Payment" not in text
            child.sendline("yes")
            expect_clean(child, "recorded across 2")
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert row_a["Status"] == "Sold" and row_b["Status"] == "Sold"
        # D-137/D-395 (2026-10-05): matches the single-unit path's fix (see
        # TestPartialPayment::test_payment_equal_to_outstanding_fully_settles)
        # -- full settlement writes the final values, not blanks. Each
        # unit's own full outstanding balance was allocated here (total
        # payment == total outstanding), so each is paid off completely.
        assert inv._sheet_float(row_a["Amount Received (USD)"]) == 125.0  # 50 (prior) + 75 (own share)
        assert inv._sheet_float(row_b["Amount Received (USD)"]) == 175.0  # 50 (prior) + 125 (own share)
        assert inv._sheet_float(row_a["Amount Outstanding (USD)"]) == 0.0
        assert inv._sheet_float(row_b["Amount Outstanding (USD)"]) == 0.0


class TestOutstandingConflictResolution:
    """Covers _resolve_outstanding_conflict() (docs/Bulk_Payment_Scoping_Op6_Op7.md
    §5) -- a concurrent sheet change is simulated with a direct update_row()
    call from the test itself, timed between answering the first
    confirmation and the app's own fresh re-read, the same way a second
    real session's write would land in that same window."""

    def test_use_current_value_recomputes_and_rewrites_summary(self):
        unit = fd.create_partial_payment_unit("OUT-CONFLICT-USE-CURRENT", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app(timeout=45)
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
            child.sendline("50")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Confirm and write to sheet\\?")

            # Simulate another session's payment landing in the gap between
            # this confirmation and the write-time re-check.
            inv.update_row(inv.find_row_index_by_sku(unit["SKU"]), {
                "Amount Received (USD)": 150.0, "Amount Outstanding (USD)": 64.18,
            })
            child.sendline("yes")

            text = expect_clean(child, "Which is correct\\?")
            assert "114.18" in text and "64.18" in text
            child.send(ENTER)  # "Use the current sheet value ($64.18) and continue"

            text = expect_clean(child, "Confirm and write to sheet\\?")
            assert "$14.18" in text  # 64.18 - 50, recomputed against the fresh balance
            child.sendline("yes")
            expect_clean(child, "still outstanding")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert inv._sheet_float(updated["Amount Received (USD)"]) == 200.0  # 150 (fresh) + 50
        assert inv._sheet_float(updated["Amount Outstanding (USD)"]) == 14.18

    def test_restore_value_overwrites_the_sheet(self):
        unit = fd.create_partial_payment_unit("OUT-CONFLICT-RESTORE", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app(timeout=45)
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
            child.sendline("50")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Confirm and write to sheet\\?")

            # Simulate a hand-edit/typo on the sheet, not a real payment.
            inv.update_row(inv.find_row_index_by_sku(unit["SKU"]), {
                "Amount Outstanding (USD)": 999.99,
            })
            child.sendline("yes")

            child.expect("Which is correct\\?")
            child.send(DOWN)
            child.send(ENTER)  # "The sheet is wrong — restore it to $114.18 and continue"
            expect_clean(child, "still outstanding")
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        # Restored to the original trajectory (100 + 50 received, 114.18 - 50
        # outstanding), not left at the bad 999.99 value.
        assert inv._sheet_float(updated["Amount Received (USD)"]) == 150.0
        assert inv._sheet_float(updated["Amount Outstanding (USD)"]) == 64.18


class TestSingleUnitStatusHardAbort:
    """TC-377: a Status change (not a balance change) still hard-aborts --
    this never routes through _resolve_outstanding_conflict()."""

    def test_status_change_still_hard_aborts_not_conflict_resolution(self):
        unit = fd.create_partial_payment_unit("OUT-STATUS-HARDABORT", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app(timeout=45)
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
            child.sendline("50")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Confirm and write to sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit["SKU"]), {"Status": "Sold"})
            child.sendline("yes")

            text = expect_clean(child, "status has changed since you started")
            assert "Which is correct" not in text
        finally:
            close(child)


class TestSingleUnitConflictCancel:
    """TC-380: choosing 'Cancel, write nothing' at the conflict prompt
    writes nothing and leaves the sheet exactly as the conflict left it."""

    def test_conflict_cancel_writes_nothing(self):
        unit = fd.create_partial_payment_unit("OUT-CONFLICT-CANCEL", amount_received=100.0,
                                               amount_outstanding=114.18)
        child = spawn_app(timeout=45)
        try:
            _enter_by_sku(child, unit["SKU"])
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Payment Received")
            child.sendline("50")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Confirm and write to sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit["SKU"]), {
                "Amount Outstanding (USD)": 999.99,
            })
            child.sendline("yes")

            child.expect("Which is correct\\?")
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # "Cancel, write nothing"
            text = expect_clean(child, "Returning to Main Menu")
            assert "Payment cancelled" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert inv._sheet_float(unchanged["Amount Outstanding (USD)"]) == 999.99  # left as-is


class TestCheckboxNudge:
    """UAT (TC-382, 2026-10-05): the checkbox screen's instruction text
    alone wasn't sufficient on first exposure -- confirms the explicit
    nudge actually renders, folded into qmark (not a separate print(),
    which would be silently swallowed by the widget's own redraw)."""

    def test_nudge_text_appears_above_the_checkbox_list(self):
        customer_name = f"Pytest Nudge Check Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        fd.create_partial_payment_unit("OUT-NUDGE-A", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=75.0)
        fd.create_partial_payment_unit("OUT-NUDGE-B", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=125.0)
        child = spawn_app(timeout=45)
        try:
            child.sendline("7")
            child.expect("How would you like to select a unit\\?")
            child.send(DOWN)
            child.send(ENTER)  # "Filter by customer"
            child.expect("Customer Search")
            child.sendline(customer_name)
            expect_clean(child, f"Outstanding units for {customer_name}")
            text = expect_clean(child, "Check one or more units, then press Enter")
            assert text  # matched at all -- the assertion is the match itself
            # Regression guard for the raw-ANSI-in-qmark bug (found live via
            # UAT screenshot, 2026-10-05): embedding "\033[2m...\033[0m"
            # directly in questionary's qmark string doesn't get interpreted
            # as styling -- it renders as literal escape-sequence text
            # ("^[[2m...^[[0m"). A plain substring match on the nudge text
            # alone doesn't catch this (it matches regardless of what
            # surrounds it), so explicitly assert no escape artifacts of
            # either form leaked into the rendered output.
            for artifact in ("^[[", "\x1b[", "\\033[", "\\x1b["):
                assert artifact not in text
            child.send(ENTER)  # back out cleanly, nothing checked
        finally:
            close(child)


class TestCheckboxSingleSelection:
    """TC-383: checking exactly one box (customer has 2+ outstanding units)
    still routes to the single-unit flow, not bulk."""

    def test_checking_exactly_one_box_routes_to_single_unit_flow(self):
        customer_name = f"Pytest Single Check Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        fd.create_partial_payment_unit("OUT-SINGLECHECK-A", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=75.0)
        fd.create_partial_payment_unit("OUT-SINGLECHECK-B", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=125.0)
        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 1)
            text = expect_clean(child, "Payment Date")
            assert "UNITS SELECTED" not in text
        finally:
            close(child)


class TestCheckboxZeroSelection:
    """Gap flagged directly by UAT review: the exact first-exposure mistake
    the TC-382 nudge is meant to prevent -- pressing Enter on the checkbox
    with nothing toggled. Confirms the code's own 'if not chosen_skus:
    continue' falls back to the nav menu, not an error or a silent
    zero-unit proceed."""

    def test_pressing_enter_with_nothing_checked_returns_to_nav_menu(self):
        customer_name = f"Pytest Zero Check Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        fd.create_partial_payment_unit("OUT-ZEROCHECK-A", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=75.0)
        fd.create_partial_payment_unit("OUT-ZEROCHECK-B", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=125.0)
        child = spawn_app(timeout=45)
        try:
            child.sendline("7")
            child.expect("How would you like to select a unit\\?")
            child.send(DOWN)
            child.send(ENTER)  # "Filter by customer"
            child.expect("Customer Search")
            child.sendline(customer_name)
            expect_clean(child, f"Outstanding units for {customer_name}")
            child.send(ENTER)  # confirm with nothing toggled
            text = expect_clean(child, "How would you like to select a unit\\?")
            assert "UNITS SELECTED" not in text
            assert "Payment Date" not in text
        finally:
            close(child)


class TestBulkPaymentValidation:
    """TC-388: a total exceeding the combined outstanding balance is rejected."""

    def test_total_payment_exceeding_combined_outstanding_rejected(self):
        customer_name = f"Pytest Overpay Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        fd.create_partial_payment_unit("OUT-BULK-OVERPAY-A", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=75.0)
        fd.create_partial_payment_unit("OUT-BULK-OVERPAY-B", customer_name=customer_name,
                                        phone=phone, amount_received=50.0, amount_outstanding=125.0)
        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("500")  # exceeds combined 200 outstanding
            text = expect_clean(child, "Total Payment Received")  # re-prompted
            assert "exceeds the total outstanding balance" in text
        finally:
            close(child)


class TestBulkPaymentAllocationAtScale:
    """TC-390 (D-391, Op 7 weighting): same largest-remainder guarantee,
    weighted by current Amount Outstanding rather than price."""

    def test_allocation_holds_at_realistic_scale(self):
        n = 10
        customer_name = f"Pytest Scale Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        balances = [round(50 + i * 23.37, 2) for i in range(n)]
        balances[n // 2] = 0.42
        units = [
            _create_partial_payment_unit_retrying_sku_collision(f"OUT-SCALE-{i}", customer_name=customer_name, phone=phone,
                                            amount_received=10.0, amount_outstanding=bal)
            for i, bal in enumerate(balances)
        ]
        total_outstanding = round(sum(balances), 2)
        payment = round(total_outstanding - 0.07, 2)

        child = spawn_app(timeout=120)
        try:
            _enter_bulk_by_customer(child, customer_name, n)
            expect_clean(child, f"UNITS SELECTED \\({n}\\)")
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline(str(payment))
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect(f"Write this payment \\({n} units\\) to the master sheet\\?", timeout=60)
            child.sendline("yes")
            expect_clean(child, f"recorded across {n}", timeout=60)
        finally:
            close(child)

        rows = [inv.get_row_by_sheet_index(inv.find_row_index_by_sku(u["SKU"])) for u in units]
        total_after = sum(
            inv._sheet_float(r["Amount Outstanding (USD)"]) if r["Amount Outstanding (USD)"] != "" else 0.0
            for r in rows
        )
        total_share = round(total_outstanding - total_after, 2)
        assert total_share == payment  # no silent shortfall
        for r in rows:
            outstanding_after = inv._sheet_float(r["Amount Outstanding (USD)"]) if r["Amount Outstanding (USD)"] != "" else 0.0
            assert outstanding_after >= -1e-9  # no overflow past zero


class TestBulkPaymentStatusConflict:
    """TC-392: a Status change on one unit aborts the whole batch."""

    def test_status_change_on_one_unit_aborts_whole_batch(self):
        customer_name = f"Pytest Bulk Status Conflict {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit("OUT-BULK-STATUSCONF-A", customer_name=customer_name,
                                                 phone=phone, amount_received=50.0, amount_outstanding=75.0)
        unit_b = fd.create_partial_payment_unit("OUT-BULK-STATUSCONF-B", customer_name=customer_name,
                                                 phone=phone, amount_received=50.0, amount_outstanding=125.0)
        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("100")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this payment \\(2 units\\) to the master sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {"Status": "Sold"})
            child.sendline("yes")

            text = expect_clean(child, "status has changed since you started")
            assert unit_a["SKU"] in text
        finally:
            close(child)

        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert inv._sheet_float(row_b["Amount Outstanding (USD)"]) == 125.0  # untouched


class TestBulkPaymentSingleConflictResolution:
    """TC-393/TC-394: one conflicting unit resolved as 'restore' needs no
    recompute; resolved as 'use current' recomputes the WHOLE batch."""

    def test_single_conflict_restore_leaves_rest_of_batch_unaffected(self):
        customer_name = f"Pytest Bulk Restore Conflict {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit("OUT-BULK-RESTORE-A", customer_name=customer_name,
                                                 phone=phone, amount_received=50.0, amount_outstanding=100.0)
        unit_b = fd.create_partial_payment_unit("OUT-BULK-RESTORE-B", customer_name=customer_name,
                                                 phone=phone, amount_received=50.0, amount_outstanding=100.0)
        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("100")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this payment \\(2 units\\) to the master sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {
                "Amount Outstanding (USD)": 999.99,
            })
            child.sendline("yes")

            child.expect("Which is correct\\?")
            child.send(DOWN)
            child.send(ENTER)  # "restore"
            expect_clean(child, "recorded across 2")  # no recompute/re-confirmation shown
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert inv._sheet_float(row_a["Amount Outstanding (USD)"]) == 50.0  # restored trajectory, 100-50
        assert inv._sheet_float(row_b["Amount Outstanding (USD)"]) == 50.0  # unaffected by a's conflict

    def test_single_conflict_use_current_recomputes_whole_batch(self):
        customer_name = f"Pytest Bulk UseCurrent Conflict {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit("OUT-BULK-USECUR-A", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        unit_b = fd.create_partial_payment_unit("OUT-BULK-USECUR-B", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("100")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this payment \\(2 units\\) to the master sheet\\?")

            # Simulate a real concurrent payment already landing on unit_a.
            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {
                "Amount Received (USD)": 50.0, "Amount Outstanding (USD)": 50.0,
            })
            child.sendline("yes")

            child.expect("Which is correct\\?")
            child.send(ENTER)  # "use the current value"
            text = expect_clean(child, "Write this payment \\(2 units\\) to the master sheet\\?")
            assert "BULK PAYMENT SUMMARY" in text
            child.sendline("yes")
            expect_clean(child, "recorded across 2")
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        expected_shares = inv._allocate_proportional_payment(100.0, [50.0, 100.0])
        assert inv._sheet_float(row_a["Amount Outstanding (USD)"]) == round(50 - expected_shares[0], 2)
        assert inv._sheet_float(row_b["Amount Outstanding (USD)"]) == round(100 - expected_shares[1], 2)


class TestBulkPaymentMultipleConflicts:
    """TC-395/TC-396: with 2-3 simultaneous conflicts, every conflicting
    unit is prompted first; recompute (if any) happens exactly once
    afterward, and all-restore triggers no recompute at all."""

    def test_multiple_conflicts_mixed_resolutions_recompute_once(self):
        customer_name = f"Pytest Bulk Mixed Conflict {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit("OUT-BULK-MIXED-A", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        unit_b = fd.create_partial_payment_unit("OUT-BULK-MIXED-B", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        unit_c = fd.create_partial_payment_unit("OUT-BULK-MIXED-C", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        child = spawn_app(timeout=90)
        try:
            _enter_bulk_by_customer(child, customer_name, 3)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("150")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this payment \\(3 units\\) to the master sheet\\?")

            # a: hand-edit (-> "restore"); b: real concurrent payment (-> "use current"); c: no conflict.
            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {"Amount Outstanding (USD)": 999.99})
            inv.update_row(inv.find_row_index_by_sku(unit_b["SKU"]), {
                "Amount Received (USD)": 40.0, "Amount Outstanding (USD)": 60.0,
            })
            child.sendline("yes")

            text = expect_clean(child, "Which is correct\\?")
            assert unit_a["SKU"] in text
            child.send(DOWN)
            child.send(ENTER)  # restore a

            # The DOWN keypress above makes questionary redraw unit a's own
            # widget, which re-renders its "Which is correct?" qmark line a
            # second time -- expecting that same literal pattern again here
            # would match that redraw instead of unit b's genuinely new
            # prompt. Unit b's SKU only appears once the next conflict's
            # _warn() line actually prints, so matching on it first skips
            # past the redraw before expecting the qmark again.
            # Matching on the SKU itself (which only appears in the fresh
            # _warn() line this conflict prints) is the proof this is
            # genuinely unit b's turn -- the remainder after that match
            # point no longer contains the SKU to assert on, so confirm
            # unit b's own conflict amount instead.
            warn_text = expect_clean(child, unit_b["SKU"])
            text = expect_clean(child, "Which is correct\\?")
            assert "$60.00" in (warn_text + text)
            time.sleep(0.3)  # let unit b's fresh widget finish rendering before accepting its default
            child.send(ENTER)  # use current for b -- BEFORE any recompute happens

            text = expect_clean(child, "Write this payment \\(3 units\\) to the master sheet\\?")
            assert "BULK PAYMENT SUMMARY" in text
            child.sendline("yes")
            expect_clean(child, "recorded across 3")
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        row_c = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_c["SKU"]))
        expected = inv._allocate_proportional_payment(150.0, [100.0, 60.0, 100.0])
        assert inv._sheet_float(row_a["Amount Outstanding (USD)"]) == round(100 - expected[0], 2)
        assert inv._sheet_float(row_b["Amount Outstanding (USD)"]) == round(60 - expected[1], 2)
        assert inv._sheet_float(row_c["Amount Outstanding (USD)"]) == round(100 - expected[2], 2)

    def test_multiple_conflicts_all_restore_no_recompute(self):
        customer_name = f"Pytest Bulk All Restore Conflict {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit("OUT-BULK-ALLRESTORE-A", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        unit_b = fd.create_partial_payment_unit("OUT-BULK-ALLRESTORE-B", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("100")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this payment \\(2 units\\) to the master sheet\\?")

            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {"Amount Outstanding (USD)": 888.88})
            inv.update_row(inv.find_row_index_by_sku(unit_b["SKU"]), {"Amount Outstanding (USD)": 777.77})
            child.sendline("yes")

            child.expect("Which is correct\\?")
            child.send(DOWN)
            child.send(ENTER)  # restore a
            child.expect("Which is correct\\?")
            child.send(DOWN)
            child.send(ENTER)  # restore b

            expect_clean(child, "recorded across 2")  # no recompute shown
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        row_b = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_b["SKU"]))
        assert inv._sheet_float(row_a["Amount Outstanding (USD)"]) == 50.0  # 100 - 50 (even split restored)
        assert inv._sheet_float(row_b["Amount Outstanding (USD)"]) == 50.0


class TestBulkPaymentRaceToZero:
    """TC-397: a selected unit's balance reaching $0.00 between table
    display and write is caught by the same balance-conflict check."""

    def test_unit_reaching_zero_outstanding_caught_as_balance_conflict(self):
        customer_name = f"Pytest Bulk Race Zero {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit("OUT-BULK-RACEZERO-A", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        unit_b = fd.create_partial_payment_unit("OUT-BULK-RACEZERO-B", customer_name=customer_name,
                                                 phone=phone, amount_received=0.0, amount_outstanding=100.0)
        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("100")
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this payment \\(2 units\\) to the master sheet\\?")

            # unit_a's balance drops to exactly $0.00 between table display
            # and write, Status left as Sold - Partial Payment -- the
            # balance-conflict check must catch this, not the status check.
            inv.update_row(inv.find_row_index_by_sku(unit_a["SKU"]), {
                "Amount Outstanding (USD)": 0.0,
            })
            child.sendline("yes")

            text = expect_clean(child, "Which is correct\\?")
            assert "0.00" in text
            child.send(ENTER)  # use current ($0.00)
            text = expect_clean(child, "Write this payment \\(2 units\\) to the master sheet\\?")
            assert "BULK PAYMENT SUMMARY" in text
            child.sendline("yes")
            expect_clean(child, "recorded across 2")
        finally:
            close(child)

        row_a = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit_a["SKU"]))
        # D-137/D-395 (2026-10-05): full settlement writes the final
        # value (0.0, read back as "0.00"), not blank -- this loose
        # tuple check predates that fix and never anticipated "0.00"
        # specifically, since blank-or-raw-zero was all that was ever
        # actually written before. Missed in the original D-137 pass
        # because that grep only matched the exact `== ""` pattern, not
        # this tuple-membership form.
        assert inv._sheet_float(row_a["Amount Outstanding (USD)"]) == 0.0


class TestHardKillMidBulkWrite:
    """TC-403/TC-400: mirrors Op 6's identical hard-kill test
    (test_interactive_record_sale.py::TestHardKillMidBulkWrite) for Op 7's
    own bulk write loop -- a SIGKILL after confirming a bulk payment but
    before the write loop finishes must leave the batch-intent log
    behind, surfaced on the NEXT launch of either operation. Verifies via
    a fresh Op 6 launch this time, for symmetry with Op 6's test verifying
    via Op 7."""

    def test_sigkill_after_confirm_surfaces_on_next_op6_launch(self):
        customer_name = f"Pytest Hard Kill Payer {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit_a = fd.create_partial_payment_unit("OUT-KILL-A", customer_name=customer_name,
                                                 phone=phone, amount_received=50.0, amount_outstanding=75.0)
        unit_b = fd.create_partial_payment_unit("OUT-KILL-B", customer_name=customer_name,
                                                 phone=phone, amount_received=50.0, amount_outstanding=125.0)

        child = spawn_app(timeout=45)
        try:
            _enter_bulk_by_customer(child, customer_name, 2)
            child.expect("Payment Date")
            child.sendline(TODAY_STR)
            child.expect("Total Payment Received")
            child.sendline("200")  # full combined payoff
            child.expect("Method of Payment:")
            child.send(ENTER)
            child.expect("Write this payment \\(2 units\\) to the master sheet\\?")
            child.sendline("yes")
            # Same fix as Op 6's version: drain the pty while polling for
            # the intent-log file, or the child genuinely deadlocks on a
            # full pty output buffer (confirmed by direct measurement,
            # not a guess carried over from the other file).
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
            verify.sendline("6")  # Op 6, not Op 7 -- proves TC-400's
            # cross-operation claim in the same pass. The leftover-intent
            # check runs before Op 6's own "How many units..." prompt
            # (inventory.py L3874-3876), so it must be expected first.
            text = expect_clean(verify, "Acknowledge and clear this notice\\?")
            assert "UNFINISHED BULK RECORD_OUTSTANDING_PAYMENT_BULK DETECTED" in text
            assert unit_a["SKU"] in text
            assert unit_b["SKU"] in text
            verify.sendline("yes")  # clear it so it doesn't bleed into later tests
            expect_clean(verify, "How many units are part of this sale")
        finally:
            close(verify)
