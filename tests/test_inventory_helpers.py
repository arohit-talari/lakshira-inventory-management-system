"""
Tier 1 unit tests -- pure helper functions in inventory.py.

Scope: only functions with no terminal I/O (no questionary/input()) and no
live Google Sheets calls, so these run in milliseconds with no credentials
and no network. Every case here traces back to either a real bug fixed
this session or a documented invariant the function's docstring/comments
claim to hold -- not speculative edge cases.

Run: pytest tests/test_inventory_helpers.py -v
"""
from datetime import date

import pytest

import inventory as inv


# ── _sheet_float ──────────────────────────────────────────────────────────────
# The bug: plain float(x or 0) crashes on comma-formatted numbers like
# "1,250.00" -- a real, reachable crash in record_outstanding_payment() for
# any balance >= $1,000. _sheet_float() is the fix; these cases pin that down.
class TestSheetFloat:
    def test_plain_number_string(self):
        assert inv._sheet_float("250.00") == 250.0

    def test_comma_formatted_thousands(self):
        assert inv._sheet_float("1,250.00") == 1250.0

    def test_comma_formatted_large(self):
        assert inv._sheet_float("12,345.67") == 12345.67

    def test_blank_string_returns_default(self):
        assert inv._sheet_float("") == 0.0

    def test_none_returns_default(self):
        assert inv._sheet_float(None) == 0.0

    def test_custom_default(self):
        assert inv._sheet_float("", default=-1.0) == -1.0
        assert inv._sheet_float(None, default=None) is None

    def test_malformed_text_returns_default(self):
        assert inv._sheet_float("N/A") == 0.0
        assert inv._sheet_float("#REF!") == 0.0

    def test_already_numeric_types_pass_through(self):
        # get_all_records() type-inference can hand back a real int/float
        # for a numeric-looking cell instead of a string.
        assert inv._sheet_float(999) == 999.0
        assert inv._sheet_float(1250.5) == 1250.5

    def test_whitespace_padded(self):
        assert inv._sheet_float("  42.00  ") == 42.0


# ── _safe_parse_date ──────────────────────────────────────────────────────────
class TestSafeParseDate:
    def test_valid_date(self):
        assert inv._safe_parse_date("08-17-2026") == date(2026, 8, 17)

    def test_blank_returns_none(self):
        assert inv._safe_parse_date("") is None
        assert inv._safe_parse_date(None) is None
        assert inv._safe_parse_date("   ") is None

    def test_malformed_returns_none_not_raise(self):
        assert inv._safe_parse_date("not a date") is None
        assert inv._safe_parse_date("2026-08-17") is None  # wrong format (ISO, not MM-DD-YYYY)
        assert inv._safe_parse_date("13-45-2026") is None  # invalid month/day

    def test_numeric_type_from_sheet_type_inference(self):
        # Should never raise even if handed a non-string.
        assert inv._safe_parse_date(20260817) is None


# ── _days_since ────────────────────────────────────────────────────────────────
# The bug: (date.today() - some_date).days goes negative when some_date is
# in the future (only reachable via a hand-edited sheet), silently producing
# a nonsensical "-40 days ago". _days_since() must return None instead.
class TestDaysSince:
    def test_none_target_returns_none(self):
        assert inv._days_since(None) is None

    def test_past_date_with_explicit_reference(self):
        target = date(2026, 8, 1)
        reference = date(2026, 8, 17)
        assert inv._days_since(target, reference) == 16

    def test_same_day_returns_zero(self):
        d = date(2026, 8, 17)
        assert inv._days_since(d, d) == 0

    def test_future_date_returns_none_not_negative(self):
        target = date(2026, 9, 1)
        reference = date(2026, 8, 17)
        assert inv._days_since(target, reference) is None

    def test_defaults_to_today_when_reference_omitted(self):
        assert inv._days_since(date.today()) == 0


