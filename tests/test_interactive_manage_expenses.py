"""
Tier 2 -- Manage Expenses (Op 9), driven through a real pty.

Four distinct flows covered: logging a new expense (including registering a
brand-new subcategory), editing an existing expense (including the
recurring-registry sync questions), and the recurring sweep that opens
every Operation 9 visit (including Finding 40's "Review the rest later"
escape hatch). Category and Subcategory are both alphabetized arrow-key
lists (Findings 30 and 43) with no fixed position -- their down-arrow
counts are computed live against the real sheet data in each helper below,
not hardcoded, so a future category/subcategory addition can't silently
desync these tests the same way _MAIN_MENU_PROMPT's own comment warns
option counts do.

Run: pytest tests/test_interactive_manage_expenses.py -v -s
"""
import time
from datetime import date, timedelta

import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

CATEGORY = fd._EXPENSE_CATEGORY            # "Marketing & Advertising"
SUBCATEGORY = fd._EXPENSE_SUBCATEGORY       # "Ad Spend (Instagram/Meta)"


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _category_downs(category):
    """How many DOWN presses from the top of _select_expense_category()'s
    list reach `category` -- computed against the real EXPENSE_CATEGORIES
    order each call, not hardcoded, since the picker sorts for display
    (Finding 30) and a future category addition would silently shift
    everyone else's position."""
    ordered = sorted(inv.EXPENSE_CATEGORIES, key=str.lower)
    return ordered.index(category)


def _subcategory_downs(category, subcategory):
    """Same idea as _category_downs(), scoped to one category's live
    subcategory list (also alphabetized, Finding 43)."""
    existing = sorted(inv._get_expense_subcategories().get(category, []), key=str.lower)
    return existing.index(subcategory)


def _open_manage_expenses(child):
    """Enters option 9. Defers any stray overdue recurring item left over
    from another test file or a prior run via "Review the rest later"
    (Finding 40's escape hatch) -- 4 downs from the top of the per-item
    action menu (Confirm/Adjust/Skip/Stop, then a separator, then this)
    -- so every other test reaches Manage Expenses' own menu regardless of
    unrelated global sweep state. TestRecurringSweep drives the sweep
    deliberately itself, separately."""
    child.sendline("9")
    index = child.expect(["How would you like to handle this recurring expense\\?",
                           "How would you like to proceed\\?"])
    while index == 0:
        child.send(DOWN * 4)
        child.send(ENTER)
        index = child.expect(["How would you like to handle this recurring expense\\?",
                               "How would you like to proceed\\?"])


def _navigate_to_category_and_subcategory(child, category=CATEGORY, subcategory=SUBCATEGORY):
    """From _select_expense_type()'s own qmark (the very first prompt of
    _log_new_expense()) through a chosen, already-registered subcategory.
    "Operating Expense" is always the first Type choice, so no downs
    needed there."""
    child.expect("Quick test: after one use")
    child.send(ENTER)  # Operating Expense
    child.expect("Select category:")
    child.send(DOWN * _category_downs(category))
    child.send(ENTER)
    child.expect(f"Select subcategory under {category}:")
    child.send(DOWN * _subcategory_downs(category, subcategory))
    child.send(ENTER)


def _log_basic_expense(child, amount, expense_date, description,
                        category=CATEGORY, subcategory=SUBCATEGORY, recurring=False, cadence_months=None):
    """Drives _log_new_expense() end to end for an already-registered
    category/subcategory, up to but not including the final "Log this
    expense?" confirm -- the caller does its own expect_clean() for that
    (to both capture the EXPENSE SUMMARY text and decide yes/no), so this
    never leaves the already-rendered prompt unconsumed for a caller to
    double-expect and hang on."""
    _open_manage_expenses(child)
    child.send(ENTER)  # "Log a new expense" (first choice)
    _navigate_to_category_and_subcategory(child, category, subcategory)
    child.expect("Amount \\(USD\\):")
    child.sendline(str(amount))
    child.expect("Date \\(MM-DD-YYYY\\):")
    child.sendline(expense_date.strftime("%m-%d-%Y"))
    child.expect("Description/vendor:")
    child.sendline(description)
    child.expect("Is this a recurring expense\\? \\(yes/no\\):")
    child.sendline("yes" if recurring else "no")
    if recurring:
        child.expect("Repeats every how many months")
        child.sendline(str(cadence_months))


