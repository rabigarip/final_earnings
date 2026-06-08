"""
Yahoo Finance probe provider (Stage 1).

Wraps `yfinance` calls and emits one field per `_fetch_<field>` method,
following the Provider contract in `probe_harness.py`.

Coverage prior: strong for US / India / China / HK, weak-to-broken for
MENA (most `.OM` and some `.AE` return 404 on .info). The probe makes
this explicit per ticker × field rather than aggregating to a single
"yahoo works" boolean.

Caching strategy: yfinance objects are NOT cached at the library level
between calls within one probe run, so we accept the per-call cost.
Raw responses (the JSON `info` dict, the DataFrame rows) get
persisted via `persist_raw` so re-running the reconciler is free.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import yfinance as yf
except ImportError:  # pragma: no cover — yfinance is a hard dep, but tolerate
    yf = None  # type: ignore

from src.services.probe_harness import Provider, persist_raw


def _safe(d: dict | None, key: str, default=None):
    if not d:
        return default
    v = d.get(key)
    if v is None:
        return default
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return default
    return v


def _df_to_records(df) -> list[dict]:
    """Convert a yfinance DataFrame to list-of-dicts safely (NaN → None)."""
    if df is None or df.empty:
        return []
    try:
        records = []
        for col in df.columns:
            row = {"period_end": str(col)}
            for idx in df.index:
                val = df.loc[idx, col]
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    val = None
                else:
                    val = float(val) if isinstance(val, (int, float)) else str(val)
                row[str(idx)] = val
            records.append(row)
        return records
    except Exception:
        return []


class YahooProvider(Provider):
    name = "yahoo"

    def __init__(self):
        if yf is None:
            raise RuntimeError("yfinance is not installed")
        self._ticker_cache: dict[str, Any] = {}

    def _t(self, ticker: str):
        """Memoise the yfinance Ticker object across fields of one probe."""
        if ticker not in self._ticker_cache:
            self._ticker_cache[ticker] = yf.Ticker(ticker)
        return self._ticker_cache[ticker]

    # ── Identity / market ──

    def _fetch_current_price(self, ticker: str):
        t = self._t(ticker)
        info = t.info or {}
        raw_id = persist_raw(self.name, ticker, "current_price", info)
        price = (
            _safe(info, "currentPrice")
            or _safe(info, "regularMarketPrice")
            or _safe(info, "previousClose")
        )
        if price is None:
            raise ValueError("no price in info")
        return (
            round(float(price), 4),
            (info.get("currency") or ""),
            "",  # live quote
            raw_id,
        )

    def _fetch_historical_prices(self, ticker: str):
        t = self._t(ticker)
        # 1-year daily history is enough for the test panel.
        hist = t.history(period="1y", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            raise ValueError("no historical bars")
        records = []
        for idx, row in hist.iterrows():
            records.append({
                "date": str(idx)[:10],
                "open": float(row["Open"]) if not math.isnan(row["Open"]) else None,
                "high": float(row["High"]) if not math.isnan(row["High"]) else None,
                "low":  float(row["Low"])  if not math.isnan(row["Low"])  else None,
                "close":float(row["Close"]) if not math.isnan(row["Close"]) else None,
                "volume": int(row["Volume"]) if not math.isnan(row["Volume"]) else None,
            })
        raw_id = persist_raw(self.name, ticker, "historical_prices", records)
        info = t.info or {}

        # Derive perf_* deltas + 52w range + YTD from the close series.
        # The renderer reads these from the canonical value dict, so we
        # surface them here (compact summary) rather than asking the
        # consumer to re-parse the full bar list. Full bars stay in the
        # persisted raw_response for any deeper analysis.
        closes = [r["close"] for r in records if r["close"] is not None]
        dates  = [r["date"]  for r in records if r["close"] is not None]
        current = closes[-1] if closes else None

        def _at_days_back(n_trading_days: int):
            if not closes or n_trading_days >= len(closes):
                return None
            return closes[-1 - n_trading_days]

        def _pct(now, then):
            if now is None or then is None or then == 0:
                return None
            return round((now / then - 1.0) * 100, 2)

        # ~21 trading days ≈ 1 month
        perf_1d  = _pct(current, _at_days_back(1))
        perf_1w  = _pct(current, _at_days_back(5))
        perf_1m  = _pct(current, _at_days_back(21))
        perf_3m  = _pct(current, _at_days_back(63))
        perf_6m  = _pct(current, _at_days_back(126))

        # YTD: first close of the current calendar year
        ytd_anchor = None
        if dates and current is not None:
            yr = dates[-1][:4]
            for d, c in zip(dates, closes):
                if d.startswith(yr):
                    ytd_anchor = c
                    break
        perf_ytd = _pct(current, ytd_anchor)

        rng_low  = min(closes) if closes else None
        rng_high = max(closes) if closes else None

        summary = {
            "n_bars": len(records),
            "first": records[0]["date"],
            "last":  records[-1]["date"],
            "perf_1d":  perf_1d,
            "perf_1w":  perf_1w,
            "perf_1m":  perf_1m,
            "perf_3m":  perf_3m,
            "perf_6m":  perf_6m,
            "perf_ytd": perf_ytd,
            "range_52w_low":  rng_low,
            "range_52w_high": rng_high,
            # Compact close-only series for the slide-3 line chart
            # (every 5th bar to keep canonical_store size manageable)
            "close_series": [
                {"date": d, "close": c}
                for d, c in list(zip(dates, closes))[::5]
            ],
        }
        return (
            summary,
            (info.get("currency") or ""),
            records[-1]["date"],
            raw_id,
        )

    def _fetch_market_cap(self, ticker: str):
        info = self._t(ticker).info or {}
        raw_id = persist_raw(self.name, ticker, "market_cap", info)
        mcap = _safe(info, "marketCap")
        if mcap is None:
            raise ValueError("no marketCap")
        return (
            float(mcap),
            (info.get("currency") or ""),
            "",
            raw_id,
        )

    def _fetch_company_profile(self, ticker: str):
        info = self._t(ticker).info or {}
        raw_id = persist_raw(self.name, ticker, "company_profile", info)
        profile = {
            "name":       info.get("shortName") or info.get("longName"),
            "sector":     info.get("sector"),
            "industry":   info.get("industry"),
            "country":    info.get("country"),
            "currency":   info.get("currency"),
            "summary":    (info.get("longBusinessSummary") or "")[:500],
            "website":    info.get("website"),
        }
        if not any(profile.values()):
            raise ValueError("info empty — Yahoo has no profile for this ticker")
        return (profile, "", "", raw_id)

    # ── Financials ──

    def _fetch_income_statement_annual(self, ticker: str):
        t = self._t(ticker)
        df = t.financials  # annual
        recs = _df_to_records(df)
        if not recs:
            raise ValueError("no annual income statement")
        raw_id = persist_raw(self.name, ticker, "income_statement_annual", recs)
        info = t.info or {}
        return (
            {"n_periods": len(recs), "fields_present": list({k for r in recs for k in r.keys()})[:10]},
            (info.get("financialCurrency") or info.get("currency") or ""),
            recs[0].get("period_end", ""),
            raw_id,
        )

    def _fetch_income_statement_quarterly(self, ticker: str):
        t = self._t(ticker)
        df = t.quarterly_financials
        recs = _df_to_records(df)
        if not recs:
            raise ValueError("no quarterly income statement")
        raw_id = persist_raw(self.name, ticker, "income_statement_quarterly", recs)
        info = t.info or {}
        return (
            {"n_periods": len(recs), "fields_present": list({k for r in recs for k in r.keys()})[:10]},
            (info.get("financialCurrency") or info.get("currency") or ""),
            recs[0].get("period_end", ""),
            raw_id,
        )

    def _fetch_balance_sheet(self, ticker: str):
        t = self._t(ticker)
        df = t.balance_sheet  # annual
        recs = _df_to_records(df)
        if not recs:
            raise ValueError("no balance sheet")
        raw_id = persist_raw(self.name, ticker, "balance_sheet", recs)
        info = t.info or {}
        return (
            {"n_periods": len(recs), "fields_present": list({k for r in recs for k in r.keys()})[:10]},
            (info.get("financialCurrency") or info.get("currency") or ""),
            recs[0].get("period_end", ""),
            raw_id,
        )

    def _fetch_cash_flow(self, ticker: str):
        t = self._t(ticker)
        df = t.cashflow  # annual
        recs = _df_to_records(df)
        if not recs:
            raise ValueError("no cash flow")
        raw_id = persist_raw(self.name, ticker, "cash_flow", recs)
        info = t.info or {}
        return (
            {"n_periods": len(recs), "fields_present": list({k for r in recs for k in r.keys()})[:10]},
            (info.get("financialCurrency") or info.get("currency") or ""),
            recs[0].get("period_end", ""),
            raw_id,
        )

    # ── Valuation ──

    def _fetch_valuation_historical(self, ticker: str):
        """Yahoo doesn't ship a full multi-year P/E history. The closest
        we get is trailingPE, forwardPE, priceToBook from `info`.
        This will show as low coverage in the matrix — that's the
        honest read for Yahoo on this field."""
        info = self._t(ticker).info or {}
        raw_id = persist_raw(self.name, ticker, "valuation_historical", info)
        vals = {
            "trailing_pe":   _safe(info, "trailingPE"),
            "forward_pe":    _safe(info, "forwardPE"),
            "price_to_book": _safe(info, "priceToBook"),
            "ev_to_ebitda":  _safe(info, "enterpriseToEbitda"),
            "ev_to_revenue": _safe(info, "enterpriseToRevenue"),
        }
        if all(v is None for v in vals.values()):
            raise ValueError("no valuation ratios in info")
        return (vals, "ratio", "", raw_id)

    def _fetch_dividend_yield(self, ticker: str):
        info = self._t(ticker).info or {}
        raw_id = persist_raw(self.name, ticker, "dividend_yield", info)
        # SCALE TRAP: the pinned yfinance (>=0.2.40) returns `dividendYield`
        # ALREADY AS A PERCENT (5.38 == 5.38%, 0.84 == 0.84%). The old code
        # multiplied by 100 → the "538%"/"84%" bugs. `trailingAnnualDividendYield`
        # is a fraction but is INCONSISTENT across tickers (for 9988.HK it
        # implies 5.9% when the real yield is 0.84%), so it is NOT a reliable
        # cross-check — use `dividendYield` as the percent directly, and only
        # fall back to the fraction field when `dividendYield` is missing.
        dy = _safe(info, "dividendYield")                  # percent (current yfinance)
        taty = _safe(info, "trailingAnnualDividendYield")  # fraction, fallback only
        pct = None
        if dy is not None:
            pct = float(dy)
        elif taty is not None:
            pct = float(taty) * 100.0
        if pct is None:
            raise ValueError("no dividend yield")
        # Sanity bound: listed-equity trailing yields live in ~0–40%.
        if pct < 0 or pct > 40:
            raise ValueError(f"dividend yield {pct:.2f}% out of sane range")
        return (round(pct, 3), "%", "", raw_id)

    def _fetch_valuation_forward(self, ticker: str):
        """yfinance gives us TWO sources for forward consensus:

          1. info.forwardEps / info.forwardPE — a single point estimate
             (the "FY+1" anchor that's always there for covered names).
          2. ticker.earnings_estimate / ticker.revenue_estimate — a
             rich panel-style table with avg/low/high + analyst count
             for 0q (current quarter), +1q (next quarter), 0y (current
             FY), +1y (next FY).

        Investing.com's earnings page only volunteers an FY guidance
        sentence for ~1 in 10 EM tickers (Aramco), so yfinance's table
        is the only realistic free-source FY estimate panel-wide.
        """
        t = self._t(ticker)
        info = t.info or {}
        bundle: dict = {
            "forward_pe":     _safe(info, "forwardPE"),
            "forward_eps":    _safe(info, "forwardEps"),
            "price_to_sales": _safe(info, "priceToSalesTrailing12Months"),
        }

        def _row(df, period: str) -> dict | None:
            """Pull one row from earnings_estimate / revenue_estimate. The
            yfinance DataFrame is indexed by period label."""
            if df is None:
                return None
            try:
                if period not in df.index:
                    return None
                row = df.loc[period]
            except (KeyError, AttributeError):
                return None
            def _f(k):
                try:
                    v = row.get(k) if hasattr(row, "get") else row[k]
                    f = float(v)
                    if f != f:   # NaN
                        return None
                    return f
                except (KeyError, TypeError, ValueError):
                    return None
            return {
                "avg":      _f("avg"),
                "low":      _f("low"),
                "high":     _f("high"),
                "n_analysts": int(_f("numberOfAnalysts") or 0) or None,
                "growth":   _f("growth"),
            }

        # The attribute accesses can raise on tickers without coverage;
        # tolerate that quietly.
        try:
            ee = t.earnings_estimate
        except Exception:
            ee = None
        try:
            re_ = t.revenue_estimate
        except Exception:
            re_ = None

        # FY+1 = current FY (column "0y" in yfinance); FY+2 = next FY (+1y).
        eps_fy1 = _row(ee, "0y")
        eps_fy2 = _row(ee, "+1y")
        eps_nq  = _row(ee, "+1q")
        rev_fy1 = _row(re_, "0y")
        rev_fy2 = _row(re_, "+1y")
        rev_nq  = _row(re_, "+1q")

        if eps_fy1: bundle["eps_fy1"] = eps_fy1.get("avg"); bundle["eps_fy1_detail"] = eps_fy1
        if eps_fy2: bundle["eps_fy2"] = eps_fy2.get("avg"); bundle["eps_fy2_detail"] = eps_fy2
        if eps_nq:  bundle["eps_next_q"] = eps_nq.get("avg"); bundle["eps_next_q_detail"] = eps_nq
        if rev_fy1: bundle["revenue_fy1"] = rev_fy1.get("avg")
        if rev_fy2: bundle["revenue_fy2"] = rev_fy2.get("avg")
        if rev_nq:  bundle["revenue_next_q"] = rev_nq.get("avg")

        raw_id = persist_raw(self.name, ticker, "valuation_forward", {
            "info_keys": list(bundle.keys()), "ticker": ticker,
        })
        if all(v is None for v in bundle.values()):
            raise ValueError("no forward valuation fields (info + estimates both empty)")
        return (bundle, "ratio", "", raw_id)

    # ── Analyst ──

    def _fetch_target_price(self, ticker: str):
        info = self._t(ticker).info or {}
        raw_id = persist_raw(self.name, ticker, "target_price", info)
        mean = _safe(info, "targetMeanPrice")
        if mean is None:
            raise ValueError("no targetMeanPrice")
        return ({
            "mean":   _safe(info, "targetMeanPrice"),
            "median": _safe(info, "targetMedianPrice"),
            "high":   _safe(info, "targetHighPrice"),
            "low":    _safe(info, "targetLowPrice"),
            "n_analysts": _safe(info, "numberOfAnalystOpinions"),
        }, (info.get("currency") or ""), "", raw_id)

    def _fetch_rating_split(self, ticker: str):
        """yfinance exposes `recommendations_summary` — a DataFrame with
        columns strongBuy / buy / hold / sell / strongSell across the
        last 4 months. We collapse to a single bucket count + a derived
        consensus label. Falls back to `recommendationKey` for tickers
        without coverage detail.
        """
        t = self._t(ticker)
        info = t.info or {}
        raw_id = persist_raw(self.name, ticker, "rating_split", info)

        # Try the richer recommendations_summary first
        try:
            rs = t.recommendations_summary
        except Exception:
            rs = None
        if rs is not None and len(rs) > 0:
            # Most-recent month is `period == "0m"`.
            try:
                row = rs.iloc[0]
                def _i(k):
                    try:
                        v = int(row.get(k) if hasattr(row, "get") else row[k] or 0)
                        return v
                    except (KeyError, TypeError, ValueError):
                        return 0
                buy_total   = _i("strongBuy") + _i("buy")
                hold_total  = _i("hold")
                sell_total  = _i("sell") + _i("strongSell")
                total       = buy_total + hold_total + sell_total
                if total > 0:
                    # Consensus label from majority
                    label = "OUTPERFORM"
                    if buy_total / total >= 0.6:        label = "BUY"
                    elif sell_total / total >= 0.4:     label = "SELL"
                    elif buy_total > sell_total:        label = "OUTPERFORM"
                    else:                                label = "HOLD"
                    return ({
                        "buy":   buy_total,
                        "hold":  hold_total,
                        "sell":  sell_total,
                        "total": total,
                        "consensus": label,
                    }, "", "", raw_id)
            except Exception:
                pass

        # Fallback: recommendationKey single bucket
        key = info.get("recommendationKey")
        n   = _safe(info, "numberOfAnalystOpinions")
        if not key:
            raise ValueError("no recommendations_summary or recommendationKey")
        return ({"consensus": (key or "").upper(), "n_analysts": n}, "", "", raw_id)
