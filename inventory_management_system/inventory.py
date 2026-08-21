# =============================================================================
# LAKSHIRA HANDWOVEN WEAVES — INVENTORY MANAGEMENT SYSTEM
# =============================================================================

import os
import re
import difflib
import textwrap

import requests
import questionary
from prompt_toolkit.styles import Style
import phonenumbers
from phonenumbers import phonemetadata as _phonemetadata
from datetime import datetime, date
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MODE = "test"

# Real spreadsheet IDs are environment-specific and never committed --
# set TEST_SHEET_ID / LIVE_SHEET_ID in your own .env before running.
SHEET_IDS = {
    "test": os.environ.get("TEST_SHEET_ID", ""),
    "live": os.environ.get("LIVE_SHEET_ID", ""),
}

# Earliest year offered in the report year pickers below. The business
# started in 2022 (first sales followed in 2024) — a year or two of
# buffer costs nothing since an empty year is just an unused dropdown
# option, so this stays a fixed floor rather than a value derived from
# sheet data.
BUSINESS_START_YEAR = 2022

# =============================================================================
# REFERENCE DATA
# =============================================================================

WEAVE_TYPES = {
    "Kanjivaram": "KKVSV",
    "Twill Kanjivaram": "TKSS",
    "Krishnamoorthy Kanjivaram Silk": "KMKKVSV",
    "Twill Kuttu Gadwal Silk": "TKGS",
    "Kuttu Gadwal Silk": "KGS",
    "Twill Gadwal Silk": "TGS",
    "Ikat Silk": "IKPT",
    "Benaras": "BENAS",
    "Benaras Kota": "BENAK",
    "Tussar Silk": "TUSS",
    "Vidharba Silk": "VIDHAR",
    "Aplickora Embroidery": "APLICKOR",
    "Kantha Tussar": "KANTUS",
    "Kodalikarpur": "KODALI",
    "Chanderi": "CHANS",
    "Fusion": "FUSION",
    "Ikat Silk Blouse": "IKB",
    "Velvet Silk Blouse": "VB",
    "Bandej Blouse": "BB",
    "Kanjivaram Silk Blouse": "KSB",
    "Tussar Blouse": "TB",
    "Mirror Silk Blouse": "MSB",
    "Zari Kota Kanjivaram Silk": "KVZKS",
    "Designer Blouses": "DBL",
    "Ikat Kanjivaram Silk": "KVIKS",
    "Bamboo Bandej Kanjivaram Silk": "KVBBS",
    "Khadi Kanjivaram Silk": "KVKHS",
    "Finest Contemporary Kanjivaram Silk": "KVFCS",
    "Kanjivaram Dupatta": "KVD",
    "Kanjivaram Silk Saree - Digital Print": "KVDPS",
    "Kanjivaram Korvai Kora Silk": "KVKS",
    "Benaras Ektara Varnasi": "BENAE",
}


def _infer_garment_type(name):
    n = name.upper()
    if "BLOUSE" in n:
        return "Blouse"
    if "DUPATTA" in n:
        return "Dupatta"
    return "Saree"


WEAVE_GARMENT_TYPES = {name: _infer_garment_type(name) for name in WEAVE_TYPES}

GARMENT_TYPES = []  # populated at startup from Category Code tab column D; extended this session when new types are added

# Sample values -- real supplier relationships are business-confidential and
# not published here. This is a seed list of the same shape/size as the
# production data (18 suppliers), swappable via load_suppliers() below,
# which also learns any new supplier typed into the live sheet at runtime.
SUPPLIERS = [
    "Anand Weaves",
    "Sundari Silks",
    "R. Venkataraman",
    "Padma Textiles",
    "S. Ramaswamy",
    "Kaveri Handlooms",
    "Vignesh Creations",
    "Meera Auntie",
    "Chandran Weaves",
    "Golden Thread Silks",
    "Priya Collections",
    "Heritage Looms",
    "Suresh Kumar",
    "Devika",
    "Ravi Anna",
    "Nandini Brothers",
    "Sri Lakshmi Sarees",
    "Varanasi Weaves Co.",
]

STATUSES = [
    "Available",
    "Reserved",
    "Sold",
    "Sold - Partial Payment",
    "Unassigned",
]

SALES_CHANNELS = [
    "Exhibition/Popup",
    "Instagram",
    "Shopify",
    "WhatsApp",
]

COUNTRIES = []  # populated at startup by load_countries()

# Session cache for Nominatim geocoding responses (reset on each run)
# Value: {"found": bool, "parts": list} on success, None on API error
_nominatim_cache = {}

# Column name → 1-based column index in the master sheet
COLUMNS = {
    "SKU": 1,
    "Category Code": 2,
    "Weave Type / Cluster": 3,
    "Source Sheet + Tab": 4,
    "Supplier": 5,
    "Date Acquired": 6,
    "Base Price + GST Tax (INR)": 7,
    "Shipping Cost (INR)": 8,
    "Design Detailing Cost (INR)": 9,
    "Total Cost (INR)": 10,
    "Total Cost (USD)": 11,
    "Selling Price (USD)": 12,
    "Selling Price (INR) - Provided": 13,
    "Selling Price (INR) - Derived": 14,
    "Discount %": 15,
    "Actual Selling Price (USD)": 16,
    "Gross Profit (USD)": 17,
    "Markup %": 18,
    "(Profit) Margin %": 19,
    # Column 20: Margin Distribution — formula-driven, never written by script
    "Date Sold": 21,
    "Sales Channel": 22,
    "Days to Sell": 23,        # formula-driven, never written by script
    "Days in Inventory": 24,   # formula-driven, never written by script
    # Column 25: Aging Bucket — formula-driven, never written by script
    "Customer Name": 26,
    "Customer Country Code": 27,
    "Customer Phone": 28,
    "Customer Email": 29,
    "Customer City": 30,
    "Customer State": 31,
    "Customer Country": 32,
    "Status": 33,
    "Reserved Date": 34,
    "Inventory Notes": 35,
    "Transaction Notes": 36,
    "Amount Received (USD)": 37,
    "Amount Outstanding (USD)": 38,
    # Column 39: Dead Stock Flag — formula-driven, never written by script
}


# =============================================================================
# PHASE 2 — GOOGLE SHEETS CONNECTION LAYER
# =============================================================================

_sheet_cache = None        # module-level cache so we only authenticate once per run
_spreadsheet_cache = None  # cached spreadsheet object for opening additional worksheets


def connect_to_sheet():
    global _sheet_cache, _spreadsheet_cache
    if _sheet_cache is not None:
        return _sheet_cache

    creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
    if not os.path.exists(creds_path):
        print("Error: credentials.json not found in the script folder.")
        print("Please place your Google service account credentials file there and try again.")
        raise SystemExit(1)

    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
        client = gspread.authorize(creds)
        sheet_id = SHEET_IDS[MODE]
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Lakshira Inventory")
        _sheet_cache = worksheet
        _spreadsheet_cache = spreadsheet
        return worksheet
    except gspread.exceptions.APIError as e:
        print("Error: Could not connect to Google Sheets.")
        print("Please check your internet connection and try again.")
        raise SystemExit(1)
    except Exception:
        print("Error: Could not open the master sheet. Check that credentials.json is valid.")
        raise SystemExit(1)


def connect_to_category_code_sheet():
    """Return the 'Category Code' worksheet using the cached spreadsheet connection."""
    if _spreadsheet_cache is None:
        connect_to_sheet()  # ensures _spreadsheet_cache is populated
    return _spreadsheet_cache.worksheet("Category Code")


def connect_to_supplier_sheet():
    """Return the 'Supplier' worksheet using the cached spreadsheet connection."""
    if _spreadsheet_cache is None:
        connect_to_sheet()
    return _spreadsheet_cache.worksheet("Supplier")


def get_all_rows():
    """Return all data rows as a list of dicts keyed by column name."""
    ws = connect_to_sheet()
    records = ws.get_all_records(expected_headers=list(COLUMNS.keys()))
    return records


def get_raw_rows():
    """Return all rows as a list of lists (row 1 = headers, row 2+ = data)."""
    ws = connect_to_sheet()
    return ws.get_all_values()


def find_row_index_by_sku(sku):
    """Return the 1-based sheet row number for the given SKU, or None.
    Case-insensitive -- every SKU the app generates is uppercase by
    construction (the "LAH-" prefix and category codes are always
    uppercase), so comparing case-insensitively just makes the lookup
    forgiving about how an operator happened to type it, without risking
    an unintended cross-match. Fixed here once so every SKU-entry screen
    benefits automatically, rather than relying on each one to remember
    to uppercase its own input."""
    raw = get_raw_rows()
    sku_col = COLUMNS["SKU"] - 1  # convert to 0-based
    target = sku.strip().upper()
    for i, row in enumerate(raw):
        if i == 0:
            continue  # skip header
        if len(row) > sku_col and row[sku_col].strip().upper() == target:
            return i + 1  # 1-based sheet row
    return None


# Formula-driven columns (never written directly by the script) that a
# brand-new row needs the live formula copied into, since the sheet has
# no auto-fill-down behavior for rows created via the API. Grouped into
# contiguous ranges: Margin Distribution (20), Days to Sell / Days in
# Inventory / Aging Bucket (23-25), Dead Stock Flag (39).
_FORMULA_COLUMN_RANGES = [(20, 20), (23, 25), (39, 39)]


def _copy_formula_columns(ws, dest_row):
    """Copy the five formula-driven columns into a newly created row, so
    it isn't left without Margin Distribution / Days to Sell / Days in
    Inventory / Aging Bucket / Dead Stock Flag until someone manually
    drags those formulas down.

    Always copies from row 2 -- the original, hand-authored source of
    these formulas -- rather than whichever row happens to sit directly
    above the new one, since a later row could have picked up an
    accidental edit over time; row 2 is the one version trusted not to
    have drifted.

    Uses a real copy-paste request (Sheets API "copyPaste", not a
    literal text copy) so relative row references shift the same way a
    manual drag-fill would -- e.g. a formula reading row 2 becomes one
    reading row 500 once copied into row 500. A plain values.update()
    with the formula text as a literal string would NOT do this; it
    would paste the exact same row-2 references into every new row.
    """
    SOURCE_ROW = 2
    if dest_row == SOURCE_ROW:
        return
    sheet_id = ws.id

    def _grid_range(col_start, col_end, row):
        return {
            "sheetId": sheet_id,
            "startRowIndex": row - 1, "endRowIndex": row,
            "startColumnIndex": col_start - 1, "endColumnIndex": col_end,
        }

    requests = [
        {"copyPaste": {
            "source": _grid_range(c1, c2, SOURCE_ROW),
            "destination": _grid_range(c1, c2, dest_row),
            "pasteType": "PASTE_FORMULA",
        }}
        for c1, c2 in _FORMULA_COLUMN_RANGES
    ]
    ws.spreadsheet.batch_update({"requests": requests})


_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def _sheet_safe(value):
    """Neutralize a value that Sheets' USER_ENTERED write mode would
    otherwise parse as a formula. Any string beginning with =, +, -, or @
    gets a leading apostrophe -- the same escape the Sheets UI itself
    inserts to force literal text -- so a note like "-10% discount" or a
    handle like "@shopname" lands as the text typed, not a formula error.
    Numbers, dates, and blanks pass through untouched."""
    if isinstance(value, str) and value[:1] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value


def append_row(row_data):
    """
    Append a new row to the sheet starting at column A.
    Finds the next empty row by scanning the WHOLE sheet (every column),
    then writes the full 39-element list via ws.update() with an explicit
    range so gspread cannot offset the write due to formula content in
    other columns.

    A row only counts as empty when nothing is in it anywhere -- scanning
    just column A (SKU) would undercount if any row ever has a blank SKU
    cell while other columns still hold real data (e.g. a manual edit that
    only cleared the SKU box), pointing the next write at an
    already-occupied row and silently overwriting it instead of appending.

    Refuses to write a duplicate SKU — a hard backstop so "no SKU ever
    appears twice" holds regardless of which caller reaches this point,
    not just the call site that already re-checks before calling here.
    """
    sku = row_data.get("SKU")
    if sku and find_row_index_by_sku(sku) is not None:
        raise ValueError(f"Refusing to append: SKU {sku} already exists in the sheet.")
    ws = connect_to_sheet()
    row = [""] * 39
    for col_name, value in row_data.items():
        idx = COLUMNS.get(col_name)
        if idx is not None:
            row[idx - 1] = _sheet_safe(value)
    next_row = len(ws.get_all_values()) + 1
    ws.update(range_name=f"A{next_row}", values=[row], value_input_option="USER_ENTERED")
    try:
        _copy_formula_columns(ws, next_row)
    except Exception as e:
        _warn(f"Could not copy formula columns into row {next_row}: {e}. "
              f"You may need to drag Margin Distribution, Days to Sell, Days in "
              f"Inventory, Aging Bucket, and Dead Stock Flag down manually for this row.")


def update_row(sheet_row_number, updates):
    """
    Update specific cells in an existing row.
    updates: dict mapping column name → new value.
    sheet_row_number: 1-based row index in the sheet.

    Sends every field as a single batched call rather than one API
    request per field, so a transient failure (rate limit, network
    blip, auth expiry) can never leave the row half-updated -- either
    the whole write lands, or none of it does. value_input_option is
    set explicitly to match update_cell()'s own default, so every
    field is interpreted exactly as before (dates recognized as
    dates, etc.) -- only the number of API calls changes, not how
    any value is read once it arrives.
    """
    ws = connect_to_sheet()
    cells = []
    for col_name, value in updates.items():
        idx = COLUMNS.get(col_name)
        if idx is not None:
            cells.append(gspread.Cell(row=sheet_row_number, col=idx, value=_sheet_safe(value)))
    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")


def get_row_by_sheet_index(sheet_row_number):
    """Return a single row as a dict keyed by column name."""
    ws = connect_to_sheet()
    row_values = ws.row_values(sheet_row_number)
    # Pad to 39 columns in case trailing cells are empty
    row_values += [""] * (39 - len(row_values))
    result = {}
    for col_name, idx in COLUMNS.items():
        result[col_name] = row_values[idx - 1]
    return result


def _status_unchanged(row_index, expected_status):
    """Re-read a row's current Status and compare against what was
    captured earlier in the calling flow — catches a concurrent edit
    (another session selling/repricing/cancelling/reserving the same
    unit) that happened while this operator worked through the
    remaining prompts before writing."""
    row = get_row_by_sheet_index(row_index)
    return row.get("Status", "").strip() == expected_status


def _row_fields_unchanged(row_index, original_row, touched_columns):
    """Re-read a row and confirm every column in touched_columns still
    matches what original_row (the snapshot captured when this session
    started) said -- catches a concurrent edit to any specific field this
    session is about to overwrite, not just a Status flip. _status_unchanged()
    already catches the unit having moved to a different lifecycle state;
    this catches a same-state edit to the data itself (e.g. someone else
    changed Shipping Cost, or recorded a different payment amount, while
    this session was still being filled in).
    Returns (True, None) if nothing drifted, or (False, column_name)
    naming the first field found to have changed underneath this session.
    Append-only Notes-style columns should never be passed here -- they're
    meant to always merge, not conflict; see _fresh_notes_append()."""
    current = get_row_by_sheet_index(row_index)
    for col in touched_columns:
        if str(current.get(col, "")).strip() != str(original_row.get(col, "")).strip():
            return False, col
    return True, None


def _fresh_notes_append(row_index, notes_column, new_lines):
    """Re-read notes_column fresh (not from a stale session-start
    snapshot) and append new_lines onto whatever's actually there right
    now -- so a concurrent session's own note isn't silently discarded
    just because this session started editing first and is committing
    second. new_lines: iterable of already-formatted note strings, in
    the order they should be appended."""
    current_notes = get_row_by_sheet_index(row_index).get(notes_column, "").strip()
    block = "\n".join(new_lines)
    return f"{current_notes}\n{block}".strip() if current_notes else block


# =============================================================================
# PHASE 3 — CORE UTILITIES
# =============================================================================

def signed(val):
    return "+" if val >= 0 else "-"


# ---------- Input helpers ----------

def _fmt_dial_code(code):
    if code and not str(code).startswith("+"):
        return "+" + str(code)
    return code or ""


def _calling_code_for_country(country_name):
    """Return the numeric calling code string for a country name via COUNTRIES → ISO → phonenumbers."""
    if not country_name or not COUNTRIES:
        return ""
    name_lower = country_name.lower()
    for c in COUNTRIES:
        if c["name"].lower() == name_lower:
            iso = c.get("code", "")
            if iso:
                cc = phonenumbers.country_code_for_region(iso)
                return str(cc) if cc else ""
            break
    return ""


def format_phone_display(dial_code, phone_digits, country=None):
    """
    Return an internationally formatted phone string using phonenumbers.
    When dial_code is blank, derives the calling code from the country name.
    Falls back to '+CC digits' (or raw digits) if parsing fails. Never raises.
    """
    if not phone_digits:
        return ""
    try:
        if str(phone_digits).startswith("+"):
            parsed = phonenumbers.parse(phone_digits, None)
        else:
            calling_code = re.sub(r"\D", "", str(dial_code))
            if not calling_code:
                calling_code = _calling_code_for_country(country)
            if not calling_code:
                return phone_digits
            parsed = phonenumbers.parse(f"+{calling_code}{phone_digits}")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except Exception:
        calling_code = re.sub(r"\D", "", str(dial_code))
        if not calling_code:
            calling_code = _calling_code_for_country(country)
        return f"+{calling_code} {phone_digits}" if calling_code else phone_digits


def _phone_length_error(dial_code, phone_digits, country_name):
    """Return an error string if phone_digits is outside the valid length range for the country, else None."""
    try:
        cc = re.sub(r"\D", "", str(dial_code))
        if not cc:
            return None
        parsed = phonenumbers.parse(f"+{cc}{phone_digits}")
        region = phonenumbers.region_code_for_number(parsed)
        if not region:
            regions = phonenumbers.region_codes_for_country_code(int(cc))
            region = regions[0] if regions else None
        if region:
            meta = _phonemetadata.PhoneMetadata.metadata_for_region(region)
            if meta:
                # Use mobile + fixed_line lengths — general_desc is too broad (includes pagers,
                # emergency numbers, etc.) and allows lengths that no real customer number would have.
                mob   = set(meta.mobile.possible_length)     if meta.mobile      else set()
                fixed = set(meta.fixed_line.possible_length) if meta.fixed_line  else set()
                lengths = sorted(mob | fixed) or sorted(meta.general_desc.possible_length)
                if lengths:
                    min_len = min(lengths)
                    max_len = max(lengths)
                    entered = len(phone_digits)
                    if entered < min_len:
                        return (f"Phone numbers in {country_name} typically require {min_len} digits. "
                                f"You entered {entered} — please check and re-enter.")
                    if entered > max_len:
                        return (f"Phone numbers in {country_name} typically require {max_len} digits. "
                                f"You entered {entered} — please check and re-enter.")
    except Exception:
        pass
    return None


def _sheet_float(value, default=0.0):
    """Convert a sheet cell value to float, tolerating comma-formatted numbers."""
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return default


def _safe_parse_date(raw):
    """Any MM-DD-YYYY string as a date, or None if blank/malformed --
    never raises. Every date column the app writes (Date Acquired, Date
    Sold, Reserved Date) is validated as a real, not-future date at entry
    -- this exists for reading rows back later, where a blank/garbled/
    future value can only mean hand-edited or legacy data the app never
    validated itself."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m-%d-%Y").date()
    except ValueError:
        return None


def _days_since(target_date, reference_date=None):
    """Days from target_date to reference_date (default: today), or None
    if target_date is unparseable or the result would be negative -- a
    "future" date only reaches the sheet by bypassing the app's own
    entry-time validation, so it should read as "can't tell," not as a
    nonsensical negative day count."""
    if target_date is None:
        return None
    delta = ((reference_date or date.today()) - target_date).days
    return delta if delta >= 0 else None


def _warn(message):
    """Print a validation error in amber with a warning symbol and leading blank line."""
    print(f"\n\033[33m⚠  {message}\033[0m")


def _looks_like_phone_query(query):
    """Whether a customer-search query looks like an attempted phone-number
    search rather than a name. A bare query.isdigit() check only catches
    unpunctuated digits ("6505551234") -- it misses every realistic way a
    phone number actually gets typed ("650-555-1234", "(650) 555-1234",
    "+1 650-555-1234"), which then silently fall through to the name
    search, match nothing, and show a generic "not found" with no hint
    that phone numbers aren't searchable here at all. This strips common
    phone punctuation first, so any of those forms are recognized."""
    normalized = re.sub(r"[\s()+\-]", "", query)
    return bool(normalized) and normalized.isdigit()


def ask_number(prompt, allow_zero=False, allow_negative=False):
    """Return a validated float from user input."""
    while True:
        raw = input(f"\n{prompt} ").strip()
        try:
            value = float(raw)
            if not allow_negative and value < 0:
                _warn("Please enter a positive number.")
                continue
            if not allow_zero and not allow_negative and value == 0:
                _warn("Please enter a number greater than zero.")
                continue
            return value
        except ValueError:
            _warn("Please enter a valid number (e.g. 5000 or 12.50).")


def ask_date(prompt, not_future=False, not_before=None, not_after=None,
             future_msg="That date is in the future. Please enter a past or present date.",
             not_before_msg=None, not_after_msg=None, allow_back=False):
    """
    Prompt for a date in MM-DD-YYYY format.
    not_future: date must not be today or in the future.
    not_before: datetime.date — date must be on or after this date.
    not_after: datetime.date — date must be on or before this date (e.g.
        Date Acquired can't be edited to fall after an existing Reserved Date
        -- you can't acquire something after you'd already reserved it).
    future_msg: override the rejection message shown when not_future is violated.
    not_before_msg: override the rejection message shown when not_before is violated
        (defaults to the Date Sold / Date Acquired wording, the original single use case).
    not_after_msg: override the rejection message shown when not_after is violated.
    allow_back: if True, typing 'back' (case-insensitive) returns None instead
        of a date -- gives a free-text date prompt a genuine way to cancel,
        matching the Back option already available in every arrow-key menu
        elsewhere. Off by default so every existing caller is unaffected.
    Returns a datetime.date object, or None if allow_back and the user typed 'back'.
    """
    while True:
        suffix = " (Type 'back' to cancel)" if allow_back else ""
        raw = input(f"\n{prompt} (MM-DD-YYYY){suffix}: ").strip()
        if allow_back and raw.lower() == "back":
            return None
        try:
            d = datetime.strptime(raw, "%m-%d-%Y").date()
        except ValueError:
            _warn("Please enter the date in MM-DD-YYYY format (e.g. 06-15-2024).")
            continue
        if not_future and d > date.today():
            _warn(future_msg)
            continue
        if not_before and d < not_before:
            _warn(not_before_msg or
                  f"Date Sold cannot be before Date Acquired ({not_before.strftime('%m-%d-%Y')}).")
            continue
        if not_after and d > not_after:
            _warn(not_after_msg or
                  f"Date cannot be after {not_after.strftime('%m-%d-%Y')}.")
            continue
        return d


def ask_yes_no(prompt):
    """Return True for yes, False for no."""
    while True:
        raw = input(f"\n{prompt} (yes/no): ").strip().lower()
        if raw in ("yes", "y"):
            return True
        if raw in ("no", "n"):
            return False
        _warn("Please type yes or no.")


# Shared styling for a "back"/"return to main menu"-style option inside a
# questionary.select() choices list: a blank-line gap setting it apart from
# the real choices above, plus de-emphasized styling — reusing the same grey
# already used for secondary/de-emphasized text elsewhere in this file
# (customer-location) — rather than the bright blue reserved for a forward
# call-to-action like "+ Register a new customer", which would send the
# wrong signal for a backward/retreat action. Mirrors how the main menu
# itself sets "9. Exit" apart from the numbered operations above it.
# Separator("") would silently fall back to a dashed line (its constructor
# does `line or default`, and "" is falsy), so a single space is used for an
# actually-blank gap.
_MENU_STYLE = Style.from_dict({
    "back-option":      "#777777",
    "register-option":  "#5fafff",
})

def _back_choice(text="Back"):
    return questionary.Choice(title=[("class:back-option", f"← {text}")], value=text)


_SEP_MARK = object()  # placeholder in a _print_boxed body-lines list,
                       # replaced with a dynamically-sized dim divider
_EQ_MARK  = object()  # placeholder replaced with a dynamically-sized solid
                       # rule (the one directly under the title)

def _visible_len(s):
    return len(re.sub(r"\033\[[0-9;]*m", "", s))

def _pad_visible(s, width):
    return s + " " * max(0, width - _visible_len(s))

def _clip(name, width=20):
    """Truncate a free-text name to a fixed width for column-aligned list
    tables (SKU/Customer/Amount-style rows), where widening the whole
    table per-row isn't practical -- unlike _print_boxed's label/value
    tables, which widen to fit instead of truncating."""
    return name if len(name) <= width else name[:width - 1] + "…"

def _print_boxed(title, sections):
    """Print a '=== TITLE === / TITLE / === ...' box whose border width
    matches the widest line actually being printed inside it, with each
    section (name, [row strings]) shown under its own orange header +
    divider. Border width is measured from the actual content every call,
    so a table with a long supplier name or customer email widens to fit
    it instead of the row overflowing past a fixed-width border.
    title=None skips the title line and the top/bottom '=' border entirely
    -- just the sections, ending in one dynamically-sized divider -- for a
    lighter-weight review block that doesn't need its own titled frame
    (e.g. the customer-details edit review in update_customer_details_if_needed()).
    The leading blank line always prints regardless of title, so a
    title=None box still reads as separate from whatever was on screen
    right before it."""
    body_lines = []
    if title is not None:
        body_lines.append(f"  \033[1;38;5;178m{title}\033[0m")
        body_lines.append(_EQ_MARK)
    first = True
    for section_name, rows in sections:
        if not rows:
            continue
        if not first:
            body_lines.append("")
        first = False
        body_lines.append(f"\033[38;5;202m{section_name}\033[0m")
        body_lines.append(_SEP_MARK)
        body_lines.extend(rows)
    width = max([50] + [_visible_len(l) for l in body_lines if l not in (_SEP_MARK, _EQ_MARK)])
    dyn_eq  = f"\033[2m{'=' * width}\033[0m"
    dyn_sep = f"\033[2m{'—' * width}\033[0m"
    print()
    if title is not None:
        print(dyn_eq)
    for line in body_lines:
        if line is _SEP_MARK:
            print(dyn_sep)
        elif line is _EQ_MARK:
            print(dyn_eq)
        else:
            print(line)
    print(dyn_eq if title is not None else dyn_sep)


def ask_text(prompt, required=True, blank_message="This field cannot be left blank. Please enter a value."):
    """Return a non-empty string (or empty string if not required)."""
    while True:
        raw = " ".join(input(f"\n{prompt} ").split())
        if raw:
            return raw
        if not required:
            return ""
        _warn(blank_message)


def ask_percent(prompt, allow_zero=True):
    """Return a validated float between 0 and 100."""
    while True:
        raw = input(f"\n{prompt} ").strip()
        try:
            value = float(raw)
            if value < 0 or value > 100:
                _warn("Please enter a percentage between 0 and 100.")
                continue
            if not allow_zero and value == 0:
                _warn("Please enter a percentage greater than 0.")
                continue
            return value
        except ValueError:
            _warn("Please enter a valid number (e.g. 25 or 12.5).")


# ---------- Contact method ----------

def ask_contact_method():
    """Prompt for a reserver's contact with type-specific validation. Returns a formatted string."""
    while True:
        print()
        contact_type = questionary.select(
            "Point of Contact:",
            choices=["Phone", "Instagram", "Email"],
            qmark="",
            instruction=" ",
        ).unsafe_ask()

        if contact_type == "Phone":
            country_name, dial_code = select_country()
            if country_name is None:
                continue  # back to Point of Contact
            dial_code = _fmt_dial_code(dial_code)
            while True:
                raw_phone = ask_text(f"Phone Number ({dial_code}):")
                stripped = re.sub(r"\D", "", raw_phone)
                err = _phone_length_error(dial_code, stripped, country_name)
                if err:
                    _warn(err)
                    continue
                return format_phone_display(dial_code, stripped, country=country_name)

        elif contact_type == "Instagram":
            while True:
                raw = input("\nInstagram handle: @").strip()
                if not raw:
                    _warn("Instagram handle cannot be empty.")
                    continue
                if not re.match(r'^[a-zA-Z0-9._]{1,30}$', raw):
                    _warn("Instagram handles can only contain letters, numbers, underscores, and periods (max 30 characters).")
                    continue
                return f"@{raw}"

        else:  # Email
            while True:
                email = ask_text("Email Address:")
                if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', email):
                    _warn("Please enter a valid email address (e.g. name@domain.com).")
                    continue
                return email


# ---------- ECB exchange rate ----------

def fetch_ecb_rate(date_mm_dd_yyyy, total_cost_usd=None):
    """
    Fetch the ECB USD→INR rate for a given date (MM-DD-YYYY).
    Retries up to 3 times on timeout before aborting.
    Returns (rate, actual_date_str) on confirmation, (None, None) on rejection,
    (None, "abort") on unrecoverable failure.
    If total_cost_usd is provided, it is displayed inside the EXCHANGE RATE block.
    """
    MAX_ATTEMPTS = 3
    dt = datetime.strptime(date_mm_dd_yyyy, "%m-%d-%Y")
    url = f"https://api.frankfurter.app/{dt.strftime('%Y-%m-%d')}?from=USD&to=INR"

    rate = None
    returned_api_date = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            rate = data["rates"]["INR"]
            returned_api_date = data["date"]
            break
        except requests.exceptions.ConnectionError:
            print("\nError: Could not connect to the internet. Please check your connection and try again.")
            return None, "abort"
        except requests.exceptions.Timeout:
            if attempt < MAX_ATTEMPTS:
                print(f"\nExchange rate service is slow — retrying... ({attempt + 1} of {MAX_ATTEMPTS})")
            else:
                print(f"\nError: Exchange rate service timed out after {MAX_ATTEMPTS} attempts. Please try again later.")
                return None, "abort"
        except (KeyError, ValueError):
            print(f"\nError: Could not retrieve the exchange rate for {date_mm_dd_yyyy}.")
            return None, "abort"
        except Exception:
            print("\nError: An unexpected problem occurred while fetching the exchange rate.")
            return None, "abort"

    actual_date_str = datetime.strptime(returned_api_date, "%Y-%m-%d").strftime("%m-%d-%Y")

    _is_fallback = actual_date_str != date_mm_dd_yyyy
    _sep = f"\033[2m{'—' * (56 if _is_fallback else 50)}\033[0m"
    print(f"\n\033[38;5;202mEXCHANGE RATE\033[0m")
    print(_sep)
    if _is_fallback:
        print(f"  {actual_date_str + ':':<14}1 USD = {rate:,.2f} INR")
        print(f"  \033[2m(No rate for {date_mm_dd_yyyy} — using nearest business day)\033[0m")
    else:
        print(f"  {date_mm_dd_yyyy + ':':<14}1 USD = {rate:,.2f} INR")
    if total_cost_usd is not None:
        print()
        print(f"  {'Total Cost:':<14}${total_cost_usd:,.2f}")
    print(_sep)
    confirmed = ask_yes_no("Confirm this rate?")

    if confirmed:
        return rate, actual_date_str
    return None, None


# ---------- SKU generation ----------

def _extract_category_from_sku(sku):
    """
    Extract the category code embedded in a LAH-[CODE][SERIAL] SKU.
    Returns the code string, or '' if the SKU doesn't match the expected format.
    """
    if not sku.startswith("LAH-"):
        return ""
    rest = sku[4:]  # strip 'LAH-'
    # Category code is everything up to the first digit
    for i, ch in enumerate(rest):
        if ch.isdigit():
            return rest[:i]
    return rest  # no digits found — whole remainder is the code


def _warn_unassigned_integrity(raw_rows):
    """
    Scan ALL Unassigned rows and print a one-time warning for any where the
    category code embedded in the SKU does not match the Category Code column.
    Detection only — never auto-corrects.
    """
    sku_col    = COLUMNS["SKU"] - 1
    status_col = COLUMNS["Status"] - 1
    cat_col    = COLUMNS["Category Code"] - 1
    min_col    = max(sku_col, status_col, cat_col)

    mismatches = []
    for i, row in enumerate(raw_rows):
        if i == 0:
            continue
        if len(row) <= min_col:
            continue
        if row[status_col].strip() != "Unassigned":
            continue
        sku          = row[sku_col].strip()
        cat_in_col   = row[cat_col].strip()
        cat_in_sku   = _extract_category_from_sku(sku)
        if sku and cat_in_sku and cat_in_sku != cat_in_col:
            mismatches.append((sku, cat_in_sku, cat_in_col))

    if mismatches:
        print(f"\n\033[33m⚠  DATA INTEGRITY WARNING\033[0m")
        print(f"\033[2m{'—' * 50}\033[0m")
        for sku, embedded, column in mismatches:
            print(f"  {sku}  —  SKU embedded code '{embedded}' ≠ Category Code column '{column}'")
        print("  Please correct these manually in the sheet.")


