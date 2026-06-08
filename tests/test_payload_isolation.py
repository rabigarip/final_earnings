"""
Regression test: payload isolation and data lineage across multiple companies.

Runs pipeline BYD → Infosys → FirstRand → BYD (with --skip-llm) and asserts:
- Each payload is ticker-specific (payload_source_ticker, company.ticker match run ticker).
- payload_entity_match is True when MS data is used.
- cross_company_contamination_detected is False.
- Second BYD run does not contain Infosys/FirstRand data (company name, MS-derived sections).
"""

import pytest

from src.pipeline import run_preview


# Tickers: BYD (SZ), Infosys (NS), FirstRand (JO), then BYD again
SEQUENCE = [
    ("002594.SZ", "BYD"),           # BYD Company (Shenzhen)
    ("INFY.NS", "Infosys"),         # Infosys (NSE)
    ("FSR.JO", "FirstRand"),        # FirstRand (JSE)
    ("002594.SZ", "BYD"),           # BYD again — must not carry Infosys/FirstRand data
]


def _payload_from_results(results):
    """Extract ReportPayload from build_report_payload step."""
    for s in results:
        if getattr(s, "step_name", None) == "build_report_payload" and getattr(s, "data", None):
            return s.data
    return None


@pytest.mark.slow
def test_payload_isolation_byd_infy_fsr_byd():
    """
    Run pipeline for BYD → Infosys → FirstRand → BYD.
    Assert payloads remain ticker-specific and no cross-company contamination.
    """
    payloads_by_ticker = {}  # ticker -> list of payloads (two for 002594.SZ)
    for ticker, expected_name_fragment in SEQUENCE:
        results = run_preview(ticker, skip_llm=True)
        payload = _payload_from_results(results)
        assert payload is not None, f"build_report_payload did not return data for {ticker}"
        if ticker not in payloads_by_ticker:
            payloads_by_ticker[ticker] = []
        payloads_by_ticker[ticker].append(payload)

    # 002594.SZ appears twice; INFY.NS and FSR.JO once
    assert "002594.SZ" in payloads_by_ticker and len(payloads_by_ticker["002594.SZ"]) == 2
    assert "INFY.NS" in payloads_by_ticker and len(payloads_by_ticker["INFY.NS"]) == 1
    assert "FSR.JO" in payloads_by_ticker and len(payloads_by_ticker["FSR.JO"]) == 1

    for ticker, expected_name_fragment in SEQUENCE:
        plist = payloads_by_ticker.get(ticker, [])
        for i, p in enumerate(plist):
            # Payload must be for this run's ticker
            assert getattr(p.company, "ticker", None) == ticker, (
                f"Payload company.ticker {getattr(p.company, 'ticker', None)} != run ticker {ticker}"
            )
            assert (getattr(p, "payload_source_ticker", None) or "").strip() == (ticker or "").strip(), (
                f"payload_source_ticker {getattr(p, 'payload_source_ticker', None)} != {ticker}"
            )
            assert getattr(p, "cross_company_contamination_detected", True) is False, (
                f"cross_company_contamination_detected is True for {ticker} run {i}"
            )
            # When MS data is present, entity should match
            if getattr(p, "ms_lineage", None) and (getattr(p, "ms_summary", None) or getattr(p, "consensus_summary", None)):
                assert getattr(p, "payload_entity_match", False) is True, (
                    f"payload_entity_match should be True when MS data present for {ticker}"
                )
            company_name = getattr(p.company, "company_name", None) or getattr(p.company, "company_name_long", None) or ""
            assert expected_name_fragment.upper() in company_name.upper(), (
                f"Payload company name '{company_name}' does not match expected fragment '{expected_name_fragment}' for {ticker}"
            )

    # Second BYD run must not contain Infosys or FirstRand in company or MS-derived content
    byd_second = payloads_by_ticker["002594.SZ"][1]
    company_name_byd2 = getattr(byd_second.company, "company_name", None) or getattr(byd_second.company, "company_name_long", None) or ""
    assert "Infosys" not in company_name_byd2, "Second BYD payload must not have Infosys company name"
    assert "FirstRand" not in company_name_byd2, "Second BYD payload must not have FirstRand company name"
    if getattr(byd_second, "ms_lineage", None):
        source_name = getattr(byd_second.ms_lineage, "source_company_name", None) or ""
        assert "Infosys" not in source_name and "FirstRand" not in source_name, (
            "Second BYD ms_lineage must not reference Infosys or FirstRand"
        )
