"""Unit tests for report_readiness (fail-loud gate before PPTX)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from src.models.company import CompanyMaster
from src.models.financials import FinancialPeriod, QuoteSnapshot
from src.models.report_payload import ReportPayload
from src.models.step_result import Status, StepResult
from src.services.report_readiness import run_readiness_check


def _payload(
    *,
    quote: QuoteSnapshot | None,
    quarterly: list[FinancialPeriod],
    consensus_summary: dict | None = None,
) -> ReportPayload:
    return ReportPayload(
        run_id="testrun1",
        generated_at=datetime.now(timezone.utc),
        company=CompanyMaster(ticker="2010.SR", company_name="Test Co"),
        quote=quote,
        quarterly_actuals=quarterly,
        consensus_summary=consensus_summary,
    )


def test_readiness_ok_with_yahoo_financials():
    q = QuoteSnapshot(ticker="2010.SR", price=12.34)
    fp = FinancialPeriod(period_label="2024-Q3", period_type="quarterly", source="yahoo", revenue=1e9)
    r = run_readiness_check(_payload(quote=q, quarterly=[fp]), [])
    assert r.status == Status.SUCCESS


def test_readiness_ok_with_ms_only():
    r = run_readiness_check(
        _payload(
            quote=QuoteSnapshot(ticker="2010.SR", price=1.0),
            quarterly=[],
            consensus_summary={"average_target_price": 100.0},
        ),
        [],
    )
    assert r.status == Status.SUCCESS


def test_readiness_fails_when_no_quote_no_step_failure():
    r = run_readiness_check(
        _payload(quote=None, quarterly=[]),
        [],
    )
    assert r.status == Status.FAILED
    assert r.data and "No usable Yahoo quote" in " ".join(r.data.get("reasons", []))


def test_readiness_skips_quote_message_when_fetch_quote_failed():
    r = run_readiness_check(
        _payload(quote=None, quarterly=[]),
        [
            StepResult(
                step_name="fetch_quote",
                status=Status.FAILED,
                source="yahoo",
                message="No price data",
            ),
        ],
    )
    assert r.status == Status.FAILED
    reasons = " ".join(r.data.get("reasons", []))
    assert "Yahoo Finance — quote" in reasons
    assert "No usable Yahoo quote" not in reasons


def test_readiness_fails_on_blocking_qa():
    q = QuoteSnapshot(ticker="2010.SR", price=1.0)
    fp = FinancialPeriod(period_label="2024-Q3", period_type="quarterly", source="yahoo", revenue=1e9)
    r = run_readiness_check(
        _payload(quote=q, quarterly=[fp]),
        [
            StepResult(
                step_name="qa_validate",
                status=Status.FAILED,
                source="qa",
                message="QA validation failed",
                error_detail="boom",
            ),
        ],
    )
    assert r.status == Status.FAILED
    sf = r.data.get("step_failures", [])
    assert any(s.get("step") == "qa_validate" for s in sf)


def test_readiness_fails_sparse_no_yahoo_no_ms():
    r = run_readiness_check(
        _payload(
            quote=QuoteSnapshot(ticker="2010.SR", price=1.0),
            quarterly=[],
        ),
        [],
    )
    assert r.status == Status.FAILED
    assert any("no numbers" in x.lower() or "consensus" in x.lower() for x in r.data.get("reasons", []))


def test_readiness_permissive_allows_sparse_yahoo_only():
    """REPORT_READINESS_MODE=permissive skips thin-data check when quote exists."""
    old = os.environ.get("REPORT_READINESS_MODE")
    try:
        os.environ["REPORT_READINESS_MODE"] = "permissive"
        r = run_readiness_check(
            _payload(
                quote=QuoteSnapshot(ticker="2010.SR", price=1.0),
                quarterly=[],
            ),
            [],
        )
        assert r.status == Status.SUCCESS
    finally:
        if old is None:
            os.environ.pop("REPORT_READINESS_MODE", None)
        else:
            os.environ["REPORT_READINESS_MODE"] = old
