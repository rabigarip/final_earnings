"""Pipeline orchestrator for earnings preview mode.

Steps 1-2 are CRITICAL (halt on failure). Steps 3+ are RESILIENT (log and continue).
Outputs: one earnings preview .pptx per ticker.
"""

from __future__ import annotations
import gc
import os
import logging
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from src.models.step_result import Status, StepResult
from src.services import (
    resolve_mapping, fetch_marketscreener_pages,
    summarize_news, build_report_payload, generate_report,
)
from src.services.pipeline_steps import (
    validate_ticker, fetch_quote, fetch_financials,
    fetch_consensus, fetch_news, fetch_earnings_date, reconcile, qa_validate,
)
from src.services.build_report_payload import get_memo_computed_for_preview
from src.services.ms_payload_fingerprint import save_fingerprint as save_ms_fingerprint
from src.services.report_readiness import run_readiness_check
from src.storage.db import save_run

logger = logging.getLogger(__name__)

# Build serialization. A single deck build peaks at ~280-400MB (matplotlib +
# pandas + yfinance + snapshot parsing). On Render's 512MB instance two builds
# running at once (FastAPI runs sync routes in a threadpool, so concurrent
# /api/preview requests build in parallel) blow past the limit and trigger an
# OOM restart. This semaphore forces builds to run one-at-a-time; a second
# concurrent request queues until the first finishes rather than OOM-ing the
# whole instance. Override with EARNINGS_MAX_CONCURRENT_BUILDS if the instance
# is later upsized.
_MAX_CONCURRENT_BUILDS = max(1, int(os.environ.get("EARNINGS_MAX_CONCURRENT_BUILDS", "1")))
_BUILD_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT_BUILDS)

# Hard backstop: refuse to START a build when the process is already near the
# instance memory limit, rather than letting the build tip it over and trigger
# an OOM restart that drops every in-flight request. The API maps this to a 503
# "busy, retry shortly". During normal serialized operation RSS sits ~280MB so
# this never trips; it only fires if memory is abnormally high (a leak we didn't
# foresee, or one pathological build). It self-heals: once the in-flight build
# finishes and frees memory, the next request goes through. Tune via
# EARNINGS_RSS_LIMIT_MB; 0 disables the guard.
_RSS_LIMIT_MB = float(os.environ.get("EARNINGS_RSS_LIMIT_MB", "460"))


class BuildRejectedError(RuntimeError):
    """Raised when a build is refused due to memory pressure (→ HTTP 503)."""


def _process_rss_mb() -> float | None:
    """Current resident-set size in MB, or None if it can't be read.

    Reads /proc/self/statm (Linux/Render) with no extra dependency. Returns
    None off-Linux (local macOS dev) so the guard is a no-op there."""
    try:
        with open("/proc/self/statm") as fh:
            rss_pages = int(fh.read().split()[1])
        return rss_pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except Exception:
        return None


def _under_memory_pressure() -> bool:
    if _RSS_LIMIT_MB <= 0:
        return False
    rss = _process_rss_mb()
    return rss is not None and rss >= _RSS_LIMIT_MB


def run_preview(ticker: str, *, skip_llm: bool = False) -> tuple[str, list[StepResult]]:
    """Returns (run_id, step_results).

    Serialized via _BUILD_SEMAPHORE so concurrent requests don't stack their
    peak memory in one process (see semaphore note above). Raises
    BuildRejectedError when the process is already near the memory ceiling."""
    if _under_memory_pressure():
        raise BuildRejectedError(
            f"Memory pressure high (RSS {_process_rss_mb():.0f}MB ≥ "
            f"{_RSS_LIMIT_MB:.0f}MB limit); build deferred. Retry shortly.")
    with _BUILD_SEMAPHORE:
        try:
            return _run_preview_inner(ticker, skip_llm=skip_llm)
        finally:
            # Sweep any matplotlib figures that escaped a chart error-path.
            # pyplot holds every figure in a global registry until close(),
            # so gc alone can't reclaim a figure that skipped its plt.close
            # — close('all') frees them all. Guard on sys.modules so we never
            # force-import matplotlib for a build that rendered no chart.
            import sys as _sys
            _plt = _sys.modules.get("matplotlib.pyplot")
            if _plt is not None:
                try:
                    _plt.close("all")
                except Exception:
                    pass
            # Release the build's transient allocations (chart buffers, parsed
            # snapshots, dataframes) promptly so the next queued build starts
            # from a low baseline instead of a ratcheted-up RSS.
            gc.collect()


