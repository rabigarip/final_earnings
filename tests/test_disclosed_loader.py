"""Tests for src/services/disclosed_loader.py — the overlay logic."""
from __future__ import annotations

import json
from pathlib import Path

from src.services.disclosed_loader import (
    load_disclosed, overlay_surprise_history,
)


def test_bkmb_disclosed_loads():
    d = load_disclosed("BKMB.OM")
    assert d is not None
    assert d["ticker"] == "BKMB.OM"
    assert "quarterly" in d
    assert len(d["quarterly"]) == 4
    periods = {q["period"] for q in d["quarterly"]}
    assert "Q1 2025" in periods
    assert "Q1 2026" in periods


def test_disclosed_unit_scale_conversion():
    """JSON declares units='thousands' so the loader multiplies values
    by 1000 to bring them to raw currency (matches Investing's raw
    surprise_history shape). EPS is per-share — never scaled."""
    d = load_disclosed("BKMB.OM")
    q1 = next(q for q in d["quarterly"] if q["period"] == "Q1 2025")
    # Published Operating Income: 140,675 thousand → 140,675,000 raw
    assert q1["operating_income"] == 140_675_000
    assert q1["net_income"] == 58_561_000
    # EPS stays as the per-share quote.
    assert q1["eps"] == 0.008


def test_disclosed_missing_ticker_returns_none():
    assert load_disclosed("ZZZ.UNKNOWN") is None


def test_overlay_with_no_aggregator_data():
    """When the aggregator returned no surprise_history at all, the
    overlay produces a list from disclosed data alone — actuals only,
    no estimates."""
    rows, src_map = overlay_surprise_history("BKMB.OM", None)
    assert isinstance(rows, list)
    assert len(rows) == 4
    assert "Q1 2025" in src_map
    assert src_map["Q1 2025"].startswith("MSM_")
    # Each row has actuals populated, estimates None.
    for r in rows:
        assert r["_source"] == "company_disclosure"
        assert r["revenue_actual"] is not None
        assert r["revenue_estimate"] is None


def test_overlay_with_aggregator_preserves_estimates():
    """When the aggregator HAS surprise_history with estimates, the
    overlay keeps the estimate side and only overwrites actuals."""
    agg_rows = [
        {"period": "Q1 2025", "revenue_actual": 999_999_999,
          "revenue_estimate": 150_000_000, "eps_actual": 0.999,
          "eps_estimate": 0.007},
    ]
    rows, src_map = overlay_surprise_history("BKMB.OM", agg_rows)
    q1 = next(r for r in rows if r["period"] == "Q1 2025")
    # Disclosed actual wins.
    assert q1["revenue_actual"] == 140_675_000
    assert q1["eps_actual"] == 0.008
    # Aggregator estimate preserved.
    assert q1["revenue_estimate"] == 150_000_000
    assert q1["eps_estimate"] == 0.007
    # Surprise percentages recomputed against the disclosed actual.
    expected_rev_surprise = (140_675_000 - 150_000_000) / 150_000_000 * 100
    assert abs(q1["revenue_surprise_pct"] - expected_rev_surprise) < 0.01


def test_overlay_sorts_newest_first():
    """The renderer's chart expects newest-quarter on the right; the
    overlay must sort the merged list in descending period order."""
    rows, _ = overlay_surprise_history("BKMB.OM", None)
    periods = [r["period"] for r in rows]
    assert periods == ["Q1 2026", "Q3 2025", "Q2 2025", "Q1 2025"]
