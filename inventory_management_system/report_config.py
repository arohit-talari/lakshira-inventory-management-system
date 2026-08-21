import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Base paths ─────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR  = os.path.join(BASE_DIR, "assets")
FONTS_DIR   = os.path.join(ASSETS_DIR, "fonts")
REPORTS_DIR = os.path.join(BASE_DIR, "Reports")

LOGO_PATH = os.path.join(ASSETS_DIR, "lakshira_logo.png")

FONT_PATHS = {
    "CormorantGaramond":        os.path.join(FONTS_DIR, "CormorantGaramond-Regular.ttf"),
    "CormorantGaramond-Bold":   os.path.join(FONTS_DIR, "CormorantGaramond-Bold.ttf"),
    "CormorantGaramond-Italic": os.path.join(FONTS_DIR, "CormorantGaramond-Italic.ttf"),
    "Montserrat-Light":         os.path.join(FONTS_DIR, "Montserrat-Light.ttf"),
    "Montserrat":               os.path.join(FONTS_DIR, "Montserrat-Regular.ttf"),
    "Montserrat-Bold":          os.path.join(FONTS_DIR, "Montserrat-Bold.ttf"),
}

# ── Brand colours ──────────────────────────────────────────────────────────────
# Used throughout the PDF for headers, tables, callouts, and accents.
COLOUR = {
    "gold":        "#D4A84C",   # primary accent — section headers, rule lines
    "sage":        "#7A9E7E",   # secondary — table headers, subheadings
    "rose":        "#D4847A",   # alert callouts — aging, outstanding payments
    "burnt_orange":"#D4511A",   # logo primary — cover page accents
    "dark_brown":  "#4A2010",   # logo script — body text
    "off_white":   "#FAF8F5",   # page background / cover fill
    "light_grey":  "#E8E4DF",   # subtle dividers, alternate table rows
}

# ── Claude API ─────────────────────────────────────────────────────────────────
# Key is loaded from .env (ANTHROPIC_API_KEY) — never commit the key itself.
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ── Email (SMTP) ───────────────────────────────────────────────────────────────
# Sender + app password are loaded from .env — never commit them directly.
# In Google Account → Security → App Passwords → generate a 16-char app password.
# Google displays the password in 4-character groups separated by a non-breaking
# space (U+00A0, not a regular space) -- pasted as-is into .env, that character
# breaks smtplib's login (UnicodeEncodeError). Strip all whitespace, regular or
# non-breaking, so a straight copy-paste from Google always works.
EMAIL_SENDER   = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"].replace("\u00a0", "").replace(" ", "")

# Comma-separated list in .env, e.g. EMAIL_RECIPIENTS="owner@example.com,founder@example.com"
EMAIL_RECIPIENTS = [addr.strip() for addr in os.environ.get("EMAIL_RECIPIENTS", "").split(",") if addr.strip()]

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT   = 587