def get_unassigned_skus_for_category(category_code, raw_rows):
    """
    Return list of SKUs in this category with Status = Unassigned.
    Also runs a one-time integrity check across ALL Unassigned rows and
    warns if any have a mismatch between their SKU-embedded code and the
    Category Code column value.
    """
    _warn_unassigned_integrity(raw_rows)

    sku_col    = COLUMNS["SKU"] - 1
    status_col = COLUMNS["Status"] - 1
    cat_col    = COLUMNS["Category Code"] - 1
    unassigned = []
    for i, row in enumerate(raw_rows):
        if i == 0:
            continue
        if len(row) <= max(sku_col, status_col, cat_col):
            continue
        if row[cat_col].strip() == category_code and row[status_col].strip() == "Unassigned":
            unassigned.append(row[sku_col].strip())
    return unassigned


def generate_next_sku(category_code, raw_rows):
    """Generate the next sequential SKU for a category, starting at 100."""
    sku_col = COLUMNS["SKU"] - 1
    prefix = f"LAH-{category_code}"
    serials = []
    for i, row in enumerate(raw_rows):
        if i == 0:
            continue
        if len(row) > sku_col:
            sku = row[sku_col].strip()
            if sku.startswith(prefix):
                suffix = sku[len(prefix):]
                if suffix.isdigit():
                    serials.append(int(suffix))
    next_serial = max(serials) + 1 if serials else 100
    return f"{prefix}{next_serial}"


def resolve_sku(category_code, weave_type):
    """
    Full SKU resolution flow:
    1. Check for Unassigned SKUs in category — offer them first.
    2. If none or user declines, generate next sequential SKU.
    3. Confirm with user before returning.
    Returns (sku, sheet_row_number_or_None).
      sheet_row_number is set only when an Unassigned row is being reused.
    """
    raw_rows = get_raw_rows()
    unassigned = get_unassigned_skus_for_category(category_code, raw_rows)

    chosen_existing_row = None

    if unassigned:
        print(f"\nThe following units are currently unassigned in this category:")
        for sku in unassigned:
            print(f"  {sku}")
        use_existing = ask_yes_no("Would you like to assign this unit to one of these SKUs?")
        if use_existing:
            print()
            chosen_sku = questionary.select(
                "Select an unassigned unit:",
                choices=unassigned + [
                    questionary.Separator(" "),
                    _back_choice("None of these — generate a new SKU"),
                ],
                qmark="",
                instruction=" ",
                style=_MENU_STYLE,
            ).unsafe_ask()
            if chosen_sku != "None of these — generate a new SKU" and chosen_sku is not None:
                chosen_existing_row = find_row_index_by_sku(chosen_sku)
                sku = chosen_sku
                print(f"\nYou have selected: {sku}")
                confirmed = ask_yes_no("Confirm?")
                if not confirmed:
                    print("\nAdd cancelled. Returning to Main Menu.")
                    return None, None
                return sku, chosen_existing_row

    sku = generate_next_sku(category_code, raw_rows)
    print(f"\nYour SKU will be: {sku}")
    confirmed = ask_yes_no("Confirm?")
    if not confirmed:
        print("\nAdd cancelled. Returning to Main Menu.")
        return None, None
    return sku, None


# ---------- Pricing calculations ----------

def calculate_pricing(total_cost_inr, ecb_rate, pricing_path, pricing_value, discount_pct=None):
    """
    Given Total Cost INR, ECB rate, pricing path, and pricing value, return all
    derived price/profit/margin columns as a dict.

    pricing_path: "markup" or "usd"
    pricing_value: markup % (0-100) if markup path, or Selling Price USD if usd path
    discount_pct: float 0-100, or None
    """
    total_cost_usd = round(total_cost_inr / ecb_rate, 2)

    if pricing_path == "markup":
        markup_fraction = pricing_value / 100
        selling_price_inr_provided = round(total_cost_inr * (1 + markup_fraction), 2)
        selling_price_usd = round(selling_price_inr_provided / ecb_rate, 2)
    else:  # usd path
        selling_price_usd = round(pricing_value, 2)
        selling_price_inr_provided = round(selling_price_usd * ecb_rate, 2)

    selling_price_inr_derived = round(selling_price_usd * ecb_rate, 2)

    if discount_pct is not None and discount_pct > 0:
        actual_selling_price_usd = round(selling_price_usd * (1 - discount_pct / 100), 2)
    else:
        actual_selling_price_usd = selling_price_usd
        discount_pct = None

    gross_profit_usd = round(actual_selling_price_usd - total_cost_usd, 2)
    markup_pct = round((gross_profit_usd / total_cost_usd) * 100, 2) if total_cost_usd else 0
    margin_pct = round((gross_profit_usd / actual_selling_price_usd) * 100, 2) if actual_selling_price_usd else 0

    return {
        "total_cost_usd": total_cost_usd,
        "selling_price_usd": selling_price_usd,
        "selling_price_inr_provided": selling_price_inr_provided,
        "selling_price_inr_derived": selling_price_inr_derived,
        "discount_pct": discount_pct,
        "actual_selling_price_usd": actual_selling_price_usd,
        "gross_profit_usd": gross_profit_usd,
        "markup_pct": markup_pct,
        "margin_pct": margin_pct,
    }


def get_status_color(status):
    """Return ANSI color code matching the sheet's Status column color coding."""
    if status == "Available":
        return "\033[32m"           # green
    if status == "Sold":
        return "\033[31m"           # red
    if status == "Reserved":
        return "\033[38;5;75m"      # sky blue
    if status == "Sold - Partial Payment":
        return "\033[38;5;208m"     # orange
    if status == "Unassigned":
        return "\033[2m"            # dim gray
    return ""


def get_margin_color(margin_pct):
    """Return ANSI color code matching the sheet's Profit Margin % conditional formatting (3 tiers)."""
    if margin_pct < 15:
        return "\033[31m"   # red
    if margin_pct < 20:
        return "\033[93m"   # yellow
    return "\033[32m"       # green


def get_markup_color(markup_pct):
    """Return ANSI color code for markup %, using 6-tier granularity (no sheet equivalent)."""
    if markup_pct < 10:
        return "\033[91m"        # bright red
    if markup_pct < 15:
        return "\033[38;5;208m"  # orange
    if markup_pct < 20:
        return "\033[93m"        # yellow
    if markup_pct <= 30:
        return "\033[92m"        # light green
    if markup_pct <= 40:
        return "\033[32m"        # green
    return "\033[38;5;22m"       # forest green


def _strip_reservation_note(notes):
    """Remove the 'Reserved by' line from Inventory Notes on release or sale."""
    if not notes:
        return ""
    cleaned = [l for l in notes.split('\n') if '· RESERVATION]' not in l and '· RSVP]' not in l]
    return '\n'.join(cleaned).strip()


def _get_reserver_name(notes):
    """Extract the reserver's name from the Inventory Notes reservation line."""
    for line in (notes or "").split('\n'):
        if 'Reserved by ' in line:
            after = line.split('Reserved by ', 1)[1]
            return after.split(' —')[0].strip()
    return None


def _reservation_days(reserved_date_str):
    """Return (days_held, is_expired) for a Reserved unit, or (None, False)
    if Reserved Date is blank/malformed -- or in the future, which can only
    happen via a hand-edited sheet, since Manage Reservation always
    validates it as not-future at entry. A future date would otherwise
    produce a negative day count instead of a real answer."""
    days = _days_since(_safe_parse_date(reserved_date_str))
    return (days, days > 7) if days is not None else (None, False)


def check_pricing_warnings(margin_pct, markup_pct, gross_profit_usd=0, exit_label="Discard & exit", recalibrate_label="Recalibrate selling price"):
    """
    Display any applicable pricing alert, then always show the guided proceed menu.
      - Below cost: BELOW COST alert + menu.
      - Low margin + low markup: combined LOW MARGIN alert + menu.
      - Low margin only: LOW MARGIN alert + menu.
      - Low markup only: LOW MARKUP note + menu.
      - Neither: menu only, no alert.
    Returns (proceed, warned): proceed=True (continue), False (re-enter), None (discard & exit).
    """
    warned = False

    if gross_profit_usd < 0:
        print(f"\n\033[91m⚠  BELOW COST\033[0m")
        print(f"\033[2m{'—' * 50}\033[0m")
        print(f"Gross Profit is -${abs(gross_profit_usd):,.2f}. This unit will sell at a loss.")
        warned = True
    else:
        low_markup = markup_pct < 7
        low_margin = margin_pct < 15
        color = get_margin_color(margin_pct)

        if low_margin and low_markup:
            print(
                f"\n{color}△ LOW MARGIN — Margin sits at {margin_pct:.1f}%, below the 15% threshold "
                f"(Markup: {markup_pct:.1f}% — unusually low).\033[0m"
            )
            warned = True
        elif low_margin:
            print(f"\n{color}△ LOW MARGIN — Margin sits at {margin_pct:.1f}%, below the 15% threshold.\033[0m")
            warned = True
        elif low_markup:
            print(f"\n\033[33m⚠  LOW MARKUP\033[0m")
            print(f"\033[2m{'—' * 50}\033[0m")
            print(f"Markup is {markup_pct:.1f}%, which is unusually low.")
            warned = True

    print()
    choice = questionary.select(
        "How would you like to proceed?",
        choices=[
            "Proceed with this price",
            recalibrate_label,
            questionary.Separator(" "),
            _back_choice(exit_label),
        ],
        qmark="",
        instruction=" ",
        style=_MENU_STYLE,
    ).unsafe_ask()
    if choice == exit_label:
        return None, warned
    return choice == "Proceed with this price", warned


# ---------- Weave type management ----------

def _enter_new_garment_type():
    """
    Prompt for a new garment type name with validation and duplicate checking.
    Appends confirmed new names to GARMENT_TYPES.
    Returns the resolved garment type name (new or matched existing), or None (back).
    """
    while True:
        raw = " ".join(input("\nEnter new garment type name (or type 'back' to go back): ").split())
        if not raw or raw.lower() == "back":
            return None

        if len(raw) < 2 or not all(c.isalpha() or c.isspace() for c in raw):
            _warn("Please enter a name using letters and spaces only, minimum 2 characters.")
            continue

        raw_lower = raw.lower()
        gt_sub = [g for g in GARMENT_TYPES if raw_lower in g.lower()]
        gt_matches = gt_sub if gt_sub else difflib.get_close_matches(raw, GARMENT_TYPES, n=3, cutoff=0.6)

        if len(gt_matches) == 1:
            if ask_yes_no(f"Did you mean '{gt_matches[0]}'?"):
                return gt_matches[0]
            # user said no — fall through to confirm as genuinely new

        elif len(gt_matches) > 1:
            print("\nClose matches found:")
            for i, m in enumerate(gt_matches, 1):
                print(f"  {i}. {m}")
            if ask_yes_no("Did you mean one of these?"):
                while True:
                    try:
                        idx = int(input("\nSelect number: ").strip()) - 1
                        if 0 <= idx < len(gt_matches):
                            return gt_matches[idx]
                        _warn(f"Please enter a number between 1 and {len(gt_matches)}.")
                    except ValueError:
                        _warn("Please enter a valid number.")
            # user said no — fall through to confirm as genuinely new

        print(f"\nNew garment type: {raw}.")
        if ask_yes_no("Confirm?"):
            GARMENT_TYPES.append(raw)
            return raw
        # else loop back to name entry


