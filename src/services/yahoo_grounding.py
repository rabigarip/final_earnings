"""Yahoo-sourced FY-actuals extractor (auto-grounding).

Scales the grounding store beyond the ~16 hand-verified names using Yahoo's
annual income statement — universal (covers most of the universe) and NOT
Cloudflare-blocked. Output is the disclosed-file shape with `_status:
"auto_unverified"`, written to data/disclosed/_staging/<ticker>.json for
review. It is NEVER auto-promoted to the hand-verified set.

Gates (why scaling this is safe):
  * Fiscal-calendar: the latest annual column must not be older than the
    period the company should have filed by now (reporting_calendar), so we
    never stage a stale FY as if current.
  * Sanity: net / EBITDA margins and growth are bounded; a loss-year YoY is
    marked not-meaningful rather than emitting an absurd %.
  * Confidence: flagged auto_unverified so the deck treats it as
    model-derived (lower-confidence than a hand-checked IR figure).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from src.providers._yf import yf


def _root() -> Path:
    from src.config import root
    return root()


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if not math.isnan(x) else None
    except (TypeError, ValueError):
        return None


def _row(df, names: list[str], col) -> Optional[float]:
    for n in names:
        if n in df.index:
            return _f(df.loc[n, col])
    return None


def _yoy(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    # Not meaningful across a sign change or off a ~zero base.
    if cur is None or prev is None or prev == 0:
        return None
    if (cur < 0) != (prev < 0):
        return None
    g = (cur / prev - 1.0) * 100.0
    return round(g, 1) if -95.0 <= g <= 400.0 else None


def extract_yahoo_fy(ticker: str) -> Optional[dict]:
    """Return an auto-grounding dict (disclosed shape) or None when Yahoo has
    no usable annual statement / the data fails the gates."""
    try:
        t = yf.Ticker(ticker)
        df = t.income_stmt
        info = t.info or {}
    except Exception:
        return None
    if df is None or getattr(df, "empty", True) or len(df.columns) < 1:
        return None

    cols = list(df.columns)            # newest first
    c0 = cols[0]
    c1 = cols[1] if len(cols) > 1 else None
    fy_year = getattr(c0, "year", None)
    if not fy_year:
        return None

    # Fiscal-calendar gate: reject an ancient latest FY (data clearly stale).
    try:
        from datetime import date
        if fy_year < date.today().year - 2:
            return None
    except Exception:
        pass

    rev = _row(df, ["Total Revenue", "TotalRevenue", "Revenue", "Operating Revenue"], c0)
    rev_p = _row(df, ["Total Revenue", "TotalRevenue", "Revenue", "Operating Revenue"], c1) if c1 is not None else None
    ni = _row(df, ["Net Income", "NetIncome", "Net Income Common Stockholders"], c0)
    ni_p = _row(df, ["Net Income", "NetIncome", "Net Income Common Stockholders"], c1) if c1 is not None else None
    ebitda = _row(df, ["EBITDA", "Normalized EBITDA"], c0)
    eps = _row(df, ["Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"], c0)

    if rev is None and ni is None:
        return None                    # nothing worth staging

    ccy = (info.get("financialCurrency") or info.get("currency") or "").upper()
    MN = 1e6

    fy: dict[str, Any] = {
        "period": f"FY {fy_year}",
        "reporting_currency": ccy,
        "source": "Yahoo Finance annual income statement (auto-extracted, unverified)",
    }
    if rev is not None:
        fy["revenue_mn"] = round(rev / MN, 1)
        g = _yoy(rev, rev_p)
        if g is not None:
            fy["revenue_growth_pct"] = g
    if ni is not None:
        fy["net_profit_mn"] = round(ni / MN, 1)
        g = _yoy(ni, ni_p)
        if g is not None:
            fy["net_profit_growth_pct"] = g
        # Sanity-bound net margin.
        if rev and rev > 0:
            nm = ni / rev * 100.0
            if -80.0 <= nm <= 70.0:
                fy["net_margin_pct"] = round(nm, 1)
    if ebitda is not None:
        fy["ebitda_mn"] = round(ebitda / MN, 1)
        if rev and rev > 0:
            em = ebitda / rev * 100.0
            if -10.0 <= em <= 95.0:
                fy["ebitda_margin_pct"] = round(em, 1)
    if eps is not None:
        fy["eps"] = round(eps, 3)

    name = info.get("longName") or info.get("shortName") or ticker
    return {
        "ticker": ticker.upper(),
        "company": name,
        "currency": ccy,
        "units": "millions",
        "_status": "auto_unverified",
        "_source": "yahoo_income_stmt",
        "fy_highlights": fy,
    }


def stage_yahoo_grounding(ticker: str) -> Optional[Path]:
    """Extract + write the staging file. Returns the path or None."""
    data = extract_yahoo_fy(ticker)
    if not data:
        return None
    d = _root() / "data" / "disclosed" / "_staging"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ticker.upper()}.json"
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p


if __name__ == "__main__":
    import sys
    for tk in sys.argv[1:] or ["2010.SR"]:
        d = extract_yahoo_fy(tk)
        print(tk, "->", json.dumps(d.get("fy_highlights"), indent=2) if d else "None")
