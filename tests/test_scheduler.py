"""
Tier 1 unit tests -- scheduler.py's period-calculation functions.

These were manually traced by hand earlier in the project (verifying the
Nov/Dec/Jan/Feb-Mar boundary behavior, the Q1-wraps-to-prior-year-Q4 case,
etc.) -- this locks that reasoning in as an automated regression test
instead of leaving it as a one-time mental trace. No network, no email, no
generate_report import triggered (run() imports that lazily inside the
function body, not at module load time).

Run: pytest tests/test_scheduler.py -v
"""
import email
import logging
import smtplib
from datetime import date
from unittest.mock import ANY, MagicMock

import pytest

import generate_report as gr
import report_config as rc
import scheduler as sch


class _FixedDate(date):
    """Subclass of the real date type (not a Mock) so isinstance checks and
    date arithmetic elsewhere in scheduler.py keep working -- only today()
    is overridden."""
    _fixed = date(2026, 1, 1)

    @classmethod
    def today(cls):
        return cls._fixed


def _freeze(monkeypatch, y, m, d):
    _FixedDate._fixed = date(y, m, d)
    monkeypatch.setattr(sch, "date", _FixedDate)


class TestMonthly:
    def test_normal_month_returns_prior_month(self, monkeypatch):
        _freeze(monkeypatch, 2026, 8, 1)
        ptype, start, end, label = sch._monthly()
        assert ptype == "monthly"
        assert start == date(2026, 7, 1)
        assert end == date(2026, 7, 31)
        assert label == "July 2026"

    def test_january_wraps_to_prior_december(self, monkeypatch):
        _freeze(monkeypatch, 2026, 1, 1)
        ptype, start, end, label = sch._monthly()
        assert start == date(2025, 12, 1)
        assert end == date(2025, 12, 31)
        assert label == "December 2025"


class TestQuarterly:
    def test_april_gives_q1(self, monkeypatch):
        _freeze(monkeypatch, 2026, 4, 1)
        ptype, start, end, label = sch._quarterly()
        assert start == date(2026, 1, 1)
        assert end == date(2026, 3, 31)
        assert label == "Q1 2026"

    def test_july_gives_q2(self, monkeypatch):
        _freeze(monkeypatch, 2026, 7, 1)
        _, start, end, label = sch._quarterly()
        assert start == date(2026, 4, 1)
        assert end == date(2026, 6, 30)
        assert label == "Q2 2026"

    def test_october_gives_q3(self, monkeypatch):
        _freeze(monkeypatch, 2026, 10, 1)
        _, start, end, label = sch._quarterly()
        assert start == date(2026, 7, 1)
        assert end == date(2026, 9, 30)
        assert label == "Q3 2026"

    def test_january_wraps_to_prior_year_q4(self, monkeypatch):
        _freeze(monkeypatch, 2026, 1, 1)
        _, start, end, label = sch._quarterly()
        assert start == date(2025, 10, 1)
        assert end == date(2025, 12, 31)
        assert label == "Q4 2025"


class TestAnnual:
    def test_returns_full_prior_calendar_year(self, monkeypatch):
        _freeze(monkeypatch, 2026, 1, 1)
        ptype, start, end, label = sch._annual()
        assert ptype == "annual"
        assert start == date(2025, 1, 1)
        assert end == date(2025, 12, 31)
        assert label == "2025"


class TestRunTaskSelection:
    """Confirms which of the three period tasks fire on a given date --
    the actual scheduling logic in run(), not just the period math."""

    def _tasks_for(self, monkeypatch, y, m, d):
        _freeze(monkeypatch, y, m, d)
        today = sch.date.today()
        tasks = []
        if today.day == 1:
            tasks.append("monthly")
        if today.day == 1 and today.month in (1, 4, 7, 10):
            tasks.append("quarterly")
        if today.day == 1 and today.month == 1:
            tasks.append("annual")
        return tasks

    def test_jan_1_fires_all_three(self, monkeypatch):
        assert self._tasks_for(monkeypatch, 2026, 1, 1) == ["monthly", "quarterly", "annual"]

    def test_apr_1_fires_monthly_and_quarterly_only(self, monkeypatch):
        assert self._tasks_for(monkeypatch, 2026, 4, 1) == ["monthly", "quarterly"]

    def test_mid_month_fires_nothing(self, monkeypatch):
        assert self._tasks_for(monkeypatch, 2026, 8, 15) == []

    def test_first_of_non_quarter_month_fires_monthly_only(self, monkeypatch):
        assert self._tasks_for(monkeypatch, 2026, 8, 1) == ["monthly"]


