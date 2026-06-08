"""Tests for src/services/live_quote.py.

The actual yfinance call is mocked — we don't make network requests
in unit tests. What we verify: the wrapping, the error handling,
the upsert merge, and the dataclass shape.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from src.services.live_quote import (
    LiveQuote, fetch_live_quote, merge_into_canonical, _safe,
)


def _now():
    return datetime.now(timezone.utc)


def test_safe_returns_first_non_none():
    d = {"a": None, "b": 42, "c": "hi"}
    assert _safe(d, "a", "b") == 42
    assert _safe(d, "c") == "hi"
    assert _safe(d, "missing", "also_missing") is None


def test_safe_handles_object_attributes():
    class O:
        x = 5
    assert _safe(O(), "x") == 5
    assert _safe(O(), "missing") is None


def test_live_quote_dataclass_serializes():
    lq = LiveQuote(ticker="T", ok=True, fetched_at=_now(),
                    price=100.0, market_cap=1e9, currency="USD")
    d = lq.as_dict()
    assert d["ticker"] == "T"
    assert d["ok"] is True
    assert d["price"] == 100.0
    assert "fetched_at" in d


def test_fetch_live_quote_handles_yfinance_missing(monkeypatch):
    """When yfinance isn't importable, return ok=False without raising."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("not installed in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    lq = fetch_live_quote("BKMB.OM")
    assert lq.ok is False
    assert any("yfinance" in w for w in lq.warnings)


def test_fetch_live_quote_handles_invalid_price():
    """yfinance returns price=None / negative → ok=False."""
    fake_fast = {"last_price": None}
    fake_ticker = MagicMock()
    fake_ticker.fast_info = fake_fast
    fake_ticker.info = {}

    with patch("yfinance.Ticker", return_value=fake_ticker):
        lq = fetch_live_quote("BAD.TX")
    assert lq.ok is False
    assert "invalid price" in (lq.warnings[0] if lq.warnings else "")


def test_fetch_live_quote_happy_path():
    fake_fast = {
        "last_price": 100.0,
        "market_cap": 1_000_000_000,
        "shares": 10_000_000,
        "year_high": 110.0,
        "year_low": 90.0,
        "previous_close": 98.0,
        "currency": "USD",
    }
    fake_ticker = MagicMock()
    fake_ticker.fast_info = fake_fast

    with patch("yfinance.Ticker", return_value=fake_ticker):
        lq = fetch_live_quote("OK.TX")
    assert lq.ok is True
    assert lq.price == 100.0
    assert lq.market_cap == 1_000_000_000
    assert lq.fifty_two_week_high == 110.0
    assert lq.fifty_two_week_low == 90.0
    assert lq.currency == "USD"


def test_merge_into_canonical_no_op_when_not_ok():
    lq = LiveQuote(ticker="T", ok=False, fetched_at=_now())
    result = merge_into_canonical("T", lq)
    assert result["updated"] is False


def test_merge_into_canonical_upserts_when_ok():
    """When ok, the merger writes to canonical_store. We patch the
    write-side to avoid touching real storage."""
    lq = LiveQuote(ticker="T", ok=True, fetched_at=_now(),
                    price=100.0, market_cap=1e9)
    with patch("src.services.canonical_store.upsert_reconciled") as mock_upsert:
        result = merge_into_canonical("T", lq)
    assert result["updated"] is True
    # Two upserts: current_price + market_cap.
    assert mock_upsert.call_count == 2
    assert "current_price" in result["fields"]
    assert "market_cap" in result["fields"]
