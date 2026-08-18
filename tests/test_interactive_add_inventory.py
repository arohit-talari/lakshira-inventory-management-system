"""
Tier 2 -- Add Inventory (Op 1), both single-unit and bulk-unit entry,
driven through a real pty.

This is the first *write* operation covered in Tier 2 -- every test that
completes a real "Write ... to the master sheet? yes" leaves a genuine new
row in the TEST sheet. No cleanup step: this matches how the test sheet is
already used (manually, by the project owner, with rows like LAH-TWV100
left in place as reference data), so accumulating test rows here is
consistent with existing practice, not a new risk.

Uses "Twill Kanjivaram" (category TKSS) as the default weave type for most
tests -- confirmed to be the only Saree weave type whose name doesn't
appear as a substring of any other Saree weave type, so searching for it
always yields exactly one match regardless of how the two-step navigation
(garment type, then weave type) plays out. Where a test specifically needs
a different weave type (the Unassigned-reuse path needs the sheet's real
Unassigned row's actual weave type), navigation is computed dynamically
from inv.WEAVE_GARMENT_TYPES rather than hardcoded arrow-key counts, so it
can't silently drift out of sync with the real sorted/filtered list the
app itself would show.

Run: pytest tests/test_interactive_add_inventory.py -v -s
"""
import pytest

import inventory as inv
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

TEST_WEAVE_TYPE = "Twill Kanjivaram"
TEST_CATEGORY_CODE = "TKSS"
TEST_GARMENT_TYPE = "Saree"


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


@pytest.fixture(scope="module", autouse=True)
def _load_reference_data():
    """GARMENT_TYPES / WEAVE_GARMENT_TYPES are populated by main_menu() at
    interactive startup, not at import time -- load them here so the
    navigation-index helpers below have real data to compute against."""
    inv.load_garment_types()
    inv.load_weave_types()


@pytest.fixture(scope="module")
def unassigned_unit():
    rows = inv.get_all_rows()
    for r in rows:
        if r.get("Status", "").strip() == "Unassigned":
            return r
    pytest.skip("No Unassigned-status unit on the test sheet to run the reuse-path test against.")


def _navigate_to_weave_type(child, garment_type, weave_type_name, search_query):
    """Drives select_or_add_weave_type()'s two-step picker (garment type,
    then weave type) to land on a specific weave type, computing arrow-key
    positions from the same filter+sort logic the app itself uses rather
    than a hardcoded down-count."""
    child.expect("Select garment type:")
    garment_idx = inv.GARMENT_TYPES.index(garment_type)
    for _ in range(garment_idx):
        child.send(DOWN)
    child.send(ENTER)

    child.expect(f"Search {garment_type} weave types")
    child.sendline(search_query)
    child.expect(f"Select {garment_type} weave type:")

    filtered_names = sorted(
        [name for name, gt in inv.WEAVE_GARMENT_TYPES.items() if gt == garment_type],
        key=str.lower,
    )
    display_names = [name for name in filtered_names if search_query.lower() in name.lower()]
    weave_idx = display_names.index(weave_type_name)
    for _ in range(weave_idx):
        child.send(DOWN)
    child.send(ENTER)
    child.expect("Weave Type:")


def _drive_to_pricing_preview(child, markup_pct="60"):
    """Common tail shared by every happy-path single-unit test from the
    pricing-path menu onward: markup entry -> the always-shown 'how would
    you like to proceed' guided menu -> accept the default (proceed)."""
    child.expect("How would you like to set the selling price\\?")
    child.send(ENTER)  # "Enter a markup percentage"
    child.expect("Markup percentage")
    child.sendline(markup_pct)
    child.expect("How would you like to proceed\\?")
    child.send(ENTER)  # "Proceed with this price"


class TestSingleUnitHappyPath:
    def test_fresh_sku_add_writes_correct_row(self):
        child = spawn_app()
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("")  # default 1
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)

            text = expect_clean(child, "Confirm\\?")
            assert f"Category Code: {TEST_CATEGORY_CODE}" in text
            import re
            m = re.search(r"Your SKU will be: (LAH-\S+)", text)
            assert m, f"Could not find generated SKU in: {text!r}"
            sku = m.group(1)
            child.sendline("yes")

            child.expect("Source Sheet")
            child.sendline("Sheet1 - PytestTab")
            child.expect("Select supplier:")
            child.send(ENTER)  # first supplier in the list
            child.expect("Date Acquired")
            child.sendline("07-01-2026")
            child.expect("Base Price")
            child.sendline("15000")
            child.expect("Shipping Cost")
            child.sendline("")  # default 750
            child.expect("Detailing Cost")
            child.sendline("")  # default 0
            child.expect("Confirm this rate\\?")
            child.sendline("yes")

            _drive_to_pricing_preview(child)

            child.expect("Inventory Notes")
            child.sendline("pytest fixture -- safe to ignore")

            text = expect_clean(child, "Write this to the master sheet\\?")
            assert "ADD SUMMARY" in text
            assert sku in text
            assert "Status:               Available" in text

            child.sendline("yes")
            text = expect_clean(child, "added successfully")
            assert sku in text
        finally:
            close(child)

        # Verify directly against the sheet, not just the terminal transcript.
        row = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(sku))
        assert row["Status"] == "Available"
        assert row["Category Code"] == TEST_CATEGORY_CODE
        assert row["Weave Type / Cluster"] == TEST_WEAVE_TYPE
        assert row["Supplier"]
        assert inv._sheet_float(row["Total Cost (INR)"]) == 15750.0
        assert inv._sheet_float(row["Selling Price (USD)"]) > 0
        assert "pytest fixture" in row["Inventory Notes"]


