"""
Tier 1 unit tests -- pure helper functions in generate_report.py.

Scope: same rule as test_inventory_helpers.py -- no PDF rendering, no Claude
API calls, no live Sheets reads. generate_report.py keeps its own
independent copies of several inventory.py helpers (it's a separate entry
point with its own data-loading path), so these are tested separately even
where the logic mirrors inventory.py's.

Run: pytest tests/test_report_helpers.py -v
"""
from datetime import date

import pytest

import generate_report as gr


# ── _flt ────────────────────────────────────────────────────────────────────────
class TestFlt:
    def test_plain_number(self):
        assert gr._flt("250.00") == 250.0

    def test_comma_formatted(self):
        assert gr._flt("1,250.00") == 1250.0

    def test_dollar_and_percent_signs_stripped(self):
        assert gr._flt("$1,250.00") == 1250.0
        assert gr._flt("15%") == 15.0

    def test_blank_and_none_return_zero(self):
        assert gr._flt("") == 0.0
        assert gr._flt(None) == 0.0

    def test_malformed_returns_zero_not_raise(self):
        assert gr._flt("N/A") == 0.0


# ── _parse_date ──────────────────────────────────────────────────────────────────
class TestParseDate:
    def test_valid(self):
        assert gr._parse_date("08-17-2026") == date(2026, 8, 17)

    def test_malformed_returns_none(self):
        assert gr._parse_date("not a date") is None
        assert gr._parse_date("") is None
        assert gr._parse_date(None) is None


# ── _in (date within range) ──────────────────────────────────────────────────────
class TestIn:
    def test_within_range(self):
        assert gr._in(date(2026, 8, 15), date(2026, 8, 1), date(2026, 8, 31)) is True

    def test_boundaries_inclusive(self):
        assert gr._in(date(2026, 8, 1), date(2026, 8, 1), date(2026, 8, 31)) is True
        assert gr._in(date(2026, 8, 31), date(2026, 8, 1), date(2026, 8, 31)) is True

    def test_outside_range(self):
        assert gr._in(date(2026, 9, 1), date(2026, 8, 1), date(2026, 8, 31)) is False

    def test_none_date_is_false(self):
        assert gr._in(None, date(2026, 8, 1), date(2026, 8, 31)) is False


# ── _days_since (report's own copy, unattended-run semantics) ────────────────────
class TestDaysSince:
    def test_past_date(self):
        assert gr._days_since(date(2026, 8, 1), date(2026, 8, 17)) == 16

    def test_none_target_returns_none(self):
        assert gr._days_since(None) is None

    def test_future_date_returns_none_not_negative(self):
        assert gr._days_since(date(2026, 9, 1), date(2026, 8, 17)) is None


# ── _customer_key ────────────────────────────────────────────────────────────────
class TestCustomerKey:
    def test_phone_and_code_take_priority(self):
        row = {"Customer Name": "Priya Sharma", "Customer Country Code": "+1",
               "Customer Phone": "5551234567", "Customer Email": "priya@example.com"}
        assert gr._customer_key(row) == ("phone", "1", "5551234567")

    def test_two_different_people_same_name(self):
        row1 = {"Customer Name": "Priya Sharma", "Customer Country Code": "1", "Customer Phone": "5551111111"}
        row2 = {"Customer Name": "Priya Sharma", "Customer Country Code": "1", "Customer Phone": "5552222222"}
        assert gr._customer_key(row1) != gr._customer_key(row2)

    def test_falls_back_to_email(self):
        row = {"Customer Name": "Priya Sharma", "Customer Email": "Priya@Example.com"}
        assert gr._customer_key(row) == ("email", "priya@example.com")

    def test_falls_back_to_name(self):
        row = {"Customer Name": "Priya Sharma"}
        assert gr._customer_key(row) == ("name", "Priya Sharma")

    def test_missing_keys_do_not_raise(self):
        assert gr._customer_key({}) == ("name", "")


