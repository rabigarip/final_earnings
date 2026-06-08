"""
Fail-loud gate before PPTX: aggregate upstream step failures and enforce a
minimum data contract so we do not ship a deck that is mostly placeholders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.step_result import Status, StepResult, StepTimer

if TYPE_CHECKING:
    from src.models.report_payload import ReportPayload

STEP = "report_readiness"


def _readiness_permissive() -> bool:
    """When True, skip data-sparsity checks so Yahoo-only (or thin MS) previews can complete."""
    import os

    env = (os.environ.get("REPORT_READINESS_MODE") or "").strip().lower()
    if env == "permissive":
        return True
    if env == "strict":
        return False
    try:
        from src.config import cfg

        m = (cfg().get("report", {}) or {}).get("readiness_mode", "strict")
        return (m or "strict").strip().lower() == "permissive"
    except Exception:
        return False


def _block_on_currency_mismatch() -> bool:
    """Opt-in gate: refuse render when company currency != MS consensus currency.

    Default off to preserve current behavior (report still renders with a Data
    Quality warning). Enable via env REPORT_BLOCK_CURRENCY_MISMATCH=1 or
    config.report.block_on_currency_mismatch=true.
    """
    import os

    env = (os.environ.get("REPORT_BLOCK_CURRENCY_MISMATCH") or "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    try:
        from src.config import cfg

        return bool((cfg().get("report", {}) or {}).get("block_on_currency_mismatch", False))
    except Exception:
        return False


def _currency_mismatch_reason(payload: ReportPayload) -> str | None:
    """Return a human-readable reason if company/consensus currencies diverge, else None."""
    c = getattr(payload, "company", None)
    cs = getattr(payload, "consensus_summary", None) or {}
    company_ccy = (getattr(c, "currency", "") or "").upper()
    ms_ccy = (cs.get("price_currency") or "").upper()
    if company_ccy and ms_ccy and company_ccy != ms_ccy:
        return (
            f"Currency mismatch: company is in {company_ccy} but consensus is in {ms_ccy}. "
            "Upside, target price, and consensus numbers would be on an inconsistent basis. "
            "Set REPORT_BLOCK_CURRENCY_MISMATCH=0 to override, or supply an FX conversion."
        )
    return None


# If any of these steps failed, the run cannot produce a trustworthy preview.
_BLOCKING_STEPS = frozenset(
    {
        "validate_ticker",
        "resolve_mapping",
        "fetch_quote",
        "build_report_payload",
        "qa_validate",
    }
)

_STEP_LABELS: dict[str, str] = {
    "validate_ticker": "Ticker validation (Yahoo)",
    "resolve_mapping": "Company mapping (master DB)",
    "fetch_quote": "Yahoo Finance — quote",
    "fetch_financials": "Yahoo Finance — financials",
    "fetch_consensus": "Consensus fetch",
    "fetch_marketscreener_pages": "MarketScreener — pages",
    "fetch_earnings_date": "Yahoo — earnings calendar",
    "fetch_news": "News",
    "reconcile": "Reconciliation / derived metrics",
    "build_report_payload": "Report payload",
    "qa_validate": "QA / memo validation",
    "summarize_news": "News summarization",
    "draft_pptx_sections": "Gemini — slide drafting",
    "generate_report": "PPTX generation",
}


def _has_quote(payload: ReportPayload) -> bool:
    q = payload.quote
    if not q:
        return False
    return getattr(q, "price", None) is not None or getattr(q, "market_cap", None) is not None


def _has_yahoo_financials(payload: ReportPayload) -> bool:
    return bool(payload.quarterly_actuals or payload.annual_actuals)


def _has_ms_forecast_data(payload: ReportPayload) -> bool:
    if payload.consensus_summary:
        return True
    ms_af = payload.ms_annual_forecasts or {}
    if (ms_af.get("annual") or {}).get("periods"):
        return True
    ms_qf = payload.ms_quarterly_forecasts or {}
    if (ms_qf.get("quarterly") or {}).get("periods"):
        return True
    vm = payload.ms_valuation_multiples or {}
    if vm.get("periods"):
        return True
    m = payload.memo_computed or {}
    if m.get("next_quarter_consensus_revenue") is not None or m.get("next_quarter_consensus_eps") is not None:
        return True
    ms_cal = payload.ms_calendar_events or {}
    if ms_cal.get("next_expected_earnings_date") or (ms_cal.get("quarterly_results") or {}).get("rows"):
        return True
    if (payload.ms_quarterly_results_table or {}).get("quarters"):
        return True
    return False


def run_readiness_check(payload: ReportPayload, step_results: list[StepResult]) -> StepResult:
    """Return FAILED with structured reasons when preview would be unusable."""
    reasons: list[str] = []
    step_failures: list[dict] = []

    for r in step_results:
        if r.status != Status.FAILED:
            continue
        if r.step_name == STEP:
            continue
        label = _STEP_LABELS.get(r.step_name, r.step_name)
        line = f"{label}: {r.message or 'failed'}"
        if r.error_detail:
            line += f" — {r.error_detail}"
        if r.step_name in _BLOCKING_STEPS:
            reasons.append(line)
        step_failures.append(
            {
                "step": r.step_name,
                "label": label,
                "message": r.message,
                "error_detail": r.error_detail,
            }
        )

    # Currency-mismatch gate is evaluated regardless of permissive mode because
    # it is a correctness issue (numbers on different bases), not a sparsity issue.
    if _block_on_currency_mismatch():
        msg = _currency_mismatch_reason(payload)
        if msg:
            reasons.append(msg)

    # In permissive mode, always generate a report (even with partial/missing data).
    # The report will show "—" for missing fields and the data validation layer
    # will flag any issues in the Data Quality line.
    if _readiness_permissive():
        pass  # Skip all data-sparsity checks
    else:
        fq_failed = any(
            r.step_name == "fetch_quote" and r.status == Status.FAILED for r in step_results
        )
        if not fq_failed and not _has_quote(payload):
            reasons.append(
                "No usable Yahoo quote (price or market cap). Cover slide and sizing cannot be shown reliably."
            )
        fin_failed = any(
            r.step_name == "fetch_financials" and r.status == Status.FAILED for r in step_results
        )
        if not _has_yahoo_financials(payload) and not _has_ms_forecast_data(payload):
            if fin_failed:
                reasons.append(
                    "Yahoo Finance financials failed (no periods) and MarketScreener did not yield usable "
                    "consensus/forecast/valuation tables — the preview would be mostly empty."
                )
            else:
                reasons.append(
                    "No quarterly/annual actuals and no MarketScreener consensus/forecast blocks — "
                    "executive summary and key tables would have no numbers."
                )

    with StepTimer() as t:
        if not reasons:
            return StepResult(
                step_name=STEP,
                status=Status.SUCCESS,
                source="policy",
                message="Report data sufficient for preview output",
                data={"reasons": [], "step_failures": [], "summary": ""},
                elapsed_seconds=t.elapsed,
            )

        summary = reasons[0] if len(reasons) == 1 else (reasons[0] + f" (+{len(reasons) - 1} more)")
        detail = {
            "summary": summary,
            "reasons": reasons,
            "step_failures": step_failures,
        }
        return StepResult(
            step_name=STEP,
            status=Status.FAILED,
            source="policy",
            message="Preview not generated: insufficient or failed upstream data",
            error_detail=summary,
            data=detail,
            elapsed_seconds=t.elapsed,
        )
