"""Tests for the v2 dashboard endpoints in src/api.py."""
from __future__ import annotations

from src.api import (
    v2_upcoming, v2_universe, v2_ticker_info, v2_recent_decks,
)


def test_upcoming_returns_expected_shape():
    r = v2_upcoming(horizon_days=14)
    assert "tickers" in r
    assert "generated_at" in r
    assert "horizon_days" in r
    assert isinstance(r["tickers"], list)


def test_upcoming_family_filter_subsets():
    """Filtering by a non-existent family returns empty; filtering by an
    existing one is a subset of all."""
    all_r = v2_upcoming(horizon_days=14)
    bank_r = v2_upcoming(horizon_days=14, family="bank")
    none_r = v2_upcoming(horizon_days=14, family="zzzfake")
    assert len(bank_r["tickers"]) <= len(all_r["tickers"])
    assert none_r["tickers"] == []


def test_universe_canonical_only_default():
    """With canonical_only=1 the count drops below total registry size."""
    canon = v2_universe(canonical_only=1, limit=10)
    all_inc = v2_universe(canonical_only=0, limit=10)
    assert canon["total"] <= all_inc["total"]


def test_universe_sorts_by_market_cap_desc():
    r = v2_universe(canonical_only=1, limit=10)
    mcaps = [t["market_cap_usd"] for t in r["tickers"] if t["market_cap_usd"]]
    assert mcaps == sorted(mcaps, reverse=True), \
        "universe should be sorted by USD market cap descending"


def test_ticker_info_known():
    r = v2_ticker_info("BKMB.OM")
    assert r["template_family"] == "bank"
    assert r["currency"] == "OMR"


def test_ticker_info_unknown_returns_default():
    r = v2_ticker_info("ZZZ.NOWHERE")
    assert r["template_family"] == "other"
    assert r["currency_unit_scale"] == 1


def test_recent_decks_returns_list():
    r = v2_recent_decks(limit=5)
    assert "decks" in r
    assert isinstance(r["decks"], list)
