"""
Tier 2 synthetic test-sheet fixtures.

Some scenarios (a blank Total Cost cell, a unit priced below its own cost)
can't be reached through the interactive app itself -- Add Inventory
requires cost fields, and check_pricing_warnings() only warns on a
below-cost price rather than blocking it, so a below-cost unit still needs
deliberate setup. These are real, documented anomalies the app is supposed
to handle gracefully (see the blank-cost-cell warnings added this session),
so they're worth testing even though they don't occur via normal use.

Each fixture is idempotent: it scans for a unit already carrying its unique
marker in Inventory Notes before creating a new one, so re-running the
suite doesn't pile up duplicate rows on every run. Uses inv.append_row()
directly (not the interactive flow) since this is test-data setup, not a
test of the add flow itself -- Add Inventory's own interactive behavior is
covered separately.

Follows the sheet's existing "TWV" / "TestWeave" convention already used
for other synthetic test rows (e.g. LAH-TWV100).
"""
import itertools
import time

import inventory as inv
from tests.pexpect_helpers import retry_on_quota

_phone_counter = itertools.count()
_PHONE_RUN_ID = int(time.time()) % 10_000


def _unique_customer_phone():
    """Each call to create_partial_payment_unit() represents a distinct
    logical customer -- a shared hardcoded phone number would collapse them
    into one _customer_identity_key() identity (phone+country_code takes
    priority over name), breaking any test that filters/searches by
    customer name. Mirrors the run-id + counter scheme in
    test_interactive_record_sale.py's _unique_phone()."""
    return f"555{_PHONE_RUN_ID:04d}{next(_phone_counter):03d}"

_BLANK_COST_MARKER = "[TEST-FIXTURE · BLANK-COST] synthetic unit for Tier 2 tests -- do not edit"
_BELOW_COST_MARKER = "[TEST-FIXTURE · BELOW-COST] synthetic unit for Tier 2 tests -- do not edit"
_BLANK_PRICE_MARKER = "[TEST-FIXTURE · BLANK-PRICE] synthetic unit for Tier 2 tests -- do not edit"

_CATEGORY_CODE = "TWV"
_WEAVE_TYPE = "TestWeave"


def _find_by_marker(marker):
    rows = retry_on_quota(inv.get_all_rows)
    for r in rows:
        if marker in (r.get("Inventory Notes") or ""):
            return r
    return None


def _next_sku():
    raw_rows = retry_on_quota(inv.get_raw_rows)
    return retry_on_quota(inv.generate_next_sku, _CATEGORY_CODE, raw_rows)


def _get_unit(sku):
    row_index = retry_on_quota(inv.find_row_index_by_sku, sku)
    return retry_on_quota(inv.get_row_by_sheet_index, row_index)


def _base_row_data(marker):
    return {
        "Category Code": _CATEGORY_CODE,
        "Weave Type / Cluster": _WEAVE_TYPE,
        "Source Sheet + Tab": "Test",
        "Supplier": "Boston guru",
        "Date Acquired": "07-11-2026",
        "Status": "Available",
        "Inventory Notes": marker,
        "Transaction Notes": "",
        "Discount %": "",
        "Selling Price (INR) - Provided": "",
    }


def get_or_create_blank_cost_unit():
    """An Available unit with a genuinely blank Total Cost (USD) cell --
    the exact data-integrity anomaly the blank-cost warning added this
    session exists to catch."""
    existing = _find_by_marker(_BLANK_COST_MARKER)
    if existing is not None:
        return existing

    row_data = _base_row_data(_BLANK_COST_MARKER)
    row_data.update({
        "Base Price + GST Tax (INR)": "",
        "Shipping Cost (INR)": "",
        "Design Detailing Cost (INR)": "",
        "Total Cost (INR)": "",
        "Total Cost (USD)": "",
        "Selling Price (USD)": 199.99,
        "Actual Selling Price (USD)": 199.99,
        "Gross Profit (USD)": "",
        "Markup %": "",
        "(Profit) Margin %": "",
    })
    sku = _next_sku()
    row_data["SKU"] = sku
    retry_on_quota(inv.append_row, row_data)
    return _find_by_marker(_BLANK_COST_MARKER)


