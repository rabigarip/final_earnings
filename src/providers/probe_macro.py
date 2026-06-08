"""
Macro overlay provider (Stage 1).

Fetches country-level macro data from the World Bank Open Data API.
Probe-harness shape: one cell per ticker × field, but the underlying
data is keyed by country code derived from `company_master.country`.

Open data endpoints used:
  - World Bank: api.worldbank.org/v2/country/{iso3}/indicator/{code}
    No key, fully open, JSON.
  - IMF WEO (for forecasts): we surface them via the World Bank
    metadata where overlapped; the dedicated IMF SDMX API has stricter
    schemas — out of scope for Stage 1.

Indicators we surface:
  NY.GDP.MKTP.KD.ZG   Real GDP growth (annual %)
  FP.CPI.TOTL.ZG      Inflation, consumer prices (annual %)
  SP.POP.TOTL         Population
  PA.NUS.FCRF         Exchange rate (LCU per USD, period average)

The macro provider responds only to `company_profile` (returns the
country macro snapshot inline with the profile). All other fields
return `not_implemented` so the matrix correctly shows macro as a
country-only data source.
"""

from __future__ import annotations

import json
from typing import Optional

import requests

from src.services.probe_harness import Provider, persist_raw
from src.storage.db import load_company


# Map ISO-2 country codes (what company_master uses) → ISO-3 (what
# World Bank uses). Only the codes our universe touches; expand as
# new countries appear.
ISO2_TO_ISO3 = {
    "SA": "SAU", "AE": "ARE", "OM": "OMN", "QA": "QAT", "KW": "KWT", "BH": "BHR",
    "IN": "IND", "CN": "CHN", "HK": "HKG",
    "PS": "PHL",  # Philippines (in our master)
    "VN": "VNM",
    "TH": "THA",
    "ID": "IDN",
    "EG": "EGY",
    "TR": "TUR",
    "PK": "PAK",
    "BD": "BGD",
    "US": "USA",
}


INDICATORS = {
    "gdp_growth_pct":   "NY.GDP.MKTP.KD.ZG",
    "inflation_pct":    "FP.CPI.TOTL.ZG",
    "population":       "SP.POP.TOTL",
    "fx_lcu_per_usd":   "PA.NUS.FCRF",
}


# IMF WEO (World Economic Outlook) public datamapper — adds FORECAST
# values for GDP and inflation that World Bank doesn't carry. The
# datamapper /api/v1 endpoint is anonymous JSON with one indicator per
# call. We surface forecast values for the next two calendar years so
# the slide can show "2026E / 2027E" alongside the WB latest actual.
_IMF_BASE = "https://www.imf.org/external/datamapper/api/v1"
_IMF_INDICATORS = {
    "gdp_growth_pct_fcst":  "NGDP_RPCH",   # Real GDP growth, %
    "inflation_pct_fcst":   "PCPIPCH",     # Inflation, CPI, %
    "current_account_pct":  "BCA_NGDPD",   # Current account, % of GDP
}


