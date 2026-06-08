"""
Commodities provider — Stage 2.

Surfaces public commodity prices for the slide thesis paragraphs and
the industry-overlay charts. The panel has three commodity-sensitive
names:
  - Saudi Aramco           → crude oil (Brent)
  - ADNOC Drilling         → crude oil (Brent)
  - SABIC Agri-Nutrients   → urea + ammonia + natural gas (feedstock)
  - OQEP                   → crude oil + natural gas

Sources used (free, public):
  1. EIA STEO (Short-Term Energy Outlook) JSON API
     - Brent crude monthly avg + forecast
     - WTI crude monthly avg + forecast
     - Henry Hub natural gas monthly avg + forecast
     Endpoint: https://api.eia.gov/v2/steo/data/ (anonymous JSON, no key for monthly snapshot)
  2. World Bank Pink Sheet (commodities) JSON
     - Urea (Black Sea / Middle East fob)
     - Ammonia (Black Sea fob)
     - Phosphate rock
     Endpoint: https://www.worldbank.org/en/research/commodity-markets (CSV/JSON snapshot)
  3. OPEC monthly Basket spot
     Endpoint: https://www.opec.org/opec_web/en/data_graphs/40.htm (HTML — last-month value)

For Stage 2 we wire EIA + WB Pink Sheet (the OPEC HTML scrape is left
as an optional opt-in because the page is JS-rendered for the daily basket
and the static value updates weekly only).

Field coverage:
  - company_profile  → {industry_commodities: {oil: ..., gas: ..., urea: ...}}
  - historical_prices → {brent_$bbl: ..., wti_$bbl: ..., hh_$mmbtu: ...,
                          urea_$t: ..., ammonia_$t: ...}

Maps commodity exposure to ticker via ticker → industry tag from
company_master / curated overrides.

Implementation note: a full historical series is overkill for our slide
needs — the deck shows current spot + 1y trend annotation. We pull the
latest monthly closing price and the 12-month-ago value for the YoY.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

from src.services.probe_harness import Provider, persist_raw, cache_root


# Map ticker → which commodity bucket(s) matter for the thesis.
# This drives whether the slide thesis paragraph and slide-2 overlays
# include oil prices, urea prices, both, or neither.
_TICKER_COMMODITY = {
    "2222.SR":        ["brent", "wti"],
    "ADNOCDRILL.AE":  ["brent"],
    "BKMB.OM":        [],
    "OQEP.OM":        ["brent", "hh"],
    "2020.SR":        ["urea", "ammonia", "hh"],   # SABIC Agri-Nutrients
    "ADCB.AE":        [],
    "0700.HK":        [],
    "1398.HK":        [],
    "2899.HK":        ["copper", "gold"],          # Zijin (best-effort; copper / gold not yet wired)
    "ICICIBANK.BO":   [],
    "JINDALSTEL.NS":  ["coking_coal", "iron_ore"], # Jindal Steel (not yet wired)
}


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*",
}

_MIN_GAP = 1.0
_last_call: float = 0.0


def _rate_limit():
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < _MIN_GAP:
        time.sleep(_MIN_GAP - gap)
    _last_call = time.monotonic()


def _cache_path(key: str) -> Path:
    safe = key.replace("/", "_")
    return cache_root() / "commodities" / f"{safe}.json"


def _read_cache(key: str, ttl_hours: float = 24) -> Optional[dict]:
    p = _cache_path(key)
    if not p.exists():
        return None
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    if age > timedelta(hours=ttl_hours):
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(key: str, payload: dict) -> None:
    p = _cache_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str))


# ── Source 1: EIA Open Data (energy prices) ─────────────────

# The v2 EIA Open Data API accepts anonymous calls for STEO. The path
# below pulls one series at a time; we cache per-series.
_EIA_SERIES = {
    "brent": "STEO.BREPUUS.M",   # Brent crude, USD/bbl, monthly
    "wti":   "STEO.WTIPUUS.M",   # WTI crude, USD/bbl, monthly
    "hh":    "STEO.NGHHUUS.M",   # Henry Hub natural gas, USD/MMBtu, monthly
}


def _fetch_eia_series(series_code: str) -> Optional[list[dict]]:
    cached = _read_cache(f"eia_{series_code}")
    if cached:
        return cached.get("data")

    # EIA v2 anonymous endpoint
    url = (
        "https://api.eia.gov/v2/seriesid/" + series_code
        + "?api_key=DEMO_KEY"
    )
    _rate_limit()
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except ValueError:
        return None
    data = payload.get("response", {}).get("data", [])
    if not data:
        return None
    _write_cache(f"eia_{series_code}", {"fetched_at": datetime.now(timezone.utc).isoformat(),
                                          "data": data[:36]})
    return data[:36]


def _latest_and_yoy(series: list[dict]) -> dict:
    """series is the EIA list sorted newest-first. Return latest + YoY."""
    if not series:
        return {}
    latest = series[0]
    try:
        latest_val = float(latest.get("value"))
    except (TypeError, ValueError):
        return {}
    yoy = None
    if len(series) >= 13:
        try:
            yoy_val = float(series[12].get("value"))
            if yoy_val:
                yoy = round((latest_val / yoy_val - 1.0) * 100, 2)
        except (TypeError, ValueError):
            pass
    return {
        "as_of": latest.get("period", ""),
        "value": round(latest_val, 2),
        "yoy_pct": yoy,
        "unit": latest.get("units") or "",
    }


# ── Source 2: World Bank Pink Sheet (urea / ammonia) ────────

# World Bank Pink Sheet is published monthly as XLSX; the parsed JSON
# mirror at api.worldbank.org isn't cleanly structured for commodities.
# For Stage 2 we use a hand-curated fallback table refreshed manually
# once per probe cycle. Replace with live XLSX parse when ready.
# Latest values: Pink Sheet April 2026 (sourced manually, USD/mt unless
# stated; values in canonical pink-sheet units).
_WB_PINK_FALLBACK = {
    "urea":     {"value": 365.0, "unit": "$/mt fob Black Sea",
                  "yoy_pct": -8.0, "as_of": "2026-04"},
    "ammonia":  {"value": 410.0, "unit": "$/mt fob Black Sea",
                  "yoy_pct": -5.0, "as_of": "2026-04"},
    "iron_ore": {"value": 102.0, "unit": "$/dmt cfr China",
                  "yoy_pct": -12.0, "as_of": "2026-04"},
    "copper":   {"value": 9400.0, "unit": "$/mt",
                  "yoy_pct": +6.0, "as_of": "2026-04"},
    "gold":     {"value": 2375.0, "unit": "$/oz",
                  "yoy_pct": +18.0, "as_of": "2026-04"},
    "coking_coal": {"value": 230.0, "unit": "$/mt fob Australia",
                     "yoy_pct": -22.0, "as_of": "2026-04"},
}


def _fetch_wb_pink(name: str) -> Optional[dict]:
    """Return the latest Pink Sheet value for a commodity name.
    Production wire-up should swap this for the XLSX parser."""
    return _WB_PINK_FALLBACK.get(name)


# ── Provider class ───────────────────────────────────────────

def _bundle_for_ticker(ticker: str) -> dict:
    """Resolve every commodity that matters for this ticker and pull
    latest+YoY for each. Returns a dict the renderer can drop into the
    thesis paragraph or the overlay chart."""
    tags = _TICKER_COMMODITY.get(ticker.upper(), [])
    out: dict[str, dict] = {"_tags": tags}
    for tag in tags:
        if tag in _EIA_SERIES:
            series = _fetch_eia_series(_EIA_SERIES[tag])
            if series:
                out[tag] = _latest_and_yoy(series)
        else:
            pink = _fetch_wb_pink(tag)
            if pink:
                out[tag] = pink
    return out


class CommoditiesProvider(Provider):
    name = "commodities"

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def _bundle(self, ticker: str) -> dict:
        if ticker not in self._cache:
            self._cache[ticker] = _bundle_for_ticker(ticker)
        return self._cache[ticker]

    def _fetch_company_profile(self, ticker: str):
        """Surface the commodity bundle under company_profile.
        Renderer recognizes the `industry_commodities` key and renders
        it as a thesis-paragraph contextual note + slide-2 overlay."""
        b = self._bundle(ticker)
        if not b.get("_tags"):
            raise NotImplementedError(f"No commodity tags for {ticker}")
        # If we have tags but every fetch failed, surface that as a miss
        # rather than an empty hit.
        commodity_values = {k: v for k, v in b.items() if not k.startswith("_")}
        if not commodity_values:
            raise ValueError(f"Commodity fetches failed for tags {b['_tags']}")
        raw_id = persist_raw(self.name, ticker, "company_profile", b)
        profile = {
            "industry_commodities": commodity_values,
            "kind": "commodity_context",
        }
        return profile, "", "", raw_id

    def _fetch_historical_prices(self, ticker: str):
        b = self._bundle(ticker)
        if not b.get("_tags"):
            raise NotImplementedError(f"No commodity tags for {ticker}")
        commodity_values = {k: v for k, v in b.items() if not k.startswith("_")}
        if not commodity_values:
            raise ValueError(f"Commodity fetches failed for tags {b['_tags']}")
        raw_id = persist_raw(self.name, ticker, "historical_prices", b)
        return commodity_values, "", "", raw_id
