"""
Tests for src/services/bloomberg_parser.py against the real Bloomberg
cons_q + FA xlsx files committed under data/bloomberg/.

These tests validate the "integration happy path" — the parser
reproducing the known numbers from ADNOCGAS.AE and 2020.SR (SAFCO).
Unit-style edge cases (malformed labels, missing columns) live in the
parser itself as graceful fallbacks that append to bundle.warnings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.bloomberg_parser import (
    BloombergBundle,
    bloomberg_coverage,
    bloomberg_dir,
    load_bloomberg_bundle,
)

ADNOC = "ADNOCGAS.AE"
SAFCO = "2020.SR"


def _has_files(ticker: str) -> bool:
    return all(bloomberg_coverage(ticker).values())


@pytest.mark.skipif(not _has_files(ADNOC), reason="ADNOCGAS Bloomberg files not present")
def test_adnoc_bundle_metadata():
    b = load_bloomberg_bundle(ADNOC)
    assert b is not None
    assert b.ticker == ADNOC
    assert b.currency == "USD"
    assert b.bbg_ticker.startswith("ADNOCGAS")


@pytest.mark.skipif(not _has_files(ADNOC), reason="ADNOCGAS Bloomberg files not present")
def test_adnoc_consensus_quarterly_shape():
    b = load_bloomberg_bundle(ADNOC)
    # Expect 1 actual + 4 estimate quarters
    assert len(b.consensus_quarterly) == 5
    actuals = [q for q in b.consensus_quarterly if not q.is_estimate]
    assert len(actuals) == 1
    assert actuals[0].period_label == "Q4 2025"
    ests = [q for q in b.consensus_quarterly if q.is_estimate]
    assert [q.period_label for q in ests] == [
        "Q1 2026", "Q2 2026", "Q3 2026", "Q4 2026",
    ]


@pytest.mark.skipif(not _has_files(ADNOC), reason="ADNOCGAS Bloomberg files not present")
def test_adnoc_consensus_metrics_and_counts():
    b = load_bloomberg_bundle(ADNOC)
    q1e = next(q for q in b.consensus_quarterly if q.period_label == "Q1 2026")
    # Revenue mean + analyst count
    rev_mean, rev_n = q1e.metrics["revenue"]
    assert rev_mean is not None and abs(rev_mean - 4_822_750_000) < 1
    assert rev_n == 8
    # EBITDA
    ebitda_mean, ebitda_n = q1e.metrics["ebitda"]
    assert abs(ebitda_mean - 1_723_555_555.556) < 1
    assert ebitda_n == 9
    # EPS Adj
    eps_mean, eps_n = q1e.metrics["eps_adj"]
    assert eps_mean is not None
    assert eps_n == 4
    # Actual column has None for n_analysts
    q_act = b.consensus_quarterly[0]
    assert not q_act.is_estimate
    _, n = q_act.metrics["revenue"]
    assert n is None


@pytest.mark.skipif(not _has_files(ADNOC), reason="ADNOCGAS Bloomberg files not present")
def test_adnoc_fa_annuals():
    b = load_bloomberg_bundle(ADNOC)
    # ADNOCGAS has 5 historical FY + LTM + 2 forward estimates = 8 annual columns
    assert len(b.annuals) >= 7
    years = [a.period_label for a in b.annuals]
    assert any("FY 2021" in y for y in years)
    assert any("FY 2025" in y for y in years)
    assert any("LTM" in y.upper() or "CURRENT" in y.upper() for y in years)
    assert any("FY 2026" in y and a.is_estimate for a, y in zip(b.annuals, years))

    fy25 = next(a for a in b.annuals if a.period_label == "FY 2025")
    assert fy25.currency == "USD"
    assert fy25.metrics.get("revenue") is not None
    assert fy25.metrics.get("ebitda") is not None
    assert fy25.metrics.get("eps") is not None
    # Margin rows should not overwrite the raw values for EBITDA / net income
    assert fy25.metrics.get("ebitda_margin_pct") is not None
    assert abs(fy25.metrics["ebitda_margin_pct"] - 39.15) < 0.5
    assert fy25.metrics["ebitda"] > 1_000  # millions, not a percent

    fy26e = next(a for a in b.annuals if a.period_label == "FY 2026 Est")
    assert fy26e.is_estimate
    assert fy26e.metrics.get("revenue") is not None
    assert fy26e.metrics.get("fcf") is not None


@pytest.mark.skipif(not _has_files(SAFCO), reason="SAFCO Bloomberg files not present")
def test_safco_bundle_currency_and_wider_history():
    b = load_bloomberg_bundle(SAFCO)
    assert b.currency == "SAR"
    # SAFCO has 7 historical FY (2019-2025) + LTM + 2 estimates = 10 annuals
    assert len(b.annuals) >= 9
    years = [a.period_label for a in b.annuals]
    assert any("FY 2019" in y for y in years)
    assert any("FY 2025" in y for y in years)


@pytest.mark.skipif(not _has_files(SAFCO), reason="SAFCO Bloomberg files not present")
def test_safco_has_dps_row():
    """SAFCO's cons_q has a DPS row that ADNOCGAS lacks; parser must pick it up."""
    b = load_bloomberg_bundle(SAFCO)
    q_act = b.consensus_quarterly[0]
    dps_val, _n = q_act.metrics.get("dps", (None, None))
    assert dps_val == 4.0


def test_missing_files_returns_none():
    b = load_bloomberg_bundle("XYZ_NOT_A_REAL_TICKER.XX")
    assert b is None


def test_coverage_helper():
    cov = bloomberg_coverage(ADNOC)
    assert set(cov.keys()) == {"cons_q", "fa"}
    assert isinstance(cov["cons_q"], bool)
    assert isinstance(cov["fa"], bool)


def test_bloomberg_dir_is_under_repo():
    d = bloomberg_dir()
    assert d.name == "bloomberg"
    assert d.parent.name == "data"