class TestLogNewExpense:
    def test_happy_path_one_off_expense(self):
        child = spawn_app()
        try:
            _log_basic_expense(child, 42.50, date(2026, 8, 1), "pytest one-off expense")
            text = expect_clean(child, "Log this expense\\?")
            assert "EXPENSE SUMMARY" in text
            assert "$42.50" in text
            assert CATEGORY in text and SUBCATEGORY in text
            child.sendline("yes")
            text = expect_clean(child, "Log another expense\\?")
            assert "Logged EXP-" in text
            assert "$42.50" in text
            assert CATEGORY in text and SUBCATEGORY in text
            child.sendline("no")
            expect_clean(child, "Returning to Main Menu")
        finally:
            close(child)

        rows = inv._get_recent_expense_rows(limit=None)
        logged = next(r for r in rows if "pytest one-off expense" in r["Description"])
        assert logged["Category"] == CATEGORY
        assert logged["Subcategory"] == SUBCATEGORY
        assert inv._sheet_float(logged["Amount (USD)"]) == 42.50
        assert logged["Recurring (Y/N)"] == "No"
        fd._created_expense_ids.append(logged["Expense ID"])

    def test_declining_final_confirmation_writes_nothing(self):
        before = len(inv._get_recent_expense_rows(limit=None))
        child = spawn_app()
        try:
            _log_basic_expense(child, 15.00, date(2026, 8, 1), "pytest declined expense")
            expect_clean(child, "Log this expense\\?")
            child.sendline("no")
            text = expect_clean(child, "Discarded. Returning to Manage Expenses.")
            assert "Discarded" in text
        finally:
            close(child)

        after = len(inv._get_recent_expense_rows(limit=None))
        assert after == before

    def test_recurring_expense_creates_a_linked_registry_row(self):
        child = spawn_app()
        try:
            _log_basic_expense(child, 60.00, date(2026, 8, 1), "pytest recurring expense",
                                recurring=True, cadence_months=1)
            text = expect_clean(child, "Log this expense\\?")
            assert "Recurring:" in text and "every 1 month" in text
            child.sendline("yes")
            text = expect_clean(child, "Log another expense\\?")
            assert "Logged EXP-" in text
            child.sendline("no")
            expect_clean(child, "Returning to Main Menu")
        finally:
            close(child)

        rows = inv._get_recent_expense_rows(limit=None)
        logged = next(r for r in rows if "pytest recurring expense" in r["Description"])
        fd._created_expense_ids.append(logged["Expense ID"])
        assert logged["Recurring (Y/N)"] == "Yes"
        series_id = logged["Recurring Series ID"].strip()
        assert series_id

        registry_rows = inv._get_recurring_registry_rows()
        reg = next(r for r in registry_rows if r["Registry ID"] == series_id)
        fd._created_registry_ids.append(series_id)
        assert reg["Category"] == CATEGORY
        assert reg["Subcategory"] == SUBCATEGORY
        assert inv._sheet_float(reg["Last Amount"]) == 60.00
        assert reg["Cadence (Months)"].strip() == "1"
        assert reg["Active (Y/N)"] == "Yes"