# ── _sheet_safe (formula-injection guard) ──────────────────────────────────────
# The bug: value_input_option="USER_ENTERED" makes Sheets interpret any
# string starting with =, +, -, or @ as a formula -- plausible in normal use
# ("-10% discount noted", an "@handle" contact). _sheet_safe() must escape
# exactly those four leading characters and nothing else.
class TestSheetSafe:
    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
    def test_each_trigger_character_gets_escaped(self, trigger):
        value = f"{trigger}something"
        result = inv._sheet_safe(value)
        assert result == f"'{value}"

    def test_realistic_discount_note(self):
        assert inv._sheet_safe("-10% additional discount given") == "'-10% additional discount given"

    def test_realistic_instagram_handle(self):
        assert inv._sheet_safe("@lakshira_customer") == "'@lakshira_customer"

    def test_normal_text_untouched(self):
        assert inv._sheet_safe("Priya Sharma") == "Priya Sharma"

    def test_apostrophe_in_middle_untouched(self):
        assert inv._sheet_safe("O'Brien") == "O'Brien"

    def test_trigger_character_not_at_start_untouched(self):
        assert inv._sheet_safe("Total: -50") == "Total: -50"

    def test_non_string_types_pass_through(self):
        assert inv._sheet_safe(42) == 42
        assert inv._sheet_safe(42.5) == 42.5
        assert inv._sheet_safe(None) is None

    def test_blank_string_untouched(self):
        assert inv._sheet_safe("") == ""


# ── calculate_pricing ───────────────────────────────────────────────────────────
class TestCalculatePricing:
    def test_markup_path_basic(self):
        # total_cost_inr=10000, ecb_rate=83 -> total_cost_usd=120.48
        result = inv.calculate_pricing(10000, 83, "markup", 60)  # 60% markup
        assert result["total_cost_usd"] == pytest.approx(120.48, abs=0.01)
        assert result["markup_pct"] == pytest.approx(60.0, abs=0.1)
        assert result["discount_pct"] is None
        assert result["actual_selling_price_usd"] == result["selling_price_usd"]

    def test_usd_path_basic(self):
        result = inv.calculate_pricing(10000, 83, "usd", 200)
        assert result["selling_price_usd"] == 200
        assert result["gross_profit_usd"] == pytest.approx(200 - 120.48, abs=0.01)

    def test_discount_applied_reduces_actual_price(self):
        result = inv.calculate_pricing(10000, 83, "usd", 200, discount_pct=10)
        assert result["actual_selling_price_usd"] == pytest.approx(180.0, abs=0.01)
        assert result["discount_pct"] == 10

    def test_zero_discount_pct_treated_as_none(self):
        result = inv.calculate_pricing(10000, 83, "usd", 200, discount_pct=0)
        assert result["discount_pct"] is None
        assert result["actual_selling_price_usd"] == result["selling_price_usd"]

    def test_zero_total_cost_does_not_crash_markup_division(self):
        # A blank/zero Total Cost cell must degrade to 0%, not raise ZeroDivisionError.
        result = inv.calculate_pricing(0, 83, "usd", 200)
        assert result["markup_pct"] == 0

    def test_zero_actual_selling_price_does_not_crash_margin_division(self):
        # 100% discount -> actual_selling_price_usd == 0.
        result = inv.calculate_pricing(10000, 83, "usd", 200, discount_pct=100)
        assert result["actual_selling_price_usd"] == 0
        assert result["margin_pct"] == 0

    def test_below_cost_produces_negative_gross_profit(self):
        result = inv.calculate_pricing(10000, 83, "usd", 50)  # sells below the ~$120 cost
        assert result["gross_profit_usd"] < 0


# ── generate_next_sku ──────────────────────────────────────────────────────────
class TestGenerateNextSku:
    HEADER = ["SKU"] + [""] * 38

    def _row(self, sku):
        row = [""] * 39
        row[0] = sku
        return row

    def test_empty_sheet_starts_at_100(self):
        raw_rows = [self.HEADER]
        assert inv.generate_next_sku("KKVSV", raw_rows) == "LAH-KKVSV100"

    def test_increments_past_highest_existing_serial(self):
        raw_rows = [self.HEADER, self._row("LAH-KKVSV100"), self._row("LAH-KKVSV105")]
        assert inv.generate_next_sku("KKVSV", raw_rows) == "LAH-KKVSV106"

    def test_ignores_other_category_codes(self):
        raw_rows = [self.HEADER, self._row("LAH-BENAS500")]
        assert inv.generate_next_sku("KKVSV", raw_rows) == "LAH-KKVSV100"

    def test_ignores_non_numeric_suffix(self):
        raw_rows = [self.HEADER, self._row("LAH-KKVSV-DRAFT")]
        assert inv.generate_next_sku("KKVSV", raw_rows) == "LAH-KKVSV100"

    def test_short_rows_do_not_crash(self):
        raw_rows = [self.HEADER, []]
        assert inv.generate_next_sku("KKVSV", raw_rows) == "LAH-KKVSV100"


