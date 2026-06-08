"""
Service: fetch_marketscreener_pages

Calls all MarketScreener page-specific providers per docs/DATA_SOURCE_AND_URL_REFERENCE.md:
slug discovery (when ID missing) → Summary → Consensus → Financials → Income → Dividend →
Valuation (multiples) → Calendar. Uses delays between requests. Returns aggregated data with
source lineage. Cache keys include ticker, isin, source, page_type (no generic keys).
"""

from __future__ import annotations
from copy import deepcopy

from src.models.company import CompanyMaster
from src.models.step_result import Status, StepResult, StepTimer
from src.providers import marketscreener_pages as ms
from src.services.entity_resolution import ensure_marketscreener_cached, get_effective_marketscreener_slug
from src.storage.db import load_company

STEP = "fetch_marketscreener_pages"


def _base_url(slug: str) -> str:
    return f"https://www.marketscreener.com/quote/stock/{slug}"


def _cache_key_prefix(ticker: str, isin: str, slug: str) -> str:
    """Hardened cache key: ticker, isin, source, page_type. No generic header.json/consensus.json."""
    t = (ticker or "").replace(".", "_").strip() or "unknown"
    i = (isin or "noisin").strip() or "noisin"
    s = (slug or "").strip() or "unknown"
    return f"ms_{t}_{i}_{s}"


