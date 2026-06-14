"""
Yahoo Finance provider — wraps yfinance for all Yahoo data.

Used for: ticker validation, quote snapshots, income-statement data.
yfinance is community-maintained and wraps Yahoo's internal JSON API.
More reliable than HTML scraping, but still subject to Yahoo changes.

FRAGILITY: Pin yfinance version. Yahoo periodically changes their API
and yfinance patches follow within days. If yfinance breaks, the pipeline
falls back gracefully because every caller checks the StepResult status.

Yahoo may rate-limit (429). Retries with backoff and explicit warnings.
"""

from __future__ import annotations
import time
import pandas as pd
from src.providers._yf import yf
from src.models.financials import FinancialPeriod, QuoteSnapshot

# Retry config for Yahoo rate-limit / transient errors
_YAHOO_RETRIES = 2
_YAHOO_BACKOFF_SEC = 1.0


# ── Ticker validation ─────────────────────────────────────────

def validate_ticker(ticker: str) -> dict | None:
    """
    Hit Yahoo for ticker identity. Returns dict with basic info or None.
    This is the source-of-truth for "does this ticker exist?".
    """
    from src.providers._yahoo_blind import is_yahoo_blind, mark_yahoo_blind
    if is_yahoo_blind(ticker):
        return None
    try:
        yt = yf.Ticker(ticker)
        info = yt.info or {}
        name = info.get("shortName") or info.get("longName")
        if not name:
            mark_yahoo_blind(ticker)
            return None
        return {
            "name":       name,
            "exchange":   info.get("exchange", ""),
            "currency":   info.get("currency", ""),
            "market_cap": info.get("marketCap"),
            "quote_type": info.get("quoteType", ""),
        }
    except Exception:
        return None


# ── Quote snapshot ────────────────────────────────────────────

def _yahoo_retry(fn, *args, _warn_cb=None, **kwargs):
    """Run fn with retries and exponential backoff. On failure or 429, warn and return None."""
    last_exc = None
    for attempt in range(_YAHOO_RETRIES):
        try:
            out = fn(*args, **kwargs)
            if out is not None:
                return out
        except Exception as e:
            last_exc = e
            if "429" in str(e) or "rate" in str(e).lower() and _warn_cb:
                _warn_cb(f"Yahoo rate-limited or error: {e}")
            if attempt < _YAHOO_RETRIES - 1:
                time.sleep(_YAHOO_BACKOFF_SEC * (2 ** attempt))
    if last_exc and _warn_cb:
        _warn_cb(f"Yahoo fallback unavailable after {_YAHOO_RETRIES} attempts: {last_exc}")
    return None


def fetch_quote(ticker: str) -> QuoteSnapshot | None:
    """Fetch current price, change, market cap, and 1Y daily close history.

    Retries with backoff on failure. The history fetch is best-effort —
    price_history_dates/prices stay empty if yfinance doesn't return a
    valid DataFrame (common for illiquid frontier-market tickers).
    """
    from src.providers._yahoo_blind import is_yahoo_blind
    if is_yahoo_blind(ticker):
        return None
    def _get():
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if price is None:
                return None
            prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
            change = round(price - prev, 4) if prev else None
            pct = round((change / prev) * 100, 2) if (change is not None and prev) else None

            # 1Y daily close history — best effort.
            hist_dates: list[str] = []
            hist_prices: list[float] = []
            try:
                hist = t.history(period="1y", interval="1d", auto_adjust=False)
                if hist is not None and not hist.empty and "Close" in hist.columns:
                    closes = hist["Close"].dropna()
                    hist_dates = [idx.strftime("%Y-%m-%d") for idx in closes.index]
                    hist_prices = [float(v) for v in closes.values]
            except Exception:
                # Leave arrays empty; price chart will silently skip.
                hist_dates, hist_prices = [], []

            return QuoteSnapshot(
                ticker=ticker,
                price=price,
                change=change,
                change_pct=pct,
                volume=info.get("volume") or info.get("regularMarketVolume"),
                market_cap=info.get("marketCap"),
                enterprise_value=info.get("enterpriseValue"),
                forward_pe=info.get("forwardPE"),
                trailing_pe=info.get("trailingPE"),
                dividend_yield=info.get("dividendYield"),
                price_to_book=info.get("priceToBook"),
                ev_to_ebitda=info.get("enterpriseToEbitda"),
                target_mean_price=info.get("targetMeanPrice"),
                target_high_price=info.get("targetHighPrice"),
                target_low_price=info.get("targetLowPrice"),
                recommendation_key=info.get("recommendationKey"),
                number_of_analysts=info.get("numberOfAnalystOpinions"),
                currency=info.get("currency") or "USD",
                price_history_dates=hist_dates,
                price_history_prices=hist_prices,
            )
        except Exception:
            return None
    return _yahoo_retry(_get)


# ── Financials ────────────────────────────────────────────────

_REV  = ["Total Revenue", "TotalRevenue", "Revenue"]
_EBITDA = ["EBITDA", "Ebitda", "Normalized EBITDA"]
_NII  = ["Net Interest Income", "NetInterestIncome", "NII"]
_EBIT = ["EBIT", "Operating Income", "OperatingIncome"]
_NI   = ["Net Income", "NetIncome", "Net Income Common Stockholders"]
_EPS  = ["Basic EPS", "BasicEPS", "Diluted EPS", "DilutedEPS"]


def _find(df: pd.DataFrame, names: list[str]) -> str | None:
    for n in names:
        if n in df.index:
            return n
    return None


def _safe(df: pd.DataFrame, row: str | None, col) -> float | None:
    if row is None:
        return None
    try:
        v = df.loc[row, col]
        return None if pd.isna(v) else float(v)
    except (KeyError, TypeError):
        return None


