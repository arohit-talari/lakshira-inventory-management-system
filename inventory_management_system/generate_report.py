"""
generate_report.py — Lakshira Business Intelligence Report Generator
"""

import os, re, json, calendar, smtplib
from datetime import date, datetime, timedelta
from collections import defaultdict
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import mysql.connector

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, NextPageTemplate, KeepTogether, CondPageBreak,
    Flowable,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from report_config import (
    LOGO_PATH, FONT_PATHS, COLOUR, REPORTS_DIR, BASE_DIR,
    ANTHROPIC_API_KEY, EMAIL_SENDER, EMAIL_PASSWORD,
    EMAIL_RECIPIENTS, SMTP_SERVER, SMTP_PORT, DB_CONFIG,
)

# ── Page geometry ──────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = LETTER          # 612 × 792 pt
MARGIN         = 0.75 * inch     # 54 pt
CONTENT_W      = PAGE_W - 2 * MARGIN   # 504 pt

# ── Sheet config ───────────────────────────────────────────────────────────────
CREDS_PATH = os.path.join(BASE_DIR, "credentials.json")
SCOPE      = ["https://spreadsheets.google.com/feeds",
               "https://www.googleapis.com/auth/drive"]
SHEET_IDS = {
    "test": os.environ.get("TEST_SHEET_ID", ""),
    "live": os.environ.get("LIVE_SHEET_ID", ""),
}
WORKSHEET_NAME = "Lakshira Inventory"

# ── Colours ────────────────────────────────────────────────────────────────────
GOLD         = HexColor(COLOUR["gold"])
SAGE         = HexColor(COLOUR["sage"])
ROSE         = HexColor(COLOUR["rose"])
BURNT_ORANGE = HexColor(COLOUR["burnt_orange"])
DARK_BROWN   = HexColor(COLOUR["dark_brown"])
OFF_WHITE    = HexColor(COLOUR["off_white"])
LIGHT_GREY   = HexColor(COLOUR["light_grey"])

# ── Font registration ──────────────────────────────────────────────────────────
for _name, _path in FONT_PATHS.items():
    pdfmetrics.registerFont(TTFont(_name, _path))

# Register font families so <b> / <i> tags work inside Paragraph markup
pdfmetrics.registerFontFamily(
    "CormorantGaramond",
    normal="CormorantGaramond",
    bold="CormorantGaramond-Bold",
    italic="CormorantGaramond-Italic",
    boldItalic="CormorantGaramond-Bold",
)
pdfmetrics.registerFontFamily(
    "Montserrat",
    normal="Montserrat",
    bold="Montserrat-Bold",
    italic="Montserrat",
    boldItalic="Montserrat-Bold",
)
pdfmetrics.registerFontFamily(
    "Montserrat-Bold",
    normal="Montserrat-Bold",
    bold="Montserrat-Bold",
    italic="Montserrat-Bold",
    boldItalic="Montserrat-Bold",
)

# ── Styles ─────────────────────────────────────────────────────────────────────
def _s(name, **kw):
    return ParagraphStyle(name, **kw)

ST = {
    "sec_title":     _s("sec_title", fontName="CormorantGaramond-Bold",
        fontSize=18, textColor=GOLD, spaceBefore=14, spaceAfter=2),
    "sub_head":      _s("sub_head", fontName="Montserrat-Bold",
        fontSize=11, textColor=GOLD, spaceBefore=10, spaceAfter=5),

    "body":          _s("body", fontName="Montserrat",
        fontSize=9, textColor=DARK_BROWN, leading=14, spaceAfter=4),
    # Footnotes: Regular weight, readable size — per hierarchy spec, never thin/light
    "note":          _s("note", fontName="Montserrat",
        fontSize=8, textColor=DARK_BROWN, leading=12, spaceAfter=4),
    # A note's own bold-labeled subsections (What's healthy:, Also see:, etc.)
    # indented and split onto their own line via _split_note() -- same
    # subordinate-content convention as rec_support, applied to notes instead
    # of recommendations. The note's opening sentence (which typically names
    # the metric itself, e.g. "Sell-Through Rate measures...") stays in "note"
    # unindented; only what follows it is a subsection of that lead.
    "note_sub":      _s("note_sub", fontName="Montserrat",
        fontSize=8, textColor=DARK_BROWN, leading=12, spaceAfter=4, leftIndent=14),

    # Standard tables: headers bold, body regular (not Light), bold rows for emphasis
    "th_l":    _s("th_l",    fontName="Montserrat-Bold",  fontSize=8,   textColor=white,      alignment=TA_LEFT,  leading=11),
    "th_r":    _s("th_r",    fontName="Montserrat-Bold",  fontSize=8,   textColor=white,      alignment=TA_RIGHT, leading=11),
    "td_l":    _s("td_l",    fontName="Montserrat-Bold",   fontSize=8.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=12),
    "td_r":    _s("td_r",    fontName="Montserrat-Bold",   fontSize=8.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=12),
    "td_bl":   _s("td_bl",   fontName="Montserrat-Bold",  fontSize=8.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=12),
    "td_br":   _s("td_br",   fontName="Montserrat-Bold",  fontSize=8.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=12),
    # Center variants: for columns with no magnitude to compare place-by-place
    # (dates) or that are mostly a placeholder dash rather than a dense column
    # of numbers (e.g. Outstanding Balance) — right-alignment exists to let a
    # reader compare digits column-wise, and neither case benefits from that.
    "th_c":    _s("th_c",    fontName="Montserrat-Bold",  fontSize=8,   textColor=white,      alignment=TA_CENTER, leading=11),
    "td_c":    _s("td_c",    fontName="Montserrat-Bold",   fontSize=8.5, textColor=DARK_BROWN, alignment=TA_CENTER, leading=12),
    "td_bc":   _s("td_bc",   fontName="Montserrat-Bold",  fontSize=8.5, textColor=DARK_BROWN, alignment=TA_CENTER, leading=12),
    # Compact variants for wide tables — same weight progression, tighter size
    "th_l_sm": _s("th_l_sm", fontName="Montserrat-Bold",  fontSize=7,   textColor=white,      alignment=TA_LEFT,  leading=10),
    "th_r_sm": _s("th_r_sm", fontName="Montserrat-Bold",  fontSize=7,   textColor=white,      alignment=TA_RIGHT, leading=10),
    "th_c_sm": _s("th_c_sm", fontName="Montserrat-Bold",  fontSize=7,   textColor=white,      alignment=TA_CENTER, leading=10),
    "td_l_sm": _s("td_l_sm", fontName="Montserrat-Bold",   fontSize=7.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=11),
    "td_r_sm": _s("td_r_sm", fontName="Montserrat-Bold",   fontSize=7.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=11),
    "td_c_sm": _s("td_c_sm", fontName="Montserrat-Bold",   fontSize=7.5, textColor=DARK_BROWN, alignment=TA_CENTER, leading=11),
    "td_bl_sm":_s("td_bl_sm",fontName="Montserrat-Bold",  fontSize=7.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=11),
    "td_br_sm":_s("td_br_sm",fontName="Montserrat-Bold",  fontSize=7.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=11),
    "td_bc_sm":_s("td_bc_sm",fontName="Montserrat-Bold",  fontSize=7.5, textColor=DARK_BROWN, alignment=TA_CENTER, leading=11),

    # Scorecard — matches Tableau layout: gold label (top), heavy bold value (bottom)
    "metric_label": _s("metric_label", fontName="Montserrat-Bold",
        fontSize=10, textColor=GOLD, alignment=TA_CENTER, leading=13,
        spaceAfter=5),
    # fontSize kept small enough that the widest realistic value (e.g. "$999.9K")
    # never wraps within a 6-up card row — see _metric_row.
    "metric_val":   _s("metric_val", fontName="Montserrat-Bold",
        fontSize=16, textColor=DARK_BROWN, alignment=TA_CENTER, leading=20),
    "metric_sub":   _s("metric_sub", fontName="Montserrat",
        fontSize=8, textColor=HexColor("#6B5246"), alignment=TA_CENTER, leading=11),
    # White-text variants for a _metric_row on a colored (non-default) card
    # background -- same white-on-color convention already used for the
    # alert callouts (BURNT_ORANGE/ROSE), since the default gold label /
    # dark brown value combo is tuned for the neutral off-white card.
    "metric_label_inverse": _s("metric_label_inverse", fontName="Montserrat-Bold",
        fontSize=10, textColor=white, alignment=TA_CENTER, leading=13, spaceAfter=5),
    "metric_val_inverse":   _s("metric_val_inverse", fontName="Montserrat-Bold",
        fontSize=16, textColor=white, alignment=TA_CENTER, leading=20),

    "alert":        _s("alert", fontName="Montserrat", fontSize=8.5,
        textColor=white, leading=13),
    "exec_body":    _s("exec_body", fontName="Montserrat",
        fontSize=9.5, textColor=DARK_BROWN, leading=15, spaceAfter=8),
    "exec_rec_head":_s("exec_rec_head", fontName="Montserrat-Bold",
        fontSize=13, textColor=GOLD, spaceBefore=18, spaceAfter=8),
    # Numbered recommendation title — compact, semi-bold, not a full sentence
    "rec_title":    _s("rec_title", fontName="Montserrat-Bold",
        fontSize=10.5, textColor=DARK_BROWN, leading=13, spaceAfter=2),
    # The 1-2 sentence recommendation itself — regular weight, not bold
    "rec_body":     _s("rec_body", fontName="Montserrat",
        fontSize=9.5, textColor=DARK_BROWN, leading=14,
        spaceAfter=6, leftIndent=14),
    # Supporting lines (Why it matters / Next step / Expected impact) — small,
    # regular weight, visually subordinate. Labels are bolded inline via <font>
    # tags rather than at the paragraph level, so only the label reads as an accent.
    "rec_support":  _s("rec_support", fontName="Montserrat",
        fontSize=8.5, textColor=HexColor("#5C4A3A"), leading=12,
        spaceAfter=2, leftIndent=14),
    # Legacy fallback bullet style (kept for safety)
    "exec_rec":     _s("exec_rec", fontName="Montserrat", fontSize=8.5,
        textColor=DARK_BROWN, leading=13, spaceAfter=5,
        leftIndent=12, firstLineIndent=-12),
}

_NOTE_LABEL_RE = re.compile(r"<b>[^<]*:</b>")
_FIRST_BOLD_RE = re.compile(r"<b>")

def _split_note(text, style=None, sub_style=None):
    """Splits a note's text into one Paragraph per bold-labeled subsection
    (What's healthy:, Also see:, How it's measured:, etc.), each on its own
    indented line, rather than one dense block where the eye has no visual
    cue that a new, distinct point has started. Only a bold phrase that opens
    the text (position 0) is exempt as the lead, even when it happens to
    carry a colon itself (e.g. "Customer Concentration:", "Lifetime
    Loyalty:") -- it's naming what the whole note is about, not a subsection
    of something before it, the same role a plain metric-name lead
    ("Sell-Through Rate measures...") plays everywhere else. A colon-labeled
    bold tag that appears after unbolded lead prose (e.g. "This is a lifetime
    figure... <b>Also see:</b> ...") is a genuine subsection, not a lead, and
    must still split out. Returns a list of Paragraphs; splice it into a story
    list with + or *, never as a single nested element, since callers expect
    one flowable per line, not one Paragraph containing this whole note."""
    style = style or ST["note"]
    sub_style = sub_style or ST["note_sub"]
    lead_bold = _FIRST_BOLD_RE.match(text)
    splits = [m.start() for m in _NOTE_LABEL_RE.finditer(text)
              if not lead_bold or m.start() != 0]
    if not splits:
        return [Paragraph(text, style)]
    bounds = [0] + splits + [len(text)]
    pieces = [text[bounds[i]:bounds[i+1]].strip() for i in range(len(bounds)-1)]
    return [Paragraph(pieces[0], style)] + [Paragraph(p, sub_style) for p in pieces[1:]]

# ── Helpers: parsing ───────────────────────────────────────────────────────────
def _flt(v):
    try:
        return float(str(v).replace(",","").replace("$","").replace("%","").strip())
    except Exception:
        return 0.0

def _parse_date(v):
    try:
        return datetime.strptime(str(v).strip(), "%m-%d-%Y").date()
    except Exception:
        return None

def _in(d, s, e):
    return d is not None and s <= d <= e

def _days_since(target, reference=None):
    """Days from target to reference (default: today), or None if target
    is None or the result would be negative -- a "future" date only
    reaches the sheet via a hand-edit (every date column is validated
    not-future at entry in the interactive app), so it should read as
    "exclude this row from the average / bucket," not silently skew a
    report metric with a negative day count. This report runs unattended
    via scheduler.py's monthly cron job, so there's no human watching a
    screen who'd notice a nonsensical negative number the way an operator
    might in the interactive app."""
    if target is None:
        return None
    delta = ((reference or date.today()) - target).days
    return delta if delta >= 0 else None

def _customer_key(r):
    """Canonical per-customer identity for grouping/matching report rows --
    mirrors inventory.py's _customer_identity_key(). Prefers (country code
    + phone): phone is mandatory and enforced-unique at customer entry in
    the interactive app, so two unrelated people can share a name but can
    never share a phone number. Falls back to email, then name only when
    both are blank. Without this, two different real customers who happen
    to share a name would have their spend, purchase history, and
    new-vs-returning classification silently merged in every
    customer-facing metric below -- this script keeps its own copy of this
    logic rather than importing inventory.py's, since it's a fully
    separate entry point with its own data-loading path."""
    name  = str(r.get("Customer Name", "") or "").strip()
    phone = re.sub(r"\D", "", str(r.get("Customer Phone", "") or ""))
    code  = re.sub(r"\D", "", str(r.get("Customer Country Code", "") or ""))
    email = str(r.get("Customer Email", "") or "").strip().lower()
    if phone and code:
        return ("phone", code, phone)
    if email:
        return ("email", email)
    return ("name", name)

def _mysql_occasion_counts(start=None, end=None):
    """Distinct real purchase occasions per customer, from the MySQL warehouse --
    keyed the same way as _customer_key() so it can be matched directly against
    this script's own sheet-derived customer dicts. transaction_group_id links
    units bought by the same customer, on the same day, through the same sales
    channel into one shared value, so a single bulk purchase correctly counts
    as one occasion, not several -- the same fix applied to the Tableau
    dashboard's "Repeat Customers" KPI, previously (and wrongly) built on raw
    transaction_id. Returns {} on any connection failure rather than raising,
    since this only refines accuracy and the report should still generate
    without it.

    start/end, if both given, restrict to occasions whose date_sold falls in
    that inclusive range -- same >=/<= convention as this script's own _in()
    -- for period-scoped tables. Omit both for a lifetime count."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur = conn.cursor()
        where = ""
        params = ()
        if start and end:
            where = "AND t.date_sold >= %s AND t.date_sold <= %s"
            params = (start, end)
        cur.execute(f"""
            SELECT c.customer_name, c.customer_phone, c.customer_country_code,
                   c.customer_email, COUNT(DISTINCT t.transaction_group_id) AS occasions
            FROM `transaction` t
            JOIN customer c ON t.customer_id = c.customer_id
            WHERE t.transaction_group_id IS NOT NULL {where}
            GROUP BY c.customer_id
        """, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception:
        return {}
    counts = {}
    for name, phone, code, email, occasions in rows:
        key = _customer_key({
            "Customer Name": name or "",
            "Customer Phone": phone or "",
            "Customer Country Code": code or "",
            "Customer Email": email or "",
        })
        counts[key] = occasions
    return counts

def _mysql_occasion_split(start=None, end=None):
    """Splits purchase occasions into single-item vs multi-item, with an
    occasion count and revenue total for each, from the MySQL warehouse.
    Same occasion definition as _mysql_period_occasion_aov: units grouped by
    transaction_group_id (falling back to transaction_id + 1000000 for
    legacy rows with no group, guaranteed unique since transaction_id never
    reaches that range), so a bulk purchase in one visit is one occasion,
    not several -- answers "what share of revenue comes from customers
    buying just one saree at a time, versus several in the same visit."

    start/end, if both given, restrict to occasions whose date_sold falls in
    that inclusive range, for a period-scoped split. Omit both for a
    lifetime split. Returns {} on any connection failure, in which case the
    caller should omit the table rather than show a broken one."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur = conn.cursor()
        where = ""
        params = ()
        if start and end:
            where = "WHERE date_sold >= %s AND date_sold <= %s"
            params = (start, end)
        cur.execute(f"""
            SELECT
                CASE WHEN units_per_occasion = 1 THEN 'single' ELSE 'multi' END AS occasion_type,
                COUNT(*) AS occasion_count,
                SUM(occ_revenue) AS revenue
            FROM (
                SELECT COUNT(*) AS units_per_occasion,
                       SUM(actual_selling_price_usd) AS occ_revenue
                FROM `transaction`
                {where}
                GROUP BY COALESCE(transaction_group_id, transaction_id + 1000000)
            ) occasions
            GROUP BY occasion_type
        """, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception:
        return {}
    split = {"single": {"occasions": 0, "revenue": 0.0},
             "multi":  {"occasions": 0, "revenue": 0.0}}
    for occasion_type, count, revenue in rows:
        split[occasion_type] = {"occasions": count,
                                 "revenue": float(revenue) if revenue is not None else 0.0}
    return split

def _mysql_period_occasion_aov(start, end):
    """True average order value for a report period: groups units into real
    purchase occasions (same customer, same day, same sales channel) before
    averaging, same fix as the Tableau dashboard's AOV KPI. A bulk purchase
    of several units in one visit is one order, not several -- averaging
    per unit sold (the old approach) understated typical order size.
    Returns None on any connection failure, in which case the caller falls
    back to the simpler per-unit calculation."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            SELECT AVG(occ_revenue) FROM (
                SELECT SUM(actual_selling_price_usd) AS occ_revenue
                FROM `transaction`
                WHERE date_sold >= %s AND date_sold <= %s
                GROUP BY COALESCE(transaction_group_id, transaction_id + 1000000)
            ) occasions
        """, (start, end))
        result = cur.fetchone()[0]
        cur.close()
        conn.close()
        return float(result) if result is not None else None
    except Exception:
        return None

# ── Helpers: formatting ────────────────────────────────────────────────────────
def _usd(v, deduction=False):
    """deduction=True renders non-zero values in accounting-style parentheses
    (e.g. ($17,187.53)) for costs/losses/refunds — zero stays $0.00, never ($0.00),
    since parentheses on zero would imply a negative event that didn't happen."""
    if deduction and v != 0:
        return f"(${abs(v):,.2f})"
    return f"${v:,.2f}"
def _usd_k(v):
    """Abbreviated dollar value for scorecards: $24.3K, $1.2M, $467"""
    # Compare against the value rounded to the SAME precision it would be
    # displayed at in the K branch (one decimal place of thousands) --
    # otherwise a number like $999,975 (technically under a million)
    # still displays as "$1000.0K" once rounded for display, instead of
    # "$1.0M" like every other value that's actually over a million.
    if round(abs(v) / 1_000, 1) >= 1_000: return f"${v/1_000_000:.1f}M"
    if abs(v) >= 1_000:     return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"
def _pct(v):   return f"{v*100:.1f}%"

