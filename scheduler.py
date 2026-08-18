"""
scheduler.py — Lakshira Report Scheduler
Invoked by launchd; generates and emails the appropriate report based on today's date.
"""

import sys
import smtplib
import logging
from datetime import date, timedelta
from email.mime.text import MIMEText
import calendar

logging.basicConfig(
    filename="/tmp/lakshira_scheduler.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _monthly():
    today = date.today()
    m, y  = today.month - 1, today.year
    if m == 0: m, y = 12, y - 1
    start = date(y, m, 1)
    end   = date(y, m, calendar.monthrange(y, m)[1])
    label = start.strftime("%B %Y")
    return "monthly", start, end, label


def _quarterly():
    today = date.today()
    q     = (today.month - 1) // 3
    if q == 0: q, y = 4, today.year - 1
    else:       y    = today.year
    sm    = (q - 1) * 3 + 1
    start = date(y, sm, 1)
    end   = date(y, sm + 2, calendar.monthrange(y, sm + 2)[1])
    label = f"Q{q} {y}"
    return "quarterly", start, end, label


def _annual():
    y     = date.today().year - 1
    start = date(y, 1, 1)
    end   = date(y, 12, 31)
    label = str(y)
    return "annual", start, end, label


def _notify_failure(ptype, label, err):
    """Best-effort failure email so a scheduled-report failure is visible to
    the same people who'd normally receive the report, not just buried in
    /tmp/lakshira_scheduler.log (unattended runs mean nobody's watching the
    log, and /tmp isn't durable -- macOS periodic maintenance purges stale
    files there)."""
    try:
        from report_config import (
            EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENTS,
            SMTP_SERVER, SMTP_PORT,
        )
    except Exception as e:
        log.error("Could not load email config to send failure notice: %s", e)
        return
    if EMAIL_SENDER.startswith("PLACEHOLDER") or not EMAIL_RECIPIENTS:
        log.warning("Email not configured — failure notice not sent.")
        return
    try:
        msg = MIMEText(
            f"The scheduled {ptype} report for {label} failed to generate.\n\n"
            f"Error: {err}\n\n"
            f"See /tmp/lakshira_scheduler.log on the machine running the "
            f"scheduler for full details.\n\n"
            f"— Lakshira IMS"
        )
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = ", ".join(EMAIL_RECIPIENTS)
        msg["Subject"] = f"⚠ Lakshira {ptype} report failed to generate — {label}"
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
        log.info("Failure notice emailed to %d recipient(s).", len(EMAIL_RECIPIENTS))
    except Exception as e:
        log.error("Could not send failure notice email: %s", e)


def run():
    today = date.today()
    tasks = []

    # Monthly: 1st of every month
    if today.day == 1:
        tasks.append(_monthly())

    # Quarterly: 1st of Jan / Apr / Jul / Oct
    if today.day == 1 and today.month in (1, 4, 7, 10):
        tasks.append(_quarterly())

    # Annual: 1st of January
    if today.day == 1 and today.month == 1:
        tasks.append(_annual())

    if not tasks:
        log.info("No scheduled reports for %s.", today)
        return

    try:
        from generate_report import generate_report
    except Exception as e:
        log.error("Could not import generate_report: %s", e)
        sys.exit(1)

    for ptype, start, end, label in tasks:
        log.info("Generating %s report: %s", ptype, label)
        try:
            path = generate_report(ptype, start, end, label, send_email=True)
            log.info("Report saved: %s", path)
        except Exception as e:
            log.exception("Failed to generate %s report: %s", ptype, e)
            _notify_failure(ptype, label, e)


if __name__ == "__main__":
    run()
