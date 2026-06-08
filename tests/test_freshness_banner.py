"""Tests for the slide-1 freshness banner builder."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.services.render_jabal_snapshot import _compute_freshness_banner


def _cv_cell(value, hours_old: int = 0):
    """Build a fake canonical-value cell with a `last_refreshed_at`."""
    c = MagicMock()
    c.value = value
    c.last_refreshed_at = (datetime.now(timezone.utc)
                            - timedelta(hours=hours_old))
    return c


def test_banner_with_live_quote():
    """When a successful live-quote record exists, prefer 'Price live'."""
    live_rec = {
        "ok": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "price": 100.0,
        "market_cap": 1e9,
    }
    banner = _compute_freshness_banner({}, live_rec)
    assert "Price live" in banner
    assert "Data freshness:" in banner


def test_banner_without_live_quote_shows_snapshot_age():
    """No live quote → 'Price snapshot {N}h old' or 'd old'."""
    cv = {
        "current_price": _cv_cell(0.41, hours_old=3),
    }
    banner = _compute_freshness_banner(cv, None)
    assert "Price snapshot" in banner
    assert "h old" in banner or "d old" in banner


def test_banner_old_snapshot_shows_days():
    cv = {
        "current_price": _cv_cell(0.41, hours_old=50),
    }
    banner = _compute_freshness_banner(cv, None)
    assert "2d old" in banner


def test_banner_includes_forecast_tier():
    cv = {
        "current_price": _cv_cell(0.41, hours_old=2),
        "target_price": _cv_cell({"mean": 0.47}, hours_old=20),
        "dividend_yield": _cv_cell(4.83, hours_old=20),
    }
    banner = _compute_freshness_banner(cv, None)
    assert "Forecasts" in banner


def test_banner_includes_macro_year():
    cv = {
        "company_profile": _cv_cell({"macro_year": "2026"}),
    }
    banner = _compute_freshness_banner(cv, None)
    assert "IMF 2026" in banner


def test_banner_empty_when_no_data():
    banner = _compute_freshness_banner({}, None)
    assert banner == ""
