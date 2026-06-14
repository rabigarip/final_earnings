from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _payload(ticker: str, run_id: str):
    # Minimal stand-in for ReportPayload: generate_report only needs company + quote-ish fields.
    company = SimpleNamespace(
        ticker=ticker,
        company_name="TestCo",
        sector="Industrials",
        industry="Machinery",
        country="SA",
        currency="SAR",
        is_bank=False,
    )
    quote = SimpleNamespace(market_cap=None)
    return SimpleNamespace(
        run_id=run_id,
        company=company,
        quote=quote,
        memo_computed={},
        consensus_summary={},
        ms_valuation_multiples={},
        derived=None,
        news_items=[],
    )


def test_generate_report_does_not_overwrite_previous_runs(tmp_path, monkeypatch):
    """
    Regression: reports for the same ticker must not overwrite each other across runs.
    """
    from src.services import generate_report as gr

    monkeypatch.setattr(gr, "report_output_dir", lambda: tmp_path)

    p1 = _payload("2222.SR", "runA1234")
    p2 = _payload("2222.SR", "runB5678")

    # Current naming is "<ticker>_<timestamp(µs)>_earnings_preview.pptx" — the
    # run_id-based "_preview_balanced" scheme was replaced. The regression we
    # guard is unchanged: two runs of the same ticker must not overwrite.
    r1 = gr.run(p1, memo_data={}, qa_audit={})
    assert r1.status.value.lower() == "success"
    f1 = Path(r1.data)
    assert f1.is_file()

    r2 = gr.run(p2, memo_data={}, qa_audit={})
    assert r2.status.value.lower() == "success"
    f2 = Path(r2.data)
    assert f2.is_file()

    # Critical: distinct files, first still present and non-empty after second run.
    assert f1 != f2
    files = sorted(tmp_path.glob("2222.SR_*_earnings_preview.pptx"))
    assert len(files) == 2
    assert f1.is_file() and f1.read_bytes() != b""
    assert f2.read_bytes() != b""

