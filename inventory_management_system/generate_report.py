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

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, NextPageTemplate, KeepTogether, CondPageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from report_config import (
    LOGO_PATH, FONT_PATHS, COLOUR, REPORTS_DIR, BASE_DIR,
    ANTHROPIC_API_KEY, EMAIL_SENDER, EMAIL_PASSWORD,
    EMAIL_RECIPIENTS, SMTP_SERVER, SMTP_PORT,
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

    # Standard tables: headers bold, body regular (not Light), bold rows for emphasis
    "th_l":    _s("th_l",    fontName="Montserrat-Bold",  fontSize=8,   textColor=white,      alignment=TA_LEFT,  leading=11),
    "th_r":    _s("th_r",    fontName="Montserrat-Bold",  fontSize=8,   textColor=white,      alignment=TA_RIGHT, leading=11),
    "td_l":    _s("td_l",    fontName="Montserrat-Bold",   fontSize=8.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=12),
    "td_r":    _s("td_r",    fontName="Montserrat-Bold",   fontSize=8.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=12),
    "td_bl":   _s("td_bl",   fontName="Montserrat-Bold",  fontSize=8.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=12),
    "td_br":   _s("td_br",   fontName="Montserrat-Bold",  fontSize=8.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=12),
    # Compact variants for wide tables — same weight progression, tighter size
    "th_l_sm": _s("th_l_sm", fontName="Montserrat-Bold",  fontSize=7,   textColor=white,      alignment=TA_LEFT,  leading=10),
    "th_r_sm": _s("th_r_sm", fontName="Montserrat-Bold",  fontSize=7,   textColor=white,      alignment=TA_RIGHT, leading=10),
    "td_l_sm": _s("td_l_sm", fontName="Montserrat-Bold",   fontSize=7.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=11),
    "td_r_sm": _s("td_r_sm", fontName="Montserrat-Bold",   fontSize=7.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=11),
    "td_bl_sm":_s("td_bl_sm",fontName="Montserrat-Bold",  fontSize=7.5, textColor=DARK_BROWN, alignment=TA_LEFT,  leading=11),
    "td_br_sm":_s("td_br_sm",fontName="Montserrat-Bold",  fontSize=7.5, textColor=DARK_BROWN, alignment=TA_RIGHT, leading=11),

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
def _metrics(data, start, end, ps, pe):
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
    cust_life = defaultdict(lambda: {"units":0,"spend":0.0,"cats":defaultdict(int),"name":""})
    for r in all_sold:
        n = r.get("Customer Name","").strip()
        if n:
            key = _customer_key(r)
            cust_life[key]["units"] += 1
            cust_life[key]["spend"] += _flt(r.get("Actual Selling Price (USD)"))
            cust_life[key]["name"] = n
            cat = r.get("Weave Type / Cluster","").strip()
            if cat: cust_life[key]["cats"][cat] += 1

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

    # Supplier performance — mostly lifetime by design: with ~52 units sold in
    # a typical month spread across 18 suppliers, a per-period sell-through
    # rate per supplier would be based on samples too thin to be meaningful.
    # Available Units is a real-time snapshot; Units Sold This Period is a raw
    # count (fine even when small); everything else is lifetime.
    suppliers = defaultdict(lambda: {
        "available": 0, "period_units": 0,
        "life_units": 0, "life_revenue": 0.0, "life_gp": 0.0,
        "dts_sum": 0.0, "dts_count": 0,
    })
    for r in all_r:
        if r.get("Status","").strip() == "Available":
            sup = r.get("Supplier","").strip() or "Unknown"
            suppliers[sup]["available"] += 1
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
    opening = [r for r in all_r
               if r.get("Status","").strip() in active
               and _parse_date(r.get("Date Acquired",""))
               and _parse_date(r.get("Date Acquired","")) < start
               and (not _parse_date(r.get("Date Sold",""))
                    or _parse_date(r.get("Date Sold","")) >= start)]
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

    # Unsold inventory at-cost value (Available units only)
    unsold_val = sum(_flt(r.get("Total Cost (USD)")) for r in all_r
                     if r.get("Status","").strip() == "Available")

    return dict(
        rev=rev, cost=cost, gp=gp, margin=margin,
        p_rev=p_rev, p_gp=p_gp, p_margin=p_margin, p_units=len(prior),
        cancel_lost=cancel_lost, cancel_cnt=can_cnt,
        units=units, avg_price=avg_price,
        pay_methods=dict(pay_methods),
        disc_rows=disc_rows, disc_lost=disc_lost, avg_disc=avg_disc,
        channels=dict(channels),
        cust_p=dict(cust_p), cust_life=dict(cust_life),
        total_lifetime_sold=len(all_sold),
        total_lifetime_revenue=sum(_flt(r.get("Actual Selling Price (USD)")) for r in all_sold),
        new_c=new_c, ret_c=ret_c, ret_rate=ret_rate, top5_share=top5_share,
        cats=dict(cats), cats_life=dict(cats_life),
        suppliers=dict(suppliers), total_dts_units=total_dts_units,
        op_cnt=op_cnt, add_cnt=add_cnt, cl_cnt=cl_cnt,
        sell_through=sell_through, avg_dts=avg_dts, turnover=turnover,
        snap=dict(snap),
        aging=aging,
        outstanding=data["outstanding"], total_out=total_out, avg_collect_days=avg_collect_days,
        unsold_val=unsold_val,
    )

# ── PDF building helpers ───────────────────────────────────────────────────────
def _sec(title):
    return [
        Spacer(1, 8),
        Paragraph(title.upper(), ST["sec_title"]),
        HRFlowable(width=CONTENT_W, thickness=1.5, color=GOLD, spaceBefore=6, spaceAfter=8),
    ]

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

def _metric_row(items):
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
    """
    n = len(items)
    w = CONTENT_W / n
    avail = w - 12   # minus LEFTPADDING + RIGHTPADDING
    labels = [label for label, _ in items]
    label_style = _fit_label_style(labels, avail, ST["metric_label"])
    label_row = [Paragraph(label.replace(" ", " "), label_style) for label in labels]
    value_row = [Paragraph(value, ST["metric_val"]) for _, value in items]
    t = Table([label_row, value_row], colWidths=[w]*n, rowHeights=[36, 34])
    t.setStyle(TableStyle([
        ("BOX",           (0,0),(-1,-1), 1.0, DARK_BROWN),
        # Vertical dividers between cards only — spans both rows, no line
        # between the title and value within a card, so each card reads as
        # one unified container rather than two stacked cells.
        ("LINEAFTER",     (0,0),(-2,-1), 0.8, DARK_BROWN),
        ("BACKGROUND",    (0,0),(-1,-1), OFF_WHITE),
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
         key_rows=None, bold_rows=None):
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
    """
    sfx = "_sm" if compact else ""
    key_rows  = set(key_rows or [])
    bold_rows = set(bold_rows or [])

    def _safe(txt):
        # _Markup values are already safe, pre-built ReportLab markup
        # (e.g. from _warn_neg() or _chg_colored()) -- escaping them would
        # turn their intentional <font color="..."> tags into visible
        # literal text instead of actual color. Everything else is
        # untrusted data and gets escaped as before.
        return txt if isinstance(txt, _Markup) else _xml_escape(txt)
    def h_para(i, txt):
        return Paragraph(_safe(txt), ST[("th_r" if i >= right_from else "th_l") + sfx])
    def d_para(i, txt, bold=False):
        if bold: return Paragraph(_safe(txt), ST[("td_br" if i >= right_from else "td_bl") + sfx])
        return Paragraph(_safe(txt), ST[("td_r" if i >= right_from else "td_l") + sfx])

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
               min_rows=3, trailing=None):
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
                    totals=totals if full_preview else None)
    reserve_h = _stack_height([*heading_flowables, preview, *trailing])

    table = _tbl(headers, rows, widths, totals=totals, right_from=right_from,
                 compact=compact, key_rows=key_rows, bold_rows=bold_rows)

    return [CondPageBreak(reserve_h), *heading_flowables, table, *trailing]

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
            "avg_selling_price": round(m["avg_price"], 2),
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
            "avg_days_outstanding": round(m["avg_collect_days"], 1),
            "units_180_plus_days": len(m["aging"]["180+"]),
            "sell_through_rate_pct": round(m["sell_through"]*100, 1),
            "avg_days_to_sell": round(m["avg_dts"], 1),
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
        }
        brand_context = _load_brand_context()
        brand_context_block = (
            f"\n\nBrand & product context, filled in directly by the founder -- treat this "
            f"as ground truth about how Lakshira actually operates, above any general "
            f"assumptions about luxury retail or Indian textiles:\n\n{brand_context}\n"
            if brand_context else ""
        )

        prompt = f"""You are a senior business analyst writing an executive summary for Lakshira Handwoven Weaves — a US-based luxury boutique specialising in premium Indian handwoven sarees. The business sells through Instagram, WhatsApp, Shopify, and Exhibition/Popup events. The long-term vision is to become a recognised luxury boutique brand.
{brand_context_block}
Business data for {period_label}:
{json.dumps(payload, indent=2)}

Write a concise executive summary followed by specific actionable recommendations. The stakeholder (business owner) is non-technical but business-savvy. Every recommendation must be concrete and implementable — not generic advice. Align recommendations with the luxury boutique vision.

Treat related metrics as connected evidence, not a list of independent facts. When two or more figures in the data reinforce or contradict each other, say so explicitly, so the reader starts to recognize which metrics move together over time — e.g. low inventory turnover paired with a high count of units_180_plus_days is the same underlying problem (stock not moving) confirmed two ways, not two separate problems; a high customer_retention_rate alongside a high top5_customer_revenue_share_pct means loyalty is real but concentrated in very few relationships, which is a different risk than broad-based repeat buying; a strong sell_through_rate_pct with a low avg_days_to_sell tells a consistent fast-moving-stock story, and if one of those two contradicts the other, that contradiction itself is worth naming. Only draw a connection when the data actually supports it — do not force a correlation that isn't there.

FORMAT RULES — follow exactly, no deviations:

SUMMARY:
[Plain business language, 3–4 sentences as a baseline. Every summary must cover these in order:
1. Revenue for the period and how it moved vs. the prior period.
2. Profitability — gross margin.
3. Volume — units sold, AND how this period stacks up against lifetime performance
   (what share of all-time units/revenue this single period represents). This is a young,
   growing business, so a period's contribution to lifetime totals is a real signal of
   momentum — call out whether this was a strong or weak period relative to that lifetime
   base, not just in isolation.
4. The single most urgent risk or opportunity in this period's data.
Do not pad to reach 4 sentences if 3 fully covers it. If something in the data is unusually significant and doesn't fit cleanly into the four points above, add one sentence to surface it — only when genuinely warranted, never by default.
Mark every key metric (dollar amounts, percentages, growth rates, unit counts) by wrapping it in double angle brackets: <<$24,250>>, <<+84% MoM>>, <<29.1% margin>>, <<52 units>>.]

RECOMMENDATIONS:
TITLE: [3–6 words, action-oriented, no verb-first requirement — this is a label, not a directive. e.g. "Liquidate Aged Inventory", "Expand Kuttu Gadwal Sourcing"]
RECOMMENDATION: [1–2 sentences, plain and concise. Start with a strong imperative verb. State the specific action and measurable target.]
WHY: [One short clause/fragment, not a full sentence. The single data point that justifies this action. Wrap the key metric in double angle brackets, e.g. <<4% sell-through>>.]
NEXT STEP: [One short clause/fragment. The concrete first action to take.]
IMPACT: [One short clause/fragment. The expected business outcome. Wrap the key metric in double angle brackets if there is one.]

TITLE: [next title]
RECOMMENDATION: [same format]
WHY: [fragment]
NEXT STEP: [fragment]
IMPACT: [fragment]

TITLE: [next title]
RECOMMENDATION: [same format]
WHY: [fragment]
NEXT STEP: [fragment]
IMPACT: [fragment]

TITLE: [fourth title, only if strongly warranted by data]
RECOMMENDATION: [same format]
WHY: [fragment]
NEXT STEP: [fragment]
IMPACT: [fragment]"""

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
            "Executive summary unavailable this run — check the console output "
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
        "All figures in USD, with <b>COGS</b> converted from INR at the ECB rate on each "
        "item's acquisition date. These are gross figures only, before operating "
        "expenses — not a substitute for accountant-prepared tax figures.",
        ST["note"]
    )
    story += _sec_table(
        [Paragraph("Tax Reference Summary", ST["sub_head"])],
        ["Metric","Value"], tax, [CONTENT_W*0.65, CONTENT_W*0.35],
        min_rows=len(tax), trailing=[tax_note]
    )
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
    if m["pay_methods"]:
        pm  = sorted(m["pay_methods"].items(), key=lambda x:x[1]["revenue"], reverse=True)
        rows= [[met, str(d["count"]), _usd(d["revenue"])] for met,d in pm]
        story += _sec_table(
            [payment_header], ["Payment Method","Units","Revenue"], rows,
            [CONTENT_W*0.5, CONTENT_W*0.2, CONTENT_W*0.3],
            totals=["Total", str(m["units"]), _usd(m["rev"])]
        )
    else:
        story.append(KeepTogether([
            payment_header,
            _no_data("No payment data recorded for this period."),
        ]))
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
    ]), Spacer(1, 10)]

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
        totals=["Total","","","","",_usd(m["disc_lost"], deduction=True)]
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
        totals=["Total", str(m["units"]), _usd(t_rev), "100.0%"]
    )

