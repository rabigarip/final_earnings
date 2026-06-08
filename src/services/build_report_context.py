"""
Build a `ReportContext` from the legacy `ReportPayload` + `memo_data` dict.

Phase A (this commit) populates **CoverData** correctly. The rest of the
context (`summary`, `snapshot`) is filled with empty defaults so the legacy
slide renderers can keep producing slides 2–4 from the original sources
while slide 1 transitions to ReportContext. Each subsequent phase
(B, C, …) will move one slide at a time onto the new contract until the
duplicated computation paths in `generate_report.py` can be deleted.

Why a separate module
---------------------
The legacy `_write_preview_pptx_portrait` reads from ~7 different places
to compute the cover (`memo_data["header"]`, `payload.consensus_summary`,
`payload.quote`, `payload.ms_valuation_multiples`, etc.). Centralizing
those reads here means there is exactly ONE definition of, e.g., the
upside percentage shown on the cover. Slide code never reaches into
`memo_data` again — it reads `ctx.cover.upside_pct` and renders it.

No invented values
------------------
Every field is `None` when no source has it. The cover renderer must
display "—" in those cases. We never compute a default that would look
plausible; that is how the deck stayed honest in earlier audits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from src.models.report_context import (
    AnnualGrid,
    ChartSeries,
    CoverData,
    FinancialSnapshotData,
    FinancialTable,
    HeadlineRef,
    KeyExpectationCard,
    PEHistory,
    PeriodRow,
    PriceHistorySeries,
    ReportContext,
    SummaryData,
    SurpriseHistory,
    ValuationSummary,
)
from src.models.report_payload import ReportPayload


# ── Helpers ────────────────────────────────────────────────────────────────

def _company_attr(c: Any, name: str, default: Any = None) -> Any:
    """Read an attribute from the company object whether it's a dataclass,
    pydantic model, dict, or plain object."""
    if c is None:
        return default
    if isinstance(c, dict):
        return c.get(name, default)
    return getattr(c, name, default)


def _field_display(v: Any) -> Any:
    """Memo header fields can be either a plain value or a `{display_value: …}`
    dict (depending on which builder produced them). Normalise."""
    if isinstance(v, dict):
        return v.get("display_value")
    return v


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "" or v == "—":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_iso_date(v: Any) -> Optional[str]:
    """Best-effort ISO-date extractor. Accepts 'YYYY-MM-DD', datetime,
    or strings starting with the date. Returns None on anything else."""
    if v is None or v == "—":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


# ── Cover ──────────────────────────────────────────────────────────────────

def _build_cover(payload: ReportPayload, memo_data: dict | None) -> CoverData:
    c = payload.company
    q = payload.quote
    memo = payload.memo_computed or {}
    cs = payload.consensus_summary or {}
    vm = payload.ms_valuation_multiples or {}
    header = (memo_data or {}).get("header") or {}

    # ── Identity ──
    company_name = _company_attr(c, "company_name", "") or ""
    ticker = _company_attr(c, "ticker", "") or ""

    # Sector. Prefer the seed-file sector (already mapped per-exchange in
    # company_master.json) over Yahoo's raw label. Fall back to Yahoo if the
    # seed is empty. Local-exchange-specific mapping (TASI, MSX, NIFTY) is a
    # separate todo and will hook into this same field.
    seed_sector = _company_attr(c, "sector", "") or ""
    seed_industry = _company_attr(c, "industry", "") or ""
    sector = " / ".join([s for s in (seed_sector, seed_industry) if s]).strip(" /") or "—"

    # ── Currency ──
    # Cover currency = the LISTING currency in which shares trade and price
    # targets are quoted. Bloomberg's `bundle.currency` is the company's
    # IFRS reporting currency, which can legitimately differ from the
    # listing currency (e.g. ADNOC Gas trades in AED on ADX but reports its
    # financial statements in USD). We keep the cover in listing currency so
    # `TARGET PRICE: AED 4.20` matches the share's quoted price; the
    # financial-table currency is resolved separately and may differ.
    seed_curr = (_company_attr(c, "currency", "") or "").strip()
    ms_curr = (cs.get("price_currency") or "").strip() if cs else ""
    if not seed_curr or seed_curr == "USD":
        currency = ms_curr or seed_curr or "USD"
    else:
        currency = seed_curr

    # ── Period label ──
    # `_resolve_quarterly_mode` checks both the calendar source AND the
    # /finances/ quarterly fallback so the cover and the slide-3 table never
    # disagree on whether we're in quarterly or annual mode.
    af = payload.ms_annual_forecasts or {}
    ann = af.get("annual", {}) if isinstance(af, dict) else {}
    ann_dates = ann.get("announcement_dates") or []
    ann_periods = ann.get("periods") or []
    first_est_period = None
    for i, d in enumerate(ann_dates):
        if not d or str(d).strip() in ("", "-", "None"):
            first_est_period = ann_periods[i] if i < len(ann_periods) else None
            break
    has_quarterly, _, _ = _resolve_quarterly_mode(payload, memo)

    # Carry-forward decks (MS hasn't yet published forward quarterly
    # consensus, so the data anchor is the LAST REPORTED quarter)
    # carry an explicit `cover_period_label` from _compute_memo:
    # "Q1 2026 Update" instead of the misleading "Q2 2026 Earnings
    # Preview". When set, that label wins.
    cover_period_override = memo.get("cover_period_label") if memo else None
    if cover_period_override:
        period_label = str(cover_period_override)
    elif has_quarterly:
        pshort = (
            (memo_data or {}).get("preview_short")
            or memo.get("preview_quarter_short")
            or f"{(datetime.now().month - 1) // 3 + 1}Q{datetime.now().strftime('%y')}"
        )
        period_label = _qlab(pshort) + " Earnings Preview"
    else:
        fy_label = _normalize_fy_label(first_est_period) or f"FY{datetime.now().year}"
        period_label = f"{fy_label} Consensus Preview"

    # ── Report date ──
    expected = _field_display(header.get("expected_report_date"))
    report_date = _norm_iso_date(expected) or _norm_iso_date(memo.get("next_earnings_date"))

    # ── Rating ──
    rating = _field_display(header.get("recommendation"))
    rating_source = "marketscreener" if rating else ""
    if not rating:
        rating = cs.get("consensus_rating") if cs else None
        if rating:
            rating_source = "marketscreener"
    if not rating and q is not None:
        yrec = getattr(q, "recommendation_key", None) or ""
        if yrec and yrec != "none":
            rating = yrec.upper().replace("_", " ")
            rating_source = "yahoo"
    rating_str = (str(rating).strip().upper()[:20]) if rating else "—"

    # ── Target price ──
    tgt = _to_float(_field_display(header.get("average_target_price")))
    target_source = "marketscreener" if tgt is not None else ""
    if tgt is None and cs:
        tgt = _to_float(cs.get("average_target_price"))
        if tgt is not None:
            target_source = "marketscreener"
    if tgt is None and q is not None:
        tgt = _to_float(getattr(q, "target_mean_price", None))
        if tgt is not None:
            target_source = "yahoo"

    # ── Upside ──
    # Compute fresh from live price wherever possible; the legacy memo value
    # can be stale by 12+ hours when MS data is cached.
    live_price = _to_float(getattr(q, "price", None) if q else None)
    if live_price is None:
        live_price = _to_float(cs.get("last_close_price")) if cs else None
    if tgt is not None and live_price is not None and live_price > 0:
        upside_pct = round((tgt - live_price) / live_price * 100, 1)
    else:
        upside_pct = _to_float(memo.get("spread_pct")) or (
            _to_float(cs.get("upside_to_average_target_pct")) if cs else None
        )

    # ── Market cap ──
    # Source priority:
    #   1. Yahoo `marketCap` — best when ticker has Yahoo coverage.
    #   2. MS /valuation/ "Capitalization" row — published in millions of
    #      reporting currency. Pick the latest non-None entry.
    #   3. Derived from MS price × shares outstanding (last resort).
    mcap = _to_float(getattr(q, "market_cap", None) if q else None)
    mcap_source = "yahoo" if mcap is not None else ""
    if mcap is None and vm:
        cap_arr = vm.get("capitalization") or []
        latest_cap = next((c for c in reversed(cap_arr) if c is not None), None)
        if latest_cap is not None:
            try:
                # MS quotes capitalization in millions of reporting ccy.
                # Multiply back to raw units so the cover formatter shows
                # "OMR 0.6B" not "OMR 621M" (consistent with Yahoo's raw
                # market_cap output).
                mcap = float(latest_cap) * 1e6
                mcap_source = "marketscreener"
            except (TypeError, ValueError):
                mcap = None
    if mcap is None:
        # Estimate from MS last close × shares outstanding when neither
        # source above carries the value. Never leak raw price into this slot.
        ms_price = _to_float(cs.get("last_close_price")) if cs else None
        shares = None
        try:
            shares_arr = (vm or {}).get("shares") or []
            shares = next((s for s in reversed(shares_arr) if s), None)
        except Exception:
            shares = None
        s_f = _to_float(shares)
        if ms_price is not None and s_f is not None and s_f > 0:
            mcap = ms_price * s_f
            mcap_source = "marketscreener_derived"

    # ── Quality-flag-driven suppression ──
    # If MS entity validation flagged a mismatch and Yahoo has no rating /
    # target either, we must not show what we have — it would be cross-
    # company contamination. Mirror the legacy behaviour exactly.
    qflags = (memo_data or {}).get("data_quality_flags") or []
    if any(
        "entity mismatch" in (f or "").lower() or "missing current" in (f or "").lower()
        for f in qflags
    ):
        if cs and not (q and getattr(q, "target_mean_price", None)):
            rating_str = "—"
            rating_source = ""
            tgt = None
            target_source = ""

    # Quick-stats strip on the redesigned cover — pulled from the same
    # MS sources the rest of the deck uses, so the cover stays in sync.
    # Each value is optional; the renderer drops the cell when None.
    last_close: Optional[float] = None
    if cs and isinstance(cs, dict) and cs.get("last_close_price") is not None:
        try:
            last_close = float(cs["last_close_price"])
        except (TypeError, ValueError):
            pass
    if last_close is None and q is not None and getattr(q, "price", None):
        try:
            last_close = float(q.price)
        except (TypeError, ValueError):
            pass

    n_analysts: Optional[int] = None
    if cs and isinstance(cs, dict) and cs.get("analyst_count") is not None:
        try:
            n_analysts = int(cs["analyst_count"])
        except (TypeError, ValueError):
            pass

    # Forward P/E + Div Yield — sourced from the valuation grid. Picks
    # the FIRST estimate column (next FY) so the cover aligns with
    # the slide-3 Valuation Summary.
    vm = (payload.ms_valuation_multiples or {}) if hasattr(payload, "ms_valuation_multiples") else {}
    pe_fy_e: Optional[float] = None
    div_yield_pct: Optional[float] = None
    if isinstance(vm, dict):
        v_periods = vm.get("periods") or []
        v_pe = vm.get("pe") or []
        v_yield = vm.get("yield_pct") or []
        # First period whose label looks like an estimate (current/next year)
        cy = datetime.now().year
        ny = cy + 1
        for i, p in enumerate(v_periods):
            ps = str(p)
            if str(cy) in ps or str(ny) in ps:
                if pe_fy_e is None and i < len(v_pe) and v_pe[i] is not None:
                    try: pe_fy_e = float(v_pe[i])
                    except (TypeError, ValueError): pass
                if div_yield_pct is None and i < len(v_yield) and v_yield[i] is not None:
                    try: div_yield_pct = float(v_yield[i])
                    except (TypeError, ValueError): pass
                if pe_fy_e is not None and div_yield_pct is not None:
                    break

    # Performance row — fill the cover's lower band with real
    # multi-period price moves, sourced from the ms_price_performance
    # block we already fetch. None entries cause the cover renderer to
    # show a "—" cell rather than collapsing the row.
    pp = (payload.ms_price_performance or {}) if hasattr(payload, "ms_price_performance") else {}
    perf_dict = (pp.get("performance") or {}) if isinstance(pp, dict) else {}
    def _pp(key: str) -> Optional[float]:
        v = perf_dict.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Top strengths — pulled from ms_ratings when MS has them. Capped at
    # 3 because the cover band can fit ~3 short bullet lines without
    # forcing aggressive truncation. Strings are taken verbatim from MS.
    rt = (payload.ms_ratings or {}) if hasattr(payload, "ms_ratings") else {}
    raw_strengths = (rt.get("strengths") or []) if isinstance(rt, dict) else []
    top_strengths = [
        s.strip() for s in raw_strengths
        if isinstance(s, str) and s.strip()
    ][:3]

    return CoverData(
        company_name=company_name,
        ticker=ticker,
        sector=sector,
        currency=currency,
        market_cap=mcap,
        report_date=report_date,
        period_label=period_label,
        rating=rating_str,
        target_price=tgt,
        upside_pct=upside_pct,
        last_close=last_close,
        n_analysts=n_analysts,
        pe_fy_e=pe_fy_e,
        div_yield_pct=div_yield_pct,
        perf_1d_pct=_pp("perf_1d_pct"),
        perf_1w_pct=_pp("perf_1w_pct"),
        perf_1m_pct=_pp("perf_1m_pct"),
        perf_3m_pct=_pp("perf_3m_pct"),
        perf_6m_pct=_pp("perf_6m_pct"),
        perf_ytd_pct=_pp("perf_ytd_pct"),
        top_strengths=top_strengths,
        rating_source=rating_source,
        target_price_source=target_source,
        market_cap_source=mcap_source,
    )


def _normalize_fy_label(raw: str | None) -> str | None:
    """Normalise an MS period label like "2026e" / "FY26" / "2026" to a
    deck-clean "FY2026". Returns None on inputs that don't look like a
    fiscal-year string (so the caller can fall back to its own default)."""
    if not raw:
        return None
    s = str(raw).strip().upper()
    # Drop trailing "E" estimate markers.
    if s.endswith("E"):
        s = s[:-1]
    # Already FYxxxx?
    if s.startswith("FY"):
        digits = "".join(ch for ch in s[2:] if ch.isdigit())
        if len(digits) == 4:
            return f"FY{digits}"
        if len(digits) == 2:
            return f"FY20{digits}"
        return None
    # Bare year — accept 4-digit; expand 2-digit by assuming 21st century.
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 4:
        return f"FY{digits}"
    if len(digits) == 2:
        return f"FY20{digits}"
    return None


def _company_relevance_tokens(company_name: str, ticker: str) -> list[str]:
    """Build a whitelist of tokens that a headline must contain to count
    as relevant to this company. Used by the Recent Headlines filter to
    drop fuzzy-match false positives like "National Symphony" appearing
    in a search for "National Bank of Oman".

    Strategy: keep tokens ≥ 4 chars that aren't structural English
    stop-words. We deliberately KEEP geographic identifiers (Oman,
    Saudi, Qatar, etc.) because for many companies the country name is
    the most distinctive token (e.g. NBOB → "Oman" is the only
    differentiator from generic banking news). Also include the ticker
    stem. Returns lower-case tokens; empty list disables filtering.
    """
    stop = {
        # Generic corporate suffixes (carry no semantic meaning).
        "company", "limited", "ltd", "corp", "corporation",
        "holding", "holdings", "group", "international",
        "industries", "industrial",
        "co", "inc", "plc", "sao", "saog", "psc", "qsc", "psqs",
        # Generic English connectors.
        "the", "and", "for",
        # Generic descriptors that match too many false positives.
        "bank", "banking", "first", "general", "national",
        "investment", "investments",
    }
    out: list[str] = []
    name = (company_name or "").strip()
    if name:
        for raw in name.split():
            t = "".join(ch.lower() for ch in raw if ch.isalnum())
            if len(t) >= 4 and t not in stop:
                out.append(t)
    # Always accept the ticker symbol stem (before the exchange suffix).
    if ticker:
        stem = ticker.split(".")[0].lower()
        if len(stem) >= 3 and stem not in out:
            out.append(stem)
    return out


def _qlab(short: str) -> str:
    """Convert "1Q26" → "Q1 2026". Falls back to identity on unexpected input."""
    raw = (short or "").strip()
    if len(raw) < 3:
        return raw
    qn = raw[0]
    if not qn.isdigit():
        return raw
    cy2 = datetime.now().strftime("%y")
    candidates = [cy2, str(int(cy2) + 1), str(int(cy2) - 1), str(int(cy2) + 2)]
    yr = next(("20" + y for y in candidates if y in raw), str(datetime.now().year))
    return f"Q{qn} {yr}"


# ── Internal helpers shared by summary & snapshot builders ────────────────

def _yoy_pct(prior: Any, est: Any) -> Optional[float]:
    """Same convention as the legacy renderer: signed % change from prior to
    est, computed against |prior| so a negative prior does not flip the sign."""
    if (
        prior is None or est is None
        or not isinstance(prior, (int, float))
        or not isinstance(est, (int, float))
        or prior == 0
    ):
        return None
    return round((est - prior) / abs(prior) * 100, 1)


def _yoy_chain(series: list[Optional[float]]) -> list[Optional[float]]:
    """Element-wise YoY % over a series. First entry is always None
    (no prior to compare); subsequent entries are signed % vs the
    previous non-None value. Used to populate the "Change" rows on
    the slide-3 financial table that mirror what MS shows on its
    /finances/ page (PDF page 2 reference)."""
    out: list[Optional[float]] = [None]
    for i in range(1, len(series)):
        out.append(_yoy_pct(series[i - 1], series[i]))
    return out


def _to_millions(v: Any) -> Optional[float]:
    """Yahoo returns values in raw units (e.g. 5e8 for 500M); MS returns in
    millions already. Apply a magnitude heuristic: only divide when the input
    is clearly in raw units."""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x / 1e6, 1) if abs(x) >= 1e6 else x


def _resolve_annual_indices(periods: list, dates: list) -> tuple[int, int]:
    """Pick the indices of the latest reported FY (prior) and the next FY
    estimate (est) inside an MS annual grid. Mirrors the legacy logic so we
    don't shift columns."""
    prior_idx, est_idx = -1, -1
    for i, _ in enumerate(periods):
        has_date = (
            i < len(dates) and dates[i]
            and str(dates[i]).strip() not in ("", "-", "None")
        )
        if has_date:
            prior_idx = i
        elif est_idx == -1:
            est_idx = i
    if prior_idx == -1 and est_idx == -1:
        # Fall back to a year-string heuristic when announcement_dates are
        # absent (some frontier markets ship sparse calendars).
        cur_year = datetime.now().year
        for i, p in enumerate(periods):
            digits = "".join(ch for ch in str(p) if ch.isdigit())[:4]
            try:
                yr_int = int(digits)
            except ValueError:
                continue
            if yr_int < cur_year:
                prior_idx = i
            if yr_int >= cur_year and est_idx == -1:
                est_idx = i
    if est_idx == -1 and len(periods) >= 2:
        prior_idx, est_idx = len(periods) - 2, len(periods) - 1
    return prior_idx, est_idx


