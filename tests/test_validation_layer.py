"""Tests for src/services/validation_layer.py."""
from __future__ import annotations

from src.services.validation_layer import (
    run_tier_1, run_tier_2, ValidationReport, ValidationFinding,
    validate_consensus_plausibility, validate_source_agreement,
    validate_macro_freshness, _token_to_float,
)


def _empty_report(ticker="T"):
    return ValidationReport(ticker=ticker, generated_at="2026-01-01T00:00:00")


def test_consensus_plausibility_flags_5x_overshoot():
    rep = _empty_report()
    validate_consensus_plausibility(
        "T",
        next_q_consensus={"revenue": 5000, "eps": 2.5},
        trailing_actuals=[{"revenue": 1000, "eps": 0.5} for _ in range(4)],
        report=rep,
    )
    assert rep.n_warnings >= 2   # both revenue and EPS overshoot the band


def test_consensus_plausibility_quiet_when_in_band():
    rep = _empty_report()
    validate_consensus_plausibility(
        "T",
        next_q_consensus={"revenue": 1100, "eps": 0.55},
        trailing_actuals=[{"revenue": 1000, "eps": 0.5} for _ in range(4)],
        report=rep,
    )
    assert rep.n_warnings == 0


def test_consensus_plausibility_skips_when_no_history():
    rep = _empty_report()
    validate_consensus_plausibility(
        "T",
        next_q_consensus={"revenue": 9999, "eps": 99},
        trailing_actuals=[],
        report=rep,
    )
    assert len(rep.findings) == 0


def test_source_agreement_quiet_under_tolerance():
    rep = _empty_report()
    validate_source_agreement(
        "T", "field_x",
        {"yfinance": 100.0, "investing": 102.0},   # 2% disagreement
        rep, tolerance_pct=5.0,
    )
    assert len(rep.findings) == 0


def test_source_agreement_flags_warn_at_10pct():
    rep = _empty_report()
    validate_source_agreement(
        "T", "field_x",
        {"yfinance": 100.0, "investing": 115.0},   # 15% disagreement
        rep, tolerance_pct=5.0,
    )
    assert rep.n_warnings == 1
    assert rep.n_errors == 0


def test_source_agreement_flags_error_at_50pct():
    rep = _empty_report()
    validate_source_agreement(
        "T", "field_x",
        {"a": 100.0, "b": 200.0},   # 100% disagreement
        rep,
    )
    assert rep.n_errors == 1


def test_macro_freshness_flags_stale():
    rep = _empty_report()
    validate_macro_freshness(
        "T", {"gdp_growth_year": 2018}, rep, max_age_years=2,
    )
    assert len(rep.findings) >= 1


def test_macro_freshness_quiet_when_recent():
    rep = _empty_report()
    validate_macro_freshness(
        "T", {"gdp_growth_year": 2026, "inflation_year": 2026}, rep,
    )
    assert len(rep.findings) == 0


def test_token_to_float_strips_suffixes():
    assert _token_to_float("12.1x") == 12.1
    assert _token_to_float("4.83%") == 4.83
    assert _token_to_float("100") == 100.0
    assert _token_to_float("not a num") is None


def test_run_tier_2_catches_hallucinated_numbers():
    rep = run_tier_2(
        "T",
        thesis_paragraph="Trades at 99.9x premium with 33% margin.",
        allowed_numbers={12.1, 4.83},
    )
    assert rep.n_warnings == 1
    assert "thesis_paragraph" in [f.metric for f in rep.findings]


def test_run_tier_2_silent_when_all_numbers_match():
    rep = run_tier_2(
        "T",
        thesis_paragraph="Trades at 12.1x premium with 4.83% yield.",
        allowed_numbers={12.1, 4.83},
    )
    assert rep.n_warnings == 0


def test_full_report_dict_serializes():
    rep = _empty_report()
    rep.add(ValidationFinding(tier=1, severity="warn",
                                metric="m", message="msg"))
    d = rep.as_dict()
    assert d["n_warnings"] == 1
    assert d["findings"][0]["severity"] == "warn"