class _Markup(str):
    """A string that's already safe, pre-built ReportLab markup (e.g. from
    _warn_neg() or _chg_colored()) -- _tbl()'s cell renderer must NOT run
    _xml_escape() on these, unlike raw data values, or the literal
    <font color="..."> tags show up as visible text in the PDF instead of
    being interpreted as color formatting. _xml_escape() exists to protect
    against arbitrary, untrusted text (customer names, notes, etc.) that
    was never meant to contain markup in the first place -- text wrapped
    in _Markup is the opposite case: markup the app deliberately built and
    wants rendered as-is."""
    pass

_TAG_RE = re.compile(r"<[^>]+>")

def _warn_neg(text, is_negative):
    """Rose-colored text for values that are genuinely anomalous when negative
    (e.g. a loss-making category) — the same warning color already used for
    aging/outstanding-balance risk callouts, not applied to routine negatives
    like COGS that are expected to always be negative."""
    return _Markup(f'<font color="{COLOUR["rose"]}">{text}</font>') if is_negative else _Markup(text)
def _chg(c,p):
    """Percent change from p to c. Direction (arrow) is decided from
    c >= p directly, not from the sign of the percentage itself -- the
    percent-change formula flips sign in a misleading way whenever p is
    negative (e.g. a $50 loss improving to a $100 profit computes as
    -300%, which would show a DOWN arrow for what is actually a strong
    turnaround). Using c >= p gets the direction right in every case,
    including a shrinking loss compared against a larger one."""
    if not p: return "—"
    x = (c - p) / p * 100
    up = c >= p
    return f"{'▲' if up else '▼'} {abs(x):.1f}%"
def _chg_colored(c, p):
    """Same text as _chg, wrapped in green/red for PDF table rendering --
    direction/color decided from c >= p, the same source of truth _chg
    itself uses, so the color can never contradict the arrow even when p
    is negative. Only for ReportLab Paragraph contexts -- the <font>
    markup would show up as literal text in a plain-text email."""
    if not p: return _Markup(_chg(c, p))
    up = c >= p
    color = COLOUR["sage"] if up else COLOUR["rose"]
    return _Markup(f'<font color="{color}">{_chg(c, p)}</font>')

# ── Prior period ───────────────────────────────────────────────────────────────
def _prior(period_type, start, end):
    if period_type == "monthly":
        m, y = start.month - 1, start.year
        if m == 0: m, y = 12, y - 1
        ps = date(y, m, 1)
        pe = start - timedelta(days=1)
    elif period_type == "quarterly":
        m, y = start.month - 3, start.year
        if m <= 0: m += 12; y -= 1
        ps = date(y, m, 1)
        pe = start - timedelta(days=1)
    elif period_type == "custom":
        # Same calendar dates one year earlier, not an equal-length
        # trailing window -- this holds the season constant (e.g.
        # Jan-Aug this year vs. Jan-Aug last year), which matters for a
        # seasonal business, rather than comparing against an arbitrary
        # same-length stretch that mixes in an unrelated set of months.
        # Matches the same year-over-year convention Annual reports
        # already use. Feb 29 has no equivalent on a non-leap prior
        # year, so it falls back to Feb 28.
        try:
            ps = start.replace(year=start.year - 1)
        except ValueError:
            ps = date(start.year - 1, 2, 28)
        try:
            pe = end.replace(year=end.year - 1)
        except ValueError:
            pe = date(end.year - 1, 2, 28)
    elif period_type == "annual":
        ps = date(start.year - 1, 1, 1)
        pe = date(start.year - 1, 12, 31)
    else:
        # Explicit on purpose -- an implicit "else" here is exactly the
        # bug class that let "custom" silently fall through to the
        # monthly rule for months before that was caught. A genuinely
        # unrecognized period_type should fail loudly, not silently
        # produce plausible-looking but wrong comparison numbers.
        raise ValueError(f"_prior(): unrecognized period_type {period_type!r}")
    return ps, pe

# ── Sheet connection ───────────────────────────────────────────────────────────
def _connect(mode):
    creds  = ServiceAccountCredentials.from_json_keyfile_name(CREDS_PATH, SCOPE)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_IDS[mode]).worksheet(WORKSHEET_NAME)

# ── Data loading ───────────────────────────────────────────────────────────────
def _find_cancel_line(txn, start, end):
    """Return the specific CANCEL note line whose date falls within
    [start, end], or None. Transaction Notes now preserves full history
    (sale/cancel notes are appended, not overwritten), so a unit that was
    sold, cancelled, resold, and cancelled again can have more than one
    "[MM-DD-YYYY - CANCEL]" tag in its notes. Scanning line by line and
    checking each one's own date -- rather than taking the first CANCEL
    tag found anywhere in the blob -- finds the cancellation actually
    relevant to this report's period instead of a possibly older,
    unrelated one.
    """
    for line in (txn or "").split("\n"):
        m = re.search(r'\[(\d{2}-\d{2}-\d{4})\s*·\s*CANCEL\]', line)
        if not m:
            continue
        d = _parse_date(m.group(1))
        if d and _in(d, start, end):
            return line
    return None

def _load(start, end, ps, pe, mode):
    ws   = _connect(mode)
    rows = ws.get_all_records()

    period_sales, prior_sales, period_added, cancelled, outstanding = [], [], [], [], []

    for r in rows:
        status = r.get("Status","").strip()
        if status == "Unassigned":
            continue

        ds  = _parse_date(r.get("Date Sold",""))
        da  = _parse_date(r.get("Date Acquired",""))
        txn = r.get("Transaction Notes","")

        # CANCEL line relevant to this report's period, if any
        cancel_line = _find_cancel_line(txn, start, end)

        if ds and _in(ds, start, end):   period_sales.append(r)
        if ds and _in(ds, ps, pe):        prior_sales.append(r)
        if da and _in(da, start, end) and status != "Unassigned": period_added.append(r)
        if cancel_line:                   cancelled.append(r)
        if status == "Sold - Partial Payment": outstanding.append(r)

    return dict(all=rows, sales=period_sales, prior=prior_sales,
                added=period_added, cancelled=cancelled, outstanding=outstanding)

# ── Payment method from notes ──────────────────────────────────────────────────
def _method(txn):
    m = re.search(r'received via ([^\.]+)\.', txn or "")
    return m.group(1).strip() if m else "Unknown"

def _cancelled_sale_amount(r, start, end):
    """The revenue actually lost to a cancellation is whatever the sale
    transacted for before it was reversed -- but cancel_sale() resets
    "Actual Selling Price (USD)" back to the unit's normal list price for
    resale (and blanks "Discount %"), so neither column reflects the
    cancelled sale's real price by the time a report runs. The
    pre-cancellation price is still recoverable from the audit note left
    in Transaction Notes (e.g. "...for $475.00.").

    Uses _find_cancel_line() to pull the price from the SPECIFIC
    cancellation relevant to this report's period, not just the first
    CANCEL entry anywhere in a unit's notes -- a unit cancelled more than
    once (sold, cancelled, resold, cancelled again) would otherwise report
    a stale price from an unrelated earlier cancellation.

    The recovered price is trusted directly rather than sanity-checked
    against the current Selling Price (USD) -- that column can change
    after the cancellation (e.g. reprice_unit() marking the unit down as
    dead stock, which only requires "Available" status, exactly what a
    cancelled unit reverts to), which would wrongly invalidate a correct
    historical price. The note text itself is safe to trust unconditionally:
    it's only ever written by cancel_sale() in one fixed format, so there's
    no realistic way for the "for $X.XX." match to be anything else.
    """
    cancel_line = _find_cancel_line(r.get("Transaction Notes", "") or "", start, end)
    if cancel_line:
        m = re.search(r'for \$([0-9,]+\.\d{2})\.', cancel_line)
        if m:
            return _flt(m.group(1))
    return _flt(r.get("Actual Selling Price (USD)"))

# ── Metrics ────────────────────────────────────────────────────────────────────
def _metrics(data, start, end, ps, pe, period_type):
    sales = data["sales"];  prior = data["prior"]
    all_r = data["all"];    today = date.today()

    # Revenue & profit
    rev   = sum(_flt(r.get("Actual Selling Price (USD)")) for r in sales)
    cost  = sum(_flt(r.get("Total Cost (USD)"))           for r in sales)
    gp    = sum(_flt(r.get("Gross Profit (USD)"))         for r in sales)
    margin= gp / rev if rev else 0
    p_rev = sum(_flt(r.get("Actual Selling Price (USD)")) for r in prior)
    p_gp  = sum(_flt(r.get("Gross Profit (USD)"))         for r in prior)
    p_margin = p_gp / p_rev if p_rev else 0
    cancel_lost = sum(_cancelled_sale_amount(r, start, end) for r in data["cancelled"])

    # Sales activity
    units     = len(sales)
    # True average order value groups units into real purchase occasions
    # (same customer, same day, same sales channel) before averaging --
    # otherwise a bulk purchase of several units in one visit is counted as
    # several separate "orders" instead of one, the same distortion fixed on
    # the Tableau dashboard's own AOV KPI. Falls back to the simpler
    # per-unit figure only if the MySQL warehouse is unreachable.
    _occasion_aov = _mysql_period_occasion_aov(start, end)
    if _occasion_aov is not None:
        avg_price = _occasion_aov
    else:
        print("Warning: could not reach the MySQL warehouse -- Avg Order Value is "
              "using the per-unit fallback instead of real purchase occasions.")
        avg_price = rev / units if units else 0
    pay_methods = defaultdict(lambda: {"count":0, "revenue":0.0})
    for r in sales:
        met = _method(r.get("Transaction Notes",""))
        pay_methods[met]["count"]   += 1
        pay_methods[met]["revenue"] += _flt(r.get("Actual Selling Price (USD)"))

    # Discounts
    disc_rows = [r for r in sales if _flt(r.get("Discount %")) > 0]
    disc_lost = sum(_flt(r.get("Selling Price (USD)")) - _flt(r.get("Actual Selling Price (USD)"))
                    for r in disc_rows)
    avg_disc  = (sum(_flt(r.get("Discount %")) for r in disc_rows) / len(disc_rows)
                 if disc_rows else 0)

    # Channels
    channels = defaultdict(lambda: {"count":0,"revenue":0.0})
    for r in sales:
        ch = r.get("Sales Channel","").strip() or "Unknown"
        channels[ch]["count"]   += 1
        channels[ch]["revenue"] += _flt(r.get("Actual Selling Price (USD)"))

    # Customer — period. Keyed by _customer_key() (phone/email identity,
    # not the raw name string) so two different real customers who share a
    # name are never merged into one line -- "name" is carried as a value
    # field for display, since the dict key is no longer display-ready.
    cust_p = defaultdict(lambda: {"units":0,"spend":0.0,"name":""})
    for r in sales:
        n = r.get("Customer Name","").strip()
        if n:
            key = _customer_key(r)
            cust_p[key]["units"] += 1
            cust_p[key]["spend"] += _flt(r.get("Actual Selling Price (USD)"))
            cust_p[key]["name"] = n

    # Customer — lifetime
    all_sold = [r for r in all_r
                if r.get("Status","").strip() in ("Sold","Sold - Partial Payment")]
    cust_life = defaultdict(lambda: {"units":0,"spend":0.0,"cats":defaultdict(int),"name":"",
                                      "outstanding":0.0,"last_purchase":None,"dates":set()})

    # Sales Consistency (Swing): mirrors the dashboard's own "Swing" field
    # (best month minus worst month, relative to a typical month, over a
    # trailing window) but anchors the window to the report's own period
    # instead of always ending "today" -- and scales the window to match
    # how far back this report type already reasons: Monthly keeps the
    # dashboard's native 6-month window (a single month has no volatility
    # story of its own), Quarterly and Annual each use their own period
    # length (3 and 12 months), so "current" is simply the requested
    # quarter/year and "prior" is the one immediately before it. Custom
    # ranges have no natural cadence to anchor a window to, so this is
    # skipped entirely rather than forcing an arbitrary window onto it.
    swing_window_months = {"monthly": 6, "quarterly": 3, "annual": 12}.get(period_type)
    if swing_window_months:
        def _month_units(y, mo):
            return sum(1 for r in all_sold
                       if (d := _parse_date(r.get("Date Sold", ""))) is not None
                       and d.year == y and d.month == mo)
        def _window(months_back):
            y, mo = end.year, end.month - months_back
            while mo <= 0:
                mo += 12; y -= 1
            vals = []
            for _ in range(swing_window_months):
                vals.append(_month_units(y, mo))
                mo -= 1
                if mo == 0:
                    mo, y = 12, y - 1
            return vals
        _cur_counts = _window(0)
        _pri_counts = _window(swing_window_months)
        _cur_avg = sum(_cur_counts) / swing_window_months
        _pri_avg = sum(_pri_counts) / swing_window_months
        swing_current = max(_cur_counts) - min(_cur_counts)
        swing_prior   = max(_pri_counts) - min(_pri_counts)
        swing_current_rel = swing_current / _cur_avg if _cur_avg else None
        swing_prior_rel   = swing_prior   / _pri_avg if _pri_avg else None
        swing_change = (swing_current_rel - swing_prior_rel
                         if swing_current_rel is not None and swing_prior_rel is not None
                         else None)
    else:
        swing_current_rel = swing_prior_rel = swing_change = None
    for r in all_sold:
        n = r.get("Customer Name","").strip()
        if n:
            key = _customer_key(r)
            cust_life[key]["units"] += 1
            cust_life[key]["spend"] += _flt(r.get("Actual Selling Price (USD)"))
            cust_life[key]["name"] = n
            cust_life[key]["outstanding"] += _flt(r.get("Amount Outstanding (USD)"))
            cat = r.get("Weave Type / Cluster","").strip()
            if cat: cust_life[key]["cats"][cat] += 1
            d_sold = _parse_date(r.get("Date Sold",""))
            if d_sold and (cust_life[key]["last_purchase"] is None
                            or d_sold > cust_life[key]["last_purchase"]):
                cust_life[key]["last_purchase"] = d_sold
            if d_sold:
                cust_life[key]["dates"].add(d_sold)

    # Region — lifetime (mirrors the dashboard's Geography Segment: domestic
    # sales break out by state, international sales break out by country)
    regions = defaultdict(lambda: {"units":0,"revenue":0.0})
    for r in all_sold:
        country = r.get("Customer Country","").strip()
        state   = r.get("Customer State","").strip()
        region  = state if country == "United States" and state else (country or "Unknown")
        regions[region]["units"]   += 1
        regions[region]["revenue"] += _flt(r.get("Actual Selling Price (USD)"))

    prior_names = {_customer_key(r) for r in all_r
                   if _parse_date(r.get("Date Sold","")) and
                      _parse_date(r.get("Date Sold","")) < start and
                      r.get("Customer Name","").strip()}
    period_names = set(cust_p.keys())
    new_c  = period_names - prior_names
    ret_c  = period_names & prior_names
    ret_rate = len(ret_c) / len(period_names) * 100 if period_names else 0

    # Customer concentration — top 5 lifetime spenders' share of lifetime
    # revenue. A small, young boutique depending heavily on a handful of
    # repeat buyers carries real concentration risk (losing one is a material
    # hit), which the Lifetime Customer table's sort-by-spend doesn't itself
    # surface as a number.
    life_rev_total = sum(d["spend"] for d in cust_life.values())
    top5_spend = sum(d["spend"] for _, d in
                      sorted(cust_life.items(), key=lambda x: x[1]["spend"], reverse=True)[:5])
    top5_share = top5_spend / life_rev_total * 100 if life_rev_total else 0

    # Lifetime Loyalty -- mirrors the dashboard's own "Repeat Customers" KPI
    # (share of named customers who've purchased on 2+ distinct occasions).
    # transaction_group_id (MySQL warehouse only) is the authoritative source:
    # it links units bought by the same customer, on the same day, through the
    # same sales channel, into one shared value, so a single bulk purchase
    # correctly counts as one occasion, not several. Falls back to a Distinct
    # Date Sold proxy only if the warehouse is unreachable when this runs --
    # a same-day purchase is *usually* one visit, but this proxy is exactly
    # what was replaced on the Tableau dashboard for undercounting genuine
    # returns, so it's a degraded fallback, not the primary calculation.
    total_named_customers = len(cust_life)
    mysql_occasions = _mysql_occasion_counts()
    if mysql_occasions:
        loyal_customers = sum(1 for key in cust_life if mysql_occasions.get(key, 0) >= 2)
    else:
        print("Warning: could not reach the MySQL warehouse -- Lifetime Loyalty is "
              "using the same-day-purchase proxy instead of real purchase occasions.")
        loyal_customers = sum(1 for d in cust_life.values() if len(d["dates"]) >= 2)
    lifetime_loyalty = (loyal_customers / total_named_customers * 100
                         if total_named_customers else 0)

    # Per-customer Avg Order Value (both the period-scoped Customer table and
    # the lifetime Customer Breakdown table) has the same units-vs-occasions
    # fix applied -- reuse mysql_occasions (already fetched above) for the
    # lifetime table, and fetch a period-scoped equivalent for the period one.
    period_customer_occasions = _mysql_occasion_counts(start, end)

    # Single vs. multi-item occasion split -- what share of revenue comes
    # from customers buying just one saree at a time versus several in the
    # same visit. Both scopes computed here (period + lifetime) so the
    # report section built from these can show one table with both rows
    # instead of two separate tables.
    period_occasion_split   = _mysql_occasion_split(start, end)
    lifetime_occasion_split = _mysql_occasion_split()

    # Category — this period
    cats = defaultdict(lambda: {"units":0,"revenue":0.0,"gp":0.0})
    for r in sales:
        c = r.get("Weave Type / Cluster","").strip() or "Unknown"
        cats[c]["units"]   += 1
        cats[c]["revenue"] += _flt(r.get("Actual Selling Price (USD)"))
        cats[c]["gp"]      += _flt(r.get("Gross Profit (USD)"))

    # Category — lifetime (all-time sold/sold-partial, not just this period)
    cats_life = defaultdict(lambda: {"units":0,"revenue":0.0,"gp":0.0})
    for r in all_sold:
        c = r.get("Weave Type / Cluster","").strip() or "Unknown"
        cats_life[c]["units"]   += 1
        cats_life[c]["revenue"] += _flt(r.get("Actual Selling Price (USD)"))
        cats_life[c]["gp"]      += _flt(r.get("Gross Profit (USD)"))
    # Sell-Through Rate denominator: every unit ever acquired in this weave
    # type, any status, mirrors the dashboard's Collection Sell-Through Rate
    # chart exactly -- Unassigned excluded, same reasoning as suppliers
    # (placeholder SKUs with no physical unit behind them yet). Kept as its
    # own plain dict, NOT folded into cats_life -- cats_life is a
    # defaultdict, so looking a category up here would silently create a
    # zero-revenue entry for anything acquired but never sold, padding the
    # Lifetime Category Performance table with categories it was never
    # meant to show (caught via a real count mismatch against the master
    # sheet's own Category Code Reference tab).
    cat_acquired = defaultdict(int)
    for r in all_r:
        if r.get("Status","").strip() != "Unassigned":
            c = r.get("Weave Type / Cluster","").strip() or "Unknown"
            cat_acquired[c] += 1

    # Supplier performance — mostly lifetime by design: with ~52 units sold in
    # a typical month spread across 18 suppliers, a per-period sell-through
    # rate per supplier would be based on samples too thin to be meaningful.
    # Available Units is a real-time snapshot; Units Sold This Period is a raw
    # count (fine even when small); everything else is lifetime.
    suppliers = defaultdict(lambda: {
        "available": 0, "period_units": 0, "life_acquired": 0,
        "life_units": 0, "life_revenue": 0.0, "life_gp": 0.0,
        "dts_sum": 0.0, "dts_count": 0,
    })
    for r in all_r:
        status = r.get("Status","").strip()
        if status == "Available":
            sup = r.get("Supplier","").strip() or "Unknown"
            suppliers[sup]["available"] += 1
        # Every unit ever acquired from this supplier, any status, is the
        # Sell-Through Rate denominator (mirrors the dashboard's Units
        # Acquired field) -- Unassigned excluded, since those are
        # placeholder SKUs with no physical unit behind them yet, not
        # something actually sourced from a supplier.
        if status != "Unassigned":
            sup = r.get("Supplier","").strip() or "Unknown"
            suppliers[sup]["life_acquired"] += 1
    for r in sales:
        sup = r.get("Supplier","").strip() or "Unknown"
        suppliers[sup]["period_units"] += 1
    for r in all_sold:
        sup = r.get("Supplier","").strip() or "Unknown"
        suppliers[sup]["life_units"]   += 1
        suppliers[sup]["life_revenue"] += _flt(r.get("Actual Selling Price (USD)"))
        suppliers[sup]["life_gp"]      += _flt(r.get("Gross Profit (USD)"))
        dts = _flt(r.get("Days to Sell"))
        if dts > 0:
            suppliers[sup]["dts_sum"]   += dts
            suppliers[sup]["dts_count"] += 1
    total_dts_units = sum(1 for r in all_sold if _flt(r.get("Days to Sell")) > 0)

    # Inventory flow
    active = {"Available","Reserved","Sold","Sold - Partial Payment"}
    # A unit counts as on-hand at `start` when status alone already says so
    # (Available/Reserved -- unambiguous, no date needed), or when it's
    # currently Sold/Partial but has an actual recorded sale on or after
    # `start` (sold during or after this period, so it was still on hand
    # going in). Never inferred from a *missing* Date Sold: some legacy
    # rows are marked Sold with no sale date ever recorded, and treating a
    # blank date as "hasn't sold yet" silently counted units that had
    # already sold as still-on-hand inventory (caught via a real ~250-unit
    # gap between this figure and a live headcount).
    opening = [r for r in all_r
               if r.get("Status","").strip() in active
               and _parse_date(r.get("Date Acquired",""))
               and _parse_date(r.get("Date Acquired","")) < start
               and (r.get("Status","").strip() in ("Available", "Reserved")
                    or (_parse_date(r.get("Date Sold",""))
                        and _parse_date(r.get("Date Sold","")) >= start))]
    op_cnt  = len(opening)
    add_cnt = len(data["added"])
    can_cnt = len(data["cancelled"])
    # can_cnt is deliberately NOT added here. A cancelled sale's Date Sold
    # gets blanked, which already makes it flow back into op_cnt (if it
    # existed before this period) or add_cnt (if it was acquired during
    # this period) -- adding can_cnt again would double-count the same
    # physical unit a second time. It's still surfaced in the Inventory
    # Flow table below for visibility, just not part of the arithmetic.
    cl_cnt  = op_cnt + add_cnt - units

    # Sell-through & avg days
    sell_through = units / op_cnt if op_cnt else 0
    dts_vals     = [_flt(r.get("Days to Sell")) for r in sales
                    if _flt(r.get("Days to Sell")) > 0]
    avg_dts      = sum(dts_vals) / len(dts_vals) if dts_vals else 0

    # Inventory turnover — units-based (units sold ÷ average units on hand),
    # not value-based (COGS ÷ avg inventory $), since a value-based version
    # would need a closing-inventory-cost figure we don't compute anywhere
    # yet. Uses opening/closing stock already derived above.
    avg_inv  = (op_cnt + cl_cnt) / 2
    turnover = units / avg_inv if avg_inv else 0

    # Status snapshot — always include every valid status, even at zero, so a
    # status disappearing from the table can't be confused with it not being
    # tracked at all.
    snap = defaultdict(int, {s: 0 for s in
        ("Available", "Reserved", "Sold", "Sold - Partial Payment", "Unassigned")})
    for r in all_r:
        s = r.get("Status","").strip()
        if s: snap[s] += 1

    # Aging (Available only). The 180+ bucket mirrors the sheet's own Dead
    # Stock Flag formula exactly -- AND(Status="Available", days>=180,
    # Selling Price (USD)<>"", Total Cost (USD)<>"") -- so this report's
    # "180+ days" count matches what filtering the sheet on Dead Stock
    # actually returns, instead of drifting from it. The boundary is
    # >=180 (not >180) and it additionally requires both price columns to
    # be populated, since the sheet won't flag a unit as Dead Stock while
    # either is blank even if it's aged past 180 days.
    aging = {"0–30":[], "31–60":[], "61–90":[], "91–180":[], "180+": []}
    for r in all_r:
        if r.get("Status","").strip() != "Available": continue
        da = _parse_date(r.get("Date Acquired",""))
        d = _days_since(da, reference=today)
        if d is None: continue
        is_dead_stock = (d >= 180
                          and str(r.get("Selling Price (USD)","")).strip() != ""
                          and str(r.get("Total Cost (USD)","")).strip() != "")
        if   d <= 30:     aging["0–30"].append(r)
        elif d <= 60:     aging["31–60"].append(r)
        elif d <= 90:     aging["61–90"].append(r)
        elif is_dead_stock: aging["180+"].append(r)
        else:             aging["91–180"].append(r)

    # Outstanding
    total_out  = sum(_flt(r.get("Amount Outstanding (USD)")) for r in data["outstanding"])
    out_days   = [_days_since(_parse_date(r.get("Date Sold","")), reference=today)
                  for r in data["outstanding"]]
    out_days   = [d for d in out_days if d is not None]
    avg_collect_days = sum(out_days) / len(out_days) if out_days else 0

    # Unsold inventory: Available + Reserved -- a reservation isn't a
    # guaranteed sale, so that capital is still genuinely at risk the same
    # way Available stock is. Unassigned is deliberately excluded: those
    # SKUs have no physical unit or cost data behind them yet (see
    # ARCHITECTURE.md's note on why that status exists).
    unsold_rows   = [r for r in all_r if r.get("Status","").strip() in ("Available","Reserved")]
    unsold_cnt    = len(unsold_rows)
    cash_exposure = sum(_flt(r.get("Total Cost (USD)")) for r in unsold_rows)

    # Months of Inventory on Hand: a plain-language companion to Inventory
    # Turnover, answering "how long would it take to sell through what's
    # sitting right now" instead of an abstract multiplier. Deliberately
    # uses unsold_cnt (a live, as-of-today count), not op_cnt/cl_cnt (a
    # historical reconstruction of stock at the period's boundaries) --
    # the whole point is "what's on the shelf right now," not a snapshot
    # from whenever the period started. Units sold is normalized to a
    # monthly-equivalent rate via the period's actual day span (not a
    # hardcoded 1/3/12 per period_type) so this stays directly comparable
    # across monthly, quarterly, annual, and custom reports alike, unlike
    # Turnover which needs a same-period-type caveat.
    period_days       = (end - start).days + 1
    monthly_sold_rate = units / (period_days / 30.44) if period_days else 0
    months_on_hand     = unsold_cnt / monthly_sold_rate if monthly_sold_rate else None

    # Units selling below cost: the same unsold population, filtered further
    # to units whose listed price is under what they cost to acquire. Cash
    # exposure here is cost basis, same definition as above, not the
    # shortfall -- it's capital at risk, not a confirmed loss, that only
    # becomes a real loss if a unit actually sells at its current price.
    below_cost_rows = [r for r in unsold_rows
                        if _flt(r.get("Selling Price (USD)")) < _flt(r.get("Total Cost (USD)"))]
    below_cost_cnt      = len(below_cost_rows)
    below_cost_exposure = sum(_flt(r.get("Total Cost (USD)")) for r in below_cost_rows)

    return dict(
        rev=rev, cost=cost, gp=gp, margin=margin,
        p_rev=p_rev, p_gp=p_gp, p_margin=p_margin, p_units=len(prior),
        cancel_lost=cancel_lost, cancel_cnt=can_cnt,
        units=units, avg_price=avg_price,
        pay_methods=dict(pay_methods),
        disc_rows=disc_rows, disc_lost=disc_lost, avg_disc=avg_disc,
        channels=dict(channels),
        cust_p=dict(cust_p), cust_life=dict(cust_life),
        period_customer_occasions=period_customer_occasions,
        lifetime_customer_occasions=mysql_occasions,
        period_occasion_split=period_occasion_split,
        lifetime_occasion_split=lifetime_occasion_split,
        total_lifetime_sold=len(all_sold),
        total_lifetime_revenue=sum(_flt(r.get("Actual Selling Price (USD)")) for r in all_sold),
        new_c=new_c, ret_c=ret_c, ret_rate=ret_rate, top5_share=top5_share,
        lifetime_loyalty=lifetime_loyalty,
        cats=dict(cats), cats_life=dict(cats_life), cat_acquired=dict(cat_acquired),
        suppliers=dict(suppliers), total_dts_units=total_dts_units,
        op_cnt=op_cnt, add_cnt=add_cnt, cl_cnt=cl_cnt,
        sell_through=sell_through, avg_dts=avg_dts, turnover=turnover,
        snap=dict(snap),
        aging=aging,
        outstanding=data["outstanding"], total_out=total_out, avg_collect_days=avg_collect_days,
        unsold_cnt=unsold_cnt, cash_exposure=cash_exposure, months_on_hand=months_on_hand,
        below_cost_rows=below_cost_rows, below_cost_cnt=below_cost_cnt,
        below_cost_exposure=below_cost_exposure,
        regions=dict(regions),
        swing_window_months=swing_window_months,
        swing_current_rel=swing_current_rel, swing_prior_rel=swing_prior_rel,
        swing_change=swing_change,
    )