class TestNotifyFailure:
    """_notify_failure() is the last line of defense when a scheduled report
    fails -- an unattended run means nobody's watching /tmp/lakshira_scheduler.log,
    so this must never itself raise, even if the SMTP send fails. Mocks
    smtplib.SMTP so this needs no live credentials or network access."""

    def _armed_smtp_mock(self, monkeypatch, error):
        mock_srv = MagicMock()
        mock_srv.sendmail.side_effect = error
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_srv
        monkeypatch.setattr(sch.smtplib, "SMTP", mock_smtp_cls)
        return mock_srv

    def _configure_email(self, monkeypatch):
        monkeypatch.setattr(rc, "EMAIL_SENDER", "sender@example.com")
        monkeypatch.setattr(rc, "EMAIL_PASSWORD", "fake-password")
        monkeypatch.setattr(rc, "EMAIL_RECIPIENTS", ["recipient@example.com"])
        monkeypatch.setattr(rc, "SMTP_SERVER", "smtp.example.com")
        monkeypatch.setattr(rc, "SMTP_PORT", 587)

    def _body_of(self, raw_message):
        # MIMEText base64-encodes the body by default -- decode it back to
        # plain text rather than substring-matching the raw SMTP payload.
        return email.message_from_string(raw_message).get_payload(decode=True).decode()

    def test_smtp_failure_is_swallowed_and_logged(self, monkeypatch, caplog):
        self._configure_email(monkeypatch)
        mock_srv = self._armed_smtp_mock(monkeypatch, smtplib.SMTPException("boom"))

        with caplog.at_level(logging.ERROR):
            sch._notify_failure(
                "monthly", "July 2026", RuntimeError("report generation failed")
            )

        assert mock_srv.sendmail.called
        assert "Could not send failure notice email" in caplog.text

    def test_placeholder_sender_short_circuits_without_calling_smtp(self, monkeypatch):
        monkeypatch.setattr(rc, "EMAIL_SENDER", "PLACEHOLDER@example.com")
        monkeypatch.setattr(rc, "EMAIL_RECIPIENTS", ["recipient@example.com"])
        mock_smtp_cls = MagicMock()
        monkeypatch.setattr(sch.smtplib, "SMTP", mock_smtp_cls)

        sch._notify_failure("monthly", "July 2026", RuntimeError("x"))

        mock_smtp_cls.assert_not_called()

    def test_email_stage_uses_generated_but_not_sent_wording(self, monkeypatch):
        self._configure_email(monkeypatch)
        mock_srv = self._armed_smtp_mock(monkeypatch, error=None)

        sch._notify_failure(
            "monthly", "July 2026", RuntimeError("smtp down"), stage="email"
        )

        sent_body = self._body_of(mock_srv.sendmail.call_args[0][2])
        assert "generated successfully" in sent_body
        assert "email delivery failed" in sent_body
        assert "failed to generate" not in sent_body

    def test_default_stage_uses_generation_failed_wording(self, monkeypatch):
        self._configure_email(monkeypatch)
        mock_srv = self._armed_smtp_mock(monkeypatch, error=None)

        sch._notify_failure("monthly", "July 2026", RuntimeError("sheets unreachable"))

        sent_body = self._body_of(mock_srv.sendmail.call_args[0][2])
        assert "failed to generate" in sent_body
        assert "generated successfully" not in sent_body