class TestSubcategoryRegistration:
    def test_registering_a_brand_new_subcategory(self):
        # A name guaranteed not to already exist, and not close enough to
        # an existing one to trigger the dedup "did you mean" prompt.
        # Second-granularity, not just today's date -- this subcategory
        # registration is permanent (matches the TWV/TestWeave convention
        # for shared reference-data fixtures elsewhere in this suite), so a
        # same-day rerun with only a date-grained name would collide with
        # its own prior run's real, still-registered subcategory and hit
        # the "already exists" dedup path instead of the new-registration
        # path this test is actually about.
        new_subcat = f"ZZTEST Subcat {int(time.time())}"
        child = spawn_app()
        try:
            _open_manage_expenses(child)
            child.send(ENTER)  # "Log a new expense"
            child.expect("Quick test: after one use")
            child.send(ENTER)  # Operating Expense
            child.expect("Select category:")
            child.send(DOWN * _category_downs(CATEGORY))
            child.send(ENTER)
            child.expect(f"Select subcategory under {CATEGORY}:")
            # No search step at all (Finding 43) -- straight to the list,
            # "+ Add new subcategory" sits right after every existing one.
            existing_count = len(inv._get_expense_subcategories().get(CATEGORY, []))
            child.send(DOWN * existing_count)
            child.send(ENTER)  # "+ Add new subcategory"
            child.expect("What's this expense\\?")
            child.sendline(new_subcat)
            text = expect_clean(child, "Amount \\(USD\\):")
            assert "Proceeding to register" in text
            assert new_subcat in text
            child.sendline("35.00")
            child.expect("Date \\(MM-DD-YYYY\\):")
            child.sendline("08-01-2026")
            child.expect("Description/vendor:")
            child.sendline("pytest new subcategory registration")
            child.expect("Is this a recurring expense\\? \\(yes/no\\):")
            child.sendline("no")
            child.expect("Log this expense\\?")
            child.sendline("yes")
            text = expect_clean(child, "Log another expense\\?")
            assert "Logged EXP-" in text
            assert new_subcat in text
            child.sendline("no")
            expect_clean(child, "Returning to Main Menu")
        finally:
            close(child)

        try:
            rows = inv._get_recent_expense_rows(limit=None)
            logged = next(r for r in rows if "pytest new subcategory registration" in r["Description"])
            fd._created_expense_ids.append(logged["Expense ID"])
            assert logged["Subcategory"] == new_subcat

            all_subs = inv._get_expense_subcategories()
            assert new_subcat in all_subs.get(CATEGORY, [])
        finally:
            # Not autouse-cleaned like _created_expense_ids/_created_registry_ids --
            # this registration is a direct write by the app itself
            # (_commit_new_subcategory()), not a create_*() fixture helper, and a
            # leftover here breaks reruns via the fuzzy "did you mean" dedup check
            # (a near-miss near-duplicate redirects instead of registering fresh),
            # not just leaving inert clutter. In its own finally, not only after
            # the assertions above, so a failed assertion still doesn't strand it.
            fd.delete_expense_subcategory(CATEGORY, new_subcat)


def _enter_edit_expense_by_id(child, expense_id):
    _open_manage_expenses(child)
    child.send(DOWN)  # "Edit an existing expense"
    child.send(ENTER)
    child.expect("press Enter to browse recent")
    child.sendline(expense_id)
    child.expect("Is this the correct expense\\?")
    child.sendline("yes")


class TestEditExpense:
    def test_edit_amount_writes_the_new_value(self):
        expense = fd.create_expense_row("EDIT-AMOUNT", amount=50.00)
        child = spawn_app()
        try:
            _enter_edit_expense_by_id(child, expense["Expense ID"])
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 4)  # Date, Type, Category, Subcategory, -> Amount (USD)
            child.send(ENTER)
            child.expect("New amount \\(USD\\):")
            child.sendline("75.00")
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 6)  # -> "Done -- review changes" (past Description + separator)
            child.send(ENTER)
            text = expect_clean(child, "Apply these changes\\?")
            assert "EDIT SUMMARY" in text
            assert "$50.00" in text and "$75.00" in text
            child.send(ENTER)  # "Confirm and apply changes" is the first choice -- an arrow-key
                                # select, not a text prompt; sendline()'s typed characters would
                                # sit unconsumed in the pty and bleed into whatever next prompt
                                # actually reads text input
            text = expect_clean(child, "Updated " + expense["Expense ID"])
        finally:
            close(child)

        updated = inv._get_recent_expense_rows(limit=None)
        row = next(r for r in updated if r["Expense ID"] == expense["Expense ID"])
        assert inv._sheet_float(row["Amount (USD)"]) == 75.00

    def test_no_changes_made_returns_without_writing(self):
        expense = fd.create_expense_row("EDIT-NOCHANGE", amount=22.00)
        child = spawn_app()
        try:
            _enter_edit_expense_by_id(child, expense["Expense ID"])
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 6)  # -> "Cancel -- no changes" (no edits staged yet)
            child.send(ENTER)
            text = expect_clean(child, "No changes made")
            assert "No changes made" in text
        finally:
            close(child)

        updated = inv._get_recent_expense_rows(limit=None)
        row = next(r for r in updated if r["Expense ID"] == expense["Expense ID"])
        assert inv._sheet_float(row["Amount (USD)"]) == 22.00

    def test_discarding_staged_changes_writes_nothing(self):
        expense = fd.create_expense_row("EDIT-DISCARD", amount=18.00)
        child = spawn_app()
        try:
            _enter_edit_expense_by_id(child, expense["Expense ID"])
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 4)  # -> Amount (USD)
            child.send(ENTER)
            child.expect("New amount \\(USD\\):")
            child.sendline("99.00")
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 6)
            child.send(ENTER)  # Done -- review changes
            child.expect("Apply these changes\\?")
            child.send(DOWN * 2)  # -> "Discard changes & continue"
            child.send(ENTER)
            text = expect_clean(child, "Edit cancelled")
        finally:
            close(child)

        updated = inv._get_recent_expense_rows(limit=None)
        row = next(r for r in updated if r["Expense ID"] == expense["Expense ID"])
        assert inv._sheet_float(row["Amount (USD)"]) == 18.00


