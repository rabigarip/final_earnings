"""
QA policy and thresholds for earnings preview memo validation.

Primary sources, staleness, tolerances, and suppression rules.
Do not mix sources silently; prefer primary source per field.
"""

from __future__ import annotations

# Primary source by field (for header / summary strip)
PRIMARY_SOURCE = {
    "expected_report_date": "marketscreener_calendar",
    "recommendation": "marketscreener_consensus",
    "analyst_count": "marketscreener_consensus",
    "target_price": "marketscreener_consensus",
    "upside_pct": "marketscreener_consensus",
    "current_price": "marketscreener_consensus",  # prefer consensus page price for strip consistency
    "current_price_quote": "yahoo_quote",
    "quarterly_actuals": "marketscreener_calendar",
    "valuation_multiples": "marketscreener_valuation",
    "dividend_forecasts": "marketscreener_valuation_dividend",
}

# Staleness: max age (seconds) for "recent" snapshot before flagging
STALE_THRESHOLD_SECONDS = 86400 * 2  # 2 days

# Timestamp mismatch: if two snapshots differ by more than this, flag
TIMESTAMP_MISMATCH_THRESHOLD_SECONDS = 3600  # 1 hour

# Price mismatch: if consensus_page_price vs quote_price differ by more than this pct, flag
PRICE_MISMATCH_PCT_THRESHOLD = 2.0  # 2%

# Numeric tolerances
PERCENTAGE_TOLERANCE = 0.1   # 0.1 percentage points
RATIO_TOLERANCE = 0.01       # 1% for P/E etc.

# Suppression
SUPPRESS_ON_AMBIGUOUS_VALUATION = True
SUPPRESS_ON_FAILED_FORMULA = True
CONSERVATIVE_INVESTMENT_VIEW_WHEN_FACT_PACK_THIN = True

# Summary strip: which price basis to use for display
# "consensus" = use consensus snapshot last_close for strip + upside from target/last_close
# "quote" = use quote snapshot price for strip + upside from target/quote
SUMMARY_STRIP_PRICE_BASIS = "consensus"