def get_or_create_blank_price_unit():
    """An Available unit with real cost data but a blank Selling Price
    (USD) cell -- distinct from the blank-cost fixture above. Manage
    Reservation's blank-price warning checks Selling Price specifically
    (it's what gets shown/confirmed in the reservation summary), not
    Total Cost, so this needs its own fixture rather than reusing that
    one."""
    existing = _find_by_marker(_BLANK_PRICE_MARKER)
    if existing is not None:
        return existing

    row_data = _base_row_data(_BLANK_PRICE_MARKER)
    row_data.update({
        "Base Price + GST Tax (INR)": 12000.0,
        "Shipping Cost (INR)": 750.0,
        "Design Detailing Cost (INR)": 0.0,
        "Total Cost (INR)": 12750.0,
        "Total Cost (USD)": 133.86,
        "Selling Price (USD)": "",
        "Actual Selling Price (USD)": "",
        "Gross Profit (USD)": "",
        "Markup %": "",
        "(Profit) Margin %": "",
    })
    sku = _next_sku()
    row_data["SKU"] = sku
    retry_on_quota(inv.append_row, row_data)
    return _find_by_marker(_BLANK_PRICE_MARKER)


def get_or_create_below_cost_unit():
    """An Available unit deliberately priced below its own cost --
    check_pricing_warnings() allows this through with a warning rather than
    blocking it (a clearance/loss-leader piece is a legitimate business
    choice), so it's reachable data, not just a corruption case."""
    existing = _find_by_marker(_BELOW_COST_MARKER)
    if existing is not None:
        return existing

    row_data = _base_row_data(_BELOW_COST_MARKER)
    total_cost_usd = 300.0
    selling_price_usd = 250.0  # below the $300 cost, on purpose
    row_data.update({
        "Base Price + GST Tax (INR)": 24000.0,
        "Shipping Cost (INR)": 750.0,
        "Design Detailing Cost (INR)": 0.0,
        "Total Cost (INR)": 24750.0,
        "Total Cost (USD)": total_cost_usd,
        "Selling Price (USD)": selling_price_usd,
        "Actual Selling Price (USD)": selling_price_usd,
        "Gross Profit (USD)": round(selling_price_usd - total_cost_usd, 2),
        "Markup %": round((selling_price_usd - total_cost_usd) / total_cost_usd, 6),
        "(Profit) Margin %": round((selling_price_usd - total_cost_usd) / selling_price_usd, 6),
    })
    sku = _next_sku()
    row_data["SKU"] = sku
    retry_on_quota(inv.append_row, row_data)
    return _find_by_marker(_BELOW_COST_MARKER)


def create_fresh_available_unit(label, status="Available"):
    """Always creates a brand-new row (not idempotent, unlike the fixtures
    above) -- callers that are about to *mutate* a unit (Edit Inventory
    Details, Reprice, etc.) need their own unit each time, not a shared one
    other tests might be reading concurrently or a stale one left over from
    a prior run with values earlier assertions already depended on."""
    marker = f"[TEST-FIXTURE · {label}] synthetic unit for Tier 2 tests"
    total_cost_inr = 12750.0
    ecb_rate = 95.25  # matches the fixed Date Acquired below, for a stable Total Cost (USD)
    total_cost_usd = round(total_cost_inr / ecb_rate, 2)
    selling_price_usd = round(total_cost_usd * 1.6, 2)  # 60% markup
    row_data = {
        "Category Code": _CATEGORY_CODE,
        "Weave Type / Cluster": _WEAVE_TYPE,
        "Source Sheet + Tab": "Test",
        "Supplier": "Boston guru",
        "Date Acquired": "07-01-2026",
        "Base Price + GST Tax (INR)": 12000.0,
        "Shipping Cost (INR)": 750.0,
        "Design Detailing Cost (INR)": 0.0,
        "Total Cost (INR)": total_cost_inr,
        "Total Cost (USD)": total_cost_usd,
        "Selling Price (USD)": selling_price_usd,
        "Actual Selling Price (USD)": selling_price_usd,
        "Gross Profit (USD)": round(selling_price_usd - total_cost_usd, 2),
        "Markup %": round((selling_price_usd - total_cost_usd) / total_cost_usd, 6),
        "(Profit) Margin %": round((selling_price_usd - total_cost_usd) / selling_price_usd, 6),
        "Status": status,
        "Inventory Notes": marker,
        "Transaction Notes": "",
        "Discount %": "",
        "Selling Price (INR) - Provided": "",
    }
    sku = _next_sku()
    row_data["SKU"] = sku
    retry_on_quota(inv.append_row, row_data)
    return _get_unit(sku)