# ── Section 5: Outstanding Balance ────────────────────────────────────────────
def _sec_outstanding(m):
    header = _sec("Outstanding Balance") + [
        Paragraph(
            "<b>Outstanding Balance</b> is money owed to the business for units "
            "already sold but not yet fully paid for.",
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
        Paragraph(
            "<b>Avg Days Outstanding</b> is the average age of balances still open "
            "today — how long currently-unpaid units have been waiting, not how long "
            "already-collected balances took to resolve.",
            ST["note"]
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
        compact=True
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
        Spacer(1, 10),
    ]

    if cp:
        rows = []
        for n, d in sorted(cp.items(), key=lambda x:x[1]["spend"], reverse=True):
            typ = "New" if n in m["new_c"] else "Returning"
            aov = _usd(d["spend"]/d["units"]) if d["units"] else "—"
            rows.append([d["name"], str(d["units"]), _usd(d["spend"]), aov, typ])
        story = _sec_table(
            heading,
            ["Customer","Units","Spend","Avg Order Value","Type"],
            rows,
            [CONTENT_W*.3, CONTENT_W*.12, CONTENT_W*.18, CONTENT_W*.22, CONTENT_W*.18]
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
        concentration_note = Paragraph(
            f"<b>Customer Concentration:</b> the top 5 customers below account for "
            f"<b>{m['top5_share']:.0f}%</b> of lifetime revenue — the share of all-time "
            f"revenue resting on the business's most valuable repeat buyers. A higher share "
            f"means more of the business depends on a small number of relationships worth "
            f"protecting; a lower share reflects a broader, more distributed customer base.",
            ST["note"]
        )

        rows = []
        for n, d in sorted(cl.items(), key=lambda x:x[1]["spend"], reverse=True):
            top = max(d["cats"], key=d["cats"].get) if d["cats"] else "—"
            top_share = f"{d['cats'][top]/d['units']*100:.0f}%" if d["units"] and d["cats"] else "—"
            aov = _usd(d["spend"]/d["units"]) if d["units"] else "—"
            rows.append([d["name"], str(d["units"]), _usd(d["spend"]), aov, top, top_share])

        headers = ["Customer","Total Units","Lifetime Spend","Avg Order Value",
                    "Top Category","Top Category %"]
        # Column widths measured from this period's actual longest values (compact
        # font) so long weave names and customer names never wrap — not a fixed
        # guess, so this adapts automatically as the underlying data changes.
        HEADER_FONT, HEADER_SIZE = "Montserrat-Bold", 7
        BODY_FONT,   BODY_SIZE   = "Montserrat-Bold", 7.5
        PAD = 12
        cols = list(zip(*rows))
        natural = [
            max(pdfmetrics.stringWidth(h, HEADER_FONT, HEADER_SIZE),
                max((pdfmetrics.stringWidth(str(v), BODY_FONT, BODY_SIZE) for v in cols[i]), default=0)
               ) + PAD
            for i, h in enumerate(headers)
        ]
        scale = CONTENT_W / sum(natural)
        widths = [w * scale for w in natural]

        covered = sum(d["units"] for d in cl.values())
        total_sold = m["total_lifetime_sold"]
        coverage_pct = covered / total_sold * 100 if total_sold else 0

        story += _sec_table(
            [lifetime_header, concentration_note, Spacer(1, 6)],
            headers, rows, widths, compact=True
        )
        story.append(Paragraph(
            f"Reflects <b>{covered} of {total_sold} total lifetime units sold "
            f"({coverage_pct:.0f}%)</b> — the remainder lack a recorded customer name "
            f"and are excluded, so lifetime figures likely understate true customer "
            f"value. Includes all transactions on record regardless of report period.",
            ST["note"]
        ))
        story.append(Paragraph(
            "<b>Top Category %</b> is the share of a customer's total units in their "
            "single most-purchased category.",
            ST["note"]
        ))
    else:
        story.append(KeepTogether([lifetime_header, _no_data()]))
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
            "Reflects sales within this report period only — see Lifetime "
            "Category Performance below for all-time trends across the full catalogue.",
            ST["note"]
        )
        story = _sec_table(
            header, col_headers, rows, col_widths,
            totals=["Total", str(m["units"]), _usd(t_rev),
                    _usd(m["gp"]), _pct(m["margin"]), "100.0%"],
            min_rows=min(len(rows), 20), trailing=[note]
        )

    # Lifetime Category Performance — unlike the customer table, this has 100%
    # data coverage (Weave Type and Selling Price are populated on every
    # historical sold row), so no completeness caveat is needed here.
    cats_life = m["cats_life"]
    lifetime_header = Paragraph("Lifetime Category Performance", ST["sub_head"])
    story.append(Spacer(1, 12))

    if cats_life:
        t_rev_life   = sum(d["revenue"] for d in cats_life.values())
        t_units_life = sum(d["units"] for d in cats_life.values())
        t_gp_life    = sum(d["gp"] for d in cats_life.values())
        rows_life = []
        for c, d in sorted(cats_life.items(), key=lambda x:x[1]["revenue"], reverse=True):
            marg  = d["gp"]/d["revenue"]*100 if d["revenue"] else 0
            share = d["revenue"]/t_rev_life*100 if t_rev_life else 0
            rows_life.append([c, str(d["units"]), _usd(d["revenue"]),
                               _warn_neg(_usd(d["gp"], deduction=d["gp"] < 0), d["gp"] < 0),
                               _warn_neg(f"{marg:.1f}%", marg < 0), f"{share:.1f}%"])
        story += _sec_table(
            [lifetime_header], col_headers, rows_life, col_widths,
            totals=["Total", str(t_units_life), _usd(t_rev_life), _usd(t_gp_life),
                    f"{(t_gp_life/t_rev_life*100 if t_rev_life else 0):.1f}%", "100.0%"]
        )
        story.append(Paragraph(
            "Includes all transactions on record regardless of report period, "
            "aggregated by weave type to show which categories have performed "
            "best historically.",
            ST["note"]
        ))
    else:
        story.append(KeepTogether([lifetime_header, _no_data()]))

    return story

# ── Section 7b: Supplier Performance ──────────────────────────────────────────
def _sec_suppliers(m, period_label):
    header = _sec("Supplier Performance")
    sup = m["suppliers"]
    if not sup:
        return [KeepTogether(header + [_no_data()])]

    total_life_rev   = sum(d["life_revenue"] for d in sup.values())
    total_available  = sum(d["available"]    for d in sup.values())
    total_life_units = sum(d["life_units"]   for d in sup.values())
    total_period_units = sum(d["period_units"] for d in sup.values())
    total_life_gp    = sum(d["life_gp"]      for d in sup.values())

    headers = ["Supplier","Available","Lifetime Units","Units This Period",
               "Avg Days to Sell","Avg Margin %","Revenue Share"]
    rows = []
    for name, d in sorted(sup.items(), key=lambda x: x[1]["life_revenue"], reverse=True):
        avg_dts = d["dts_sum"] / d["dts_count"] if d["dts_count"] else None
        margin  = d["life_gp"] / d["life_revenue"] * 100 if d["life_revenue"] else None
        share   = d["life_revenue"] / total_life_rev * 100 if total_life_rev else 0
        rows.append([
            name,
            str(d["available"]),
            str(d["life_units"]),
            str(d["period_units"]),
            f"{avg_dts:.0f}" if avg_dts is not None else "—",
            _warn_neg(f"{margin:.1f}%", margin < 0) if margin is not None else "—",
            f"{share:.1f}%",
        ])

    # Column widths measured from this period's actual longest values (compact
    # font) so long supplier names never wrap — same approach used for the
    # Lifetime Customer Intelligence table.
    HEADER_FONT, HEADER_SIZE = "Montserrat-Bold", 7
    BODY_FONT,   BODY_SIZE   = "Montserrat-Bold", 7.5
    PAD = 12
    cols = list(zip(*rows))
    natural = [
        max(pdfmetrics.stringWidth(h, HEADER_FONT, HEADER_SIZE),
            max((pdfmetrics.stringWidth(str(v), BODY_FONT, BODY_SIZE) for v in cols[i]), default=0)
           ) + PAD
        for i, h in enumerate(headers)
    ]
    scale = CONTENT_W / sum(natural)
    widths = [w * scale for w in natural]

    blended_margin = total_life_gp / total_life_rev * 100 if total_life_rev else 0
    total_dts = m["total_dts_units"]
    total_sold_life = m["total_lifetime_sold"]
    dts_coverage_pct = total_dts / total_sold_life * 100 if total_sold_life else 0

    story = _sec_table(
        header, headers, rows, widths, compact=True,
        totals=["Total", str(total_available), str(total_life_units),
                str(total_period_units), "—", f"{blended_margin:.1f}%", "100.0%"]
    )
    story.append(Paragraph(
        f"<b>Available</b> reflects current inventory; Lifetime figures cover all sold/"
        f"sold-partial units on record; <b>Units This Period</b> reflects {period_label}.",
        ST["note"]
    ))
    story.append(Paragraph(
        f"<b>Avg Days to Sell</b> is based on {total_dts} of {total_sold_life} lifetime "
        f"sold units ({dts_coverage_pct:.0f}%) with a recorded sell date — suppliers with "
        f'no qualifying units show "—".',
        ST["note"]
    ))
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
        "<b>Cancellations Restored</b> reflects units returned to available "
        "stock this period — already accounted for within Opening Stock or "
        "Units Added above, not an additional adjustment to Closing Stock.",
        ST["note"]
    )
    heading = _sec("Inventory Flow") + [
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
        Paragraph(
            "<b>Sell-Through Rate</b> measures the proportion of available inventory "
            "converted to sales within the period.",
            ST["note"]
        ),
        Paragraph(
            "<b>Avg Days to Sell</b> reflects how quickly the business converts stock to revenue.",
            ST["note"]
        ),
        Paragraph(
            "<b>Inventory Turnover</b> is units sold ÷ average units on hand — how many "
            "times stock cycled through sales this period. A higher ratio means efficient, "
            "fast-moving inventory, but pushed too far it signals stock is thin enough to "
            "risk running out of popular pieces before they can be restocked. A lower ratio "
            "means inventory is accumulating faster than it sells, tying up capital in stock "
            f"that isn't converting. Because the ratio scales with period length, only "
            f"compare it against other {period_type} reports.",
            ST["note"]
        ),
        Spacer(1, 6),
    ]
    return _sec_table(heading, ["","Units"], rows, [CONTENT_W*.7, CONTENT_W*.3],
                       key_rows={3}, trailing=[inv_flow_note])

# ── Section 9: Inventory Snapshot ─────────────────────────────────────────────
def _sec_snapshot(m, gen_date_str):
    sc   = m["snap"]
    rows = [[s, str(c)] for s,c in sorted(sc.items(), key=lambda x:x[1], reverse=True)]
    heading = _sec("Inventory Snapshot") + [
        Paragraph(
            f"A real-time census of every unit by status as of {gen_date_str} — the "
            f"baseline for tracking unsold inventory ({_usd(m['unsold_val'])} in capital "
            f"currently tied up) against what's already sold.",
            ST["note"]
        ),
        Spacer(1, 4),
    ]
    return _sec_table(heading, ["Status","Units"], rows,
                       [CONTENT_W*.7, CONTENT_W*.3],
                       totals=["Total", str(sum(sc.values()))])

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
    heading = _sec("Aging Inventory") + [
        Paragraph(
            "Available units only, grouped by days in stock — surfaces which unsold "
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
            compact=True
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
    story += _sec_outstanding(metrics)
    story += _sec_customers(metrics)
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
        msg["Subject"] = f"Lakshira {report_type} — {period_label}"
        msg.attach(MIMEText(
            f"Hello,\n\n"
            f"Please find attached the Lakshira {report_type} for {period_label}.\n\n"
            f"{headline}\n\n"
            f"This report was generated automatically by the Lakshira Inventory "
            f"Management System and is confidential — for internal use only.\n\n"
            f"— Lakshira IMS", "plain"
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
    metrics = _metrics(data, start, end, ps, pe)

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