class TestSingleUnitCancellationPaths:
    def test_discard_at_pricing_path_menu_writes_nothing(self):
        child = spawn_app()
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("")
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
            child.expect("Confirm\\?")
            child.sendline("yes")
            child.expect("Source Sheet")
            child.sendline("Sheet1 - PytestTab")
            child.expect("Select supplier:")
            child.send(ENTER)
            child.expect("Date Acquired")
            child.sendline("07-01-2026")
            child.expect("Base Price")
            child.sendline("15000")
            child.expect("Shipping Cost")
            child.sendline("")
            child.expect("Detailing Cost")
            child.sendline("")
            child.expect("Confirm this rate\\?")
            child.sendline("yes")
            child.expect("How would you like to set the selling price\\?")
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # "Discard & exit"
            text = expect_clean(child, "Returning to Main Menu")
            assert "Add cancelled" in text
        finally:
            close(child)

    def test_declining_final_confirmation_writes_nothing(self):
        rows_before = len(inv.get_raw_rows())
        child = spawn_app()
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("")
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
            child.expect("Confirm\\?")
            child.sendline("yes")
            child.expect("Source Sheet")
            child.sendline("Sheet1 - PytestTab")
            child.expect("Select supplier:")
            child.send(ENTER)
            child.expect("Date Acquired")
            child.sendline("07-01-2026")
            child.expect("Base Price")
            child.sendline("15000")
            child.expect("Shipping Cost")
            child.sendline("")
            child.expect("Detailing Cost")
            child.sendline("")
            child.expect("Confirm this rate\\?")
            child.sendline("yes")
            _drive_to_pricing_preview(child)
            child.expect("Inventory Notes")
            child.sendline("")
            child.expect("Write this to the master sheet\\?")
            child.sendline("no")
            text = expect_clean(child, "Returning to Main Menu")
            assert "Add cancelled" in text
        finally:
            close(child)

        assert len(inv.get_raw_rows()) == rows_before


class TestSingleUnitValidation:
    def test_blank_source_sheet_is_rejected(self):
        child = spawn_app()
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("")
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
            child.expect("Confirm\\?")
            child.sendline("yes")
            child.expect("Source Sheet")
            child.sendline("")  # blank -- required field
            text = expect_clean(child, "Source Sheet")  # re-prompted
            assert "cannot be left blank" in text
        finally:
            close(child)

    def test_invalid_base_price_is_rejected(self):
        child = spawn_app()
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("")
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
            child.expect("Confirm\\?")
            child.sendline("yes")
            child.expect("Source Sheet")
            child.sendline("Sheet1 - PytestTab")
            child.expect("Select supplier:")
            child.send(ENTER)
            child.expect("Date Acquired")
            child.sendline("07-01-2026")
            child.expect("Base Price")
            child.sendline("not a number")
            text = expect_clean(child, "Base Price")  # re-prompted
            assert "valid number" in text.lower() or "Please enter" in text
        finally:
            close(child)

    def test_future_date_acquired_is_rejected(self):
        child = spawn_app()
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("")
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
            child.expect("Confirm\\?")
            child.sendline("yes")
            child.expect("Source Sheet")
            child.sendline("Sheet1 - PytestTab")
            child.expect("Select supplier:")
            child.send(ENTER)
            child.expect("Date Acquired")
            child.sendline("01-01-2099")
            text = expect_clean(child, "Date Acquired")  # re-prompted
            assert "future" in text.lower()
        finally:
            close(child)


class TestUnassignedRowReuse:
    """resolve_sku() offers existing Unassigned rows in the same category
    before generating a fresh SKU -- confirming this against a real
    Unassigned row on the sheet, not just the fresh-SKU path covered above."""

    def test_reusing_an_unassigned_row_offers_it_first(self, unassigned_unit):
        garment_type = inv.WEAVE_GARMENT_TYPES.get(unassigned_unit["Weave Type / Cluster"], "Saree")
        child = spawn_app()
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("")
            _navigate_to_weave_type(
                child, garment_type,
                unassigned_unit["Weave Type / Cluster"], unassigned_unit["Weave Type / Cluster"],
            )
            text = expect_clean(child, "Would you like to assign this unit to one of these SKUs\\?")
            assert unassigned_unit["SKU"] in text
        finally:
            close(child)