# ── PDF building helpers ───────────────────────────────────────────────────────
class _Anchor(Flowable):
    """Zero-size flowable that registers a named jump target at wherever it
    lands on the page, so a cross-reference elsewhere in the report (e.g.
    "see Aging Inventory below") can be a real clickable link instead of
    just text pointing somewhere the reader has to go find manually.
    Platypus doesn't know page numbers in advance since content reflows,
    so the target has to register itself at draw time rather than being
    wired up when the story is first assembled."""
    def __init__(self, key):
        Flowable.__init__(self)
        self.key = key
        self.width = self.height = 0
    def draw(self):
        self.canv.bookmarkPage(self.key)

def _sec(title, anchor=None):
    flow = [
        Spacer(1, 8),
        Paragraph(title.upper(), ST["sec_title"]),
        HRFlowable(width=CONTENT_W, thickness=1.5, color=GOLD, spaceBefore=6, spaceAfter=8),
    ]
    if anchor:
        flow.insert(0, _Anchor(anchor))
    return flow

def _rule(color=GOLD, thick=0.5, sb=4, sa=6):
    return HRFlowable(width=CONTENT_W, thickness=thick, color=color,
                      spaceBefore=sb, spaceAfter=sa)

def _no_data(msg="No data for this period."):
    return Paragraph(msg, ST["note"])

def _callout(text, bg=None):
    bg = bg or ROSE
    t  = Table([[Paragraph(text, ST["alert"])]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), bg),
        ("TOPPADDING",    (0,0),(-1,-1), 8),
        ("BOTTOMPADDING", (0,0),(-1,-1), 8),
        ("LEFTPADDING",   (0,0),(-1,-1), 10),
        ("RIGHTPADDING",  (0,0),(-1,-1), 10),
    ]))
    return t

_METRIC_LABEL_FLOOR = 7.0   # never shrink titles below this size

def _fit_label_style(labels, avail_width, base_style,
                      floor=_METRIC_LABEL_FLOOR, step=0.5):
    """Find the largest font size (down to floor) at which every label in
    this row fits on one line within avail_width — applied uniformly to the
    whole row so all cards in a scorecard stay visually consistent, rather
    than each card picking its own size."""
    size = base_style.fontSize
    while size > floor:
        if all(pdfmetrics.stringWidth(l, base_style.fontName, size) <= avail_width
               for l in labels):
            break
        size -= step
    else:
        size = floor
    return base_style.clone("metric_label_fit", fontSize=size,
                             leading=size * (base_style.leading / base_style.fontSize))

def _metric_row(items, bg=None, value_color=None):
    """items: list of (label, value) — matches Tableau 2-tier scorecard design.

    Labels and values are genuinely separate table rows with fixed heights,
    not a stacked flowable per cell — that's what guarantees every value
    starts at the same shared baseline regardless of whether an individual
    label wraps to one line or two. The label row is bottom-aligned within
    its reserved 2-line-tall area so every label's last line lands on one
    common row right above the values; the value row is top-aligned so
    every value starts immediately below that shared line.

    Titles never wrap: label font size is shrunk (uniformly across the row,
    down to a floor) until every title in this row fits on a single line —
    non-breaking spaces are a backstop against ReportLab wrapping anyway.

    value_color: mark a card's severity through the value text alone,
    keeping the normal off-white card and gold label -- for a tier that
    needs to read as "same severity as the flooded callouts" without
    actually flooding the card, which just competes with a nearby callout
    for the same loudest visual slot on the page instead of reading as a
    distinct kind of information.
    """
    n = len(items)
    w = CONTENT_W / n
    avail = w - 12   # minus LEFTPADDING + RIGHTPADDING
    labels = [label for label, _ in items]
    inverse = bg is not None and bg is not OFF_WHITE
    label_base = ST["metric_label_inverse"] if inverse else ST["metric_label"]
    value_style = ST["metric_val_inverse"] if inverse else ST["metric_val"]
    if value_color is not None and not inverse:
        value_style = value_style.clone("metric_val_accent", textColor=value_color)
    label_style = _fit_label_style(labels, avail, label_base)
    label_row = [Paragraph(label.replace(" ", " "), label_style) for label in labels]
    value_row = [Paragraph(value, value_style) for _, value in items]
    t = Table([label_row, value_row], colWidths=[w]*n, rowHeights=[36, 34])
    t.setStyle(TableStyle([
        ("BOX",           (0,0),(-1,-1), 1.0, DARK_BROWN),
        # Vertical dividers between cards only — spans both rows, no line
        # between the title and value within a card, so each card reads as
        # one unified container rather than two stacked cells.
        ("LINEAFTER",     (0,0),(-2,-1), 0.8, DARK_BROWN),
        ("BACKGROUND",    (0,0),(-1,-1), bg or OFF_WHITE),
        ("ALIGN",         (0,0),(-1,-1), "CENTER"),
        ("LEFTPADDING",   (0,0),(-1,-1), 6),
        ("RIGHTPADDING",  (0,0),(-1,-1), 6),
        ("VALIGN",        (0,0),(-1,0), "BOTTOM"),
        ("TOPPADDING",    (0,0),(-1,0), 6),
        ("BOTTOMPADDING", (0,0),(-1,0), 4),
        ("VALIGN",        (0,1),(-1,1), "TOP"),
        ("TOPPADDING",    (0,1),(-1,1), 4),
        ("BOTTOMPADDING", (0,1),(-1,1), 10),
    ]))
    return t

def _xml_escape(text):
    """Escape &, <, > so arbitrary business data (customer/supplier names,
    SKUs, notes, etc.) can never be misread as ReportLab markup -- e.g. a
    customer named "Smith & Co" would otherwise crash the whole report,
    since ReportLab's Paragraph parser treats those characters as the
    start of a markup tag. Unlike _md_to_rl(), this does no markdown-to-tag
    conversion -- table cells should render exactly the literal text stored
    in the sheet, not have a stray "**" or "<<" in a name reinterpreted as
    formatting.
    """
    text = str(text)
    text = text.replace("&", "&amp;")   # must be first, same reason as _md_to_rl()
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def _tbl(headers, rows, widths, totals=None, right_from=1, compact=False,
         key_rows=None, bold_rows=None, center_cols=None, left_cols=None):
    """
    headers: list of str
    rows: list of lists
    widths: list of pt values summing to CONTENT_W
    totals: optional bold summary row
    right_from: column index from which values are right-aligned
    compact: use 7.5 pt cell font for wide tables
    key_rows: 0-based row indices for calculated results/subtotals within a
        bridge-style table (e.g. Net Revenue, Gross Profit) — bold text AND a
        pastel-yellow highlight. Component/input rows stay plain white.
    bold_rows: 0-based row indices to bold without the yellow highlight — for
        a result worth distinguishing by weight alone, without adding another
        highlighted row (e.g. Gross Margin % sitting right below Gross Profit).
    center_cols: 0-based column indices to center instead of left/right —
        for columns with no digit-place magnitude to compare (dates) or that
        are mostly a placeholder dash rather than a dense column of real
        numbers (e.g. Outstanding Balance). Overrides right_from for those
        specific columns; every other column still follows right_from.
    left_cols: 0-based column indices to force left-aligned regardless of
        right_from — for a text column (a name, a collection, a payment
        method) that sits past the right_from threshold alongside numeric
        columns, where a single threshold can't separate the two.
    """
    sfx = "_sm" if compact else ""
    key_rows    = set(key_rows or [])
    bold_rows   = set(bold_rows or [])
    center_cols = set(center_cols or [])
    left_cols   = set(left_cols or [])

    def _safe(txt):
        # _Markup values are already safe, pre-built ReportLab markup
        # (e.g. from _warn_neg() or _chg_colored()) -- escaping them would
        # turn their intentional <font color="..."> tags into visible
        # literal text instead of actual color. Everything else is
        # untrusted data and gets escaped as before.
        return txt if isinstance(txt, _Markup) else _xml_escape(txt)
    def _align(i):
        if i in center_cols: return "c"
        if i in left_cols: return "l"
        return "r" if i >= right_from else "l"
    def h_para(i, txt):
        return Paragraph(_safe(txt), ST[f"th_{_align(i)}" + sfx])
    def d_para(i, txt, bold=False):
        a = _align(i)
        if bold: return Paragraph(_safe(txt), ST[("td_bc" if a == "c" else f"td_b{a}") + sfx])
        return Paragraph(_safe(txt), ST[f"td_{a}" + sfx])

    data = [[h_para(i, h) for i, h in enumerate(headers)]]
    for idx, row in enumerate(rows):
        bold = idx in key_rows or idx in bold_rows
        data.append([d_para(i, v, bold=bold) for i, v in enumerate(row)])
    if totals:
        data.append([d_para(i, v, bold=True) for i, v in enumerate(totals)])

    n_rows = len(data)
    cmds   = [
        ("BACKGROUND",    (0,0),  (-1,0),   SAGE),
        ("LINEABOVE",     (0,0),  (-1,0),   0.5, SAGE),
        ("LINEBELOW",     (0,-1), (-1,-1),  1.0, SAGE),
        ("GRID",          (0,0),  (-1,-1),  0.4, LIGHT_GREY),
        ("TOPPADDING",    (0,0),  (-1,-1),  4),
        ("BOTTOMPADDING", (0,0),  (-1,-1),  4),
        ("LEFTPADDING",   (0,0),  (-1,-1),  6),
        ("RIGHTPADDING",  (0,0),  (-1,-1),  6),
    ]
    # Alternating row backgrounds
    for i in range(1, n_rows - (1 if totals else 0)):
        bg = white if i % 2 == 1 else OFF_WHITE
        cmds.append(("BACKGROUND", (0,i), (-1,i), bg))
    # Key financial rows: warm gold tint to distinguish from regular body rows
    for idx in key_rows:
        actual_row = idx + 1  # +1 for header
        cmds.append(("BACKGROUND", (0, actual_row), (-1, actual_row), HexColor("#FDF5E6")))
    if totals:
        cmds += [
            ("BACKGROUND", (0, n_rows-1), (-1, n_rows-1), OFF_WHITE),
            ("LINEABOVE",  (0, n_rows-1), (-1, n_rows-1), 1.0, GOLD),
        ]

    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle(cmds))
    return t

