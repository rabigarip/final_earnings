"""
Automated data validation — runs on every report before PPTX generation.

Cross-checks numbers across sources (MS vs Yahoo) and flags anomalies.
Returns a list of warnings that get embedded in the report's Data Quality line.
No warning = every check passed = numbers are trustworthy.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)


def _thresholds() -> dict:
    """Load validation thresholds from config, with safe defaults."""
    try:
        from src.config import cfg
        return cfg().get("validation", {})
    except Exception:
        return {}


def validate_report_data(payload, memo_data: dict | None = None) -> list[str]:
    """Run all validation checks. Returns list of human-readable warnings."""
    warnings: list[str] = []
    th = _thresholds()
    q = getattr(payload, "quote", None)
    c = getattr(payload, "company", None)
    cs = getattr(payload, "consensus_summary", None) or {}
    vm = getattr(payload, "ms_valuation_multiples", None) or {}
    memo = getattr(payload, "memo_computed", None) or {}

    price = getattr(q, "price", None) if q else None
    mcap = getattr(q, "market_cap", None) if q else None
    ticker = getattr(c, "ticker", "") if c else ""
    is_bank = getattr(c, "is_bank", False) if c else False

    # ── 1. Price vs Target sanity ──
    target = cs.get("average_target_price")
    if target and price and price > 0:
        ratio = target / price
        _tgt_max = th.get("target_price_max_ratio", 5.0)
        _tgt_min = th.get("target_price_min_ratio", 0.2)
        if ratio > _tgt_max:
            warnings.append(f"Target price ({target:.2f}) is {ratio:.0f}x current price ({price:.2f}) — verify")
        elif ratio < _tgt_min:
            warnings.append(f"Target price ({target:.2f}) is far below current price ({price:.2f}) — verify")

    # ── 2. P/E bounds ──
    pe_vals = vm.get("pe") or []
    _pe_max = th.get("pe_max", 500)
    for i, pe in enumerate(pe_vals):
        if pe is not None and (pe < 0 or pe > _pe_max):
            warnings.append(f"P/E {pe:.1f}x outside normal range (0-500x)")
            break
    # Yahoo P/E cross-check
    ya_fwd_pe = getattr(q, "forward_pe", None) if q else None
    if pe_vals and ya_fwd_pe:
        # Find the forward MS P/E (latest estimate)
        ms_pe = next((p for p in reversed(pe_vals) if p is not None), None)
        if ms_pe and ya_fwd_pe > 0:
            pe_ratio = ms_pe / ya_fwd_pe
            if pe_ratio > 2.0 or pe_ratio < 0.5:
                log.info("P/E divergence: MS=%.1f vs Yahoo=%.1f for %s (methodology difference)", ms_pe, ya_fwd_pe, ticker)

    # ── 3. Market cap vs Revenue sanity ──
    # Revenue should roughly relate to market cap (P/S ratio typically 0.1x-100x)
    ann_forecasts = getattr(payload, "ms_annual_forecasts", None) or {}
    ann = ann_forecasts.get("annual", {}) if isinstance(ann_forecasts, dict) else {}
    rev_list = ann.get("net_sales") or []
    latest_rev = next((r for r in reversed(rev_list) if r is not None), None)
    if mcap and latest_rev and latest_rev > 0:
        # MS revenue is typically in millions; Yahoo mcap is in raw units
        rev_scaled = latest_rev * 1e6 if latest_rev < 1e9 else latest_rev
        ps_ratio = mcap / rev_scaled
        if ps_ratio > th.get("ps_max", 200):
            warnings.append(f"Price/Sales ratio {ps_ratio:.0f}x unusually high — verify revenue units")

    # ── 4. YoY growth bounds ──
    for label, key in [("Revenue", "yoy_revenue_pct_table"), ("NI", "yoy_ni_pct_table"), ("EPS", "yoy_eps_pct_table")]:
        yoy = memo.get(key)
        if yoy is not None and abs(yoy) > th.get("yoy_extreme_pct", 500):
            warnings.append(f"{label} YoY {yoy:+.1f}% is extreme — verify comparison period")

    # ── 5. Consensus vs Yahoo target cross-check ──
    ya_target = getattr(q, "target_mean_price", None) if q else None
    ms_target = cs.get("average_target_price")
    if ya_target and ms_target and ya_target > 0:
        tgt_ratio = ms_target / ya_target
        _div_r = th.get("ms_yahoo_divergence_ratio", 3.0)
        if tgt_ratio < 1/_div_r or tgt_ratio > _div_r:
            warnings.append(f"MS target ({ms_target:.2f}) diverges from Yahoo ({ya_target:.2f}) — possible wrong entity")

    # ── 6. Currency consistency ──
    company_ccy = getattr(c, "currency", "") if c else ""
    ms_ccy = cs.get("price_currency", "")
    if company_ccy and ms_ccy and company_ccy.upper() != ms_ccy.upper():
        warnings.append(f"Currency mismatch: company={company_ccy}, MS consensus={ms_ccy}")

    # ── 7. Negative revenue ──
    cp = memo.get("calendar_prior_quarter_released") or {}
    cn = memo.get("calendar_next_quarter") or {}
    for label, val in [("Prior Q rev", cp.get("net_sales")), ("Next Q rev", cn.get("net_sales"))]:
        if val is not None and val < 0:
            warnings.append(f"{label} is negative ({val:,.0f}) — unusual, verify")

    # ── 8. EPS sign vs Net Income sign ──
    ni = cn.get("net_income")
    eps = cn.get("eps")
    if ni is not None and eps is not None:
        if (ni > 0 and eps < 0) or (ni < 0 and eps > 0):
            warnings.append(f"NI ({ni:,.0f}) and EPS ({eps:.2f}) have opposite signs")

    # ── 9. Analyst count sanity ──
    analyst_count = cs.get("analyst_count")
    if analyst_count is not None and (analyst_count < 1 or analyst_count > th.get("analyst_count_max", 100)):
        warnings.append(f"Analyst count ({analyst_count}) outside normal range (1-100)")

    # ── 10. Stale earnings date ──
    from datetime import datetime
    earnings_date = memo.get("next_earnings_date")
    if earnings_date:
        try:
            ed = datetime.strptime(str(earnings_date)[:10], "%Y-%m-%d")
            days_away = (ed - datetime.now()).days
            if days_away < -30:
                warnings.append(f"Earnings date ({earnings_date}) is in the past")
        except (ValueError, TypeError):
            pass

    # ── 11. MarketScreener data completeness (drift detection) ──
    # If MS pages returned SUCCESS but zero data points, the HTML structure may have changed
    ms_pages_ok = bool(getattr(payload, "ms_annual_forecasts", None) or
                       getattr(payload, "ms_valuation_multiples", None) or
                       getattr(payload, "ms_eps_dividend_forecasts", None))
    ms_consensus_ok = bool(cs.get("consensus_rating") or cs.get("average_target_price"))
    if not ms_pages_ok and not ms_consensus_ok:
        # Check if we even tried MS (company has an MS slug)
        ms_url = getattr(c, "marketscreener_company_url", "") if c else ""
        if ms_url:
            warnings.append("MarketScreener returned no data despite valid URL — possible HTML structure change")

    # ── 12. Yahoo data completeness ──
    ya_actuals = getattr(payload, "annual_actuals", None) or []
    if q and price and not ya_actuals:
        warnings.append("Yahoo quote exists but no annual financials — yfinance may need updating")

    # ── 13. Report has at least some financial data ──
    has_any_table_data = bool(
        memo.get("calendar_next_quarter") or
        memo.get("calendar_prior_quarter_released") or
        ann.get("net_sales") or
        ya_actuals
    )
    if not has_any_table_data:
        warnings.append("No financial data from any source — report will have empty tables")

    if warnings:
        log.warning("Data validation warnings for %s: %s", ticker, "; ".join(warnings))

    return warnings
