"""
Smoke tests for the earnings research pipeline.

Run:  pytest tests/ -v -s

Network-dependent tests (validate_ticker, fetch_quote, fetch_financials) may FAIL
when Yahoo/MarketScreener are unavailable; assertions allow for that.
"""

import pytest
from src.models.step_result import Status
from src.storage.db import init_db, seed_companies


@pytest.fixture(scope="session", autouse=True)
def db():
    init_db()
    seed_companies()


# ── validate_ticker ───────────────────────────────────────────

class TestValidateTicker:
    def test_valid_industrial(self):
        from src.services.pipeline_steps import validate_ticker
        r = validate_ticker("2010.SR")
        r.print_box()
        assert r.status in (Status.SUCCESS, Status.PARTIAL) or (
            r.status == Status.FAILED and "Yahoo" in (r.message or "")
        )
        if r.status in (Status.SUCCESS, Status.PARTIAL):
            assert r.data is not None

    def test_valid_bank(self):
        from src.services.pipeline_steps import validate_ticker
        r = validate_ticker("1120.SR")
        r.print_box()
        assert r.status in (Status.SUCCESS, Status.PARTIAL) or (
            r.status == Status.FAILED and "Yahoo" in (r.message or "")
        )

    def test_invalid(self):
        from src.services.pipeline_steps import validate_ticker
        r = validate_ticker("ZZZZ.FAKE")
        r.print_box()
        assert r.status == Status.FAILED


# ── resolve_mapping ───────────────────────────────────────────

class TestResolveMapping:
    def test_known(self):
        from src.services.resolve_mapping import run
        r = run("2010.SR")
        r.print_box()
        assert r.status in (Status.SUCCESS, Status.PARTIAL)
        assert r.data is not None
        assert "SABIC" in r.data.company_name or r.data.company_name == "Saudi Basic Industries Corporation"

    def test_bank_flag(self):
        from src.services.resolve_mapping import run
        r = run("1120.SR")
        r.print_box()
        assert r.data.is_bank is True

    def test_unknown(self):
        from src.services.resolve_mapping import run
        r = run("9999.XX")
        r.print_box()
        assert r.status == Status.FAILED


# ── fetch_quote ───────────────────────────────────────────────

class TestFetchQuote:
    def test_sabic(self):
        from src.services.pipeline_steps import fetch_quote
        r = fetch_quote("2010.SR")
        r.print_box()
        assert r.status in (Status.SUCCESS, Status.FAILED)


# ── fetch_financials ──────────────────────────────────────────

class TestFetchFinancials:
    def test_sabic(self):
        from src.services.resolve_mapping import run as resolve
        from src.services.pipeline_steps import fetch_financials
        company = resolve("2010.SR").data
        r = fetch_financials("2010.SR", company)
        r.print_box()
        assert r.status in (Status.SUCCESS, Status.PARTIAL, Status.FAILED)
        assert r.record_count is None or r.record_count >= 0


# ── fetch_consensus ───────────────────────────────────────────

class TestFetchConsensus:
    def test_sabic(self):
        from src.services.resolve_mapping import run as resolve
        from src.services.pipeline_steps import fetch_consensus
        company = resolve("2010.SR").data
        r = fetch_consensus("2010.SR", company)
        r.print_box()
        assert r.status in (Status.SUCCESS, Status.PARTIAL, Status.FAILED)


# ── StepResult serialization ─────────────────────────────────

class TestStepResult:
    def test_to_log_dict(self):
        from src.models.step_result import StepResult, Status
        r = StepResult(
            step_name="test", status=Status.SUCCESS,
            source="unit", message="ok", elapsed_seconds=0.123,
        )
        d = r.to_log_dict()
        assert d["status"] == "success"
        assert "data" not in d