def select_or_add_weave_type():
    """
    Step 1: Garment type filter (Blouse/Dupatta/Saree/custom, or Add new/Return to Main Menu).
    Step 2: Filtered weave type list, sorted alphabetically.
    Step 3: Add new flow with garment type pre-filled from the selected bucket.
    Returns (weave_type_name, category_code), or (None, None) if user goes back.
    """
    # Outer loop lets Step 2 send control back to Step 1 -- realizing the
    # wrong garment type was picked only after browsing/searching weave
    # types is a real, expected scenario (Saree especially, given how many
    # weave types fall under it), not an edge case to leave unhandled.
    while True:
        # Step 1: Garment type filter (with add-new sub-flow)
        garment_is_new = False
        while True:
            garment_choice = questionary.select(
                "Select garment type:",
                choices=GARMENT_TYPES + [
                    questionary.Separator("─" * 44),
                    questionary.Choice(
                        title=[("class:register-option", "+ Add new garment type")],
                        value="Add new garment type",
                    ),
                    questionary.Separator(" "),
                    _back_choice("Return to Main Menu"),
                ],
                qmark="",
                instruction=" ",
                style=_MENU_STYLE,
            ).unsafe_ask()
            if garment_choice is None or garment_choice == "Return to Main Menu":
                return None, None
            if garment_choice != "Add new garment type":
                break
            gt_count_before = len(GARMENT_TYPES)
            resolved = _enter_new_garment_type()
            if resolved is not None:
                garment_choice = resolved
                garment_is_new = len(GARMENT_TYPES) > gt_count_before
                break
            # None → back, loop to garment type menu

        # Step 2: Filtered list for this garment type, sorted alphabetically
        filtered_names = sorted(
            [name for name, gt in WEAVE_GARMENT_TYPES.items() if gt == garment_choice],
            key=str.lower,
        )

        # Type-to-narrow search before the arrow-key list -- some garment
        # buckets (Saree especially) hold 20+ weave types, too many to
        # comfortably scroll through blind. Substring match, not fuzzy --
        # this is a deliberate fragment search ("gadwal" narrowing to every
        # Gadwal variant), not a typo-tolerant lookup of a name you already
        # know, so exact substring containment is the right tool here (see
        # the "Add new" duplicate-detection flow below for where fuzzy
        # matching is the better fit instead).
        while True:
            query = input(f"\nSearch {garment_choice} weave types (or press Enter to see all): ").strip()
            if not query:
                display_names = filtered_names
                break
            display_names = [name for name in filtered_names if query.lower() in name.lower()]
            if display_names:
                break
            _warn(f"No {garment_choice} weave type matches '{query}'. Try again, or press Enter to see all.")

        print()
        selected = questionary.select(
            f"Select {garment_choice} weave type:",
            choices=display_names + [
                questionary.Separator("─" * 44),
                questionary.Choice(
                    title=[("class:register-option", "+ Add new weave type")],
                    value="Add new weave type",
                ),
                questionary.Separator(" "),
                _back_choice("Change garment type"),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()

        if selected == "Change garment type":
            print()
            continue

        if selected != "Add new weave type":
            return selected, WEAVE_TYPES[selected]

        break  # fall through to Step 3 (add-new flow) below, outside the loop

    # Add new weave type flow — type-first, fuzzy-match second
    raw = " ".join(input("\nEnter new weave type name (or press Enter / type 'back' to go back): ").split())
    if not raw or raw.lower() == "back":
        print("Returning to weave type selection.")
        print()
        return select_or_add_weave_type()

    if raw in WEAVE_TYPES:
        _warn(f"'{raw}' already exists. Please select it from the list.")
        print()
        return select_or_add_weave_type()

    raw_lower = raw.lower()
    substring_matches = [k for k in WEAVE_TYPES if raw_lower in k.lower()]
    matches = substring_matches if substring_matches else difflib.get_close_matches(raw, list(WEAVE_TYPES.keys()), n=3, cutoff=0.6)

    if len(matches) == 1:
        print(f"\nClose match found: {matches[0]}")
        if ask_yes_no(f"Did you mean '{matches[0]}'?"):
            return matches[0], WEAVE_TYPES[matches[0]]
        # user said no — proceed as genuinely new

    elif len(matches) > 1:
        print()
        pick = questionary.select(
            "Multiple close matches found — did you mean one of these?",
            choices=matches + [
                questionary.Separator("─" * 44),
                questionary.Choice(
                    title=[("class:register-option", "+ None of these — add as new")],
                    value="__NONE__",
                ),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if pick != "__NONE__":
            return pick, WEAVE_TYPES[pick]
        # user picked "none of these" — proceed as genuinely new

    else:
        print("\nNo existing weave type matches that name.")

    new_name = raw

    # Show used codes for this garment type only
    print(f"\nCurrently used category codes ({garment_choice}):")
    bucket_entries = sorted(
        [(n, c) for n, c in WEAVE_TYPES.items() if WEAVE_GARMENT_TYPES.get(n) == garment_choice],
        key=lambda x: x[0].lower(),
    )
    if bucket_entries:
        for name, code in bucket_entries:
            print(f"  {code}  —  {name}")
    else:
        print(f"  No existing category codes for {garment_choice} yet.")

    first = True
    while True:
        prompt = "\nEnter a new category code for this weave type (or type 'back' to go back): " if first else "\nTry again (or type 'back'): "
        first = False
        raw = input(prompt).strip()
        if not raw or raw.lower() == "back":
            print("Returning to weave type selection.")
            print()
            return select_or_add_weave_type()
        new_code = raw.upper()
        if new_code in WEAVE_TYPES.values():
            existing_name = [k for k, v in WEAVE_TYPES.items() if v == new_code][0]
            _warn(f"Category code '{new_code}' is already assigned to '{existing_name}'. Please choose a different code.")
            continue
        break

    print(f"\nNew weave type: {new_name}  —  Category Code: {new_code}")
    confirmed = ask_yes_no("Confirm?")
    if not confirmed:
        print("Cancelled.")
        return select_or_add_weave_type()

    # Garment type step — pre-filled from bucket, confirm or override
    label = "your new garment type" if garment_is_new else "based on your selection"
    print(f"\nGarment type: {garment_choice} ({label}).")
    print()
    garment_confirm = questionary.select(
        "Is this correct?",
        choices=["Yes", "No — select a different type"],
        qmark="",
        instruction=" ",
    ).unsafe_ask()
    if "No" in garment_confirm:
        print()
        garment_type = questionary.select(
            "Select garment type:",
            choices=GARMENT_TYPES + [
                questionary.Separator(" "),
                _back_choice(f"Keep '{garment_choice}' after all"),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if garment_type == f"Keep '{garment_choice}' after all":
            garment_type = garment_choice
    else:
        garment_type = garment_choice

    # Write new entry to Category Code tab
    try:
        cat_ws = connect_to_category_code_sheet()
        all_values = cat_ws.get_all_values()
        next_row = None
        for i, row in enumerate(all_values[3:200], start=4):
            if len(row) < 2 or not row[1].strip():
                next_row = i
                break
        if next_row is None:
            next_row = len(all_values) + 1
        cat_ws.update(range_name=f"B{next_row}", values=[[_sheet_safe(new_code), _sheet_safe(new_name), _sheet_safe(garment_type)]], value_input_option="USER_ENTERED")
    except Exception:
        print("Warning: Could not write to the Category Code tab. The weave type has been added in memory and will be usable this session, but please add it to the Category Code sheet manually.")

    WEAVE_TYPES[new_name] = new_code
    WEAVE_GARMENT_TYPES[new_name] = garment_type
    return new_name, new_code


# ---------- Supplier management ----------

def select_or_add_supplier():
    """
    Present supplier list. If user picks Add New, run the addition flow.
    Returns supplier name string.
    """
    print()
    selected = questionary.select(
        "Select supplier:",
        choices=sorted(SUPPLIERS, key=str.lower) + [
            questionary.Separator("─" * 44),
            questionary.Choice(
                title=[("class:register-option", "+ Add new supplier")],
                value="Add new supplier",
            ),
        ],
        qmark="",
        instruction=" ",
        style=_MENU_STYLE,
    ).unsafe_ask()
    chosen = "__ADD_NEW__" if selected == "Add new supplier" else selected

    if chosen != "__ADD_NEW__":
        return chosen

    # Add new supplier flow — type-first, fuzzy-match second
    raw = " ".join(input("\nEnter supplier name (or press Enter / type 'back' to go back): ").split())
    if not raw or raw.lower() == "back":
        print("Returning to supplier selection.")
        return select_or_add_supplier()

    if raw in SUPPLIERS:
        _warn(f"'{raw}' already exists. Please select it from the list.")
        return select_or_add_supplier()

    raw_lower = raw.lower()
    substring_matches = [s for s in SUPPLIERS if raw_lower in s.lower()]
    matches = substring_matches if substring_matches else difflib.get_close_matches(raw, SUPPLIERS, n=3, cutoff=0.6)

    if len(matches) == 1:
        print(f"\nClose match found: {matches[0]}")
        if ask_yes_no(f"Did you mean '{matches[0]}'?"):
            return matches[0]
        # user said no — fall through to add as new

    elif len(matches) > 1:
        print()
        pick = questionary.select(
            "Multiple close matches found — did you mean one of these?",
            choices=matches + [
                questionary.Separator("─" * 44),
                questionary.Choice(
                    title=[("class:register-option", "+ None of these — add as new")],
                    value="__NONE__",
                ),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if pick != "__NONE__":
            return pick
        # user picked "none of these" — proceed as genuinely new

    else:
        print(f"\nNo existing supplier matches that name.")

    new_name = raw
    print(f"\nYou are adding: {new_name}")
    confirmed = ask_yes_no("Confirm?")
    if not confirmed:
        print("Cancelled.")
        return select_or_add_supplier()

    # Write new supplier to Supplier tab
    try:
        sup_ws = connect_to_supplier_sheet()
        all_values = sup_ws.get_all_values()
        next_row = None
        for i, row in enumerate(all_values[3:200], start=4):  # start=4 → 1-based sheet row
            if len(row) < 2 or not row[1].strip():
                next_row = i
                break
        if next_row is None:
            next_row = len(all_values) + 1
        sup_ws.update(range_name=f"B{next_row}", values=[[_sheet_safe(new_name)]], value_input_option="USER_ENTERED")
    except Exception:
        print("Warning: Could not write to the Supplier tab. The supplier has been added in memory and will be usable this session, but please add it to the Supplier sheet manually.")

    SUPPLIERS.append(new_name)
    return new_name


# =============================================================================
# CUSTOMER AND COUNTRY HELPERS
# =============================================================================

def col_to_letter(n):
    """Convert 1-based column index to A1-notation column letters."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def load_garment_types():
    """
    Read column D of the Category Code tab (rows 4+) and build GARMENT_TYPES
    from all unique non-empty values, sorted alphabetically.
    Falls back to ["Blouse", "Dupatta", "Saree"] if the sheet cannot be reached.
    """
    global GARMENT_TYPES
    try:
        cat_ws = connect_to_category_code_sheet()
        all_values = cat_ws.get_all_values()
        seen = set()
        for row in all_values[3:]:  # skip 3 header rows
            if len(row) > 3:
                val = row[3].strip()
                if val:
                    seen.add(val)
        GARMENT_TYPES = sorted(seen, key=str.lower)
    except Exception:
        GARMENT_TYPES = ["Blouse", "Dupatta", "Saree"]


def load_suppliers():
    """
    Read column B of the Supplier tab (rows 4+) and merge all unique
    non-empty values into SUPPLIERS -- so a supplier added in a previous
    session (already written to the Supplier tab by select_or_add_supplier())
    is visible again on this run, instead of the picker only ever showing
    the hardcoded seed list plus whatever's been typed in *this* process.
    Merges rather than replaces: falls back to (and keeps) the existing
    SUPPLIERS list if the sheet can't be reached, and never drops a
    hardcoded name that hasn't made it into the Supplier tab yet.
    """
    global SUPPLIERS
    try:
        sup_ws = connect_to_supplier_sheet()
        all_values = sup_ws.get_all_values()
        seen = set()
        for row in all_values[3:]:  # skip 3 header rows, same convention as load_garment_types
            if len(row) > 1:
                val = row[1].strip()
                if val:
                    seen.add(val)
        SUPPLIERS = sorted(set(SUPPLIERS) | seen, key=str.lower)
    except Exception:
        pass  # keep the existing SUPPLIERS list if the sheet is unreachable


def load_weave_types():
    """
    Read columns B (Category Code), C (Weave Type), and D (Garment Type) of
    the Category Code tab (rows 4+) and merge any weave type not already in
    WEAVE_TYPES -- so a weave type added in a previous session (already
    written to the Category Code tab by select_or_add_weave_type()) is
    visible again on this run, instead of the picker only ever showing the
    hardcoded seed dict plus whatever's been typed in *this* process. Only
    ever adds -- never overwrites an existing entry's code/garment type and
    never removes one -- so a sheet read can't silently change what an
    already-known weave type maps to, and a network hiccup just leaves the
    existing dicts untouched.
    """
    global WEAVE_TYPES, WEAVE_GARMENT_TYPES
    try:
        cat_ws = connect_to_category_code_sheet()
        all_values = cat_ws.get_all_values()
        for row in all_values[3:]:  # skip 3 header rows, same convention as load_garment_types
            if len(row) > 2:
                code = row[1].strip()
                name = row[2].strip()
                garment_type = row[3].strip() if len(row) > 3 else ""
                if name and code and name not in WEAVE_TYPES:
                    WEAVE_TYPES[name] = code
                    WEAVE_GARMENT_TYPES[name] = garment_type or _infer_garment_type(name)
    except Exception:
        pass  # keep the existing WEAVE_TYPES / WEAVE_GARMENT_TYPES if the sheet is unreachable


def load_countries():
    """Fetch all countries from countriesnow.space once at startup. Populates COUNTRIES."""
    global COUNTRIES
    try:
        resp = requests.get(
            "https://countriesnow.space/api/v0.1/countries/codes",
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])
        if not data:
            raise ValueError("Empty country list returned.")
    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to the internet. Country data could not be loaded.")
        print("Please check your connection and try again.")
        raise SystemExit(1)
    except requests.exceptions.Timeout:
        print("Error: The country data service timed out. Please try again.")
        raise SystemExit(1)
    except Exception:
        print("Error: Could not load country reference data. Please try again.")
        raise SystemExit(1)

    parsed = []
    for c in data:
        try:
            name      = str(c.get("name", "")).strip()
            dial_code = str(c.get("dial_code", "")).strip()
            code      = str(c.get("code", "")).strip()
            if not name:
                continue
            parsed.append({"name": name, "dial_code": dial_code, "code": code})
        except Exception:
            continue

    COUNTRIES[:] = sorted(parsed, key=lambda x: x["name"])


def select_country():
    """
    Search-as-you-type country selection using plain input().
    Returns (country_name, dial_code), or (None, None) if cancelled.
    """
    display_entries = [
        {
            "display": f"{c['name']} ({c['dial_code']})" if c["dial_code"] else c["name"],
            "name": c["name"],
            "dial_code": c["dial_code"],
        }
        for c in COUNTRIES
    ]

    while True:
        raw = input("\nType to search by country name (or press Enter to go back): ").strip()
        if not raw:
            return None, None
        if raw.lower() == "back":
            return None, None

        query = raw.lower()
        matches = sorted(
            [e for e in display_entries if query in e["name"].lower()],
            key=lambda e: (0 if e["name"].lower().startswith(query) else 1, e["name"].lower()),
        )

        if not matches:
            _warn("No countries match that search. Try again.")
            continue

        if len(matches) == 1:
            entry = matches[0]
            confirmed = ask_yes_no(f"Did you mean {entry['display']}?")
            if confirmed:
                return entry["name"], entry["dial_code"]
            continue

        truncated = len(matches) > 10
        display_matches = matches[:10]
        print()
        for i, entry in enumerate(display_matches, 1):
            print(f"  {i}. {entry['display']}")
        if truncated:
            print(f"\n  Showing top 10 of {len(matches)} results. Type more characters to narrow down.")
            continue

        while True:
            raw_num = input("\nEnter the number of your choice (or press Enter to search again): ").strip()
            if not raw_num:
                break
            try:
                idx = int(raw_num) - 1
                if 0 <= idx < len(display_matches):
                    entry = display_matches[idx]
                    return entry["name"], entry["dial_code"]
                _warn(f"Please enter a number between 1 and {len(display_matches)}.")
            except ValueError:
                _warn("Please enter a valid number.")


def _customer_identity_key(name, country_code="", phone="", email=""):
    """Canonical identity key for grouping/matching customer rows across
    the sheet -- used everywhere a "which customer does this row belong
    to" decision is made, instead of comparing Customer Name strings
    directly. Prefers (country code + phone): phone is mandatory and
    enforced-unique at customer entry (see enter_new_customer's duplicate
    check against _find_customer_by_contact), so two unrelated people can
    share a name but can never share a phone number in this system's own
    data. Falls back to email, then to name alone only for rows with no
    phone on file at all -- data entered before phone became mandatory,
    or edited directly on the sheet outside the app.

    Customer Name is never a reliable row-matching key on its own -- two
    different customers can share a name, and a name-only comparison has
    already caused real bugs here (merged purchase histories, edits
    propagated onto a stranger's rows, duplicate-contact checks that
    didn't fire). Any new code that needs to decide whether two rows
    belong to the same customer should compare through this function,
    not by comparing Customer Name strings directly."""
    # gspread's get_all_records() (dict-row reads) infers types from cell
    # content -- a purely-numeric country code or phone cell comes back as
    # an int/float, not a string, unlike get_all_values() (raw-row reads),
    # which always returns strings. Coercing here means every caller can
    # pass a dict-row value straight through without knowing which read
    # path it came from.
    name         = str(name or "").strip()
    country_code = str(country_code or "")
    phone        = str(phone or "")
    email        = str(email or "")

    phone_digits = re.sub(r"\D", "", phone)
    code_digits  = re.sub(r"\D", "", country_code)
    if phone_digits and code_digits:
        return ("phone", code_digits, phone_digits)
    if email:
        return ("email", email.strip().lower())
    return ("name", name)


def build_customer_list(raw_rows):
    """
    Scan all rows and return a list of unique customers who have at least one
    Sold or Sold - Partial Payment transaction, with purchase count and most
    recent location details. Customers are grouped by identity (see
    _customer_identity_key), not by Customer Name alone -- two different
    people who happen to share a name are kept as separate entries as long
    as either has a phone or email on file.
    """
    name_col  = COLUMNS["Customer Name"] - 1
    status_col = COLUMNS["Status"] - 1
    code_col  = COLUMNS["Customer Country Code"] - 1
    phone_col = COLUMNS["Customer Phone"] - 1
    email_col = COLUMNS["Customer Email"] - 1
    city_col  = COLUMNS["Customer City"] - 1
    state_col = COLUMNS["Customer State"] - 1
    ctry_col  = COLUMNS["Customer Country"] - 1

    seen = {}
    for i, row in enumerate(raw_rows):
        if i == 0:
            continue
        name   = row[name_col].strip()  if len(row) > name_col   else ""
        status = row[status_col].strip() if len(row) > status_col else ""
        if not name or status not in ("Sold", "Sold - Partial Payment"):
            continue

        def _get(col): return row[col].strip() if len(row) > col else ""

        key = _customer_identity_key(name, _get(code_col), _get(phone_col), _get(email_col))

        if key not in seen:
            seen[key] = {"count": 0, "name": "", "country_code": "", "phone": "",
                         "email": "", "city": "", "state": "", "country": ""}
        seen[key]["count"] += 1
        # Always overwrite with latest non-blank values
        for field, col in [("name", name_col), ("country_code", code_col), ("phone", phone_col),
                           ("email", email_col), ("city", city_col),
                           ("state", state_col), ("country", ctry_col)]:
            val = name if field == "name" else _get(col)
            if val:
                seen[key][field] = val

    return sorted(seen.values(), key=lambda d: d["name"])


def find_customer_row_indices(name, raw_rows, country_code="", phone="", email=""):
    """Return 1-based sheet row numbers for every row belonging to this
    customer, matched by identity (see _customer_identity_key), not by
    Customer Name alone -- otherwise two different customers who share a
    name would have edits/backfills meant for one silently written onto
    the other's historical rows. Callers should pass the customer's
    identity as it existed *before* any in-progress edit (e.g. the
    original phone, not a newly-typed replacement) -- the search is for
    "every row that is currently this same person," which a not-yet-
    written new value would never match."""
    name_col  = COLUMNS["Customer Name"] - 1
    code_col  = COLUMNS["Customer Country Code"] - 1
    phone_col = COLUMNS["Customer Phone"] - 1
    email_col = COLUMNS["Customer Email"] - 1
    target_key = _customer_identity_key(name, country_code, phone, email)

    def _get(row, col): return row[col].strip() if len(row) > col else ""

    return [
        i + 1
        for i, row in enumerate(raw_rows)
        if i > 0 and _customer_identity_key(
            _get(row, name_col), _get(row, code_col), _get(row, phone_col), _get(row, email_col)
        ) == target_key
    ]


def _print_customer_block(c):
    """Render a styled customer profile block matching the confirmation summary style."""
    purchases_label = str(c['count'])
    phone_display = format_phone_display(c["country_code"], c["phone"], country=c["country"])
    contact_rows = [f"  {'Phone:':<18}{phone_display}"]
    if c["email"]:
        contact_rows.append(f"  {'Email:':<18}{c['email']}")
    location_rows = [f"  {'City:':<18}{c['city']}"]
    if c["state"]:
        location_rows.append(f"  {'State / Region:':<18}{c['state']}")
    location_rows.append(f"  {'Country:':<18}{c['country']}")
    _print_boxed("CUSTOMER PROFILE", [
        ("CUSTOMER", [f"  {'Name:':<18}{c['name']}", f"  {'Purchases:':<18}{purchases_label}"]),
        ("CONTACT", contact_rows),
        ("LOCATION", location_rows),
    ])


def search_and_select_customer(raw_rows, allow_new=True):
    """
    Present searchable customer list. Returns an existing customer dict,
    "__CANCEL__", or None -- or ("__NEW__", query) to signal new customer
    entry, only when allow_new is True.
    allow_new: whether registering a new customer is a valid outcome of
        this search. False for callers (Customer Insights) where every
        selectable outcome must already have purchase history -- offering
        "Register a new customer" there would be a dead end, since
        Insights has nothing to show for someone who hasn't bought
        anything yet.
    """
    customers = build_customer_list(raw_rows)

    if not customers:
        if allow_new:
            print("\nNo existing customers on record. Proceeding to new customer entry.")
        else:
            print("\nNo customers with purchase history on record.")
        return None

    _STYLE = Style.from_dict({
        "customer-location": "#777777",
        "register-option":   "#5fafff",
        "back-option":       "#777777",
    })

    while True:
        query = input("\nCustomer search (or press Enter to see all): ").strip()
        if _looks_like_phone_query(query):
            _warn("Customer names cannot be numbers. Please enter a name or press Enter to see all customers.")
            continue

        filtered = [c for c in customers if query.lower() in c["name"].lower()] if query else list(customers)

        if not filtered:
            _warn(f"No existing record found for '{query}'.")
            print()
            _no_match_choices = []
            if allow_new:
                _no_match_choices.append(
                    questionary.Choice(title=[("class:register-option", "+ Register as a new customer")], value="__NEW__")
                )
            _no_match_choices += [
                questionary.Choice(title="↩ Search again", value="__SEARCH__"),
                questionary.Separator(" "),
                _back_choice("Return to Main Menu"),
            ]
            action = questionary.select(
                "",
                choices=_no_match_choices,
                qmark="",
                instruction=" ",
                style=_STYLE,
            ).unsafe_ask()
            if action == "__NEW__":
                return ("__NEW__", query)
            if action is None or action == "Return to Main Menu":
                return "__CANCEL__"
            continue

        col_width = max(len(c["name"]) for c in filtered) + 4

        choices = []
        for c in filtered:
            location = ", ".join(p for p in [c["city"], c["country"]] if p)
            if location:
                label = [("", f"{c['name']:<{col_width}}"), ("class:customer-location", location)]
            else:
                label = c["name"]
            choices.append(questionary.Choice(title=label, value=c))

        if allow_new:
            choices.append(questionary.Separator("─" * 44))
            choices.append(questionary.Choice(
                title=[("class:register-option", "+ Register a new customer")],
                value="__NEW__",
            ))
        choices.append(questionary.Separator(" "))
        choices.append(_back_choice("Return to Main Menu"))

        print()
        selected = questionary.select(
            "Select customer:",
            choices=choices,
            qmark="",
            instruction=" ",
            style=_STYLE,
        ).unsafe_ask()

        if selected is None:
            return "__CANCEL__"
        if selected == "__NEW__":
            return ("__NEW__", query)
        if selected == "Return to Main Menu":
            return "__CANCEL__"
        print(f"\033[1A\033[2KSelect customer: \033[1;38;5;214m{selected['name']}\033[0m")
        return selected


def update_customer_details_if_needed(customer, raw_rows):
    """
    Display current customer details. If outdated, step through each field
    and batch-propagate any changes to all historical rows for that customer.
    Returns the (possibly updated) customer dict.
    """
    _print_customer_block(customer)

    if not ask_yes_no("Are any of these details outdated?"):
        return customer

    updated = dict(customer)
    field_updates = {}

    COL_TO_KEY = {
        "Customer Name":         "name",
        "Customer Country":      "country",
        "Customer Country Code": "country_code",
        "Customer Phone":        "phone",
        "Customer Email":        "email",
        "Customer City":         "city",
        "Customer State":        "state",
    }

    def _is_same_customer(other):
        """Whether `other` (a customer dict, e.g. a duplicate-contact match)
        is this same customer's own existing record, compared by identity
        (see _customer_identity_key) rather than by name -- a name-only
        comparison here would let a customer claim another same-named
        customer's already-registered phone/email, since the two would
        read as 'the same person' by name alone."""
        return _customer_identity_key(
            other["name"], other.get("country_code", ""), other.get("phone", ""), other.get("email", "")
        ) == _customer_identity_key(
            customer["name"], customer.get("country_code", ""), customer.get("phone", ""), customer.get("email", "")
        )

    _sep = f"\033[2m{'—' * 50}\033[0m"

    def _edit_field(field):
        if field == "Name":
            while True:
                new_name = ask_text("Customer Name:", blank_message="This field cannot be left blank. Please enter a name.")
                if not re.match(r"^[A-Za-z\s\-'\.]{2,}$", new_name):
                    _warn("Name can only contain letters, spaces, hyphens, apostrophes, and periods (minimum 2 characters).")
                    continue
                new_name = " ".join(new_name.split())
                if " " not in new_name:
                    print(f"\n\033[33m⚠  SINGLE NAME DETECTED\033[0m")
                    print(f"\033[2m{'—' * 50}\033[0m")
                    print("Adding a last name helps distinguish customers with the same first name.")
                    if ask_yes_no("Would you like to add a last name?"):
                        continue
                updated["name"] = new_name
                if new_name != customer["name"]:
                    field_updates["Customer Name"] = new_name
                else:
                    field_updates.pop("Customer Name", None)
                break

        elif field == "Country":
            new_country, new_dial = select_country()
            if new_country is not None:
                new_dial = _fmt_dial_code(new_dial)
                updated["country"] = new_country
                updated["country_code"] = new_dial
                if new_country != customer["country"] or new_dial != customer["country_code"]:
                    field_updates["Customer Country"] = new_country
                    field_updates["Customer Country Code"] = new_dial
                else:
                    field_updates.pop("Customer Country", None)
                    field_updates.pop("Customer Country Code", None)

        elif field == "Phone":
            while True:
                raw_phone = ask_text("Customer Phone:")
                stripped = re.sub(r"\D", "", raw_phone)
                err = _phone_length_error(updated.get("country_code", ""), stripped, updated.get("country", "this country"))
                if err:
                    _warn(err)
                    continue
                match = _find_customer_by_contact(raw_rows, phone=stripped, country_code=updated.get("country_code", ""))
                if match and not _is_same_customer(match):
                    formatted = format_phone_display(updated.get("country_code", ""), stripped, country=updated.get("country", ""))
                    _warn(f"Phone '{formatted}' is already registered to: {match['name']}. "
                          f"Duplicate phone numbers are not allowed. Please enter a different phone number.")
                    continue
                updated["phone"] = stripped
                if stripped != re.sub(r"\D", "", customer["phone"]):
                    field_updates["Customer Phone"] = stripped
                else:
                    field_updates.pop("Customer Phone", None)
                break

        elif field == "Email":
            while True:
                raw_email = ask_text("Customer Email (press Enter to skip):", required=False)
                if not raw_email:
                    break  # keep existing
                if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', raw_email):
                    _warn("Please enter a valid email address (e.g. name@domain.com), or press Enter to skip.")
                    continue
                match = _find_customer_by_contact(raw_rows, email=raw_email)
                if match and not _is_same_customer(match):
                    _warn(f"Email '{raw_email}' is already registered to: {match['name']}. "
                          f"Duplicate email addresses are not allowed. Please enter a different email address.")
                    continue
                updated["email"] = raw_email
                if raw_email != customer["email"]:
                    field_updates["Customer Email"] = raw_email
                else:
                    field_updates.pop("Customer Email", None)
                break

        elif field == "City":
            _city_country = updated.get("country") or customer.get("country") or ""
            while True:
                new_city = ask_text("Customer City:", blank_message="This field cannot be left blank. Please enter a city.")
                if not re.match(r"^[A-Za-z\s\-\.]{2,}$", new_city):
                    _warn("City name can only contain letters, spaces, hyphens, and periods (minimum 2 characters).")
                    continue
                if _city_country:
                    valid = _nominatim_city_valid(new_city, _city_country)
                    if valid is False:
                        _warn(f"'{new_city}' doesn't appear to be a recognised city in {_city_country}. Check the spelling and try again.")
                        continue
                updated["city"] = new_city
                if new_city != customer["city"]:
                    field_updates["Customer City"] = new_city
                else:
                    field_updates.pop("Customer City", None)
                break

        elif field == "State / Region":
            new_state = _resolve_state(updated.get("city", ""), updated.get("country", ""))
            updated["state"] = new_state
            if new_state != customer["state"]:
                field_updates["Customer State"] = new_state
            else:
                field_updates.pop("Customer State", None)

    # Canonical column order (mirrors the sheet's own layout via COLUMNS),
    # not whatever order the fields happened to be edited in -- so the
    # review table always reads top-to-bottom the same way regardless of
    # session path.
    _FIELD_ORDER = ["Customer Name", "Customer Country Code", "Customer Phone",
                     "Customer Email", "Customer City", "Customer State", "Customer Country"]

    def _pick_and_edit_field(has_changes):
        """Returns True (real change made), False (explicit stop -- Cancel
        before anything changed, Done once something has, or Ctrl+C), or
        None (the attempt produced no change -- caller should show the
        field picker again, not exit)."""
        _stop_choice = "Done — review changes" if has_changes else "Cancel — no changes"
        field = questionary.select(
            "Which field would you like to update?",
            choices=["Name", "Phone", "Email", "City", "State / Region", "Country",
                     questionary.Separator(" "),
                     _back_choice(_stop_choice)],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if field is None or field == _stop_choice:
            return False
        _before = dict(field_updates)
        _edit_field(field)
        if field_updates == _before:
            print("\n\033[33mNo change.\033[0m")
            return None
        return True

    def _collect_edits():
        """Loop the field picker freely -- edit as many fields as needed --
        without re-showing the accumulated diff after each one. Stops only
        when the user explicitly picks Cancel/Done or Ctrl+C's out."""
        while True:
            _result = _pick_and_edit_field(has_changes=bool(field_updates))
            if _result is False:
                return
            if _result is None:
                print()
                continue
            print()

    # First field selection — immediately after "yes, details are outdated"
    print()
    _collect_edits()

    # PROPOSED CHANGES review + confirm loop -- shown once per review pass,
    # not after every individual field edit. Re-checked every time this
    # loop is entered, not just once before it -- "Edit more fields"
    # re-invokes _collect_edits() and can legitimately net back to zero
    # (e.g. a field changed then changed back to its true original), which
    # would otherwise render an empty PROPOSED CHANGES box and still offer
    # to "Confirm and apply changes" on nothing.
    while True:
        if not field_updates:
            print("\n\033[33mNo changes made.\033[0m")
            return customer
        _rows = []
        for col_name in _FIELD_ORDER:
            if col_name not in field_updates:
                continue
            new_val = field_updates[col_name]
            old_raw = customer.get(COL_TO_KEY.get(col_name, ""), "") or ""
            if col_name == "Customer Phone":
                _cc      = updated.get("country_code") or customer.get("country_code") or ""
                _country = updated.get("country") or customer.get("country") or ""
                old_val     = format_phone_display(_cc, old_raw, country=_country) or old_raw or "(not set)"
                display_new = format_phone_display(_cc, new_val, country=_country) if new_val else "(cleared)"
            else:
                old_val     = old_raw or "(not set)"
                display_new = new_val if new_val else "(cleared)"
            _rows.append(f"  {col_name}: {old_val} → {display_new}")
        _print_boxed(None, [("PROPOSED CHANGES", _rows)])
        print()

        action = questionary.select(
            "Apply these changes?",
            choices=[
                "Confirm and apply changes",
                "Edit more fields",
                questionary.Separator(" "),
                _back_choice("Discard changes & continue"),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()

        if action == "Confirm and apply changes":
            all_indices = find_customer_row_indices(
                customer["name"], raw_rows,
                country_code=customer.get("country_code", ""),
                phone=customer.get("phone", ""),
                email=customer.get("email", ""),
            )
            n = len(all_indices)
            print(f"\n{n} row{'s' if n != 1 else ''} will be updated.")
            if not ask_yes_no("Confirm?"):
                continue  # back to review block
            ws = connect_to_sheet()
            cells = []
            for row_num in all_indices:
                for col_name, value in field_updates.items():
                    cells.append(gspread.Cell(row=row_num, col=COLUMNS[col_name], value=_sheet_safe(value)))
            ws.update_cells(cells, value_input_option="USER_ENTERED")
            print(f"\n\033[38;5;202m✓ {n} row{'s' if n != 1 else ''} updated successfully.\033[0m")
            return updated

        if action == "Edit more fields":
            print()
            _collect_edits()
            continue

        # Discard
        print(f"\n\033[38;5;202mUpdate cancelled. Existing customer profile retained.\033[0m")
        return customer


def _nominatim_lookup(city, country):
    """
    Fetch Nominatim result for city+country once per session.
    Returns {"found": bool, "parts": list} on success, None if the API call failed.
    """
    key = (city.lower(), country)
    if key in _nominatim_cache:
        return _nominatim_cache[key]
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"city": city, "country": country, "format": "json", "limit": 1},
            headers={"User-Agent": "LakshiraIMS/1.0", "Accept-Language": "en"},
            timeout=5,
        )
        data = r.json()
        if data:
            parts = [p.strip() for p in data[0].get("display_name", "").split(",")]
            result = {"found": True, "parts": parts}
        else:
            result = {"found": False, "parts": []}
    except Exception:
        result = None  # API unavailable — callers must treat as inconclusive
    _nominatim_cache[key] = result
    return result


def _nominatim_city_valid(city, country):
    """
    Return True if Nominatim recognises the city in the country,
    False if it doesn't, or None if the API was unreachable.
    """
    hit = _nominatim_lookup(city, country)
    if hit is None:
        return None
    return hit["found"]


def _nominatim_state_for_city(city, country):
    """Return extracted state/region string from Nominatim, or None."""
    hit = _nominatim_lookup(city, country)
    if not hit or not hit["found"]:
        return None
    parts = hit["parts"]
    state = parts[1] if len(parts) > 1 else ""
    if state.isdigit() and len(parts) > 2:
        state = parts[2]
    # Discard if Nominatim returned the country name instead of a state
    # (happens when the city is itself a federal state, e.g. Vienna, Austria)
    if state.lower() == country.lower():
        return None
    return state if state else None


def _resolve_state(city, country_name):
    """Return State/Region string for city+country; may be "" when the field is omitted."""
    if country_name == "United States":
        while True:
            state = ask_text(
                "Customer State / Region:",
                blank_message="This field cannot be left blank. Please enter a state.",
            )
            if len(state) < 3:
                _warn("Please enter the full state name (minimum 3 characters).")
                continue
            if not re.match(r"^[A-Za-z\s\-]{1,}$", state):
                _warn("State / Region can only contain letters, spaces, and hyphens.")
                continue
            return state

    auto = _nominatim_state_for_city(city, country_name)
    if auto:
        return auto

    while True:
        state = ask_text("Customer State / Region (press Enter to skip):", required=False)
        if not state:
            return ""
        if len(state) < 3:
            _warn("Please enter the full state or region name (minimum 3 characters).")
            continue
        if not re.match(r"^[A-Za-z\s\-]{1,}$", state):
            _warn("State / Region can only contain letters, spaces, and hyphens.")
            continue
        return state


def enter_new_customer(prefill_name="", raw_rows=None):
    """Full new customer entry flow. Returns a dict of customer fields."""
    print("\n--- \033[1;38;5;124mNEW CUSTOMER\033[0m ---")

    # Name + Country — outer loop: pressing Enter in country search returns here
    while True:
        if prefill_name:
            print(f"\nCustomer Name [{prefill_name}] (press Enter to accept or type to change):")
            raw = " ".join(input("> ").split())
            name = raw if raw else " ".join(prefill_name.split())
            prefill_name = ""  # only show prefill once
        else:
            name = ask_text("Customer Name:", blank_message="This field cannot be left blank. Please enter a name.")
        if not re.match(r"^[A-Za-z\s\-'\.]{2,}$", name):
            _warn("Name can only contain letters, spaces, hyphens, apostrophes, and periods (minimum 2 characters).")
            continue
        name = " ".join(name.split())
        if " " not in name:
            print(f"\n\033[33m⚠  SINGLE NAME DETECTED\033[0m")
            print(f"\033[2m{'—' * 50}\033[0m")
            print("Adding a last name helps distinguish customers with the same first name.")
            if ask_yes_no("Would you like to add a last name?"):
                continue

        # Fuzzy match against existing customers — runs on every invocation path
        if raw_rows:
            all_customers = build_customer_list(raw_rows)
            if all_customers:
                qlow = name.lower()
                matched_names = set()

                # Pass 1: case-insensitive substring of full name
                for c in all_customers:
                    if qlow in c["name"].lower():
                        matched_names.add(c["name"])

                # Pass 2: full-name fuzzy match
                for n in difflib.get_close_matches(
                    name, [c["name"] for c in all_customers], n=5, cutoff=0.6
                ):
                    matched_names.add(n)

                # Pass 3: token-level fuzzy match (catches 'Adil' → 'Adil Talari')
                all_tokens = {
                    token
                    for c in all_customers
                    for token in c["name"].split()
                }
                token_hits = set(difflib.get_close_matches(name, all_tokens, n=5, cutoff=0.6))
                if token_hits:
                    for c in all_customers:
                        if any(t in token_hits for t in c["name"].split()):
                            matched_names.add(c["name"])

                matches = [c for c in all_customers if c["name"] in matched_names]

                if matches:
                    print(f"\n\033[38;5;202mClose matches found:\033[0m")
                    print(f"\033[2m{'—' * 50}\033[0m")
                    for c in matches:
                        location = ", ".join(p for p in [c["city"], c["country"]] if p) or "location unknown"
                        purchases = f"{c['count']} purchase{'s' if c['count'] != 1 else ''}"
                        print(f"  {c['name']}  —  {location}  —  {purchases}")
                    if ask_yes_no("Proceed with an existing customer?"):
                        if len(matches) == 1:
                            c = matches[0]
                            location = ", ".join(p for p in [c["city"], c["country"]] if p) or "location unknown"
                            purchases = f"{c['count']} purchase{'s' if c['count'] != 1 else ''}"
                            print(f"\n\033[38;5;202mClose matches found:\033[0m")
                            print(f"\033[2m{'—' * 50}\033[0m")
                            print(f"  {c['name']}  —  {location}  —  {purchases}")
                            if ask_yes_no("Confirm?"):
                                return c
                            # declined — continue with new entry below
                        else:
                            print()
                            for i, c in enumerate(matches, 1):
                                location = ", ".join(p for p in [c["city"], c["country"]] if p) or "location unknown"
                                purchases = f"{c['count']} purchase{'s' if c['count'] != 1 else ''}"
                                print(f"  {i}. {c['name']}  —  {location}  —  {purchases}")
                            picked = None
                            while picked is None:
                                try:
                                    idx = int(input("\nEnter the number of your choice: ").strip()) - 1
                                    if 0 <= idx < len(matches):
                                        picked = matches[idx]
                                    else:
                                        _warn(f"Please enter a number between 1 and {len(matches)}.")
                                except ValueError:
                                    _warn("Please enter a valid number.")
                            location = ", ".join(p for p in [picked["city"], picked["country"]] if p) or "location unknown"
                            purchases = f"{picked['count']} purchase{'s' if picked['count'] != 1 else ''}"
                            print(f"\n\033[38;5;202mClose matches found:\033[0m")
                            print(f"\033[2m{'—' * 50}\033[0m")
                            print(f"  {picked['name']}  —  {location}  —  {purchases}")
                            if ask_yes_no("Confirm?"):
                                return picked
                            # declined — continue with new entry below

        country_name, dial_code = select_country()
        if country_name is None:
            continue  # back to name prompt
        dial_code = _fmt_dial_code(dial_code)
        break

    # Phone
    phone = ""
    while not phone:
        raw_phone = ask_text("Customer Phone:")
        stripped = re.sub(r"\D", "", raw_phone)
        err = _phone_length_error(dial_code, stripped, country_name)
        if err:
            _warn(err)
            continue
        if raw_rows:
            match = _find_customer_by_contact(raw_rows, phone=stripped, country_code=dial_code)
            if match:
                formatted = format_phone_display(dial_code, stripped, country=country_name)
                _warn(f"Phone '{formatted}' is already registered to: {match['name']}. "
                      f"Duplicate phone numbers are not allowed. Please enter a different phone number.")
                continue
        phone = stripped

    # Email (optional)
    email = None
    while email is None:
        raw_email = ask_text("Customer Email (press Enter to skip):", required=False)
        if not raw_email:
            email = ""
        elif not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', raw_email):
            _warn("Please enter a valid email address (e.g. name@domain.com), or press Enter to skip.")
        else:
            if raw_rows:
                match = _find_customer_by_contact(raw_rows, email=raw_email)
                if match:
                    _warn(f"Email '{raw_email}' is already registered to: {match['name']}. "
                          f"Duplicate email addresses are not allowed. Please enter a different email address.")
                    continue
            email = raw_email

    # City
    while True:
        city = ask_text("Customer City:", blank_message="This field cannot be left blank. Please enter a city.")
        if not re.match(r"^[A-Za-z\s\-\.]{2,}$", city):
            _warn("City name can only contain letters, spaces, hyphens, and periods (minimum 2 characters).")
            continue
        valid = _nominatim_city_valid(city, country_name)
        if valid is False:
            _warn(f"'{city}' doesn't appear to be a recognised city in {country_name}. Check the spelling and try again.")
            continue
        break

    # State / Region — resolved via API or prompted per _resolve_state() rules
    state = _resolve_state(city, country_name)

    # Review + edit loop
    while True:
        _contact_rows = [f"  {'Phone:':<18}{format_phone_display(dial_code, phone, country=country_name)}"]
        if email:
            _contact_rows.append(f"  {'Email:':<18}{email}")
        _location_rows = [f"  {'City:':<18}{city}"]
        if state:
            _location_rows.append(f"  {'State / Region:':<18}{state}")
        _location_rows.append(f"  {'Country:':<18}{country_name}")
        _print_boxed(None, [
            ("CUSTOMER", [f"  {'Name:':<18}{name}"]),
            ("CONTACT", _contact_rows),
            ("LOCATION", _location_rows),
        ])
        print()

        action = questionary.select(
            "Apply these changes?",
            choices=[
                "Confirm and apply changes",
                "Edit a field",
                questionary.Separator(" "),
                _back_choice("Discard changes & continue"),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()

        if action == "Confirm and apply changes":
            print(f"\n\033[38;5;202m✓ Customer profile saved.\033[0m")
            print(f"\n\033[2mProceeding with sale.\033[0m")
            return {
                "name": name,
                "country_code": dial_code,
                "phone": phone,
                "email": email,
                "city": city,
                "state": state,
                "country": country_name,
            }

        if action == "Discard changes & continue":
            return None

        # Edit a field — questionary arrow-key selection, re-displays block on return
        print()
        field = questionary.select(
            "Which field would you like to edit?",
            choices=["Name", "Phone", "Email", "City", "State / Region", "Country",
                     questionary.Separator(" "),
                     _back_choice("Cancel — no changes")],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()

        if field is None or field == "Cancel — no changes":
            continue  # back to review block

        if field == "Name":
            while True:
                new_name = ask_text("Customer Name:", blank_message="This field cannot be left blank. Please enter a name.")
                if not re.match(r"^[A-Za-z\s\-'\.]{2,}$", new_name):
                    _warn("Name can only contain letters, spaces, hyphens, apostrophes, and periods (minimum 2 characters).")
                    continue
                if " " not in new_name.strip():
                    print(f"\n\033[33m⚠  SINGLE NAME DETECTED\033[0m")
                    print(f"\033[2m{'—' * 50}\033[0m")
                    print("Adding a last name helps distinguish customers with the same first name.")
                    if ask_yes_no("Would you like to add a last name?"):
                        continue
                name = new_name
                break

        elif field == "Country":
            new_country, new_dial = select_country()
            if new_country is not None:
                country_name = new_country
                dial_code = _fmt_dial_code(new_dial)

        elif field == "Phone":
            while True:
                raw_phone = ask_text("Customer Phone:")
                stripped = re.sub(r"\D", "", raw_phone)
                err = _phone_length_error(dial_code, stripped, country_name)
                if err:
                    _warn(err)
                else:
                    if raw_rows:
                        match = _find_customer_by_contact(raw_rows, phone=stripped, country_code=dial_code)
                        if match:
                            formatted = format_phone_display(dial_code, stripped, country=country_name)
                            _warn(f"Phone '{formatted}' is already registered to: {match['name']}. "
                                  f"Duplicate phone numbers are not allowed. Please enter a different phone number.")
                            continue
                    phone = stripped
                    break

        elif field == "Email":
            while True:
                raw_email = ask_text("Customer Email (press Enter to skip):", required=False)
                if not raw_email:
                    email = ""
                    break
                elif not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', raw_email):
                    _warn("Please enter a valid email address (e.g. name@domain.com), or press Enter to skip.")
                else:
                    if raw_rows:
                        match = _find_customer_by_contact(raw_rows, email=raw_email)
                        if match:
                            _warn(f"Email '{raw_email}' is already registered to: {match['name']}. "
                                  f"Duplicate email addresses are not allowed. Please enter a different email address.")
                            continue
                    email = raw_email
                    break

        elif field == "City":
            while True:
                new_city = ask_text("Customer City:", blank_message="This field cannot be left blank. Please enter a city.")
                if not re.match(r"^[A-Za-z\s\-\.]{2,}$", new_city):
                    _warn("City name can only contain letters, spaces, hyphens, and periods (minimum 2 characters).")
                    continue
                valid = _nominatim_city_valid(new_city, country_name)
                if valid is False:
                    _warn(f"'{new_city}' doesn't appear to be a recognised city in {country_name}. Check the spelling and try again.")
                    continue
                city = new_city
                break

        elif field == "State / Region":
            state = _resolve_state(city, country_name)


def count_customer_purchases(customer_name, country_code="", phone="", email=""):
    """Count Sold / Sold - Partial Payment rows for this customer, matched
    by identity (see _customer_identity_key) rather than Customer Name
    alone. Re-reads sheet live."""
    raw = get_raw_rows()
    name_col   = COLUMNS["Customer Name"] - 1
    code_col   = COLUMNS["Customer Country Code"] - 1
    phone_col  = COLUMNS["Customer Phone"] - 1
    email_col  = COLUMNS["Customer Email"] - 1
    status_col = COLUMNS["Status"] - 1
    target_key = _customer_identity_key(customer_name, country_code, phone, email)

    def _get(row, col): return row[col].strip() if len(row) > col else ""

    count = 0
    for i, row in enumerate(raw):
        if i == 0:
            continue
        if _get(row, status_col) not in ("Sold", "Sold - Partial Payment"):
            continue
        row_key = _customer_identity_key(_get(row, name_col), _get(row, code_col),
                                          _get(row, phone_col), _get(row, email_col))
        if row_key == target_key:
            count += 1
    return count


def _find_customer_by_contact(raw_rows, phone=None, email=None, country_code=None):
    """
    Scan raw_rows for a row where (Customer Country Code + Customer Phone)
    exactly matches, or Customer Email (case-insensitive) matches. Returns
    a customer dict with count, or None.

    Phone matching requires both country code and phone digits to match
    exactly -- a bare digit-suffix match (e.g. one number ending with
    another) is not enough. A phone number is only unique in combination
    with its country code, so two customers in different countries whose
    numbers happen to share trailing digits are correctly treated as
    different people, and a single mistyped digit no longer risks
    colliding with an unrelated customer's real number.
    """
    name_col   = COLUMNS["Customer Name"] - 1
    code_col   = COLUMNS["Customer Country Code"] - 1
    phone_col  = COLUMNS["Customer Phone"] - 1
    email_col  = COLUMNS["Customer Email"] - 1
    city_col   = COLUMNS["Customer City"] - 1
    state_col  = COLUMNS["Customer State"] - 1
    ctry_col   = COLUMNS["Customer Country"] - 1
    status_col = COLUMNS["Status"] - 1

    for i, row in enumerate(raw_rows):
        if i == 0:
            continue

        def _get(col):
            return row[col].strip() if len(row) > col else ""

        matched = False
        if phone is not None and country_code is not None:
            stored_digits = re.sub(r"\D", "", _get(phone_col))
            input_digits  = re.sub(r"\D", "", phone)
            stored_code   = re.sub(r"\D", "", _get(code_col))
            input_code    = re.sub(r"\D", "", country_code)
            if (stored_digits and input_digits and stored_digits == input_digits
                    and stored_code and input_code and stored_code == input_code):
                matched = True
        if email is not None and not matched:
            stored = _get(email_col)
            if stored and stored.lower() == email.lower():
                matched = True

        if not matched:
            continue

        cname = _get(name_col)
        if not cname:
            continue

        target_key = _customer_identity_key(cname, _get(code_col), _get(phone_col), _get(email_col))

        def _get2(r, col): return r[col].strip() if len(r) > col else ""

        count = 0
        for j, r in enumerate(raw_rows):
            if j == 0:
                continue
            if _get2(r, status_col) not in ("Sold", "Sold - Partial Payment"):
                continue
            row_key = _customer_identity_key(_get2(r, name_col), _get2(r, code_col),
                                              _get2(r, phone_col), _get2(r, email_col))
            if row_key == target_key:
                count += 1
        return {
            "name": cname,
            "country_code": _get(code_col),
            "phone": _get(phone_col),
            "email": _get(email_col),
            "city": _get(city_col),
            "state": _get(state_col),
            "country": _get(ctry_col),
            "count": count,
        }
    return None


# =============================================================================
# PHASE 4 — THREE CORE OPERATIONS
# =============================================================================

def add_new_inventory():
    print("\n--- \033[1;38;5;124mADD NEW INVENTORY\033[0m ---")

    while True:
        raw = input("\nHow many units are you adding? (press Enter for 1): ").strip()
        if not raw:
            batch_size = 1
            break
        try:
            batch_size = int(raw)
            if batch_size < 1:
                _warn("Please enter a number of 1 or more.")
                continue
            break
        except ValueError:
            _warn("Please enter a valid whole number.")

    if batch_size == 1:
        _add_single_unit()
    else:
        _add_bulk_units(batch_size)


def _add_single_unit():
    print()

    # Step 1: Weave type
    weave_type, category_code = select_or_add_weave_type()
    if weave_type is None:
        return
    print(f"\nWeave Type: {weave_type}  |  Category Code: {category_code}")

    # Step 2: SKU resolution (Unassigned check first)
    sku, existing_row = resolve_sku(category_code, weave_type)
    if sku is None:
        return

    # Step 3: Remaining fields
    source_sheet = ask_text("Source Sheet + Tab (e.g. 'Sheet1 - Tab2'):", blank_message="This field cannot be left blank. Please enter a sheet reference.")
    supplier = select_or_add_supplier()
    date_acquired = ask_date("Date Acquired", not_future=True,
                  future_msg="Acquisition cannot be recorded in the future. Please enter a current or past date.")
    date_acquired_str = date_acquired.strftime("%m-%d-%Y")

    base_price = ask_number("Base Price + GST Tax (INR):", allow_zero=False)
    while True:
        raw = input("\nShipping Cost (INR) [default: 750]: ").strip()
        if not raw:
            shipping = 750.0
            break
        try:
            shipping = float(raw)
            if shipping < 0:
                _warn("Please enter a positive number or 0.")
                continue
            break
        except ValueError:
            _warn("Please enter a valid number (e.g. 750 or 0).")
    while True:
        raw = input("\nDetailing Cost (INR) (press Enter if none): ").strip()
        if not raw:
            design = 0.0
            break
        try:
            design = float(raw)
            if design < 0:
                _warn("Please enter a positive number or 0.")
                continue
            break
        except ValueError:
            _warn("Please enter a valid number (e.g. 500 or 0).")
    total_cost_inr = round(base_price + shipping + design, 2)
    print(f"\nTotal Cost (INR): {total_cost_inr:,.2f}")

    # Step 4: ECB rate — loop until confirmed; date_acquired_str always holds user-entered date
    while True:
        ecb_rate, rate_date_str = fetch_ecb_rate(date_acquired_str)
        if rate_date_str == "abort":
            print("\nCould not fetch the exchange rate.")
            if not ask_yes_no("Retry the lookup? (Everything you've entered so far is still saved.)"):
                print("Returning to Main Menu.")
                return
            continue
        if ecb_rate is not None:
            break
        print("\nNote: The acquisition date determines the ECB rate used to calculate")
        print("Total Cost (USD) and all downstream pricing dimensions for this unit.")
        date_acquired = ask_date("Enter the corrected acquisition date", not_future=True)
        date_acquired_str = date_acquired.strftime("%m-%d-%Y")
    total_cost_usd = round(total_cost_inr / ecb_rate, 2)
    _sep = f"\033[2m{'—' * 50}\033[0m"
    print(f"\n\033[38;5;202mCOST BASIS\033[0m")
    print(_sep)
    print(f"  {'Total Cost:':<14}${total_cost_usd:,.2f}")
    print(_sep)

    # Step 5: Pricing path (loops until valid margin or explicit cancel)
    while True:
        print()
        path_choice = questionary.select(
            "How would you like to set the selling price?",
            choices=[
                "Enter a markup percentage",
                "Enter a selling price (USD)",
                questionary.Separator(" "),
                _back_choice("Discard & exit"),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if path_choice == "Discard & exit":
            print("\nAdd cancelled. Returning to Main Menu.")
            return
        if "markup" in path_choice.lower():
            pricing_path = "markup"
            pricing_value = ask_percent("Markup percentage (e.g. 60 for 60%):", allow_zero=False)
        else:
            pricing_path = "usd"
            pricing_value = ask_number("Selling Price (USD):", allow_zero=False)

        pricing = calculate_pricing(total_cost_inr, ecb_rate, pricing_path, pricing_value)

        _mc      = get_margin_color(pricing['margin_pct'])
        _muc     = get_markup_color(pricing['markup_pct'])
        _gp_sign = signed(pricing['gross_profit_usd'])
        _gpc     = "\033[92m" if pricing['gross_profit_usd'] >= 0 else "\033[91m"
        print(f"\n\033[38;5;202mPRICING PREVIEW\033[0m")
        print(_sep)
        print(f"  {'Total Cost:':<16}${pricing['total_cost_usd']:,.2f}")
        print()
        print(f"  {'Selling Price:':<16}${pricing['selling_price_usd']:,.2f}")
        print(f"  {'Markup %:':<16}{_muc}{pricing['markup_pct']:.1f}%\033[0m")
        print(f"  {'Gross Profit:':<16}{_gpc}{_gp_sign}${abs(pricing['gross_profit_usd']):,.2f}\033[0m")
        print(f"  {'Margin %:':<16}{_mc}{pricing['margin_pct']:.1f}%\033[0m")
        print(_sep)

        proceed, warned = check_pricing_warnings(pricing["margin_pct"], pricing["markup_pct"], pricing["gross_profit_usd"])
        if proceed is None:
            print("\nAdd cancelled. Returning to Main Menu.")
            return
        if proceed:
            break

    # Step 6: Optional notes
    inventory_notes = ask_text("Inventory Notes (press Enter to skip):", required=False)

    # Step 8: Auto-populated exchange rate note (always appended)
    ecb_note = f"[{rate_date_str} · ECB] Exchange rate: 1 USD = {ecb_rate:,.2f} INR"
    if inventory_notes:
        full_inventory_notes = f"[{rate_date_str} · NOTE] {inventory_notes}\n{ecb_note}"
    else:
        full_inventory_notes = ecb_note

    # Step 9: Days in Inventory
    days_in_inventory = (date.today() - date_acquired).days

    # Step 10: Confirmation summary
    _gp_sign = signed(pricing['gross_profit_usd'])
    _mc  = get_margin_color(pricing['margin_pct'])
    _muc = get_markup_color(pricing['markup_pct'])
    _gpc = "\033[92m" if pricing['gross_profit_usd'] >= 0 else "\033[91m"

    _notes_rows = [f"  {line}" for line in full_inventory_notes.split("\n")] if full_inventory_notes else []
    _print_boxed("ADD SUMMARY", [
        ("UNIT", [
            f"  {'SKU:':<22}{sku}",
            f"  {'Category Code:':<22}{category_code}",
            f"  {'Weave Type:':<22}{weave_type}",
            f"  {'Source Sheet + Tab:':<22}{source_sheet}",
            f"  {'Supplier:':<22}{supplier}",
            f"  {'Date Acquired:':<22}{date_acquired_str}",
        ]),
        ("COST", [
            f"  {'Base + GST (INR):':<22}{base_price:,.2f}",
            f"  {'Shipping (INR):':<22}{shipping:,.2f}",
            f"  {'Detailing Cost (INR):':<22}{design:,.2f}",
            f"  {'Total Cost (INR):':<22}{total_cost_inr:,.2f}",
            f"  {'Total Cost (USD):':<22}${pricing['total_cost_usd']:,.2f}",
        ]),
        ("PRICING", [
            f"  {'Selling Price (USD):':<22}${pricing['selling_price_usd']:,.2f}",
            f"  {'Selling Price (INR):':<22}{pricing['selling_price_inr_derived']:,.2f}",
            f"  {'Markup %:':<22}{_muc}{pricing['markup_pct']:.1f}%\033[0m",
            f"  {'Gross Profit (USD):':<22}{_gpc}{_gp_sign}${abs(pricing['gross_profit_usd']):,.2f}\033[0m",
            f"  {'Margin %:':<22}{_mc}{pricing['margin_pct']:.1f}%\033[0m",
        ]),
        ("STATUS", [
            f"  {'Days in Inventory:':<22}{days_in_inventory}",
            f"  {'Status:':<22}Available",
        ]),
        ("NOTES", _notes_rows),
    ])

    confirmed = ask_yes_no("Write this to the master sheet?")
    if not confirmed:
        print("\nAdd cancelled. Returning to Main Menu.")
        return

    # Step 11: Build row data
    row_data = {
        "SKU": sku,
        "Category Code": category_code,
        "Weave Type / Cluster": weave_type,
        "Source Sheet + Tab": source_sheet,
        "Supplier": supplier,
        "Date Acquired": date_acquired_str,
        "Base Price + GST Tax (INR)": round(base_price, 2),
        "Shipping Cost (INR)": round(shipping, 2),
        "Design Detailing Cost (INR)": round(design, 2),
        "Total Cost (INR)": total_cost_inr,
        "Total Cost (USD)": pricing["total_cost_usd"],
        "Selling Price (USD)": pricing["selling_price_usd"],
        "Selling Price (INR) - Provided": "",
        "Selling Price (INR) - Derived": pricing["selling_price_inr_derived"],
        "Discount %": "",
        "Actual Selling Price (USD)": pricing["selling_price_usd"],
        "Gross Profit (USD)": pricing["gross_profit_usd"],
        "Markup %": round(pricing["markup_pct"] / 100, 6),
        "(Profit) Margin %": round(pricing["margin_pct"] / 100, 6),
        "Status": "Available",
        "Inventory Notes": full_inventory_notes,
        "Transaction Notes": "",
    }

    # Step 12: Write — either update existing Unassigned row or append new row
    if existing_row is not None:
        # Re-check right before writing: the Unassigned row was picked back
        # in Step 2, and everything since (supplier, cost fields, ECB rate
        # fetch) can take a minute or more -- long enough for a second
        # concurrent session to have claimed the same Unassigned row in the
        # meantime. Same guard already used for record_sale(), reprice_unit(),
        # cancel_sale(), and manage_reservation() -- this call site was
        # missing it.
        if not _status_unchanged(existing_row, "Unassigned"):
            _warn("This unit's status has changed since you started (no longer 'Unassigned'). "
                  "Someone else may have just claimed it — nothing was written. Please re-check the SKU.")
            return
        update_row(existing_row, row_data)
    else:
        # Re-check right before writing: the SKU was picked back in Step 2,
        # and everything since (supplier, cost fields, ECB rate fetch) can
        # take a minute or more — long enough for a second concurrent
        # session to have claimed the same next-serial SKU in the meantime.
        # Auto-regenerate rather than fail outright so this operator's
        # already-entered data isn't lost to a race they had no way to see.
        if find_row_index_by_sku(sku) is not None:
            new_sku = generate_next_sku(category_code, get_raw_rows())
            _warn(f"{sku} was just claimed by another session. Reassigning this unit to {new_sku}.")
            sku = new_sku
            row_data["SKU"] = sku
        append_row(row_data)

    print(f"\n\033[38;5;202m✓ {sku} added successfully.\033[0m")


def _add_bulk_units(batch_size):
    """
    Bulk intake for N > 1 units in one shipment. Reuses every underlying
    building block _add_single_unit() uses (select_or_add_weave_type(),
    resolve_sku(), check_pricing_warnings(), calculate_pricing(),
    append_row(), update_row()) -- this is deliberately the same
    per-field validation and duplicate/collision protection, just looped
    and with the genuinely batch-constant fields (Source Sheet + Tab,
    Supplier, Date Acquired, ECB rate) asked once instead of per unit.
    Garment Type and Weave Type stay per-unit since a shipment can mix
    garment types. Nothing is written to the sheet until the final
    batch-wide confirmation -- each unit is only held in memory until then,
    so Ctrl+C at any point during entry safely discards the whole
    in-progress batch via the app's existing top-level interrupt handler.
    """
    print(f"\n--- BULK INTAKE MODE ({batch_size} units) ---")

    # Batch-common fields
    source_sheet = ask_text("Source Sheet + Tab (e.g. 'Sheet1 - Tab2'):", blank_message="This field cannot be left blank. Please enter a sheet reference.")
    supplier = select_or_add_supplier()
    date_acquired = ask_date("Date Acquired", not_future=True,
                  future_msg="Acquisition cannot be recorded in the future. Please enter a current or past date.")
    date_acquired_str = date_acquired.strftime("%m-%d-%Y")

    # ECB rate is derived entirely from Date Acquired, which is already
    # batch-common -- fetching it once here (rather than per unit) avoids
    # N-1 redundant calls to the same external rate API for a batch that
    # all shares one acquisition date.
    while True:
        ecb_rate, rate_date_str = fetch_ecb_rate(date_acquired_str)
        if rate_date_str == "abort":
            print("\nCould not fetch the exchange rate.")
            if not ask_yes_no("Retry the lookup? (Everything you've entered so far is still saved.)"):
                print("Returning to Main Menu.")
                return
            continue
        if ecb_rate is not None:
            break
        print("\nNote: The acquisition date determines the ECB rate used to calculate")
        print("Total Cost (USD) and all downstream pricing dimensions for this batch.")
        date_acquired = ask_date("Enter the corrected acquisition date", not_future=True)
        date_acquired_str = date_acquired.strftime("%m-%d-%Y")

    _sep = f"\033[2m{'—' * 50}\033[0m"
    _eq  = "=" * 50

    units = []
    for i in range(1, batch_size + 1):
        print(f"\n--- UNIT INTAKE ({i} of {batch_size}) ---")

        weave_type, category_code = select_or_add_weave_type()
        if weave_type is None:
            print(f"\nUnit {i} skipped — no data will be recorded for this unit.")
            continue
        garment_type = WEAVE_GARMENT_TYPES.get(weave_type, "Saree")
        print(f"\nWeave Type: {weave_type}  |  Category Code: {category_code}")

        sku, existing_row = resolve_sku(category_code, weave_type)
        if sku is None:
            print(f"\nUnit {i} skipped — no data will be recorded for this unit.")
            continue

        base_price = ask_number("Base Price + GST Tax (INR):", allow_zero=False)
        while True:
            raw = input("\nShipping Cost (INR) [default: 750]: ").strip()
            if not raw:
                shipping = 750.0
                break
            try:
                shipping = float(raw)
                if shipping < 0:
                    _warn("Please enter a positive number or 0.")
                    continue
                break
            except ValueError:
                _warn("Please enter a valid number (e.g. 750 or 0).")
        while True:
            raw = input("\nDetailing Cost (INR) (press Enter if none): ").strip()
            if not raw:
                design = 0.0
                break
            try:
                design = float(raw)
                if design < 0:
                    _warn("Please enter a positive number or 0.")
                    continue
                break
            except ValueError:
                _warn("Please enter a valid number (e.g. 500 or 0).")
        total_cost_inr = round(base_price + shipping + design, 2)
        total_cost_usd_preview = round(total_cost_inr / ecb_rate, 2)
        print(f"\n\033[38;5;202mCOST BASIS\033[0m")
        print(_sep)
        print(f"  {'Total Cost:':<14}${total_cost_usd_preview:,.2f}")
        print(_sep)

        skipped = False
        pricing = None
        while True:
            print()
            path_choice = questionary.select(
                "How would you like to set the selling price?",
                choices=[
                    "Enter a markup percentage",
                    "Enter a selling price (USD)",
                    questionary.Separator(" "),
                    _back_choice("Skip this unit"),
                ],
                qmark="",
                instruction=" ",
                style=_MENU_STYLE,
            ).unsafe_ask()
            if path_choice == "Skip this unit":
                skipped = True
                break
            if "markup" in path_choice.lower():
                pricing_path = "markup"
                pricing_value = ask_percent("Markup percentage (e.g. 60 for 60%):", allow_zero=False)
            else:
                pricing_path = "usd"
                pricing_value = ask_number("Selling Price (USD):", allow_zero=False)

            pricing = calculate_pricing(total_cost_inr, ecb_rate, pricing_path, pricing_value)

            _mc      = get_margin_color(pricing['margin_pct'])
            _muc     = get_markup_color(pricing['markup_pct'])
            _gp_sign = signed(pricing['gross_profit_usd'])
            _gpc     = "\033[92m" if pricing['gross_profit_usd'] >= 0 else "\033[91m"
            print(f"\n\033[38;5;202mPRICING PREVIEW\033[0m")
            print(_sep)
            print(f"  {'Total Cost:':<16}${pricing['total_cost_usd']:,.2f}")
            print()
            print(f"  {'Selling Price:':<16}${pricing['selling_price_usd']:,.2f}")
            print(f"  {'Markup %:':<16}{_muc}{pricing['markup_pct']:.1f}%\033[0m")
            print(f"  {'Gross Profit:':<16}{_gpc}{_gp_sign}${abs(pricing['gross_profit_usd']):,.2f}\033[0m")
            print(f"  {'Margin %:':<16}{_mc}{pricing['margin_pct']:.1f}%\033[0m")
            print(_sep)

            proceed, warned = check_pricing_warnings(
                pricing["margin_pct"], pricing["markup_pct"], pricing["gross_profit_usd"],
                exit_label="Skip this unit",
            )
            if proceed is None:
                skipped = True
                break
            if proceed:
                break

        if skipped:
            print(f"\nUnit {i} skipped — no data will be recorded for this unit.")
            continue

        inventory_notes = ask_text("Inventory Notes (press Enter to skip):", required=False)
        ecb_note = f"[{rate_date_str} · ECB] Exchange rate: 1 USD = {ecb_rate:,.2f} INR"
        if inventory_notes:
            full_inventory_notes = f"[{rate_date_str} · NOTE] {inventory_notes}\n{ecb_note}"
        else:
            full_inventory_notes = ecb_note

        days_in_inventory = (date.today() - date_acquired).days

        _gp_sign = signed(pricing['gross_profit_usd'])
        _mc  = get_margin_color(pricing['margin_pct'])
        _muc = get_markup_color(pricing['markup_pct'])
        _gpc = "\033[92m" if pricing['gross_profit_usd'] >= 0 else "\033[91m"

        _notes_rows = [f"  {line}" for line in full_inventory_notes.split("\n")] if full_inventory_notes else []
        _print_boxed("ADD SUMMARY", [
            ("UNIT", [
                f"  {'SKU:':<22}{sku}",
                f"  {'Category Code:':<22}{category_code}",
                f"  {'Weave Type:':<22}{weave_type}",
                f"  {'Source Sheet + Tab:':<22}{source_sheet}",
                f"  {'Supplier:':<22}{supplier}",
                f"  {'Date Acquired:':<22}{date_acquired_str}",
            ]),
            ("COST", [
                f"  {'Base + GST (INR):':<22}{base_price:,.2f}",
                f"  {'Shipping (INR):':<22}{shipping:,.2f}",
                f"  {'Detailing Cost (INR):':<22}{design:,.2f}",
                f"  {'Total Cost (INR):':<22}{total_cost_inr:,.2f}",
                f"  {'Total Cost (USD):':<22}${pricing['total_cost_usd']:,.2f}",
            ]),
            ("PRICING", [
                f"  {'Selling Price (USD):':<22}${pricing['selling_price_usd']:,.2f}",
                f"  {'Selling Price (INR):':<22}{pricing['selling_price_inr_derived']:,.2f}",
                f"  {'Markup %:':<22}{_muc}{pricing['markup_pct']:.1f}%\033[0m",
                f"  {'Gross Profit (USD):':<22}{_gpc}{_gp_sign}${abs(pricing['gross_profit_usd']):,.2f}\033[0m",
                f"  {'Margin %:':<22}{_mc}{pricing['margin_pct']:.1f}%\033[0m",
            ]),
            ("STATUS", [
                f"  {'Days in Inventory:':<22}{days_in_inventory}",
                f"  {'Status:':<22}Available",
            ]),
            ("NOTES", _notes_rows),
        ])

        confirmed = ask_yes_no("Add this unit to the batch?")
        if not confirmed:
            print(f"\nUnit {i} skipped — no data will be recorded for this unit.")
            continue

        row_data = {
            "SKU": sku,
            "Category Code": category_code,
            "Weave Type / Cluster": weave_type,
            "Source Sheet + Tab": source_sheet,
            "Supplier": supplier,
            "Date Acquired": date_acquired_str,
            "Base Price + GST Tax (INR)": round(base_price, 2),
            "Shipping Cost (INR)": round(shipping, 2),
            "Design Detailing Cost (INR)": round(design, 2),
            "Total Cost (INR)": total_cost_inr,
            "Total Cost (USD)": pricing["total_cost_usd"],
            "Selling Price (USD)": pricing["selling_price_usd"],
            "Selling Price (INR) - Provided": "",
            "Selling Price (INR) - Derived": pricing["selling_price_inr_derived"],
            "Discount %": "",
            "Actual Selling Price (USD)": pricing["selling_price_usd"],
            "Gross Profit (USD)": pricing["gross_profit_usd"],
            "Markup %": round(pricing["markup_pct"] / 100, 6),
            "(Profit) Margin %": round(pricing["margin_pct"] / 100, 6),
            "Status": "Available",
            "Inventory Notes": full_inventory_notes,
            "Transaction Notes": "",
        }

        units.append({
            "row_data": row_data,
            "sku": sku,
            "existing_row": existing_row,
            "category_code": category_code,
            "weave_type": weave_type,
            "garment_type": garment_type,
            "total_cost_usd": pricing["total_cost_usd"],
            "selling_price_usd": pricing["selling_price_usd"],
            "markup_pct": pricing["markup_pct"],
            "gross_profit_usd": pricing["gross_profit_usd"],
            "margin_pct": pricing["margin_pct"],
        })

    if not units:
        print("\nNo units were entered — nothing to write. Returning to Main Menu.")
        return

    # Tier 2 -- condensed, grouped-by-garment-type batch summary. Deliberately
    # not a 1:1 repeat of each unit's full tier-1 confirmation -- the
    # margin/loss check already happened once per unit when it mattered;
    # showing all 16 fields again per unit here would be re-reading, not
    # re-checking. Grouping by garment type (rather than a Garment Type
    # column) surfaces a mixed-category batch more clearly than a repeated
    # column value would, without adding a column for something that isn't
    # actually a sheet field.
    print(f"\n--- BULK INTAKE SUMMARY ({len(units)} unit{'s' if len(units) != 1 else ''}) ---")
    groups = {}
    for u in units:
        groups.setdefault(u["garment_type"], []).append(u)

    for garment_type in sorted(groups.keys()):
        print(f"\n\033[38;5;202m{garment_type.upper()}\033[0m")
        print(f"  {'SKU':<16}{'Weave Type':<24}{'Total Cost':<13}{'Selling Price':<15}{'Markup':<9}{'Gross Profit':<15}Margin")
        for u in groups[garment_type]:
            cost_str   = f"${u['total_cost_usd']:,.2f}"
            price_str  = f"${u['selling_price_usd']:,.2f}"
            markup_str = f"{u['markup_pct']:.1f}%"
            gp_str     = f"${u['gross_profit_usd']:,.2f}"
            margin_str = f"{u['margin_pct']:.1f}%"
            print(
                f"  {u['sku']:<16}{u['weave_type']:<24}"
                f"{cost_str:<13}{price_str:<15}{markup_str:<9}{gp_str:<15}{margin_str}"
            )

    print(f"\n  Supplier: {supplier}")
    print(f"  Date Acquired: {date_acquired_str}")
    print(f"  Source Sheet + Tab: {source_sheet}")

    confirmed = ask_yes_no(f"\nWrite all {len(units)} unit{'s' if len(units) != 1 else ''} to the master sheet?")
    if not confirmed:
        print("\nBulk add cancelled. Nothing was written. Returning to Main Menu.")
        return

    # Final write loop -- same collision/status-recheck protection as
    # single-unit entry, applied per unit (not once for the whole batch),
    # since even a few units' worth of time is enough for another session
    # to have changed something between when this unit was gathered and
    # when it's actually written.
    # Tracks SKUs this batch has already written to the sheet, so a collision
    # against one of THIS batch's own earlier units (routine when a batch has
    # multiple units of the same weave type -- generate_next_sku() has no
    # visibility into still-uncommitted batch units) can be told apart from a
    # genuine collision against another session's write.
    skus_written_this_batch = set()
    succeeded, skipped_at_write, failed_sku, failed_err, not_attempted = [], [], None, None, []
    for idx, u in enumerate(units):
        if failed_sku is not None:
            not_attempted.append(u["sku"])
            continue
        try:
            if u["existing_row"] is not None:
                if not _status_unchanged(u["existing_row"], "Unassigned"):
                    _warn(f"{u['sku']}: status changed since selected (no longer 'Unassigned') — skipped.")
                    skipped_at_write.append(u["sku"])
                    continue
                update_row(u["existing_row"], u["row_data"])
                succeeded.append(u["sku"])
            else:
                sku = u["sku"]
                if find_row_index_by_sku(sku) is not None:
                    new_sku = generate_next_sku(u["category_code"], get_raw_rows())
                    if sku in skus_written_this_batch:
                        _warn(f"{sku} was already used earlier in this batch (same weave type). Reassigning this unit to {new_sku}.")
                    else:
                        _warn(f"{sku} was just claimed by another session. Reassigning this unit to {new_sku}.")
                    sku = new_sku
                    u["row_data"]["SKU"] = sku
                append_row(u["row_data"])
                succeeded.append(sku)
                skus_written_this_batch.add(sku)
        except Exception as e:
            failed_sku, failed_err = u["sku"], str(e)

    if succeeded:
        print(f"\n\033[38;5;202m✓ {len(succeeded)} unit{'s' if len(succeeded) != 1 else ''} added successfully: {', '.join(succeeded)}\033[0m")
    if skipped_at_write:
        print(f"\n\033[33m⚠  {len(skipped_at_write)} unit{'s' if len(skipped_at_write) != 1 else ''} skipped due to a status change since selection: {', '.join(skipped_at_write)}\033[0m")
    if failed_sku is not None:
        print(f"\n\033[91m✗ Failed to write {failed_sku}: {failed_err}\033[0m")
        if not_attempted:
            print(f"\033[91m  The following units were not attempted: {', '.join(not_attempted)}\033[0m")


def ask_payment_method():
    method = questionary.select(
        "Method of Payment:",
        choices=["Cash", "Digital Transfer (CashApp, PayPal, Venmo, Zelle)", "Point-of-Sale (POS)"],
        qmark="",
        instruction=" ",
    ).unsafe_ask()
    if method == "Digital Transfer (CashApp, PayPal, Venmo, Zelle)":
        print()
        method = questionary.select(
            "Digital transfer method:",
            choices=["Zelle", "Venmo", "PayPal", "CashApp"],
            qmark="",
            instruction=" ",
        ).unsafe_ask()
    return method


def record_sale():
    print("\n--- \033[1;38;5;124mRECORD A SALE\033[0m ---")

    # Steps 1–2: SKU lookup + confirmation (loops on wrong unit)
    while True:
        sku = ask_text("SKU:", blank_message="SKU cannot be left blank. Please enter a valid SKU (e.g. LAH-KKVSV500).")
        row_index = find_row_index_by_sku(sku)
        if row_index is None:
            _warn(f"SKU '{sku}' was not found in the master sheet. Please check the SKU and try again.")
            continue

        row = get_row_by_sheet_index(row_index)
        current_status = row.get("Status", "").strip()

        if current_status == "Unassigned":
            _warn(f"SKU '{sku}' is Unassigned. It cannot be sold. Please assign it first via 'Add New Inventory'.")
            continue
        if current_status in ("Sold", "Sold - Partial Payment"):
            _warn(f"SKU '{sku}' has already been recorded as '{current_status}'. A sale cannot be recorded again.")
            continue
        if current_status not in ("Available", "Reserved"):
            _warn(f"SKU '{sku}' has status '{current_status}', which is not valid for recording a sale.")
            continue

        _status_color = get_status_color(current_status)
        _res_expired  = False

        # Both feed every pricing figure this operation computes and then
        # permanently writes to the sale record -- a blank one here means
        # Gross Profit/Markup/Margin get written from a fabricated $0.00,
        # corrupting real business metrics downstream in Generate Report.
        _cost_cols = ["Total Cost (USD)", "Selling Price (USD)"]
        _blank_cost_cols = [c for c in _cost_cols if not str(row.get(c, "")).strip()]
        if _blank_cost_cols:
            _label = _blank_cost_cols[0] if len(_blank_cost_cols) == 1 else f"{len(_blank_cost_cols)} fields"
            _warn(f"{_label} blank or unreadable on the sheet for this unit -- every figure below that "
                  f"depends on it is being shown as $0.00, which may not be correct. Verify directly on "
                  f"the sheet before relying on these numbers.")

        selling_price_fmt = f"${_sheet_float(row.get('Selling Price (USD)', '')):,.2f}"

        _unit_rows = [
            f"  {'SKU:':<18}{row['SKU']}",
            f"  {'Weave Type:':<18}{row['Weave Type / Cluster']}",
            f"  {'Supplier:':<18}{row['Supplier']}",
            f"  {'Date Acquired:':<18}{row['Date Acquired']}",
            f"  {'Selling Price:':<18}{selling_price_fmt}",
        ]
        _sections = [("UNIT", _unit_rows)]
        if current_status == "Reserved":
            _rdate        = row.get("Reserved Date", "").strip()
            _reserver_cur = _get_reserver_name(row.get("Inventory Notes", "")) or "—"
            _res_rows = [
                f"  {'Reserved By:':<18}{_reserver_cur}",
                f"  {'Reserved Date:':<18}{_rdate}",
            ]
            if _rdate:
                _days_res, _res_expired = _reservation_days(_rdate)
                if _days_res is not None:
                    _dr_color = "\033[91m" if _res_expired else "\033[92m"
                    _res_rows.append(f"  {'Days Reserved:':<18}{_dr_color}{_days_res} of 7\033[0m")
            _res_rows.append(f"  {'Status:':<18}{_status_color}{current_status}\033[0m")
            _sections.append(("RESERVATION", _res_rows))
        else:
            _sections.append(("STATUS", [f"  {'Status:':<18}{_status_color}{current_status}\033[0m"]))
        _print_boxed("CURRENT RECORD", _sections)
        if _res_expired:
            _warn("Reservation has exceeded the 7-day maximum.")
        if ask_yes_no("Is this the correct unit?"):
            break
        print()

    # Step 3: Sale details
    date_acquired = _safe_parse_date(row.get("Date Acquired", ""))
    if date_acquired is None:
        _warn("Date Acquired is blank or unreadable on the sheet for this unit -- cannot validate "
              "Date Sold against it. Please correct Date Acquired on the sheet before recording this sale.")
        print("\nSale cancelled. Returning to Main Menu.")
        return
    date_sold = ask_date("Date Sold", not_future=True, not_before=date_acquired,
                  future_msg="Sales cannot be recorded in the future. Please enter a current or past date.")
    date_sold_str = date_sold.strftime("%m-%d-%Y")

    print()
    sales_channel = questionary.select(
        "Sales Channel:",
        choices=SALES_CHANNELS + [questionary.Separator(" "), _back_choice("Return to Main Menu")],
        qmark="",
        instruction=" ",
    ).unsafe_ask()
    if sales_channel is None or sales_channel == "Return to Main Menu":
        print("\nSale cancelled. Returning to Main Menu.")
        return

    # Customer lookup — loops if user returns from new-customer review
    raw_rows_for_customer = get_raw_rows()
    while True:
        selected = search_and_select_customer(raw_rows_for_customer)
        if selected == "__CANCEL__":
            print("\nSale cancelled. Returning to Main Menu.")
            return
        if selected is None:
            customer = enter_new_customer(raw_rows=raw_rows_for_customer)
        elif isinstance(selected, tuple) and selected[0] == "__NEW__":
            customer = enter_new_customer(prefill_name=selected[1], raw_rows=raw_rows_for_customer)
        else:
            customer = update_customer_details_if_needed(selected, raw_rows_for_customer)
        if customer is not None:
            break

    customer_name         = customer["name"]
    customer_country_code = customer["country_code"]
    customer_phone        = customer["phone"]
    customer_email        = customer["email"]
    customer_city         = customer["city"]
    customer_state        = customer["state"]
    customer_country      = customer["country"]

    # If the stored country code is blank but the country name is known, derive it now
    # so both the new sale row and the backfill get the correct value.
    if not customer_country_code and customer_country:
        _derived_cc = _calling_code_for_country(customer_country)
        if _derived_cc:
            customer_country_code = _fmt_dial_code(_derived_cc)

    # Step 4: Discount + tiered profit alert
    has_discount = ask_yes_no("Was a discount applied to this sale?")
    discount_pct = None

    selling_price_usd = _sheet_float(row["Selling Price (USD)"])
    total_cost_usd = _sheet_float(row["Total Cost (USD)"])

    if has_discount:
        discount_pct = ask_percent("Discount percentage:", allow_zero=False)

    # Step 5: Recalculate pricing with discount; alerts only fire when a discount was entered
    if not has_discount:
        actual_selling_price_usd = selling_price_usd
        gross_profit_usd = round(actual_selling_price_usd - total_cost_usd, 2)
        markup_pct = round((gross_profit_usd / total_cost_usd) * 100, 2) if total_cost_usd else 0
        margin_pct = round((gross_profit_usd / actual_selling_price_usd) * 100, 2) if actual_selling_price_usd else 0
    else:
        while True:
            # discount_pct can go back to None mid-loop (operator removed it
            # below) -- re-evaluating from the top each pass, rather than
            # exiting immediately after removing it, is what catches the
            # unit's own full price still being below cost or low-margin
            # (units can already be added/repriced at a thin/negative margin
            # via "proceed anyway" at those steps, so this isn't hypothetical).
            if discount_pct is not None:
                actual_selling_price_usd = round(selling_price_usd * (1 - discount_pct / 100), 2)
            else:
                actual_selling_price_usd = selling_price_usd
            gross_profit_usd = round(actual_selling_price_usd - total_cost_usd, 2)
            markup_pct = round((gross_profit_usd / total_cost_usd) * 100, 2) if total_cost_usd else 0
            margin_pct = round((gross_profit_usd / actual_selling_price_usd) * 100, 2) if actual_selling_price_usd else 0

            _pricing_desc = "This discount puts" if discount_pct is not None else "This unit's full price (no discount) puts"
            if gross_profit_usd < 0:
                print(f"\n\033[91m⚠  BELOW COST\033[0m")
                print(f"\033[2m{'—' * 50}\033[0m")
                print(f"{_pricing_desc} Gross Profit at ${gross_profit_usd:,.2f}. The unit will sell at a loss.")
            elif margin_pct < 15:
                print(f"\n{get_margin_color(margin_pct)}△ LOW MARGIN — Margin sits at {margin_pct:.1f}%, below the 15% threshold.\033[0m")
            else:
                break

            print()
            _proceed_label = "Proceed with current discount" if discount_pct is not None else "Proceed at full price"
            _choices = ["Re-enter discount percentage"]
            if discount_pct is not None:
                _choices.append("Remove discount and proceed at full price")
            _choices += [_proceed_label, questionary.Separator(" "), _back_choice("Discard & exit")]
            alert_choice = questionary.select(
                "How would you like to proceed?",
                choices=_choices,
                qmark="",
                instruction=" ",
                style=_MENU_STYLE,
            ).unsafe_ask()
            if alert_choice == "Discard & exit":
                print("\nSale cancelled. Returning to Main Menu.")
                return
            if alert_choice == "Re-enter discount percentage":
                discount_pct = ask_percent("Discount percentage:", allow_zero=False)
                continue
            if alert_choice == "Remove discount and proceed at full price":
                discount_pct = None
                continue
            break

    # Step 6: Payment status
    print()
    if actual_selling_price_usd <= 0:
        # A 100% discount (or already-$0 pricing) leaves nothing owed --
        # offering "Partial payment" here is a dead end: the amount-received
        # prompt below requires a positive amount but rejects anything above
        # the (zero) full price, so no input could ever satisfy both and the
        # prompt would loop forever.
        payment_choice = "Paid in full"
    else:
        payment_choice = questionary.select(
            "Payment status:",
            choices=["Paid in full", "Partial payment"],
            qmark="",
            instruction=" ",
        ).unsafe_ask()
    if payment_choice == "Paid in full":
        amount_received = actual_selling_price_usd
        amount_outstanding = 0.0
        new_status = "Sold"
    else:
        while True:
            amount_received = ask_number(
                f"Amount Received (USD) (full price is ${actual_selling_price_usd:,.2f}):",
                allow_zero=False,
            )
            if amount_received <= actual_selling_price_usd:
                break
            _warn(f"Amount received (${amount_received:,.2f}) cannot exceed the actual selling price (${actual_selling_price_usd:,.2f}). Please enter a valid amount.")
        amount_outstanding = round(actual_selling_price_usd - amount_received, 2)
        if amount_outstanding <= 0:
            # Entered amount rounds up to the full price — this is a full
            # payment, not a partial one, regardless of which menu option
            # the operator picked. Mirrors record_outstanding_payment()'s
            # own "payment >= outstanding -> fully settled" rule, so a unit
            # never ends up "Sold - Partial Payment" with $0.00 actually owed.
            amount_outstanding = 0.0
            new_status = "Sold"
        else:
            new_status = "Sold - Partial Payment"

    # Step 7: Payment method
    payment_method = ask_payment_method()

    # Step 8: Confirmation summary
    days_to_sell = (date_sold - date_acquired).days
    original_markup_pct = round((selling_price_usd - total_cost_usd) / total_cost_usd * 100, 2) if total_cost_usd else 0
    orig_gp_usd         = round(selling_price_usd - total_cost_usd, 2)
    orig_margin_pct     = round(orig_gp_usd / selling_price_usd * 100, 2) if selling_price_usd else 0
    orig_gp_sign        = signed(orig_gp_usd)
    orig_m_sign         = signed(orig_margin_pct)
    gp_sign  = signed(gross_profit_usd)
    mar_sign = signed(margin_pct)
    _orig_gpc = "\033[92m" if orig_gp_usd >= 0 else "\033[91m"
    _disc_gpc = "\033[92m" if gross_profit_usd >= 0 else "\033[91m"

    _gp_label = "Original Margin:" if discount_pct else "Margin:"
    _pricing_rows = [
        f"  {'Total Cost:':<22}${total_cost_usd:,.2f}",
        f"  {'Selling Price:':<22}${selling_price_usd:,.2f}",
        f"  {'Original Markup:':<22}{original_markup_pct:.1f}%",
        f"  {_gp_label:<22}{get_margin_color(orig_margin_pct)}{orig_m_sign}{abs(orig_margin_pct):.1f}%\033[0m ({_orig_gpc}{orig_gp_sign}${abs(orig_gp_usd):,.2f}\033[0m)",
    ]

    if discount_pct:
        discount_amount = round(selling_price_usd - actual_selling_price_usd, 2)
        if gross_profit_usd >= 0:
            discount_impact = f"Discounted ${discount_amount:,.2f}, kept ${gross_profit_usd:,.2f}"
        else:
            discount_impact = f"Discounted ${discount_amount:,.2f}, lost ${abs(gross_profit_usd):,.2f} below cost"
        _pricing_rows.append("")
        _pricing_rows.append(f"  {'Discount Applied:':<22}{discount_pct:.1f}% (-${discount_amount:,.2f})")
        _pricing_rows.append(f"  {'Unit Sold For:':<22}${actual_selling_price_usd:,.2f}")
        _pricing_rows.append(f"  {'Discount Impact:':<22}{discount_impact}")
        _pricing_rows.append(f"  {'Discounted Margin:':<22}{get_margin_color(margin_pct)}{mar_sign}{abs(margin_pct):.1f}%\033[0m ({_disc_gpc}{gp_sign}${abs(gross_profit_usd):,.2f}\033[0m)")

    _status_color = get_status_color(new_status)
    _payment_rows = [
        f"  {'Status:':<22}{_status_color}{new_status}\033[0m",
        f"  {'Payment Method:':<22}{payment_method}",
    ]
    if payment_choice != "Paid in full":
        _payment_rows.append(f"  {'Full Price:':<22}${actual_selling_price_usd:,.2f}")
    _payment_rows.append(f"  {'Amount Received:':<22}${amount_received:,.2f}")
    if payment_choice == "Paid in full":
        _payment_rows.append(f"  {'Amount Outstanding:':<22}$0.00")
    else:
        _payment_rows.append(f"  {'Amount Outstanding:':<22}${amount_outstanding:,.2f}")

    _print_boxed("SALE SUMMARY", [
        ("TRANSACTION", [
            f"  {'SKU:':<22}{sku}",
            f"  {'Date Sold:':<22}{date_sold_str}",
            f"  {'Days to Sell:':<22}{days_to_sell}",
            f"  {'Sales Channel:':<22}{sales_channel}",
            f"  {'Customer:':<22}{customer_name}",
        ]),
        ("PRICING", _pricing_rows),
        ("PAYMENT", _payment_rows),
    ])

    confirmed = ask_yes_no("Write this sale to the master sheet?")
    if not confirmed:
        print("\nSale cancelled. Returning to Main Menu.")
        return

    updates = {
        "Date Sold": date_sold_str,
        "Sales Channel": sales_channel,
        "Customer Name": customer_name,
        "Customer Country Code": _fmt_dial_code(customer_country_code),
        "Customer Phone": customer_phone,
        "Customer Email": customer_email,
        "Customer City": customer_city,
        "Customer State": customer_state,
        "Customer Country": customer_country,
        "Status": new_status,
        "Actual Selling Price (USD)": actual_selling_price_usd,
        "Gross Profit (USD)": gross_profit_usd,
        "Markup %": round(markup_pct / 100, 6),
        "(Profit) Margin %": round(margin_pct / 100, 6),
    }
    if discount_pct is not None:
        updates["Discount %"] = round(discount_pct / 100, 6)
    if current_status == "Reserved":
        updates["Reserved Date"] = ""
    if new_status == "Sold - Partial Payment":
        updates["Amount Received (USD)"] = round(amount_received, 2)
        updates["Amount Outstanding (USD)"] = amount_outstanding

    if not _status_unchanged(row_index, current_status):
        _warn(f"This unit's status has changed since you started (no longer '{current_status}'). "
              f"Someone else may have just updated it — nothing was written. Please re-check the SKU.")
        return

    # Total Cost (USD) and Selling Price (USD) aren't written by this
    # operation, but every figure in this sale record was derived from them
    # -- if either drifted since Step 1 (e.g. a concurrent reprice or cost
    # edit during this session's customer/discount/payment steps, which can
    # run long), the Gross Profit/Markup/Margin about to be permanently
    # written no longer matches the unit's actual cost or price.
    _unchanged, _conflict_col = _row_fields_unchanged(
        row_index, row, updates.keys() - {"Status"} | {"Total Cost (USD)", "Selling Price (USD)"}
    )
    if not _unchanged:
        _warn(f"This unit's '{_conflict_col}' has changed since you started — someone else may have "
              f"just updated it. Nothing was written. Please re-check the SKU.")
        return

    # Append rather than overwrite -- Transaction Notes is the only record
    # a report can later scan for a prior cancellation on this unit (via the
    # "[MM-DD-YYYY - CANCEL]" tag). Overwriting it here would silently erase
    # that history the moment a previously-cancelled unit sells again,
    # permanently hiding the cancellation from every future report. Read
    # fresh at commit time (not the Step-1 snapshot) so a concurrent
    # session's own note isn't discarded either.
    if new_status == "Sold":
        auto_note = f"[{date_sold_str} · SALE] Paid in full: ${amount_received:,.2f} received via {payment_method}."
        updates["Transaction Notes"] = _fresh_notes_append(row_index, "Transaction Notes", [auto_note])
    elif new_status == "Sold - Partial Payment":
        auto_note = (
            f"[{date_sold_str} · SALE] Initial partial payment of ${round(amount_received, 2):,.2f} received via {payment_method}. "
            f"${amount_outstanding:,.2f} outstanding."
        )
        updates["Transaction Notes"] = _fresh_notes_append(row_index, "Transaction Notes", [auto_note])

    if current_status == "Reserved":
        updates["Inventory Notes"] = _strip_reservation_note(
            get_row_by_sheet_index(row_index).get("Inventory Notes", "")
        )

    update_row(row_index, updates)

    # Backfill blank customer-level cells in prior rows (returning customers only).
    # raw_rows_for_customer was fetched before the new row was written, so it only
    # contains historical rows — the current SKU row is never in prior_indices.
    if not isinstance(selected, (type(None), tuple)):
        _backfill_fields = {
            "Customer Country Code": _fmt_dial_code(customer_country_code),
            "Customer Phone":        customer_phone,
            "Customer Email":        customer_email,
            "Customer City":         customer_city,
            "Customer State":        customer_state,
            "Customer Country":      customer_country,
        }
        # Matched on `selected` -- the customer's identity as of the
        # pre-sale snapshot -- not `customer`, which may reflect an
        # in-session detail edit (update_customer_details_if_needed
        # above) not yet reflected in raw_rows_for_customer.
        prior_indices = find_customer_row_indices(
            selected["name"], raw_rows_for_customer,
            country_code=selected.get("country_code", ""),
            phone=selected.get("phone", ""),
            email=selected.get("email", ""),
        )
        backfill_cells = []
        for row_num in prior_indices:
            raw_row = raw_rows_for_customer[row_num - 1]
            for col_name, new_val in _backfill_fields.items():
                if not new_val:
                    continue
                col_idx = COLUMNS[col_name] - 1
                current = raw_row[col_idx].strip() if len(raw_row) > col_idx else ""
                if not current:
                    backfill_cells.append(gspread.Cell(row=row_num, col=COLUMNS[col_name], value=_sheet_safe(new_val)))
        if backfill_cells:
            ws = connect_to_sheet()
            ws.update_cells(backfill_cells, value_input_option="USER_ENTERED")

    print(f"\n\033[38;5;202m✓ {sku} sold to {customer_name}.\033[0m")
    purchase_count = count_customer_purchases(customer_name, customer_country_code, customer_phone, customer_email)
    print(f"\n{customer_name} now has {purchase_count} purchase{'s' if purchase_count != 1 else ''} on record.")


def reprice_unit():
    print("\n--- \033[1;38;5;124mREPRICE A UNIT\033[0m ---")

    # Steps 1–2: SKU lookup + confirmation (loops on wrong unit or invalid status)
    def _pct(val):
        try:
            return float(str(val).replace("%", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return 0.0

    while True:
        sku = ask_text("SKU:", blank_message="SKU cannot be left blank. Please enter a valid SKU (e.g. LAH-KKVSV500).")
        row_index = find_row_index_by_sku(sku)
        if row_index is None:
            _warn(f"SKU '{sku}' was not found in the master sheet. Please check the SKU and try again.")
            continue

        row = get_row_by_sheet_index(row_index)
        current_status = row.get("Status", "").strip()

        if current_status != "Available":
            _warn(f"SKU '{sku}' has status '{current_status}'. Only Available units can be repriced.")
            continue

        _markup_pct   = _pct(row.get("Markup %", 0))
        _margin_pct   = _pct(row.get("(Profit) Margin %", 0))
        _status_color = get_status_color(current_status)

        # See edit_inventory_details()'s identical check -- no cost cell
        # should ever be genuinely blank, so a blank one here means every
        # figure below (and the new price this operation is about to
        # compute) is being built on a fabricated zero, not a real cost.
        _cost_cols = ["Total Cost (INR)", "Total Cost (USD)"]
        _blank_cost_cols = [c for c in _cost_cols if not str(row.get(c, "")).strip()]
        if _blank_cost_cols:
            _label = _blank_cost_cols[0] if len(_blank_cost_cols) == 1 else f"{len(_blank_cost_cols)} cost fields"
            _warn(f"{_label} blank or unreadable on the sheet for this unit -- every figure below that "
                  f"depends on it is being shown as $0.00, which may not be correct. Verify directly on "
                  f"the sheet before relying on these numbers.")

        _print_boxed("CURRENT RECORD", [
            ("UNIT", [
                f"  {'SKU:':<19}{row['SKU']}",
                f"  {'Weave Type:':<19}{row['Weave Type / Cluster']}",
                f"  {'Supplier:':<19}{row['Supplier']}",
                f"  {'Date Acquired:':<19}{row['Date Acquired']}",
            ]),
            ("PRICING", [
                f"  {'Total Cost:':<19}${_sheet_float(row['Total Cost (USD)']):,.2f}",
                f"  {'Selling Price:':<19}${_sheet_float(row['Selling Price (USD)']):,.2f}",
                f"  {'Current Markup %:':<19}{get_markup_color(_markup_pct)}{_markup_pct:.1f}%\033[0m",
                f"  {'Current Margin %:':<19}{get_margin_color(_margin_pct)}{_margin_pct:.1f}%\033[0m",
            ]),
            ("STATUS", [f"  {'Status:':<19}{_status_color}{current_status}\033[0m"]),
        ])

        if ask_yes_no("Is this the correct unit?"):
            break
        print()

    # Step 3: Fetch ECB rate for original acquisition date (single attempt — date is fixed)
    total_cost_inr = _sheet_float(row["Total Cost (INR)"])
    total_cost_usd = _sheet_float(row["Total Cost (USD)"])
    date_acquired_str = row["Date Acquired"]
    while True:
        ecb_rate, rate_date_str = fetch_ecb_rate(date_acquired_str, total_cost_usd=total_cost_usd)
        if ecb_rate is not None:
            break
        if rate_date_str != "abort":
            print("\nReprice cancelled. Returning to Main Menu.")
            return
        print("\nCould not fetch the exchange rate.")
        if not ask_yes_no("Retry the lookup?"):
            print("Reprice cancelled. Returning to Main Menu.")
            return

    old_selling_price = _sheet_float(row["Selling Price (USD)"])
    old_markup_pct = _markup_pct
    old_margin_pct = _margin_pct

    # Step 4: Pricing path (loops until confirmed or cancelled)
    _sep = f"\033[2m{'—' * 50}\033[0m"
    while True:
        print()
        path_choice = questionary.select(
            "How would you like to set the new selling price?",
            choices=[
                "Enter a markup percentage",
                "Enter a selling price (USD)",
                questionary.Separator(" "),
                _back_choice("Discard & exit"),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if path_choice == "Discard & exit":
            print("\nReprice cancelled. Returning to Main Menu.")
            return
        if "markup" in path_choice.lower():
            pricing_value = ask_percent("New markup percentage:", allow_zero=False)
            pricing_path = "markup"
        else:
            pricing_value = ask_number("New Selling Price (USD):", allow_zero=False)
            pricing_path = "usd"

        pricing = calculate_pricing(total_cost_inr, ecb_rate, pricing_path, pricing_value)

        _mc          = get_margin_color(pricing['margin_pct'])
        _muc         = get_markup_color(pricing['markup_pct'])
        _gp_sign     = signed(pricing['gross_profit_usd'])
        _gpc         = "\033[92m" if pricing['gross_profit_usd'] >= 0 else "\033[91m"
        _price_chg     = pricing['selling_price_usd'] - old_selling_price
        _price_chg_pct = (_price_chg / old_selling_price * 100) if old_selling_price else 0.0
        _price_sign    = "+" if _price_chg >= 0 else ""
        _mu_delta      = pricing['markup_pct'] - old_markup_pct
        _mg_delta      = pricing['margin_pct'] - old_margin_pct
        _mu_sign       = "+" if _mu_delta >= 0 else ""
        _mg_sign       = "+" if _mg_delta >= 0 else ""
        print(f"\n\033[38;5;202mPRICING PREVIEW\033[0m")
        print(_sep)
        print(f"  {'Total Cost:':<17}${total_cost_usd:,.2f}")
        print()
        print(f"  {'Original Price:':<17}${old_selling_price:,.2f}")
        print(f"  {'New Price:':<17}${pricing['selling_price_usd']:,.2f}")
        print(f"  {'Price Change:':<17}{_price_sign}${abs(_price_chg):,.2f} ({_price_sign}{_price_chg_pct:.1f}%)")
        print()
        print(f"  {'Markup %:':<17}{_muc}{pricing['markup_pct']:.1f}%\033[0m  \033[2m({_mu_sign}{_mu_delta:.1f}%)\033[0m")
        print(f"  {'Gross Profit:':<17}{_gpc}{_gp_sign}${abs(pricing['gross_profit_usd']):,.2f}\033[0m")
        print(f"  {'Margin %:':<17}{_mc}{pricing['margin_pct']:.1f}%\033[0m  \033[2m({_mg_sign}{_mg_delta:.1f}%)\033[0m")
        print(_sep)

        proceed, warned = check_pricing_warnings(pricing["margin_pct"], pricing["markup_pct"], pricing["gross_profit_usd"])
        if proceed is None:
            print("\nReprice cancelled. Returning to Main Menu.")
            return
        if proceed:
            break

    # Step 5: Confirmation summary
    _print_boxed("REPRICE SUMMARY", [
        ("UNIT", [
            f"  {'SKU:':<17}{sku}",
        ]),
        ("PRICING", [
            f"  {'Total Cost:':<17}${total_cost_usd:,.2f}",
            "",
            f"  {'Original Price:':<17}${old_selling_price:,.2f}",
            f"  {'New Price:':<17}${pricing['selling_price_usd']:,.2f}",
            "",
            f"  {'Gross Profit:':<17}{_gpc}{_gp_sign}${abs(pricing['gross_profit_usd']):,.2f}\033[0m",
            f"  {'Markup %:':<17}{_muc}{pricing['markup_pct']:.1f}%\033[0m",
            f"  {'Margin %:':<17}{_mc}{pricing['margin_pct']:.1f}%\033[0m",
        ]),
        ("EXCHANGE RATE", [
            f"  {rate_date_str + ':':<17}1 USD = {ecb_rate:,.2f} INR",
        ]),
    ])

    confirmed = ask_yes_no("Write this reprice to the master sheet?")
    if not confirmed:
        print("\nReprice cancelled. Returning to Main Menu.")
        return

    # Step 6: Append reprice note to Inventory Notes — never overwrite
    today_str = date.today().strftime("%m-%d-%Y")
    reprice_note = f"[{today_str} · REPRICE] Repriced: ${old_selling_price:,.2f} → ${pricing['selling_price_usd']:,.2f}"

    updates = {
        "Selling Price (USD)": pricing["selling_price_usd"],
        "Selling Price (INR) - Provided": "",
        "Selling Price (INR) - Derived": pricing["selling_price_inr_derived"],
        "Actual Selling Price (USD)": pricing["actual_selling_price_usd"],
        "Gross Profit (USD)": pricing["gross_profit_usd"],
        "Markup %": round(pricing["markup_pct"] / 100, 6),
        "(Profit) Margin %": round(pricing["margin_pct"] / 100, 6),
    }

    if not _status_unchanged(row_index, current_status):
        _warn(f"This unit's status has changed since you started (no longer '{current_status}'). "
              f"Someone else may have just updated it — nothing was written. Please re-check the SKU.")
        return

    # Total Cost isn't written by this operation, but every figure above was
    # derived from it -- if it drifted since Step 1 (e.g. someone edited
    # cost fields via Edit Inventory Details mid-reprice), the pricing this
    # write is about to commit no longer matches the unit's actual cost, so
    # it has to be checked here even though it's not itself being updated.
    _unchanged, _conflict_col = _row_fields_unchanged(
        row_index, row, updates.keys() | {"Total Cost (INR)", "Total Cost (USD)"}
    )
    if not _unchanged:
        _warn(f"This unit's '{_conflict_col}' has changed since you started — someone else may have "
              f"just updated it. Nothing was written. Please re-check the SKU.")
        return

    updates["Inventory Notes"] = _fresh_notes_append(row_index, "Inventory Notes", [reprice_note])
    update_row(row_index, updates)
    print(f"\n\033[38;5;202m✓ {sku} repriced: ${old_selling_price:,.2f} → ${pricing['selling_price_usd']:,.2f}\033[0m")


def edit_inventory_details():
    """
    Correct an existing Available or Reserved unit's acquisition-side
    details -- Weave Type/Category Code/SKU are deliberately excluded
    (SKU's category-code prefix would go stale), Status is excluded (owned
    by Record a Sale/Cancel a Sale/Manage Reservation), and Selling Price
    is excluded (owned by Reprice a Unit). Unassigned units are excluded
    too -- Add Inventory's own Unassigned-reuse path already owns "fill in
    a blank unit's details", so this operation would just be a second way
    to do that exact job.
    """
    print("\n--- \033[1;38;5;124mEDIT INVENTORY DETAILS\033[0m ---")

    def _pct(val):
        try:
            return float(str(val).replace("%", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return 0.0

    # Canonical build-up order per section (mirrors CURRENT DETAILS), not
    # whatever order the user happened to edit fields in -- so every table
    # in this operation always reads top-to-bottom the same way regardless
    # of session path. Shared by the recalculation preview and the final
    # PROPOSED CHANGES table, so the same field is never named two
    # different ways across the two.
    _FIELD_LABELS = {
        "Supplier": "Supplier:",
        "Source Sheet + Tab": "Source Sheet + Tab:",
        "Date Acquired": "Date Acquired:",
        "Base Price + GST Tax (INR)": "Base Price + GST (INR):",
        "Shipping Cost (INR)": "Shipping (INR):",
        "Design Detailing Cost (INR)": "Detailing Cost (INR):",
        "Total Cost (INR)": "Total Cost (INR):",
        "Total Cost (USD)": "Total Cost (USD):",
        "Selling Price (INR) - Derived": "Selling Price (INR) - Derived:",
        "Markup %": "Markup %:",
        "Gross Profit (USD)": "Gross Profit (USD):",
        "(Profit) Margin %": "Margin %:",
    }
    _COST_FIELD_ORDER = ["Base Price + GST Tax (INR)", "Shipping Cost (INR)", "Design Detailing Cost (INR)"]
    _COST_FIELD_META = {
        "Base Price + GST Tax (INR)": ("Base Price + GST Tax (INR)", False),
        "Shipping Cost (INR)": ("Shipping Cost (INR)", True),
        "Design Detailing Cost (INR)": ("Design Detailing Cost (INR)", True),
    }

    # Step 1: SKU lookup, restricted to Available/Reserved
    while True:
        sku = ask_text("SKU:", blank_message="SKU cannot be left blank. Please enter a valid SKU (e.g. LAH-KKVSV500).")
        row_index = find_row_index_by_sku(sku)
        if row_index is None:
            _warn(f"SKU '{sku}' was not found in the master sheet. Please check the SKU and try again.")
            continue

        row = get_row_by_sheet_index(row_index)
        current_status = row.get("Status", "").strip()

        if current_status in ("Sold", "Sold - Partial Payment"):
            _warn(f"SKU '{sku}' has already been sold. This operation doesn't handle sold units.")
            continue
        if current_status not in ("Available", "Reserved"):
            _warn(f"SKU '{sku}' has status '{current_status}', which isn't eligible for this operation.")
            continue

        markup_pct   = _pct(row.get("Markup %", 0))
        margin_pct   = _pct(row.get("(Profit) Margin %", 0))
        status_color = get_status_color(current_status)

        # No cost cell should ever be genuinely blank -- even "no shipping
        # cost" gets written as an explicit 0 at entry, never left empty --
        # so a blank cell here is always a data-integrity problem, not a
        # legitimate zero. _sheet_float() can't tell blank apart from a
        # real 0 once it's converted, so check the raw string first and
        # warn before any figure below is silently built on a fabricated
        # zero.
        _cost_cols = ["Base Price + GST Tax (INR)", "Shipping Cost (INR)", "Design Detailing Cost (INR)",
                      "Total Cost (INR)", "Total Cost (USD)"]
        _blank_cost_cols = [c for c in _cost_cols if not str(row.get(c, "")).strip()]
        if _blank_cost_cols:
            _label = _blank_cost_cols[0] if len(_blank_cost_cols) == 1 else f"{len(_blank_cost_cols)} cost fields"
            _warn(f"{_label} blank or unreadable on the sheet for this unit -- every figure below that "
                  f"depends on it is being shown as $0.00, which may not be correct. Verify directly on "
                  f"the sheet before relying on these numbers.")

        _print_boxed("CURRENT DETAILS", [
            ("UNIT", [
                f"  {'Weave Type:':<24}{row['Weave Type / Cluster']}",
                f"  {'Category Code:':<24}{row['Category Code']}",
                f"  {'Supplier:':<24}{row['Supplier']}",
                f"  {'Source Sheet + Tab:':<24}{row['Source Sheet + Tab']}",
                f"  {'Date Acquired:':<24}{row['Date Acquired']}",
            ]),
            ("COST", [
                f"  {'Base Price + GST (INR):':<24}{_sheet_float(row['Base Price + GST Tax (INR)']):,.2f}",
                f"  {'Shipping (INR):':<24}{_sheet_float(row['Shipping Cost (INR)']):,.2f}",
                f"  {'Detailing Cost (INR):':<24}{_sheet_float(row['Design Detailing Cost (INR)']):,.2f}",
                f"  {'Total Cost (INR):':<24}{_sheet_float(row['Total Cost (INR)']):,.2f}",
                f"  {'Total Cost (USD):':<24}${_sheet_float(row['Total Cost (USD)']):,.2f}",
            ]),
            ("PRICING", [
                f"  {'Selling Price (USD):':<24}${_sheet_float(row['Selling Price (USD)']):,.2f}",
                f"  {'Markup %:':<24}{get_markup_color(markup_pct)}{markup_pct:.1f}%\033[0m",
                f"  {'Gross Profit (USD):':<24}${_sheet_float(row['Gross Profit (USD)']):,.2f}",
                f"  {'Margin %:':<24}{get_margin_color(margin_pct)}{margin_pct:.1f}%\033[0m",
            ]),
            ("STATUS", [f"  {'Status:':<24}{status_color}{current_status}\033[0m"]),
        ])

        if ask_yes_no("Is this the correct unit?"):
            break
        print()

    print()

    # Working state threads through the whole session -- compounding edits
    # (e.g. Base Price then Shipping Cost in the same sitting) always
    # calculate from the most recently edited values, never stale originals
    # and never a fresh sheet re-read (which wouldn't see this session's
    # own not-yet-written edits either).
    working = {
        "Supplier": row["Supplier"],
        "Source Sheet + Tab": row["Source Sheet + Tab"],
        "Date Acquired": row["Date Acquired"],
        "Base Price + GST Tax (INR)": _sheet_float(row["Base Price + GST Tax (INR)"]),
        "Shipping Cost (INR)": _sheet_float(row["Shipping Cost (INR)"]),
        "Design Detailing Cost (INR)": _sheet_float(row["Design Detailing Cost (INR)"]),
        "Total Cost (INR)": _sheet_float(row["Total Cost (INR)"]),
        "Total Cost (USD)": _sheet_float(row["Total Cost (USD)"]),
        "Selling Price (USD)": _sheet_float(row["Selling Price (USD)"]),
        "Selling Price (INR) - Derived": _sheet_float(row["Selling Price (INR) - Derived"]),
        "Markup %": markup_pct,
        "Gross Profit (USD)": _sheet_float(row["Gross Profit (USD)"]),
        "Margin %": margin_pct,
    }
    reserved_date = None
    if current_status == "Reserved":
        _rd = row.get("Reserved Date", "").strip()
        if _rd:
            try:
                reserved_date = datetime.strptime(_rd, "%m-%d-%Y").date()
            except ValueError:
                reserved_date = None

    # ECB rate is fetched lazily -- only the first time a cost-affecting
    # field is actually edited -- and cached for reuse by any further cost
    # edits in the same sitting, unless Date Acquired itself gets edited,
    # which replaces the cached rate with a freshly-fetched one.
    ecb_cache = {"rate": None, "date_str": None}

    changes_log = []        # human-readable diff lines for the automatic [CORRECTION] note
    manual_note_lines = []  # separate free-text lines from an explicit Inventory Notes edit
    field_changed = {}      # sheet column name -> new value, for the final write

    def _prune_settled_fields():
        """Drop any field_changed/changes_log entry that has settled back
        to matching the true original sheet value -- covers a field
        touched more than once in the same session (e.g. Supplier changed
        then changed back, or a cost component adjusted across two
        separate rounds) netting to no real change. Without this, the
        review table would show 'X -> X' for a field that isn't actually
        changing, since the old column always reads the true original
        while the new column reflects only the most recent edit."""
        _pct_fields = {"Markup %", "(Profit) Margin %"}
        for _key in list(field_changed.keys()):
            _new = field_changed[_key]
            _orig_raw = row.get(_key, "")
            if _key in _pct_fields:
                _matches = round(_new * 100, 2) == round(_pct(_orig_raw), 2)
            elif isinstance(_new, str):
                _matches = _new == _orig_raw
            else:
                _matches = round(float(_new), 2) == round(_sheet_float(_orig_raw), 2)
            if _matches:
                del field_changed[_key]
                changes_log[:] = [c for c in changes_log if not c.startswith(f"{_key} changed from")]

    def _recalc_and_confirm(component_changes, new_total_cost_inr, ecb_rate):
        """Shared recalculation + check_pricing_warnings() flow for a batch
        of one or more cost-affecting edits (any of Base Price, Shipping,
        Design Detailing Cost). Selling Price (USD) never changes here --
        that's Reprice's job -- so Gross Profit/Markup/Margin are
        recomputed against the *existing* selling price, not a newly
        entered one.
        component_changes: {field_name: new_val} for every cost field
        touched in this batch -- each gets its own before/after row ahead
        of the Total Cost rows, so a multi-field batch shows exactly what
        changed, not just the resulting total.
        Returns ("keep", result_dict) | ("reenter", None) | ("discard", None).
        """
        new_total_cost_usd = round(new_total_cost_inr / ecb_rate, 2)
        selling_price_usd = working["Selling Price (USD)"]
        new_gross_profit = round(selling_price_usd - new_total_cost_usd, 2)
        new_markup = round((new_gross_profit / new_total_cost_usd) * 100, 2) if new_total_cost_usd else 0
        new_margin = round((new_gross_profit / selling_price_usd) * 100, 2) if selling_price_usd else 0

        _wm_c, _nm_c   = get_markup_color(working['Markup %']), get_markup_color(new_markup)
        _wmg_c, _nmg_c = get_margin_color(working['Margin %']), get_margin_color(new_margin)
        _wgp_c = "\033[92m" if working['Gross Profit (USD)'] >= 0 else "\033[91m"
        _ngp_c = "\033[92m" if new_gross_profit >= 0 else "\033[91m"
        _wgp_sign, _ngp_sign = signed(working['Gross Profit (USD)']), signed(new_gross_profit)

        _entries = []
        for _cf in _COST_FIELD_ORDER:
            if _cf in component_changes:
                _entries.append(("COST", _FIELD_LABELS[_cf], f"{working[_cf]:,.2f}", f"{component_changes[_cf]:,.2f}"))
            else:
                # Shown even though it isn't changing, so Total Cost below
                # is auditable from this table alone -- otherwise the two
                # changed components plus the total wouldn't add up to
                # anything a reader could verify without knowing this
                # field's value from elsewhere.
                _entries.append(("COST", _FIELD_LABELS[_cf], f"{working[_cf]:,.2f}", None))
        _entries += [
            ("COST", _FIELD_LABELS["Total Cost (INR)"], f"{working['Total Cost (INR)']:,.2f}", f"{new_total_cost_inr:,.2f}"),
            ("COST", _FIELD_LABELS["Total Cost (USD)"], f"${working['Total Cost (USD)']:,.2f}", f"${new_total_cost_usd:,.2f}"),
            ("PRICING", "Selling Price (USD):", f"${selling_price_usd:,.2f}", None),
            ("PRICING", _FIELD_LABELS["Markup %"], f"{_wm_c}{working['Markup %']:.1f}%\033[0m", f"{_nm_c}{new_markup:.1f}%\033[0m"),
            ("PRICING", _FIELD_LABELS["Gross Profit (USD)"], f"{_wgp_c}{_wgp_sign}${abs(working['Gross Profit (USD)']):,.2f}\033[0m", f"{_ngp_c}{_ngp_sign}${abs(new_gross_profit):,.2f}\033[0m"),
            ("PRICING", _FIELD_LABELS["(Profit) Margin %"], f"{_wmg_c}{working['Margin %']:.1f}%\033[0m", f"{_nmg_c}{new_margin:.1f}%\033[0m"),
        ]
        _label_w = max(len(l) for _, l, _, _ in _entries) + 2
        _old_w   = max(_visible_len(o) for _, _, o, _ in _entries)
        _by_section = {}
        for sec, label, old, new in _entries:
            row = (f"  {label:<{_label_w}}{_pad_visible(old, _old_w)}  →  \033[2m(unchanged)\033[0m" if new is None
                   else f"  {label:<{_label_w}}{_pad_visible(old, _old_w)}  →  {new}")
            _by_section.setdefault(sec, []).append(row)

        _print_boxed("RECALCULATION PREVIEW", [
            ("COST", _by_section.get("COST", [])),
            ("PRICING", _by_section.get("PRICING", [])),
        ])

        proceed, warned = check_pricing_warnings(
            new_margin, new_markup, new_gross_profit,
            exit_label="Discard this edit",
            recalibrate_label="Re-enter this value",
        )
        if proceed is None:
            return "discard", None
        if proceed is False:
            return "reenter", None
        return "keep", {
            "total_cost_usd": new_total_cost_usd,
            "gross_profit": new_gross_profit,
            "markup": new_markup,
            "margin": new_margin,
        }

    def _apply_cost_results(component_changes, new_total_cost_inr, result):
        for field_name, new_val in component_changes.items():
            old_val = working[field_name]
            changes_log.append(f"{field_name} changed from {old_val:,.2f} to {new_val:,.2f}.")
            field_changed[field_name] = round(new_val, 2)
            working[field_name] = new_val
        working["Total Cost (INR)"] = new_total_cost_inr
        working["Total Cost (USD)"] = result["total_cost_usd"]
        working["Gross Profit (USD)"] = result["gross_profit"]
        working["Markup %"] = result["markup"]
        working["Margin %"] = result["margin"]
        field_changed["Total Cost (INR)"] = round(new_total_cost_inr, 2)
        field_changed["Total Cost (USD)"] = result["total_cost_usd"]
        field_changed["Gross Profit (USD)"] = result["gross_profit"]
        field_changed["Markup %"] = round(result["markup"] / 100, 6)
        field_changed["(Profit) Margin %"] = round(result["margin"] / 100, 6)

    def _edit_cost_field(field_name, prompt_label, allow_zero):
        _draft = {}  # persists across "Re-enter this value" retries within
                     # this attempt -- re-entering one field (because it
                     # triggered a pricing warning) must not silently
                     # discard values already entered for the other two
        while True:
            print(f"\n\033[2mCurrent value: {working[field_name]:,.2f}\033[0m")
            new_val = ask_number(f"New {prompt_label}:", allow_zero=allow_zero)
            if new_val == working[field_name]:
                _draft.pop(field_name, None)
                if not _draft:
                    print("\n\033[33mNo change.\033[0m")
                    return False
            else:
                _draft[field_name] = new_val

            # Base Price, Shipping, and Design Detailing Cost together make
            # up Total Cost -- editing just one leaves the picture
            # incomplete unless the other two are consciously reviewed
            # too, so offer to walk through them right here rather than
            # silently carrying forward whatever they already were.
            _other_fields = [f for f in _COST_FIELD_ORDER if f != field_name]
            _other_labels = " and ".join(_FIELD_LABELS[f].rstrip(":") for f in _other_fields)
            print(f"\n\033[33m⚠  OTHER COST COMPONENTS\033[0m")
            print(f"\033[2m{'—' * 50}\033[0m")
            print(f"{_other_labels} also make up Total Cost.")
            if ask_yes_no("Would you like to review or update those too before continuing?"):
                for _other_field in _other_fields:
                    _other_label, _other_allow_zero = _COST_FIELD_META[_other_field]
                    _oref = _draft.get(_other_field, working[_other_field])
                    print(f"\n\033[2mCurrent value: {_oref:,.2f}\033[0m")
                    _other_val = ask_number(f"New {_other_label}:", allow_zero=_other_allow_zero)
                    if _other_val == _oref:
                        # Matches what was just shown as "Current value" --
                        # genuinely nothing changed from this attempt.
                        print("\n\033[33mNo change.\033[0m")
                    elif _other_val == working[_other_field]:
                        # Differs from the draft but lands back on the true
                        # original -- a real edit was made (e.g. reverting
                        # an earlier draft value), just no "No change."
                        # message, since something did happen. It drops out
                        # of the draft because the net effect is zero --
                        # there's nothing left to write for this field.
                        _draft.pop(_other_field, None)
                    else:
                        _draft[_other_field] = _other_val

            component_changes = dict(_draft)
            if not component_changes:
                print("\n\033[33mNo change.\033[0m")
                return False

            if ecb_cache["rate"] is None:
                while True:
                    ecb_cache["rate"], ecb_cache["date_str"] = fetch_ecb_rate(
                        working["Date Acquired"], total_cost_usd=working["Total Cost (USD)"]
                    )
                    if ecb_cache["rate"] is not None:
                        break
                    if ecb_cache["date_str"] != "abort":
                        # Declined to confirm the fetched rate -- not a
                        # failure, and retrying would just show the same
                        # rate again for the same date.
                        print("\nThis edit was not applied.")
                        return False
                    print("\nCould not fetch the exchange rate.")
                    if not ask_yes_no("Retry the lookup? (Values you've already entered are still saved.)"):
                        print("This edit was not applied.")
                        return False

            new_total_cost_inr = round(
                sum(component_changes.get(_cf, working[_cf]) for _cf in _COST_FIELD_ORDER),
                2,
            )
            state, result = _recalc_and_confirm(component_changes, new_total_cost_inr, ecb_cache["rate"])
            if state == "reenter":
                continue
            if state == "discard":
                return False
            _apply_cost_results(component_changes, new_total_cost_inr, result)
            return True

    def _edit_date_acquired():
        _last_date_str = None
        while True:
            print(f"\n\033[2mCurrent value: {working['Date Acquired']}\033[0m")
            kwargs = dict(
                not_future=True,
                future_msg="Acquisition cannot be recorded in the future. Please enter a current or past date.",
                allow_back=True,
            )
            if reserved_date is not None:
                kwargs["not_after"] = reserved_date
                kwargs["not_after_msg"] = (
                    f"Date Acquired cannot be after this unit's Reserved Date "
                    f"({reserved_date.strftime('%m-%d-%Y')})."
                )
            new_date = ask_date("New Date Acquired", **kwargs)
            if new_date is None:
                print("\n\033[33mNo change.\033[0m")
                return False
            new_date_str = new_date.strftime("%m-%d-%Y")
            if new_date_str == working["Date Acquired"]:
                print("\n\033[33mNo change.\033[0m")
                return False

            if new_date_str == _last_date_str:
                # Same value as the immediately-previous "Re-enter this
                # value" attempt -- nothing to recompute, so skip the
                # ECB fetch and its own confirm prompt entirely rather
                # than re-running an identical lookup, and fall straight
                # through to re-showing the same (unchanged) preview below.
                print("\n\033[33mNo change.\033[0m")
            else:
                while True:
                    new_rate, new_rate_date_str = fetch_ecb_rate(new_date_str)
                    if new_rate is not None:
                        break
                    if new_rate_date_str != "abort":
                        # Declined to confirm the fetched rate -- not a
                        # failure, and retrying would just show the same
                        # rate again for the same date.
                        print("\nThis edit was not applied.")
                        return False
                    print("\nCould not fetch the exchange rate for that date.")
                    if not ask_yes_no("Retry the lookup?"):
                        print("This edit was not applied.")
                        return False

                new_total_cost_usd = round(working["Total Cost (INR)"] / new_rate, 2)
                new_selling_price_inr = round(working["Selling Price (USD)"] * new_rate, 2)
                selling_price_usd = working["Selling Price (USD)"]
                new_gross_profit = round(selling_price_usd - new_total_cost_usd, 2)
                new_markup = round((new_gross_profit / new_total_cost_usd) * 100, 2) if new_total_cost_usd else 0
                new_margin = round((new_gross_profit / selling_price_usd) * 100, 2) if selling_price_usd else 0
                _last_date_str = new_date_str

            _wm_c, _nm_c   = get_markup_color(working['Markup %']), get_markup_color(new_markup)
            _wmg_c, _nmg_c = get_margin_color(working['Margin %']), get_margin_color(new_margin)
            _wgp_c = "\033[92m" if working['Gross Profit (USD)'] >= 0 else "\033[91m"
            _ngp_c = "\033[92m" if new_gross_profit >= 0 else "\033[91m"
            _wgp_sign, _ngp_sign = signed(working['Gross Profit (USD)']), signed(new_gross_profit)

            _entries = [
                ("UNIT", _FIELD_LABELS["Date Acquired"], working['Date Acquired'], new_date_str),
                ("COST", _FIELD_LABELS["Total Cost (USD)"], f"${working['Total Cost (USD)']:,.2f}", f"${new_total_cost_usd:,.2f}"),
                ("PRICING", "Selling Price (USD):", f"${selling_price_usd:,.2f}", None),
                ("PRICING", _FIELD_LABELS["Selling Price (INR) - Derived"], f"{working['Selling Price (INR) - Derived']:,.2f}", f"{new_selling_price_inr:,.2f}"),
                ("PRICING", _FIELD_LABELS["Markup %"], f"{_wm_c}{working['Markup %']:.1f}%\033[0m", f"{_nm_c}{new_markup:.1f}%\033[0m"),
                ("PRICING", _FIELD_LABELS["Gross Profit (USD)"], f"{_wgp_c}{_wgp_sign}${abs(working['Gross Profit (USD)']):,.2f}\033[0m", f"{_ngp_c}{_ngp_sign}${abs(new_gross_profit):,.2f}\033[0m"),
                ("PRICING", _FIELD_LABELS["(Profit) Margin %"], f"{_wmg_c}{working['Margin %']:.1f}%\033[0m", f"{_nmg_c}{new_margin:.1f}%\033[0m"),
            ]
            _ecb_label = "ECB Rate:"
            _label_w = max([len(l) for _, l, _, _ in _entries] + [len(_ecb_label)]) + 2
            _old_w   = max(_visible_len(o) for _, _, o, _ in _entries)
            _by_section = {}
            for sec, label, old, new in _entries:
                row = (f"  {label:<{_label_w}}{_pad_visible(old, _old_w)}  →  \033[2m(unchanged)\033[0m" if new is None
                       else f"  {label:<{_label_w}}{_pad_visible(old, _old_w)}  →  {new}")
                _by_section.setdefault(sec, []).append(row)
            _ecb_date_suffix = f" (as of {new_rate_date_str})" if new_rate_date_str != new_date_str else ""
            _by_section.setdefault("UNIT", []).append(
                f"  {_ecb_label:<{_label_w}}1 USD = {new_rate:,.2f} INR{_ecb_date_suffix}"
            )

            _print_boxed("RECALCULATION PREVIEW", [
                ("UNIT", _by_section.get("UNIT", [])),
                ("COST", _by_section.get("COST", [])),
                ("PRICING", _by_section.get("PRICING", [])),
            ])

            proceed, warned = check_pricing_warnings(
                new_margin, new_markup, new_gross_profit,
                exit_label="Discard this edit",
                recalibrate_label="Re-enter this value",
            )
            if proceed is False:
                continue
            if proceed is None:
                return False

            old_date_str = working["Date Acquired"]
            changes_log.append(f"Date Acquired changed from {old_date_str} to {new_date_str}.")
            field_changed["Date Acquired"] = new_date_str
            field_changed["Total Cost (USD)"] = new_total_cost_usd
            field_changed["Selling Price (INR) - Derived"] = new_selling_price_inr
            field_changed["Gross Profit (USD)"] = new_gross_profit
            field_changed["Markup %"] = round(new_markup / 100, 6)
            field_changed["(Profit) Margin %"] = round(new_margin / 100, 6)
            working["Date Acquired"] = new_date_str
            working["Total Cost (USD)"] = new_total_cost_usd
            working["Selling Price (INR) - Derived"] = new_selling_price_inr
            working["Gross Profit (USD)"] = new_gross_profit
            working["Markup %"] = new_markup
            working["Margin %"] = new_margin
            ecb_cache["rate"], ecb_cache["date_str"] = new_rate, new_rate_date_str
            return True

    def _pick_and_edit_field(has_changes, cost_fields_closed):
        """Returns True (real change made), False (explicit stop -- Cancel
        before anything changed, Done once something has, or Ctrl+C --
        caller should stop looping), or None (the attempt produced no
        change -- caller should show the field picker again, not exit).
        The escape choice reads 'Cancel' when there's nothing to lose yet,
        or 'Done -- review changes' once there is, so it never claims to
        discard changes it's actually about to show you.
        cost_fields_closed: once a cost-field batch has been proceeded
        with, the three cost components drop out of this menu for the
        rest of the attempt -- the user just explicitly closed out the
        cost picture, so re-offering them immediately would suggest that
        decision wasn't final."""
        _stop_choice = "Done — review changes" if has_changes else "Cancel — no changes"
        # Ordered to match the UNIT -> COST build-up used by CURRENT DETAILS
        # and PROPOSED CHANGES (Supplier, Source Sheet + Tab, Date Acquired,
        # then cost components), not the order fields happen to be listed
        # in code -- Inventory Notes has no canonical section of its own,
        # so it sits with the other UNIT-adjacent fields.
        _choices = ["Supplier", "Source Sheet + Tab", "Date Acquired", "Inventory Notes"]
        if not cost_fields_closed:
            _choices += ["Base Price + GST Tax (INR)", "Shipping Cost (INR)", "Design Detailing Cost (INR)"]
        _choices += [questionary.Separator(" "), _back_choice(_stop_choice)]
        field = questionary.select(
            "Which field would you like to update?",
            choices=_choices,
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()

        if field is None or field == _stop_choice:
            return False

        if field == "Supplier":
            print(f"\n\033[2mCurrent value: {working['Supplier']}\033[0m")
            new_val = select_or_add_supplier()
            if new_val == working["Supplier"]:
                print("\n\033[33mNo change.\033[0m")
                return None
            changes_log.append(f"Supplier changed from {working['Supplier']} to {new_val}.")
            field_changed["Supplier"] = new_val
            working["Supplier"] = new_val
            return True

        if field == "Source Sheet + Tab":
            print(f"\n\033[2mCurrent value: {working['Source Sheet + Tab']}\033[0m")
            new_val = ask_text("New Source Sheet + Tab:", blank_message="This field cannot be left blank. Please enter a sheet reference.")
            if new_val == working["Source Sheet + Tab"]:
                print("\n\033[33mNo change.\033[0m")
                return None
            changes_log.append(f"Source Sheet + Tab changed from {working['Source Sheet + Tab']} to {new_val}.")
            field_changed["Source Sheet + Tab"] = new_val
            working["Source Sheet + Tab"] = new_val
            return True

        if field == "Inventory Notes":
            new_note = ask_text("Additional note (press Enter to skip):", required=False)
            if not new_note:
                print("\n\033[33mNo change.\033[0m")
                return None
            manual_note_lines.append(new_note)
            return True

        if field == "Base Price + GST Tax (INR)":
            return True if _edit_cost_field("Base Price + GST Tax (INR)", "Base Price + GST Tax (INR)", allow_zero=False) else None

        if field == "Shipping Cost (INR)":
            return True if _edit_cost_field("Shipping Cost (INR)", "Shipping Cost (INR)", allow_zero=True) else None

        if field == "Design Detailing Cost (INR)":
            return True if _edit_cost_field("Design Detailing Cost (INR)", "Design Detailing Cost (INR)", allow_zero=True) else None

        if field == "Date Acquired":
            return True if _edit_date_acquired() else None

        return None

    def _collect_edits():
        """Loop the field picker freely -- edit as many fields as needed --
        without re-showing the accumulated diff after each one. Stops only
        when the user explicitly picks Cancel/Done or Ctrl+C's out. Once a
        cost-field batch is proceeded with, cost fields drop out of the
        menu and the user is asked directly whether they need to change
        anything else, rather than silently landing back on the same
        field picker they just finished with."""
        _cost_fields_closed = False
        _cost_state_keys = _COST_FIELD_ORDER + ["Total Cost (INR)", "Total Cost (USD)"]
        while True:
            _before_cost_state = {k: field_changed.get(k) for k in _cost_state_keys}
            _result = _pick_and_edit_field(
                has_changes=bool(changes_log or manual_note_lines),
                cost_fields_closed=_cost_fields_closed,
            )
            if _result is False:
                _prune_settled_fields()
                return
            if _result is None:
                # No-op attempt -- show the field picker again, don't exit.
                # Still pruned: a "no-op" here can also be a silent settle-
                # back-to-original (e.g. a cost field reverted via a
                # separate visit), which needs the same cleanup as a normal
                # successful edit or has_changes would keep reflecting a
                # change that no longer exists.
                _prune_settled_fields()
                print()
                continue
            # Compares values, not just which keys are present -- a second,
            # separate edit to a cost field already touched in an earlier
            # round overwrites the same keys rather than adding new ones,
            # so a plain "were any keys added" check would silently miss it
            # and the gate would never re-fire.
            _after_cost_state = {k: field_changed.get(k) for k in _cost_state_keys}
            if not _cost_fields_closed and _before_cost_state != _after_cost_state:
                _cost_fields_closed = True
                if not ask_yes_no("Do you need to change any more details?"):
                    _prune_settled_fields()
                    return
            _prune_settled_fields()
            print()

    _collect_edits()

    # PROPOSED CHANGES review + confirm loop -- shown once per review pass,
    # not after every individual field edit. The "anything to show" check
    # is re-run every time this loop is entered, not just once before it --
    # "Edit more fields" re-invokes _collect_edits() and can legitimately
    # net back to zero (e.g. a field changed then reverted to its true
    # original), which would otherwise render an empty PROPOSED CHANGES box
    # and still offer to "Confirm and apply changes" on nothing.
    while True:
        _prune_settled_fields()
        if not changes_log and not manual_note_lines:
            print("\n\033[33mNo changes made. Returning to Main Menu.\033[0m")
            return
        _SECTION_ORDER = [
            ("UNIT", ["Supplier", "Source Sheet + Tab", "Date Acquired"]),
            ("COST", ["Base Price + GST Tax (INR)", "Shipping Cost (INR)", "Design Detailing Cost (INR)",
                      "Total Cost (INR)", "Total Cost (USD)"]),
            ("PRICING", ["Selling Price (INR) - Derived", "Markup %", "Gross Profit (USD)", "(Profit) Margin %"]),
        ]

        _cost_touched = any(cf in field_changed for cf in _COST_FIELD_ORDER) or \
                        "Total Cost (INR)" in field_changed or "Total Cost (USD)" in field_changed

        _entries = []
        for section_name, order in _SECTION_ORDER:
            for col_name in order:
                if col_name not in field_changed:
                    # A cost component that didn't change is still shown
                    # whenever another one did, so Total Cost below stays
                    # auditable from this table alone -- otherwise the
                    # visible components plus the total wouldn't add up to
                    # anything a reader could verify without knowing this
                    # field's value from elsewhere.
                    if col_name in _COST_FIELD_ORDER and _cost_touched:
                        old_raw = row.get(col_name, "")
                        _entries.append((section_name, col_name, f"{_sheet_float(old_raw):,.2f}", None))
                    continue
                old_raw = row.get(col_name, "")
                new_val = field_changed[col_name]
                if col_name in ("Markup %", "(Profit) Margin %"):
                    old_pct = _pct(old_raw)
                    new_pct = new_val * 100
                    _color = get_markup_color if col_name == "Markup %" else get_margin_color
                    old_disp = f"{_color(old_pct)}{old_pct:.1f}%\033[0m"
                    new_disp = f"{_color(new_pct)}{new_pct:.1f}%\033[0m"
                elif col_name == "Gross Profit (USD)":
                    old_f = _sheet_float(old_raw)
                    _old_c = "\033[92m" if old_f >= 0 else "\033[91m"
                    _new_c = "\033[92m" if new_val >= 0 else "\033[91m"
                    old_disp = f"{_old_c}{signed(old_f)}${abs(old_f):,.2f}\033[0m"
                    new_disp = f"{_new_c}{signed(new_val)}${abs(new_val):,.2f}\033[0m"
                elif col_name == "Total Cost (USD)":
                    old_disp = f"${_sheet_float(old_raw):,.2f}"
                    new_disp = f"${new_val:,.2f}"
                elif col_name in ("Total Cost (INR)", "Selling Price (INR) - Derived",
                                   "Base Price + GST Tax (INR)", "Shipping Cost (INR)", "Design Detailing Cost (INR)"):
                    old_disp = f"{_sheet_float(old_raw):,.2f}"
                    new_disp = f"{new_val:,.2f}"
                else:
                    old_disp = old_raw or "(not set)"
                    new_disp = new_val
                _entries.append((section_name, col_name, old_disp, new_disp))

        # Column widths are measured from what's actually changing this
        # session, not hardcoded -- so a table with only short fields stays
        # tight, and one with a long label (e.g. "Selling Price (INR) -
        # Derived:") widens the whole table rather than overflowing it.
        if _entries:
            label_width = max(len(_FIELD_LABELS[c]) for _, c, _, _ in _entries) + 2
            old_width   = max(_visible_len(od) for _, _, od, _ in _entries)
        _sections = []
        for section_name, _order in _SECTION_ORDER:
            rows = [
                (f"  {_FIELD_LABELS[c]:<{label_width}}{_pad_visible(od, old_width)}  →  \033[2m(unchanged)\033[0m"
                 if nd is None else
                 f"  {_FIELD_LABELS[c]:<{label_width}}{_pad_visible(od, old_width)}  →  {nd}")
                for sn, c, od, nd in _entries if sn == section_name
            ]
            if rows:
                _sections.append((section_name, rows))
        if manual_note_lines:
            _sections.append(("NOTES", [f"  {note}" for note in manual_note_lines]))

        _print_boxed(f"PROPOSED CHANGES — {sku}", _sections)
        print()

        action = questionary.select(
            "Apply these changes?",
            choices=[
                "Confirm and apply changes",
                "Edit more fields",
                questionary.Separator(" "),
                _back_choice("Discard changes & continue"),
            ],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()

        if action == "Confirm and apply changes":
            if not _status_unchanged(row_index, current_status):
                _warn(f"This unit's status has changed since you started (no longer '{current_status}'). "
                      f"Someone else may have just updated it — nothing was written. Please re-check the SKU.")
                return

            _unchanged, _conflict_col = _row_fields_unchanged(row_index, row, field_changed.keys())
            if not _unchanged:
                _warn(f"This unit's '{_conflict_col}' has changed since you started editing — "
                      f"someone else may have updated it. Nothing was written. Please re-check the SKU.")
                return

            today_str = date.today().strftime("%m-%d-%Y")
            correction_note = f"[{today_str} · CORRECTION] " + " ".join(changes_log)
            all_new_lines = [f"[{today_str} · NOTE] {n}" for n in manual_note_lines]
            if changes_log:
                all_new_lines.append(correction_note)
            field_changed["Inventory Notes"] = _fresh_notes_append(row_index, "Inventory Notes", all_new_lines)

            update_row(row_index, field_changed)
            print(f"\n\033[38;5;202m✓ {sku} updated successfully.\033[0m")
            return

        if action == "Edit more fields":
            print()
            _collect_edits()
            continue

        print("\nEdit cancelled. Existing details retained. Returning to Main Menu.")
        return


# =============================================================================
# PHASE 5 — MAIN MENU LOOP
# =============================================================================

def print_banner():
    print()
    print(f"\033[2m{'=' * 58}\033[0m")
    print("  LAKSHIRA HANDWOVEN WEAVES — INVENTORY MANAGEMENT SYSTEM")
    print(f"\033[2m{'=' * 58}\033[0m")
    if MODE == "test":
        print("  ⚠️  TEST MODE — no changes made to the live sheet")
    else:
        print("  ✅  LIVE MODE — changes written to the production sheet")
    print(f"\033[2m{'=' * 58}\033[0m")
    print()


def discount_simulator():
    """Read-only discount simulation tool. Never writes to the sheet."""

    def _sim(total_cost_usd, selling_price_usd, discount_pct):
        """Print the simulation results block for a given discount percentage."""
        original_markup_pct  = round((selling_price_usd - total_cost_usd) / total_cost_usd * 100, 2) if total_cost_usd else 0
        orig_gp_usd          = round(selling_price_usd - total_cost_usd, 2)
        orig_margin_pct      = round(orig_gp_usd / selling_price_usd * 100, 2) if selling_price_usd else 0
        discount_amount      = round(selling_price_usd * discount_pct / 100, 2)
        actual_price         = round(selling_price_usd - discount_amount, 2)
        gross_profit         = round(actual_price - total_cost_usd, 2)
        margin_pct           = round(gross_profit / actual_price * 100, 2) if actual_price else 0
        disc_markup_pct      = round((actual_price - total_cost_usd) / total_cost_usd * 100, 2) if total_cost_usd else 0

        gp_sign        = signed(gross_profit)
        mar_sign       = signed(margin_pct)
        orig_gp_sign   = signed(orig_gp_usd)
        orig_m_sign    = signed(orig_margin_pct)
        disc_mu_sign   = signed(disc_markup_pct)
        _orig_gpc      = "\033[92m" if orig_gp_usd >= 0 else "\033[91m"
        _gpc           = "\033[92m" if gross_profit >= 0 else "\033[91m"
        _orig_mc       = get_markup_color(original_markup_pct)
        _disc_mc       = get_markup_color(disc_markup_pct)

        if gross_profit >= 0:
            if orig_margin_pct >= 15 and margin_pct >= 15:
                _closing = "The unit remains profitable."
            elif orig_margin_pct >= 15 and margin_pct < 15:
                _closing = "The discount brings margin below the 15% threshold."
            else:
                _closing = "The unit remains profitable but falls further below the 15% threshold."
            note = (
                f"A {discount_pct:.1f}% discount reduces the selling price by "
                f"${discount_amount:,.2f}, from ${selling_price_usd:,.2f} to ${actual_price:,.2f}. "
                f"Margin drops from {orig_margin_pct:.1f}% to {margin_pct:.1f}%. "
                f"{_closing}"
            )
        else:
            note = (
                f"A {discount_pct:.1f}% discount reduces the selling price by "
                f"${discount_amount:,.2f}, from ${selling_price_usd:,.2f} to ${actual_price:,.2f}, "
                f"pushing the unit ${abs(gross_profit):,.2f} below cost. "
                f"Selling at this discount results in a loss."
            )

        _eq  = f"\033[2m{'=' * 50}\033[0m"
        _sec = f"\033[2m{'—' * 50}\033[0m"

        print("\n" + _eq)
        print("  [1;38;5;178mDISCOUNT SIMULATION[0m")
        print(_eq)
        print("\033[38;5;202mCOST\033[0m")
        print(_sec)
        print(f"  {'Total Cost:':<19}${total_cost_usd:,.2f}")
        print()
        print("\033[38;5;202mORIGINAL\033[0m")
        print(_sec)
        print(f"  {'Selling Price:':<19}${selling_price_usd:,.2f}")
        print(f"  {'Markup:':<19}{_orig_mc}{original_markup_pct:.1f}%\033[0m")
        print(f"  {'Margin:':<19}{get_margin_color(orig_margin_pct)}{orig_m_sign}{abs(orig_margin_pct):.1f}%\033[0m ({_orig_gpc}{orig_gp_sign}${abs(orig_gp_usd):,.2f}\033[0m)")
        print()
        print("\033[38;5;202mDISCOUNTED\033[0m")
        print(_sec)
        print(f"  {'Discount Applied:':<19}{discount_pct:.1f}% (-${discount_amount:,.2f})")
        print(f"  {'Sale Price:':<19}${actual_price:,.2f}")
        print(f"  {'Markup:':<19}{_disc_mc}{disc_mu_sign}{abs(disc_markup_pct):.1f}%\033[0m")
        print(f"  {'Margin:':<19}{get_margin_color(margin_pct)}{mar_sign}{abs(margin_pct):.1f}%\033[0m ({_gpc}{gp_sign}${abs(gross_profit):,.2f}\033[0m)")
        print()
        print("\033[38;5;202mNOTES\033[0m")
        print(_sec)
        for line in textwrap.wrap(note, width=46):
            print(f"  {line}")
        print(_eq)
        if gross_profit < 0:
            print()
            print(f"\033[91m⚠  BELOW COST\033[0m")
            print(f"\033[2m{'—' * 50}\033[0m")
            print("This unit is selling at a loss.")
        elif margin_pct < 15:
            print()
            print(f"{get_margin_color(margin_pct)}△ LOW MARGIN — Margin sits at {margin_pct:.1f}%, below the 15% threshold\033[0m")

    # ── Sub-menu loop ──────────────────────────────────────────────────────────
    print("\n--- \033[1;38;5;124mDISCOUNT SIMULATOR\033[0m ---")
    while True:
        print()
        mode = questionary.select(
            "Select an option:",
            choices=["Look up an existing unit", "Simulate with custom figures",
                     questionary.Separator(" "),
                     _back_choice("Return to Main Menu")],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if mode is None or mode == "Return to Main Menu":
            return

        # ── MODE 1: SKU LOOKUP ─────────────────────────────────────────────────
        if mode == "Look up an existing unit":
            while True:  # SKU re-entry loop
                sku = ask_text("SKU:", blank_message="SKU cannot be left blank. Please enter a valid SKU (e.g. LAH-KKVSV500).")
                row_index = find_row_index_by_sku(sku)
                if row_index is None:
                    _warn(f"No unit found with SKU '{sku}'. Please check the SKU and try again.")
                    continue

                row = get_row_by_sheet_index(row_index)
                status = row["Status"].strip()

                # Only Available and Reserved are live pre-sale states where a
                # discount decision is actually actionable. Unassigned units
                # have no real cost/price yet -- simulating a discount there
                # would just run the math against fabricated $0.00 figures.
                # Sold / Sold - Partial Payment are already closed transactions.
                if status not in ("Available", "Reserved"):
                    _warn(f"SKU '{sku}' has status '{status}'. Only Available or Reserved units can be simulated.")
                    continue

                selling_price_usd = _sheet_float(row["Selling Price (USD)"])
                total_cost_usd    = _sheet_float(row["Total Cost (USD)"])

                if not str(row.get("Total Cost (USD)", "")).strip():
                    _warn("Total Cost (USD) is blank or unreadable on the sheet for this unit -- every "
                          "figure below that depends on it is being shown as $0.00, which may not be "
                          "correct. Verify directly on the sheet before relying on these numbers.")

                _dii = _days_since(_safe_parse_date(row["Date Acquired"]))
                days_in_inventory = _dii if _dii is not None else "unknown"

                orig_markup  = round((selling_price_usd - total_cost_usd) / total_cost_usd * 100, 2) if total_cost_usd else 0
                gross_profit = round(selling_price_usd - total_cost_usd, 2)
                margin_pct   = round(gross_profit / selling_price_usd * 100, 2) if selling_price_usd else 0
                _gpc         = "\033[92m" if gross_profit >= 0 else "\033[91m"
                _gp_sign     = signed(gross_profit)
                _m_sign      = signed(margin_pct)
                _mc          = get_markup_color(orig_markup)
                _mmc         = get_margin_color(margin_pct)

                _sc = get_status_color(status)

                _eq  = f"\033[2m{'=' * 50}\033[0m"
                _sec = f"\033[2m{'—' * 50}\033[0m"

                print("\n" + _eq)
                print("  [1;38;5;178mUNIT SUMMARY[0m")
                print(_eq)
                print("\033[38;5;202mUNIT\033[0m")
                print(_sec)
                print(f"  {'SKU:':<20}{sku}")
                print(f"  {'Status:':<20}{_sc}{status}\033[0m")
                print(f"  {'Days in Inventory:':<20}{days_in_inventory}")
                print()
                print("\033[38;5;202mPRICING\033[0m")
                print(_sec)
                print(f"  {'Total Cost:':<20}${total_cost_usd:,.2f}")
                print(f"  {'Selling Price:':<20}${selling_price_usd:,.2f}")
                print(f"  {'Markup:':<20}{_mc}{orig_markup:.1f}%\033[0m")
                print(f"  {'Gross Profit:':<20}{_gpc}{_gp_sign}${abs(gross_profit):,.2f}\033[0m")
                print(f"  {'Margin:':<20}{_mmc}{_m_sign}{abs(margin_pct):.1f}%\033[0m")
                print(_eq)

                if gross_profit < 0:
                    print()
                    print(f"\033[91m⚠  BELOW COST\033[0m")
                    print(f"\033[2m{'—' * 50}\033[0m")
                    print("Current pricing is already at a loss, prior to any discount.")
                elif margin_pct < 15:
                    print()
                    print(f"{get_margin_color(margin_pct)}△ LOW MARGIN — Margin sits at {margin_pct:.1f}%, below the 15% threshold, prior to any discount\033[0m")

                if status == "Reserved":
                    print("\nNote: This unit is reserved for a customer — simulation only.")

                _run_sim = True
                if gross_profit < 0:
                    _run_sim = ask_yes_no("Simulate a discount anyway?")

                if _run_sim:
                    while True:  # discount loop
                        discount_pct = ask_percent("Discount to simulate:", allow_zero=False)
                        _sim(total_cost_usd, selling_price_usd, discount_pct)
                        if not ask_yes_no("Try a different discount?"):
                            break

                if not ask_yes_no("Look up a different unit?"):
                    break

        # ── MODE 2: HYPOTHETICAL UNIT ──────────────────────────────────────────
        else:
            total_cost_inr = ask_number("Total Cost (INR):", allow_zero=False)

            ecb_rate = None
            while ecb_rate is None:
                ref_date = ask_date("Exchange Rate Date", not_future=True)
                ref_date_str = ref_date.strftime("%m-%d-%Y")
                while True:
                    rate, status = fetch_ecb_rate(ref_date_str)
                    if rate is not None:
                        ecb_rate = rate
                        break
                    if status != "abort":
                        break
                    print("\nCould not fetch the exchange rate.")
                    if not ask_yes_no("Retry the lookup? (Your entered Total Cost is still saved.)"):
                        print("Returning to Main Menu.")
                        return

            total_cost_usd = round(total_cost_inr / ecb_rate, 2)
            _sec = f"\033[2m{'—' * 50}\033[0m"
            print(f"\n\033[38;5;202mCOST BASIS\033[0m")
            print(_sec)
            print(f"  {'Total Cost (INR):':<19}₹{total_cost_inr:,.0f}")
            print(f"  {'Total Cost (USD):':<19}${total_cost_usd:,.2f}")
            print(_sec)

            restart_pricing = True
            while restart_pricing:  # pricing loop
                restart_pricing = False
                print()
                path = questionary.select(
                    "How would you like to set the selling price?",
                    choices=[
                        "Enter a markup percentage",
                        "Enter a selling price (USD)",
                        questionary.Separator(" "),
                        _back_choice("Discard & exit"),
                    ],
                    qmark="",
                    instruction=" ",
                    style=_MENU_STYLE,
                ).unsafe_ask()
                if path == "Discard & exit":
                    break
                if "markup" in path.lower():
                    markup_val = ask_percent("Markup percentage:", allow_zero=False)
                    selling_price_usd = round(total_cost_usd * (1 + markup_val / 100), 2)
                else:
                    selling_price_usd = ask_number("Selling Price (USD):", allow_zero=False)

                base_gross    = round(selling_price_usd - total_cost_usd, 2)
                base_markup   = round(base_gross / total_cost_usd * 100, 2) if total_cost_usd else 0
                base_margin   = round(base_gross / selling_price_usd * 100, 2) if selling_price_usd else 0
                _base_gpc     = "\033[92m" if base_gross >= 0 else "\033[91m"
                _base_gp_sign = signed(base_gross)
                _base_m_sign  = signed(base_margin)
                _sec = f"\033[2m{'—' * 50}\033[0m"
                print(f"\n\033[38;5;202mBASE PRICING\033[0m")
                print(_sec)
                print(f"  {'Selling Price:':<16}${selling_price_usd:,.2f}")
                print(f"  {'Markup:':<16}{get_markup_color(base_markup)}{base_markup:.1f}%\033[0m")
                print(f"  {'Gross Profit:':<16}{_base_gpc}{_base_gp_sign}${abs(base_gross):,.2f}\033[0m")
                print(f"  {'Margin:':<16}{get_margin_color(base_margin)}{_base_m_sign}{abs(base_margin):.1f}%\033[0m")
                print(_sec)

                if base_gross < 0:
                    print()
                    print(f"\033[91m⚠  BELOW COST\033[0m")
                    print(f"\033[2m{'—' * 50}\033[0m")
                    print("Current pricing is already at a loss, prior to any discount.")
                elif base_margin < 15:
                    print()
                    print(f"{get_margin_color(base_margin)}△ LOW MARGIN — Margin sits at {base_margin:.1f}%, below the 15% threshold, prior to any discount\033[0m")

                _run_sim = True
                if base_gross < 0:
                    _run_sim = ask_yes_no("Simulate a discount anyway?")

                if _run_sim:
                    while True:  # discount loop
                        discount_pct = ask_percent("Discount to simulate:", allow_zero=False)
                        _sim(total_cost_usd, selling_price_usd, discount_pct)
                        if not ask_yes_no("Try a different discount?"):
                            break

                if ask_yes_no("Adjust selling price or markup?"):
                    restart_pricing = True

            print("\nReturning to Main Menu.")
            return


def record_outstanding_payment():
    """Record a payment against a Sold - Partial Payment unit."""
    print("\n--- \033[1;38;5;124mRECORD OUTSTANDING PAYMENT\033[0m ---")
    while True:  # outer loop: "Record another payment?"
        rows = get_all_rows()
        partial_rows = [r for r in rows if r.get("Status", "").strip() == "Sold - Partial Payment"]

        if not partial_rows:
            print("\nNo outstanding payments on record. Returning to Main Menu.")
            return

        today = date.today()

        def _days(r, _today=today):
            """Days outstanding, or None if Date Sold is blank/malformed/
            in the future -- never silently reads as 0 (fresh/low-urgency)
            for a balance whose real age is simply unknown."""
            return _days_since(_parse_date_sold(r), reference_date=_today)

        # Cached once per row so sorting and display don't each recompute
        # it, and so unknown-age rows can sort to the front (they need
        # investigation) instead of silently behaving like the freshest
        # balance on the list.
        for r in partial_rows:
            r["_days_cache"] = _days(r)

        def _sort_key(r):
            d = r["_days_cache"]
            is_known = d is not None
            return (is_known, -d if is_known else 0, r.get("SKU", "").strip())

        all_sorted = sorted(partial_rows, key=_sort_key)

        # Display all outstanding units
        def _days_color(d):
            if d is None:
                return "\033[95m"
            if d <= 30:
                return "\033[92m"
            elif d < 60:
                return "\033[93m"
            else:
                return "\033[91m"

        def _days_display(d):
            return "unknown" if d is None else str(d)

        total_outstanding = sum(_sheet_float(r.get("Amount Outstanding (USD)")) for r in all_sorted)
        _eq  = f"\033[2m{'=' * 70}\033[0m"
        _sep = f"\033[2m{'—' * 70}\033[0m"

        print("\n" + _eq)
        print("  [1;38;5;178mCURRENT OUTSTANDING PAYMENTS[0m")
        print(_eq)
        print(f"\033[38;5;202m  {'SKU':<14}{'Customer':<22}{'Outstanding':<16}Days Outstanding\033[0m")
        print(_sep)
        for r in all_sorted:
            sku   = r.get("SKU", "").strip()
            cust  = _clip(r.get("Customer Name", "").strip())
            amt   = _sheet_float(r.get("Amount Outstanding (USD)"))
            days  = r["_days_cache"]
            dc    = _days_color(days)
            print(f"  {sku:<14}{cust:<22}${amt:<15,.2f}{dc}{_days_display(days)}\033[0m")
        _footer = f"Units Outstanding: {len(all_sorted)} | Amount Outstanding: ${total_outstanding:,.2f}"
        print(_eq)
        print(_footer.center(70))
        print(_eq)

        # Entry menu
        selected_row = None
        while selected_row is None:
            print()
            nav = questionary.select(
                "How would you like to select a unit?",
                choices=["Enter by SKU", "Filter by customer",
                         questionary.Separator(" "),
                         _back_choice("Return to Main Menu")],
                qmark="",
                instruction=" ",
                style=_MENU_STYLE,
            ).unsafe_ask()

            if nav is None or nav == "Return to Main Menu":
                print("\nReturning to Main Menu.")
                return

            elif nav == "Enter by SKU":
                sku_list = sorted(r.get("SKU", "").strip() for r in all_sorted)
                while True:
                    raw = input("\nSearch SKU (or press Enter to see all): ").strip().upper()
                    matches = sorted([s for s in sku_list if raw in s.upper()]) if raw else sku_list
                    if not matches:
                        _warn(f"No outstanding units matching '{raw}'. Please try again.")
                        continue
                    if len(matches) == 1:
                        chosen_sku = matches[0]
                    else:
                        print()
                        chosen_sku = questionary.select(
                            "Select a unit:",
                            choices=matches + [
                                questionary.Separator(" "),
                                _back_choice("Change selection method"),
                            ],
                            qmark="",
                            instruction=" ",
                            style=_MENU_STYLE,
                        ).unsafe_ask()
                    if chosen_sku is None or chosen_sku == "Change selection method":
                        break  # back to nav menu
                    selected_row = next(r for r in all_sorted if r.get("SKU", "").strip() == chosen_sku)
                    break

            elif nav == "Filter by customer":
                # Grouped by identity (see _customer_identity_key), not by
                # Customer Name alone -- two different customers who share
                # a name get separate entries here as long as either has a
                # phone or email on file, same as the Customer Insights
                # picker. Location is shown alongside each name so those
                # entries are actually distinguishable in the list, not
                # just correctly separated behind the scenes.
                _FILTER_STYLE = Style.from_dict({"customer-location": "#777777"})
                seen_keys, all_customers = set(), []
                for r in partial_rows:
                    # Not pre-.strip()'d here -- gspread's get_all_records()
                    # can return a numeric-only cell (e.g. country code "1")
                    # as an int; _customer_identity_key() coerces+strips
                    # every field itself.
                    name = str(r.get("Customer Name", "") or "").strip()
                    if not name:
                        continue
                    key = _customer_identity_key(
                        name, r.get("Customer Country Code", ""),
                        r.get("Customer Phone", ""), r.get("Customer Email", ""),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    location = ", ".join(p for p in [str(r.get("Customer City", "") or "").strip(),
                                                      str(r.get("Customer Country", "") or "").strip()] if p)
                    all_customers.append({"key": key, "name": name, "location": location})

                selected_customer = None
                want_nav_back = False
                while selected_customer is None:
                    query = input("\nCustomer search (or press Enter to see all): ").strip()
                    if _looks_like_phone_query(query):
                        _warn("Customer names cannot be numbers. Please enter a name or press Enter to see all.")
                        continue
                    filtered = sorted(
                        [c for c in all_customers if query.lower() in c["name"].lower()],
                        key=lambda c: c["name"].lower(),
                    )
                    if not filtered:
                        _warn("No customers with outstanding payments matching that search.")
                        continue
                    if len(filtered) == 1:
                        selected_customer = filtered[0]
                    else:
                        print()
                        col_width = max(len(c["name"]) for c in filtered) + 4
                        choices = []
                        for c in filtered:
                            title = ([("", f"{c['name']:<{col_width}}"), ("class:customer-location", c["location"])]
                                      if c["location"] else c["name"])
                            choices.append(questionary.Choice(title=title, value=c))
                        selected_customer = questionary.select(
                            "Select a customer:",
                            choices=choices + [
                                questionary.Separator(" "),
                                _back_choice("Change selection method"),
                            ],
                            qmark="",
                            instruction=" ",
                            style=_FILTER_STYLE,
                        ).unsafe_ask()
                        if selected_customer is None or selected_customer == "Change selection method":
                            want_nav_back = True
                            break

                if want_nav_back:
                    continue  # back to nav menu

                customer_units = [
                    r for r in partial_rows
                    if _customer_identity_key(
                        r.get("Customer Name", ""), r.get("Customer Country Code", ""),
                        r.get("Customer Phone", ""), r.get("Customer Email", ""),
                    ) == selected_customer["key"]
                ]
                sku_options = sorted(r.get("SKU", "").strip() for r in customer_units)
                if len(sku_options) == 1:
                    chosen_sku = sku_options[0]
                else:
                    print()
                    chosen_sku = questionary.select(
                        f"Outstanding units for {selected_customer['name']}:",
                        choices=sku_options + [
                            questionary.Separator(" "),
                            _back_choice("Change selection method"),
                        ],
                        qmark="",
                        instruction=" ",
                        style=_MENU_STYLE,
                    ).unsafe_ask()
                if chosen_sku is None or chosen_sku == "Change selection method":
                    continue
                selected_row = next(r for r in customer_units if r.get("SKU", "").strip() == chosen_sku)

        # Step 3 — Payment history summary
        sku              = selected_row.get("SKU", "").strip()
        customer_name    = selected_row.get("Customer Name", "").strip()
        date_sold_str    = selected_row.get("Date Sold", "").strip()
        selling_price    = _sheet_float(selected_row.get("Selling Price (USD)"))
        actual_price     = _sheet_float(selected_row.get("Actual Selling Price (USD)"))
        discount_pct     = _sheet_float(str(selected_row.get("Discount %") or "").replace("%", "").strip())
        amount_received  = _sheet_float(selected_row.get("Amount Received (USD)"))
        amount_outstanding = _sheet_float(selected_row.get("Amount Outstanding (USD)"))
        txn_notes        = selected_row.get("Transaction Notes", "").strip()
        sales_channel    = selected_row.get("Sales Channel", "").strip()
        has_discount     = abs(actual_price - selling_price) >= 0.01 and discount_pct > 0

        days = _days_since(_safe_parse_date(date_sold_str), reference_date=today)

        if days is None:
            days_str = "\033[95munknown\033[0m"
        else:
            if days >= 60:
                days_color = "\033[91m"
            elif days >= 31:
                days_color = "\033[93m"
            else:
                days_color = "\033[92m"
            days_str = f"{days_color}{days} days\033[0m"

        _payment_rows = []
        if has_discount:
            _payment_rows.append(f"  {'Listed Price:':<22}${selling_price:,.2f}")
            _payment_rows.append(f"  {'Discount:':<22}\033[33m{discount_pct:.1f}%\033[0m")
            _payment_rows.append(f"  {'Selling Price:':<22}${actual_price:,.2f}")
        else:
            _payment_rows.append(f"  {'Selling Price:':<22}${selling_price:,.2f}")
        _payment_rows.append(f"  {'Amount Received:':<22}\033[92m${amount_received:,.2f}\033[0m")
        _payment_rows.append(f"  {'Amount Outstanding:':<22}\033[91m${amount_outstanding:,.2f}\033[0m")

        _notes_rows = []
        if txn_notes:
            for entry in txn_notes.split("\n"):
                wrapped = textwrap.fill(entry.strip(), width=46, subsequent_indent="  ")
                for wline in wrapped.split("\n"):
                    _notes_rows.append(f"  {wline}")

        _print_boxed("TRANSACTION SUMMARY", [
            ("TRANSACTION", [
                f"  {'SKU:':<22}{sku}",
                f"  {'Customer:':<22}{customer_name}",
                f"  {'Sales Channel:':<22}{sales_channel}",
                f"  {'Date Sold:':<22}{date_sold_str}",
                f"  {'Days Outstanding:':<22}{days_str}",
            ]),
            ("PAYMENT", _payment_rows),
            ("NOTES", _notes_rows),
        ])

        # Step 4 — Payment date
        payment_date_str = ask_date("Payment date", not_future=True).strftime("%m-%d-%Y")

        # Step 4a — Payment entry
        payment = None
        while payment is None:
            payment = ask_number("Payment received (USD):", allow_zero=False)
            if payment > round(amount_outstanding, 2):
                _warn(
                    f"This amount exceeds the outstanding balance of ${amount_outstanding:,.2f}. "
                    f"Please enter ${amount_outstanding:,.2f} or less."
                )
                payment = None

        # Step 4b — Payment method
        payment_method = ask_payment_method()

        # Step 5 — Determine outcome
        new_received = round(amount_received + payment, 2)

        if round(payment, 2) >= round(amount_outstanding, 2):
            new_outstanding = 0.0
            new_status = "Sold"
            note_append = f"[{payment_date_str} · PAYMENT] Final payment of ${payment:,.2f} received via {payment_method}. Fully settled."
        else:
            new_outstanding = round(amount_outstanding - payment, 2)
            new_status = "Sold - Partial Payment"
            note_append = (
                f"[{payment_date_str} · PAYMENT] Partial payment of ${payment:,.2f} received via {payment_method}. "
                f"${new_outstanding:,.2f} still outstanding."
            )

        # Step 6 — Confirmation summary
        _status_color      = get_status_color(new_status)
        _outstanding_color = "\033[92m" if new_outstanding == 0 else "\033[91m"

        _print_boxed("PAYMENT SUMMARY", [
            ("TRANSACTION", [
                f"  {'SKU:':<22}{sku}",
                f"  {'Customer:':<22}{customer_name}",
                f"  {'Payment Date:':<22}{payment_date_str}",
            ]),
            ("PAYMENT", [
                f"  {'Payment Received:':<22}\033[92m${payment:,.2f}\033[0m",
                f"  {'Payment Method:':<22}{payment_method}",
                f"  {'Amount Received:':<22}\033[92m${new_received:,.2f}\033[0m",
                f"  {'Amount Outstanding:':<22}{_outstanding_color}${new_outstanding:,.2f}\033[0m",
                f"  {'Status:':<22}{_status_color}{new_status}\033[0m",
            ]),
        ])

        confirmed = ask_yes_no("Confirm and write to sheet?")
        if not confirmed:
            print("\nPayment cancelled. Returning to Main Menu.")
            return

        # Step 7 — Write
        sheet_row = find_row_index_by_sku(sku)
        if sheet_row is None:
            print(f"Error: Could not locate {sku} in the sheet. No changes written.")
            return

        # Re-check right before writing: the balance/status snapshot was
        # captured back when this unit was selected, and the operator has
        # since entered a payment date, amount, and payment method -- long
        # enough for another session to have recorded a different payment,
        # or for this sale to have been cancelled, in the meantime. Same
        # guard already used for record_sale(), reprice_unit(), cancel_sale(),
        # manage_reservation(), and add_new_inventory()'s Unassigned-reuse
        # path -- this call site never had it.
        if not _status_unchanged(sheet_row, "Sold - Partial Payment"):
            _warn("This unit's status has changed since you started (no longer 'Sold - Partial Payment'). "
                  "Someone else may have just updated it — nothing was written. Please re-check the SKU.")
            return

        # Numeric-aware, not the generic string-based _row_fields_unchanged --
        # selected_row came from get_all_rows() (gspread's get_all_records(),
        # which type-infers numeric cells), so a fresh re-read via
        # get_row_by_sheet_index() (always plain strings) could otherwise
        # false-positive on "40" vs "40.0" formatting differences that don't
        # represent an actual change. This is also the one field in this
        # flow where "someone else changed it" must hard-abort rather than
        # merge -- two different payments computed from two different stale
        # baselines can't be safely combined after the fact.
        _fresh_outstanding = _sheet_float(get_row_by_sheet_index(sheet_row).get("Amount Outstanding (USD)"))
        if round(_fresh_outstanding, 2) != round(amount_outstanding, 2):
            _warn(f"This unit's outstanding balance has changed since you started (was "
                  f"${amount_outstanding:,.2f}, is now ${_fresh_outstanding:,.2f}) — someone else may "
                  f"have recorded a different payment. Nothing was written. Please re-check the SKU.")
            return

        updated_notes = _fresh_notes_append(sheet_row, "Transaction Notes", [note_append])

        update_row(sheet_row, {
            "Amount Received (USD)": "" if new_status == "Sold" else new_received,
            "Amount Outstanding (USD)": "" if new_status == "Sold" else new_outstanding,
            "Status": new_status,
            "Transaction Notes": updated_notes,
        })

        if new_outstanding == 0:
            print(f"\n\033[38;5;202m✓ {sku} — payment of ${payment:,.2f} recorded. Balance fully settled.\033[0m")
        else:
            print(f"\n{sku} — payment of ${payment:,.2f} recorded. ${new_outstanding:,.2f} still outstanding.")

        if not ask_yes_no("Record another payment?"):
            return
        # continue outer loop with fresh data on next iteration


def cancel_sale():
    print("\n--- \033[1;38;5;124mCANCEL A SALE\033[0m ---")

    # Pending refund sweep — surfaces unresolved refunds before cancellation flow
    _all_rows     = get_all_rows()
    _refund_sep   = f"\033[2m{'—' * 50}\033[0m"
    _pending      = []
    for _r in _all_rows:
        _inv = _r.get("Inventory Notes", "")
        for _line in _inv.split('\n'):
            if '· REFUND]' in _line and 'REFUND-CLEARED' not in _line:
                _amt   = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', _line)
                _cust  = _line.split('—', 1)[1].strip() if '—' in _line else "—"
                _pending.append((
                    _r.get("SKU", "").strip(),
                    _cust,
                    _amt.group(0) if _amt else "—",
                    _line.strip(),
                ))
                break

    if _pending:
        _ct = len(_pending)
        print("\n--- \033[1;38;5;124mPENDING REFUNDS\033[0m ---")
        _warn(f"{_ct} refund{'s' if _ct > 1 else ''} {'are' if _ct > 1 else 'is'} pending resolution:")
        print(_refund_sep)
        print(f"  \033[2m{'UNIT':<14}{'CUSTOMER':<20}AMOUNT PENDING\033[0m")
        print(_refund_sep)
        for _psku, _pcust, _pamt, _ in _pending:
            print(f"  {_psku:<14}{_clip(_pcust):<20}{_pamt}")
        print(_refund_sep)

        if ask_yes_no("Mark a refund as issued?"):
            if len(_pending) == 1:
                _match = _pending[0]
            else:
                _resolve_sku = ask_text("SKU to clear:").strip().upper()
                _match       = next((p for p in _pending if p[0].upper() == _resolve_sku), None)
                if _match is None:
                    _warn(f"No pending refund found for '{_resolve_sku}'. Continuing to cancellation.")
            if _match:
                _psku, _pcust, _pamt, _pline = _match
                if ask_yes_no(f"Refund of {_pamt} to {_pcust} — confirm as issued?"):
                    _ri = find_row_index_by_sku(_psku)
                    if _ri:
                        _rrow      = get_row_by_sheet_index(_ri)
                        _today     = date.today().strftime("%m-%d-%Y")

                        # Re-check right before writing: the pending-refund list
                        # was built from a full-sheet snapshot at the very start
                        # of this operation, so if someone else already cleared
                        # this same refund in the meantime, the flag won't be in
                        # this fresh read anymore -- skip instead of writing a
                        # redundant/duplicate REFUND-CLEARED note.
                        _old_inv = _rrow.get("Inventory Notes", "").strip()
                        if not any('· REFUND]' in l for l in _old_inv.split('\n')):
                            _warn(f"This refund for {_psku} appears to have already been cleared by someone else — nothing was written.")
                        else:
                            # Strip the · REFUND flag from Inventory Notes — no REFUND-CLEARED left behind
                            _new_inv = '\n'.join(
                                l for l in _old_inv.split('\n') if '· REFUND]' not in l
                            ).strip()

                            # Append REFUND-CLEARED to Transaction Notes to complete the arc
                            _old_txn   = _rrow.get("Transaction Notes", "").strip()
                            _cleared   = f"[{_today} · REFUND-CLEARED] {_pamt} refunded to {_pcust}."
                            _new_txn   = f"{_old_txn}\n{_cleared}".strip() if _old_txn else _cleared

                            update_row(_ri, {
                                "Inventory Notes":   _new_inv,
                                "Transaction Notes": _new_txn,
                            })
                            print(f"\n\033[38;5;202m✓ {_psku} — refund of {_pamt} marked as issued.\033[0m")

    # Steps 1–2: SKU lookup + confirmation (loops on wrong unit or invalid status)
    # Second header only when the refund sweep above actually printed
    # something to transition away from — otherwise this is the only
    # header in the operation, same as every other single-phase flow.
    if _pending:
        print("\n--- \033[1;38;5;124mSELECT SALE TO CANCEL\033[0m ---")
    while True:
        sku = ask_text("SKU:", blank_message="SKU cannot be left blank. Please enter a valid SKU (e.g. LAH-KKVSV500).")
        row_index = find_row_index_by_sku(sku)
        if row_index is None:
            _warn(f"SKU '{sku}' was not found in the master sheet. Please check the SKU and try again.")
            continue

        row = get_row_by_sheet_index(row_index)
        current_status = row.get("Status", "").strip()

        if current_status not in ("Sold", "Sold - Partial Payment"):
            _warn(f"SKU '{sku}' has status '{current_status}'. Only units recorded as Sold or Sold - Partial Payment can have a sale cancelled.")
            continue

        _status_color  = get_status_color(current_status)

        # Both feed the "restored" Gross Profit/Markup/Margin this operation
        # writes back when undoing the sale -- a blank one here means those
        # restored figures get written from a fabricated $0.00 instead of
        # the unit's real cost or price.
        _cost_cols = ["Total Cost (USD)", "Selling Price (USD)"]
        _blank_cost_cols = [c for c in _cost_cols if not str(row.get(c, "")).strip()]
        if _blank_cost_cols:
            _label = _blank_cost_cols[0] if len(_blank_cost_cols) == 1 else f"{len(_blank_cost_cols)} fields"
            _warn(f"{_label} blank or unreadable on the sheet for this unit -- every figure below that "
                  f"depends on it is being shown as $0.00, which may not be correct. Verify directly on "
                  f"the sheet before relying on these numbers.")

        selling_price  = _sheet_float(row.get("Selling Price (USD)", ""))
        actual_price   = _sheet_float(row.get("Actual Selling Price (USD)", ""))

        _sale_rows = [
            f"  {'Customer:':<20}{row.get('Customer Name', '')}",
            f"  {'Date Sold:':<20}{row.get('Date Sold', '')}",
            f"  {'Sales Channel:':<20}{row.get('Sales Channel', '')}",
        ]
        if selling_price:
            _sale_rows.append(f"  {'Selling Price:':<20}${selling_price:,.2f}")
        if actual_price and actual_price != selling_price:
            _sale_rows.append(f"  {'Unit Sold For:':<20}${actual_price:,.2f}")
        if current_status == "Sold - Partial Payment":
            amt_recv = _sheet_float(row.get("Amount Received (USD)", ""))
            amt_out  = _sheet_float(row.get("Amount Outstanding (USD)", ""))
            _sale_rows.append(f"  {'Amount Received:':<20}${amt_recv:,.2f}")
            _sale_rows.append(f"  {'Amount Outstanding:':<20}${amt_out:,.2f}")
        _sale_rows.append(f"  {'Status:':<20}{_status_color}{current_status}\033[0m")

        _print_boxed("CURRENT RECORD", [
            ("UNIT", [
                f"  {'SKU:':<20}{row['SKU']}",
                f"  {'Weave Type:':<20}{row['Weave Type / Cluster']}",
                f"  {'Supplier:':<20}{row['Supplier']}",
            ]),
            ("SALE", _sale_rows),
        ])

        if ask_yes_no("Is this the correct unit?"):
            break
        print()

    # Step 3: Cancellation reason (mandatory — every cancellation must be documented)
    cancel_reason = ask_text(
        f"Reason for cancelling {sku}:",
        blank_message="A reason is required. Please explain why this sale is being cancelled.",
    )

    # Restore original pricing metrics from the base selling price (pre-discount)
    selling_price_usd = _sheet_float(row.get("Selling Price (USD)", ""))
    total_cost_usd    = _sheet_float(row.get("Total Cost (USD)", ""))
    if total_cost_usd:
        orig_gross_profit = round(selling_price_usd - total_cost_usd, 2)
        orig_markup_pct   = round(orig_gross_profit / total_cost_usd * 100, 2)
        orig_margin_pct   = round(orig_gross_profit / selling_price_usd * 100, 2) if selling_price_usd else 0
    else:
        orig_gross_profit = 0.0
        orig_markup_pct   = 0.0
        orig_margin_pct   = 0.0

    # Step 4: Partial payment refund acknowledgment (before summary so it appears in ACTION)
    _refund_pending   = False
    _partial_amt_recv = _sheet_float(row.get("Amount Received (USD)", ""))
    _refund_clause    = ""
    _refund_label     = ""
    if current_status == "Sold - Partial Payment" and _partial_amt_recv:
        _refunded = ask_yes_no(f"Was the partial payment of ${_partial_amt_recv:,.2f} refunded to the customer?")
        if _refunded:
            _refund_clause = f" Partial payment of ${_partial_amt_recv:,.2f} refunded to customer."
            _refund_label  = f"Refunded ${_partial_amt_recv:,.2f} to customer."
        else:
            _refund_clause = f" Partial payment of ${_partial_amt_recv:,.2f} not yet refunded."
            _refund_label  = f"${_partial_amt_recv:,.2f} refund pending."
            _refund_pending = True

    # Step 5: Confirmation summary
    _av_color = get_status_color("Available")

    _txn_rows = [
        f"  {'Customer:':<20}{row.get('Customer Name', '')}",
        f"  {'Date Sold:':<20}{row.get('Date Sold', '')}",
        f"  {'Sales Channel:':<20}{row.get('Sales Channel', '')}",
    ]
    if actual_price:
        _txn_rows.append(f"  {'Unit Sold For:':<20}${actual_price:,.2f}")
    if current_status == "Sold - Partial Payment":
        _txn_rows.append(f"  {'Amount Received:':<20}${_sheet_float(row.get('Amount Received (USD)', '')):,.2f}")
        _txn_rows.append(f"  {'Amount Outstanding:':<20}${_sheet_float(row.get('Amount Outstanding (USD)', '')):,.2f}")

    _action_rows = [f"  {'Status:':<20}{_status_color}{current_status}\033[0m  →  {_av_color}Available\033[0m"]
    if cancel_reason:
        _reason_lines = textwrap.wrap(cancel_reason, width=38)
        _action_rows.append(f"  {'Reason:':<20}{_reason_lines[0]}")
        for _rl in _reason_lines[1:]:
            _action_rows.append(f"  {'':<20}{_rl}")
    if _refund_label:
        _action_rows.append(f"  {'Refund:':<20}{_refund_label}")

    _print_boxed("CANCELLATION SUMMARY", [
        ("UNIT", [
            f"  {'SKU:':<20}{sku}",
            f"  {'Weave Type:':<20}{row['Weave Type / Cluster']}",
            f"  {'Supplier:':<20}{row['Supplier']}",
        ]),
        ("TRANSACTION BEING REVERSED", _txn_rows),
        ("ACTION", _action_rows),
    ])

    confirmed = ask_yes_no("Cancel this sale and restore the unit to Available?")
    if not confirmed:
        print("\nCancellation discarded. Returning to Main Menu.")
        return

    # Build audit note appended to Transaction Notes for traceability
    today_str       = date.today().strftime("%m-%d-%Y")
    _orig_customer  = row.get("Customer Name", "").strip()
    _orig_date_sold = row.get("Date Sold", "").strip()

    note_sub = f"Previously sold to {_orig_customer}" if _orig_customer else ""
    if note_sub and _orig_date_sold:
        note_sub += f" on {_orig_date_sold}"
    if note_sub and actual_price:
        note_sub += f" for ${actual_price:,.2f}"
    if note_sub:
        note_sub += "."
    reason_clause = f" Reason for cancelling sale of {sku}: {cancel_reason}." if cancel_reason else ""
    _new_audit_note = f"[{today_str} · CANCEL] Sale cancelled. {note_sub}{reason_clause}{_refund_clause}".strip()

    updates = {
        "Date Sold":                  "",
        "Sales Channel":              "",
        "Customer Name":              "",
        "Customer Country Code":      "",
        "Customer Phone":             "",
        "Customer Email":             "",
        "Customer City":              "",
        "Customer State":             "",
        "Customer Country":           "",
        "Status":                     "Available",
        "Actual Selling Price (USD)": selling_price_usd,
        "Gross Profit (USD)":         orig_gross_profit,
        "Markup %":                   round(orig_markup_pct / 100, 6),
        "(Profit) Margin %":          round(orig_margin_pct / 100, 6),
        "Discount %":                 "",
        "Amount Received (USD)":      "",
        "Amount Outstanding (USD)":   "",
    }

    if not _status_unchanged(row_index, current_status):
        _warn(f"This unit's status has changed since you started (no longer '{current_status}'). "
              f"Someone else may have just updated it — nothing was written. Please re-check the SKU.")
        return

    # Total Cost (USD) and Selling Price (USD) aren't written by this
    # operation, but the "restored" Gross Profit/Markup/Margin were derived
    # from them -- if either drifted since Step 1 (a concurrent reprice or
    # cost edit while this cancellation was being confirmed), the restored
    # figures no longer match the unit's actual cost or price.
    _unchanged, _conflict_col = _row_fields_unchanged(
        row_index, row, updates.keys() - {"Status"} | {"Total Cost (USD)", "Selling Price (USD)"}
    )
    if not _unchanged:
        _warn(f"This unit's '{_conflict_col}' has changed since you started — someone else may have "
              f"just updated it (e.g. a payment recorded against it). Nothing was written. "
              f"Please re-check the SKU.")
        return

    # Append rather than overwrite -- same reasoning as record_sale(): this
    # is the only place a prior SALE (or an earlier CANCEL) note on this
    # unit survives. Read fresh at commit time, not the Step-1 snapshot, so
    # a concurrent session's own note isn't discarded either.
    updates["Transaction Notes"] = _fresh_notes_append(row_index, "Transaction Notes", [_new_audit_note])

    # If refund is pending, flag on Inventory Notes so it surfaces in future
    # operations -- otherwise leave Inventory Notes as whatever it currently
    # is (read fresh, not the stale snapshot, so this cancel doesn't
    # accidentally revert a note someone else just added).
    if _refund_pending:
        _refund_flag = f"[{today_str} · REFUND] ${_partial_amt_recv:,.2f} refund pending — {_orig_customer}"
        updates["Inventory Notes"] = _fresh_notes_append(row_index, "Inventory Notes", [_refund_flag])

    update_row(row_index, updates)
    print(f"\n\033[38;5;202m✓ {sku} sale cancelled. Unit restored to Available.\033[0m")


def manage_reservation():
    print("\n--- \033[1;38;5;124mMANAGE RESERVATION\033[0m ---")

    # Overdue reservation sweep — surfaces expired holds before SKU entry
    _all_rows = get_all_rows()
    _overdue  = []
    for _r in _all_rows:
        if _r.get("Status", "").strip() == "Reserved":
            _rdate = _r.get("Reserved Date", "").strip()
            if _rdate:
                _days, _expired = _reservation_days(_rdate)
                if _expired:
                    _rname = _get_reserver_name(_r.get("Inventory Notes", "")) or "—"
                    _overdue.append((_r.get("SKU", "").strip(), _rname, _days))
    if _overdue:
        _sweep_sep = f"\033[2m{'—' * 50}\033[0m"
        _ct = len(_overdue)
        _warn(f"{_ct} reservation{'s' if _ct > 1 else ''} {'have' if _ct > 1 else 'has'} exceeded the 7-day window:")
        print(_sweep_sep)
        print(f"  \033[2m{'UNIT':<14}{'RESERVER':<16}DAYS RESERVED\033[0m")
        print(_sweep_sep)
        for _osku, _orname, _odays in _overdue:
            print(f"  {_osku:<14}{_clip(_orname, 16):<16}{_odays} days")
        print(_sweep_sep)

    # Steps 1–2: SKU lookup + confirmation (loops on wrong unit or invalid status)
    while True:
        sku = ask_text("SKU:", blank_message="SKU cannot be left blank. Please enter a valid SKU (e.g. LAH-KKVSV500).")
        row_index = find_row_index_by_sku(sku)
        if row_index is None:
            _warn(f"SKU '{sku}' was not found in the master sheet. Please check the SKU and try again.")
            continue

        row = get_row_by_sheet_index(row_index)
        current_status = row.get("Status", "").strip()

        if current_status not in ("Available", "Reserved"):
            _warn(f"SKU '{sku}' has status '{current_status}'. Only Available or Reserved units can be managed here.")
            continue

        _status_color = get_status_color(current_status)
        _res_expired  = False

        if not str(row.get("Selling Price (USD)", "")).strip():
            _warn("Selling Price (USD) is blank or unreadable on the sheet for this unit -- the price "
                  "shown below is being displayed as $0.00, which may not be correct. Verify directly "
                  "on the sheet before relying on it.")

        selling_price_fmt = f"${_sheet_float(row.get('Selling Price (USD)', '')):,.2f}"

        _unit_rows = [
            f"  {'SKU:':<18}{row['SKU']}",
            f"  {'Weave Type:':<18}{row['Weave Type / Cluster']}",
            f"  {'Supplier:':<18}{row['Supplier']}",
            f"  {'Date Acquired:':<18}{row['Date Acquired']}",
            f"  {'Selling Price:':<18}{selling_price_fmt}",
        ]
        _sections = [("UNIT", _unit_rows)]
        if current_status == "Available":
            _sections.append(("STATUS", [f"  {'Status:':<18}{_status_color}{current_status}\033[0m"]))
        if current_status == "Reserved":
            _rdate        = row.get("Reserved Date", "").strip()
            _reserver_cur = _get_reserver_name(row.get("Inventory Notes", "")) or "—"
            _res_rows = [
                f"  {'Reserved By:':<18}{_reserver_cur}",
                f"  {'Reserved Date:':<18}{_rdate}",
            ]
            if _rdate:
                _days_res, _res_expired = _reservation_days(_rdate)
                if _days_res is not None:
                    _dr_color = "\033[91m" if _res_expired else "\033[92m"
                    _res_rows.append(f"  {'Days Reserved:':<18}{_dr_color}{_days_res} of 7\033[0m")
            _res_rows.append(f"  {'Status:':<18}{_status_color}{current_status}\033[0m")
            _sections.append(("RESERVATION", _res_rows))
        _print_boxed("CURRENT RECORD", _sections)
        if _res_expired:
            _warn("Reservation has exceeded the 7-day maximum.")

        if ask_yes_no("Is this the correct unit?"):
            break
        print()

    # Branch: Available → Reserved
    if current_status == "Available":
        _date_acquired = _safe_parse_date(row.get("Date Acquired", ""))
        while True:
            reserved_date     = ask_date(
                "Reserved Date", not_future=True, not_before=_date_acquired,
                not_before_msg=(
                    f"Reserved Date cannot be before Date Acquired "
                    f"({_date_acquired.strftime('%m-%d-%Y')})." if _date_acquired else None
                ),
            )
            reserved_date_str = reserved_date.strftime("%m-%d-%Y")
            days_since        = (date.today() - reserved_date).days
            if days_since > 7:
                _warn("Reserved Date is past the 7-day window — this unit will show as overdue on save.")
                if ask_yes_no("Proceed anyway?"):
                    break
            else:
                break

        # Reserver identity — use existing customer lookup, fall back to manual entry
        raw_rows = get_raw_rows()
        selected = search_and_select_customer(raw_rows)

        if selected == "__CANCEL__":
            print("\nReservation cancelled. Returning to Main Menu.")
            return

        if selected is not None and not isinstance(selected, tuple):
            selected          = update_customer_details_if_needed(selected, raw_rows) or selected
            reserver_name     = selected["name"]
            _cc               = _fmt_dial_code(selected.get("country_code", "").strip())
            _ph               = selected.get("phone", "").strip()
            reserver_phone    = format_phone_display(_cc, _ph, country=selected.get("country", "")) if _ph else ""
            reserver_email    = selected.get("email", "").strip()
            _existing_customer = True
        else:
            _initial_name = selected[1] if isinstance(selected, tuple) else None
            while True:
                if _initial_name:
                    reserver_name  = _initial_name
                    _initial_name  = None
                else:
                    _initial_name  = None
                    reserver_name = ask_text("Reserver's name:")
                if " " not in reserver_name.strip():
                    print(f"\n\033[33m⚠  SINGLE NAME DETECTED\033[0m")
                    print(f"\033[2m{'—' * 50}\033[0m")
                    print("Adding a last name helps distinguish customers with the same first name.")
                    if ask_yes_no("Would you like to add a last name?"):
                        continue
                break
            reserver_phone     = ask_contact_method()
            reserver_email     = ""
            _existing_customer = False

        reservation_context = ask_text("Reservation Note (press Enter to skip):", required=False)

        _contact_parts    = [c for c in [reserver_phone, reserver_email] if c]
        reserver_display  = f"{reserver_name} — {', '.join(_contact_parts)}" if _contact_parts else reserver_name
        reservation_note  = f"[{reserved_date_str} · RESERVATION] Reserved by {reserver_display}"

        _res_color = get_status_color("Reserved")
        _av_color  = get_status_color("Available")

        _reserver_rows = [f"  {'Reserver:':<18}{reserver_name}"]
        if _existing_customer:
            if reserver_phone:
                _reserver_rows.append(f"  {'Phone:':<18}{reserver_phone}")
            if reserver_email:
                _reserver_rows.append(f"  {'Email:':<18}{reserver_email}")
        elif reserver_phone:
            _reserver_rows.append(f"  {'Contact:':<18}{reserver_phone}")

        _action_rows = [
            f"  {'Reserved Date:':<18}{reserved_date_str}",
            f"  {'Status:':<18}{_av_color}Available\033[0m  →  {_res_color}Reserved\033[0m",
        ]
        if reservation_context:
            _note_lines = textwrap.wrap(reservation_context, width=30)
            _action_rows.append(f"  {'Note:':<18}{_note_lines[0]}")
            for _nl in _note_lines[1:]:
                _action_rows.append(f"  {'': <18}{_nl}")

        _print_boxed("RESERVATION SUMMARY", [
            ("UNIT", [
                f"  {'SKU:':<18}{sku}",
                f"  {'Weave Type:':<18}{row['Weave Type / Cluster']}",
                f"  {'Supplier:':<18}{row['Supplier']}",
                f"  {'Selling Price:':<18}{selling_price_fmt}",
            ]),
            ("RESERVER", _reserver_rows),
            ("ACTION", _action_rows),
        ])

        confirmed = ask_yes_no("Mark this unit as Reserved?")
        if not confirmed:
            print("\nReservation cancelled. Returning to Main Menu.")
            return

        if not _status_unchanged(row_index, current_status):
            _warn(f"This unit's status has changed since you started (no longer '{current_status}'). "
                  f"Someone else may have just updated it — nothing was written. Please re-check the SKU.")
            return

        _new_note_lines = [reservation_note]
        if reservation_context:
            _new_note_lines.append(f"[{reserved_date_str} · RSVP] {reservation_context}")

        update_row(row_index, {
            "Status":          "Reserved",
            "Reserved Date":   reserved_date_str,
            "Inventory Notes": _fresh_notes_append(row_index, "Inventory Notes", _new_note_lines),
        })
        print(f"\n\033[38;5;202m✓ {sku} reserved for {reserver_name}.\033[0m")

    # Branch: Reserved → Available
    else:
        reserved_date_display = row.get("Reserved Date", "").strip()
        existing_notes        = row.get("Inventory Notes", "").strip()
        cleaned_notes         = _strip_reservation_note(existing_notes)

        _res_line = next(
            (l for l in existing_notes.split('\n') if '· RESERVATION]' in l and 'Reserved by ' in l),
            None,
        )
        if _res_line:
            _res_full = _res_line.split('Reserved by ', 1)[1].strip()
            if ' — ' in _res_full:
                reserver_name_display    = _res_full.split(' — ')[0].strip()
                reserver_contact_display = _res_full.split(' — ', 1)[1].strip()
            else:
                reserver_name_display    = _res_full
                reserver_contact_display = "—"
        else:
            reserver_name_display    = "—"
            reserver_contact_display = "—"

        _res_color   = get_status_color("Reserved")
        _av_color    = get_status_color("Available")

        release_note = ask_text("Release note (press Enter to skip):", required=False)

        _release_action_rows = [
            f"  {'Reserved Date:':<18}{reserved_date_display}",
            f"  {'Status:':<18}{_res_color}Reserved\033[0m  →  {_av_color}Available\033[0m",
        ]
        if release_note:
            _rn_lines = textwrap.wrap(release_note, width=30)
            _release_action_rows.append(f"  {'Note:':<18}{_rn_lines[0]}")
            for _rn in _rn_lines[1:]:
                _release_action_rows.append(f"  {'': <18}{_rn}")

        _print_boxed("RELEASE RESERVATION SUMMARY", [
            ("UNIT", [
                f"  {'SKU:':<18}{sku}",
                f"  {'Weave Type:':<18}{row['Weave Type / Cluster']}",
                f"  {'Supplier:':<18}{row['Supplier']}",
                f"  {'Selling Price:':<18}{selling_price_fmt}",
            ]),
            ("RESERVER", [
                f"  {'Reserver:':<18}{reserver_name_display}",
                f"  {'Contact:':<18}{reserver_contact_display}",
            ]),
            ("ACTION", _release_action_rows),
        ])

        confirmed = ask_yes_no("Release this reservation?")
        if not confirmed:
            print("\nRelease cancelled. Returning to Main Menu.")
            return

        today_str    = date.today().strftime("%m-%d-%Y")
        _note_clause = f" {release_note}" if release_note else ""
        _release_tag = f"[{today_str} · RELEASE] Reservation released.{_note_clause}"

        if not _status_unchanged(row_index, current_status):
            _warn(f"This unit's status has changed since you started (no longer '{current_status}'). "
                  f"Someone else may have just updated it — nothing was written. Please re-check the SKU.")
            return

        # Re-strip against a fresh read, not the Step-1 snapshot's
        # cleaned_notes (used only for the confirmation display above) --
        # so a note added by someone else in the meantime survives.
        _fresh_cleaned = _strip_reservation_note(get_row_by_sheet_index(row_index).get("Inventory Notes", ""))
        final_notes = f"{_fresh_cleaned}\n{_release_tag}".strip() if _fresh_cleaned else _release_tag

        update_row(row_index, {
            "Status":          "Available",
            "Reserved Date":   "",
            "Inventory Notes": final_notes,
        })
        print(f"\n\033[38;5;202m✓ {sku} reservation released. Unit restored to Available.\033[0m")


def _pick_report_year(today, years, prompt="Select a year:"):
    """Arrow-key year list with a Back option set apart from the real
    choices — blank-line gap + de-emphasized styling, matching how the main
    menu itself separates "Exit" from the numbered operations above it.
    Returns the chosen year, or None if the user backed out — the caller
    loops back to whatever prompt came before this one rather than exiting
    the whole flow."""
    print()
    choice = questionary.select(
        prompt,
        choices=[str(yr) for yr in years] + [questionary.Separator(" "), _back_choice()],
        qmark="", instruction=" ", style=_MENU_STYLE,
    ).unsafe_ask()
    if choice is None or choice == "Back":
        return None
    return int(choice)

def _pick_monthly_period(today, _cal):
    """Year, then month within that year — both lists stay short (at most 12
    items) no matter how far back the business's history eventually runs,
    rather than one flat trailing-12-months list that never reaches further
    back. Returns (ptype, start, end, label), or None if backed out to the
    period-type menu entirely."""
    years = list(range(today.year, BUSINESS_START_YEAR - 1, -1))[:12]
    while True:
        yr = _pick_report_year(today, years)
        if yr is None:
            return None
        # Exclude the current, still-running month -- same reasoning as
        # _pick_annual_period excluding the current year: a report for a
        # period that isn't over yet isn't meaningful, and would compare
        # a partial month against a complete prior one.
        last_month = today.month - 1 if yr == today.year else 12
        if last_month == 0:
            _warn(f"No completed months yet in {yr}.")
            continue
        month_labels = [date(yr, mo, 1).strftime("%B") for mo in range(1, last_month + 1)]
        print()
        choice = questionary.select(
            f"Select a month in {yr}:",
            choices=month_labels + [questionary.Separator(" "), _back_choice()],
            qmark="", instruction=" ", style=_MENU_STYLE,
        ).unsafe_ask()
        if choice is None or choice == "Back":
            continue
        mo    = month_labels.index(choice) + 1
        start = date(yr, mo, 1)
        end   = date(yr, mo, _cal.monthrange(yr, mo)[1])
        label = f"{choice} {yr}"
        return ("monthly", start, end, label)

def _pick_quarterly_period(today, _cal):
    """Year, then quarter within that year — same reasoning as monthly."""
    years = list(range(today.year, BUSINESS_START_YEAR - 1, -1))[:12]
    while True:
        yr = _pick_report_year(today, years)
        if yr is None:
            return None
        # Exclude the current, still-running quarter -- same reasoning as
        # _pick_annual_period excluding the current year.
        current_q  = (today.month - 1) // 3 + 1
        last_q     = current_q - 1 if yr == today.year else 4
        if last_q == 0:
            _warn(f"No completed quarters yet in {yr}.")
            continue
        quarter_labels = [f"Q{q}" for q in range(1, last_q + 1)]
        print()
        choice = questionary.select(
            f"Select a quarter in {yr}:",
            choices=quarter_labels + [questionary.Separator(" "), _back_choice()],
            qmark="", instruction=" ", style=_MENU_STYLE,
        ).unsafe_ask()
        if choice is None or choice == "Back":
            continue
        qt    = quarter_labels.index(choice) + 1
        sm    = (qt - 1) * 3 + 1
        start = date(yr, sm, 1)
        end   = date(yr, sm + 2, _cal.monthrange(yr, sm + 2)[1])
        label = f"{choice} {yr}"
        return ("quarterly", start, end, label)

def _pick_annual_period(today):
    """A single 12-year list — capped at the same length as the month list,
    per design. Excludes the current (still-in-progress) year, same as the
    original trailing-window behavior, since a report for an incomplete year
    isn't meaningful."""
    years = list(range(today.year - 1, BUSINESS_START_YEAR - 1, -1))[:12]
    yr = _pick_report_year(today, years)
    if yr is None:
        return None
    return ("annual", date(yr, 1, 1), date(yr, 12, 31), str(yr))

def _pick_custom_range():
    while True:
        start = ask_date("Start date", not_future=True,
                          not_before=date(BUSINESS_START_YEAR, 1, 1),
                          not_before_msg=f"Date cannot be before {BUSINESS_START_YEAR}, when the business began.",
                          allow_back=True)
        if start is None:
            return None
        end = ask_date("End date", not_future=True, not_before=start,
                        not_before_msg="End date must be on or after start date.",
                        allow_back=True)
        if end is None:
            continue
        label = f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"
        return ("custom", start, end, label)

def _parse_date_sold(row):
    """Date Sold as a date, or None if blank/malformed -- never raises.
    Anything that sorts, compares, or scopes by Date Sold should go
    through this instead of calling datetime.strptime directly, so a bad
    cell (blank, or hand-edited into something invalid -- the app itself
    always writes a valid date at entry) degrades gracefully -- excluded,
    visibly -- instead of either silently skewing stats or crashing the
    whole Insights view. Thin wrapper over _safe_parse_date() -- kept as
    its own function since it's called from several places by row alone."""
    return _safe_parse_date(row.get("Date Sold", ""))


def _customer_sold_rows(rows, customer, start=None, end=None):
    """Rows for one customer with Status Sold/Sold - Partial Payment,
    optionally scoped to Date Sold falling within [start, end]. Matched
    by identity (see _customer_identity_key), not by Customer Name alone
    -- otherwise two different customers who share a name would have
    their purchase history merged here."""
    target_key = _customer_identity_key(
        customer["name"], customer.get("country_code", ""),
        customer.get("phone", ""), customer.get("email", ""),
    )
    out = []
    for r in rows:
        # Not .strip()'d here -- gspread's get_all_records() can return a
        # numeric-only cell (e.g. country code "1") as an int, and
        # _customer_identity_key() already coerces+strips every field.
        row_key = _customer_identity_key(
            r.get("Customer Name", ""), r.get("Customer Country Code", ""),
            r.get("Customer Phone", ""), r.get("Customer Email", ""),
        )
        if row_key != target_key:
            continue
        if r.get("Status", "").strip() not in ("Sold", "Sold - Partial Payment"):
            continue
        if start is not None:
            ds = _parse_date_sold(r)
            if ds is None or not (start <= ds <= end):
                continue
        out.append(r)
    return out


def _business_totals(rows, start=None, end=None):
    """Total revenue and gross profit across every customer, same scope as
    a customer's own figures -- the denominator for business-contribution %."""
    total_rev = 0.0
    total_gp  = 0.0
    for r in rows:
        if r.get("Status", "").strip() not in ("Sold", "Sold - Partial Payment"):
            continue
        if start is not None:
            ds = _parse_date_sold(r)
            if ds is None or not (start <= ds <= end):
                continue
        total_rev += _sheet_float(r.get("Actual Selling Price (USD)"))
        total_gp  += _sheet_float(r.get("Gross Profit (USD)"))
    return total_rev, total_gp


def _print_customer_insights(customer, all_rows, ptype, start, end, label):
    """Print the Purchase Summary + Purchase History for one customer,
    scoped to [start, end] (ptype == 'all' -- start/end None -- for
    lifetime). Lifetime-only facts (preferred weave/channel, recency)
    always come from the customer's full history regardless of scope,
    since those describe the customer, not a window in time -- a stale
    "days since last purchase" under an old period filter would mislead."""
    name = customer["name"]
    lifetime_rows = _customer_sold_rows(all_rows, customer)
    scope_rows = lifetime_rows if ptype == "all" else _customer_sold_rows(all_rows, customer, start, end)

    if not scope_rows:
        print(f"\n\033[33mNo purchases by {name} in {label}.\033[0m")
        return

    units       = len(scope_rows)
    total_spend = sum(_sheet_float(r.get("Actual Selling Price (USD)")) for r in scope_rows)
    total_gp    = sum(_sheet_float(r.get("Gross Profit (USD)")) for r in scope_rows)
    aov         = total_spend / units if units else 0.0

    disc_rows_scope = [r for r in scope_rows if _sheet_float(r.get("Discount %")) > 0]
    total_discount   = sum(_sheet_float(r.get("Selling Price (USD)")) - _sheet_float(r.get("Actual Selling Price (USD)"))
                            for r in disc_rows_scope)
    if disc_rows_scope:
        avg_discount_pct = sum(_sheet_float(r.get("Discount %")) for r in disc_rows_scope) / len(disc_rows_scope)
        discount_display = f"${total_discount:,.2f} (avg {avg_discount_pct:.1f}%)"
    else:
        discount_display = f"${total_discount:,.2f}"

    biz_start, biz_end = (None, None) if ptype == "all" else (start, end)
    biz_rev, biz_gp = _business_totals(all_rows, biz_start, biz_end)
    revenue_share = (total_spend / biz_rev * 100) if biz_rev else 0.0
    gp_share      = (total_gp / biz_gp * 100) if biz_gp else 0.0

    spend_label = "Lifetime Spend:" if ptype == "all" else "Total Spend:"
    sections = [
        ("SPEND", [
            f"  {spend_label:<28}${total_spend:,.2f}",
            f"  {'Gross Profit:':<28}${total_gp:,.2f}",
            f"  {'Total Discount Given:':<28}{discount_display}",
            f"  {'Average Order Value:':<28}${aov:,.2f}",
            f"  {'Units Purchased:':<28}{units}",
        ]),
        ("BUSINESS CONTRIBUTION", [
            f"  {'Revenue Share:':<28}{revenue_share:.1f}%",
            f"  {'Gross Profit Share:':<28}{gp_share:.1f}%",
        ]),
    ]

    undated_count = 0
    if ptype == "all":
        weave_tally, channel_tally = {}, {}
        first_sold, last_sold = None, None
        for r in lifetime_rows:
            w = r.get("Weave Type / Cluster", "").strip()
            if w:
                weave_tally[w] = weave_tally.get(w, 0) + 1
            ch = r.get("Sales Channel", "").strip()
            if ch:
                channel_tally[ch] = channel_tally.get(ch, 0) + 1
            ds = _parse_date_sold(r)
            if ds is None:
                undated_count += 1
                continue
            if last_sold is None or ds > last_sold:
                last_sold = ds
            if first_sold is None or ds < first_sold:
                first_sold = ds

        if weave_tally:
            top_weave = max(weave_tally, key=weave_tally.get)
            weave_display = f"{top_weave} ({weave_tally[top_weave] / units * 100:.0f}%)"
        else:
            weave_display = "—"
        first_display = first_sold.strftime("%m-%d-%Y") if first_sold else "—"
        last_display  = last_sold.strftime("%m-%d-%Y") if last_sold else "—"
        days_since = _days_since(last_sold)
        recency_display = f"{days_since} days" if days_since is not None else "—"
        if units > 1 and first_sold and last_sold:
            avg_between_display = f"{(last_sold - first_sold).days / (units - 1):.0f} days"
        else:
            avg_between_display = "—"

        # Sales Channel capture is currently at 0% at point-of-sale (per
        # Deliverables_Roadmap.md item 7.5) -- showing "Sales Channel: —"
        # on every customer would just be permanent clutter for a field
        # that isn't populated yet, so this line is dropped entirely
        # rather than shown as a dash, mirroring how Generate Report
        # already hides its own Sales Channel breakdown under the same
        # low-coverage condition. It'll appear naturally once real data
        # exists.
        prefs_rows = [f"  {'Weave Type:':<28}{weave_display}"]
        if channel_tally:
            top_channel = max(channel_tally, key=channel_tally.get)
            channel_display = f"{top_channel} ({channel_tally[top_channel] / units * 100:.0f}%)"
            prefs_rows.append(f"  {'Sales Channel:':<28}{channel_display}")
        sections.append(("PREFERENCES", prefs_rows))
        sections.append(("RECENCY", [
            f"  {'Date of First Purchase:':<28}{first_display}",
            f"  {'Date of Last Purchase:':<28}{last_display}",
            f"  {'Days Since Last Purchase:':<28}{recency_display}",
            f"  {'Avg Days Between Purchases:':<28}{avg_between_display}",
        ]))

    _print_boxed(f"PURCHASE SUMMARY — {label.upper()}", sections)

    if undated_count:
        _verb = "has" if undated_count == 1 else "have"
        print(f"\n\033[2mNote: {undated_count} of {units} purchase(s) {_verb} no usable Date Sold on "
              f"file -- included in the totals above, excluded from the recency figures.\033[0m")

    if ptype == "all":
        outstanding_rows = [r for r in lifetime_rows if r.get("Status", "").strip() == "Sold - Partial Payment"]
        if outstanding_rows:
            today = date.today()

            def _out_days(r):
                """Days outstanding, or None if Date Sold is blank/
                malformed/in the future -- never silently reads as 0
                (fresh/low-urgency) for a balance whose real age is
                simply unknown."""
                return _days_since(_parse_date_sold(r), reference_date=today)

            def _out_days_color(d):
                if d is None:
                    return "\033[95m"
                if d <= 30:
                    return "\033[92m"
                elif d < 60:
                    return "\033[93m"
                else:
                    return "\033[91m"

            def _out_days_display(d):
                return "unknown" if d is None else str(d)

            def _out_sort_key(r):
                d = _out_days(r)
                is_known = d is not None
                return (is_known, -d if is_known else 0, r.get("SKU", "").strip())

            outstanding_sorted = sorted(outstanding_rows, key=_out_sort_key)
            total_outstanding  = sum(_sheet_float(r.get("Amount Outstanding (USD)")) for r in outstanding_sorted)

            _oeq  = f"\033[2m{'=' * 50}\033[0m"
            _osep = f"\033[2m{'—' * 50}\033[0m"
            print()  # extra gap -- follows straight after the PURCHASE
                     # SUMMARY box with no prompt between them
            print("\n" + _oeq)
            print("  [1;38;5;178mCURRENT OUTSTANDING BALANCE[0m")
            print(_oeq)
            print(f"\033[38;5;202m  {'SKU':<16}{'Outstanding':<16}Days Outstanding\033[0m")
            print(_osep)
            for r in outstanding_sorted:
                sku = r.get("SKU", "").strip()
                amt = _sheet_float(r.get("Amount Outstanding (USD)"))
                d   = _out_days(r)
                dc  = _out_days_color(d)
                print(f"  {sku:<16}${amt:<15,.2f}{dc}{_out_days_display(d)}\033[0m")
            _ofooter = f"Units Outstanding: {len(outstanding_sorted)} | Amount Outstanding: ${total_outstanding:,.2f}"
            print(_oeq)
            print(_ofooter.center(50))
            print(_oeq)

    ordered = sorted(
        scope_rows,
        key=lambda r: _parse_date_sold(r) or date.min,
        reverse=True,
    )

    print()  # extra gap -- follows straight after either the PURCHASE
             # SUMMARY box or the CURRENT OUTSTANDING BALANCE table (when
             # shown) with no prompt between them
    _eq  = f"\033[2m{'=' * 82}\033[0m"
    _sep = f"\033[2m{'—' * 82}\033[0m"
    print("\n" + _eq)
    print(f"  [1;38;5;178mPURCHASE HISTORY — {label.upper()}[0m")
    print(_eq)
    print(f"\033[38;5;202m  {'Date':<12}{'SKU':<16}{'Weave Type':<20}{'List Price':<12}{'Discount':<10}Sold For\033[0m")
    print(_sep)
    for r in ordered:
        date_str = r.get("Date Sold", "").strip() or "—"
        sku      = r.get("SKU", "").strip()
        weave    = _clip(r.get("Weave Type / Cluster", "").strip(), 18)
        orig     = _sheet_float(r.get("Selling Price (USD)"))
        actual   = _sheet_float(r.get("Actual Selling Price (USD)"))
        disc_pct = _sheet_float(r.get("Discount %"))
        disc_str = f"{disc_pct:.1f}%" if disc_pct else "—"
        print(f"  {date_str:<12}{sku:<16}{weave:<20}${orig:<11,.2f}{disc_str:<10}${actual:,.2f}")
    _footer = f"{units} purchase(s)  |  ${total_spend:,.2f} total"
    print(_eq)
    print(_footer.center(82))
    print(_eq)


def _pick_customer_period():
    """Reuses Generate Report's period-type picker. Returns (ptype, start,
    end, label), or None if backed out -- caller re-asks whether a period
    view is wanted rather than silently looping the menu again."""
    import calendar as _cal
    today = date.today()
    print()
    period_choice = questionary.select(
        "Select a period type:",
        choices=["Monthly", "Quarterly", "Annual", "Custom",
                 questionary.Separator(" "),
                 _back_choice()],
        qmark="", instruction=" ", style=_MENU_STYLE,
    ).unsafe_ask()
    if period_choice is None or period_choice == "Back":
        return None
    if period_choice == "Monthly":
        return _pick_monthly_period(today, _cal)
    if period_choice == "Quarterly":
        return _pick_quarterly_period(today, _cal)
    if period_choice == "Annual":
        return _pick_annual_period(today)
    return _pick_custom_range()


def _run_insights_for_customer(customer, all_rows):
    _print_customer_insights(customer, all_rows, "all", None, None, "All Time")
    asked_before = False
    while True:
        # No extra print() here -- matches the single-blank-line spacing
        # every confirmation box in the app uses before its own prompt;
        # ask_yes_no()'s own leading blank line is enough.
        prompt = "View a different period?" if asked_before else "View a specific period for this customer?"
        if not ask_yes_no(prompt):
            return
        result = _pick_customer_period()
        if result is None:
            continue
        ptype, start, end, label = result
        _print_customer_insights(customer, all_rows, ptype, start, end, label)
        asked_before = True


def customer_insights():
    print("\n--- \033[1;38;5;124mCUSTOMER INSIGHTS\033[0m ---")
    raw_rows = get_raw_rows()

    while True:  # "Look up another customer?" loop
        customer = search_and_select_customer(raw_rows, allow_new=False)
        if customer is None or customer == "__CANCEL__":
            print("\nReturning to Main Menu.")
            return

        _print_customer_block(customer)
        print()  # extra gap -- two boxes run back-to-back here with no
                 # prompt between them, so the usual single leading
                 # blank line inside _print_boxed isn't enough to keep
                 # them from reading as one continuous block
        all_rows = get_all_rows()
        _run_insights_for_customer(customer, all_rows)

        # No extra print() here, unlike the customer-block gap above --
        # _run_insights_for_customer() only ever returns right after a
        # single-line "no" answer to a yes/no prompt, never straight after
        # a table, so ask_yes_no()'s own leading blank line is enough.
        if not ask_yes_no("Look up another customer?"):
            print("\nReturning to Main Menu.")
            return


def generate_report_menu():
    try:
        import importlib
        # report_config must be reloaded BEFORE generate_report -- otherwise
        # generate_report's own "from report_config import ..." line re-runs
        # against the stale, already-cached report_config module, silently
        # ignoring any edit made to report_config.py during this session.
        import report_config
        importlib.reload(report_config)
        import generate_report as _report_module
        importlib.reload(_report_module)   # always run the latest version on disk
        generate_report = _report_module.generate_report
    except Exception as e:
        # Broad on purpose: this block only imports/reloads the report
        # generator, it doesn't generate anything yet. A missing module
        # (ImportError), a missing .env key like ANTHROPIC_API_KEY/
        # EMAIL_SENDER/EMAIL_PASSWORD (KeyError, since report_config.py
        # reads those at import time with no fallback), or a syntax error
        # from a live edit to generate_report.py (SyntaxError) should all
        # land here as "unavailable," not crash the whole CLI.
        _warn(f"Report generator unavailable: {e}")
        return

    import calendar as _cal

    print("\n--- \033[1;38;5;124mGENERATE REPORT\033[0m ---")

    today = date.today()

    while True:
        print()
        period_choice = questionary.select(
            "Select a period type:",
            choices=["Monthly", "Quarterly", "Annual", "Custom",
                     questionary.Separator(" "),
                     _back_choice("Return to Main Menu")],
            qmark="",
            instruction=" ",
            style=_MENU_STYLE,
        ).unsafe_ask()
        if period_choice is None or period_choice == "Return to Main Menu":
            return

        if period_choice == "Monthly":
            result = _pick_monthly_period(today, _cal)
        elif period_choice == "Quarterly":
            result = _pick_quarterly_period(today, _cal)
        elif period_choice == "Annual":
            result = _pick_annual_period(today)
        else:
            result = _pick_custom_range()

        if result is None:
            continue   # backed all the way out — re-show the period-type menu
        ptype, start, end, label = result
        break

    send_email = ask_yes_no("Email report when complete?")

    print()
    print(f"\033[2mGenerating {label} report...\033[0m")
    try:
        pdf_path = generate_report(ptype, start, end, label, send_email=send_email, mode=MODE)
        print(f"\n\033[38;5;202m✓ Report complete.\033[0m")
        print(f"\033[2m  {pdf_path}\033[0m")
    except Exception as e:
        # Return to the Main Menu instead of re-raising -- every other
        # operation's failure path does the same (show the warning, then
        # return), and this was the one place in the app where an error
        # could end the whole session instead.
        _warn(f"Report generation failed: {e}")
        print("\nReturning to Main Menu.")
        return


def main_menu():
    load_garment_types()
    load_countries()
    load_suppliers()
    load_weave_types()

    while True:
        print_banner()
        print("\033[2m1.\033[0m  Add inventory")
        print("\033[2m2.\033[0m  Edit inventory details")
        print("\033[2m3.\033[0m  Reprice a unit")
        print("\033[2m4.\033[0m  Discount simulator")
        print("\033[2m5.\033[0m  Manage reservation")
        print("\033[2m6.\033[0m  Record a sale")
        print("\033[2m7.\033[0m  Record outstanding payment")
        print("\033[2m8.\033[0m  Cancel a sale")
        print("\033[2m9.\033[0m  Customer insights")
        print("\033[2m10.\033[0m Generate report")
        print()
        print("\033[2m11. Exit\033[0m")
        print()
        raw = input("Select an option (1-11): ").strip()
        try:
            if raw == "1":
                add_new_inventory()
            elif raw == "2":
                edit_inventory_details()
            elif raw == "3":
                reprice_unit()
            elif raw == "4":
                discount_simulator()
            elif raw == "5":
                manage_reservation()
            elif raw == "6":
                record_sale()
            elif raw == "7":
                record_outstanding_payment()
            elif raw == "8":
                cancel_sale()
            elif raw == "9":
                customer_insights()
            elif raw == "10":
                generate_report_menu()
            elif raw == "11":
                print("\nGoodbye.")
                break
            else:
                _warn("Please enter a number between 1 and 11.")
        except KeyboardInterrupt:
            print("\n\nOperation interrupted. Returning to Main Menu.")
        except gspread.exceptions.APIError as e:
            print(f"\n\n\033[91mA Google Sheets error interrupted this operation: {e}\033[0m")
            print("Nothing further was written. Please check your connection and try again.")
        except Exception as e:
            print(f"\n\n\033[91mAn unexpected error interrupted this operation: {e}\033[0m")
            print("Returning to Main Menu. If this keeps happening, please contact Arohit for support.")


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Exiting.")
    except SystemExit:
        raise
    except Exception as e:
        print("\nAn unexpected error occurred. Please contact Arohit for support.")
        raise