def _stack_height(flowables):
    """Total vertical space a sequence of flowables consumes when placed
    back-to-back in a Frame — not just the sum of their own wrap() heights.

    ReportLab applies spaceBefore/spaceAfter at the Frame level (Frame._add),
    collapsing adjacent margins (like CSS: the gap between two flowables is
    max(prev.spaceAfter, cur.spaceBefore), not their sum) rather than folding
    them into wrap()'s return value. Summing raw wrap() heights alone ignores
    this entirely, which understates the true height enough to make a
    CondPageBreak reservation fall short (a trailing note computed as "fits"
    can still overflow to the next page). prev_sa starts at 0, i.e. we assume
    nothing collapses into the first flowable's spaceBefore — the real prior
    content might absorb some of it, so this errs toward reserving slightly
    more space, never less.
    """
    total, prev_sa = 0.0, 0.0
    for f in flowables:
        sb = f.getSpaceBefore()
        _, h = f.wrap(CONTENT_W, 2000)
        total += max(sb - prev_sa, 0) + h
        prev_sa = f.getSpaceAfter()
        total += prev_sa
    return total

def _sec_table(heading_flowables, headers, rows, widths, totals=None,
               right_from=1, compact=False, key_rows=None, bold_rows=None,
               min_rows=3, trailing=None, center_cols=None, left_cols=None):
    """
    A heading (section title, sub-heading, callout — whatever flowables should
    introduce this table) plus a table, guaranteed to never split apart with
    zero rows visible under the heading.

    Unlike wrapping the whole table in KeepTogether — which forces the ENTIRE
    table onto a fresh page even when it's only slightly too tall for the
    remaining space, wasting whatever space was left on the page before it —
    this only guarantees the heading plus a small preview (min_rows) stay
    together. If there's room for that much, the heading is placed and the
    full table flows from there, splitting across pages normally (repeating
    its header row) like any other table. Only if there isn't even room for
    the heading + min_rows does the whole block move to a fresh page.

    trailing: flowables (e.g. a caption/note) to render right after the table,
    whose height is folded into the same up-front reservation as the heading
    preview — so a short trailing note can't land alone on the next page while
    the table it describes stays on the current one. Only pass this alongside
    a min_rows that covers the table's full, structurally-bounded row count
    (e.g. min_rows=len(rows) for a table that's always small) — never for a
    table that's expected to paginate, since reserving its full height would
    either waste a page's worth of space or, if the table can legitimately
    exceed one page, force a page break that can never be satisfied.
    """
    key_rows  = key_rows or set()
    bold_rows = bold_rows or set()
    trailing  = trailing or []
    preview_n = min(min_rows, len(rows))
    full_preview = preview_n >= len(rows)
    preview = _tbl(headers, rows[:preview_n], widths, right_from=right_from, compact=compact,
                    key_rows={i for i in key_rows if i < preview_n},
                    bold_rows={i for i in bold_rows if i < preview_n},
                    totals=totals if full_preview else None, center_cols=center_cols,
                    left_cols=left_cols)
    reserve_h = _stack_height([*heading_flowables, preview, *trailing])

    table = _tbl(headers, rows, widths, totals=totals, right_from=right_from,
                 compact=compact, key_rows=key_rows, bold_rows=bold_rows,
                 center_cols=center_cols, left_cols=left_cols)

    return [CondPageBreak(reserve_h), *heading_flowables, table, *trailing]

def _smart_widths(headers, rows, protect, totals=None):
    """Column widths for a data-driven table where `protect` (a set of
    header names) always keeps its full natural, unwrapped width — a
    person's name or a currency/date value that reads as broken if split
    across two lines. Every other column shares whatever space remains,
    scaling down together only if that's not enough to hold their own
    natural widths, rather than the old approach of shrinking every column
    by the same proportional factor regardless of what it holds (which is
    exactly what let a currency figure like "$33,235.00" wrap mid-number
    once enough columns were on the page).

    totals: the same totals row that will be passed to _sec_table()/_tbl(),
    if any. A summed total is very often the widest value in its column
    (e.g. "$1,291.49" vs. every individual row's "$340.71"), so it has to
    feed the same natural-width measurement as the body rows -- otherwise
    the total is exactly the value that ends up wrapping, since it was
    never accounted for when sizing the column.
    """
    HEADER_FONT, HEADER_SIZE = "Montserrat-Bold", 7
    BODY_FONT,   BODY_SIZE   = "Montserrat-Bold", 7.5
    PAD = 12
    # stringWidth() measures raw glyph advances; Paragraph's actual layout in
    # a table cell runs slightly wider than that sum in practice, so a
    # column sized to the exact predicted width can still wrap by a
    # character or two. A flat 6% headroom absorbs that gap.
    SAFETY = 1.06
    # Body content drives width, not the full header text -- headers are
    # allowed to wrap across lines, so reserving space for one like
    # "Outstanding Balance" to fit on one line inflated several columns'
    # width well past what the data itself needs (that's what caused
    # "$33,235.00" to wrap mid-number: it pushed the protected total over
    # the page width and silently triggered the uniform-scale fallback
    # below, defeating protection even for columns with room to spare).
    # But a header still can't wrap *inside* a word ("Units" -> "Un"/"its"),
    # so each column is floored at its single longest header word, not the
    # header's full width -- narrow enough to leave room for the data,
    # wide enough that a header can only break between words, never within one.
    # _warn_neg()/_chg_colored() wrap flagged values in literal
    # <font color="...">...</font> markup (a _Markup string) so ReportLab
    # renders them in color -- but that markup is invisible ink as far as
    # column width goes. Measuring str(v) directly on one of these values
    # counts the tag characters themselves as glyphs, wildly overstating
    # the natural width of any column that has even one flagged (e.g.
    # negative-margin) row, which then starves every other column once
    # the flex scale factor kicks in. Strip tags before measuring so
    # width reflects only what actually prints.
    def _visible(v):
        return _TAG_RE.sub("", str(v)) if isinstance(v, _Markup) else str(v)

    protect_idx = {i for i, h in enumerate(headers) if h in protect}

    all_rows = list(rows) + ([totals] if totals else [])
    cols = list(zip(*all_rows)) if all_rows else [[] for _ in headers]
    # Full-body width (never wraps within a value) always feeds full_floor,
    # protected or not -- that's the zero-compromise ideal every column
    # starts from. word_floor is where the two kinds of column diverge: a
    # protected column's body must never wrap (a name or currency figure
    # broken across lines reads as an error), so its word_floor is anchored
    # to that same full-body width. A flex column's body is allowed to wrap
    # the same way its header already can -- a long collection name like
    # "Krishnamoorthy Kanjivaram Silk" reads fine split across two lines --
    # so its word_floor only needs its single longest word, not the whole
    # phrase. Without this distinction the one flex column in a dense table
    # (the one place meant to absorb shrinkage) carried a floor so wide it
    # left every protected numeric/currency column short of its own
    # minimum, which is exactly what forced "Units", "Outstanding Balance",
    # and "Most Purchased %" below their word floors even after those
    # floors were made theoretically inviolable.
    body_full_max = [
        max((pdfmetrics.stringWidth(_visible(v), BODY_FONT, BODY_SIZE) for v in cols[i]), default=0)
        for i in range(len(headers))
    ]
    body_word_max = [
        body_full_max[i] if i in protect_idx else
        max((pdfmetrics.stringWidth(word, BODY_FONT, BODY_SIZE)
             for v in cols[i] for word in _visible(v).split()), default=0)
        for i in range(len(headers))
    ]
    # Two floors per column: word_floor never wraps *inside* a word (the
    # hard invariant -- "Units" must never become "Un"/"its"), full_floor
    # is the zero-wrap ideal (the whole header phrase, and the whole body
    # value for protected columns, on one line). Most tables have enough
    # page width to just give every column its full_floor outright; only
    # when they don't does a column need to give back space, and even then
    # only down to its own word_floor, never further -- a single-word
    # header on a protected column (word_floor == full_floor, no slack to
    # give) is untouched either way.
    word_floor = [
        max(body_word_max[i], max((pdfmetrics.stringWidth(w, HEADER_FONT, HEADER_SIZE)
                                    for w in h.split()), default=0)) * SAFETY + PAD
        for i, h in enumerate(headers)
    ]
    full_floor = [
        max(body_full_max[i], pdfmetrics.stringWidth(h, HEADER_FONT, HEADER_SIZE)) * SAFETY + PAD
        for i, h in enumerate(headers)
    ]
    flex_idx = [i for i in range(len(headers)) if i not in protect_idx]
    # Protected columns always get their zero-wrap ideal -- that's what
    # protection means. Everything else negotiates for whatever's left.
    protected_total = sum(full_floor[i] for i in protect_idx)
    remaining = CONTENT_W - protected_total
    flex_ideal_total = sum(full_floor[i] for i in flex_idx)
    flex_floor_total = sum(word_floor[i] for i in flex_idx)

    if not flex_idx or remaining <= 0 or flex_floor_total <= 0:
        # No flex columns, or the protected columns alone exceed the page.
        # Every column (protected ones included) gives back space here, but
        # never below its own word_floor -- naively scaling full_floor by a
        # flat factor, as this branch used to, ignored that invariant and
        # could shrink a single-word protected header like "Units" past its
        # own floor, wrapping it mid-word exactly like the shared-space case
        # was already designed to prevent.
        word_floor_total = sum(word_floor)
        if word_floor_total >= CONTENT_W:
            # Not even everyone's bare word-floor fits together -- true
            # last resort, scaled from word_floor so headers only overflow
            # the page rather than ever breaking mid-word.
            scale = CONTENT_W / word_floor_total
            return [w * scale for w in word_floor]
        frac = (sum(full_floor) - CONTENT_W) / (sum(full_floor) - word_floor_total)
        return [full_floor[i] - frac * (full_floor[i] - word_floor[i])
                for i in range(len(headers))]

    widths = list(full_floor)
    if flex_ideal_total <= remaining:
        # Enough room for every flex column's full, unwrapped header too --
        # nothing needs to give anything back.
        return widths
    if flex_floor_total >= remaining:
        # Even everyone's bare word-floor doesn't fit -- shrink uniformly
        # to that floor total rather than the ideal one (same emergency
        # shape as the no-flex-idx case above, just scoped to flex only).
        scale = remaining / flex_floor_total
        for i in flex_idx:
            widths[i] = word_floor[i] * scale
        return widths
    # The common "almost fits" case: interpolate each flex column between
    # its ideal and its floor, proportional to how much slack it has to
    # give (a single-word column with zero slack stays put; a three-word
    # column absorbs its fair share of the shortfall), until the total
    # exactly matches what's actually available.
    frac = (flex_ideal_total - remaining) / (flex_ideal_total - flex_floor_total)
    for i in flex_idx:
        widths[i] = full_floor[i] - frac * (full_floor[i] - word_floor[i])
    return widths

# ── Cover page ─────────────────────────────────────────────────────────────────
_COVER_TITLE_FLOOR = 28.0   # never shrink the cover title below this size

def _fit_title_size(text, avail_width, font_name="CormorantGaramond-Bold",
                     base_size=44, floor=_COVER_TITLE_FLOOR, step=0.5):
    """Find the largest font size (down to floor) at which text fits on one
    line within avail_width -- same approach as _fit_label_style() uses for
    scorecard labels, applied here to the cover page's period title. Only
    Monthly/Quarterly/Annual labels ("July 2026", "Q2 2026", "2026") were
    ever short enough that this was never needed; Custom range labels
    ("Jan 01, 2025 - Aug 11, 2026") overflow the title's intended width at
    the fixed 44pt size on every single Custom report, not just long ones.
    """
    size = base_size
    while size > floor:
        if pdfmetrics.stringWidth(text, font_name, size) <= avail_width:
            break
        size -= step
    else:
        size = floor
    return size

def _draw_cover(canvas, doc, period_label, report_type, gen_date_str):
    c = canvas
    c.saveState()

    # Background
    c.setFillColor(OFF_WHITE)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Top + bottom accent bars
    c.setFillColor(BURNT_ORANGE)
    c.rect(0, PAGE_H - 6, PAGE_W, 6, fill=1, stroke=0)
    c.rect(0, 0,          PAGE_W, 6, fill=1, stroke=0)

    # Logo
    logo_w = 175
    try:
        from reportlab.lib.utils import ImageReader
        ir     = ImageReader(LOGO_PATH)
        iw, ih = ir.getSize()
        logo_h = logo_w * ih / iw
        logo_x = (PAGE_W - logo_w) / 2
        logo_y = PAGE_H * 0.535
        c.drawImage(LOGO_PATH, logo_x, logo_y,
                    width=logo_w, height=logo_h, mask="auto")
    except Exception:
        pass

    # Eyebrow — regular weight retains letter-spacing refinement without appearing faint
    c.setFont("Montserrat", 8)
    c.setFillColor(GOLD)
    c.drawCentredString(PAGE_W/2, PAGE_H * 0.505,
                        "B U S I N E S S   I N T E L L I G E N C E   R E P O R T")

    # Thin gold rule — standardised inset matches the lower rule below
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.line(MARGIN + 50, PAGE_H * 0.49, PAGE_W - MARGIN - 50, PAGE_H * 0.49)

    # Period -- font size shrinks to fit within the same span the gold rule
    # lines above/below it are drawn across, so the title never overflows
    # past its own visual frame (see _fit_title_size()).
    title_avail_width = PAGE_W - 2 * (MARGIN + 50)
    title_size = _fit_title_size(period_label, title_avail_width)
    c.setFont("CormorantGaramond-Bold", title_size)
    c.setFillColor(DARK_BROWN)
    c.drawCentredString(PAGE_W/2, PAGE_H * 0.44, period_label)

    # Report type — regular weight keeps it legible while staying subordinate to the period title
    c.setFont("Montserrat", 10)
    c.setFillColor(SAGE)
    c.drawCentredString(PAGE_W/2, PAGE_H * 0.41, report_type)

    # Thick gold rule — same inset as the thin rule above for symmetry
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.5)
    c.line(MARGIN + 50, PAGE_H * 0.392, PAGE_W - MARGIN - 50, PAGE_H * 0.392)

    # Footer
    c.setFont("Montserrat", 7.5)
    c.setFillColor(DARK_BROWN)
    c.drawCentredString(PAGE_W/2, 52,
                        "Prepared by Lakshira Inventory Management System")
    c.drawCentredString(PAGE_W/2, 40,
                        f"Generated: {gen_date_str}   ·   Data current as of {gen_date_str}")
    # Regular weight + warmer grey keeps this readable without competing with metadata above
    c.setFont("Montserrat", 7.5)
    c.setFillColor(HexColor("#8C7E78"))
    c.drawCentredString(PAGE_W/2, 28, "Confidential  —  For Internal Use Only")

    c.restoreState()

# ── Running header / footer ────────────────────────────────────────────────────
def _page_cb(period_label, report_type):
    def on_page(canvas, doc):
        if doc.page == 1:
            return
        c = canvas
        c.saveState()
        yh = PAGE_H - MARGIN + 14
        yf = MARGIN - 16

        # Header
        c.setFont("Montserrat-Bold", 8.5)
        c.setFillColor(BURNT_ORANGE)
        c.drawString(MARGIN, yh, "LAKSHIRA")
        c.setFont("Montserrat", 7.5)
        c.setFillColor(GOLD)
        c.drawRightString(PAGE_W - MARGIN, yh,
                          f"{report_type}  ·  {period_label}")
        c.setStrokeColor(GOLD)
        c.setLineWidth(0.5)
        c.line(MARGIN, yh - 4, PAGE_W - MARGIN, yh - 4)

        # Footer text
        c.setStrokeColor(LIGHT_GREY)
        c.setLineWidth(0.5)
        c.line(MARGIN, yf + 8, PAGE_W - MARGIN, yf + 8)
        c.setFont("Montserrat", 7)
        c.setFillColor(HexColor("#8C7E78"))
        c.drawString(MARGIN, yf, "Confidential — For Internal Use Only")
        c.setFillColor(DARK_BROWN)
        c.drawRightString(PAGE_W - MARGIN, yf, f"Page {doc.page - 1}")

        # Branded orange accent bar — thinner version of the cover's bottom bar
        c.setFillColor(BURNT_ORANGE)
        c.rect(0, 0, PAGE_W, 3, fill=1, stroke=0)
        c.restoreState()
    return on_page

# ── Section 0: Executive Summary ──────────────────────────────────────────────
# Sales Channel is a newer field, built ahead of when it becomes load-bearing
# (once Shopify launches) -- historical sales predate it and are being
# manually backfilled over time. Below this threshold, a period's channel
# data is almost entirely "Unknown", which isn't a useful signal to show in
# the report or hand to the AI -- it would just read as "Unknown: 100%".
_CHANNEL_COVERAGE_THRESHOLD = 0.5

def _channels_ready(ch, total_units):
    """True once at least _CHANNEL_COVERAGE_THRESHOLD of this period's sold
    units have a real (non-"Unknown") Sales Channel value."""
    if not ch or not total_units:
        return False
    unknown = ch.get("Unknown", {"count": 0})["count"]
    return (total_units - unknown) / total_units >= _CHANNEL_COVERAGE_THRESHOLD

# Payment Method has no rollout-timeline justification for being blank the
# way Sales Channel does -- ask_payment_method() is a mandatory prompt in
# record_sale(), so it should be fully populated for any sale entered
# through the live system. A period below this threshold points at a
# data-entry gap for that specific period (older/migrated rows, or sales
# entered outside the normal guided flow), not a field that isn't
# load-bearing yet. Still gated the same way: a table that's almost
# entirely "Unknown" isn't a useful signal to show either way.
_PAYMENT_COVERAGE_THRESHOLD = 0.5

def _payment_methods_ready(pm, total_units):
    """True once at least _PAYMENT_COVERAGE_THRESHOLD of this period's sold
    units have a real (non-"Unknown") Payment Method value."""
    if not pm or not total_units:
        return False
    unknown = pm.get("Unknown", {"count": 0})["count"]
    return (total_units - unknown) / total_units >= _PAYMENT_COVERAGE_THRESHOLD

_BRAND_CONTEXT_PATH = os.path.join(BASE_DIR, "Brand_Context_Checklist.md")