def _ann_lookup(
    ann: dict, eps_div: dict, vm: dict, key: str, idx: int
) -> Optional[float]:
    """Read `ann[key][idx]`; for EPS, fall back to /valuation-dividend/ then
    /valuation/. Mirrors the legacy `_ann_val` closure exactly."""
    arr = ann.get(key) or []
    if 0 <= idx < len(arr) and arr[idx] is not None:
        return arr[idx]
    if key == "eps":
        eps_arr = eps_div.get("eps") or []
        if 0 <= idx < len(eps_arr) and eps_arr[idx] is not None:
            return eps_arr[idx]
        vm_eps = vm.get("eps") or []
        if 0 <= idx < len(vm_eps) and vm_eps[idx] is not None:
            return vm_eps[idx]
    return None


def _is_estimate_period(dates: list, i: int) -> bool:
    """A period without a published announcement date is an estimate."""
    if i < 0 or i >= len(dates):
        return True
    d = dates[i]
    return not d or str(d).strip() in ("", "-", "None")


def _bbg_quarter_to_dict(q: dict) -> dict:
    """Normalize a BloombergConsensusQuarter row (already as_dict) into the
    `cp`/`cn` shape consumed downstream. BBG values are in raw units; we
    rescale to millions for non-EPS metrics so they line up with MS data."""
    metrics = q.get("metrics") or {}

    def _val(key: str):
        v = metrics.get(key)
        if v is None:
            return None
        # `metrics` was a dict[str, tuple[float|None, int|None]]; after
        # dataclasses.asdict it can come back as a list/tuple.
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        return v

    def _to_m(v):
        if v is None:
            return None
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        # BBG cons_q raw values are in units (e.g. 3194000000 for SAR3,194M).
        # Anything ≥ 1e6 we treat as raw and rescale to millions; small
        # numbers (EPS, ratios) pass through unchanged.
        return round(x / 1e6, 1) if abs(x) >= 1e6 else x

    rev = _to_m(_val("revenue"))
    ebitda = _to_m(_val("ebitda"))
    ebit = _to_m(_val("ebit"))
    # BBG splits net income into _adj and _gaap; prefer _adj (street-aligned).
    ni = _to_m(_val("net_income_adj") or _val("net_income_gaap") or _val("net_income"))
    # Same for EPS — adj is the street figure.
    eps_raw = _val("eps_adj") or _val("eps_gaap") or _val("eps")
    try:
        eps = float(eps_raw) if eps_raw is not None else None
    except (TypeError, ValueError):
        eps = None
    return {
        "net_sales": rev,
        "ebitda": ebitda,
        "ebit": ebit,
        "net_income": ni,
        "eps": eps,
        "_period_label": q.get("period_label") or "",
    }