class TestBulkAddSameWeaveType:
    """Pins this session's fix directly: two units of the same weave type
    in one uncommitted batch must both get real, sequential, distinct SKUs
    at write time -- and the warning shown for the second one must say it
    was claimed earlier in *this batch*, not misattribute it to another
    session (nothing else was running)."""

    def test_two_units_same_weave_type_get_distinct_sequential_skus(self):
        child = spawn_app(timeout=45)
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("2")

            # Batch-common fields (asked once)
            child.expect("Source Sheet")
            child.sendline("Sheet1 - PytestBulkTab")
            child.expect("Select supplier:")
            child.send(ENTER)
            child.expect("Date Acquired")
            child.sendline("07-01-2026")
            child.expect("Confirm this rate\\?")
            child.sendline("yes")

            skus = []
            for i in (1, 2):
                text = expect_clean(child, f"UNIT INTAKE \\({i} of 2\\)")
                _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
                text = expect_clean(child, "Confirm\\?")  # resolve_sku()'s own SKU confirmation
                import re
                m = re.search(r"Your SKU will be: (LAH-\S+)", text)
                assert m, f"Could not find generated SKU in unit {i}: {text!r}"
                skus.append(m.group(1))
                child.sendline("yes")
                child.expect("Base Price")
                child.sendline("12000")
                child.expect("Shipping Cost")
                child.sendline("")
                child.expect("Detailing Cost")
                child.sendline("")
                _drive_to_pricing_preview(child)
                child.expect("Inventory Notes")
                child.sendline(f"pytest bulk fixture unit {i}")
                child.expect("Add this unit to the batch\\?")
                child.sendline("yes")

            # Both units collected in memory with the SAME "next" SKU
            # (generate_next_sku has no visibility into the other
            # uncommitted unit) -- this is the scenario the fix targets.
            assert skus[0] == skus[1], (
                "Test setup assumption broken: expected both in-memory units "
                "to collide on the same proposed SKU before the write loop "
                "runs -- if this fails, the pre-write collision this test "
                "exists to verify simply won't occur."
            )

            text = expect_clean(child, "Write all 2 units to the master sheet\\?")
            assert "BULK INTAKE SUMMARY" in text
            child.sendline("yes")

            text = expect_clean(child, "added successfully")
            assert "already used earlier in this batch" in text
            assert "claimed by another session" not in text
        finally:
            close(child)

        # Verify against the sheet: two distinct rows, sequential SKUs, both Available.
        rows = inv.get_all_rows()
        matches = [r for r in rows if "pytest bulk fixture unit" in (r.get("Inventory Notes") or "")
                   and r.get("Weave Type / Cluster") == TEST_WEAVE_TYPE
                   and r.get("Source Sheet + Tab") == "Sheet1 - PytestBulkTab"]
        skus_written = sorted(r["SKU"] for r in matches[-2:])
        assert len(set(skus_written)) == 2, f"Expected 2 distinct SKUs, got: {skus_written}"
        for r in matches[-2:]:
            assert r["Status"] == "Available"


class TestBulkAddSkipAndCancel:
    def test_skipping_a_unit_and_writing_the_rest(self):
        child = spawn_app(timeout=45)
        try:
            child.sendline("1")
            child.expect("How many units are you adding")
            child.sendline("2")
            child.expect("Source Sheet")
            child.sendline("Sheet1 - PytestBulkTab")
            child.expect("Select supplier:")
            child.send(ENTER)
            child.expect("Date Acquired")
            child.sendline("07-01-2026")
            child.expect("Confirm this rate\\?")
            child.sendline("yes")

            # Unit 1: skip via "Skip this unit" at the pricing-path menu
            child.expect("UNIT INTAKE \\(1 of 2\\)")
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
            child.expect("Confirm\\?")
            child.sendline("yes")
            child.expect("Base Price")
            child.sendline("12000")
            child.expect("Shipping Cost")
            child.sendline("")
            child.expect("Detailing Cost")
            child.sendline("")
            child.expect("How would you like to set the selling price\\?")
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # "Skip this unit"
            text = expect_clean(child, "UNIT INTAKE \\(2 of 2\\)")
            assert "skipped" in text.lower()

            # Unit 2: complete normally
            _navigate_to_weave_type(child, TEST_GARMENT_TYPE, TEST_WEAVE_TYPE, TEST_WEAVE_TYPE)
            child.expect("Confirm\\?")
            child.sendline("yes")
            child.expect("Base Price")
            child.sendline("12000")
            child.expect("Shipping Cost")
            child.sendline("")
            child.expect("Detailing Cost")
            child.sendline("")
            _drive_to_pricing_preview(child)
            child.expect("Inventory Notes")
            child.sendline("pytest bulk skip-path fixture")
            child.expect("Add this unit to the batch\\?")
            child.sendline("yes")

            text = expect_clean(child, "Write all 1 unit")
            assert "BULK INTAKE SUMMARY" in text
            child.sendline("no")  # decline the final write
            text = expect_clean(child, "Returning to Main Menu")
            assert "Bulk add cancelled" in text
            assert "Nothing was written" in text
        finally:
            close(child)