def _run_preview_inner(ticker: str, *, skip_llm: bool = False) -> tuple[str, list[StepResult]]:
    run_id = uuid.uuid4().hex[:8]
    t0 = datetime.now(timezone.utc)
    results: list[StepResult] = []

    if not (ticker or "").strip():
        r = StepResult(step_name="validate_ticker", status=Status.FAILED, source="local",
                       message="Empty ticker")
        results.append(r)
        return run_id, results

    _banner(ticker, run_id, t0)

    # ── 1. Validate ticker (CRITICAL) ─────────────────────────
    r = validate_ticker(ticker)
    _collect(r, results)
    if r.status == Status.FAILED:
        _finish(run_id, ticker, t0, results)
        return run_id, results

    # ── 2. Resolve company mapping (CRITICAL) ─────────────────
    r = resolve_mapping.run(ticker)
    _collect(r, results)
    if r.status == Status.FAILED:
        _finish(run_id, ticker, t0, results)
        return run_id, results
    company = r.data

    # ── 2b. Bloomberg manual export (OPT-IN only) ─────────────
    # Trust model: Bloomberg data is silent-override-dangerous, so we
    # never auto-load just because a .xlsx exists on disk. The analyst
    # must explicitly enable it via:
    #   data/bloomberg/<TICKER>.manifest.json with {"enabled": true, ...}
    # which is written by the dashboard's "Use Bloomberg upload" toggle.
    # When disabled, the deck uses free-source data (Investing / MS /
    # Yahoo / IMF) and the provenance.xlsx documents exactly that.
    bloomberg_bundle_dict: dict | None = None
    try:
        from pathlib import Path as _P
        import json as _json
        manifest_path = _P("data/bloomberg") / f"{ticker}.manifest.json"
        bbg_enabled = False
        if manifest_path.is_file():
            try:
                _manifest = _json.loads(manifest_path.read_text())
                bbg_enabled = bool(_manifest.get("enabled"))
            except (OSError, _json.JSONDecodeError):
                bbg_enabled = False
        if bbg_enabled:
            from src.services.bloomberg_parser import load_bloomberg_bundle
            from dataclasses import asdict as _dc_asdict
            _bbg = load_bloomberg_bundle(ticker)
            if _bbg is not None:
                bloomberg_bundle_dict = _dc_asdict(_bbg)
                logger.info(
                    "[bloomberg] %s bundle loaded (analyst-enabled): "
                    "%d quarters, %d annuals",
                    ticker, len(_bbg.consensus_quarterly), len(_bbg.annuals),
                )
        else:
            logger.info(
                "[bloomberg] %s manifest absent or disabled — using "
                "free-source data only", ticker)
    except Exception as exc:
        logger.warning("[bloomberg] manifest check failed for %s: %s", ticker, exc)

    # ── 3. Fetch quote ────────────────────────────────────────
    r = fetch_quote(ticker)
    _collect(r, results)
    quote = r.data if r.status != Status.FAILED else None

    # ── 4. Fetch financials ───────────────────────────────────
    r = fetch_financials(ticker, company)
    _collect(r, results)
    quarterly = r.data.get("quarterly", []) if r.data else []
    annual    = r.data.get("annual", [])    if r.data else []
    # Persist latest quarterly actual as fallback history for future runs.
    try:
        if quarterly:
            from src.services.store_actuals import upsert_actuals
            from src.utils.periods import latest_period
            latest_q = latest_period(quarterly)
            ebitda_margin = (latest_q.ebitda / latest_q.revenue * 100) if (latest_q.ebitda is not None and latest_q.revenue) else None
            upsert_actuals(
                ticker=ticker,
                period=latest_q.period_label,
                revenue=latest_q.revenue,
                net_income=latest_q.net_income,
                eps=latest_q.eps,
                ebitda=latest_q.ebitda,
                ebitda_margin=ebitda_margin,
                reported_date=None,
            )
    except Exception:
        pass

    # ── 5. Fetch consensus ────────────────────────────────────
    r = fetch_consensus(ticker, company)
    _collect(r, results)
    consensus = r.data if isinstance(r.data, list) else []

    # ── 5b. Fetch MarketScreener pages ────────────────────────
    r = fetch_marketscreener_pages.run(ticker, company)
    _collect(r, results)
    ms_blocks = deepcopy(r.data) if isinstance(r.data, dict) else {}

    # ── 5c. Yahoo earnings date fallback (helps when MS /calendar/ blocked) ──
    r = fetch_earnings_date(ticker)
    _collect(r, results)
    yahoo_earnings_date = None
    try:
        yahoo_earnings_date = (r.data or {}).get("next_earnings_date") if isinstance(r.data, dict) else None
    except Exception:
        yahoo_earnings_date = None
    if yahoo_earnings_date and (not (ms_blocks.get("ms_calendar_events") or {}).get("next_expected_earnings_date")):
        # Store as memo-only hint; do not pretend this is MarketScreener data.
        ms_blocks["yahoo_earnings_date"] = yahoo_earnings_date

    # ── 6. Fetch news ─────────────────────────────────────────
    r = fetch_news(ticker, company)
    _collect(r, results)
    news_data = r.data if isinstance(r.data, dict) else {}
    news_items = news_data.get("items") or (r.data if isinstance(r.data, list) else [])

    # ── 7. Reconcile + derived metrics ────────────────────────
    r = reconcile(ticker, company, quarterly, consensus, quote=quote)
    _collect(r, results)
    derived = r.data if r.status != Status.FAILED else None

    # ── 7b. LIVE QUOTE REFRESH (overwrite volatile fields) ────
    # After the reconciler picks canonical winners, fetch a single live
    # yfinance quote for the intraday-volatile fields (price, market
    # cap, 52w range) and overwrite the canonical-store cells. This
    # closes the "Investing.com tab shows 0.427 but our deck shows 0.41"
    # trust gap — the deck now matches what the analyst sees on a live
    # quote page.
    #
    # Fails gracefully — on Yahoo rate-limit / missing-ticker / network
    # error, the snapshot-sourced values remain authoritative. The
    # provenance writer records whether live data was applied, and the
    # slide-1 footer banner reflects which freshness tier each field
    # ended up in.
    live_quote_record: dict | None = None
    try:
        from src.services.live_quote import fetch_live_quote, merge_into_canonical
        from src.services.ticker_registry import get_ticker_info as _gti
        _tinfo = _gti(ticker)
        _yf_support = (_tinfo.get("providers") or {}).get("yfinance", "supported")
        if _yf_support != "unsupported":
            lq = fetch_live_quote(ticker)
            if lq.ok:
                merge_result = merge_into_canonical(ticker, lq)
                live_quote_record = {**lq.as_dict(), **merge_result}
                logger.info(
                    "[live_quote] %s live-refreshed: price=%s mcap=%s fields=%s",
                    ticker, lq.price, lq.market_cap,
                    merge_result.get("fields"))
            else:
                logger.info("[live_quote] %s skipped: %s",
                            ticker, "; ".join(lq.warnings))
        else:
            logger.info("[live_quote] %s skipped — yfinance unsupported per registry",
                        ticker)
    except Exception as exc:
        logger.warning("[live_quote] %s exception: %s", ticker, exc)

    # ── 8. Summarize news (LLM) ──────────────────────────────
    # Deferred: LLM should be the last thing produced before rendering.
    # We do the slide text via `draft_pptx_sections` after QA instead.
    r = StepResult(
        step_name="summarize_news",
        status=Status.SKIPPED,
        source="gemini",
        message="Deferred to end (PPTX uses draft_pptx_sections)",
    )
    _collect(r, results)
    summary = None

    # ── 9. Build report payload ───────────────────────────────
    r = build_report_payload.run(
        run_id=run_id, company=company, quote=quote,
        quarterly=quarterly, annual=annual, consensus=consensus,
        consensus_summary=ms_blocks.get("consensus_summary"),
        ms_lineage=ms_blocks.get("ms_lineage"),
        ms_summary=ms_blocks.get("ms_summary"),
        ms_annual_forecasts=ms_blocks.get("ms_annual_forecasts"),
        ms_quarterly_forecasts=ms_blocks.get("ms_quarterly_forecasts"),
        ms_eps_dividend_forecasts=ms_blocks.get("ms_eps_dividend_forecasts"),
        ms_income_statement_actuals=ms_blocks.get("ms_income_statement_actuals"),
        ms_valuation_multiples=ms_blocks.get("ms_valuation_multiples"),
        ms_calendar_events=ms_blocks.get("ms_calendar_events"),
        ms_quarterly_results_table=ms_blocks.get("ms_quarterly_results_table"),
        ms_ratings=ms_blocks.get("ms_ratings"),
        ms_sector_peers=ms_blocks.get("ms_sector_peers"),
        ms_price_performance=ms_blocks.get("ms_price_performance"),
        ms_analyst_recommendations=ms_blocks.get("ms_analyst_recommendations"),
        derived=derived, news_items=news_items, news_summary=summary,
        # Memo-only fallback (Yahoo calendar)
        yahoo_earnings_date=ms_blocks.get("yahoo_earnings_date"),
        duplicate_screening_log=news_data.get("duplicate_screening_log") or [],
        step_log=[s.to_log_dict() for s in results],
        recent_context_query_log=news_data.get("recent_context_query_log") or [],
        recent_context_candidate_count=news_data.get("recent_context_candidate_count") or 0,
        recent_context_valid_count=news_data.get("recent_context_valid_count") or 0,
        recent_context_rejected_reasons=news_data.get("recent_context_rejected_reasons") or [],
        candidate_valid_basic=news_data.get("candidate_valid_basic", False),
        candidate_has_date_before_enrichment=news_data.get("candidate_has_date_before_enrichment", 0),
        candidate_has_extracted_fact=news_data.get("candidate_has_extracted_fact", 0),
        final_article_valid_count=news_data.get("final_article_valid_count", 0),
        date_parse_attempted=news_data.get("date_parse_attempted", 0),
        date_parse_source=news_data.get("date_parse_source") or [],
        date_parse_success=news_data.get("date_parse_success", 0),
        candidates_rejected_for_missing_date=news_data.get("candidates_rejected_for_missing_date", 0),
        candidates_recovered_after_article_fetch=news_data.get("candidates_recovered_after_article_fetch", 0),
        recent_context_enrichment_log=news_data.get("recent_context_enrichment_log") or [],
        rejected_candidates_top_10=news_data.get("rejected_candidates_top_10") or [],
        recent_context_articles_qa=news_data.get("recent_context_articles_qa") or [],
        bloomberg_bundle=bloomberg_bundle_dict,
    )
    _collect(r, results)
    if r.status == Status.FAILED:
        _finish(run_id, ticker, t0, results)
        return run_id, results
    payload = r.data

    # Persist MS fingerprint for cross-company contamination checks
    try:
        fp = getattr(payload, "ms_payload_fingerprint", "") or ""
        if fp and not getattr(payload, "cross_company_contamination_detected", True):
            save_ms_fingerprint(ticker, run_id, fp)
    except Exception as exc:
        logger.warning("Could not save MS fingerprint: %s", exc)

    # ── 10. QA validate ───────────────────────────────────────
    r = qa_validate(payload)
    _collect(r, results)
    memo_data = None
    qa_audit = None
    if r.status == Status.SUCCESS and isinstance(r.data, dict):
        memo_data = r.data.get("memo_data")
        qa_audit = r.data.get("qa_audit")
    # Attach the live-quote record so the renderer's freshness banner
    # and the provenance writer both know whether live data was applied.
    if memo_data is not None and live_quote_record is not None:
        memo_data["live_quote_record"] = live_quote_record

    # ── 10b. Report readiness (fail loud before PPTX) ─────────
    r = run_readiness_check(payload, results)
    _collect(r, results)
    if r.status == Status.FAILED:
        _finish(run_id, ticker, t0, results)
        return run_id, results

    # ── 11. Draft slide text (LLM LAST) ─────────────────────
    # We draft PPTX sections (thesis / watch / catalysts / risks) via Gemini when an API key is present.
    # `skip_llm` primarily affects slower, upstream summarization; drafting is small and makes slides higher quality.
    if memo_data and os.environ.get("GEMINI_API_KEY"):
        try:
            from src.services.draft_pptx_sections import run as draft_sections
            headlines = []
            for n in (getattr(payload, "news_items", None) or [])[:8]:
                h = (getattr(n, "headline", None) or "").strip()
                if h:
                    headlines.append(h)
            sector = f"{getattr(payload.company, 'sector', '')} / {getattr(payload.company, 'industry', '')}".strip(" /")
            quarter = (memo_data.get("preview_short") or getattr(payload, "memo_computed", {}).get("preview_quarter_short") or "").strip()
            # Surface payload.memo_computed inside memo_data so the LLM prompt
            # can read direction signals (surprise history, YoY direction,
            # spread sign) and ground catalysts/risks in real facts. Pass-by-
            # value so the QA artifact downstream isn't mutated.
            memo_data_for_prompt = dict(memo_data)
            memo_data_for_prompt["memo_computed"] = dict(getattr(payload, "memo_computed", {}) or {})
            rr = draft_sections(
                company_name=getattr(payload.company, "company_name", "") or "",
                ticker=getattr(payload.company, "ticker", "") or "",
                sector=sector,
                quarter=quarter,
                memo_data=memo_data_for_prompt,
                news_headlines=headlines,
            )
            _collect(rr, results)
            if rr.status in (Status.SUCCESS, Status.PARTIAL) and isinstance(rr.data, dict):
                memo_data["pptx_sections"] = rr.data
        except Exception:
            pass

    # ── 11b. Automated data validation ─────────────────────────
    data_warnings: list[str] = []
    try:
        from src.services.data_validation import validate_report_data
        data_warnings = validate_report_data(payload, memo_data=memo_data)
    except Exception:
        pass

    # ── 12. Generate report (.pptx) ──────────────────────────
    r = generate_report.run(payload, memo_data=memo_data, qa_audit=qa_audit, data_warnings=data_warnings)
    _collect(r, results)

    _finish(run_id, ticker, t0, results)
    return run_id, results


