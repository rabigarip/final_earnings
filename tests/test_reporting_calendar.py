"""Tests for calendar-aware grounding currency."""
from datetime import date

from src.services.reporting_calendar import (
    expected_latest, grounding_staleness, period_end_date,
)

_BKMB_FY2025 = {"fy_highlights": {"period": "FY 2025"},
                "quarterly": [{"period": "Q1 2026"}]}


def test_expected_annual_advances_with_time_dec_year_end():
    # Dec year-end: FY2025 is the latest filed in mid-2026; FY2026 in mid-2027.
    assert expected_latest("BKMB.OM", date(2026, 6, 4))["annual_period"] == "FY2025"
    assert expected_latest("BKMB.OM", date(2027, 6, 4))["annual_period"] == "FY2026"


def test_grounding_current_today_stale_next_year():
    today = grounding_staleness("BKMB.OM", disclosed=_BKMB_FY2025, as_of=date(2026, 6, 4))
    assert today["annual_current"] is True
    assert "current" in today["status"].lower()

    nextyr = grounding_staleness("BKMB.OM", disclosed=_BKMB_FY2025, as_of=date(2027, 6, 4))
    assert nextyr["annual_current"] is False
    assert "STALE" in nextyr["status"]
    assert nextyr["expected_annual"] == "FY2026"


def test_next_month_still_current():
    # A report generated next month (before the next annual is due) is still
    # grounded on FY2025 — not flagged stale.
    s = grounding_staleness("BKMB.OM", disclosed=_BKMB_FY2025, as_of=date(2026, 7, 20))
    assert s["annual_current"] is True


def test_no_grounding_reports_missing():
    s = grounding_staleness("BKMB.OM", disclosed={}, as_of=date(2026, 6, 4))
    assert s["have_grounded_fy"] is False
    assert s["annual_current"] is False


def test_period_end_date_parsing():
    assert period_end_date("FY 2025", 12) == date(2025, 12, 31)
    assert period_end_date("Q1 2026", 12) == date(2026, 3, 31)
    assert period_end_date("Q4 2025", 12) == date(2025, 12, 31)


def test_march_year_end_label():
    # Indian FY (March): the latest annual mid-2026 is an FY ending in March.
    e = expected_latest("ICICIBANK.NS", date(2026, 6, 4))
    assert e["fiscal_year_end_month"] == 3
    assert e["annual_period_end"].endswith("-03-31")
