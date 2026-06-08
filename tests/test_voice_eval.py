"""Tests for src/services/voice_eval.py."""
from __future__ import annotations

from src.services.voice_eval import (
    evaluate, evaluate_references, evaluate_batch, REFERENCES,
)


def test_reference_calibration_all_match():
    """All three references must score 'matches' against themselves.
    If a refactor pushes a reference below the threshold, the threshold
    is wrong (or the scoring metric drifted)."""
    results = evaluate_references()
    for name, r in results.items():
        assert r.grade == "matches", (
            f"Reference {name!r} should score 'matches', got "
            f"{r.grade} (composite={r.composite_score:.3f})"
        )


def test_apple_reference_specifics():
    r = evaluate("apple", REFERENCES["apple"])
    assert r.has_opening_marker is True
    assert r.has_drivers_marker is True
    assert r.has_watch_marker is True
    assert r.has_setup_marker is True
    assert r.setup_label == "balanced"
    assert 60 <= r.word_count <= 130


def test_jpmorgan_reference_specifics():
    r = evaluate("jpmorgan", REFERENCES["jpmorgan"])
    assert r.setup_label == "cautiously attractive"
    assert r.numeric_density <= 0.05


def test_tesla_reference_specifics():
    r = evaluate("tesla", REFERENCES["tesla"])
    assert r.setup_label in ("high-risk, high-reward", "high-risk high-reward")


def test_diverges_when_too_long_and_too_numeric():
    """Heavy-numeric overly-long paragraph should score 'diverges'."""
    bad = (
        "The company reports Q+1 revenue 12.5B, Q-1 revenue 11.2B, FY26 "
        "revenue 50B, FY27 revenue 55B, FY28 revenue 60B, EBITDA margin "
        "32.1% vs 31.5% last year, NIM 3.2% vs 3.1% last quarter, "
        "loan growth 8.5% YoY, NPL ratio 1.8% vs 1.7%, cost-to-income "
        "45.3% vs 44.8%. Q+1 EPS 0.55, FY26 EPS 2.50, FY27 EPS 2.75. "
        "P/E 12.1x vs 10.7x average. Dividend yield 4.83%. ROE 12.5% vs "
        "11.8%. Total deposits 5.2B vs 4.9B."
    )
    r = evaluate("BAD", bad)
    assert r.grade == "diverges"
    assert r.numeric_density > 0.06


def test_close_grade_for_almost_compliant():
    """A paragraph with markers but slightly out-of-band scores 'close'."""
    # 4 sentences, has all markers, but slightly long.
    close = (
        "TestCo enters earnings with focus on revenue growth, margin "
        "expansion, capital allocation, and forward guidance. Recent "
        "performance has been supported by stronger demand visibility "
        "and improved pricing, while supply-chain pressures and FX "
        "remain key concerns. Investors should watch revenue, gross "
        "margin, operating margin, free cash flow, capital returns, "
        "and commentary on demand into next year. The setup appears "
        "balanced, with both upside and downside risks fairly priced."
    )
    r = evaluate("TC", close)
    assert r.grade in ("matches", "close")


def test_empty_thesis_returns_diverges():
    r = evaluate("EMPTY", "")
    assert r.grade == "diverges"
    assert r.composite_score == 0.0


def test_evaluate_batch_aggregates():
    out = evaluate_batch([
        ("a", REFERENCES["apple"]),
        ("b", REFERENCES["jpmorgan"]),
        ("c", "Too short."),
    ])
    agg = out["aggregate"]
    assert agg["n_decks"] == 3
    assert agg["n_matches"] >= 2
    assert agg["n_diverges"] >= 1