def _load_brand_context():
    """Read the filled-in Brand & Product Context Checklist fresh on every
    call (not cached) so an edit to the file takes effect on the very next
    report generated, with no code change or restart needed. Missing file
    degrades to an empty string rather than failing the whole report --
    the executive summary still works, just without the brand-specific
    grounding, exactly like before this was wired in."""
    try:
        with open(_BRAND_CONTEXT_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""

def _claude_summary(m, period_label, report_type):
    if ANTHROPIC_API_KEY.startswith("PLACEHOLDER"):
        print("\033[2m  Executive summary skipped — ANTHROPIC_API_KEY not configured.\033[0m")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        payload = {
            "period": period_label, "report_type": report_type,
            "revenue_usd": round(m["rev"], 2),
            "gross_profit_usd": round(m["gp"], 2),
            "avg_margin_pct": round(m["margin"]*100, 1),
            "units_sold": m["units"],
            "avg_order_value_usd": round(m["avg_price"], 2),
            "lifetime_units_total": m["total_lifetime_sold"],
            "lifetime_units_share_pct": round(
                m["units"] / m["total_lifetime_sold"] * 100 if m["total_lifetime_sold"] else 0, 1),
            "lifetime_revenue_total_usd": round(m["total_lifetime_revenue"], 2),
            "lifetime_revenue_share_pct": round(
                m["rev"] / m["total_lifetime_revenue"] * 100 if m["total_lifetime_revenue"] else 0, 1),
            "units_discounted": len(m["disc_rows"]),
            "discount_revenue_lost": round(m["disc_lost"], 2),
            "prior_revenue_usd": round(m["p_rev"], 2),
            "new_customers": len(m["new_c"]),
            "returning_customers": len(m["ret_c"]),
            "outstanding_balance_usd": round(m["total_out"], 2),
            "avg_days_outstanding": round(m["avg_collect_days"]),
            "units_180_plus_days": len(m["aging"]["180+"]),
            "sell_through_rate_pct": round(m["sell_through"]*100, 1),
            "avg_days_to_sell": round(m["avg_dts"]),
            "inventory_turnover": round(m["turnover"], 2),
            "customer_retention_rate_pct": round(m["ret_rate"], 1),
            "top5_customer_revenue_share_pct": round(m["top5_share"], 1),
            "cancellations": m["cancel_cnt"],
            **({"top_channels": {k: {"units": v["count"], "revenue": round(v["revenue"],2)}
                                 for k, v in m["channels"].items()}}
               if _channels_ready(m["channels"], m["units"]) else {}),
            "top_categories": sorted(
                [{"category": k,
                  "revenue": round(v["revenue"],2),
                  "units": v["units"],
                  "margin_pct": round(v["gp"]/v["revenue"]*100 if v["revenue"] else 0, 1)}
                 for k, v in m["cats"].items()],
                key=lambda x: x["revenue"], reverse=True)[:5],
            # Same reliability gate as the Supplier Performance table itself
            # (10 units acquired) -- a supplier with only a handful ever
            # acquired can land at 100% or 0% sell-through purely by chance,
            # not a real signal worth handing to the executive summary.
            "lowest_sell_through_suppliers": sorted(
                [{"supplier": k,
                  "sell_through_pct": round(v["life_units"]/v["life_acquired"]*100, 1),
                  "margin_pct": round(v["life_gp"]/v["life_revenue"]*100, 1) if v["life_revenue"] else None}
                 for k, v in m["suppliers"].items() if v["life_acquired"] >= 10],
                key=lambda x: x["sell_through_pct"])[:3],
            **({"purchase_behavior_pct": {
                    "single_item_revenue_share_pct": round(
                        m["period_occasion_split"]["single"]["revenue"] /
                        (m["period_occasion_split"]["single"]["revenue"] + m["period_occasion_split"]["multi"]["revenue"]) * 100, 1)
                        if (m["period_occasion_split"]["single"]["revenue"] + m["period_occasion_split"]["multi"]["revenue"]) else 0,
                    "multi_item_revenue_share_pct": round(
                        m["period_occasion_split"]["multi"]["revenue"] /
                        (m["period_occasion_split"]["single"]["revenue"] + m["period_occasion_split"]["multi"]["revenue"]) * 100, 1)
                        if (m["period_occasion_split"]["single"]["revenue"] + m["period_occasion_split"]["multi"]["revenue"]) else 0,
                }} if m["period_occasion_split"] else {}),
        }
        brand_context = _load_brand_context()
        brand_context_block = (
            f"\n\nBrand & product context, filled in directly by the founder -- treat this "
            f"as ground truth about how Lakshira actually operates, above any general "
            f"assumptions about luxury retail or Indian textiles:\n\n{brand_context}\n"
            if brand_context else ""
        )

        prompt = f"""You are a senior business analyst writing an executive summary for Lakshira Handwoven Weaves, a US-based luxury boutique specialising in premium Indian handwoven sarees. The business sells through Instagram, WhatsApp, Shopify, and Exhibition/Popup events. The long-term vision is to become a recognised luxury boutique brand.
{brand_context_block}
Business data for {period_label}:
{json.dumps(payload, indent=2)}

Write a concise executive summary followed by specific actionable recommendations. The stakeholder (business owner) is non-technical but business-savvy, and will read this report on her own, without an analyst present to explain it. The summary itself has to do the work an analyst would normally do out loud. She may also relay individual findings to functional people (a bookkeeper, a marketer) who won't have read the rest of the report, so each point needs to stand on its own. Every recommendation must be concrete and implementable, not generic advice. Align recommendations with the luxury boutique vision. Never use an abbreviation or industry shorthand (e.g. "MoM", "YoY", "AOV") without writing it out in full first: spell out "month-over-month" or "year-over-year" the way a person would say it out loud, not the acronym a spreadsheet would use.

Write like a senior analyst presenting findings out loud to a founder, not like a printout of the underlying math. Use em dashes sparingly, only where a sentence genuinely calls for one grammatically, never as a default habit or a way to bolt a second thought onto a sentence you could have just split in two. A human analyst reaches for one occasionally; AI-generated text tends to overuse them, and that overuse is exactly what to avoid. That means: one idea per sentence. State the finding in plain words first, then support it with the number, not the number first with the meaning tacked on afterward. When you connect two metrics as evidence for the same underlying story (e.g. low inventory turnover and a high count of aged units both pointing at stock not moving), give that connection its own full sentence, for example "X and Y point at the same problem: ...", rather than folding it into the sentence that already stated the headline number. Only draw a connection when the data actually supports it; do not force a correlation that isn't there. Do not stack multiple clauses onto one sentence with dashes or semicolons as a way to fit more in: if a sentence needs a dash to bolt on a second idea, split it into two sentences instead. Leave out supporting numbers that don't actually change the reader's takeaway; every figure you cite should be doing work, not just be available.

When a margin or cost figure looks like a problem, ground the explanation only in what Lakshira actually controls: its own pricing and markup relative to what it pays to acquire a unit, which suppliers it chooses to source from and how much, and its discounting policy. Lakshira does not set what a supplier or weaver charges, so never frame the fix as something that needs to happen on the supplier's side (for example, do not say something like "the fix sits with weaver pricing"), and never describe a margin gap as a "sourcing problem" as if the acquisition cost itself were the thing to change. If margin is a genuine concern, the actionable conclusion is about Lakshira's own pricing decisions -- staying competitively priced while still sustaining the margin the business needs to grow -- not a call for suppliers to charge less.

FORMAT RULES (follow exactly, no deviations):

SUMMARY:
[Plain business language. Write as many sentences as the analysis genuinely needs to stay clear at one idea per sentence: most periods will run 7–11 short sentences, not 3–4 long ones. Structure the summary around these beats, in order:
1. SITUATION (1 sentence): a brief grounding statement connecting this period to where the business actually is in its longer arc toward the luxury boutique goal, drawing on the brand & product context above where it's genuinely relevant. Orientation, not a metric and not a recap of the prior period -- what is this report about to tell her, in the context of what she's actually building.
2. BOTTOM LINE (1 sentence, immediately after, before any supporting numbers): the single takeaway a founder would want if she only read one sentence of this report. State the meaning, not a metric -- for example "the business grew revenue but that growth is dangerously concentrated in one customer relationship," not a dollar figure. Pick whichever takeaway the data actually supports as the period's real headline, not necessarily whichever number happens to be largest. Everything that follows has to support this sentence, not introduce a separate topic from it.
3. Revenue for the period and how it moved vs. the prior period.
4. Profitability: gross margin.
5. Volume: units sold, AND how this period stacks up against lifetime performance
   (what share of all-time units/revenue this single period represents). This is a young,
   growing business, so a period's contribution to lifetime totals is a real signal of
   momentum, so call out whether this was a strong or weak period relative to that lifetime
   base, not just in isolation.
6. The most consequential additional finding(s) in the data, beyond revenue/profit/volume -- at most two, and only if each is genuinely significant. This does not have to be framed as a "risk": it can be a risk, an opportunity, a customer-behavior shift, a supplier or inventory signal, or anything else the data actually supports as consequential this period. Every finding included here must reinforce, complicate, or explain the Bottom Line from step 2 -- if you cannot connect it to that throughline in one clause, leave it out of the summary rather than stating it as a disconnected fact. A real finding that doesn't fit the throughline still belongs in this report -- put it in Recommendations instead, where it can stand on its own without needing to fit a narrative.
Do not pad with sentences that repeat a point already made. Do not treat steps 3-6 as a checklist to work through independently of the Bottom Line -- every sentence should read as support for, or elaboration of, the single throughline stated in step 2, not as a section-by-section survey of the report.
Mark every key metric (dollar amounts, percentages, growth rates, unit counts) by wrapping it in double angle brackets: <<$24,250>>, <<up 84% from last month>>, <<29.1% margin>>, <<52 units>>.]

RECOMMENDATIONS:
[Include as many recommendations, 2 to 4, as the data genuinely supports as concrete and high-impact this period -- never manufacture a recommendation just to fill a slot, and never pick one from each area of the report for the sake of covering everything. Rank and select purely by actual business impact: it's entirely fine for 2 or even all of the recommendations to cluster in the same problem area (e.g. two different inventory actions) if that's genuinely where the biggest opportunities are this period, rather than forcing spread across topics for the appearance of thoroughness. Repeat the block below once per recommendation, in descending order of impact:

TITLE: [3–6 words, action-oriented, no verb-first requirement (this is a label, not a directive). e.g. "Liquidate Aged Inventory", "Expand Kuttu Gadwal Sourcing"]
RECOMMENDATION: [1–2 sentences, plain and concise. Start with a strong imperative verb. State the specific action and measurable target.]
WHY: [One short clause/fragment, not a full sentence. The single data point that justifies this action. Wrap the key metric in double angle brackets, e.g. <<4% sell-through>>.]
NEXT STEP: [One short clause/fragment. The concrete first action to take.]
IMPACT: [One short clause/fragment. The expected business outcome. Wrap the key metric in double angle brackets if there is one.]]"""

        resp = client.messages.create(
            model="claude-opus-5", max_tokens=8192,
            messages=[{"role":"user","content":prompt}]
        )
        if resp.stop_reason == "max_tokens":
            print("\033[33m  ⚠  Executive summary was cut short — some recommendations "
                  "may be missing from the report.\033[0m")
        # Not content[0] -- Opus 5 thinks by default, so a thinking block
        # precedes the text block; find the text block by type instead of
        # assuming position.
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise ValueError(f"No text content in response (stop_reason={resp.stop_reason!r})")
        return text.strip()
    except Exception as e:
        print(f"\033[33m  ⚠  Executive summary could not be generated ({e}) — "
              f"the report will show a fallback message instead.\033[0m")
        return None

def _md_to_rl(text):
    """Convert minimal markdown + metric markers to reportlab XML."""
    text = text.replace('&', '&amp;')                                   # XML-safe (must be first)
    # Escape any stray bare < or > from the AI's natural prose (e.g. a
    # sentence using "<" the way a comparison symbol reads) BEFORE it can
    # be misread as XML tag syntax -- ReportLab's Paragraph parser can
    # crash on this, and _safe_paragraph()'s own recovery attempt only
    # works when the stray "<" happens to have a later ">" to pair with;
    # when it doesn't, the "recovery" fails the same way the first
    # attempt did and the whole report generation crashes uncaught.
    # The << >> metric-marker delimiters are intentional and get
    # converted into real tags below, so they're protected here first.
    text = text.replace('<<', '\x00LT\x00').replace('>>', '\x00GT\x00')
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    text = text.replace('\x00LT\x00', '<<').replace('\x00GT\x00', '>>')
    text = re.sub(r'-{3,}', '', text)                                   # remove --- dividers
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.S)    # **bold** → <b>
    text = re.sub(r'\*\*', '', text)                                    # strip orphaned **
    # <<metric>> → bold in a fixed dark-brown, regardless of the surrounding
    # paragraph's color, so key numbers read consistently important everywhere.
    metric_repl = f'<font color="{COLOUR["dark_brown"]}"><b>\\1</b></font>'
    text = re.sub(r'<<(.+?)>>', metric_repl, text, flags=re.S)
    text = re.sub(r'<<|>>', '', text)                                   # strip orphaned << >>
    return text.strip()

def _safe_paragraph(text, style):
    """AI-generated text can occasionally survive _md_to_rl() with unbalanced
    markup (e.g. a stray unclosed tag) that ReportLab's parser rejects — which
    would otherwise crash the entire report over one malformed AI response.
    Fall back to stripping all markup and rendering as plain text instead."""
    try:
        return Paragraph(text, style)
    except Exception:
        return Paragraph(re.sub(r'<[^>]*>', '', text), style)

def _auto_bold_financials(text):
    """Bold dollar amounts and percentages using explicit font tags (reliable across ReportLab versions)."""
    parts = re.split(r'(<[^>]+>.*?</[^>]+>)', text, flags=re.S)
    result = []
    for part in parts:
        if part.startswith('<'):
            result.append(part)
        else:
            part = re.sub(r'(\$[\d,]+(?:\.\d{1,2})?[KMB]?)', r'<font face="Montserrat-Bold" color="#D4A84C">\1</font>', part)
            part = re.sub(r'([+-]?\d+(?:\.\d+)?%)', r'<font face="Montserrat-Bold" color="#D4A84C">\1</font>', part)
            result.append(part)
    return ''.join(result)

def _sec_exec(claude_text):
    header = _sec("Executive Summary")
    if not claude_text:
        return [KeepTogether(header + [_callout(
            "Executive summary unavailable this run. Check the console output "
            "from report generation for the reason, or regenerate the report.",
            bg=HexColor(COLOUR["sage"])
        )])]

    sm = re.search(r'SUMMARY:\s*(.*?)(?=RECOMMENDATIONS:|$)', claude_text, re.S)
    rm = re.search(r'RECOMMENDATIONS:\s*(.*)',                 claude_text, re.S)

    # Header + summary paragraph kept together so the section title is never orphaned
    if sm:
        raw = _md_to_rl(sm.group(1).strip())   # <<metric>> and **bold** markers become <b> tags
        story = [KeepTogether(header + [_safe_paragraph(raw, ST["exec_body"])]), Spacer(1, 10)]
    else:
        story = header

    if rm:
        rec_header = [
            HRFlowable(width=CONTENT_W, thickness=0.5, color=GOLD, spaceBefore=4, spaceAfter=0),
            Paragraph("Recommendations", ST["exec_rec_head"]),
            Spacer(1, 10),
        ]
        rm_text = rm.group(1).strip()

        # Parse TITLE / RECOMMENDATION / WHY / NEXT STEP / IMPACT groups
        pairs = re.findall(
            r'TITLE:\s*(.*?)\s*RECOMMENDATION:\s*(.*?)\s*WHY:\s*(.*?)\s*NEXT STEP:\s*(.*?)\s*IMPACT:\s*(.*?)(?=TITLE:|\Z)',
            rm_text, re.S
        )

        sage_hex = COLOUR["sage"]

        def _support_line(label, text):
            return _safe_paragraph(
                f'<font color="{sage_hex}"><b>{label}:</b></font>  {_md_to_rl(text.strip())}',
                ST["rec_support"]
            )

        if pairs:
            for idx, (title, rec, why, next_step, impact) in enumerate(pairs, start=1):
                title = _md_to_rl(title.strip())
                rec   = _md_to_rl(rec.strip())
                block = [
                    _safe_paragraph(f"{idx}.  {title}", ST["rec_title"]),
                    _safe_paragraph(rec, ST["rec_body"]),
                    _support_line("Why it matters", why),
                    _support_line("Next step", next_step),
                    _support_line("Expected impact", impact),
                    Spacer(1, 12),
                ]
                # "Recommendations" heading is only ever orphan-guarded alongside
                # recommendation #1 — pairing it with every recommendation would
                # force the whole list onto one page regardless of length.
                story.append(KeepTogether((rec_header if idx == 1 else []) + block))
        else:
            # Fallback: old bullet format
            story.append(KeepTogether(rec_header))
            for line in rm_text.split("\n"):
                line = _md_to_rl(line.strip().lstrip("•·- ").strip())
                if line:
                    story.append(_safe_paragraph(f"• {line}", ST["exec_rec"]))

    story.append(Spacer(1, 6))
    return story

