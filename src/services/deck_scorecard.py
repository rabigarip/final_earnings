"""Per-ticker deck-readiness scorecard.

Answers the scaling question "which of the ~500 tickers can produce a
BKMB-grade deck, and what's missing for the rest?" WITHOUT generating
anything — it inspects data availability (registry, disclosed IR, cached
provider snapshots, peer set) and returns a 0-100 score, a tier, and an
ACTIONABLE list of what each ticker still needs.

Deck quality is bounded by grounded-data coverage: a ticker with no
disclosed financials gets generic LLM commentary no matter how good the
prompt. This module makes that coverage measurable so manual curation
effort goes where it moves the needle, and so the pipeline can flag a
thin deck instead of shipping it silently.

The component weights encode what actually separated the BKMB deck from a
bare one — most of all, the grounded FY metrics (loans/deposits/CAR/ROE
for a bank; the sector equivalent otherwise).

Run locally or on the deployment box (where the cached snapshots live):
    python -m scripts.score_universe                 # whole universe
    python -m scripts.score_universe --ticker BKMB.OM
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.services.ticker_registry import get_ticker_info, registry_peer_set


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


# ── data-availability probes (offline; file/registry only) ──────────

def _disclosed_path(ticker: str) -> Path:
    return _root() / "data" / "disclosed" / f"{ticker}.json"


def _disclosed_state(ticker: str) -> tuple[bool, bool, bool]:
    """(has_file, has_fy_highlights, has_quarterly)."""
    p = _disclosed_path(ticker)
    if not p.is_file():
        return False, False, False
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return False, False, False
    fy = isinstance(d.get("fy_highlights"), dict) and bool(d.get("fy_highlights"))
    q = bool(d.get("quarterly"))
    return True, fy, q


def _ms_cached(ticker: str, kinds: tuple[str, ...] = ()) -> bool:
    """True if any MarketScreener snapshot is cached for the ticker. The
    cache names files ms_<TICKER-with-_>_<kind>.html (BKMB.OM → BKMB_OM)."""
    d = _root() / "data" / "marketscreener"
    if not d.is_dir():
        return False
    stem = f"ms_{ticker.replace('.', '_')}_"
    files = [f.name for f in d.glob(f"{stem}*")]
    if not files:
        return False
    if not kinds:
        return True
    return any(any(f"_{k}." in n or n.endswith(f"_{k}.html") for k in kinds) for n in files)


def _has_investing_slug(ticker: str) -> bool:
    try:
        from src.providers.probe_investing import _SLUGS
        return ticker.upper() in _SLUGS
    except Exception:
        return False


# ── scoring ─────────────────────────────────────────────────────────

# (weight, label) — weights sum to 100. Grounded FY metrics carry the most
# because they are what made BKMB's commentary specific and verifiable.
_WEIGHTS = {
    "grounded_fy":       25,   # disclosed fy_highlights (sector-grounded actuals)
    "consensus":         15,   # rating + target (Investing slug or MS consensus)
    "ms_estimates":      15,   # MS annual estimates (the slide-2 table)
    "peers":             15,   # >=3 comparables for the peer table
    "sector_template":   10,   # classified into a real sector family
    "price_perf":        10,   # a live/snapshot price + performance source
    "quarterly_actuals": 10,   # disclosed or MS quarterly history
}


def score_ticker(ticker: str) -> dict[str, Any]:
    info = get_ticker_info(ticker)
    fam = (info.get("template_family") or "other").lower()
    providers = info.get("providers") or {}
    yf = providers.get("yfinance", "supported")
    peers = registry_peer_set(ticker)   # industry-aware; ignores stale pre-built set

    has_disc, has_fy, has_q_disc = _disclosed_state(ticker)
    has_slug = _has_investing_slug(ticker)
    ms_any = _ms_cached(ticker)
    ms_consensus = _ms_cached(ticker, ("consensus", "ratings"))
    ms_finances = _ms_cached(ticker, ("finances", "income_statement"))

    pts: dict[str, int] = {}
    missing: list[str] = []

    # Grounded FY metrics — the BKMB differentiator — but only count them at
    # FULL weight when they are CURRENT for today's date. Stale grounding
    # (a newer annual is due) earns half and is flagged, so a ticker that was
    # an A silently drops to B the moment its baseline goes out of date.
    if has_fy:
        from src.services.reporting_calendar import grounding_staleness
        _st = grounding_staleness(ticker)
        if _st.get("annual_current"):
            pts["grounded_fy"] = _WEIGHTS["grounded_fy"]
        else:
            pts["grounded_fy"] = _WEIGHTS["grounded_fy"] // 2
            missing.append(f"refresh FY metrics — {_st.get('status')}")
    else:
        pts["grounded_fy"] = 0
        missing.append("grounded FY metrics (add disclosed fy_highlights)")

    # Consensus rating + target.
    if has_slug or ms_consensus:
        pts["consensus"] = _WEIGHTS["consensus"]
    else:
        pts["consensus"] = 0
        missing.append("analyst consensus (Investing slug or MS consensus)")

    # MS annual estimates (slide-2 table).
    pts["ms_estimates"] = _WEIGHTS["ms_estimates"] if (ms_finances or ms_any) else 0
    if not (ms_finances or ms_any):
        missing.append("MS annual estimates")

    # Peer comparables.
    if len(peers) >= 3:
        pts["peers"] = _WEIGHTS["peers"]
    elif peers:
        pts["peers"] = _WEIGHTS["peers"] // 2
        missing.append(f"more peers (only {len(peers)})")
    else:
        pts["peers"] = 0
        missing.append("peer set (none in registry)")

    # Sector template.
    pts["sector_template"] = _WEIGHTS["sector_template"] if fam not in ("other", "") else 0
    if fam in ("other", ""):
        missing.append("sector classification (generic template)")

    # Price + performance source.
    if has_slug or yf == "supported":
        pts["price_perf"] = _WEIGHTS["price_perf"]
    elif yf == "partial":
        pts["price_perf"] = _WEIGHTS["price_perf"] // 2
        missing.append("full price coverage (yfinance partial)")
    else:
        pts["price_perf"] = 0
        missing.append("price/performance source (yfinance unsupported, no Investing slug)")

    # Quarterly actuals.
    if has_q_disc or ms_finances or ms_any:
        pts["quarterly_actuals"] = _WEIGHTS["quarterly_actuals"]
    else:
        pts["quarterly_actuals"] = 0
        missing.append("quarterly earnings history")

    score = sum(pts.values())
    # Tier: "Ready" requires grounded FY metrics AND a high score — that's
    # the BKMB bar. Lower tiers still generate, just less grounded.
    if score >= 80 and has_fy:
        tier = "A · Ready"
    elif score >= 60:
        tier = "B · Solid"
    elif score >= 40:
        tier = "C · Partial"
    else:
        tier = "D · Thin"

    return {
        "ticker": ticker,
        "company": info.get("company_name") or ticker,
        "sector_family": fam,
        "score": score,
        "tier": tier,
        "components": pts,
        "missing": missing,
    }


def score_universe(tickers: list[str] | None = None) -> dict[str, Any]:
    """Score every registry ticker (or a supplied subset). Returns the
    per-ticker rows plus a tier-count summary."""
    if tickers is None:
        from src.services.ticker_registry import _registry_index
        tickers = sorted(_registry_index().keys())
    rows = [score_ticker(t) for t in tickers]
    rows.sort(key=lambda r: r["score"], reverse=True)
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["tier"]] = summary.get(r["tier"], 0) + 1
    return {"rows": rows, "summary": summary, "n": len(rows)}