def _imf_fetch(iso3: str, indicator: str) -> Optional[dict]:
    """One IMF datamapper call. Returns latest year's value + the
    next-year forecast where available. The IMF publishes WEO twice a
    year (Apr / Oct) and includes future-year forecasts alongside
    historical actuals."""
    url = f"{_IMF_BASE}/{indicator}/{iso3}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    values = data.get("values", {}).get(indicator, {}).get(iso3, {})
    if not values:
        return None
    pairs = []
    for yr, val in values.items():
        try:
            pairs.append((int(yr), float(val)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return None
    pairs.sort()
    # IMF WEO publishes both historical actuals and out-year forecasts in
    # a single series (e.g. Oman 2022-2031). Picking pairs[-1] gave us
    # the 2031 long-run reversion estimate — wrong frame for a Q2 2026
    # earnings preview. Anchor on the *current calendar year* instead so
    # the macro context reflects today's expected conditions; `next_value`
    # then becomes the one-year-out forecast.
    from datetime import datetime as _dt
    cur_yr = _dt.now().year
    by_year = {yr: val for yr, val in pairs}
    # Pick the current year if IMF carries it; otherwise the closest
    # available year (forecasts always cover the current year, but
    # historical-only series — e.g. some HK indicators — may stop short).
    if cur_yr in by_year:
        anchor_year, anchor_val = cur_yr, by_year[cur_yr]
    else:
        anchor_year, anchor_val = min(
            pairs, key=lambda p: (abs(p[0] - cur_yr), -p[0])
        )
    next_year = next_val = None
    if (anchor_year + 1) in by_year:
        next_year, next_val = anchor_year + 1, by_year[anchor_year + 1]
    return {
        "value":      round(anchor_val, 2),
        "year":       anchor_year,
        "next_value": round(next_val, 2) if next_val is not None else None,
        "next_year":  next_year,
        "indicator":  indicator,
    }


def _wb_fetch(iso3: str, indicator: str) -> Optional[dict]:
    """One World Bank Open Data call. Returns the most recent
    non-null observation as {value, year, indicator} or None.

    Uses `requests` (not urllib) because Python's stdlib SSL on macOS
    sometimes lacks the system CA bundle. `requests` ships with certifi
    and is already a project dep."""
    url = (
        f"https://api.worldbank.org/v2/country/{iso3}/indicator/{indicator}"
        f"?format=json&per_page=8&mrnev=1"
    )
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, list) or len(data) < 2:
        return None
    rows = data[1] or []
    for r in rows:
        v = r.get("value")
        if v is not None:
            return {
                "value": v,
                "year":  r.get("date"),
                "indicator": indicator,
            }
    return None


class MacroProvider(Provider):
    name = "macro"

    def __init__(self):
        # cache: (iso3, indicator) -> last-observation dict
        self._cache: dict[tuple[str, str], Optional[dict]] = {}

    def _country_iso3(self, ticker: str) -> Optional[str]:
        row = load_company(ticker) or {}
        c2 = (row.get("country") or "").strip().upper()
        # company_master uses country names in some rows (e.g. "Saudi
        # Arabia") and ISO-2 codes in others. Normalize.
        # Fallback: try the country full name to ISO-2 mapping.
        if c2 in ISO2_TO_ISO3:
            return ISO2_TO_ISO3[c2]
        name_to_iso2 = {
            "SAUDI ARABIA": "SA", "UAE": "AE", "OMAN": "OM", "QATAR": "QA",
            "KUWAIT": "KW", "BAHRAIN": "BH", "INDIA": "IN", "CHINA": "CN",
            "HONG KONG": "HK", "VIETNAM": "VN", "THAILAND": "TH",
            "INDONESIA": "ID", "EGYPT": "EG", "TURKEY": "TR",
            "PAKISTAN": "PK", "BANGLADESH": "BD", "PHILIPPINES": "PS",
            "UNITED STATES": "US",
        }
        iso2 = name_to_iso2.get(c2)
        return ISO2_TO_ISO3.get(iso2) if iso2 else None

    def _macro_for(self, iso3: str) -> dict:
        out = {}
        # World Bank: actuals
        for key, code in INDICATORS.items():
            ck = ("wb", iso3, code)
            if ck not in self._cache:
                self._cache[ck] = _wb_fetch(iso3, code)
            obs = self._cache[ck]
            if obs:
                out[key] = obs["value"]
                out[f"{key}_year"] = obs["year"]
        # IMF WEO: forecasts (+ current-account ratio)
        for key, code in _IMF_INDICATORS.items():
            ck = ("imf", iso3, code)
            if ck not in self._cache:
                self._cache[ck] = _imf_fetch(iso3, code)
            obs = self._cache[ck]
            if obs:
                out[key]              = obs["value"]
                out[f"{key}_year"]    = obs["year"]
                out[f"{key}_next"]    = obs["next_value"]
                out[f"{key}_next_year"] = obs["next_year"]
        return out

    # ── Only company_profile is implemented for the macro overlay. ──

    def _fetch_company_profile(self, ticker: str):
        iso3 = self._country_iso3(ticker)
        if not iso3:
            raise ValueError(f"no ISO-3 mapping for ticker {ticker}")
        snap = self._macro_for(iso3)
        # World Bank doesn't carry every indicator for every country
        # (HK in particular lacks GDP growth and inflation in some
        # series). Accept partial data — flag missing indicators as
        # None in the profile rather than failing the whole cell.
        if not snap:
            raise ValueError(f"World Bank returned no data for {iso3}")
        raw_id = persist_raw(self.name, ticker, "company_profile", {
            "iso3": iso3, **snap,
        })
        # We pack the macro snapshot into the profile shape so the
        # reconciler can show "country macro context" alongside the
        # standard profile fields.
        profile = {
            "country_iso3":      iso3,
            "gdp_growth_pct":    snap.get("gdp_growth_pct"),
            "inflation_pct":     snap.get("inflation_pct"),
            "population":        snap.get("population"),
            "fx_lcu_per_usd":    snap.get("fx_lcu_per_usd"),
            "macro_year":        snap.get("gdp_growth_pct_year"),
            # IMF WEO forecasts (preferred over WB historical actuals in
            # the LLM prompt — see llm_summary.build_context.macro block).
            "gdp_growth_fcst_pct":       snap.get("gdp_growth_pct_fcst"),
            "gdp_growth_fcst_year":      snap.get("gdp_growth_pct_fcst_year"),
            "gdp_growth_fcst_next_pct":  snap.get("gdp_growth_pct_fcst_next"),
            "gdp_growth_fcst_next_year": snap.get("gdp_growth_pct_fcst_next_year"),
            "inflation_fcst_pct":        snap.get("inflation_pct_fcst"),
            "inflation_fcst_year":       snap.get("inflation_pct_fcst_year"),
            "inflation_fcst_next_pct":   snap.get("inflation_pct_fcst_next"),
            "inflation_fcst_next_year":  snap.get("inflation_pct_fcst_next_year"),
            "current_account_pct":       snap.get("current_account_pct"),
        }
        return profile, "macro", snap.get("gdp_growth_pct_year") or "", raw_id
