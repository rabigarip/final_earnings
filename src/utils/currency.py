"""Currency conversion utility (Yahoo FX only).

Governance: do not invent FX rates. If Yahoo FX is unavailable, return None
and let callers decide how to display / warn.
"""

from __future__ import annotations

from functools import lru_cache
import yfinance as yf


@lru_cache(maxsize=32)
def get_fx_rate(from_ccy: str, to_ccy: str) -> float | None:
    """Get FX rate; return 1.0 for same currency; None when unavailable."""
    f = (from_ccy or "").strip().upper()
    t = (to_ccy or "").strip().upper()
    if not f or not t or f == t:
        return 1.0
    pair = f"{f}{t}=X"
    try:
        info = yf.Ticker(pair).info or {}
        rate = info.get("regularMarketPrice") or info.get("previousClose")
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate)
    except Exception:
        pass
    return None


def convert(amount: float | None, from_ccy: str, to_ccy: str) -> float | None:
    """Convert amount or return None when amount is None."""
    if amount is None:
        return None
    try:
        rate = get_fx_rate(from_ccy, to_ccy)
        if rate is None:
            return None
        return float(amount) * float(rate)
    except Exception:
        return None

