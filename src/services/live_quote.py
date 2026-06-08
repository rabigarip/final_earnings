"""Live quote refresher — fetches intraday-volatile fields from yfinance
at deck generation time and merges into the canonical store BEFORE the
deck is rendered.

Motivation: Investing.com snapshots cache for days/weeks. The MS GHA
cron refreshes daily. Neither catches the analyst's "I'm looking at
Investing right now in another tab and it says 0.427, not 0.41"
moment. yfinance is live (delayed ~15 min) and free; one call per deck
generation gives us same-day-fresh price + market cap + range.

Scope is deliberately narrow — only the fields that move intraday:

  * current_price
  * market_cap (recomputed from live price × shares_outstanding)
  * 52w high / low (Yahoo's `fiftyTwoWeekHigh` / `Low`)
  * Day's range (intraday high/low — not currently rendered, but
    inexpensive to capture for future use)

Slow-moving fields (target_price, analyst rating split, dividend
yield, consensus estimates, P/E) stay on their existing source paths
— refreshing them live is unnecessary churn and the live-quote API
doesn't expose them reliably anyway.

Failure mode: Yahoo doesn't cover the ticker, rate-limits, returns
malformed data. ALL of these fall through silently; the snapshot path
continues to feed the deck. Live data is a freshness bonus, not a
hard dependency. The freshness-banner on slide 1 will reflect what
actually happened.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class LiveQuote:
    """Outcome of one yfinance call. `ok=False` means we have nothing
    fresh and the snapshot path remains the source of truth."""
    ticker: str
    ok: bool
    fetched_at: datetime
    source: str = "yfinance"
    price: Optional[float] = None
    market_cap: Optional[float] = None
    shares_outstanding: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    previous_close: Optional[float] = None
    currency: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker, "ok": self.ok,
            "fetched_at": self.fetched_at.isoformat(),
            "source": self.source,
            "price": self.price, "market_cap": self.market_cap,
            "shares_outstanding": self.shares_outstanding,
            "fifty_two_week_high": self.fifty_two_week_high,
            "fifty_two_week_low": self.fifty_two_week_low,
            "day_high": self.day_high, "day_low": self.day_low,
            "previous_close": self.previous_close,
            "currency": self.currency, "warnings": self.warnings,
        }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_live_quote(ticker: str, *, timeout_s: float = 5.0) -> LiveQuote:
    """One yfinance call. Returns a LiveQuote — `ok=False` on any
    failure (network, rate limit, malformed response).

    `timeout_s` is the hard wall — we don't want a slow Yahoo response
    to block deck generation. yfinance doesn't expose a direct
    timeout knob; we wrap the call so a long-running fetch is at
    least logged loudly."""
    started = _now_utc()
    try:
        import yfinance as yf
    except ImportError:
        return LiveQuote(ticker=ticker, ok=False, fetched_at=started,
                         warnings=["yfinance not installed"])
    try:
        t0 = time.time()
        # `fast_info` is yfinance's lightweight quote endpoint —
        # significantly faster than `.info` which pulls a giant payload.
        # Falls back to .info when fast_info is missing fields.
        tk = yf.Ticker(ticker)
        fast = getattr(tk, "fast_info", None) or {}
        price = _safe(fast, "last_price") or _safe(fast, "lastPrice")
        mcap = _safe(fast, "market_cap") or _safe(fast, "marketCap")
        shares = _safe(fast, "shares") or _safe(fast, "sharesOutstanding")
        hi52 = _safe(fast, "year_high") or _safe(fast, "fiftyTwoWeekHigh")
        lo52 = _safe(fast, "year_low") or _safe(fast, "fiftyTwoWeekLow")
        day_hi = _safe(fast, "day_high") or _safe(fast, "dayHigh")
        day_lo = _safe(fast, "day_low") or _safe(fast, "dayLow")
        prev_close = _safe(fast, "previous_close") or _safe(fast, "previousClose")
        currency = (_safe(fast, "currency") or "").upper() or None

        # If fast_info is sparse, do one .info fallback for the missing
        # fields. .info is slow (~1-2s) but only the missing keys are
        # consulted.
        if price is None or mcap is None or hi52 is None:
            try:
                info = tk.info or {}
                price = price or _safe(info, "regularMarketPrice") or _safe(info, "currentPrice")
                mcap = mcap or _safe(info, "marketCap")
                shares = shares or _safe(info, "sharesOutstanding")
                hi52 = hi52 or _safe(info, "fiftyTwoWeekHigh")
                lo52 = lo52 or _safe(info, "fiftyTwoWeekLow")
                day_hi = day_hi or _safe(info, "dayHigh")
                day_lo = day_lo or _safe(info, "dayLow")
                prev_close = prev_close or _safe(info, "previousClose")
                currency = currency or (_safe(info, "currency") or "").upper() or None
            except Exception as exc:
                log.debug("yfinance .info fallback failed for %s: %s",
                          ticker, exc)

        elapsed = time.time() - t0
        if elapsed > timeout_s:
            log.warning("[live_quote] %s yfinance call took %.1fs (> %.1fs)",
                        ticker, elapsed, timeout_s)

        # Reject obviously-bad responses (zero/negative price).
        if not isinstance(price, (int, float)) or price <= 0:
            return LiveQuote(ticker=ticker, ok=False, fetched_at=started,
                              warnings=[f"yfinance returned invalid price: {price!r}"])

        return LiveQuote(
            ticker=ticker, ok=True, fetched_at=_now_utc(),
            price=float(price),
            market_cap=float(mcap) if isinstance(mcap, (int, float)) and mcap > 0 else None,
            shares_outstanding=float(shares) if isinstance(shares, (int, float)) and shares > 0 else None,
            fifty_two_week_high=float(hi52) if isinstance(hi52, (int, float)) and hi52 > 0 else None,
            fifty_two_week_low=float(lo52) if isinstance(lo52, (int, float)) and lo52 > 0 else None,
            day_high=float(day_hi) if isinstance(day_hi, (int, float)) and day_hi > 0 else None,
            day_low=float(day_lo) if isinstance(day_lo, (int, float)) and day_lo > 0 else None,
            previous_close=float(prev_close) if isinstance(prev_close, (int, float)) and prev_close > 0 else None,
            currency=currency,
        )
    except Exception as exc:
        log.warning("[live_quote] %s yfinance call failed: %s", ticker, exc)
        return LiveQuote(ticker=ticker, ok=False, fetched_at=_now_utc(),
                          warnings=[f"{type(exc).__name__}: {exc}"])


def _safe(d, *keys):
    """fast_info acts like a dict; .info is a dict; both can raise on
    missing-attr access. Coerce attribute / key access to None."""
    for k in keys:
        try:
            v = d[k] if hasattr(d, "__getitem__") else getattr(d, k, None)
            if v is not None:
                return v
        except (KeyError, AttributeError, TypeError):
            continue
    return None


def merge_into_canonical(ticker: str, live: LiveQuote) -> dict:
    """Take a successful LiveQuote and overwrite the canonical store
    cells for the volatile fields with the live values. Returns a dict
    describing what was overwritten (used by provenance + the slide-1
    freshness banner to credit yfinance-live)."""
    if not live.ok:
        return {"updated": False, "reason": "live quote not ok"}
    from src.services.canonical_store import upsert_reconciled
    updates: dict = {}
    fetched_iso = live.fetched_at.isoformat()
    note = f"live-fetched at {fetched_iso} via yfinance"

    def _upsert(field, value):
        if not isinstance(value, (int, float)) or value <= 0: return
        try:
            upsert_reconciled(
                ticker=ticker, field=field,
                canonical_value=float(value),
                canonical_source="yfinance-live",
                confidence="high",
                sources_with_value=["yfinance-live"],
                sources_agreeing=["yfinance-live"],
                max_disagreement_pct=None,
                notes=note,
            )
            updates[field] = float(value)
        except Exception as exc:
            log.warning("[live_quote] upsert %s/%s failed: %s",
                        ticker, field, exc)

    _upsert("current_price", live.price)
    _upsert("market_cap", live.market_cap)
    # 52w high/low are tracked on the same `historical_prices` cell in
    # canonical_store; we don't overwrite the daily-series cell from
    # here, but we DO surface the live-quote values via the live-quote
    # record itself (read by the snapshot renderer).
    return {"updated": True, "fields": list(updates.keys()),
            "fetched_at": fetched_iso, "source": "yfinance-live"}