class TestRecurringSync:
    def test_editing_the_latest_instance_offers_to_sync_the_registry(self):
        registry = fd.create_recurring_registry_row("SYNC-SOURCE", amount=40.00, cadence_months=1,
                                                      logged_date=date.today())
        expense = fd.create_expense_row("SYNC-EXPENSE", amount=40.00, expense_date=date.today(),
                                         recurring=True, series_id=registry["Registry ID"])
        child = spawn_app()
        try:
            _enter_edit_expense_by_id(child, expense["Expense ID"])
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 4)  # -> Amount (USD)
            child.send(ENTER)
            child.expect("New amount \\(USD\\):")
            child.sendline("55.00")
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 6)
            child.send(ENTER)  # Done -- review changes
            child.expect("Apply these changes\\?")
            child.send(ENTER)  # "Confirm and apply changes" (arrow-key select, not a text prompt)
            text = expect_clean(child, "as well\\?")
            assert "update the tracked amount to $55.00" in text
            child.sendline("yes")
            expect_clean(child, "Updated " + expense["Expense ID"])
        finally:
            close(child)

        reg_rows = inv._get_recurring_registry_rows()
        reg = next(r for r in reg_rows if r["Registry ID"] == registry["Registry ID"])
        assert inv._sheet_float(reg["Last Amount"]) == 55.00

    def test_declining_the_sync_question_leaves_the_registry_untouched(self):
        registry = fd.create_recurring_registry_row("SYNC-DECLINE-SOURCE", amount=40.00, cadence_months=1,
                                                      logged_date=date.today())
        expense = fd.create_expense_row("SYNC-DECLINE-EXPENSE", amount=40.00, expense_date=date.today(),
                                         recurring=True, series_id=registry["Registry ID"])
        child = spawn_app()
        try:
            _enter_edit_expense_by_id(child, expense["Expense ID"])
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 4)
            child.send(ENTER)
            child.expect("New amount \\(USD\\):")
            child.sendline("65.00")
            child.expect("Which field would you like to edit\\?")
            child.send(DOWN * 6)
            child.send(ENTER)
            child.expect("Apply these changes\\?")
            child.send(ENTER)  # "Confirm and apply changes" (arrow-key select, not a text prompt)
            child.expect("update the tracked amount")
            child.sendline("no")
            expect_clean(child, "Updated " + expense["Expense ID"])
        finally:
            close(child)

        reg_rows = inv._get_recurring_registry_rows()
        reg = next(r for r in reg_rows if r["Registry ID"] == registry["Registry ID"])
        assert inv._sheet_float(reg["Last Amount"]) == 40.00

        updated_expense = inv._get_recent_expense_rows(limit=None)
        row = next(r for r in updated_expense if r["Expense ID"] == expense["Expense ID"])
        assert inv._sheet_float(row["Amount (USD)"]) == 65.00