def _extract(df: pd.DataFrame | None, period_type: str, currency: str,
             is_bank: bool) -> list[FinancialPeriod]:
    """Convert a yfinance income-statement DataFrame to FinancialPeriod list."""
    if df is None or df.empty:
        return []

    rev = _find(df, _REV)
    ebitda = _find(df, _EBITDA)
    nii = _find(df, _NII)
    ebit = _find(df, _EBIT)
    ni  = _find(df, _NI)
    eps = _find(df, _EPS)

    out: list[FinancialPeriod] = []
    for col_ts in df.columns:
        if period_type == "quarterly":
            q = (col_ts.month - 1) // 3 + 1
            label = f"{col_ts.year}-Q{q}"
        else:
            label = f"FY{col_ts.year}"

        out.append(FinancialPeriod(
            period_label=label,
            period_type=period_type,
            source="yahoo",
            revenue=_safe(df, rev, col_ts),
            ebitda=None if is_bank else _safe(df, ebitda, col_ts),
            nii=_safe(df, nii, col_ts) if is_bank else None,
            ebit=_safe(df, ebit, col_ts),
            net_income=_safe(df, ni, col_ts),
            eps=_safe(df, eps, col_ts),
            currency=currency,
        ))
    return out


def fetch_financials(ticker: str, currency: str, is_bank: bool
                     ) -> dict[str, list[FinancialPeriod]]:
    """
    Returns {"quarterly": [...], "annual": [...]}.
    Each list contains FinancialPeriod objects or is empty.
    """
    from src.providers._yahoo_blind import is_yahoo_blind
    if is_yahoo_blind(ticker):
        return {"quarterly": [], "annual": []}
    currency = (currency or "").strip() or "USD"
    try:
        yt = yf.Ticker(ticker)
        return {
            "quarterly": _extract(yt.quarterly_income_stmt, "quarterly", currency, is_bank),
            "annual":    _extract(yt.income_stmt, "annual", currency, is_bank),
        }
    except Exception:
        return {"quarterly": [], "annual": []}


# ── Analyst estimates (consensus fallback) ────────────────────

def fetch_analyst_estimates(ticker: str, currency: str) -> list[FinancialPeriod]:
    """
    Yahoo analyst estimates — used as consensus fallback when
    MarketScreener is unavailable.
    """
    from src.providers._yahoo_blind import is_yahoo_blind
    if is_yahoo_blind(ticker):
        return []
    try:
        yt = yf.Ticker(ticker)
        out: list[FinancialPeriod] = []

        # Revenue estimates
        rev_est = getattr(yt, "revenue_estimate", None)
        if rev_est is not None and not rev_est.empty:
            for col in rev_est.columns:
                try:
                    avg = float(rev_est.loc["avg", col])
                except (KeyError, TypeError, ValueError):
                    continue
                out.append(FinancialPeriod(
                    period_label=str(col), period_type="estimate",
                    source="yahoo", is_consensus=True,
                    revenue=avg, currency=currency,
                ))

        # EPS estimates — merge into existing if period matches
        eps_est = getattr(yt, "earnings_estimate", None)
        if eps_est is not None and not eps_est.empty:
            for col in eps_est.columns:
                try:
                    avg = float(eps_est.loc["avg", col])
                except (KeyError, TypeError, ValueError):
                    continue
                matched = False
                for e in out:
                    if e.period_label == str(col):
                        e.eps = avg
                        matched = True
                        break
                if not matched:
                    out.append(FinancialPeriod(
                        period_label=str(col), period_type="estimate",
                        source="yahoo", is_consensus=True,
                        eps=avg, currency=currency,
                    ))
        return out
    except Exception:
        return []


# ── Earnings date (calendar) ────────────────────────────────────────────────

def fetch_next_earnings_date(ticker: str) -> str | None:
    """
    Best-effort next earnings date from Yahoo (yfinance).
    Returns ISO date string 'YYYY-MM-DD' when available, else None.
    """
    from src.providers._yahoo_blind import is_yahoo_blind
    if is_yahoo_blind(ticker):
        return None
    try:
        yt = yf.Ticker(ticker)
        cal = getattr(yt, "calendar", None)
        # yfinance typically returns a DataFrame with index like 'Earnings Date'
        if cal is not None and hasattr(cal, "empty") and not cal.empty:
            try:
                # Row may be 'Earnings Date' or 'Earnings Date' (case varies)
                for key in ["Earnings Date", "Earnings date", "EarningsDate", "Earnings"]:
                    if key in cal.index:
                        v = cal.loc[key]
                        # v can be a scalar, Series, or list-like of timestamps
                        if hasattr(v, "tolist"):
                            xs = [x for x in v.tolist() if x is not None]
                        else:
                            xs = [v] if v is not None else []
                        for x in xs:
                            # pandas Timestamp / datetime-like
                            dt = getattr(x, "to_pydatetime", None)() if hasattr(x, "to_pydatetime") else x
                            if hasattr(dt, "date"):
                                return dt.date().isoformat()
            except Exception:
                pass
        # Fallback: yfinance sometimes exposes earnings_dates dataframe
        ed = getattr(yt, "earnings_dates", None)
        if ed is not None and hasattr(ed, "empty") and not ed.empty:
            # pick the first upcoming row
            try:
                idx = ed.index
                if len(idx) > 0:
                    dt = idx[0]
                    dt = getattr(dt, "to_pydatetime", None)() if hasattr(dt, "to_pydatetime") else dt
                    if hasattr(dt, "date"):
                        return dt.date().isoformat()
            except Exception:
                pass
    except Exception:
        return None
    return None
