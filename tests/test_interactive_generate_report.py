"""
Tier 2 -- Generate Report (Op 10), driven through a real pty.

Structurally unlike every other operation tested so far: it builds a real
PDF and calls the real Claude API for the executive summary (ANTHROPIC_API_KEY
is a live key in this environment, not a placeholder). Every test below
answers "no" to "Email report when complete?" -- EMAIL_SENDER/EMAIL_PASSWORD
are also live credentials, and a test suite that emails a real inbox on
every run is not something to build. Kept to a small number of full
generations given the real API cost of each one; Custom range is used
throughout since it needs only two date prompts, unlike Monthly/Quarterly's
extra year/month picker layer.

Run: pytest tests/test_interactive_generate_report.py -v -s
"""
import glob
import os
import time

import pytest

from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Reports", "Custom")


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _enter_custom_range(child, start_str, end_str):
    child.sendline("10")
    child.expect("Select a period type:")
    child.send(DOWN)
    child.send(DOWN)
    child.send(DOWN)
    child.send(ENTER)  # Custom
    child.expect("Start date")
    child.sendline(start_str)
    child.expect("End date")
    child.sendline(end_str)


class TestHappyPath:
    def test_custom_range_report_generates_a_real_pdf(self):
        fd.create_sold_unit("REPORT-HAPPY-PATH")
        before = set(glob.glob(os.path.join(REPORTS_DIR, "*.pdf")))

        child = spawn_app(timeout=90)
        try:
            _enter_custom_range(child, "08-01-2026", "08-17-2026")
            child.expect("Email report when complete\\?", timeout=20)
            child.sendline("no")
            # Success falls straight back into the main menu loop -- no
            # "Returning to Main Menu" line, unlike the exception path.
            text = expect_clean(child, "Select an option \\(1-11\\)", timeout=90)
            assert "Report complete" in text
            assert ".pdf" in text
        finally:
            close(child)

        after = set(glob.glob(os.path.join(REPORTS_DIR, "*.pdf")))
        new_files = after - before
        assert new_files, "no new PDF appeared in Reports/Custom/"
        newest = max(new_files, key=os.path.getmtime)
        assert os.path.getsize(newest) > 0


class TestNoDataPeriod:
    def test_period_with_no_sales_still_completes(self):
        child = spawn_app(timeout=90)
        try:
            _enter_custom_range(child, "01-02-2022", "01-03-2022")
            child.expect("Email report when complete\\?", timeout=20)
            child.sendline("no")
            text = expect_clean(child, "Select an option \\(1-11\\)", timeout=90)
            assert "Report complete" in text
        finally:
            close(child)


class TestBackNavigation:
    def test_declining_at_period_type_returns_to_main_menu(self):
        child = spawn_app()
        try:
            child.sendline("10")
            child.expect("Select a period type:")
            child.send(DOWN)
            child.send(DOWN)
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # Return to Main Menu
            expect_clean(child, "Select an option \\(1-11\\)")
        finally:
            close(child)