def create_sold_unit(label, customer_name="Pytest Sold Customer", phone=None):
    """A fully-paid Sold unit with real customer/sale data -- Cancel a Sale
    needs Customer Name and Date Sold populated (unlike
    create_fresh_available_unit(), which only sets Status for a unit still
    awaiting a sale), and Amount Received/Outstanding left blank matches
    record_sale()'s own convention for a paid-in-full sale."""
    marker = f"[TEST-FIXTURE · {label}] synthetic unit for Tier 2 tests"
    total_cost_inr = 12750.0
    ecb_rate = 95.25
    total_cost_usd = round(total_cost_inr / ecb_rate, 2)
    selling_price_usd = round(total_cost_usd * 1.6, 2)
    row_data = {
        "Category Code": _CATEGORY_CODE,
        "Weave Type / Cluster": _WEAVE_TYPE,
        "Source Sheet + Tab": "Test",
        "Supplier": "Boston guru",
        "Date Acquired": "07-01-2026",
        "Base Price + GST Tax (INR)": 12000.0,
        "Shipping Cost (INR)": 750.0,
        "Design Detailing Cost (INR)": 0.0,
        "Total Cost (INR)": total_cost_inr,
        "Total Cost (USD)": total_cost_usd,
        "Selling Price (USD)": selling_price_usd,
        "Actual Selling Price (USD)": selling_price_usd,
        "Gross Profit (USD)": round(selling_price_usd - total_cost_usd, 2),
        "Markup %": round((selling_price_usd - total_cost_usd) / total_cost_usd, 6),
        "(Profit) Margin %": round((selling_price_usd - total_cost_usd) / selling_price_usd, 6),
        "Status": "Sold",
        "Date Sold": "08-01-2026",
        "Sales Channel": "Exhibition/Popup",
        "Customer Name": customer_name,
        "Customer Country Code": "+1",
        "Customer Phone": phone or _unique_customer_phone(),
        "Customer City": "New York",
        "Customer State": "New York",
        "Customer Country": "United States",
        "Amount Received (USD)": "",
        "Amount Outstanding (USD)": "",
        "Inventory Notes": marker,
        "Transaction Notes": f"[08-01-2026 · SALE] Sold to {customer_name} for ${selling_price_usd:,.2f}.",
        "Discount %": "",
        "Selling Price (INR) - Provided": "",
    }
    sku = _next_sku()
    row_data["SKU"] = sku
    retry_on_quota(inv.append_row, row_data)
    return _get_unit(sku)


