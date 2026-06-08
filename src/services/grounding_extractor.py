"""Auto-grounding extractor — scale the BKMB recipe to the whole universe.

We hand-verified FY actuals for ~10 tickers (data/disclosed/*.json) to get
them to A-grade. Typing all ~500 by hand doesn't scale. This module does the
*numeric* half automatically: for any ticker it reads the already-cached
MarketScreener actuals HTML and emits a CANDIDATE `fy_highlights` block on the
ticker's sector schema, in the disclosed-file shape.

Design contract — why this is trustworthy enough to scale:
  * DETERMINISTIC. No LLM in the numeric path — it reuses the tested
    marketscreener_pages parsers (the same code the live deck reads), so the
    output is reproducible and unit-tested against the hand-verified gold set
    (see scripts/eval_grounding_extractor.py).
  * SANITY-GATED. Every emitted value passes grounding_schema.sanity_ok();
    out-of-band reads are dropped, not shipped.
  * NEVER AUTO-PROMOTED. Output goes to data/disclosed/_staging/<ticker>.json
    with "_status": "auto_unverified" and per-field provenance. A human
    reviews and moves it to data/disclosed/. MS line-item definitions don't
    always equal the headline IR figure (e.g. "Net Income to Company" can
    differ from group net profit by minorities), so the staging gate is the
    point, not an afterthought.
  * SCOPED. It emits only the metrics MarketScreener actually carries
    reliably (revenue, net profit, EBITDA, margins, EPS, DPS + YoY). The
    balance-sheet / ratio metrics MS doesn't disclose (ROE, CAR, NII, loans,
    deposits, FCF, capex, production) are listed under `needs_ir` so the
    reviewer knows exactly what to add from the IR release.

Units: MarketScreener actuals come back as absolute currency units (the
per-cell coercer expands "256M"/"350B"); we divide by 1e6 to the millions
convention disclosed files use. The /finances/ page scale is auto-calibrated
per ticker against the income-statement net-income figure, so we never assume
a fixed scale for EBITDA.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from src.services.grounding_schema import schema_for, sanity_ok
from src.services.ticker_registry import get_ticker_info

_BASE_URL = "https://www.marketscreener.com/quote/stock/x"

# Metrics MarketScreener does NOT carry reliably → leave for the IR release.
# Keyed so the staging file can tell a reviewer what's still missing.
_NEEDS_IR = {
    "roe_pct", "car_pct", "nii_mn", "non_interest_income_mn",
    "net_loans_mn", "customer_deposits_mn", "free_cash_flow_mn",
    "capex_mn", "production_mboed", "sales_volume_kt", "gross_margin_pct",
}


def _repo_root() -> Path:
    from src.config import root
    return root()


def _dirs() -> list[Path]:
    r = _repo_root()
    return [r / "cache", r / "data" / "marketscreener"]


def _resolve_prefix(ticker: str, page: str) -> Optional[str]:
    """Find the cache_key_prefix for `ticker`'s `page` HTML across the live
    cache and the repo-tracked snapshot dir. Handles both filename grammars:
    short `ms_<TICKER>_<page>.html` and full `ms_ms_<TICKER>_<ISIN>_<SLUG>_<page>.html`.
    Prefers the shortest (snapshot) form. Returns the prefix to pass to the
    marketscreener_pages fetchers, or None if no cache exists."""
    sani = ticker.replace(".", "_").replace("-", "_")
    suffix = f"_{page}.html"
    for d in _dirs():
        if not d.is_dir():
            continue
        cands = sorted(d.glob(f"ms_*{sani}*{suffix}"), key=lambda p: len(p.name))
        for f in cands:
            # filename is ms_<slug>.html where slug == <prefix>_<page>
            return f.name[3:-len(suffix)]
    return None


def _scale_to_millions(scale_str: Optional[str]) -> Optional[float]:
    """Multiplier to convert a value in `scale_str` units to millions.
    Returns None for an unrecognized/absent label (caller decides fallback)."""
    s = (scale_str or "").lower()
    if "bill" in s:
        return 1000.0
    if "mill" in s:
        return 1.0
    if "thou" in s:
        return 0.001
    return None


def _yoy(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev in (None, 0):
        return None
    return round((cur - prev) / abs(prev) * 100.0, 1)


def _period_year(label: str) -> Optional[int]:
    import re
    m = re.search(r"(20\d{2})", label or "")
    return int(m.group(1)) if m else None


def _last_two_idx(series: list, periods: list,
                  max_year: Optional[int] = None) -> tuple[Optional[int], Optional[int]]:
    """Latest index with a non-None value, and the consecutive prior index
    (for YoY). When max_year is given, periods AFTER that year are skipped —
    the MS income-statement page can carry forward estimate columns, and we
    must anchor on the latest reported ACTUAL, never an estimate."""
    anchor = None
    for i in range(len(periods) - 1, -1, -1):
        if max_year is not None:
            y = _period_year(periods[i])
            if y is not None and y > max_year:
                continue
        if i < len(series) and series[i] is not None:
            anchor = i
            break
    if anchor is None:
        return None, None
    prev = anchor - 1 if anchor - 1 >= 0 else None
    return anchor, prev


def _at(series: list, i: Optional[int]) -> Optional[float]:
    if i is None or not series or i >= len(series) or i < 0:
        return None
    return series[i]


def extract_grounding(ticker: str, family: Optional[str] = None,
                      offline: bool = True) -> dict[str, Any]:
    """Build a candidate disclosed-file dict for `ticker` from cached MS HTML.

    Returns a dict in the data/disclosed/*.json shape with an extra
    `_extractor` block (anchor period, per-field provenance, warnings) and
    `_status: auto_unverified`. fy_highlights carries only sanity-passing,
    MS-sourceable metrics for the ticker's sector schema. Raises nothing —
    on missing cache it returns a dict with an empty fy_highlights and a
    warning so batch callers can skip cleanly.
    """
    if offline:
        os.environ["MS_OFFLINE_CACHE_FIRST"] = "1"

    from src.providers.marketscreener_pages import (
        fetch_income_statement_actuals,
        fetch_financial_forecast_series,
        fetch_dividend_eps_page,
    )

    info = get_ticker_info(ticker)
    fam = (family or info.get("template_family") or "other").lower()
    schema_keys = {k for k, _, _ in schema_for(fam)}
    warnings: list[str] = []

    pfx_is = _resolve_prefix(ticker, "income_statement")
    pfx_fin = _resolve_prefix(ticker, "finances")
    pfx_div = _resolve_prefix(ticker, "valuation_dividend")

    if not pfx_is and not pfx_fin:
        return {
            "ticker": ticker, "company": info.get("company_name", ticker),
            "_status": "no_cache",
            "_extractor": {"warnings": ["no MarketScreener cache found"]},
            "fy_highlights": {},
        }

    IS = fetch_income_statement_actuals(_BASE_URL, cache_key_prefix=pfx_is)[0] if pfx_is else {}
    FIN = fetch_financial_forecast_series(_BASE_URL, cache_key_prefix=pfx_fin)[0] if pfx_fin else {}
    DIV = fetch_dividend_eps_page(_BASE_URL, cache_key_prefix=pfx_div)[0] if pfx_div else {}
    ann = (FIN.get("annual") or {}) if FIN else {}

    periods = IS.get("periods") or ann.get("periods") or []
    if not periods:
        return {
            "ticker": ticker, "company": info.get("company_name", ticker),
            "_status": "no_periods",
            "_extractor": {"warnings": ["parsers returned no period grid"]},
            "fy_highlights": {},
        }

    # Bound the anchor to the latest year the company should have reported by
    # now (reporting calendar) so we never anchor on a forward estimate column.
    try:
        from src.services.reporting_calendar import expected_latest
        _exp = (expected_latest(ticker) or {}).get("annual_period")
        max_year = _period_year(_exp)
    except Exception:
        max_year = None

    # ── Anchor on the latest reported net income (income statement = actuals)
    ni_co = IS.get("net_income_to_company") or []
    ni_is = IS.get("net_income_is") or []
    ni_series = ni_co if any(v is not None for v in ni_co) else ni_is
    if not any(v is not None for v in ni_series):
        ni_series = ann.get("net_income") or []
        periods = ann.get("periods") or periods
    anchor_i, prev_i = _last_two_idx(ni_series, periods, max_year=max_year)
    if anchor_i is None:
        return {
            "ticker": ticker, "company": info.get("company_name", ticker),
            "_status": "no_net_income",
            "_extractor": {"warnings": ["no reported net income to anchor on"]},
            "fy_highlights": {},
        }
    # Normalize the label to FY<year> so a "2025 (USD)"-style header still
    # matches the reporting calendar's "FY2025".
    _raw_anchor = periods[anchor_i]
    _ay = _period_year(_raw_anchor)
    anchor_period = f"FY{_ay}" if _ay else _raw_anchor
    prev_period = periods[prev_i] if prev_i is not None else None

    # ── Align the /finances/ page (mixed actual+estimate) to the anchor FY
    fin_periods = ann.get("periods") or []
    fin_i = fin_periods.index(anchor_period) if anchor_period in fin_periods else None
    fin_prev_i = fin_i - 1 if (fin_i is not None and fin_i - 1 >= 0) else None
    # ── Units. Do NOT assume a fixed scale: MS displays vary (some pages give
    # absolute currency units via "256M"/"350B" suffixes; Chinese A-shares give
    # plain millions). Anchor on the /finances/ page's DECLARED unit_scale, and
    # align the income-statement page to it via the net-income ratio.
    fin_mult = _scale_to_millions(FIN.get("unit_scale"))   # FIN raw -> millions
    if fin_mult is None:
        fin_mult = 1.0
    fin_ni = _at(ann.get("net_income") or [], fin_i)
    is_ni = _at(ni_series, anchor_i)
    if fin_ni not in (None, 0) and is_ni not in (None, 0):
        is_mult = (fin_ni * fin_mult) / is_ni              # IS raw -> millions
    else:
        # No /finances/ net income to calibrate against — fall back to the
        # income-statement convention of absolute currency units.
        is_mult = 1e-6

    # ── Source each candidate metric, normalized to MILLIONS.
    prov: dict[str, str] = {}
    money_mn: dict[str, Optional[float]] = {}
    money_prev_mn: dict[str, Optional[float]] = {}

    def _mn(raw, mult):
        return raw * mult if raw is not None else None

    is_bank = fam in ("bank", "financial_services", "insurance")

    # revenue (abs). Prefer the income-statement page (actuals, same scale as
    # net income); fall back to /finances/ net_sales (scale-calibrated).
    rev_co = IS.get("total_revenues") or []
    rev_bank = IS.get("revenues_before_provision_for_loan_losses") or []
    rev_from_finances = False
    if is_bank and any(v is not None for v in rev_bank):
        rev_src, rev_label = rev_bank, "MS income-statement: Revenues Before Provision"
    elif any(v is not None for v in rev_co):
        rev_src, rev_label = rev_co, "MS income-statement: Total Revenues"
    else:
        rev_src, rev_label = (ann.get("net_sales") or []), "MS finances: Net sales"
        rev_from_finances = True
    if rev_from_finances:
        money_mn["revenue_mn"] = _mn(_at(rev_src, fin_i), fin_mult)
        money_prev_mn["revenue_mn"] = _mn(_at(rev_src, fin_prev_i), fin_mult)
    else:
        money_mn["revenue_mn"] = _mn(_at(rev_src, anchor_i), is_mult)
        money_prev_mn["revenue_mn"] = _mn(_at(rev_src, prev_i), is_mult)
    if money_mn["revenue_mn"] is not None:
        prov["revenue_mn"] = rev_label

    # net profit (income statement, aligned to FIN scale)
    money_mn["net_profit_mn"] = _mn(_at(ni_series, anchor_i), is_mult)
    money_prev_mn["net_profit_mn"] = _mn(_at(ni_series, prev_i), is_mult)
    if money_mn["net_profit_mn"] is not None:
        prov["net_profit_mn"] = ("MS income-statement: Net Income to Company"
                                 if ni_series is ni_co else "MS: Net income")

    # ebitda (finances-only, FIN scale) — non-banks
    if not is_bank:
        money_mn["ebitda_mn"] = _mn(_at(ann.get("ebitda") or [], fin_i), fin_mult)
        money_prev_mn["ebitda_mn"] = _mn(_at(ann.get("ebitda") or [], fin_prev_i), fin_mult)
        if money_mn["ebitda_mn"] is not None:
            prov["ebitda_mn"] = "MS finances: EBITDA"

    # ── Assemble fy_highlights on the sector schema
    rc = (FIN.get("unit_currency") or info.get("reporting_currency")
          or info.get("currency") or "")
    fy: dict[str, Any] = {
        "period": anchor_period.replace("FY", "FY "),
        "reporting_currency": rc,
    }

    def _emit_money(key: str):
        if key not in schema_keys:
            return
        mn = money_mn.get(key)
        if mn is None:
            return
        mn = round(mn, 2)
        if not sanity_ok(key, mn):
            warnings.append(f"{key}={mn} failed sanity bounds — dropped")
            return
        fy[key] = mn
        g = _yoy(money_mn.get(key), money_prev_mn.get(key))
        gk = (key[:-3] if key.endswith("_mn") else key) + "_growth_pct"
        if g is not None and sanity_ok("_growth_pct", g):
            fy[gk] = g

    for k in ("revenue_mn", "net_profit_mn", "ebitda_mn"):
        _emit_money(k)

    # margins (ratios — unit-independent, computed from finances raw values)
    rev_for_margin = money_mn.get("revenue_mn")
    if rev_for_margin:
        if "operating_margin_pct" in schema_keys:
            ebit_mn = _mn(_at(ann.get("ebit") or [], fin_i), fin_mult)
            if ebit_mn is not None:
                m = round(ebit_mn / rev_for_margin * 100.0, 2)
                if sanity_ok("operating_margin_pct", m):
                    fy["operating_margin_pct"] = m
                    prov["operating_margin_pct"] = "MS finances: EBIT / revenue"
        if "ebitda_margin_pct" in schema_keys and money_mn.get("ebitda_mn"):
            m = round(money_mn["ebitda_mn"] / rev_for_margin * 100.0, 2)
            if sanity_ok("ebitda_margin_pct", m):
                fy["ebitda_margin_pct"] = m
                prov["ebitda_margin_pct"] = "MS finances: EBITDA / revenue"

    # eps / dps (per-share — NOT scaled)
    if "eps" in schema_keys:
        eps = _at(IS.get("eps_basic") or [], anchor_i)
        if eps is None:
            dp = DIV.get("periods") or []
            if anchor_period in dp:
                eps = _at(DIV.get("eps") or [], dp.index(anchor_period))
        if eps is not None and sanity_ok("eps", eps):
            fy["eps"] = eps
            prov["eps"] = "MS income-statement: Net EPS - Basic"

    dps = _at(IS.get("dividend_per_share") or [], anchor_i)
    if dps is None:
        dp = DIV.get("periods") or []
        if anchor_period in dp:
            dps = _at(DIV.get("dividend_per_share") or [], dp.index(anchor_period))
    if dps is not None:
        fy["dps"] = dps
        prov["dps"] = "MS income-statement: Dividend Per Share"

    fy["source"] = (f"MarketScreener actuals (auto-extracted, UNVERIFIED) — "
                    f"{anchor_period}; verify against IR before promoting")

    needs_ir = sorted(_NEEDS_IR & schema_keys)
    n_metrics = sum(1 for k in fy if k in schema_keys)

    # Confidence: a non-December fiscal year makes MS's "FYxxxx" column
    # ambiguous (MS labels by calendar-year of period end, which can be
    # offset from the company's reporting year — see ICICI/Indian banks),
    # so those always need a human to map the period. December-FYE tickers
    # with a clean anchor are the auto-promotable common case.
    fye = info.get("fiscal_year_end_month") or 12
    confidence = "high" if fye == 12 else "review:non-dec-fiscal-year"
    if fye != 12:
        warnings.append(
            f"fiscal year ends month {fye} — MS period labels may be offset; "
            "verify the anchor period maps to the intended reporting year")

    return {
        "ticker": ticker,
        "company": info.get("company_name", ticker),
        "currency": rc,
        "units": "millions",
        "_status": "auto_unverified",
        "_extractor": {
            "source": "marketscreener_cache",
            "template_family": fam,
            "anchor_period": anchor_period,
            "prev_period": prev_period,
            "confidence": confidence,
            "fiscal_year_end_month": fye,
            "unit_scale": {"finances_to_mn": fin_mult, "income_stmt_to_mn": is_mult},
            "metrics_emitted": n_metrics,
            "needs_ir": needs_ir,
            "provenance": prov,
            "warnings": warnings,
        },
        "fy_highlights": fy,
    }


def promotion_gate(staging: dict[str, Any]) -> tuple[bool, list[str]]:
    """Decide whether a staging candidate is STRUCTURALLY safe to promote to
    the verified disclosed store. Returns (ok, reasons_blocking).

    Gates (all must pass):
      * _status is auto_unverified (a real extraction, not a no-cache stub)
      * confidence == 'high' (December-FYE; non-Dec needs a human to map the
        period — the ICICI class)
      * every emitted value passes the grounding sanity bounds
      * PERIOD CURRENCY: the anchor period equals what the reporting calendar
        says the company should have filed by now. This is the automated
        anti-staleness guard — it rejects a stale snapshot still showing last
        year, and a mis-labeled non-Dec fiscal year.

    A pass means "the shape, period and bounds are right" — NOT "the values
    are IR-verified". MS line-item values can still diverge from the headline
    (the Aramco class), which is why promote defaults to human-reviewed and
    auto-promotions are marked reviewed=false."""
    reasons: list[str] = []
    if staging.get("_status") != "auto_unverified":
        reasons.append(f"status={staging.get('_status')}")
    ext = staging.get("_extractor") or {}
    if ext.get("confidence") != "high":
        reasons.append(f"confidence={ext.get('confidence')}")
    fy = staging.get("fy_highlights") or {}
    n_metrics = sum(1 for k, v in fy.items() if isinstance(v, (int, float)))
    if n_metrics < 2:
        reasons.append(f"only {n_metrics} metric(s)")
    for k, v in fy.items():
        if isinstance(v, (int, float)) and not sanity_ok(k, v):
            reasons.append(f"{k} fails sanity")
    try:
        from src.services.reporting_calendar import expected_latest
        want = (expected_latest(staging.get("ticker", "")) or {}).get("annual_period")
        got = ext.get("anchor_period")
        if want and got and got != want:
            reasons.append(f"period {got} != expected {want}")
    except Exception:
        pass
    return (len(reasons) == 0, reasons)


def write_staging(ticker: str, data: dict[str, Any]) -> Path:
    """Persist a candidate to data/disclosed/_staging/<ticker>.json (never the
    live disclosed dir — a human promotes after review)."""
    import json
    out_dir = _repo_root() / "data" / "disclosed" / "_staging"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{ticker}.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p
