"""Yahoo-coverage guard.

Some exchanges in our universe are simply not on Yahoo Finance (every call
404s) — notably the Oman MSX (BKMB.OM, OQEP.OM, …). The pipeline already
falls back to Investing/MarketScreener for those, but it still fired ~10
yfinance round-trips per generation (validate, quote, financials, balance
sheet, cash flow, valuation, target, rating, …), each printing an HTTP-404
and wasting a network call.

This guard short-circuits those calls: known region-gated suffixes are blind
up front, and any ticker that 404s at runtime is remembered for the rest of
the process so the remaining calls skip it too.
"""
from __future__ import annotations

# Exchanges Yahoo does not cover in this universe.
_BLIND_SUFFIXES = (".OM",)          # Oman — Muscat Securities Market

_dynamic_blind: set[str] = set()


def is_yahoo_blind(ticker: str) -> bool:
    t = (ticker or "").strip().upper()
    if not t:
        return False
    if any(t.endswith(s) for s in _BLIND_SUFFIXES):
        return True
    return t in _dynamic_blind


def mark_yahoo_blind(ticker: str) -> None:
    """Remember a ticker that 404'd so later calls in this run skip Yahoo."""
    t = (ticker or "").strip().upper()
    if t:
        _dynamic_blind.add(t)
