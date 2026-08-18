"""
Tier 2 -- Customer Insights (Op 9), driven through a real pty.

Read-only: no test in this file writes to the master sheet. All fixtures
here create the sale history first, then Customer Insights just reads it
back -- so unlike every operation tested so far, there are no
"declining leaves data unchanged" cases, since nothing here is ever
written in the first place.

Run: pytest tests/test_interactive_customer_insights.py -v -s
"""
import time
from datetime import date

import pytest

from tests import fixtures_data as fd
from tests.pexpect_helpers import DOWN, ENTER, close, expect_clean, require_test_mode, spawn_app

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

TODAY_STR = date.today().strftime("%m-%d-%Y")


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _enter_insights(child, search_query):
    child.sendline("9")
    child.expect("Customer search")
    child.sendline(search_query)


class TestHappyPath:
    def test_lifetime_summary_for_single_purchase_customer(self):
        name = f"Pytest Insights Single {int(time.time())}"
        unit = fd.create_sold_unit("INSIGHTS-SINGLE", customer_name=name)
        selling_price = unit["Selling Price (USD)"]

        child = spawn_app()
        try:
            _enter_insights(child, name)
            child.send(ENTER)  # only match -- select it

            text = expect_clean(child, "View a specific period for this customer\\?")
            assert "CUSTOMER PROFILE" in text
            assert name in text
            assert "Purchases:" in text
            assert "PURCHASE SUMMARY — ALL TIME" in text
            assert "Units Purchased:" in text
            assert f"${float(selling_price):,.2f}" in text
            assert "Lifetime Spend:" in text

            child.sendline("no")
            child.expect("Look up another customer\\?")
            child.sendline("no")
            expect_clean(child, "Returning to Main Menu")
        finally:
            close(child)


class TestMultiplePurchases:
    def test_two_purchases_aggregate_spend_and_units(self):
        name = f"Pytest Insights Multi {int(time.time())}"
        phone = fd._unique_customer_phone()
        unit1 = fd.create_sold_unit("INSIGHTS-MULTI-1", customer_name=name, phone=phone)
        unit2 = fd.create_sold_unit("INSIGHTS-MULTI-2", customer_name=name, phone=phone)
        total_spend = float(unit1["Selling Price (USD)"]) + float(unit2["Selling Price (USD)"])

        child = spawn_app()
        try:
            _enter_insights(child, name)
            child.send(ENTER)
            text = expect_clean(child, "View a specific period for this customer\\?")
            assert "Units Purchased:" in text
            assert f"${total_spend:,.2f}" in text
            child.sendline("no")
            child.expect("Look up another customer\\?")
            child.sendline("no")
        finally:
            close(child)


class TestOutstandingBalance:
    def test_partial_payment_shows_current_outstanding_balance(self):
        name = f"Pytest Insights Outstanding {int(time.time())}"
        unit = fd.create_partial_payment_unit(
            "INSIGHTS-OUTSTANDING", customer_name=name,
            amount_received=60.0, amount_outstanding=80.0,
        )
        child = spawn_app()
        try:
            _enter_insights(child, name)
            child.send(ENTER)
            text = expect_clean(child, "View a specific period for this customer\\?")
            assert "CURRENT OUTSTANDING BALANCE" in text
            assert unit["SKU"] in text
            assert "$80.00" in text
            child.sendline("no")
            child.expect("Look up another customer\\?")
            child.sendline("no")
        finally:
            close(child)


class TestNoMatch:
    def test_searching_unknown_customer_returns_to_main_menu(self):
        unknown_name = f"Pytest Nobody Nonexistent {int(time.time())}"
        child = spawn_app()
        try:
            _enter_insights(child, unknown_name)
            text = expect_clean(child, "Return to Main Menu")
            assert f"No existing record found for '{unknown_name}'" in text
            child.send(DOWN)  # "Search again" -> "Return to Main Menu"
            child.send(ENTER)
            expect_clean(child, "Returning to Main Menu")
        finally:
            close(child)


class TestPeriodScoped:
    def test_custom_range_excluding_purchase_shows_no_purchases_message(self):
        name = f"Pytest Insights Period Miss {int(time.time())}"
        fd.create_sold_unit("INSIGHTS-PERIOD-MISS", customer_name=name)
        child = spawn_app()
        try:
            _enter_insights(child, name)
            child.send(ENTER)
            child.expect("View a specific period for this customer\\?")
            child.sendline("yes")
            child.expect("Select a period type:")
            child.send(DOWN)
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # Custom
            child.expect("Start date")
            child.sendline("01-02-2022")
            child.expect("End date")
            child.sendline("01-03-2022")
            text = expect_clean(child, "View a different period\\?")
            assert f"No purchases by {name} in" in text
            child.sendline("no")
        finally:
            close(child)

    def test_custom_range_including_purchase_shows_total_spend_label(self):
        name = f"Pytest Insights Period Hit {int(time.time())}"
        unit = fd.create_sold_unit("INSIGHTS-PERIOD-HIT", customer_name=name)
        selling_price = float(unit["Selling Price (USD)"])
        child = spawn_app()
        try:
            _enter_insights(child, name)
            child.send(ENTER)
            child.expect("View a specific period for this customer\\?")
            child.sendline("yes")
            child.expect("Select a period type:")
            child.send(DOWN)
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # Custom
            child.expect("Start date")
            child.sendline("01-01-2026")
            child.expect("End date")
            child.sendline(TODAY_STR)
            text = expect_clean(child, "View a different period\\?")
            # Scoped view -- "Total Spend:", not the lifetime-only "Lifetime Spend:"
            assert "Total Spend:" in text
            assert f"${selling_price:,.2f}" in text
            assert "Lifetime Spend:" not in text
            child.sendline("no")
        finally:
            close(child)


class TestLookUpAnotherCustomerLoop:
    def test_answering_yes_loops_back_to_a_new_search(self):
        name = f"Pytest Insights Loop {int(time.time())}"
        fd.create_sold_unit("INSIGHTS-LOOP", customer_name=name)
        child = spawn_app()
        try:
            _enter_insights(child, name)
            child.send(ENTER)
            child.expect("View a specific period for this customer\\?")
            child.sendline("no")
            child.expect("Look up another customer\\?")
            child.sendline("yes")
            expect_clean(child, "Customer search")
        finally:
            close(child)