# ── _customer_identity_key ──────────────────────────────────────────────────────
# The bug: matching customers by Customer Name alone merges two unrelated
# people who share a name. Priority order: phone+country_code > email > name.
class TestCustomerIdentityKey:
    def test_phone_and_code_take_priority(self):
        key = inv._customer_identity_key("Priya Sharma", "+1", "5551234567", "priya@example.com")
        assert key == ("phone", "1", "5551234567")

    def test_two_different_people_same_name_different_phone(self):
        key1 = inv._customer_identity_key("Priya Sharma", "+1", "5551234567")
        key2 = inv._customer_identity_key("Priya Sharma", "+1", "5559999999")
        assert key1 != key2

    def test_falls_back_to_email_when_no_phone(self):
        key = inv._customer_identity_key("Priya Sharma", "", "", "Priya@Example.com")
        assert key == ("email", "priya@example.com")  # lowercased

    def test_falls_back_to_name_when_nothing_else(self):
        key = inv._customer_identity_key("Priya Sharma")
        assert key == ("name", "Priya Sharma")

    def test_coerces_numeric_type_inference_from_sheet(self):
        # gspread get_all_records() can hand back an int for a purely-numeric
        # country code cell instead of a string -- must not raise.
        key = inv._customer_identity_key("Priya Sharma", 1, 5551234567, "")
        assert key == ("phone", "1", "5551234567")

    def test_phone_formatting_characters_stripped(self):
        key1 = inv._customer_identity_key("Priya Sharma", "+1", "(555) 123-4567")
        key2 = inv._customer_identity_key("Priya Sharma", "1", "5551234567")
        assert key1 == key2

    def test_blank_inputs_do_not_raise(self):
        assert inv._customer_identity_key("") == ("name", "")
        assert inv._customer_identity_key(None) == ("name", "")


# ── _looks_like_phone_query ──────────────────────────────────────────────────────
class TestLooksLikePhoneQuery:
    def test_plain_digits(self):
        assert inv._looks_like_phone_query("5551234567") is True

    def test_punctuated_phone_number(self):
        assert inv._looks_like_phone_query("(555) 123-4567") is True
        assert inv._looks_like_phone_query("+1-555-123-4567") is True

    def test_plain_name(self):
        assert inv._looks_like_phone_query("Priya Sharma") is False

    def test_blank_query(self):
        assert inv._looks_like_phone_query("") is False


# ── _reservation_days ──────────────────────────────────────────────────────────
class TestReservationDays:
    def test_recent_not_expired(self):
        recent = (date.today())
        days, expired = inv._reservation_days(recent.strftime("%m-%d-%Y"))
        assert days == 0
        assert expired is False

    def test_blank_returns_none_not_expired(self):
        days, expired = inv._reservation_days("")
        assert days is None
        assert expired is False

    def test_malformed_returns_none_not_expired(self):
        days, expired = inv._reservation_days("not a date")
        assert days is None
        assert expired is False


# ── get_margin_color / get_markup_color / get_status_color ──────────────────────
class TestColorTiers:
    def test_margin_color_boundaries(self):
        assert inv.get_margin_color(14.99) == "\033[31m"   # red, below 15
        assert inv.get_margin_color(15.0) == "\033[93m"    # yellow, exactly at 15
        assert inv.get_margin_color(19.99) == "\033[93m"   # yellow, below 20
        assert inv.get_margin_color(20.0) == "\033[32m"    # green, exactly at 20

    def test_markup_color_boundaries(self):
        assert inv.get_markup_color(9.99) == "\033[91m"
        assert inv.get_markup_color(10.0) == "\033[38;5;208m"
        assert inv.get_markup_color(40.0) == "\033[32m"
        assert inv.get_markup_color(40.01) == "\033[38;5;22m"

    def test_status_colors_known_values(self):
        assert inv.get_status_color("Available") == "\033[32m"
        assert inv.get_status_color("Sold") == "\033[31m"
        assert inv.get_status_color("Reserved") == "\033[38;5;75m"
        assert inv.get_status_color("Sold - Partial Payment") == "\033[38;5;208m"
        assert inv.get_status_color("Unassigned") == "\033[2m"

    def test_status_color_unknown_status_returns_blank_not_raise(self):
        assert inv.get_status_color("Some Future Status") == ""