# ── Section 1: Revenue & Profit ────────────────────────────────────────────────
def _sec_revenue(m):
    rv, gp, mg = m["rev"], m["gp"], m["margin"]

    # Section title + scorecard kept together so the title is never orphaned
    story = [KeepTogether(_sec("Revenue & Profit") + [_metric_row([
        ("Total Revenue",    _usd_k(rv)),
        ("Gross Profit",     _usd_k(gp)),
        ("Gross Margin",     _pct(mg)),
        ("Units Sold",       str(m["units"])),
        ("Avg Order Value",  _usd_k(m["avg_price"])),
    ])])]
    story.append(Spacer(1, 10))

    rows = [
        ["Revenue (Gross)",          _usd(rv)],
        ["Cancellation Revenue Lost",_usd(m["cancel_lost"], deduction=True)],
        ["Net Revenue",              _usd(rv - m["cancel_lost"])],
        ["Cost of Goods Sold (COGS)",_usd(m["cost"], deduction=True)],
        ["Gross Profit",             _usd(gp)],
        ["Gross Margin %",           _pct(mg)],
    ]
    story.append(_tbl(["", "Amount"], rows,
                       [CONTENT_W*0.65, CONTENT_W*0.35],
                       key_rows={2, 4}, bold_rows={5}))
    story.append(Spacer(1, 8))

    tax = [
        ["Total Revenue",    _usd(rv)],
        ["Cost of Goods Sold",_usd(m["cost"], deduction=True)],
        ["Gross Profit",     _usd(gp)],
        ["Units Sold",       str(m["units"])],
    ]
    # This table is always exactly 4 fixed rows, so min_rows=len(tax) safely
    # protects the whole table + note together — never at risk of exceeding
    # a page's height the way a variable-length table could be.
    tax_note = Paragraph(
        "All figures in USD, with <b>COGS</b> converted from INR using the official "
        "exchange rate on each item's acquisition date, not today's rate. These are "
        "gross figures only, before operating expenses, not a substitute for "
        "accountant-prepared tax figures.",
        ST["note"]
    )
    story += _sec_table(
        [Paragraph("Tax Reference Summary", ST["sub_head"])],
        ["Metric",""], tax, [CONTENT_W*0.65, CONTENT_W*0.35],
        min_rows=len(tax), trailing=[tax_note]
    )

    story.append(Spacer(1, 10))
    swing_n = m.get("swing_window_months")
    if swing_n:
        cur_rel, pri_rel, chg = m["swing_current_rel"], m["swing_prior_rel"], m["swing_change"]
        cur_disp = f"{cur_rel:.1f}x a typical month" if cur_rel is not None else "—"
        pri_disp = f"{pri_rel:.1f}x a typical month" if pri_rel is not None else "—"
        if chg is None:
            direction = "No prior-window data available to compare against."
        elif chg < 0:
            direction = "<b>▼ Swing decreased:</b> sales were steadier than the prior window."
        elif chg > 0:
            direction = "<b>▲ Swing increased:</b> sales bounced around more than the prior window."
        else:
            direction = "No change from the prior window."

        swing_story = [
            Paragraph("Sales Consistency", ST["sub_head"]),
            _metric_row([
                (f"Current {swing_n}-Month Swing", cur_disp),
                (f"Prior {swing_n}-Month Swing",   pri_disp),
            ]),
            Spacer(1, 6),
            *_split_note(
                f"<b><u>Swing</u></b> compares the best month against the worst month within "
                f"the trailing {swing_n}-month window, relative to a typical month in "
                f"that same window. <b>What's healthy:</b> a lower number means steadier, "
                f"more predictable sales; a higher number means results are swinging "
                f"harder from month to month. {direction}"
            ),
        ]
        if swing_n == 3:
            swing_story.append(Spacer(1, 4))
            swing_story.append(Paragraph(
                "Because a Quarterly report compares only 3 months against 3 months, "
                "a single unusually strong or weak month can move this number more "
                "than it would in a wider window, so read it as a general signal, not "
                "a precise measurement.",
                ST["note"]
            ))
        story.append(KeepTogether(swing_story))
    else:
        story.append(KeepTogether([
            Paragraph("Sales Consistency", ST["sub_head"]),
            Paragraph(
                "Not shown for a custom date range. Sales Consistency compares a "
                "fixed trailing window (6, 3, or 12 months) tied to how Monthly, "
                "Quarterly, and Annual reports are already scoped. A custom range "
                "doesn't map onto that cleanly, so it's left out rather than forcing "
                "an arbitrary window onto it.",
                ST["note"]
            ),
        ]))
    return story

# ── Section 2: Sales Activity ──────────────────────────────────────────────────
def _sec_sales(m):
    total_life_units = m["total_lifetime_sold"]
    total_life_rev   = m["total_lifetime_revenue"]
    units_share_pct  = m["units"] / total_life_units * 100 if total_life_units else 0
    rev_share_pct    = m["rev"] / total_life_rev * 100 if total_life_rev else 0

    story = [KeepTogether(_sec("Sales Activity") + [_metric_row([
        ("Units Sold",             str(m["units"])),
        ("Avg Order Value",        _usd_k(m["avg_price"])),
        ("Cancellations",          str(m["cancel_cnt"])),
        ("Lifetime Units %",   f"{units_share_pct:.1f}%"),
        ("Lifetime Revenue %", f"{rev_share_pct:.1f}%"),
    ]), Paragraph(
        f"This period contributed <b>{m['units']} of {total_life_units} lifetime units "
        f"({units_share_pct:.1f}%)</b> and <b>{_usd(m['rev'])} of {_usd(total_life_rev)} "
        f"lifetime revenue ({rev_share_pct:.1f}%)</b>.",
        ST["note"]
    )])]
    story.append(Spacer(1, 10))

    payment_header = Paragraph("Payment Method Breakdown", ST["sub_head"])
    if not m["pay_methods"]:
        story.append(KeepTogether([
            payment_header,
            _no_data("No payment data recorded for this period."),
        ]))
    elif _payment_methods_ready(m["pay_methods"], m["units"]):
        pm  = sorted(m["pay_methods"].items(), key=lambda x:x[1]["revenue"], reverse=True)
        rows= [[met, str(d["count"]), _usd(d["revenue"])] for met,d in pm]
        story += _sec_table(
            [payment_header], ["Payment Method","Units","Revenue"], rows,
            [CONTENT_W*0.5, CONTENT_W*0.2, CONTENT_W*0.3],
            totals=["Total", str(m["units"]), _usd(m["rev"])]
        )
    # else: below coverage threshold, omit entirely -- mirrors _sec_channels,
    # a table that's almost all "Unknown" isn't a useful signal to show.
    return story

# ── Section 3: Discount Summary ────────────────────────────────────────────────
def _sec_discounts(m):
    header = _sec("Discount Summary")
    dr = m["disc_rows"]
    if not dr:
        return [KeepTogether(header + [_no_data("No discounts were applied in this period.")])]

    # Heading + scorecard travel through _sec_table() below, which reserves
    # space for the heading plus a preview of the table so the two can't get
    # split across a page break -- previously this table was appended raw,
    # risking the heading landing at the bottom of a page with the entire
    # table pushed to the next.
    heading = header + [_metric_row([
        ("Units Discounted", str(len(dr))),
        ("Revenue Given Up", _usd_k(m["disc_lost"])),
        ("Avg Discount",     f"{m['avg_disc']:.1f}%"),   # not _pct() -- avg_disc is already %, not decimal
    ]), Spacer(1, 6), *_split_note(
        "At Lakshira, discounts are reserved for the top tier of established, proven "
        "repeat customers, the ones the founder has come to recognize through a "
        "consistent buying pattern, sometimes including bulk purchases of several "
        "units at once. They're never given to new customers or those who've only "
        "bought a piece or two and haven't yet shown that pattern. It's a way to "
        "reward and maintain the business's most valuable relationships, not a "
        "routine pricing lever. <b>What's healthy:</b> an occasional discount to one "
        "of these customers is expected and healthy; what's worth watching is "
        "discounting reaching customers outside that top tier, which would signal "
        "pricing discipline slipping rather than intentional relationship-building."
    ), Spacer(1, 6)]

    rows = []
    for r in dr:
        orig = _flt(r.get("Selling Price (USD)"))
        sold = _flt(r.get("Actual Selling Price (USD)"))
        dp   = _flt(r.get("Discount %"))   # sheet stores as %, not decimal -- see line ~1596
        rows.append([r.get("SKU",""), r.get("Weave Type / Cluster",""),
                     _usd(orig), _usd(sold), f"{dp:.1f}%",
                     _usd(orig-sold, deduction=True)])
    return _sec_table(
        heading,
        ["SKU","Weave Type","Original","Sold For","Disc %","Given Up"],
        rows,
        [CONTENT_W*.18, CONTENT_W*.25, CONTENT_W*.15,
         CONTENT_W*.15, CONTENT_W*.12, CONTENT_W*.15],
        totals=["Total","","","","",_usd(m["disc_lost"], deduction=True)],
        left_cols={1}, center_cols={2, 3, 4, 5}
    )

# ── Section 4: Sales Channel Breakdown ────────────────────────────────────────
def _sec_channels(m):
    header = _sec("Sales Channel Breakdown")
    ch = m["channels"]
    if not ch:
        return [KeepTogether(header + [_no_data()])]
    if not _channels_ready(ch, m["units"]):
        return []

    t_rev = m["rev"]
    rows  = []
    for c, d in sorted(ch.items(), key=lambda x:x[1]["revenue"], reverse=True):
        share = d["revenue"]/t_rev*100 if t_rev else 0
        rows.append([c, str(d["count"]), _usd(d["revenue"]), f"{share:.1f}%"])
    return _sec_table(
        header, ["Channel","Units Sold","Revenue","Revenue Share"], rows,
        [CONTENT_W*.35, CONTENT_W*.2, CONTENT_W*.25, CONTENT_W*.2],
        totals=["Total", str(m["units"]), _usd(t_rev), "100.0%"],
        center_cols={1, 2, 3}
    )

# ── Section 4b: Purchase Behavior ─────────────────────────────────────────────
def _sec_purchase_behavior(m):
    """What share of revenue comes from customers buying just one saree at a
    time versus several in the same visit -- both this period and lifetime
    in one table, since basket-grouping data is only informative in
    aggregate, not row-by-row like most other tables in this report.
    Silently omitted (not a "no data" placeholder) if the MySQL warehouse is
    unreachable, since there's no meaningful per-row fallback the way AOV
    has one -- same convention as Sales Channel Breakdown's own
    _channels_ready guard."""
    period_split   = m["period_occasion_split"]
    lifetime_split = m["lifetime_occasion_split"]
    if not period_split or not lifetime_split:
        return []

    header = _sec("Purchase Behavior")
    rows = []
    for label, split in (("This Period", period_split), ("Lifetime", lifetime_split)):
        single, multi = split["single"], split["multi"]
        t_rev = single["revenue"] + multi["revenue"]
        single_share = single["revenue"] / t_rev * 100 if t_rev else 0
        multi_share  = multi["revenue"]  / t_rev * 100 if t_rev else 0
        rows.append([
            label, str(single["occasions"]), str(multi["occasions"]),
            f"{single_share:.1f}%", f"{multi_share:.1f}%",
        ])

    heading = header + _split_note(
        "<b><u>Single vs. Multi-Item Occasions</u></b> splits revenue by whether "
        "a customer bought one saree or several in the same visit. "
        "<b>What's healthy:</b> neither is inherently better -- a rising "
        "multi-item share can mean more bulk/gifting occasions or deepening "
        "trust from repeat customers buying more at once; a rising "
        "single-item share can mean more first-time or occasion-driven "
        "buyers testing the brand. Worth reading alongside Retention Rate "
        "and Customer Concentration rather than as a standalone signal."
    ) + [Spacer(1, 6)]

    return _sec_table(
        heading,
        ["Scope", "Single-Item Occasions", "Multi-Item Occasions",
         "Single-Item Revenue Share", "Multi-Item Revenue Share"],
        rows, [CONTENT_W*.2, CONTENT_W*.2, CONTENT_W*.2, CONTENT_W*.2, CONTENT_W*.2],
        min_rows=2, center_cols={1, 2, 3, 4}
    )

# ── Section 5: Outstanding Balance ────────────────────────────────────────────
def _sec_outstanding(m):
    header = _sec("Outstanding Balance") + [
        Paragraph(
            "<b><u>Outstanding Balance</u></b> is money owed to the business for units "
            "already sold but not yet fully paid for: revenue that's been earned on "
            "paper but hasn't reached the bank yet. The dollar total matters less on "
            "its own than how long it's been open; see <b>Avg Days Outstanding</b> "
            "below for that.",
            ST["note"]
        ),
        Spacer(1, 6),
    ]
    out   = m["outstanding"]
    if not out:
        return [KeepTogether(header + [_no_data("No outstanding balances.")])]

    today = date.today()

    def _days_out(r):
        # None for blank/malformed *or future* Date Sold -- a future date
        # only reaches the sheet via a hand-edit, and should read as
        # unknown, not as a negative day count in an emailed report.
        return _days_since(_parse_date(r.get("Date Sold","")), reference=today)

    def _out_sort_key(r):
        d = _days_out(r)
        return (d is not None, -d if d is not None else 0, r.get("SKU","").strip())

    rows  = []
    for r in sorted(out, key=_out_sort_key):
        d = _days_out(r)
        days = str(d) if d is not None else "—"
        rows.append([
            r.get("SKU",""),
            r.get("Weave Type / Cluster",""),
            r.get("Customer Name",""),
            r.get("Date Sold",""),
            _usd(_flt(r.get("Amount Received (USD)"))),
            _usd(_flt(r.get("Amount Outstanding (USD)"))),
            _method(r.get("Transaction Notes","")),
            days,
        ])
    # Heading + callout treated as one unit the table's heading — the callout
    # introduces the table, so it can't be orphaned from at least the first
    # few rows either. The table itself is then free to paginate normally.
    heading = header + [
        _callout(
            f"Total Outstanding Balance: {_usd(m['total_out'])}  "
            f"across {len(out)} unit{'s' if len(out)!=1 else ''}, "
            f"averaging {m['avg_collect_days']:.0f} days outstanding",
            bg=ROSE
        ),
        Spacer(1, 6),
        *_split_note(
            "<b><u>Avg Days Outstanding</u></b> is the average age of balances still open "
            "today: how long currently-unpaid units have been waiting, not how long "
            "already-collected balances took to resolve. <b>Why it matters:</b> the "
            "longer this runs, the less likely that balance is to ever fully collect, "
            "so it's worth following up on sooner rather than later."
        ),
        Spacer(1, 8),
    ]
    return _sec_table(
        heading,
        ["SKU","Weave","Customer","Date Sold","Received","Outstanding","Method","Days Open"],
        rows,
        [CONTENT_W*.16, CONTENT_W*.14, CONTENT_W*.15, CONTENT_W*.12,
         CONTENT_W*.12, CONTENT_W*.13, CONTENT_W*.11, CONTENT_W*.07],
        totals=["Total","","","","",_usd(m["total_out"]),"",""],
        compact=True, center_cols={3, 4, 5, 7}, left_cols={1, 2, 6}
    )

# ── Section 6: Customer Intelligence ──────────────────────────────────────────
def _sec_customers(m):
    cp = m["cust_p"]
    cl = m["cust_life"]

    # Heading + sub-heading + scorecard travel through _sec_table() below
    # (when there's a table to pair them with), which reserves space for
    # the heading plus a preview of the table so they can't get split
    # across a page break -- previously this table was appended raw after
    # a KeepTogether that only covered the heading, risking the heading
    # landing at the bottom of a page with the whole table pushed to the
    # next (the second table in this function, Lifetime Customer
    # Intelligence, already used this correctly).
    heading = _sec("Customer Intelligence") + [
        Paragraph("This Period", ST["sub_head"]),
        _metric_row([
            ("Unique Customers", str(len(cp))),
            ("New Customers",    str(len(m["new_c"]))),
            ("Returning",        str(len(m["ret_c"]))),
            ("Retention Rate",   f"{m['ret_rate']:.1f}%"),
        ]),
        Spacer(1, 6),
        *_split_note(
            "<b><u>Retention Rate</u></b> is the share of this period's buyers who had "
            "already purchased from Lakshira before this period, not whether last "
            "period's customers specifically came back, but how much of this "
            "period's business came from existing relationships versus first-time "
            "buyers. <b>What's healthy:</b> a higher rate means repeat customers are "
            "driving sales; a lower rate means this period leaned more on new "
            "customer acquisition. Neither is inherently better for a young, growing "
            "business, but a rate that stays consistently low signals first-time "
            "buyers aren't coming back, worth watching alongside Customer "
            "Concentration below."
        ),
        Spacer(1, 10),
    ]

    if cp:
        rows = []
        for n, d in sorted(cp.items(), key=lambda x:x[1]["spend"], reverse=True):
            typ = "New" if n in m["new_c"] else "Returning"
            # Avg Order Value is per real purchase occasion, not per unit --
            # falls back to units for a customer missing from the MySQL
            # occasion data (e.g. warehouse unreachable), same degradation
            # shape as Lifetime Loyalty above.
            occasions = m["period_customer_occasions"].get(n, d["units"])
            aov = _usd(d["spend"]/occasions) if occasions else "—"
            rows.append([d["name"], typ, str(d["units"]), _usd(d["spend"]), aov])
        story = _sec_table(
            heading,
            ["Customer","Type","Units","Spend","Avg Order Value"],
            rows,
            [CONTENT_W*.3, CONTENT_W*.18, CONTENT_W*.12, CONTENT_W*.18, CONTENT_W*.22],
            right_from=2, center_cols={2, 3, 4}
        )
    else:
        story = [KeepTogether(heading + [_no_data()])]

    story.append(Spacer(1, 12))
    lifetime_header = Paragraph(
        f"Lifetime Customer Intelligence  "
        f"(all-time through {date.today().strftime('%B %d, %Y')})",
        ST["sub_head"]
    )

    if cl:
        concentration_note = _split_note(
            f"<b><u>Customer Concentration</u>:</b> the top 5 customers below account for "
            f"<b>{m['top5_share']:.1f}%</b> of lifetime revenue: the share of all-time "
            f"revenue resting on the business's most valuable repeat buyers. "
            f"<b>What's healthy:</b> a higher share means more of the business depends on "
            f"a small number of relationships worth protecting; a lower share reflects a "
            f"broader, more distributed customer base."
        )
        loyalty_note = _split_note(
            f"<b><u>Lifetime Loyalty</u>:</b> <b>{m['lifetime_loyalty']:.1f}%</b> of all customers "
            f"who've ever purchased from Lakshira have returned to buy again on a separate "
            f"occasion. <b>What's healthy:</b> a higher share means customers are coming "
            f"back on their own; a lower share means most of the customer base has only "
            f"purchased once. <b>How it's measured:</b> by grouping units into real purchase "
            f"occasions (same customer, same day, same sales channel), so multiple pieces "
            f"bought in the same visit correctly count as one occasion, not a return."
        )

        rows = []
        for n, d in sorted(cl.items(), key=lambda x:x[1]["spend"], reverse=True):
            top = max(d["cats"], key=d["cats"].get) if d["cats"] else "—"
            top_share = f"{d['cats'][top]/d['units']*100:.1f}%" if d["units"] and d["cats"] else "—"
            # Avg Order Value is per real purchase occasion, not per unit --
            # same fix and fallback shape as the period Customer table above.
            occasions = m["lifetime_customer_occasions"].get(n, d["units"])
            aov = _usd(d["spend"]/occasions) if occasions else "—"
            outstanding = _usd(d["outstanding"]) if d["outstanding"] > 0 else "—"
            last_purchase = d["last_purchase"].strftime("%m-%d-%Y") if d["last_purchase"] else "—"
            rows.append([d["name"], top, str(d["units"]), _usd(d["spend"]), aov,
                         outstanding, last_purchase, top_share])

        headers = ["Customer","Most Purchased","Units","Lifetime Spend","Avg Order Value",
                    "Outstanding Balance","Last Purchase Date","Most Purchased %"]
        # Every column keeps its full natural width except Most Purchased,
        # which absorbs any shrinkage needed to fit the page -- a wrapped
        # collection name still reads fine; a wrapped customer name or a
        # currency figure broken mid-number doesn't.
        widths = _smart_widths(headers, rows, protect={
            "Customer", "Units", "Lifetime Spend", "Avg Order Value",
            "Outstanding Balance", "Last Purchase Date", "Most Purchased %",
        })

        covered = sum(d["units"] for d in cl.values())
        total_sold = m["total_lifetime_sold"]
        coverage_pct = covered / total_sold * 100 if total_sold else 0

        story += _sec_table(
            [lifetime_header, *concentration_note, *loyalty_note, Spacer(1, 6)],
            headers, rows, widths, compact=True,
            right_from=2, center_cols={2, 3, 4, 5, 6, 7},
        )
        story.append(Paragraph(
            f"Reflects <b>{covered} of {total_sold} total lifetime units sold "
            f"({coverage_pct:.0f}%)</b>. The remainder lack a recorded customer name "
            f"and are excluded, so lifetime figures likely understate true customer "
            f"value. Includes all transactions on record regardless of report period.",
            ST["note"]
        ))
        story.append(Paragraph(
            "<b><u>Most Purchased %</u></b> is the share of a customer's total units in their "
            "single most-purchased category.",
            ST["note"]
        ))
    else:
        story.append(KeepTogether([lifetime_header, _no_data()]))
    return story

