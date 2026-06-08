"""Tests for the quarter-label derivation in scripts/build_earnings_calendar.py.

Distinct branches: calendar FY-end (Dec) and Indian FY-end (Mar).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "build_earnings_calendar",
    Path(__file__).resolve().parents[1] / "scripts" / "build_earnings_calendar.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_next_quarter_label = _mod._next_quarter_label


def test_calendar_fy_q1_announcement():
    """Q1 results typically announce in May-July of the same year."""
    assert _next_quarter_label("2026-05-15", 12) == "Q1 2026"
    assert _next_quarter_label("2026-07-31", 12) == "Q1 2026"


def test_calendar_fy_q2_announcement():
    """Q2 results typically announce in Aug-Oct."""
    assert _next_quarter_label("2026-08-15", 12) == "Q2 2026"
    assert _next_quarter_label("2026-10-31", 12) == "Q2 2026"


def test_calendar_fy_q3_announcement():
    """Q3 results typically announce in Nov-Dec."""
    assert _next_quarter_label("2026-11-15", 12) == "Q3 2026"


def test_calendar_fy_q4_announcement():
    """Q4/FY results announce in Jan-Apr of the FOLLOWING year."""
    assert _next_quarter_label("2027-02-15", 12) == "Q4 2026"
    assert _next_quarter_label("2027-04-15", 12) == "Q4 2026"


def test_indian_fy_q1_announcement():
    """Indian FY runs Apr-Mar. Q1 (Apr-Jun) typically announces in
    Jul-Sep — that's our 'Aug-Oct' branch."""
    # Aug 2026 → Q1 of FY27 (Apr 2026 - Mar 2027)
    assert _next_quarter_label("2026-08-15", 3) == "Q1 FY27"


def test_indian_fy_q2_announcement():
    """Q2 (Jul-Sep) typically announces in Oct-Dec."""
    assert _next_quarter_label("2026-11-15", 3) == "Q2 FY27"


def test_indian_fy_q3_announcement():
    """Q3 (Oct-Dec) typically announces in Jan-Mar."""
    assert _next_quarter_label("2027-02-15", 3) == "Q3 FY27"


def test_indian_fy_q4_announcement():
    """Q4 (Jan-Mar) typically announces in Apr-Jul."""
    assert _next_quarter_label("2026-05-15", 3) == "Q4 FY26"


def test_bad_date_returns_empty():
    assert _next_quarter_label("not-a-date", 12) == ""
    assert _next_quarter_label("", 12) == ""
