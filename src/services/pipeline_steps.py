"""
Small pipeline step functions consolidated into one module.

Each function corresponds to one step in the earnings preview pipeline.
All return StepResult for uniform logging and error handling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config import cfg
from src.models.company import CompanyMaster
from src.models.financials import DerivedMetrics, FinancialPeriod
from src.models.news import NewsItem
from src.models.step_result import Status, StepResult, StepTimer
from src.providers.yahoo import (
    validate_ticker as _yahoo_validate,
    fetch_quote as _yahoo_quote,
    fetch_financials as _yahoo_fin,
    fetch_analyst_estimates as _yahoo_est,
    fetch_next_earnings_date as _yahoo_earnings_date,
)
from src.providers.marketscreener import fetch_consensus as _ms_consensus
from src.providers.marketscreener_pages import resolve_slug_from_search as _ms_resolve_slug_from_search
from src.services.entity_resolution import ensure_marketscreener_cached, get_effective_marketscreener_slug
from src.services.recent_context_pipeline import run as _run_context_pipeline
from src.storage.db import set_marketscreener_source_redirect


# ═══════════════════════════════════════════════════════════════════════════════
# 1. validate_ticker
# ═══════════════════════════════════════════════════════════════════════════════

def validate_ticker(ticker: str) -> StepResult:
    with StepTimer() as t:
        info = _yahoo_validate(ticker)
    if info is None:
        # Yahoo failed — check local DB or try auto-discovery before giving up.
        from src.storage.db import load_company
        exists_locally = bool(load_company(ticker))
        if not exists_locally:
            # Last chance: auto-discovery uses same yfinance API but also inserts into DB
            try:
                from src.services.resolve_mapping import _auto_discover
                discovered = _auto_discover(ticker)
                if discovered:
                    exists_locally = True
            except Exception:
                pass
        if not exists_locally:
            return StepResult(
                step_name="validate_ticker", status=Status.FAILED, source="yahoo",
                message=f"Ticker '{ticker}' not found on Yahoo Finance",
                error_detail="yfinance returned no name or quoteType",
                elapsed_seconds=t.elapsed,
            )
        return StepResult(
            step_name="validate_ticker", status=Status.PARTIAL, source="yahoo",
            message=f"Yahoo identity lookup failed for {ticker} (continuing with local mapping)",
            error_detail="yfinance returned no name or quoteType",
            data={"name": "", "exchange": "", "currency": "", "market_cap": None, "quote_type": ""},
            elapsed_seconds=t.elapsed,
        )
    return StepResult(
        step_name="validate_ticker", status=Status.SUCCESS, source="yahoo",
        message=f"{ticker} validated: {info['name']} on {info['exchange']} ({info['currency']})",
        data=info, elapsed_seconds=t.elapsed,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. fetch_quote
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_quote(ticker: str) -> StepResult:
    with StepTimer() as t:
        q = _yahoo_quote(ticker)
    if q is None:
        return StepResult(
            step_name="fetch_quote", status=Status.PARTIAL, source="yahoo",
            message=f"Yahoo Finance — quote: No price data returned for {ticker}",
            elapsed_seconds=t.elapsed,
        )
    sign = "+" if (q.change_pct or 0) >= 0 else ""
    mcap = f" | MCap {q.market_cap:,.0f}" if q.market_cap else ""
    return StepResult(
        step_name="fetch_quote", status=Status.SUCCESS, source="yahoo",
        message=f"Quote: {q.currency} {q.price:.2f} ({sign}{q.change_pct}%){mcap}",
        data=q, elapsed_seconds=t.elapsed,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. fetch_financials
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_financials(ticker: str, company: CompanyMaster) -> StepResult:
    with StepTimer() as t:
        data = _yahoo_fin(ticker, company.currency, company.is_bank)
    q_count = len(data["quarterly"])
    a_count = len(data["annual"])
    total = q_count + a_count
    if total == 0:
        return StepResult(
            step_name="fetch_financials", status=Status.FAILED, source="yahoo",
            message=f"No financial data returned for {ticker}",
            elapsed_seconds=t.elapsed,
        )
    bank_note = " (bank: EBITDA skipped)" if company.is_bank else ""
    status = Status.SUCCESS if q_count >= 4 else Status.PARTIAL
    return StepResult(
        step_name="fetch_financials", status=status, source="yahoo",
        message=f"Financials: {q_count} quarters, {a_count} annual{bank_note}",
        record_count=total, data=data, elapsed_seconds=t.elapsed,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. fetch_consensus
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_consensus(ticker: str, company: CompanyMaster) -> StepResult:
    with StepTimer() as t:
        company_dict = company.model_dump()
        slug = get_effective_marketscreener_slug(company_dict)
        if not slug and getattr(company, "isin", None):
            updated = ensure_marketscreener_cached(ticker, company_dict)
            if updated:
                slug = get_effective_marketscreener_slug(updated)
        if not slug:
            slug = _ms_resolve_slug_from_search(ticker) or ""
            # Guardrail: when using the weak search fallback, validate the candidate page
            # against our expected entity before scraping numbers.
            if slug:
                try:
                    from src.services.entity_resolution import validate_candidate_page
                    from src.storage.db import reject_marketscreener_candidate as _reject
                    candidate_url = f"https://www.marketscreener.com/quote/stock/{slug}/"
                    vr = validate_candidate_page(company_dict, slug, candidate_url, cache_name=f"validate_search_{ticker.replace('.', '_')}")
                    if not vr.valid:
                        _reject(ticker, reason=vr.rejection_reason or "search_candidate_validation_failed", status="needs_review")
                        slug = ""
                except Exception:
                    # If validation fails unexpectedly, proceed with slug but downstream
                    # fingerprinting and QA can still suppress MS sections.
                    pass
        ms, diagnostic = _ms_consensus(
            slug, company.currency, company.is_bank,
            ticker=ticker,
            company_name=getattr(company, "company_name", None) or getattr(company, "company_name_long", None),
            isin=getattr(company, "isin", None),
        )
        if (
            (not ms or len(ms) == 0)
            and diagnostic
            and diagnostic.get("classification") == "redirected_to_homepage"
        ):
            set_marketscreener_source_redirect(ticker)
        if ms and len(ms) > 0:
            return StepResult(
                step_name="fetch_consensus", status=Status.SUCCESS,
                source="marketscreener",
                message=f"Consensus from MarketScreener: {len(ms)} periods",
                record_count=len(ms), data=ms, elapsed_seconds=t.elapsed,
            )
        yest = _yahoo_est(ticker, company.currency)
        ms_reason = "source redirect" if (diagnostic and diagnostic.get("classification") == "redirected_to_homepage") else "unavailable"
        if yest:
            return StepResult(
                step_name="fetch_consensus", status=Status.PARTIAL,
                source="yahoo", fallback_used=True,
                message=f"MarketScreener {ms_reason} → Yahoo fallback: {len(yest)} estimate periods",
                record_count=len(yest), data=yest, elapsed_seconds=t.elapsed,
            )
        return StepResult(
            step_name="fetch_consensus", status=Status.FAILED,
            source="none", fallback_used=True,
            message=f"Consensus unavailable (MarketScreener {ms_reason}, Yahoo failed)",
            data=[], elapsed_seconds=t.elapsed,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. fetch_news
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_news(ticker: str, company: CompanyMaster) -> StepResult:
    with StepTimer() as t:
        try:
            recent_days = int(cfg()["news"].get("recent_days", 30))
        except Exception:
            recent_days = 30
        since = datetime.now(timezone.utc) - timedelta(days=recent_days)
        normalized, qa_data = _run_context_pipeline(
            company_name=company.company_name or "",
            is_bank=getattr(company, "is_bank", False),
            country=getattr(company, "country", "") or "",
            since=since,
        )
        valid_items: list[NewsItem] = [a.to_news_item() for a in normalized]
        candidate_count = qa_data.get("recent_context_candidate_count", 0)
        valid_count = len(valid_items)
        return StepResult(
            step_name="fetch_news", status=Status.SUCCESS, source="multiple",
            message=f"News: {valid_count} valid ({candidate_count} candidates)" if valid_count else f"No valid recent context ({candidate_count} candidates)",
            record_count=valid_count,
            data={
                "items": valid_items,
                "duplicate_screening_log": [],
                "recent_context_query_log": qa_data.get("recent_context_query_log", []),
                "recent_context_candidate_count": candidate_count,
                "recent_context_valid_count": valid_count,
                "recent_context_rejected_reasons": qa_data.get("recent_context_rejected_reasons", []),
                "candidate_valid_basic": qa_data.get("candidate_valid_basic", False),
                "candidate_has_date_before_enrichment": qa_data.get("candidate_has_date_before_enrichment", 0),
                "candidate_has_extracted_fact": qa_data.get("candidate_has_extracted_fact", 0),
                "final_article_valid_count": qa_data.get("final_article_valid_count", 0),
                "date_parse_attempted": qa_data.get("date_parse_attempted", 0),
                "date_parse_source": qa_data.get("date_parse_source", []),
                "date_parse_success": qa_data.get("date_parse_success", 0),
                "candidates_rejected_for_missing_date": qa_data.get("candidates_rejected_for_missing_date", 0),
                "candidates_recovered_after_article_fetch": qa_data.get("candidates_recovered_after_article_fetch", 0),
                "recent_context_enrichment_log": qa_data.get("recent_context_enrichment_log", []),
                "rejected_candidates_top_10": qa_data.get("rejected_candidates_top_10", []),
                "recent_context_articles_qa": qa_data.get("recent_context_articles_qa", []),
            },
            elapsed_seconds=t.elapsed,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5b. fetch_earnings_date (Yahoo calendar fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_earnings_date(ticker: str) -> StepResult:
    """Best-effort next earnings date from Yahoo calendar."""
    with StepTimer() as t:
        d = _yahoo_earnings_date(ticker)
    if not d:
        return StepResult(
            step_name="fetch_earnings_date",
            status=Status.PARTIAL,
            source="yahoo",
            message="No earnings date from Yahoo calendar",
            data=None,
            elapsed_seconds=t.elapsed,
        )
    return StepResult(
        step_name="fetch_earnings_date",
        status=Status.SUCCESS,
        source="yahoo",
        message=f"Earnings date (Yahoo): {d}",
        data={"next_earnings_date": d},
        elapsed_seconds=t.elapsed,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. reconcile
# ═══════════════════════════════════════════════════════════════════════════════

def _growth_series(periods: list[FinancialPeriod], field_name: str) -> list[dict]:
    s = sorted(periods, key=lambda p: p.period_label)
    out = []
    for i in range(1, len(s)):
        curr = getattr(s[i], field_name, None)
        prev = getattr(s[i - 1], field_name, None)
        if curr is not None and prev is not None and prev != 0:
            out.append({"period": s[i].period_label, "pct": round(((curr - prev) / abs(prev)) * 100, 2)})
    return out


def _avg_last_n(series: list[dict], n: int) -> float | None:
    tail = series[-n:] if len(series) >= n else series
    if not tail:
        return None
    return round(sum(g["pct"] for g in tail) / len(tail), 2)


def _cross_check(yahoo: list[FinancialPeriod], consensus: list[FinancialPeriod]) -> list[str]:
    thresholds = cfg()["thresholds"]
    warn_pct = thresholds["revenue_warn_pct"]
    alert_pct = thresholds["revenue_alert_pct"]
    warnings: list[str] = []
    yahoo_map = {fp.period_label: fp for fp in yahoo if fp.revenue is not None}
    for est in consensus:
        if est.revenue is None:
            continue
        actual = yahoo_map.get(est.period_label)
        if actual is None or actual.revenue is None:
            continue
        diff_pct = abs((est.revenue - actual.revenue) / actual.revenue) * 100
        if diff_pct >= alert_pct:
            warnings.append(f"ALERT {est.period_label}: consensus rev ({est.revenue:,.0f}) differs from actual ({actual.revenue:,.0f}) by {diff_pct:.1f}%")
        elif diff_pct >= warn_pct:
            warnings.append(f"WARN {est.period_label}: consensus rev deviation {diff_pct:.1f}%")
    return warnings


def reconcile(
    ticker: str,
    company: CompanyMaster,
    quarterly: list[FinancialPeriod],
    consensus: list[FinancialPeriod],
    quote=None,
) -> StepResult:
    with StepTimer() as t:
        warnings: list[str] = []
        xcheck = _cross_check(quarterly, consensus)
        warnings.extend(xcheck)
        # Fallback: if not enough quarterly points in this run, try persisted actuals.
        if len(quarterly) < 2:
            try:
                from src.services.store_actuals import latest_actuals
                cached = latest_actuals(ticker, limit=8)
                q_from_cache: list[FinancialPeriod] = []
                for r in reversed(cached):
                    q_from_cache.append(
                        FinancialPeriod(
                            period_label=r.get("period") or "",
                            period_type="quarterly",
                            source="actuals_db",
                            revenue=r.get("revenue"),
                            ebitda=r.get("ebitda"),
                            net_income=r.get("net_income"),
                            eps=r.get("eps"),
                            currency=company.currency,
                        )
                    )
                if len(q_from_cache) >= 2:
                    quarterly = q_from_cache
                    warnings.append("Using persisted actuals fallback for growth calculations")
            except Exception:
                pass
        if len(quarterly) < 2:
            return StepResult(
                step_name="reconcile", status=Status.PARTIAL, source="computed",
                message="Fewer than 2 quarters — limited derived metrics",
                data=DerivedMetrics(ticker=ticker, is_bank=company.is_bank, warnings=warnings),
                elapsed_seconds=t.elapsed,
            )
        rev_g = _growth_series(quarterly, "revenue")
        ni_g = _growth_series(quarterly, "net_income")
        # Valuation core fields
        pe_forward = None
        ev_ebitda = None
        pb_ratio = None
        div_yield_pct = None
        consensus_target = None
        upside_pct = None
        pe_vs_sector_pct = None
        ev_ebitda_vs_sector_pct = None
        if quote is not None:
            pe_forward = getattr(quote, "forward_pe", None)
            pb_ratio = None
            dy = getattr(quote, "dividend_yield", None)
            if isinstance(dy, (int, float)):
                div_yield_pct = round(dy * 100, 1)
        # Use consensus estimates to fill missing multiples
        cons_eps = next((c.eps for c in consensus if c.eps is not None), None)
        cons_ebitda = next((c.ebitda for c in consensus if c.ebitda is not None and c.ebitda > 0), None)
        if pe_forward is None and quote is not None and cons_eps and cons_eps > 0 and getattr(quote, "price", None):
            pe_forward = round(float(getattr(quote, "price")) / float(cons_eps), 1)
        if quote is not None and getattr(quote, "enterprise_value", None) and cons_ebitda:
            try:
                ev_ebitda = round(float(getattr(quote, "enterprise_value")) / float(cons_ebitda), 1)
            except Exception:
                ev_ebitda = None
        # Consensus target price is not carried in FinancialPeriod list; left for build payload / summary source.
        consensus_target = None
        if quote is not None and consensus_target and getattr(quote, "price", None):
            try:
                upside_pct = round((float(consensus_target) - float(getattr(quote, "price"))) / float(getattr(quote, "price")) * 100, 1)
            except Exception:
                upside_pct = None
        # Sector-relative comparison using optional peer group
        peer_group = getattr(company, "peer_group", None) or []
        if peer_group:
            try:
                from src.services.fetch_peers import fetch_peer_multiples
                peer = fetch_peer_multiples(peer_group)
                pe_med = peer.get("pe_sector_median")
                ev_med = peer.get("ev_ebitda_sector_median")
                if pe_forward and pe_med:
                    pe_vs_sector_pct = round(((pe_forward / pe_med) - 1) * 100, 1)
                if ev_ebitda and ev_med:
                    ev_ebitda_vs_sector_pct = round(((ev_ebitda / ev_med) - 1) * 100, 1)
            except Exception:
                pass
        derived = DerivedMetrics(
            ticker=ticker, is_bank=company.is_bank,
            quarterly_revenue_growth=rev_g, avg_4q_revenue_growth=_avg_last_n(rev_g, 4),
            quarterly_ni_growth=ni_g, avg_4q_ni_growth=_avg_last_n(ni_g, 4),
            pe_forward=pe_forward,
            ev_ebitda=ev_ebitda,
            pb_ratio=pb_ratio,
            div_yield_pct=div_yield_pct,
            consensus_target_price=consensus_target,
            upside_pct=upside_pct,
            pe_vs_sector_pct=pe_vs_sector_pct,
            ev_ebitda_vs_sector_pct=ev_ebitda_vs_sector_pct,
            warnings=warnings,
        )
        parts = []
        if derived.avg_4q_revenue_growth is not None:
            parts.append(f"avg 4Q rev growth {derived.avg_4q_revenue_growth}%")
        if xcheck:
            parts.append(f"{len(xcheck)} cross-source warnings")
        return StepResult(
            step_name="reconcile",
            status=Status.SUCCESS if not warnings else Status.PARTIAL,
            source="computed",
            message=f"Reconciled: {' | '.join(parts)}" if parts else "Reconciled (no warnings)",
            data=derived, elapsed_seconds=t.elapsed,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 7. qa_validate
# ═══════════════════════════════════════════════════════════════════════════════

def qa_validate(payload) -> StepResult:
    from src.services.qa_engine import run_qa

    with StepTimer() as t:
        try:
            memo_data, qa_audit = run_qa(payload)
            return StepResult(
                step_name="qa_validate", status=Status.SUCCESS, source="qa",
                message="QA complete; memo_data and audit ready",
                data={"memo_data": memo_data, "qa_audit": qa_audit},
                elapsed_seconds=t.elapsed,
            )
        except Exception as exc:
            return StepResult(
                step_name="qa_validate", status=Status.FAILED, source="qa",
                message="QA validation failed",
                error_detail=str(exc), elapsed_seconds=t.elapsed,
            )