# ── Helpers ───────────────────────────────────────────────────

def _collect(r: StepResult, results: list[StepResult]) -> None:
    r.print_box()
    results.append(r)


def _overall(results: list[StepResult]) -> str:
    statuses = {r.status for r in results}
    if Status.FAILED in statuses or Status.PARTIAL in statuses:
        return "partial"
    return "success"


def _banner(ticker: str, run_id: str, t0: datetime) -> None:
    print(f"\n{'█' * 66}")
    print(f"  EARNINGS PREVIEW PIPELINE")
    print(f"  Ticker:   {ticker}")
    print(f"  Run ID:   {run_id}")
    print(f"  Started:  {t0:%Y-%m-%d %H:%M:%S} UTC")
    print(f"{'█' * 66}")


def _finish(run_id: str, ticker: str, t0: datetime,
            results: list[StepResult]) -> None:
    from pathlib import Path
    t1 = datetime.now(timezone.utc)
    overall = _overall(results)
    failed = sum(1 for r in results if r.status == Status.FAILED)
    elapsed = (t1 - t0).total_seconds()

    memo_path = None
    for r in results:
        if r.step_name == "generate_report" and r.status == Status.SUCCESS and r.data:
            memo_path = Path(str(r.data)).name
            break

    try:
        save_run(run_id, ticker, "preview",
                 t0.isoformat(), t1.isoformat(),
                 overall, [r.to_log_dict() for r in results], memo_path=memo_path)
    except Exception as exc:
        # Was silently swallowed; that handed the API a run_id the DB didn't
        # have, so downloads 404'd with "Report not found". Now surface as a
        # step so the API can return the real error to the caller.
        logger.exception("Could not save run %s to DB", run_id)
        results.append(StepResult(
            step_name="save_run",
            status=Status.FAILED,
            source="sqlite",
            message="DB save failed; run_id not persisted",
            error_detail=f"{type(exc).__name__}: {exc}",
        ))

    print(f"\n{'█' * 66}")
    print(f"  PIPELINE COMPLETE — {overall.upper()}")
    print(f"  Steps: {len(results)}  |  Failed: {failed}  |  {elapsed:.1f}s total")
    print(f"{'█' * 66}\n")
