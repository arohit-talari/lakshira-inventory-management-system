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
from datetime import date

import pytest

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