class TestRecurringSweep:
    def test_confirm_logs_a_new_instance_and_advances_the_registry(self):
        # 35 days, not something like 65 -- deliberately within a single
        # overdue period for a monthly cadence. The sweep catches up one
        # period at a time (its own documented behavior), so a start date
        # overdue by more than one full cadence would re-show this same
        # per-item menu a second time after the first Confirm, which this
        # test doesn't handle (that multi-period catch-up is a real,
        # separate behavior, not something to accidentally exercise here).
        overdue_date = date.today() - timedelta(days=35)
        registry = fd.create_recurring_registry_row("SWEEP-CONFIRM", amount=20.00, cadence_months=1,
                                                      logged_date=overdue_date)
        child = spawn_app()
        try:
            child.sendline("9")
            text = expect_clean(child, "How would you like to handle this recurring expense\\?")
            assert "RECURRING EXPENSE DUE" in text
            assert "$20.00" in text
            child.send(ENTER)  # "Confirm at $20.00" (first choice)
            text = expect_clean(child, "How would you like to proceed\\?")
            assert "Logged EXP-" in text
            child.send(DOWN)  # back out cleanly via Main Menu
            child.send(ENTER)
        finally:
            close(child)

        reg_rows = inv._get_recurring_registry_rows()
        reg = next(r for r in reg_rows if r["Registry ID"] == registry["Registry ID"])
        assert reg["Last Logged Date"] != overdue_date.strftime("%m-%d-%Y")

        logged = inv._get_recent_expense_rows(limit=None)
        new_instance = next(r for r in logged if r.get("Recurring Series ID", "").strip() == registry["Registry ID"])
        fd._created_expense_ids.append(new_instance["Expense ID"])

    def test_review_the_rest_later_exits_without_writing_or_forcing_the_next_item(self):
        overdue_date = date.today() - timedelta(days=65)
        first = fd.create_recurring_registry_row("SWEEP-DEFER-A", amount=10.00, cadence_months=1,
                                                   logged_date=overdue_date)
        second = fd.create_recurring_registry_row("SWEEP-DEFER-B", amount=10.00, cadence_months=1,
                                                    logged_date=overdue_date)
        child = spawn_app()
        try:
            child.sendline("9")
            text = expect_clean(child, "How would you like to handle this recurring expense\\?")
            assert "2 recurring expenses are due" in text or "recurring expenses are due" in text
            child.send(DOWN * 4)  # -> "Review the rest later"
            child.send(ENTER)
            text = expect_clean(child, "How would you like to proceed\\?")
            # Neither the second item's own prompt, nor any write confirmation,
            # should appear between the sweep and Manage Expenses' own menu.
            assert "RECURRING EXPENSE DUE" not in text
        finally:
            close(child)

        reg_rows = inv._get_recurring_registry_rows()
        reg_a = next(r for r in reg_rows if r["Registry ID"] == first["Registry ID"])
        reg_b = next(r for r in reg_rows if r["Registry ID"] == second["Registry ID"])
        assert reg_a["Last Reviewed Date"] == overdue_date.strftime("%m-%d-%Y")
        assert reg_b["Last Reviewed Date"] == overdue_date.strftime("%m-%d-%Y")

    def test_skip_just_this_period_advances_last_reviewed_without_logging(self):
        # Single overdue period, same reasoning as test_confirm_... above --
        # Skip also advances Last Reviewed Date and re-checks, so more than
        # one cadence overdue would re-show this same menu a second time.
        overdue_date = date.today() - timedelta(days=35)
        registry = fd.create_recurring_registry_row("SWEEP-SKIP", amount=10.00, cadence_months=1,
                                                      logged_date=overdue_date)
        before_count = len(inv._get_recent_expense_rows(limit=None))
        child = spawn_app()
        try:
            child.sendline("9")
            child.expect("How would you like to handle this recurring expense\\?")
            child.send(DOWN * 2)  # -> "Skip just this period"
            child.send(ENTER)
            expect_clean(child, "How would you like to proceed\\?")
            child.send(DOWN)
            child.send(ENTER)  # Back to Main Menu
        finally:
            close(child)

        after_count = len(inv._get_recent_expense_rows(limit=None))
        assert after_count == before_count  # nothing logged

        reg_rows = inv._get_recurring_registry_rows()
        reg = next(r for r in reg_rows if r["Registry ID"] == registry["Registry ID"])
        assert reg["Last Reviewed Date"] != overdue_date.strftime("%m-%d-%Y")
        assert reg["Last Logged Date"] == overdue_date.strftime("%m-%d-%Y")  # untouched by Skip