# ── _usd / _usd_k / _pct ─────────────────────────────────────────────────────────
class TestFormatting:
    def test_usd_basic(self):
        assert gr._usd(1234.5) == "$1,234.50"

    def test_usd_deduction_style_nonzero(self):
        assert gr._usd(1234.5, deduction=True) == "($1,234.50)"

    def test_usd_deduction_style_zero_no_parens(self):
        assert gr._usd(0, deduction=True) == "$0.00"

    def test_usd_k_under_thousand(self):
        assert gr._usd_k(467) == "$467"

    def test_usd_k_thousands(self):
        assert gr._usd_k(24300) == "$24.3K"

    def test_usd_k_millions(self):
        assert gr._usd_k(1_200_000) == "$1.2M"

    def test_usd_k_boundary_rounds_up_to_million_label(self):
        # 999,975 rounds to 1000.0K at 1-decimal precision -- must display
        # as $1.0M, not the nonsensical "$1000.0K".
        assert gr._usd_k(999_975) == "$1.0M"

    def test_pct(self):
        assert gr._pct(0.153) == "15.3%"


# ── _xml_escape ──────────────────────────────────────────────────────────────────
class TestXmlEscape:
    def test_escapes_ampersand_first(self):
        assert gr._xml_escape("Smith & Co") == "Smith &amp; Co"

    def test_escapes_angle_brackets(self):
        assert gr._xml_escape("<script>") == "&lt;script&gt;"

    def test_does_not_double_escape_ampersand_in_entities(self):
        # & must be replaced before < and > so we don't mangle the entities
        # we just created.
        result = gr._xml_escape("A < B & C > D")
        assert result == "A &lt; B &amp; C &gt; D"

    def test_plain_text_untouched(self):
        assert gr._xml_escape("Priya Sharma") == "Priya Sharma"

    def test_non_string_input_coerced(self):
        assert gr._xml_escape(42) == "42"


# ── _channels_ready ──────────────────────────────────────────────────────────────
class TestChannelsReady:
    def test_below_threshold_not_ready(self):
        ch = {"Unknown": {"count": 60}, "Instagram": {"count": 40}}
        assert gr._channels_ready(ch, 100) is False

    def test_above_threshold_ready(self):
        ch = {"Unknown": {"count": 10}, "Instagram": {"count": 90}}
        assert gr._channels_ready(ch, 100) is True

    def test_empty_channels_not_ready(self):
        assert gr._channels_ready({}, 100) is False

    def test_zero_units_not_ready(self):
        assert gr._channels_ready({"Instagram": {"count": 0}}, 0) is False


# ── _prior (comparison period calculation) ────────────────────────────────────────
class TestPrior:
    def test_monthly_normal(self):
        ps, pe = gr._prior("monthly", date(2026, 8, 1), date(2026, 8, 31))
        assert ps == date(2026, 7, 1)
        assert pe == date(2026, 7, 31)

    def test_monthly_january_wraps_to_prior_december(self):
        ps, pe = gr._prior("monthly", date(2026, 1, 1), date(2026, 1, 31))
        assert ps == date(2025, 12, 1)
        assert pe == date(2025, 12, 31)

    def test_quarterly_normal(self):
        ps, pe = gr._prior("quarterly", date(2026, 4, 1), date(2026, 6, 30))
        assert ps == date(2026, 1, 1)
        assert pe == date(2026, 3, 31)

    def test_quarterly_q1_wraps_to_prior_year_q4(self):
        ps, pe = gr._prior("quarterly", date(2026, 1, 1), date(2026, 3, 31))
        assert ps == date(2025, 10, 1)
        assert pe == date(2025, 12, 31)

    def test_annual(self):
        ps, pe = gr._prior("annual", date(2026, 1, 1), date(2026, 12, 31))
        assert ps == date(2025, 1, 1)
        assert pe == date(2025, 12, 31)

    def test_custom_same_calendar_dates_prior_year(self):
        ps, pe = gr._prior("custom", date(2026, 3, 15), date(2026, 8, 20))
        assert ps == date(2025, 3, 15)
        assert pe == date(2025, 8, 20)

    def test_custom_leap_day_falls_back_to_feb_28(self):
        # 2028 is a leap year; 2027 is not -- Feb 29 has no equivalent.
        ps, pe = gr._prior("custom", date(2028, 2, 29), date(2028, 2, 29))
        assert ps == date(2027, 2, 28)
        assert pe == date(2027, 2, 28)

    def test_unrecognized_period_type_raises_loudly(self):
        # Deliberately not a silent fallback to monthly -- see the
        # function's own docstring/comment for why.
        with pytest.raises(ValueError):
            gr._prior("weekly", date(2026, 8, 1), date(2026, 8, 7))
