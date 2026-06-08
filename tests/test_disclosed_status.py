"""Tests for src/services/disclosed_status.py."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.services.disclosed_status import (
    assess, assess_universe, DisclosedCoverage, _period_to_quarter_end,
)


def test_bkmb_known_ticker():
    cov = assess("BKMB.OM")
    assert cov.file_exists is True
    assert cov.n_quarters == 4
    assert cov.most_recent_period == "Q1 2026"
    assert cov.fields_complete is True
    assert cov.coverage_pct == 100.0


def test_missing_ticker_returns_no_file():
    cov = assess("XYZ.MISSING")
    assert cov.file_exists is False
    assert cov.n_quarters == 0
    assert cov.coverage_pct == 0.0
    assert any("No data/disclosed" in i for i in cov.issues)


def test_period_to_quarter_end_calendar():
    assert _period_to_quarter_end("Q1 2025").isoformat() == "2025-03-31"
    assert _period_to_quarter_end("Q2 2025").isoformat() == "2025-06-30"
    assert _period_to_quarter_end("Q3 2025").isoformat() == "2025-09-30"
    assert _period_to_quarter_end("Q4 2025").isoformat() == "2025-12-31"


def test_period_to_quarter_end_handles_garbage():
    assert _period_to_quarter_end("not a period") is None
    assert _period_to_quarter_end("") is None


def test_assess_universe():
    out = assess_universe(["BKMB.OM", "XYZ.MISSING"])
    assert len(out) == 2
    assert out[0].ticker == "BKMB.OM"
    assert out[1].ticker == "XYZ.MISSING"


def test_staleness_warning_emits_when_old(tmp_path, monkeypatch):
    """When the most recent quarter is > 90 days behind today, emit
    a staleness warning. We mock by writing a JSON with an old period."""
    # Synthesize a fixture by copying BKMB but with an older period.
    fixture = tmp_path / "disclosed"
    fixture.mkdir()
    payload = {
        "ticker": "FAKE.TX",
        "currency": "USD", "units": "thousands",
        "quarterly": [
            {"period": "Q1 2020", "operating_income": 100,
              "net_income": 50, "eps": 0.5},
        ],
    }
    (fixture / "FAKE.TX.json").write_text(json.dumps(payload))
    monkeypatch.setattr(
        "src.services.disclosed_status._DISCLOSED_DIR", fixture)
    cov = assess("FAKE.TX")
    assert cov.file_exists is True
    assert any("days behind today" in i for i in cov.issues)
