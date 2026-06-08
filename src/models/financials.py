"""Financial data models — quotes, period snapshots, derived metrics."""

from __future__ import annotations
from pydantic import BaseModel


class QuoteSnapshot(BaseModel):
    ticker:     str
    price:      float | None = None
    change:     float | None = None
    change_pct: float | None = None
    volume:     int   | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    forward_pe: float | None = None
    trailing_pe: float | None = None
    dividend_yield: float | None = None  # decimal (e.g. 0.045)
    price_to_book: float | None = None
    ev_to_ebitda: float | None = None
    target_mean_price: float | None = None
    target_high_price: float | None = None
    target_low_price: float | None = None
    recommendation_key: str | None = None  # "buy", "hold", "sell", "strong_buy", etc.
    number_of_analysts: int | None = None
    currency:   str          = "USD"
    source:     str          = "yahoo"
    # 1-year daily closing-price history (parallel arrays). Populated by
    # fetch_quote when yfinance returns a history; empty otherwise. Used by
    # the slide-3 price chart. Kept on QuoteSnapshot rather than a separate
    # model to avoid payload-shape changes elsewhere.
    price_history_dates:  list[str]   = []   # ISO YYYY-MM-DD
    price_history_prices: list[float] = []


class FinancialPeriod(BaseModel):
    """One row of financial data (actual or consensus)."""
    period_label:      str              # "2024-Q3" or "FY2024"
    period_type:       str              # "quarterly" | "annual" | "estimate"
    source:            str              # "yahoo" | "marketscreener"
    is_consensus:      bool = False
    revenue:           float | None = None
    ebitda:            float | None = None    # None for banks
    nii:               float | None = None    # Net Interest Income; banks only
    ebit:              float | None = None
    net_income:        float | None = None
    eps:               float | None = None
    dps:               float | None = None    # dividends per share
    announcement_date: str   | None = None
    currency:          str          = "SAR"


class DerivedMetrics(BaseModel):
    ticker:                    str
    is_bank:                   bool = False
    quarterly_revenue_growth:  list[dict] = []   # [{period, pct}, ...]
    avg_4q_revenue_growth:     float | None = None
    quarterly_ni_growth:       list[dict] = []
    avg_4q_ni_growth:          float | None = None
    pe_forward:                float | None = None
    ev_ebitda:                 float | None = None
    pb_ratio:                  float | None = None
    div_yield_pct:             float | None = None
    consensus_target_price:    float | None = None
    upside_pct:                float | None = None
    pe_vs_sector_pct:          float | None = None
    ev_ebitda_vs_sector_pct:   float | None = None
    warnings:                  list[str]  = []