# ── Section 6b: Revenue by Region ─────────────────────────────────────────────
def _sec_regions(m):
    header = _sec("Revenue by Region") + [
        Paragraph(
            "Domestic sales break out by state, international sales by country, "
            "the same view used to gauge where online revenue has grown enough to "
            "justify the cost of an in-person pop-up or exhibition.",
            ST["note"]
        ),
        Spacer(1, 6),
    ]
    reg = m["regions"]
    if not reg:
        return [KeepTogether(header + [_no_data()])]

    total_rev   = sum(d["revenue"] for d in reg.values())
    total_units = sum(d["units"]   for d in reg.values())
    rows = []
    # "Unknown" (no recorded customer country/state) sorts last regardless
    # of revenue -- it isn't a real market to plan a pop-up or exhibition
    # around, so it shouldn't be able to outrank one that is, the way it
    # otherwise would purely by being the single largest revenue bucket.
    for name, d in sorted(reg.items(), key=lambda x: (x[0] != "Unknown", x[1]["revenue"]),
                           reverse=True):
        share = d["revenue"] / total_rev * 100 if total_rev else 0
        rows.append([name, str(d["units"]), _usd(d["revenue"]), f"{share:.1f}%"])

    story = _sec_table(
        header, ["Region","Units","Revenue","Revenue Share"], rows,
        [CONTENT_W*.35, CONTENT_W*.2, CONTENT_W*.25, CONTENT_W*.2],
        totals=["Total", str(total_units), _usd(total_rev), "100.0%"],
        center_cols={1, 2, 3}
    )
    if "Unknown" in reg:
        unk = reg["Unknown"]
        unk_unit_pct = unk["units"] / total_units * 100 if total_units else 0
        story.append(Paragraph(
            f"<b><u>Unknown</u></b> reflects units with no recorded customer country or "
            f"state, the same legacy data gap noted in Lifetime Customer "
            f"Intelligence, not a real geographic market, which is why it's sorted "
            f"below the named regions rather than ranked among them. It accounts "
            f"for <b>{unk['units']} of {total_units} lifetime units sold "
            f"({unk_unit_pct:.0f}%)</b>. The known regions' shares above are "
            f"calculated against total revenue including this gap, not adjusted to "
            f"exclude it, since there's no way to know whether those missing units "
            f"would actually mirror the same geographic split, and assuming they "
            f"would could overstate how concentrated the real customer base is.",
            ST["note"]
        ))
    return story

# ── Section 7: Category Performance ───────────────────────────────────────────
def _sec_categories(m):
    header = _sec("Category Performance")
    cats  = m["cats"]
    col_widths = [CONTENT_W*.28, CONTENT_W*.1, CONTENT_W*.17,
                  CONTENT_W*.17, CONTENT_W*.13, CONTENT_W*.15]
    col_headers = ["Weave Type","Units","Revenue","Gross Profit","Margin %","Share"]

    if not cats:
        story = [KeepTogether(header + [_no_data()])]
    else:
        t_rev = m["rev"]
        rows  = []
        for c, d in sorted(cats.items(), key=lambda x:x[1]["revenue"], reverse=True):
            marg  = d["gp"]/d["revenue"]*100 if d["revenue"] else 0
            share = d["revenue"]/t_rev*100    if t_rev        else 0
            rows.append([c, str(d["units"]), _usd(d["revenue"]),
                         _warn_neg(_usd(d["gp"], deduction=d["gp"] < 0), d["gp"] < 0),
                         _warn_neg(f"{marg:.1f}%", marg < 0), f"{share:.1f}%"])
        # min_rows capped at 20 (not literally len(rows)): protects the whole
        # table + note together for any realistic month, while staying safely
        # under a full page's height in the extreme case of a very
        # category-diverse period, where CondPageBreak's threshold must never
        # exceed what a fresh page can actually satisfy.
        note = Paragraph(
            "Reflects sales within this report period only. See Lifetime "
            "Category Performance below for all-time trends across the full catalogue.",
            ST["note"]
        )
        margin_note = _split_note(
            "A negative margin, shown in rose, means that line sold at a net loss "
            "overall: one or more units went out priced below what it cost to "
            "acquire them, so there was no profit to recover, only a loss. Because "
            "these are blended figures, even a single below-cost sale pulls the "
            "whole line's average down, not just its own. <b>Why it matters:</b> "
            "margin discipline is part of what luxury boutique positioning actually "
            "requires, not separate from it. A rose-colored figure here is worth "
            "tracing back to the specific pricing decision behind it before it "
            "repeats, since no amount of competitive pricing offsets selling below "
            "cost."
        )
        story = _sec_table(
            header, col_headers, rows, col_widths,
            totals=["Total", str(m["units"]), _usd(t_rev),
                    _usd(m["gp"]), _pct(m["margin"]), "100.0%"],
            min_rows=min(len(rows), 20), trailing=[note, *margin_note],
            center_cols={1, 2, 3, 4, 5}
        )

    # Lifetime Category Performance — unlike the customer table, this has 100%
    # data coverage (Weave Type and Selling Price are populated on every
    # historical sold row), so no completeness caveat is needed here.
    cats_life = m["cats_life"]
    cat_acquired = m["cat_acquired"]
    lifetime_header = Paragraph("Lifetime Category Performance", ST["sub_head"])
    story.append(Spacer(1, 12))

    if cats_life:
        t_rev_life     = sum(d["revenue"] for d in cats_life.values())
        t_units_life   = sum(d["units"] for d in cats_life.values())
        t_gp_life      = sum(d["gp"] for d in cats_life.values())
        t_acquired_life = sum(cat_acquired.get(c, 0) for c in cats_life)

        def _cat_sell_through(c, d):
            acquired = cat_acquired.get(c, 0)
            return d["units"] / acquired * 100 if acquired else None

        # Same reliability floor as Supplier Performance, and the same
        # reasoning: a weave type with only 1-4 units ever acquired can
        # land at 100% or 0% purely by chance (e.g. Velvet Silk Blouse:
        # 1 acquired, 1 sold, a "perfect" rate that says nothing about
        # whether that collection genuinely sells well). 10 units is a
        # real break in this business's own data here too.
        MIN_CATEGORY_SAMPLE = 10
        def _cat_sort_key(item):
            c, d = item
            acquired = cat_acquired.get(c, 0)
            reliable = acquired >= MIN_CATEGORY_SAMPLE
            st = _cat_sell_through(c, d) if reliable else 0
            return (reliable, st if st is not None else 0, acquired)

        life_headers = ["Weave Type","Units","Sell-Through Rate","Revenue",
                         "Gross Profit","Margin %","Share"]
        rows_life = []
        # Ranked by Sell-Through Rate, matching both the dashboard's own
        # Collection Sell-Through Rate chart (sorted the same way) and
        # Supplier Performance elsewhere in this report -- revenue
        # leadership is already covered by the period-scoped Category
        # Performance table above and the report's own KPI cards.
        for c, d in sorted(cats_life.items(), key=_cat_sort_key, reverse=True):
            marg  = d["gp"]/d["revenue"]*100 if d["revenue"] else 0
            share = d["revenue"]/t_rev_life*100 if t_rev_life else 0
            st_rate = _cat_sell_through(c, d)
            rows_life.append([c, str(d["units"]),
                               f"{st_rate:.1f}%" if st_rate is not None else "—",
                               _usd(d["revenue"]),
                               _warn_neg(_usd(d["gp"], deduction=d["gp"] < 0), d["gp"] < 0),
                               _warn_neg(f"{marg:.1f}%", marg < 0), f"{share:.1f}%"])
        blended_st_life = (t_units_life / t_acquired_life * 100
                            if t_acquired_life else None)
        life_totals = ["Total", str(t_units_life),
                        f"{blended_st_life:.1f}%" if blended_st_life is not None else "—",
                        _usd(t_rev_life), _usd(t_gp_life),
                        f"{(t_gp_life/t_rev_life*100 if t_rev_life else 0):.1f}%", "100.0%"]
        life_widths = _smart_widths(life_headers, rows_life, protect={"Weave Type"},
                                     totals=life_totals)
        story += _sec_table(
            [lifetime_header], life_headers, rows_life, life_widths, compact=True,
            totals=life_totals, center_cols={1, 2, 3, 4, 5, 6}
        )
        story.append(Paragraph(
            "Includes all transactions on record regardless of report period, "
            "aggregated by weave type to show which categories have performed "
            "best historically.",
            ST["note"]
        ))
        story += _split_note(
            "<b><u>Sell-Through Rate</u></b> measures how much of what's been acquired in "
            "a weave type has actually sold. <b>What's healthy:</b> a high rate means "
            "that collection is resonating with customers and worth allocating more "
            "toward in future intake; a low rate means too much of it is sitting "
            "unsold, worth reconsidering before reordering more of it, rather than "
            f"restocking by habit. <b>How categories are ranked:</b> a weave type with "
            f"only a handful of units ever acquired can land at 100% or 0% purely by "
            f"chance, so categories with fewer than {MIN_CATEGORY_SAMPLE} units "
            f"acquired are sorted to the bottom by volume instead of competing for "
            f"the top of this ranking on a rate that isn't yet a reliable signal."
        )
        story += _split_note(
            "This is a lifetime figure, not scoped to this report period: lifetime "
            "units sold ÷ lifetime units ever acquired in that weave type. "
            "<b>Also see:</b> two other, differently-scoped versions of Sell-Through "
            f'Rate also appear in this report: a period-scoped, whole-business figure in '
            f'<a href="#inventory_flow" color="{COLOUR["gold"]}"><u><b>Inventory Flow</b></u></a>, '
            f'and a lifetime, per-supplier figure in '
            f'<a href="#supplier_performance" color="{COLOUR["gold"]}">'
            f'<u><b>Supplier Performance</b></u></a>.'
        )
    else:
        story.append(KeepTogether([lifetime_header, _no_data()]))

    return story

# ── Section 7b: Supplier Performance ──────────────────────────────────────────
def _sec_suppliers(m, period_label):
    header = _sec("Supplier Performance", anchor="supplier_performance")
    sup = m["suppliers"]
    if not sup:
        return [KeepTogether(header + [_no_data()])]

    total_available     = sum(d["available"]     for d in sup.values())
    total_life_units    = sum(d["life_units"]    for d in sup.values())
    total_period_units  = sum(d["period_units"]  for d in sup.values())
    total_life_acquired = sum(d["life_acquired"] for d in sup.values())
    total_life_rev       = sum(d["life_revenue"] for d in sup.values())
    total_life_gp         = sum(d["life_gp"]     for d in sup.values())

    def _sell_through(d):
        return d["life_units"] / d["life_acquired"] * 100 if d["life_acquired"] else None

    # A supplier with only 1-3 units ever acquired can land at 100% or 0%
    # sell-through purely by chance, not because their inventory genuinely
    # moves well or poorly -- ranking on the raw rate alone would let a
    # single lucky (or unlucky) sale outrank suppliers with a real,
    # statistically meaningful track record. 10 units is a real break in
    # this business's own data (every supplier is either under 3 or over
    # 10 acquired, nothing in between), not an arbitrary round number.
    MIN_SUPPLIER_SAMPLE = 10
    def _sup_sort_key(item):
        _, d = item
        acquired = d["life_acquired"]
        reliable = acquired >= MIN_SUPPLIER_SAMPLE
        st = _sell_through(d) if reliable else 0
        return (reliable, st if st is not None else 0, acquired)

    headers = ["Supplier","Available","Lifetime Sold","Sold This Period",
               "Sell-Through Rate","Avg Days to Sell","Avg Margin %"]
    rows = []
    # Ranked by Sell-Through Rate rather than revenue -- surfaces which
    # suppliers' inventory isn't moving first, a sourcing-quality signal,
    # matching how the dashboard's own Supplier Scorecard is ranked.
    # Revenue leadership is already covered by the KPI cards and Category
    # Performance elsewhere in this report. Suppliers below the minimum
    # sample size sort to the bottom (by volume, since their rate isn't a
    # reliable signal) rather than competing for the top of the ranking.
    for name, d in sorted(sup.items(), key=_sup_sort_key, reverse=True):
        avg_dts = d["dts_sum"] / d["dts_count"] if d["dts_count"] else None
        margin  = d["life_gp"] / d["life_revenue"] * 100 if d["life_revenue"] else None
        st_rate = _sell_through(d)
        rows.append([
            name,
            str(d["available"]),
            str(d["life_units"]),
            str(d["period_units"]),
            f"{st_rate:.1f}%" if st_rate is not None else "—",
            f"{avg_dts:.0f}" if avg_dts is not None else "—",
            _warn_neg(f"{margin:.1f}%", margin < 0) if margin is not None else "—",
        ])

    # Supplier keeps its full natural width so a long name never wraps; the
    # remaining numeric columns share whatever space is left, same approach
    # used for the Lifetime Customer Intelligence table.
    blended_margin = total_life_gp / total_life_rev * 100 if total_life_rev else 0
    blended_st = (total_life_units / total_life_acquired * 100
                  if total_life_acquired else None)
    total_dts = m["total_dts_units"]
    total_sold_life = m["total_lifetime_sold"]
    dts_coverage_pct = total_dts / total_sold_life * 100 if total_sold_life else 0
    sup_totals = ["Total", str(total_available), str(total_life_units),
                   str(total_period_units),
                   f"{blended_st:.1f}%" if blended_st is not None else "—",
                   "—", f"{blended_margin:.1f}%"]
    widths = _smart_widths(headers, rows, protect={"Supplier"}, totals=sup_totals)

    story = _sec_table(
        header, headers, rows, widths, compact=True,
        totals=sup_totals,
        center_cols={1, 2, 3, 4, 5, 6},
    )
    story.append(Paragraph(
        f"<b><u>Available</u></b> reflects current inventory; Lifetime figures cover all units "
        f"on record; <b>Sold This Period</b> is the only column scoped to {period_label}.",
        ST["note"]
    ))
    story += _split_note(
        "<b><u>Sell-Through Rate</u></b> measures how much of what's been acquired from a "
        "supplier has actually sold. <b>What's healthy:</b> a high rate means that "
        "supplier's units are resonating with customers and converting to revenue; "
        "a low rate means too much of what's sourced from them is sitting unsold, "
        "worth investigating whether it's a fit problem with what's being ordered, "
        "or simply too much volume relative to demand. <b>How suppliers are ranked:</b> "
        f"a supplier with only a handful of units ever acquired can land at 100% or "
        f"0% purely by chance, not because their inventory genuinely moves well or "
        f"poorly, so suppliers with fewer than {MIN_SUPPLIER_SAMPLE} units acquired "
        f"are sorted to the bottom by volume instead of competing for the top of "
        f"this ranking on a rate that isn't yet a reliable signal."
    )
    story += _split_note(
        f"This is a lifetime figure, not scoped to {period_label}: lifetime units sold "
        f"÷ lifetime units ever acquired from that supplier. <b>Also see:</b> a "
        f"different, period-scoped Sell-Through Rate for the whole business (not per "
        f'supplier) appears later in '
        f'<a href="#inventory_flow" color="{COLOUR["gold"]}"><u><b>Inventory Flow</b></u></a>.'
    )
    story += _split_note(
        f"<b><u>Avg Days to Sell</u></b> here is a lifetime figure, not scoped to {period_label}: "
        f"the average number of days between when a unit was acquired and when it sold, "
        f"based on {total_dts} of {total_sold_life} lifetime sold units "
        f'({dts_coverage_pct:.0f}%) with a recorded sell date; suppliers with no '
        f'qualifying units show "—". <b>What\'s healthy:</b> a lower number means that '
        f"supplier's units typically sell faster once in stock; a higher number means "
        f"they tend to sit longer. <b>Also see:</b> a different, period-scoped Avg "
        f'Days to Sell for the whole business appears later in '
        f'<a href="#inventory_flow" color="{COLOUR["gold"]}">'
        f'<u><b>Inventory Flow</b></u></a>.'
    )
    return story

# ── Section 8: Inventory Flow ──────────────────────────────────────────────────
def _sec_inv_flow(m, period_type):
    # Outflow gets accounting-style parentheses, inflow stays a plain number —
    # same convention already used for COGS and other deductions in Revenue &
    # Profit, rather than the +/-/= prefixes used before. No label prefixes
    # needed either: "Units Sold" vs "Units Added" already reads as outflow
    # vs inflow without a symbol, matching how that ledger table's own labels
    # (e.g. "Cost of Goods Sold") carry no directional marker either.
    units_sold_disp = f"({m['units']})"
    # Closing Stock sits right after the three rows that actually sum to
    # it (Opening + Added - Sold), so the ledger reads correctly top to
    # bottom without any mental adjustment. Cancellations Restored moves
    # below the total -- it's real, useful context (how many units came
    # back to available this period) but it's already folded into
    # Opening Stock or Units Added above once a sale is reversed, so it
    # was never actually part of the Closing Stock math (see the note
    # this section renders below the table).
    rows = [
        ["Opening Stock",          str(m["op_cnt"])],
        ["Units Added",            str(m["add_cnt"])],
        ["Units Sold",             units_sold_disp],
        ["Closing Stock",          str(m["cl_cnt"])],
        ["Cancellations Restored", str(m["cancel_cnt"])],
    ]
    inv_flow_note = Paragraph(
        "<b><u>Cancellations Restored</u></b> reflects units returned to available "
        "stock this period, already accounted for within Opening Stock or "
        "Units Added above, not an additional adjustment to Closing Stock.",
        ST["note"]
    )
    heading = _sec("Inventory Flow", anchor="inventory_flow") + [
        # No scorecard for Opening/Added/Sold/Cancellations/Closing here —
        # the ledger table below already shows all five, and does it better:
        # top-to-bottom as a reconciliation with Closing Stock bolded and
        # highlighted (key_rows={3}) as the derived result, which flat cards
        # can't convey. Restating the same five numbers as tiles first would
        # be pure duplication with no new information, unlike every other
        # scorecard+table pairing in this report where the table adds detail
        # the scorecard doesn't show.
        #
        # Key Retail Metrics folded in here rather than living in its own
        # section — Sell-Through Rate and Avg Days to Sell are the velocity
        # counterpart to the stock counts below (same underlying
        # inventory-flow data, viewed as rate instead of count), and a
        # standalone section for just these two numbers paid a full
        # heading+rule's worth of page space for minimal content. Both notes
        # travel with the cards into this same heading block so they can
        # never end up separated from the metrics they explain.
        _metric_row([
            ("Sell-Through Rate", f"{m['sell_through']*100:.1f}%"),
            ("Avg Days to Sell",  f"{m['avg_dts']:.0f}"),
            ("Inventory Turnover",f"{m['turnover']:.2f}x"),
        ]),
        Spacer(1, 6),
        *_split_note(
            "<b><u>Sell-Through Rate</u></b> compares units sold against Opening Stock: how much "
            "of what the business started the period with actually sold, for the whole "
            "business this period only. <b>What's healthy:</b> a higher rate means "
            "inventory is moving efficiently relative to what's on hand; a consistently "
            "low rate can mean overstocking, weak demand for what's currently available, "
            "or both. <b>Also see:</b> a different, lifetime, per-supplier version of "
            "Sell-Through Rate appears "
            f'earlier in <a href="#supplier_performance" color="{COLOUR["gold"]}">'
            f'<u><b>Supplier Performance</b></u></a>.'
        ),
        *_split_note(
            "<b><u>Avg Days to Sell</u></b> is the average number of days between when a unit "
            "was acquired and when it sold, for units sold this period only: how "
            "quickly the business is currently converting stock to revenue. "
            "<b>What's healthy:</b> a lower number means units are moving faster off "
            "the shelf; a rising number over time is worth watching alongside "
            "Inventory Turnover. <b>Also see:</b> a different, lifetime, per-supplier "
            "version of Avg Days to Sell appears earlier in "
            f'<a href="#supplier_performance" color="{COLOUR["gold"]}">'
            f'<u><b>Supplier Performance</b></u></a>.'
        ),
        *_split_note(
            "<b><u>Inventory Turnover</u></b> measures how many times stock cycled through sales "
            "this period, comparing units sold against the average amount on hand. "
            "<b>What's healthy:</b> a higher ratio means efficient, "
            "fast-moving inventory, but pushed too far it signals stock is thin enough to "
            "risk running out of popular pieces before they can be restocked. A lower ratio "
            "means inventory is accumulating faster than it sells, tying up capital in stock "
            f"that isn't converting. A low ratio can also simply mean the business is "
            f"restocking heavily, not that inventory is moving slowly, so check it alongside "
            f"Sell-Through Rate for the fuller picture. Because the ratio scales with period "
            f"length, only compare it against other {period_type} reports."
            + (f" <b>In plain terms:</b> at this period's pace, it would take roughly "
               f"<b>{m['months_on_hand']:.0f} months ({m['months_on_hand']/12:.1f} years)</b> "
               f"to sell through everything "
               f"currently in stock. Because Lakshira carries a broad, varied selection "
               f"by design rather than fast-turnover retail stock, a large number here "
               f"isn't itself a warning sign, and there's no target to chase -- what "
               f"matters is the direction it moves over time. "
               f"<b>Also see:</b> whether that pace is concentrated in specific aging "
               f"units, which "
               f'<a href="#aging_inventory" color="{COLOUR["gold"]}"><u><b>Aging Inventory</b></u></a> '
               f"below breaks down directly." if m["months_on_hand"] is not None else "")
        ),
        Spacer(1, 6),
    ]
    return _sec_table(heading, ["","Units"], rows, [CONTENT_W*.7, CONTENT_W*.3],
                       key_rows={3}, trailing=[inv_flow_note])