def create_sold_unit_blank_total_cost(label, customer_name="Pytest Blank Cost Customer"):
    """A Sold unit (Customer Name/Date Sold populated, like create_sold_unit())
    but with a genuinely blank Total Cost (USD) cell -- Cancel a Sale's
    blank-cost warning fires on Total Cost or Selling Price specifically for
    a unit that's already passed the Sold/Partial-Payment status check, which
    get_or_create_blank_cost_unit() (an Available unit) can't reach."""
    marker = f"[TEST-FIXTURE · {label}] synthetic unit for Tier 2 tests"
    selling_price_usd = 199.99
    row_data = {
        "Category Code": _CATEGORY_CODE,
        "Weave Type / Cluster": _WEAVE_TYPE,
        "Source Sheet + Tab": "Test",
        "Supplier": "Boston guru",
        "Date Acquired": "07-01-2026",
        "Base Price + GST Tax (INR)": "",
        "Shipping Cost (INR)": "",
        "Design Detailing Cost (INR)": "",
        "Total Cost (INR)": "",
        "Total Cost (USD)": "",
        "Selling Price (USD)": selling_price_usd,
        "Actual Selling Price (USD)": selling_price_usd,
        "Gross Profit (USD)": "",
        "Markup %": "",
        "(Profit) Margin %": "",
        "Status": "Sold",
        "Date Sold": "08-01-2026",
        "Sales Channel": "Exhibition/Popup",
        "Customer Name": customer_name,
        "Customer Country Code": "+1",
        "Customer Phone": _unique_customer_phone(),
        "Customer City": "New York",
        "Customer State": "New York",
        "Customer Country": "United States",
        "Amount Received (USD)": "",
        "Amount Outstanding (USD)": "",
        "Inventory Notes": marker,
        "Transaction Notes": f"[08-01-2026 · SALE] Sold to {customer_name} for ${selling_price_usd:,.2f}.",
        "Discount %": "",
        "Selling Price (INR) - Provided": "",
    }
    sku = _next_sku()
    row_data["SKU"] = sku
    retry_on_quota(inv.append_row, row_data)
    return _get_unit(sku)


def create_partial_payment_unit(label, customer_name="Pytest Outstanding Customer",
                                 amount_received=100.0, amount_outstanding=114.18, phone=None):
    """A Sold - Partial Payment unit with real customer/payment data --
    Record Outstanding Payment and Cancel a Sale both need Customer Name,
    Date Sold, and populated Amount Received/Outstanding, none of which
    create_fresh_available_unit() sets (it's meant for units still awaiting
    a sale, not ones with a transaction already on them)."""
    marker = f"[TEST-FIXTURE · {label}] synthetic unit for Tier 2 tests"
    total_cost_inr = 12750.0
    ecb_rate = 95.25
    total_cost_usd = round(total_cost_inr / ecb_rate, 2)
    selling_price_usd = round(amount_received + amount_outstanding, 2)
    row_data = {
        "Category Code": _CATEGORY_CODE,
        "Weave Type / Cluster": _WEAVE_TYPE,
        "Source Sheet + Tab": "Test",
        "Supplier": "Boston guru",
        "Date Acquired": "07-01-2026",
        "Base Price + GST Tax (INR)": 12000.0,
        "Shipping Cost (INR)": 750.0,
        "Design Detailing Cost (INR)": 0.0,
        "Total Cost (INR)": total_cost_inr,
        "Total Cost (USD)": total_cost_usd,
        "Selling Price (USD)": selling_price_usd,
        "Actual Selling Price (USD)": selling_price_usd,
        "Gross Profit (USD)": round(selling_price_usd - total_cost_usd, 2),
        "Markup %": round((selling_price_usd - total_cost_usd) / total_cost_usd, 6),
        "(Profit) Margin %": round((selling_price_usd - total_cost_usd) / selling_price_usd, 6),
        "Status": "Sold - Partial Payment",
        "Date Sold": "08-01-2026",
        "Sales Channel": "Exhibition/Popup",
        "Customer Name": customer_name,
        "Customer Country Code": "+1",
        "Customer Phone": phone or _unique_customer_phone(),
        "Customer City": "New York",
        "Customer State": "New York",
        "Customer Country": "United States",
        "Amount Received (USD)": amount_received,
        "Amount Outstanding (USD)": amount_outstanding,
        "Inventory Notes": marker,
        "Transaction Notes": f"[08-01-2026 · SALE] Initial partial payment of ${amount_received:,.2f} received via Cash. ${amount_outstanding:,.2f} outstanding.",
        "Discount %": "",
        "Selling Price (INR) - Provided": "",
    }
    sku = _next_sku()
    row_data["SKU"] = sku
    retry_on_quota(inv.append_row, row_data)
    return _get_unit(sku)