class TestRunNotifiesOnFailure:
    """End-to-end (within run()) proof that D-353 is actually closed: an
    email-delivery failure inside generate_report() reaches _notify_failure()
    with stage='email', a generation failure reaches it with the default
    stage, and a clean run never calls it at all (no false-positive alert on
    the happy path). generate_report itself is mocked at the function level
    -- no Sheets/PDF/SMTP work happens."""

    def _run_on(self, monkeypatch, y, m, d):
        monkeypatch.setattr(rc, "SCHEDULER_MODE", "live")  # gate must be open for these
        _freeze(monkeypatch, y, m, d)  # 1st of a non-quarter month -> exactly one task: monthly

    def test_email_delivery_failure_triggers_notify_with_email_stage(self, monkeypatch):
        self._run_on(monkeypatch, 2026, 8, 1)
        err = gr.EmailDeliveryError("smtp down")
        monkeypatch.setattr(gr, "generate_report", MagicMock(side_effect=err))
        mock_notify = MagicMock()
        monkeypatch.setattr(sch, "_notify_failure", mock_notify)

        sch.run()

        mock_notify.assert_called_once_with("monthly", "July 2026", err, stage="email")

    def test_generation_failure_triggers_notify_with_default_stage(self, monkeypatch):
        self._run_on(monkeypatch, 2026, 8, 1)
        err = RuntimeError("sheets unreachable")
        monkeypatch.setattr(gr, "generate_report", MagicMock(side_effect=err))
        mock_notify = MagicMock()
        monkeypatch.setattr(sch, "_notify_failure", mock_notify)

        sch.run()

        mock_notify.assert_called_once_with("monthly", "July 2026", err)

    def test_successful_run_never_calls_notify_failure(self, monkeypatch):
        self._run_on(monkeypatch, 2026, 8, 1)
        monkeypatch.setattr(
            gr, "generate_report", MagicMock(return_value="/fake/Lakshira_Monthly.pdf")
        )
        mock_notify = MagicMock()
        monkeypatch.setattr(sch, "_notify_failure", mock_notify)

        sch.run()

        mock_notify.assert_not_called()


class TestSchedulerModeGate:
    """SCHEDULER_MODE is the kill switch: cron can invoke run() daily and
    safely no-op until a human explicitly sets SCHEDULER_MODE=live in .env.
    Every case here is on a real trigger day (Jan 1, fires all three tasks)
    so a passing "nothing happened" result is actually proof the gate did
    the blocking, not just that there was nothing to do that day."""

    def test_default_test_mode_never_calls_generate_report(self, monkeypatch):
        monkeypatch.setattr(rc, "SCHEDULER_MODE", "test")
        _freeze(monkeypatch, 2026, 1, 1)
        mock_generate = MagicMock()
        monkeypatch.setattr(gr, "generate_report", mock_generate)

        sch.run()

        mock_generate.assert_not_called()

    def test_unset_or_unrecognized_value_also_blocks(self, monkeypatch):
        monkeypatch.setattr(rc, "SCHEDULER_MODE", "")
        _freeze(monkeypatch, 2026, 1, 1)
        mock_generate = MagicMock()
        monkeypatch.setattr(gr, "generate_report", mock_generate)

        sch.run()

        mock_generate.assert_not_called()

    def test_live_mode_proceeds_to_generate_report(self, monkeypatch):
        monkeypatch.setattr(rc, "SCHEDULER_MODE", "live")
        _freeze(monkeypatch, 2026, 1, 1)
        mock_generate = MagicMock(return_value="/fake/Lakshira_Monthly.pdf")
        monkeypatch.setattr(gr, "generate_report", mock_generate)

        sch.run()

        assert mock_generate.called

    def test_broken_report_config_import_returns_safely(self, monkeypatch, caplog):
        # Simulate a missing/broken .env (e.g. a required key absent) the
        # same way the real import would fail -- must log and return, never
        # raise, since an unattended cron run has no one to catch it.
        import builtins
        real_import = builtins.__import__

        def _blow_up_on_report_config(name, *args, **kwargs):
            if name == "report_config":
                raise KeyError("SCHEDULER_MODE")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blow_up_on_report_config)
        _freeze(monkeypatch, 2026, 1, 1)

        with caplog.at_level(logging.ERROR):
            sch.run()  # must not raise

        assert "Could not load report_config" in caplog.text