def run(ticker: str, company: CompanyMaster) -> StepResult:
    with StepTimer() as t:
        # Use latest DB status (fetch_consensus may have set source_redirect this run)
        row = load_company(ticker)
        ms_status = (row.get("marketscreener_status") if row else None) or (getattr(company, "marketscreener_status", None) or "").strip().lower()
        if ms_status == "source_redirect":
            return StepResult(
                step_name=STEP,
                status=Status.SKIPPED,
                source="marketscreener",
                message="MarketScreener skipped (source redirect); consensus/appendices from other sources",
                elapsed_seconds=t.elapsed,
            )
        slug = get_effective_marketscreener_slug(company.model_dump())
        if not slug and company.isin:
            updated = ensure_marketscreener_cached(ticker, company.model_dump())
            if updated:
                slug = get_effective_marketscreener_slug(updated)
        if not slug:
            slug = ms.resolve_slug_from_search(
                ticker, company_name=getattr(company, "company_name", "") or ""
            )
            # Guardrail: validate weak search fallback before scraping multiple pages.
            if slug:
                try:
                    from src.services.entity_resolution import validate_candidate_page
                    from src.storage.db import (
                        reject_marketscreener_candidate as _reject,
                        update_company_marketscreener as _persist_slug,
                    )
                    vr = validate_candidate_page(
                        company.model_dump(),
                        slug,
                        _base_url(slug).rstrip("/") + "/",
                        cache_name=f"validate_search_pages_{ticker.replace('.', '_')}",
                    )
                    if not vr.valid:
                        _reject(ticker, reason=vr.rejection_reason or "search_candidate_validation_failed", status="needs_review")
                        slug = ""
                    else:
                        # Persist a successfully-validated search slug so
                        # subsequent runs skip the search+validate round-trip
                        # entirely. Without this, every run for tickers whose
                        # ISIN-based candidate path fails (e.g. some non-US
                        # exchanges) re-pays 2 MS requests per ticker — and
                        # those extra requests are exactly what trips MS's
                        # rate-limiter on heavy back-to-back use, which is
                        # what regressed 1010.SR / 2020.SR on 2026-05-09.
                        try:
                            from datetime import datetime as _dt, timezone as _tz
                            _persist_slug(
                                ticker=ticker,
                                marketscreener_company_url=_base_url(slug).rstrip("/") + "/",
                                marketscreener_symbol=ticker,
                                marketscreener_status="ok",
                                last_verified=_dt.now(_tz.utc).isoformat(),
                                marketscreener_id=slug,
                            )
                        except Exception:
                            # Best-effort: a DB write failure shouldn't
                            # abort the rest of the run.
                            pass
                except Exception:
                    pass
            if not slug:
                return StepResult(
                    step_name=STEP,
                    status=Status.SKIPPED,
                    source="marketscreener",
                    message="No MarketScreener ID and slug discovery failed — all page fetchers skipped",
                    elapsed_seconds=t.elapsed,
                )
        base = _base_url(slug)
        cache_prefix = _cache_key_prefix(ticker, getattr(company, "isin", None) or "", slug)
        company_name = getattr(company, "company_name", None) or getattr(company, "company_name_long", None) or ""

        out: dict = {
            "consensus_summary": None,
            "ms_summary": None,
            "ms_annual_forecasts": None,
            "ms_quarterly_forecasts": None,
            "ms_eps_dividend_forecasts": None,
            "ms_income_statement_actuals": None,
            "ms_valuation_multiples": None,
            "ms_calendar_events": None,
            "ms_quarterly_results_table": None,
            "ms_ratings": None,
            "ms_sector_peers": None,
            "ms_price_performance": None,
            "ms_analyst_recommendations": None,
            "ms_finances_sections": None,
            "ms_lineage": {
                "source_ticker": ticker,
                "source_company_name": company_name,
                "source_url": base.rstrip("/") + "/",
                "final_url": base.rstrip("/") + "/",
                "source_page_type": "multi",
            },
        }
        errors: list[str] = []
        record_count = 0

        # 1. Summary (/{SLUG}/) — consensus box + valuation snapshot (no delay before first request)
        try:
            payload_summary, status_s = ms.fetch_summary_page(base, cache_key_prefix=cache_prefix)
            out["ms_summary"] = deepcopy(payload_summary) if payload_summary else None
            if status_s.record_count:
                record_count += status_s.record_count
            if status_s.errors:
                errors.extend(status_s.errors)
        except Exception as e:
            errors.append(f"summary_page: {e}")

        # 2. Consensus (/consensus/)
        try:
            ms._delay_between_requests()
            payload_e, status_e = ms.fetch_consensus_summary(base, cache_key_prefix=cache_prefix)
            out["consensus_summary"] = deepcopy(payload_e) if payload_e else None
            if status_e.record_count:
                record_count += status_e.record_count
            if status_e.errors:
                errors.extend(status_e.errors)
        except Exception as e:
            errors.append(f"consensus_summary: {e}")

        # If consensus page failed but summary has consensus data, use summary as fallback
        if out["consensus_summary"] is None and out["ms_summary"]:
            s = out["ms_summary"]
            if s.get("consensus_rating") or s.get("average_target_price") is not None:
                out["consensus_summary"] = {
                    "consensus_rating": s.get("consensus_rating"),
                    "analyst_count": s.get("analyst_count"),
                    "last_close_price": s.get("last_close_price"),
                    "price_currency": s.get("price_currency"),
                    "average_target_price": s.get("average_target_price"),
                    "high_target_price": s.get("high_target_price"),
                    "low_target_price": s.get("low_target_price"),
                    "upside_to_average_target_pct": s.get("spread_pct"),
                    "downside_to_low_target_pct": None,
                    "source_page": s.get("source_page"),
                    "source_type": "summary_page_fallback",
                }

        # 3. Financials (/finances/) — annual + quarterly
        try:
            ms._delay_between_requests()
            payload_a, status_a = ms.fetch_financial_forecast_series(base, cache_key_prefix=cache_prefix)
            out["ms_annual_forecasts"] = deepcopy(payload_a) if payload_a else None
            out["ms_quarterly_forecasts"] = deepcopy(payload_a) if payload_a else None
            if status_a.record_count:
                record_count += status_a.record_count
            if status_a.errors:
                errors.extend(status_a.errors)
        except Exception as e:
            errors.append(f"financial_forecast_series: {e}")

        # 4. Section detection (/finances/)
        try:
            ms._delay_between_requests()
            payload_b, _ = ms.detect_finances_page_sections(base, cache_key_prefix=cache_prefix)
            out["ms_finances_sections"] = deepcopy(payload_b) if payload_b else None
        except Exception as e:
            errors.append(f"detect_finances_sections: {e}")

        # 5. Income statement actuals (/finances-income-statement/)
        try:
            ms._delay_between_requests()
            payload_c, status_c = ms.fetch_income_statement_actuals(base, cache_key_prefix=cache_prefix)
            out["ms_income_statement_actuals"] = deepcopy(payload_c) if payload_c else None
            if status_c.record_count:
                record_count += status_c.record_count
            if status_c.errors:
                errors.extend(status_c.errors)
        except Exception as e:
            errors.append(f"income_statement_actuals: {e}")

        # 6. Dividend & EPS (/valuation-dividend/)
        try:
            ms._delay_between_requests()
            payload_d, status_d = ms.fetch_dividend_eps_page(base, cache_key_prefix=cache_prefix)
            out["ms_eps_dividend_forecasts"] = deepcopy(payload_d) if payload_d else None
            if status_d.record_count:
                record_count += status_d.record_count
            if status_d.errors:
                errors.extend(status_d.errors)
        except Exception as e:
            errors.append(f"dividend_eps_page: {e}")

        # 7. Valuation multiples (/valuation/)
        try:
            ms._delay_between_requests()
            payload_val, status_val = ms.fetch_valuation_multiples(base, cache_key_prefix=cache_prefix)
            out["ms_valuation_multiples"] = deepcopy(payload_val) if payload_val else None
            if status_val.record_count:
                record_count += status_val.record_count
            if status_val.errors:
                errors.extend(status_val.errors)
        except Exception as e:
            errors.append(f"valuation_multiples: {e}")

        # 8. Calendar (/calendar/) — includes Quarterly results table (metrics-dict shape)
        try:
            ms._delay_between_requests()
            payload_f, status_f = ms.fetch_calendar_events(base, cache_key_prefix=cache_prefix)
            out["ms_calendar_events"] = deepcopy(payload_f) if payload_f else None
            out["ms_quarterly_results_table"] = deepcopy(payload_f.get("quarterly_results_table")) if (payload_f and payload_f.get("quarterly_results_table")) else None
            if payload_f.get("next_expected_earnings_date"):
                record_count += 1
            if status_f.errors:
                errors.extend(status_f.errors)
        except Exception as e:
            errors.append(f"calendar_events: {e}")

        # 9. Ratings (/ratings/) — strengths/weaknesses + composite ratings + peer ESG
        try:
            ms._delay_between_requests()
            payload_r, status_r = ms.fetch_ratings_page(base, cache_key_prefix=cache_prefix)
            out["ms_ratings"] = deepcopy(payload_r) if payload_r else None
            if status_r.record_count:
                record_count += status_r.record_count
            if status_r.errors:
                errors.extend(status_r.errors)
        except Exception as e:
            errors.append(f"ratings_page: {e}")

        # 10. Sector peers (/sector/) — peer comparison table
        try:
            ms._delay_between_requests()
            payload_sp, status_sp = ms.fetch_sector_peers(base, cache_key_prefix=cache_prefix)
            out["ms_sector_peers"] = deepcopy(payload_sp) if payload_sp else None
            if status_sp.record_count:
                record_count += status_sp.record_count
            if status_sp.errors:
                errors.extend(status_sp.errors)
        except Exception as e:
            errors.append(f"sector_peers: {e}")

        # 11. Price performance (/{SLUG}/) — re-uses cached summary HTML
        try:
            payload_pp, status_pp = ms.fetch_price_performance(base, cache_key_prefix=cache_prefix)
            out["ms_price_performance"] = deepcopy(payload_pp) if payload_pp else None
            if status_pp.record_count:
                record_count += status_pp.record_count
            if status_pp.errors:
                errors.extend(status_pp.errors)
        except Exception as e:
            errors.append(f"price_performance: {e}")

        # 12. Analyst recommendations (/consensus/) — re-uses cached consensus HTML
        try:
            payload_ar, status_ar = ms.fetch_analyst_recommendations(base, cache_key_prefix=cache_prefix)
            out["ms_analyst_recommendations"] = deepcopy(payload_ar) if payload_ar else None
            if status_ar.record_count:
                record_count += status_ar.record_count
            if status_ar.errors:
                errors.extend(status_ar.errors)
        except Exception as e:
            errors.append(f"analyst_recommendations: {e}")

        status = Status.FAILED if errors and not any([out["consensus_summary"], out["ms_annual_forecasts"], out["ms_eps_dividend_forecasts"]]) else (Status.PARTIAL if errors else Status.SUCCESS)
        return StepResult(
            step_name=STEP,
            status=status,
            source="marketscreener",
            message=f"Page-specific fetch: summary, consensus, finances, valuation, calendar, ratings, sector, perf, recommendations" + (" with errors" if errors else ""),
            error_detail="; ".join(errors[:5]) if errors else None,
            record_count=record_count or None,
            data=out,
            elapsed_seconds=t.elapsed,
        )