# ── Section 9: Inventory Snapshot ─────────────────────────────────────────────
def _sec_snapshot(m, gen_date_str):
    sc   = m["snap"]
    rows = [[s, str(c)] for s,c in sorted(sc.items(), key=lambda x:x[1], reverse=True)]
    total_units = sum(sc.values())
    heading = _sec("Inventory Snapshot") + [
        Paragraph(
            f"A real-time census of every unit by status as of {gen_date_str}, sold and "
            f"unsold traced through from units to dollars.",
            ST["note"]
        ),
        Spacer(1, 6),
        _metric_row([
            ("Units Sold",          str(m["total_lifetime_sold"])),
            ("Revenue Generated",   _usd_k(m["total_lifetime_revenue"])),
            ("Outstanding Balance", _usd_k(m["total_out"])),
            ("Unsold Units",        str(m["unsold_cnt"])),
            ("Cash Exposure",       _usd_k(m["cash_exposure"])),
        ]),
        Spacer(1, 6),
        *_split_note(
            "<b><u>Cash Exposure</u></b> is the total cost basis (what it cost to acquire, "
            "not a confirmed loss) tied up in every unsold unit (Available and "
            "Reserved). <b>Why it matters:</b> it's capital the business has already "
            "spent and can't reinvest until those units sell; a rising figure means "
            "more cash is parked in inventory rather than working for the business."
        ),
        Spacer(1, 10),
    ]
    story = _sec_table(heading, ["Status","Units"], rows,
                        [CONTENT_W*.7, CONTENT_W*.3],
                        totals=["Total", str(total_units)])
    story.append(Spacer(1, 10))

    below = m["below_cost_rows"]
    if not below:
        story.append(Paragraph(
            "No units are currently priced below cost.", ST["note"]
        ))
        return story

    # BURNT_ORANGE, the report's most severe tier (same as Dead Stock): a
    # unit priced below cost is a guaranteed loss the moment it sells, so it
    # shouldn't be marketed or quoted at its current price until repriced --
    # that's a higher-urgency flag than a routine at-risk figure like
    # Outstanding Balance. Severity reads through the value color alone
    # (not a fully flooded card): the callout right below already claims
    # the flooded-BURNT_ORANGE treatment for the actual instruction, and
    # having both look identical made it hard to tell "here's a number"
    # apart from "here's something you must act on."
    story.append(_metric_row([
        ("Units Selling Below Cost", str(m["below_cost_cnt"])),
        ("Below Cost Cash Exposure", _usd_k(m["below_cost_exposure"])),
    ], value_color=BURNT_ORANGE))
    story.append(Spacer(1, 10))

    bc_rows = []
    loss_total = 0.0
    for r in sorted(below, key=lambda r: _flt(r.get("Total Cost (USD)"))
                                          - _flt(r.get("Selling Price (USD)")), reverse=True):
        price = _flt(r.get("Selling Price (USD)"))
        cost  = _flt(r.get("Total Cost (USD)"))
        loss  = cost - price
        loss_total += loss
        bc_rows.append([
            r.get("SKU",""),
            r.get("Weave Type / Cluster",""),
            _usd(price),
            _usd(cost),
            _usd(loss, deduction=True),
        ])
    bc_headers = ["SKU","Weave Type","Price","Cost","Loss"]
    # Weave Type is the one flexible column, same reasoning as the Dead Stock
    # detail table: a wrapped collection name still reads fine, a wrapped
    # SKU or currency figure doesn't.
    bc_totals = ["Total", "", "", _usd(m["below_cost_exposure"]), _usd(loss_total, deduction=True)]
    bc_widths = _smart_widths(bc_headers, bc_rows, protect={"SKU","Price","Cost","Loss"},
                               totals=bc_totals)
    story += _sec_table(
        [_callout(
            "These units should not be marketed, or quoted at their current price "
            "if a customer inquires, until repriced. Sorted by loss, largest first.",
            bg=BURNT_ORANGE
        ), Spacer(1, 8)],
        bc_headers, bc_rows, bc_widths, compact=True,
        totals=bc_totals, left_cols={1}, center_cols={2, 3, 4}
    )
    return story

# ── Section 10: Aging Inventory ───────────────────────────────────────────────
def _sec_aging(m):
    buckets = m["aging"]
    labels  = ["0–30","31–60","61–90","91–180","180+"]
    bucket_tags = {
        "0–30":   "Fresh",
        "31–60":  "Normal",
        "61–90":  "Slowing",
        "91–180": "Aging",
        "180+":   "⚠  Dead Stock Watch",
    }
    rows = [[f"{lb} Days: {bucket_tags[lb]}", str(len(buckets[lb]))] for lb in labels]
    heading = _sec("Aging Inventory", anchor="aging_inventory") + [
        Paragraph(
            "Available units only, grouped by days in stock, surfacing which unsold "
            "pieces are at growing risk of tying up capital the longer they go unsold.",
            ST["note"]
        ),
        Spacer(1, 4),
    ]
    story = _sec_table(heading, ["Aging Bucket","Units"], rows, [CONTENT_W*.7, CONTENT_W*.3],
                        totals=["Total", str(sum(len(buckets[lb]) for lb in labels))])
    story.append(Spacer(1, 10))

    dead = buckets["180+"]
    if dead:
        # BURNT_ORANGE marks this as the report's most severe tier, distinct
        # from the routine ROSE used for outstanding balances elsewhere --
        # 180+ days is the same "more painful inflection point" the dashboard
        # weights toward in Inventory Age Distribution, so it earns a color
        # one step past the standard attention-needed flag.
        callout_block = [_callout(
            f"{len(dead)} unit{'s' if len(dead)!=1 else ''} "
            f"{'have' if len(dead)!=1 else 'has'} been in inventory for over 180 days. "
            f"Consider restaging across marketing platforms and developing stronger unit "
            f"narratives before any pricing adjustment.",
            bg=BURNT_ORANGE
        ), Spacer(1, 8)]

        today = date.today()
        ds_rows = []
        # Every row here already passed _metrics()'s aging filter (which
        # excludes blank/malformed/future Date Acquired from every bucket,
        # "180+" included), so _days_since() can't actually return None on
        # this list today -- routed through it anyway so this stays safe if
        # that upstream filter ever changes, instead of silently
        # reintroducing a negative day count into an emailed report.
        def _dead_sort_key(x):
            d = _days_since(_parse_date(x.get("Date Acquired","")), reference=today)
            return -1 if d is None else d

        for r in sorted(dead, key=_dead_sort_key, reverse=True):
            d    = _days_since(_parse_date(r.get("Date Acquired","")), reference=today)
            days = str(d) if d is not None else "—"
            mp   = _flt(r.get("(Profit) Margin %"))   # sheet stores as %, not decimal
            ds_rows.append([
                r.get("SKU",""),
                r.get("Weave Type / Cluster",""),
                r.get("Date Acquired",""),
                days,
                _usd(_flt(r.get("Selling Price (USD)"))),
                _usd(_flt(r.get("Total Cost (USD)"))),
                _warn_neg(f"{mp:.1f}%", mp < 0),
            ])
        # Callout kept with at least the start of the detail table (not the
        # entire 768-row table) — so this only fills whatever space remains
        # on the current page rather than deferring the whole block to a
        # fresh page whenever it's too tall to fit in full.
        # Weave Type gets the lion's share of the row width — some catalogue
        # names (e.g. "Finest Contemporary Kanjivaram Silk") run ~147pt at
        # this font size, well past what the old 22% share (~99pt usable)
        # could hold on one line. The other columns hold short fixed-format
        # values (codes, dates, currency) with plenty of slack to give up.
        story += _sec_table(
            callout_block,
            ["SKU","Weave Type","Acquired","Days","Price","Cost","Margin"],
            ds_rows,
            [CONTENT_W*.18, CONTENT_W*.335, CONTENT_W*.125,
             CONTENT_W*.06, CONTENT_W*.105, CONTENT_W*.105, CONTENT_W*.09],
            compact=True, center_cols={2}, left_cols={1}
        )
    return story

# ── Section 0b: Period-over-Period (placed right after the exec summary as
#    an up-front benchmark snapshot, not with the other sections below) ───────
def _sec_pop(m, period_label, period_type, ps, pe):
    prior_label = {
        "monthly":   ps.strftime("%B %Y"),
        "quarterly": f"Q{(ps.month-1)//3+1} {ps.year}",
        "annual":    str(ps.year),
        "custom":    f"{ps.strftime('%b %d, %Y')} – {pe.strftime('%b %d, %Y')}",
    }.get(period_type, "Prior Period")

    rows = [
        ["Total Revenue",  _usd(m["p_rev"]),    _usd(m["rev"]),    _chg_colored(m["rev"],  m["p_rev"])],
        ["Gross Profit",   _usd(m["p_gp"]),     _usd(m["gp"]),     _chg_colored(m["gp"],   m["p_gp"])],
        ["Gross Margin %", _pct(m["p_margin"]), _pct(m["margin"]), _chg_colored(m["margin"],m["p_margin"])],
        ["Units Sold",     str(m["p_units"]),   str(m["units"]),   _chg_colored(m["units"],m["p_units"])],
    ]
    # Always exactly 4 fixed rows — safe to protect the whole table + note.
    pop_note = Paragraph(
        "As historical data accumulates, these benchmarks will develop into "
        "reliable targets for assessing seasonal patterns and performance trends.",
        ST["note"]
    )
    story = _sec_table(
        _sec("Period-over-Period Comparison"),
        ["Metric", prior_label, period_label, "Change"],
        rows,
        [CONTENT_W*.28, CONTENT_W*.24, CONTENT_W*.24, CONTENT_W*.24],
        min_rows=len(rows), trailing=[pop_note]
    )
    return story

# ── PDF assembly ───────────────────────────────────────────────────────────────
def _build_pdf(pdf_path, period_label, report_type, period_type,
               ps, pe, metrics, claude_text, gen_date_str):

    on_page = _page_cb(period_label, report_type)

    doc = BaseDocTemplate(
        pdf_path, pagesize=LETTER,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
    )

    cover_frame = Frame(0, 0, PAGE_W, PAGE_H, id="cover",
                        leftPadding=0, rightPadding=0,
                        topPadding=0,  bottomPadding=0)
    content_frame = Frame(
        MARGIN, MARGIN + 20,
        CONTENT_W, PAGE_H - 2*MARGIN - 48,
        id="content"
    )

    def cover_cb(canvas, doc):
        _draw_cover(canvas, doc, period_label, report_type, gen_date_str)

    doc.addPageTemplates([
        PageTemplate(id="Cover",   frames=[cover_frame],   onPage=cover_cb),
        PageTemplate(id="Content", frames=[content_frame], onPage=on_page),
    ])

    story = [
        Spacer(1, 1),                  # occupies cover page frame
        NextPageTemplate("Content"),
        PageBreak(),
    ]
    story += _sec_exec(claude_text)
    story += _sec_pop(metrics, period_label, period_type, ps, pe)
    story += _sec_revenue(metrics)
    story += _sec_sales(metrics)
    story += _sec_discounts(metrics)
    story += _sec_channels(metrics)
    story += _sec_purchase_behavior(metrics)
    story += _sec_outstanding(metrics)
    story += _sec_customers(metrics)
    story += _sec_regions(metrics)
    story += _sec_categories(metrics)
    story += _sec_suppliers(metrics, period_label)
    story += _sec_inv_flow(metrics, period_type)
    story += _sec_snapshot(metrics, gen_date_str)
    story += _sec_aging(metrics)

    doc.build(story)

# ── Email ──────────────────────────────────────────────────────────────────────
def _send(pdf_path, period_label, report_type, m):
    if EMAIL_SENDER.startswith("PLACEHOLDER"):
        print("\033[2m  Email not configured — PDF saved locally.\033[0m")
        return
    if not EMAIL_RECIPIENTS:
        # Catch this before any SMTP connection/login work starts --
        # otherwise an empty list reaches smtplib's own recipient loop,
        # which raises SMTPRecipientsRefused({}) with a completely empty
        # error dict (0 refused out of 0 attempted still counts as "all
        # refused"), giving a blank, unhelpful "Email failed: {}." message
        # instead of a clear explanation.
        print(f"\033[2m  No email recipients configured — PDF saved locally at {pdf_path}.\033[0m")
        return
    try:
        headline = (
            f"This period: {_usd(m['rev'])} revenue "
            f"({_chg(m['rev'], m['p_rev'])} vs. prior period), "
            f"{_pct(m['margin'])} gross margin."
        )
        msg            = MIMEMultipart()
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = ", ".join(EMAIL_RECIPIENTS)
        msg["Subject"] = f"Lakshira {report_type}: {period_label}"
        msg.attach(MIMEText(
            f"Hello,\n\n"
            f"Please find attached the Lakshira {report_type} for {period_label}.\n\n"
            f"{headline}\n\n"
            f"This report was generated automatically by the Lakshira Inventory "
            f"Management System and is confidential, for internal use only.\n\n"
            f"Lakshira IMS", "plain"
        ))
        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="{os.path.basename(pdf_path)}"')
        msg.attach(part)
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
        print(f"\033[2m  Emailed to {len(EMAIL_RECIPIENTS)} recipient(s).\033[0m")
    except Exception as e:
        print(f"\033[33m  ⚠  Email could not be sent ({e}). The PDF was still saved at {pdf_path}.\033[0m")

# ── Main entry ─────────────────────────────────────────────────────────────────
def generate_report(period_type: str, start: date, end: date,
                    period_label: str, send_email: bool = True,
                    mode: str = "live") -> str:
    """
    Generate the Lakshira Business Intelligence Report.

    Args:
        period_type:  'monthly' | 'quarterly' | 'annual' | 'custom'
        start:        first day of the report period
        end:          last day of the report period
        period_label: e.g. 'July 2026', 'Q2 2026', '2026', 'Jan 01, 2026 – Aug 11, 2026'
        send_email:   whether to email after generating
        mode:         'live' or 'test' — which sheet to read from

    Returns:
        Absolute path to the generated PDF.
    """
    gen_date     = date.today()
    gen_date_str = gen_date.strftime("%B %d, %Y")
    # Explicit up-front check rather than a per-dict fallback -- otherwise
    # type_label's .get(..., "Report") default and subfolder's plain [..]
    # indexing would disagree on what happens for an unrecognized
    # period_type (one silently degrades, the other crashes). One check
    # here means both lookups below are safe by construction.
    if period_type not in ("monthly", "quarterly", "annual", "custom"):
        raise ValueError(f"generate_report(): unrecognized period_type {period_type!r}")
    type_label   = {"monthly":"Monthly Report",
                    "quarterly":"Quarterly Report",
                    "annual":"Annual Report",
                    "custom":"Custom Range Report"}[period_type]
    subfolder    = {"monthly":"Monthly","quarterly":"Quarterly",
                    "annual":"Annual","custom":"Custom"}[period_type]
    safe         = period_label.replace(" ","_").replace("/","-")
    pdf_name     = (f"Lakshira_{type_label.replace(' ','_')}"
                    f"_{safe}_{gen_date.strftime('%Y%m%d')}.pdf")
    os.makedirs(os.path.join(REPORTS_DIR, subfolder), exist_ok=True)
    pdf_path     = os.path.join(REPORTS_DIR, subfolder, pdf_name)

    # Regenerating the same report later the same day would otherwise
    # silently overwrite the earlier PDF with no trace it ever existed --
    # append v2, v3, ... instead of replacing it.
    if os.path.exists(pdf_path):
        _base, _ext = os.path.splitext(pdf_path)
        _v = 2
        while os.path.exists(f"{_base}_v{_v}{_ext}"):
            _v += 1
        pdf_path = f"{_base}_v{_v}{_ext}"

    ps, pe = _prior(period_type, start, end)

    print("\n\033[2m  Fetching data...\033[0m")
    data = _load(start, end, ps, pe, mode)

    print("\033[2m  Computing metrics...\033[0m")
    metrics = _metrics(data, start, end, ps, pe, period_type)

    print("\033[2m  Generating executive summary...\033[0m")
    claude_text = _claude_summary(metrics, period_label, type_label)

    print("\033[2m  Building PDF...\033[0m")
    _build_pdf(pdf_path, period_label, type_label, period_type,
               ps, pe, metrics, claude_text, gen_date_str)

    # No "Saved: ..." print here -- inventory.py's generate_report_menu()
    # already shows the saved path once, right under "Report complete.",
    # which is where an interactive user should see it. scheduler.py (the
    # automated path) doesn't rely on this print either -- it logs the
    # returned path itself.
    if send_email:
        print("\033[2m  Sending email...\033[0m")
        _send(pdf_path, period_label, type_label, metrics)

    return pdf_path