# ── signed ──────────────────────────────────────────────────────────────────────
class TestSigned:
    def test_positive(self):
        assert inv.signed(5) == "+"

    def test_zero_is_positive_sign(self):
        assert inv.signed(0) == "+"

    def test_negative(self):
        assert inv.signed(-5) == "-"


# ── _fmt_dial_code ──────────────────────────────────────────────────────────────
class TestFmtDialCode:
    def test_adds_plus_when_missing(self):
        assert inv._fmt_dial_code("1") == "+1"

    def test_leaves_existing_plus_alone(self):
        assert inv._fmt_dial_code("+1") == "+1"

    def test_blank_returns_blank(self):
        assert inv._fmt_dial_code("") == ""
        assert inv._fmt_dial_code(None) == ""


# ── _clip / _visible_len ──────────────────────────────────────────────────────────
class TestClipAndVisibleLen:
    def test_clip_leaves_short_strings_alone(self):
        assert inv._clip("Priya", 20) == "Priya"

    def test_clip_truncates_long_strings_with_ellipsis(self):
        result = inv._clip("A Very Long Customer Name Indeed", 10)
        assert result == "A Very Lo…"
        assert len(result) == 10

    def test_visible_len_ignores_ansi_codes(self):
        colored = "\033[92m+$50.00\033[0m"
        assert inv._visible_len(colored) == len("+$50.00")


# ── _strip_reservation_note / _get_reserver_name ──────────────────────────────────
class TestReservationNoteParsing:
    def test_strip_removes_reservation_and_rsvp_lines(self):
        notes = (
            "[08-01-2026 · NOTE] Called ahead\n"
            "[08-01-2026 · RESERVATION] Reserved by Priya Sharma — 5551234567\n"
            "[08-01-2026 · RSVP] Hold until Friday"
        )
        result = inv._strip_reservation_note(notes)
        assert "RESERVATION" not in result
        assert "RSVP" not in result
        assert "Called ahead" in result

    def test_strip_blank_notes_returns_blank(self):
        assert inv._strip_reservation_note("") == ""
        assert inv._strip_reservation_note(None) == ""

    def test_get_reserver_name_extracts_name_before_dash(self):
        notes = "[08-01-2026 · RESERVATION] Reserved by Priya Sharma — 5551234567"
        assert inv._get_reserver_name(notes) == "Priya Sharma"

    def test_get_reserver_name_no_contact_info(self):
        notes = "[08-01-2026 · RESERVATION] Reserved by Priya Sharma"
        assert inv._get_reserver_name(notes) == "Priya Sharma"

    def test_get_reserver_name_returns_none_when_absent(self):
        assert inv._get_reserver_name("Just a regular note") is None
        assert inv._get_reserver_name("") is None


# ── get_unassigned_skus_for_category ──────────────────────────────────────────────
class TestGetUnassignedSkusForCategory:
    def _row(self, sku, category, status):
        row = [""] * 39
        row[inv.COLUMNS["SKU"] - 1] = sku
        row[inv.COLUMNS["Category Code"] - 1] = category
        row[inv.COLUMNS["Status"] - 1] = status
        return row

    def test_filters_by_category_and_status(self, capsys):
        raw_rows = [
            ["header"] * 39,
            self._row("LAH-KKVSV100", "KKVSV", "Unassigned"),
            self._row("LAH-KKVSV101", "KKVSV", "Available"),
            self._row("LAH-BENAS100", "BENAS", "Unassigned"),
        ]
        result = inv.get_unassigned_skus_for_category("KKVSV", raw_rows)
        assert result == ["LAH-KKVSV100"]

    def test_no_matches_returns_empty_list(self, capsys):
        raw_rows = [["header"] * 39]
        assert inv.get_unassigned_skus_for_category("KKVSV", raw_rows) == []
