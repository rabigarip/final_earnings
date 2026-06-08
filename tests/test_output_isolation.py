from __future__ import annotations

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

    r1 = gr.run(p1, memo_data={}, qa_audit={})
    assert r1.status.value.lower() == "success"
    f1 = tmp_path / "2222.SR_runA1234_preview_balanced.pptx"
    assert f1.is_file()

    r2 = gr.run(p2, memo_data={}, qa_audit={})
    assert r2.status.value.lower() == "success"
    f2 = tmp_path / "2222.SR_runB5678_preview_balanced.pptx"
    assert f2.is_file()

    # Critical: first file must still exist and must not be replaced by second run.
    assert f1.is_file()
    assert f1.read_bytes() != b""
    assert f2.read_bytes() != b""

