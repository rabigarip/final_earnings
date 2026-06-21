"""Read-side adapter for data/tickers.json.

One process-lifetime cache (the file is ~300KB and static for the run).
Returns a typed dict per ticker; callers downstream import:

    from src.services.ticker_registry import get_ticker_info

The registry feeds:
  - build_report_payload.run: template_family + currency_unit_scale
  - render_jabal_*: peer_set defaults + is_bank flag derivation
  - render_provenance: BR/SIC routing for fundamentals attribution
  - LLM context: sector classification

When the registry doesn't carry a ticker (e.g. one-off analyst ticker
not in the 500-name universe), `get_ticker_info` returns a sensible
default record so downstream code never crash-loops on a missing key.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "tickers.json"
# Hand-curated overrides/additions that survive a build-script regeneration
# of tickers.json. Use it to fix a misclassification (e.g. a bank stuck on
# "other" because the source row had no company name) or to add a peer-only
# ticker that isn't in the auto-built 500 universe. Each entry is a partial
# record merged ONTO the default/built record (so you only specify the
# fields you're changing). Shape: {"TICKER": {"template_family": "bank", ...}}.
_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "data" / "registry_overrides.json"


def _default_record(ticker: str) -> dict[str, Any]:
    """The baseline record covering every key downstream code reads, so a
    missing ticker produces a usable bare deck rather than a KeyError."""
    return {
        "ticker": ticker, "company_name": ticker, "exchange": "",
        "exchange_country": "", "currency": "", "currency_unit_scale": 1,
        "reporting_currency": "", "sector": "Other", "industry": "Other",
        "template_family": "other", "market_cap_local": None,
        "market_cap_usd": None, "is_canonical": True, "company_group": "",
        "siblings": [], "is_depositary_receipt": False,
        "underlying_ticker": None, "dr_fundamentals_source": None,
        "peer_set": [],
        "providers": {"yfinance": "supported", "marketscreener": "supported",
                       "investing": "supported", "bloomberg_ticker": None},
        "ir_portal_url": None, "disclosure_feed": None,
        "fiscal_year_end_month": 12, "active": True, "notes": "",
    }


@lru_cache(maxsize=1)
def _registry_index() -> dict[str, dict]:
    """Load the registry once (ticker→record), then merge hand-curated
    overrides on top so fixes/additions survive a tickers.json rebuild."""
    index: dict[str, dict] = {}
    if _REGISTRY_PATH.is_file():
        try:
            recs = json.loads(_REGISTRY_PATH.read_text())
            index = {r["ticker"]: r for r in recs if "ticker" in r}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ticker registry parse failed: %s", exc)
    else:
        log.warning("Ticker registry missing at %s; downstream will use defaults",
                    _REGISTRY_PATH)

    if _OVERRIDES_PATH.is_file():
        try:
            overrides = json.loads(_OVERRIDES_PATH.read_text())
            for tkr, patch in (overrides or {}).items():
                if not isinstance(patch, dict):
                    continue
                base = index.get(tkr) or _default_record(tkr)
                merged = dict(base)
                merged.update(patch)       # override fields win
                merged["ticker"] = tkr
                index[tkr] = merged
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Registry overrides parse failed: %s", exc)
    return index


def get_ticker_info(ticker: str) -> dict[str, Any]:
    """Return the registry record for `ticker`, or a sensible default.

    Default record covers the keys downstream code reads — currency
    unit scale 1, template family 'other', empty peer set — so a
    missing ticker produces a usable but bare deck rather than a
    KeyError trace.
    """
    idx = _registry_index()
    if ticker in idx:
        return idx[ticker]
    return _default_record(ticker)


def is_bank(ticker: str) -> bool:
    """Convenience derivation — true when template_family == 'bank'.
    The pipeline historically uses `is_bank` for table-schema dispatch;
    keep that contract but compute from the registry."""
    return get_ticker_info(ticker).get("template_family") == "bank"


# Region groups for peer comparability — a Gulf bank's peers are GCC banks,
# not US banks. Peers are drawn from the same group.
_REGION_GROUPS = {
    "GULF":          {"SA", "QA", "KW", "OM", "AE", "BH"},
    "GREATER_CHINA": {"HK", "CN"},
    "INDIA":         {"IN"},
    "BRAZIL":        {"BR"},
    "SOUTH_AFRICA":  {"ZA"},
    "MEXICO":        {"MX"},
}


def _region_group(country: str | None) -> str:
    c = (country or "").upper()
    for g, members in _REGION_GROUPS.items():
        if c in members:
            return g
    return c or "?"


def registry_peer_set(ticker: str) -> list[str]:
    """Auto peer set: same INDUSTRY within the same region group, ranked by
    market-cap proximity (closest size = most comparable), topped up with
    same-template-family peers when the industry pool is thin.

    This fixes the cross-sector auto-pick (Alibaba's table once listed Intel
    and CATL because the old set ranked on template_family + market cap only,
    ignoring sub-industry). A curated company_master.peer_group overrides this
    upstream.
    """
    import math
    info = get_ticker_info(ticker)
    fam = info.get("template_family")
    ind = (info.get("industry") or "").strip().lower()
    mcap = info.get("market_cap_usd")
    grp = (info.get("company_group") or "").strip()
    region = _region_group(info.get("exchange_country"))
    tkr_u = ticker.upper()

    def _prox(other_mcap) -> float:
        # Higher = closer in size (negative log distance). Sentinel when missing.
        if not (isinstance(mcap, (int, float)) and mcap > 0
                and isinstance(other_mcap, (int, float)) and other_mcap > 0):
            return -99.0
        return -abs(math.log(mcap) - math.log(other_mcap))

    import re as _re

    def _ind_tokens(s: str) -> set[str]:
        return {w for w in _re.split(r"[^a-z]+", (s or "").lower()) if len(w) > 3}

    # Coarse industry clusters so a thin-industry name tops up with ADJACENT
    # industries (ZTE Hardware → Semiconductors) before distant ones (fintech
    # / media), even when they share the same broad template_family.
    _CLUSTERS = (
        ("tech_hw", ("hardware", "semiconductor", "electronic", "communication equip",
                     "technology hardware", "components")),
        ("tech_sw", ("software", "internet", "interactive media", "it services", "fintech")),
        ("financials", ("bank", "insurance", "capital markets", "diversified financ")),
        ("energy", ("oil", "gas", "energy", "refin", "petro")),
        ("materials", ("chemical", "metal", "mining", "steel", "fertil", "materials", "cement")),
        ("consumer", ("food", "beverage", "retail", "consumer", "apparel", "auto")),
        ("health", ("pharma", "health", "biotech", "medical")),
        ("realestate", ("real estate", "reit", "property")),
        ("telecom", ("telecom", "wireless", "communication services")),
        ("utilities", ("utilit", "power", "electric")),
    )

    def _cluster(s: str) -> str | None:
        il = (s or "").lower()
        for name, keys in _CLUSTERS:
            if any(k in il for k in keys):
                return name
        return None

    subj_tokens = _ind_tokens(ind)
    subj_cluster = _cluster(ind)

    region_industry: list[tuple[float, str]] = []   # same region + same industry
    global_industry: list[tuple[float, str]] = []   # any region + same industry
    region_family: list[tuple[int, float, str]] = []  # same region + same family
    for t, r in _registry_index().items():
        if t == tkr_u:
            continue
        if not r.get("is_canonical", True) or not r.get("active", True):
            continue
        if not isinstance(r.get("market_cap_usd"), (int, float)) or r["market_cap_usd"] <= 0:
            continue
        # Skip the subject's own dual-listings (same company on another venue).
        if grp and (r.get("company_group") or "").strip() == grp:
            continue
        same_region = _region_group(r.get("exchange_country")) == region
        r_ind = (r.get("industry") or "").strip().lower()
        prox = _prox(r.get("market_cap_usd"))
        if ind and r_ind == ind:
            (region_industry if same_region else global_industry).append((prox, t))
        elif same_region and r.get("template_family") == fam:
            # Rank family top-ups by (same cluster, word overlap) so a
            # hardware name pulls semiconductors/electronics before a
            # fintech-software name (ZTE no longer lists RoyalFlush).
            cl = _cluster(r_ind)
            score = (2 if (subj_cluster and cl == subj_cluster) else 0) \
                + len(subj_tokens & _ind_tokens(r_ind))
            region_family.append((score, prox, t))

    region_industry.sort(key=lambda c: -c[0])
    global_industry.sort(key=lambda c: -c[0])
    region_family.sort(key=lambda c: (-c[0], -c[1]))  # relatedness desc, then size

    chosen: list[str] = [t for _, t in region_industry]
    for _, t in global_industry:                       # then same industry, any region
        if len(chosen) >= 6:
            break
        chosen.append(t)
    for _ov, _px, t in region_family:                  # then closest-industry family
        if len(chosen) >= 5:
            break
        chosen.append(t)
    return chosen[:6]


def reset_cache() -> None:
    """Used by tests to force a re-read after fixture edits."""
    _registry_index.cache_clear()
