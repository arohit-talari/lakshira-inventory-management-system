"""
Tier 2 -- Manage Reservation (Op 5), driven through a real pty.

Uses "Instagram" as the reserver contact method throughout (not Phone) --
phone entry requires a country picker and length-validated digits, adding
real complexity to every test for a detail this operation doesn't
otherwise care about. Instagram just needs a handle.

Run: pytest tests/test_interactive_manage_reservation.py -v -s
"""
from datetime import date, timedelta

import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

TODAY_STR = date.today().strftime("%m-%d-%Y")


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _enter_manage_reservation(child, sku):
    child.sendline("5")
    child.expect("SKU:")
    child.sendline(sku)
    child.expect("Is this the correct unit\\?")
    child.sendline("yes")


class TestReserveHappyPath:
    def test_reserving_an_available_unit_for_a_new_customer(self):
        unit = fd.create_fresh_available_unit("RESV-HAPPY")
        child = spawn_app()
        try:
            _enter_manage_reservation(child, unit["SKU"])
            child.expect("Reserved Date")
            child.sendline(TODAY_STR)
            child.expect("Customer search")
            child.sendline("Pytest Reserver")
            child.expect("Register as a new customer")
            child.send(ENTER)
            child.expect("Point of Contact:")
            child.send(DOWN)  # Instagram
            child.send(ENTER)
            child.expect("Instagram handle")
            child.sendline("pytestreserver")
            child.expect("Reservation Note")
            child.sendline("pytest reservation fixture")

            text = expect_clean(child, "Mark this unit as Reserved\\?")
            assert "RESERVATION SUMMARY" in text
            assert "Available" in text and "Reserved" in text
            child.sendline("yes")
            text = expect_clean(child, "reserved for")
            assert unit["SKU"] in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Reserved"
        assert updated["Reserved Date"] == TODAY_STR
        assert "Reserved by Pytest Reserver" in updated["Inventory Notes"]


class TestReleaseHappyPath:
    def test_releasing_a_reserved_unit(self):
        unit = fd.create_fresh_available_unit("RESV-RELEASE", status="Reserved")
        child = spawn_app()
        try:
            _enter_manage_reservation(child, unit["SKU"])
            child.expect("Release note")
            child.sendline("pytest release fixture")
            text = expect_clean(child, "Release this reservation\\?")
            assert "RELEASE RESERVATION SUMMARY" in text
            assert "Reserved" in text and "Available" in text
            child.sendline("yes")
            text = expect_clean(child, "restored to Available")
            assert unit["SKU"] in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert updated["Status"] == "Available"
        assert updated["Reserved Date"] == ""


class TestReservedDateValidation:
    """Pins this session's fix: Reserved Date must not be before the
    unit's own Date Acquired."""

    def test_date_before_date_acquired_is_rejected(self):
        unit = fd.create_fresh_available_unit("RESV-DATE-VALIDATION")
        date_acquired = inv._safe_parse_date(unit["Date Acquired"])
        before_acquired = (date_acquired - timedelta(days=30)).strftime("%m-%d-%Y")

        child = spawn_app()
        try:
            _enter_manage_reservation(child, unit["SKU"])
            child.expect("Reserved Date")
            child.sendline(before_acquired)
            # Not "Reserved Date" -- that matches the start of the warning
            # message itself ("Reserved Date cannot be before..."), cutting
            # `before` off right before the part being asserted on.
            text = expect_clean(child, "cannot be before Date Acquired")
            assert "cannot be before Date Acquired" in text
        finally:
            close(child)

    def test_date_past_seven_day_window_warns_and_allows_override(self):
        unit = fd.create_fresh_available_unit("RESV-OVERDUE-WARN")
        stale_date = (date.today() - timedelta(days=10)).strftime("%m-%d-%Y")

        child = spawn_app()
        try:
            _enter_manage_reservation(child, unit["SKU"])
            child.expect("Reserved Date")
            child.sendline(stale_date)
            text = expect_clean(child, "Proceed anyway\\?")
            assert "past the 7-day window" in text
            child.sendline("no")  # decline the override, re-prompted for date
            expect_clean(child, "Reserved Date")
        finally:
            close(child)


class TestCancellationPaths:
    def test_declining_reserve_confirmation_writes_nothing(self):
        unit = fd.create_fresh_available_unit("RESV-DECLINE")
        child = spawn_app()
        try:
            _enter_manage_reservation(child, unit["SKU"])
            child.expect("Reserved Date")
            child.sendline(TODAY_STR)
            child.expect("Customer search")
            child.sendline("Pytest Decliner")
            child.expect("Register as a new customer")
            child.send(ENTER)
            child.expect("Point of Contact:")
            child.send(DOWN)
            child.send(ENTER)
            child.expect("Instagram handle")
            child.sendline("pytestdecliner")
            child.expect("Reservation Note")
            child.sendline("")
            child.expect("Mark this unit as Reserved\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Reservation cancelled" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Status"] == "Available"

    def test_declining_release_confirmation_writes_nothing(self):
        unit = fd.create_fresh_available_unit("RESV-RELEASE-DECLINE", status="Reserved")
        child = spawn_app()
        try:
            _enter_manage_reservation(child, unit["SKU"])
            child.expect("Release note")
            child.sendline("")
            child.expect("Release this reservation\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Release cancelled" in text
        finally:
            close(child)

        unchanged = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert unchanged["Status"] == "Reserved"


class TestStatusRejection:
    def test_sold_unit_is_rejected(self):
        unit = fd.create_fresh_available_unit("RESV-SOLD-REJECT", status="Sold")
        child = spawn_app()
        try:
            child.sendline("5")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert "Only Available or Reserved units can be managed here" in text
        finally:
            close(child)


class TestBlankPriceWarning:
    def test_blank_price_unit_shows_warning(self):
        unit = fd.get_or_create_blank_price_unit()
        child = spawn_app()
        try:
            child.sendline("5")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "Is this the correct unit\\?")
            assert "blank or unreadable" in text
        finally:
            close(child)