def _resolve_quarterly_mode(
    payload: ReportPayload, memo: dict
) -> tuple[bool, dict, dict]:
    """Decide whether the deck should render in quarterly mode and return the
    resolved (cp, cn) dicts that drive slide 3.

    Source priority (per the "Bloomberg overrides everything" rule):
      0. payload.bloomberg_bundle.consensus_quarterly  — when an .xlsx is
         present, BBG fully replaces MS+Yahoo for both prior actual and
         next estimate. Provenance shown in the deck footer.
      1. memo.calendar_*  — populated by `build_report_payload` from the
         /calendar/ quarterly_results table (has both released + forecast).
      2. payload.ms_annual_forecasts.quarterly  — populated by
         `fetch_financial_forecast_series` from /finances/ (single value per
         quarter; the announcement_date column distinguishes A vs E).
    Falls through to (False, {}, {}) which triggers annual-mode rendering.

    Returned dicts may carry a synthetic `_period_label` and `_source` key
    used for slide 3 headers and footer attribution.
    """
    # ── Priority 0: Bloomberg bundle ──
    bbg = payload.bloomberg_bundle or {}
    bbg_quarters = (bbg.get("consensus_quarterly") if isinstance(bbg, dict) else None) or []
    if bbg_quarters:
        actual = next((q for q in bbg_quarters if not q.get("is_estimate")), None)
        estimate = next((q for q in bbg_quarters if q.get("is_estimate")), None)
        if actual and estimate:
            cp_bbg = _bbg_quarter_to_dict(actual)
            cn_bbg = _bbg_quarter_to_dict(estimate)
            cp_bbg["_source"] = "Bloomberg"
            cn_bbg["_source"] = "Bloomberg"
            if cp_bbg.get("net_sales") and cn_bbg.get("net_sales"):
                return True, cp_bbg, cn_bbg

    # ── Priority 1: MS calendar ──
    cp = memo.get("calendar_prior_quarter_released") or {}
    cn = memo.get("calendar_next_quarter") or {}
    has_q = bool(
        (cp.get("net_sales") or cp.get("revenue"))
        and (cn.get("net_sales") or cn.get("revenue"))
    )
    if has_q:
        return True, dict(cp), dict(cn)

    # ── Priority 2: MS /finances/ quarterly grid ──
    af = payload.ms_annual_forecasts or {}
    qb = af.get("quarterly", {}) if isinstance(af, dict) else {}
    qp = qb.get("periods") or []
    qd = qb.get("announcement_dates") or []
    pi, ei = _resolve_annual_indices(qp, qd)
    if not (0 <= pi < len(qp) and 0 <= ei < len(qp)):
        return False, {}, {}

    def _v(key: str, idx: int):
        arr = qb.get(key) or []
        return arr[idx] if 0 <= idx < len(arr) and arr[idx] is not None else None

    # MS frequently leaves the quarterly EPS row empty even when net
    # income IS published (NBOB.OM is a clean example: NI populated for
    # every quarter, EPS column entirely empty). The cards on slide 2
    # then fell back to FY-est EPS, which is the bug a reviewer caught
    # on 2026-05 (deck showed Q1 26 "EPS 0.03" — actually FY 26E —
    # against MS's actual Q1 EPS of 0.012). Compute quarterly EPS from
    # quarterly NI / shares-outstanding when MS leaves the row empty.
    # Shares come from market_cap / last_close, both already in payload.
    shares: Optional[float] = None
    cs_payload = payload.consensus_summary or {}
    last_close = (
        cs_payload.get("last_close_price") if isinstance(cs_payload, dict) else None
    )
    mcap_units: Optional[float] = None
    vm = (payload.ms_valuation_multiples or {}) if hasattr(payload, "ms_valuation_multiples") else {}
    if isinstance(vm, dict):
        # vm.capitalization is in millions of currency, ordered FY-asc
        # (oldest → newest). Walk BACKWARDS to pick the most recent
        # non-None value (typically the FY-est cap MS published most
        # recently). The earlier "first non-None" version picked the
        # oldest cap, which produced wrong shares-outstanding (e.g.
        # NBOB.OM FY21 cap 318.7M → 762M shares vs the actual 1.626B).
        for cap_v in reversed(vm.get("capitalization") or []):
            if cap_v is not None:
                try:
                    mcap_units = float(cap_v) * 1e6
                except (TypeError, ValueError):
                    mcap_units = None
                break
    # Fallback to Yahoo's market_cap on QuoteSnapshot.
    if mcap_units is None and getattr(payload, "quote", None):
        q_mcap = getattr(payload.quote, "market_cap", None)
        if q_mcap:
            try:
                mcap_units = float(q_mcap)
            except (TypeError, ValueError):
                pass
    if last_close and mcap_units and float(last_close) > 0:
        try:
            shares = mcap_units / float(last_close)
        except (TypeError, ValueError, ZeroDivisionError):
            shares = None

    def _eps_or_compute(idx: int) -> Optional[float]:
        """Return MS-published quarterly EPS if available; otherwise
        compute it from NI / shares so slide-2 cards don't fall back
        to the FY-est EPS for tickers where MS leaves the row empty."""
        v = _v("eps", idx)
        if v is not None:
            return v
        ni = _v("net_income", idx)
        if ni is None or shares is None or shares == 0:
            return None
        # MS NI is in millions of currency; shares are raw units.
        try:
            return round((float(ni) * 1e6) / float(shares), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    cp_fb = {
        "net_sales": _v("net_sales", pi), "ebitda": _v("ebitda", pi),
        "ebit": _v("ebit", pi), "net_income": _v("net_income", pi),
        "eps": _eps_or_compute(pi),
        "_period_label": str(qp[pi]),
    }
    cn_fb = {
        "net_sales": _v("net_sales", ei), "ebitda": _v("ebitda", ei),
        "ebit": _v("ebit", ei), "net_income": _v("net_income", ei),
        "eps": _eps_or_compute(ei),
        "_period_label": str(qp[ei]),
    }
    if cp_fb.get("net_sales") and cn_fb.get("net_sales"):
        return True, cp_fb, cn_fb
    return False, {}, {}


# ── Slide 3: Financial Snapshot ───────────────────────────────────────────

def _build_annual_grid(
    payload: ReportPayload,
    ann: dict,
    eps_div: dict,
    vm: dict,
    currency: str,
) -> Optional[AnnualGrid]:
    """Assemble the multi-period income-statement grid for slide 3.

    Source priority:
      1. payload.bloomberg_bundle.annuals — when the BBG xlsx is on disk we
         use the full FY series (typically 7+ historical FYs plus Current/LTM
         and forward estimates).
      2. payload.ms_annual_forecasts.annual — MS /finances/ annual block.
      3. None — slide 3 falls back to the (prior, est) PeriodRow pair.

    Slices to the 6 most-recent periods so the table fits the portrait
    layout's available width without horizontal scrolling. EPS is sourced
    from /valuation-dividend/ (`eps_div`) when /finances/ doesn't carry it,
    matching the legacy `_ann_lookup` fallback chain.
    """
    bbg = payload.bloomberg_bundle or {}
    bbg_annuals = (bbg.get("annuals") if isinstance(bbg, dict) else None) or []
    if bbg_annuals:
        # Strip Current/LTM rows so we don't double-count alongside the
        # adjacent FY actual.
        bbg_clean = [a for a in bbg_annuals if not a.get("is_ltm")]
        if not bbg_clean:
            bbg_clean = bbg_annuals
        slice_ = bbg_clean[-6:] if len(bbg_clean) > 6 else bbg_clean
        periods = []
        ann_dates: list[Optional[str]] = []
        revenue: list[Optional[float]] = []
        ebitda: list[Optional[float]] = []
        ebit: list[Optional[float]] = []
        ni: list[Optional[float]] = []
        eps: list[Optional[float]] = []
        for a in slice_:
            label = str(a.get("period_label") or "")
            is_est = bool(a.get("is_estimate"))
            # Normalise BBG labels like "FY 2026 Est" → "FY2026". The
            # renderer already adds an "(E)" suffix to estimate columns;
            # carrying "Est" inside the label produced redundant
            # "FY2026 EST (E)" headers.
            import re
            m_fy = re.match(r"\s*FY\s*(\d{4})", label, flags=re.I)
            if m_fy:
                label = f"FY{m_fy.group(1)}"
            periods.append(label)
            ann_dates.append(None if is_est else (a.get("period_end") or None))
            metrics = a.get("metrics") or {}
            revenue.append(_to_millions(metrics.get("revenue")))
            ebitda.append(_to_millions(metrics.get("ebitda")))
            ebit.append(_to_millions(metrics.get("ebit")))
            ni_val = metrics.get("net_income_adj") or metrics.get("net_income_gaap") or metrics.get("net_income")
            ni.append(_to_millions(ni_val))
            eps_val = metrics.get("eps_adj") or metrics.get("eps_gaap") or metrics.get("eps")
            try:
                eps.append(float(eps_val) if eps_val is not None else None)
            except (TypeError, ValueError):
                eps.append(None)
        return AnnualGrid(
            periods=periods, announcement_dates=ann_dates,
            revenue=revenue, ebitda=ebitda, ebit=ebit,
            net_income=ni, eps=eps,
            revenue_yoy=_yoy_chain(revenue),
            ebitda_yoy=_yoy_chain(ebitda),
            ebit_yoy=_yoy_chain(ebit),
            net_income_yoy=_yoy_chain(ni),
            eps_yoy=_yoy_chain(eps),
        )

    # MS annual path
    ms_periods = ann.get("periods") or []
    if not ms_periods:
        return None
    n = min(6, len(ms_periods))
    sl = slice(-n, None)
    raw_periods = ms_periods[sl]
    raw_dates = (ann.get("announcement_dates") or [None] * len(ms_periods))[sl]
    revenue = (ann.get("net_sales") or [None] * len(ms_periods))[sl]
    ebitda = (ann.get("ebitda") or [None] * len(ms_periods))[sl]
    ebit = (ann.get("ebit") or [None] * len(ms_periods))[sl]
    ni = (ann.get("net_income") or [None] * len(ms_periods))[sl]
    # EPS fallback to /valuation-dividend/ when /finances/ doesn't carry it.
    eps_from_ann = ann.get("eps") or [None] * len(ms_periods)
    eps_from_div = eps_div.get("eps") or []
    div_periods = eps_div.get("periods") or []
    eps: list[Optional[float]] = []
    for i, p in enumerate(raw_periods):
        v = eps_from_ann[sl][i] if i < len(eps_from_ann[sl]) else None
        if v is None and div_periods:
            try:
                j = div_periods.index(p)
                v = eps_from_div[j] if j < len(eps_from_div) else None
            except ValueError:
                v = None
        try:
            eps.append(float(v) if v is not None else None)
        except (TypeError, ValueError):
            eps.append(None)

    # The renderer adds the "(A)"/"(E)" suffix; we just normalise the period
    # label itself. MS often returns "2026e" — strip the lowercase "e" so the
    # header reads "2026 (E)" rather than "2026e (E)".
    periods_display: list[str] = []
    ann_dates: list[Optional[str]] = []
    for i, p in enumerate(raw_periods):
        d = raw_dates[i] if i < len(raw_dates) else None
        is_estimate = not d or str(d).strip() in ("", "-", "None")
        label = str(p).strip()
        if label.endswith(("e", "E")) and label[:-1].isdigit():
            label = label[:-1]
        periods_display.append(label)
        ann_dates.append(None if is_estimate else d)

    rev_l = list(revenue)
    ebitda_l = list(ebitda)
    ebit_l = list(ebit)
    ni_l = list(ni)
    return AnnualGrid(
        periods=periods_display,
        announcement_dates=ann_dates,
        revenue=rev_l, ebitda=ebitda_l, ebit=ebit_l,
        net_income=ni_l, eps=eps,
        revenue_yoy=_yoy_chain(rev_l),
        ebitda_yoy=_yoy_chain(ebitda_l),
        ebit_yoy=_yoy_chain(ebit_l),
        net_income_yoy=_yoy_chain(ni_l),
        eps_yoy=_yoy_chain(eps),
    )


def _build_snapshot(
    payload: ReportPayload, memo_data: dict | None, currency: str
) -> FinancialSnapshotData:
    """Build the financial snapshot table + valuation summary + 1Y price.

    Quarterly mode is preferred: when the memo has BOTH a prior quarter and a
    next-quarter estimate, the table renders Q-prior(A) vs Q-next(E). When
    quarterly data is sparse, fall back to FY-prior(A) vs FY-est(E). When
    the entire MS forecast block is empty, fall back to Yahoo annuals.
    """
    c = payload.company
    q = payload.quote
    memo = payload.memo_computed or {}
    af = payload.ms_annual_forecasts or {}
    ann = af.get("annual", {}) if isinstance(af, dict) else {}
    eps_div = payload.ms_eps_dividend_forecasts or {}
    vm = payload.ms_valuation_multiples or {}

    ann_periods = ann.get("periods") or eps_div.get("periods") or []
    ann_dates = ann.get("announcement_dates") or []
    prior_idx, est_idx = _resolve_annual_indices(ann_periods, ann_dates)

    has_quarterly, cp, cn = _resolve_quarterly_mode(payload, memo)
    fb_prior_label = cp.get("_period_label") if isinstance(cp, dict) else None
    fb_est_label = cn.get("_period_label") if isinstance(cn, dict) else None
    # Track which source supplied the resolved quarter pair so the deck
    # footer can attribute correctly. BBG override sets this; otherwise the
    # default sources (Yahoo for actuals, MS for estimates) are used.
    cp_source = cp.get("_source") if isinstance(cp, dict) else None
    cn_source = cn.get("_source") if isinstance(cn, dict) else None

    # Table currency may differ from cover currency. When BBG override is
    # the data source, use BBG's reporting currency; otherwise inherit the
    # cover (listing) currency. This is the only place the deck honestly
    # tells the reader "the financials are in USD even though the stock
    # trades in AED" — via the `(USDM)` units label on the table.
    bbg_top = payload.bloomberg_bundle or {}
    bbg_curr_top = (bbg_top.get("currency") or "").strip() if isinstance(bbg_top, dict) else ""
    table_currency = bbg_curr_top if (cp_source == "Bloomberg" and bbg_curr_top) else currency

    units_label_money = f"({table_currency}M)" if table_currency else "(M)"
    units_label_per_share = f"({table_currency})" if table_currency else ""

    def _row(label: str, prior: Any, est: Any) -> PeriodRow:
        # The legacy rows tuple was (label, prior, est, yoy). We carry both
        # values on the dataclass and let the renderer compute display.
        return PeriodRow(
            label=label,
            is_estimate=False,  # not used at row-pair granularity
            announcement_date=None,
            revenue=None,
            ebitda=None,
            ebit=None,
            net_income=None,
            eps=None,
        )

    rows: list[PeriodRow] = []
    yoy_map: dict[str, Optional[float]] = {}

    if has_quarterly:
        mode = "quarterly"
        # When the calendar source is used, exact quarter labels are unknown
        # at this layer (they live in `memo.next_quarter_label`); fall back to
        # generic "Q prior / Q next" headers. When we built `cp`/`cn` from the
        # /finances/ quarterly grid, we know the exact period labels —
        # surface them so slide 3's headers say e.g. "2025 Q4 (A)".
        prior_label = fb_prior_label or "Q prior"
        est_label = fb_est_label or "Q next"
        prior_row = PeriodRow(
            label=prior_label, is_estimate=False, announcement_date=None,
            revenue=cp.get("net_sales"),
            ebitda=cp.get("ebitda"),
            ebit=cp.get("ebit"),
            net_income=cp.get("net_income"),
            eps=cp.get("eps"),
        )
        est_row = PeriodRow(
            label=est_label, is_estimate=True, announcement_date=None,
            revenue=cn.get("net_sales") or cn.get("revenue"),
            ebitda=cn.get("ebitda"),
            ebit=cn.get("ebit"),
            net_income=cn.get("net_income"),
            eps=cn.get("eps"),
        )
        rows = [prior_row, est_row]
        # Per-metric QoQ fallback. Earlier this was all-or-nothing: when
        # `memo.yoy_revenue_pct_table` was set but `memo.yoy_eps_pct_table`
        # was None, the EPS card showed no delta because the all-None
        # check was false. Now each metric falls back independently to
        # `_yoy_pct(prior, est)` (which is QoQ in quarterly mode) when
        # the upstream YoY value is missing.
        yoy_map = {
            "revenue":    memo.get("yoy_revenue_pct_table") or _yoy_pct(prior_row.revenue, est_row.revenue),
            "ebitda":     memo.get("yoy_ebitda_pct_table")  or _yoy_pct(prior_row.ebitda, est_row.ebitda),
            "net_income": memo.get("yoy_ni_pct_table")      or _yoy_pct(prior_row.net_income, est_row.net_income),
            "eps":        memo.get("yoy_eps_pct_table")     or _yoy_pct(prior_row.eps, est_row.eps),
        }
    else:
        mode = "annual"
        prior_label = (
            ann_periods[prior_idx] if 0 <= prior_idx < len(ann_periods) else "Prior"
        )
        est_label = (
            ann_periods[est_idx] if 0 <= est_idx < len(ann_periods) else "Current"
        )

        def _v(key: str, idx: int) -> Optional[float]:
            return _ann_lookup(ann, eps_div, vm, key, idx)

        prior_row = PeriodRow(
            label=str(prior_label), is_estimate=False,
            announcement_date=ann_dates[prior_idx] if 0 <= prior_idx < len(ann_dates) else None,
            revenue=_v("net_sales", prior_idx),
            ebitda=_v("ebitda", prior_idx),
            ebit=_v("ebit", prior_idx),
            net_income=_v("net_income", prior_idx),
            eps=_v("eps", prior_idx),
        )
        est_row = PeriodRow(
            label=str(est_label), is_estimate=True, announcement_date=None,
            revenue=_v("net_sales", est_idx),
            ebitda=_v("ebitda", est_idx),
            ebit=_v("ebit", est_idx),
            net_income=_v("net_income", est_idx),
            eps=_v("eps", est_idx),
        )
        rows = [prior_row, est_row]
        yoy_map = {
            "revenue":   _yoy_pct(prior_row.revenue, est_row.revenue),
            "ebitda":    _yoy_pct(prior_row.ebitda, est_row.ebitda),
            "net_income": _yoy_pct(prior_row.net_income, est_row.net_income),
            "eps":       _yoy_pct(prior_row.eps, est_row.eps),
        }

    # ── Bloomberg ANNUAL override ──
    # When BBG xlsx is present and we are in annual mode, prefer BBG's clean
    # annual grid over MS's parser. BBG annuals carry full historical FY rows
    # and (when available) forward FY estimates flagged is_estimate=True. We
    # only override when BBG has BOTH a prior actual AND a forward estimate
    # for the deck — otherwise the MS path produces a more complete table.
    bbg = payload.bloomberg_bundle or {}
    bbg_annuals = (bbg.get("annuals") if isinstance(bbg, dict) else None) or []
    if not has_quarterly and bbg_annuals:
        bbg_actuals = [a for a in bbg_annuals if not a.get("is_estimate") and not a.get("is_ltm")]
        bbg_estimates = [a for a in bbg_annuals if a.get("is_estimate")]
        if bbg_actuals and bbg_estimates:
            ba = bbg_actuals[-1]   # latest actual FY
            be = bbg_estimates[0]  # nearest forward FY estimate

            def _bm(period: dict, key: str):
                v = (period.get("metrics") or {}).get(key)
                return _to_millions(v) if v is not None else None

            prior_row = PeriodRow(
                label=str(ba.get("period_label") or "Prior"),
                is_estimate=False,
                announcement_date=ba.get("period_end"),
                revenue=_bm(ba, "revenue"),
                ebitda=_bm(ba, "ebitda"),
                ebit=_bm(ba, "ebit"),
                net_income=_bm(ba, "net_income"),
                eps=(ba.get("metrics") or {}).get("eps"),
            )
            est_row = PeriodRow(
                label=str(be.get("period_label") or "Current"),
                is_estimate=True,
                announcement_date=None,
                revenue=_bm(be, "revenue"),
                ebitda=_bm(be, "ebitda"),
                ebit=_bm(be, "ebit"),
                net_income=_bm(be, "net_income"),
                eps=(be.get("metrics") or {}).get("eps"),
            )
            rows = [prior_row, est_row]
            yoy_map = {
                "revenue":    _yoy_pct(prior_row.revenue, est_row.revenue),
                "ebitda":     _yoy_pct(prior_row.ebitda, est_row.ebitda),
                "net_income": _yoy_pct(prior_row.net_income, est_row.net_income),
                "eps":        _yoy_pct(prior_row.eps, est_row.eps),
            }
            cp_source = "Bloomberg"
            cn_source = "Bloomberg"

    # Yahoo fallback when the entire MS / quarterly block is empty.
    rows_empty = all(
        r.revenue is None and r.net_income is None and r.eps is None for r in rows
    )
    # Default source attribution. Overridden below when BBG / Yahoo paths take over.
    actuals_source = cp_source or "Yahoo Finance"
    estimates_source = cn_source or "MarketScreener"
    if rows_empty:
        ya = sorted(payload.annual_actuals or [], key=lambda p: p.period_label, reverse=True)
        if len(ya) >= 2:
            cur, pri = ya[0], ya[1]
            prior_row = PeriodRow(
                label=str(getattr(pri, "period_label", "Prior")),
                is_estimate=False,
                announcement_date=None,
                revenue=_to_millions(getattr(pri, "revenue", None)),
                ebitda=_to_millions(getattr(pri, "ebitda", None)),
                ebit=_to_millions(getattr(pri, "ebit", None)),
                net_income=_to_millions(getattr(pri, "net_income", None)),
                eps=getattr(pri, "eps", None),
            )
            est_row = PeriodRow(
                label=str(getattr(cur, "period_label", "Current")),
                is_estimate=False,  # both are actuals in the Yahoo path
                announcement_date=None,
                revenue=_to_millions(getattr(cur, "revenue", None)),
                ebitda=_to_millions(getattr(cur, "ebitda", None)),
                ebit=_to_millions(getattr(cur, "ebit", None)),
                net_income=_to_millions(getattr(cur, "net_income", None)),
                eps=getattr(cur, "eps", None),
            )
            rows = [prior_row, est_row]
            yoy_map = {
                "revenue":   _yoy_pct(prior_row.revenue, est_row.revenue),
                "ebitda":    _yoy_pct(prior_row.ebitda, est_row.ebitda),
                "net_income": _yoy_pct(prior_row.net_income, est_row.net_income),
                "eps":       _yoy_pct(prior_row.eps, est_row.eps),
            }
            actuals_source = "Yahoo Finance"
            estimates_source = "Yahoo Finance"

    # ── Multi-period annual grid (slide 3 main table) ──
    # Show the full MS snapshot — 5–6 years of Revenue / EBITDA / EBIT /
    # NI / EPS — matching the gold-standard deck. The (prior, est) pair
    # above remains for slide-2 cards and the QoQ/YoY chip; slide 3 uses
    # this richer view when annual data is available.
    annual_grid = _build_annual_grid(payload, ann, eps_div, vm, table_currency)

    table = FinancialTable(
        mode=mode,
        rows=rows,
        currency=table_currency,
        units_label=units_label_money,
        units_label_per_share=units_label_per_share,
        yoy_by_metric=yoy_map,
        annual_grid=annual_grid,
        actuals_source=actuals_source,
        estimates_source=estimates_source,
        estimates_as_of=datetime.now().strftime("%Y-%m-%d"),
    )

    # ── Valuation Summary ──
    pv = vm.get("periods") or []
    cy, ny = str(datetime.now().year), str(datetime.now().year + 1)
    fy_idx = next(
        (i for i, p in enumerate(pv) if cy in str(p) or ny in str(p)),
        len(pv) - 1 if pv else -1,
    )

    def _pick(arr: list) -> Optional[float]:
        if not arr:
            return None
        if 0 <= fy_idx < len(arr) and arr[fy_idx] is not None:
            return arr[fy_idx]
        for v in reversed(arr):
            if v is not None:
                return v
        return None

    pe = _pick(vm.get("pe") or [])
    evv = _pick(vm.get("ev_ebitda") or []) or _pick(vm.get("ev_ebit") or [])
    pb = _pick(vm.get("pbr") or [])
    dy = _pick(vm.get("yield_pct") or [])
    if q is not None:
        if pe is None:
            pe = getattr(q, "forward_pe", None) or getattr(q, "trailing_pe", None)
        if evv is None:
            evv = getattr(q, "ev_to_ebitda", None)
        if pb is None:
            pb = getattr(q, "price_to_book", None)
        if dy is None:
            dy_raw = getattr(q, "dividend_yield", None)
            if dy_raw is not None:
                dy = round(dy_raw * 100, 2) if dy_raw < 1 else dy_raw

    valuation = ValuationSummary(
        pe_fy_e=_to_float(pe),
        ev_ebitda=_to_float(evv),
        pb=_to_float(pb),
        div_yield=_to_float(dy),
        bank_disclaimer_needed=bool(_company_attr(c, "is_bank", False)),
    )

    # ── 1-Year Price chart ──
    price_history = None
    hist_dates = (getattr(q, "price_history_dates", None) or []) if q else []
    hist_prices = (getattr(q, "price_history_prices", None) or []) if q else []
    if hist_dates and hist_prices and len(hist_dates) >= 10:
        price_history = PriceHistorySeries(
            dates=list(hist_dates),
            prices=list(hist_prices),
        )

    return FinancialSnapshotData(
        table=table, valuation=valuation, price_history=price_history,
    )


# ── Slide 2: Executive Summary ────────────────────────────────────────────

def _build_summary(
    payload: ReportPayload,
    memo_data: dict | None,
    cover: CoverData,
    snapshot: FinancialSnapshotData,
    *,
    iv_text: str = "",
    watch: list[str] | None = None,
) -> SummaryData:
    """Populate Executive Summary inputs.

    `iv_text` and `watch` are computed by the upstream `_iv_text_and_watch`
    helper (which orchestrates Gemini + the analytical fallback) and threaded
    in by `build()`. The builder does not re-derive thesis prose here.
    """
    c = payload.company
    memo = payload.memo_computed or {}
    af = payload.ms_annual_forecasts or {}
    ann = af.get("annual", {}) if isinstance(af, dict) else {}
    eps_div = payload.ms_eps_dividend_forecasts or {}
    vm = payload.ms_valuation_multiples or {}
    sections = (memo_data or {}).get("pptx_sections") or {}

    ann_periods = ann.get("periods") or eps_div.get("periods") or []
    ann_dates = ann.get("announcement_dates") or []
    prior_idx, est_idx = _resolve_annual_indices(ann_periods, ann_dates)

    # ── Surprise history ──
    surprise = SurpriseHistory(
        avg_revenue_surprise_pct=_to_float(memo.get("avg_revenue_surprise_pct")),
        avg_eps_surprise_pct=_to_float(memo.get("avg_eps_surprise_pct")),
        quarters_observed=int(memo.get("surprise_quarters_observed") or 0),
        beat_count=int(memo.get("surprise_beat_count") or 0),
        miss_count=int(memo.get("surprise_miss_count") or 0),
    )

    # ── Card values: Revenue, EPS, EBITDA Margin ──
    # Source-of-truth is the snapshot's est_row (built by `_build_snapshot`
    # which already applied the BBG → MS-calendar → MS-finances → Yahoo
    # priority chain). Reading from the snapshot prevents "table says X but
    # cards say Y" drift across sources.
    est_row = snapshot.table.rows[1] if len(snapshot.table.rows) >= 2 else None
    rv = est_row.revenue if est_row else None
    ev_eps = est_row.eps if est_row else None
    em_revenue = est_row.revenue if est_row else None
    em_ebitda = est_row.ebitda if est_row else None

    # Fall back to the multi-period annual grid when the quarterly est_row
    # is sparse (e.g. NBOB.OM where MS doesn't publish a quarterly EPS
    # forecast). The grid carries forward FY EPS from /valuation-dividend/
    # via the lookup chain in `_build_annual_grid`. Pick the first
    # estimate column (the next FY) so the card aligns with the cover's
    # FY-preview framing.
    grid = snapshot.table.annual_grid
    if grid and grid.periods:
        # The first period whose announcement_date is None is the next FY
        # estimate — same convention the renderer uses for shading.
        for i, d in enumerate(grid.announcement_dates):
            if not d and i < len(grid.eps):
                if ev_eps is None and grid.eps[i] is not None:
                    ev_eps = grid.eps[i]
                if rv is None and i < len(grid.revenue) and grid.revenue[i] is not None:
                    rv = grid.revenue[i]
                if em_revenue is None and i < len(grid.revenue):
                    em_revenue = grid.revenue[i]
                if em_ebitda is None and i < len(grid.ebitda):
                    em_ebitda = grid.ebitda[i]
                break

    # YoY deltas come from the same yoy_by_metric map the table uses, so the
    # number under each card lines up with the YoY % column on slide 3.
    yoy_map = snapshot.table.yoy_by_metric or {}
    rev_delta = _to_float(yoy_map.get("revenue") or memo.get("yoy_revenue_pct_table") or memo.get("qoq_revenue_pct"))
    eps_delta = _to_float(yoy_map.get("eps")     or memo.get("yoy_eps_pct_table")     or memo.get("qoq_eps_pct"))

    is_bank = bool(_company_attr(c, "is_bank", False))
    if em_revenue and em_ebitda and em_revenue != 0:
        em_pct = round(em_ebitda / em_revenue * 100, 1)
        em_value = f"{em_pct}%"
    else:
        em_value = "N/A*" if is_bank else "—"

    # Currency hints make the cards self-describing — the seed deck showed
    # "12,448" with no unit; reviewers had to scroll to slide 3 to confirm
    # the currency. Now each card carries its unit explicitly.
    money_unit = f"{cover.currency}M" if cover.currency else "M"
    eps_unit = cover.currency or ""
    cards = [
        KeyExpectationCard(
            label="Revenue",
            value_str=_format_card_value(rv, is_eps=False),
            delta_pct=rev_delta,
            delta_str=_format_signed_pct(rev_delta) if (rv is not None and rev_delta is not None) else None,
            unit=money_unit if rv is not None else "",
        ),
        KeyExpectationCard(
            label="EPS",
            value_str=_format_card_value(ev_eps, is_eps=True),
            delta_pct=eps_delta,
            delta_str=_format_signed_pct(eps_delta) if (ev_eps is not None and eps_delta is not None) else None,
            unit=eps_unit if ev_eps is not None else "",
        ),
        KeyExpectationCard(
            label="EBITDA Margin",
            value_str=em_value,
            delta_pct=None,
            delta_str=None,
            unit="",  # already a %, no currency
        ),
    ]

    # ── Income Statement chart series ──
    bbg = payload.bloomberg_bundle or {}
    bbg_annuals = (bbg.get("annuals") if isinstance(bbg, dict) else None) or []
    if bbg_annuals:
        # Strip LTM rows so we don't double-count alongside the adjacent FY.
        bbg_noltm = [a for a in bbg_annuals if not a.get("is_ltm")]
        bbg_slice = bbg_noltm[-6:] if len(bbg_noltm) > 6 else bbg_noltm
        chart_periods = [str(a.get("period_label") or "") for a in bbg_slice]
        rev_series = [(a.get("metrics") or {}).get("revenue") for a in bbg_slice]
        ni_series = [(a.get("metrics") or {}).get("net_income") for a in bbg_slice]
        ebit_series: list[Optional[float]] = []  # BBG FA has no EBIT line
        actuals_boundary = -1
        for i, a in enumerate(bbg_slice):
            if not a.get("is_estimate"):
                actuals_boundary = i
        chart_source = "Bloomberg"
    else:
        periods = [str(p) for p in (ann_periods or []) if str(p).strip()]
        chart_periods = periods[-6:] if len(periods) > 6 else periods
        n = len(chart_periods)
        rev_series = (ann.get("net_sales") or [])[-n:] if n else []
        ni_series = (ann.get("net_income") or [])[-n:] if n else []
        ebit_raw = (ann.get("ebit") or [])[-n:] if n else []
        ebit_series = ebit_raw if any(v is not None for v in ebit_raw) else []
        date_slice = (ann.get("announcement_dates") or [])[-n:]
        actuals_boundary = -1
        for i, d in enumerate(date_slice):
            if d and str(d).strip() not in ("", "-", "None"):
                actuals_boundary = i
        chart_source = "MarketScreener"

    income_chart = None
    if chart_periods and (any(rev_series) or any(ni_series)):
        income_chart = ChartSeries(
            periods=list(chart_periods),
            revenue=list(rev_series),
            ebit=list(ebit_series),
            net_income=list(ni_series),
            actuals_boundary=actuals_boundary,
            source_label=chart_source,
        )

    # ── P/E chart ──
    pe_chart = None
    if chart_periods:
        pe_vals = (vm.get("pe") or [])[-len(chart_periods):]
        if pe_vals and any(v for v in pe_vals if v):
            hist_pe = [
                float(v)
                for v in pe_vals[: max(0, actuals_boundary + 1)]
                if isinstance(v, (int, float)) and v and v > 0
            ]
            avg = (sum(hist_pe) / len(hist_pe)) if hist_pe else None
            pe_chart = PEHistory(
                periods=list(chart_periods),
                pe_values=list(pe_vals),
                five_yr_avg=avg,
            )

    # ── What to Watch / Catalysts / Risks ──
    # Prefer Gemini sections when present; else use the analytical fallback
    # (`watch` arg). The legacy renderer's hard-coded "Guidance / Segment / FX
    # / Capital allocation" placeholder is dropped per the editorial decision
    # to either generate from real news or omit. An empty list hides the
    # section in the renderer.
    wtw_raw = sections.get("what_to_watch") if isinstance(sections, dict) else None
    if wtw_raw and isinstance(wtw_raw, list):
        what_to_watch = [str(x).strip() for x in wtw_raw if str(x).strip()][:4]
    elif watch:
        what_to_watch = [str(x).strip() for x in watch if str(x).strip()][:4]
    else:
        what_to_watch = []

    cat_raw = sections.get("catalysts") if isinstance(sections, dict) else None
    catalysts = (
        [str(x).strip() for x in cat_raw if str(x).strip()][:3]
        if isinstance(cat_raw, list) else []
    )
    risk_raw = sections.get("risks") if isinstance(sections, dict) else None
    risks = (
        [str(x).strip() for x in risk_raw if str(x).strip()][:3]
        if isinstance(risk_raw, list) else []
    )

    # ── Recent Headlines sidebar ──
    # Pull up to 4 most-recent items from `payload.news_items`. Editorial
    # rule: thesis prose refers to *themes* only; specific headlines live
    # here so the deck never stitches news copy mid-paragraph (the
    # "Argaam Volume…" bug from the gold deck).
    #
    # Relevance filter: a headline must mention the ticker OR a
    # distinctive company-name token (≥4 chars, not a common stop-word).
    # Without this, fuzzy news APIs (Google News) return false positives
    # — the "National Symphony" headline on the NBOB deck because the
    # query was "National Bank of Oman".
    company_tokens = _company_relevance_tokens(
        cover.company_name, cover.ticker,
    )

    def _headline_relevant(h: str) -> bool:
        if not company_tokens:
            return True  # no whitelist — accept everything (legacy behaviour)
        hl = h.lower()
        return any(tok in hl for tok in company_tokens)

    raw_news = list(getattr(payload, "news_items", None) or [])
    # Sort by published_at descending; treat missing dates as oldest.
    raw_news.sort(
        key=lambda n: getattr(n, "published_at", None) or datetime(1970, 1, 1),
        reverse=True,
    )

    def _build_list(filter_fn) -> list[HeadlineRef]:
        out: list[HeadlineRef] = []
        seen: set[str] = set()
        for item in raw_news:
            if len(out) >= 4:
                break
            h = (getattr(item, "headline", None) or "").strip()
            if not h or h.lower() in seen:
                continue
            if not filter_fn(h):
                continue
            if len(h) > 90:
                h = h[:87] + "…"
            published = getattr(item, "published_at", None)
            date_iso = (
                published.date().isoformat()
                if isinstance(published, datetime) else None
            )
            out.append(HeadlineRef(
                headline=h, date=date_iso,
                source=(getattr(item, "source", None) or "").strip(),
                url=(getattr(item, "url", None) or "").strip(),
            ))
            seen.add(h.lower())
        return out

    # Pass 1: strict relevance filter. Drops false-positives like
    # "National Symphony" matching "National Bank of Oman".
    headlines = _build_list(_headline_relevant)
    # Pass 2: if the strict filter rejected EVERY headline (the company
    # name is too generic to generate a useful whitelist, e.g. names
    # whose only distinctive token is the ticker abbreviation), fall
    # back to showing whatever news we have. Better to show roughly-
    # related items than to leave the sidebar empty.
    if not headlines and raw_news:
        headlines = _build_list(lambda _h: True)

    return SummaryData(
        period_label=cover.period_label,
        company_name=cover.company_name,
        thesis_text=iv_text or "",
        thesis_source="gemini" if (sections.get("investment_thesis") if isinstance(sections, dict) else None) else "analytical_fallback",
        surprise=surprise,
        cards=cards,
        income_chart=income_chart,
        pe_chart=pe_chart,
        headlines=headlines,
        what_to_watch=what_to_watch,
        catalysts=catalysts,
        risks=risks,
        consensus_unavailable=bool(memo.get("quarterly_consensus_unavailable")),
        last_reported_quarter_label=str(memo.get("last_reported_quarter_label") or ""),
    )


def _format_card_value(v: Any, *, is_eps: bool) -> str:
    """Mirror legacy `pn()` card formatting: `12,359` for revenue,
    `10.18` for EPS, `—` for None.

    EPS precision mirrors the slide-3 grid: 3 decimals for sub-0.5
    values (Oman / Bangladesh / India sub-rial EPS), 2 decimals
    otherwise. Two decimals on Q1 26 EPS = 0.012 collapsed to "0.01"
    which a reviewer flagged on 2026-05 as misleading vs the actual
    MS-reported value."""
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if is_eps:
        if abs(x) < 0.5 and x != 0:
            return f"{x:,.3f}"
        return f"{x:,.2f}" if x != int(x) else f"{int(x):,}"
    if abs(x) >= 1e6:
        return f"{x:,.0f}"
    return f"{x:,.2f}" if x != int(x) else f"{int(x):,}"


def _format_signed_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


# ── Public entry point ────────────────────────────────────────────────────

def build(
    payload: ReportPayload,
    memo_data: dict | None = None,
    *,
    iv_text: str = "",
    watch: list[str] | None = None,
    quality_flags: list[str] | None = None,
) -> ReportContext:
    """Assemble a ReportContext from the pipeline outputs.

    This is the only legitimate constructor for ReportContext during the
    migration. Slide renderers must accept a ReportContext and never reach
    back into `payload` or `memo_data`.

    `iv_text` and `watch` are produced by the upstream `_iv_text_and_watch`
    helper in `generate_report.py` (which orchestrates Gemini and the
    analytical fallback). They are threaded in rather than re-derived here
    so the builder stays free of LLM concerns.
    """
    cover = _build_cover(payload, memo_data)
    snapshot = _build_snapshot(payload, memo_data, cover.currency)
    summary = _build_summary(
        payload, memo_data, cover, snapshot, iv_text=iv_text, watch=watch
    )

    # MS-extras slides — each builder is no-op-safe when its section is None.
    from src.services.build_extras_context import (
        build_ratings as _build_ratings,
        build_sector as _build_sector,
        build_price_action as _build_price_action,
        build_income_evolution as _build_income_evolution,
    )
    ratings = _build_ratings(getattr(payload, "ms_ratings", None))
    income_evolution = _build_income_evolution(
        getattr(payload, "ms_quarterly_forecasts", None)
        or getattr(payload, "ms_annual_forecasts", None),
        getattr(payload, "ms_calendar_events", None),
        units_label=cover.currency,
    )
    sector_label = (
        _company_attr(payload.company, "sector", "") or
        _company_attr(payload.company, "industry", "") or ""
    )
    sector = _build_sector(
        getattr(payload, "ms_sector_peers", None),
        getattr(payload, "ms_ratings", None),
        sector_label=sector_label,
    )
    price_action = _build_price_action(
        getattr(payload, "ms_price_performance", None),
        getattr(payload, "ms_analyst_recommendations", None),
    )

    resolved_flags = list(
        quality_flags
        if quality_flags is not None
        else ((memo_data or {}).get("data_quality_flags") or [])
    )
    # Surface carry-forward state as a deck-wide quality flag so slide 3's
    # footer banner names it explicitly. Reader sees one unambiguous line:
    # "Q2 2026 quarterly consensus pending — Key Expectations show Q1 2026
    # actuals." instead of having to infer it from chip wording.
    if summary.consensus_unavailable and summary.last_reported_quarter_label:
        import re as _rqlbl2
        m_last = _rqlbl2.search(r"(\d{4})\s*Q(\d)|Q(\d)\s*(\d{4})",
                                summary.last_reported_quarter_label)
        if m_last:
            last_yr = m_last.group(1) or m_last.group(4)
            last_q = m_last.group(2) or m_last.group(3)
            last = f"Q{last_q} {last_yr}"
        else:
            last = summary.last_reported_quarter_label
        rolled = cover.period_label.replace(" Earnings Preview", "").strip() or "next quarter"
        flag = (
            f"{rolled} quarterly consensus pending — Key Expectations show "
            f"{last} actuals"
        )
        if flag not in resolved_flags:
            resolved_flags.append(flag)

    return ReportContext(
        run_id=payload.run_id,
        generated_at=payload.generated_at,
        ticker=cover.ticker,
        company_name=cover.company_name,
        currency=cover.currency,
        cover=cover,
        summary=summary,
        snapshot=snapshot,
        income_evolution=income_evolution,
        ratings=ratings,
        sector=sector,
        price_action=price_action,
        quality_flags=resolved_flags,
    )
