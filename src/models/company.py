"""CompanyMaster — pydantic model for the curated company mapping."""

from __future__ import annotations
from pydantic import BaseModel


class CompanyMaster(BaseModel):
    ticker:                        str  = ""   # yfinance ticker (input key)
    company_name:                  str  = ""   # Short: "SABIC"
    company_name_long:             str  = ""   # Full name
    exchange:                      str  = ""   # "Tadawul"
    country:                       str  = ""   # ISO alpha-2: "SA"
    currency:                      str  = ""   # "SAR"
    isin:                          str  = ""   # Exact security ID (primary for MS lookup)
    marketscreener_id:             str  = ""   # URL slug (derived from cached URL)
    marketscreener_company_url:    str  = ""   # Cached company page URL (ISIN-based resolution)
    marketscreener_symbol:         str  = ""   # MS symbol if different from ticker
    marketscreener_status:         str  = ""   # ok | stale | ""
    last_verified:                 str  = ""   # ISO timestamp of last successful verification
    zawya_slug:                    str  = ""
    sector:                        str  = ""
    industry:                      str  = ""
    peer_group:                    list[str] = []  # Optional peer tickers for sector-relative comps
    is_bank:                       bool = False
    notes:                         str  = ""
