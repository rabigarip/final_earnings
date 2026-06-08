"""Tests for src/services/pdf_interim_parser.py.

Ground truth: Bank Muscat's published Q1/Q2/Q3 2025 and Q1 2026 interim
financial statements (RO'000). The parser is run against the actual
IR-portal PDFs (downloaded to /tmp/bkmb_ir in the fixture).
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import pytest

from src.services.pdf_interim_parser import (
    extract_interim_quarter, to_disclosed_quarterly_record,
    _parse_column_headers, _identify_standalone_quarter_column,
    _find_income_statement_page,
)

PDF_CACHE = Path("/tmp/bkmb_ir")
BKMB_URLS = {
    "MSM_0325.pdf": "https://www.bankmuscat.om/en/investorrelations/QuarterlyReports/MSM_0325.pdf",
    "MSM_0625.pdf": "https://www.bankmuscat.om/en/investorrelations/QuarterlyReports/MSM_0625.pdf",
    "MSM_0925.pdf": "https://www.bankmuscat.om/en/investorrelations/QuarterlyReports/MSM_0925.pdf",
    "MSM_0326.pdf": "https://www.bankmuscat.om/en/investorrelations/QuarterlyReports/MSM_0326.pdf",
}

# Ground truth from BKMB IR PDFs. Values are RO'000 as published.
EXPECTED = {
    "MSM_0325.pdf": {"period": "Q1 2025", "operating_income": 140675,
                      "net_income": 58561, "eps": 0.008},
    "MSM_0625.pdf": {"period": "Q2 2025", "operating_income": 147602,
                      "net_income": 67257, "eps": 0.006},
    "MSM_0925.pdf": {"period": "Q3 2025", "operating_income": 146190,
                      "net_income": 65754, "eps": 0.015},
    "MSM_0326.pdf": {"period": "Q1 2026", "operating_income": 145275,
                      "net_income": 63945, "eps": 0.009},
}


def _ensure_pdf(name: str) -> Path:
    """Download the PDF once and cache it in /tmp."""
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    dest = PDF_CACHE / name
    if not dest.is_file() or dest.stat().st_size < 50_000:
        try:
            urllib.request.urlretrieve(BKMB_URLS[name], dest)
        except Exception:
            pytest.skip(f"Could not download {name} (offline?)")
    return dest


@pytest.fixture(scope="module", params=list(EXPECTED.keys()))
def bkmb_pdf(request) -> tuple[Path, dict]:
    name = request.param
    return _ensure_pdf(name), EXPECTED[name]


def test_period_label_matches(bkmb_pdf):
    pdf_path, expected = bkmb_pdf
    extracted = extract_interim_quarter(pdf_path)
    assert extracted is not None, f"extraction returned None for {pdf_path.name}"
    assert extracted.period == expected["period"]


def test_operating_income_exact(bkmb_pdf):
    pdf_path, expected = bkmb_pdf
    extracted = extract_interim_quarter(pdf_path)
    assert extracted.operating_income == expected["operating_income"], (
        f"{pdf_path.name}: got {extracted.operating_income}, "
        f"expected {expected['operating_income']}")


def test_net_income_exact(bkmb_pdf):
    pdf_path, expected = bkmb_pdf
    extracted = extract_interim_quarter(pdf_path)
    assert extracted.net_income == expected["net_income"]


def test_eps_exact(bkmb_pdf):
    pdf_path, expected = bkmb_pdf
    extracted = extract_interim_quarter(pdf_path)
    assert extracted.eps == expected["eps"]


def test_extraction_confidence_high(bkmb_pdf):
    """All 4 BKMB PDFs should yield 'high' confidence — Q1/Q3/Q1 reports
    have explicit '3-month' qualifiers; H1 report yields 'high' too
    because we picked the rightmost-3M column correctly."""
    pdf_path, _ = bkmb_pdf
    extracted = extract_interim_quarter(pdf_path)
    assert extracted.extraction_confidence == "high", (
        f"{pdf_path.name}: confidence={extracted.extraction_confidence}")


def test_to_disclosed_record_shape(bkmb_pdf):
    pdf_path, expected = bkmb_pdf
    extracted = extract_interim_quarter(pdf_path)
    rec = to_disclosed_quarterly_record(extracted, pdf_path.name)
    assert rec["period"] == expected["period"]
    assert rec["source_doc"] == pdf_path.name
    assert "extraction_confidence" in rec
    assert rec["operating_income"] == expected["operating_income"]


def test_missing_file_returns_none(tmp_path):
    """Parser handles missing PDFs gracefully (used by populate_disclosed
    when the FY report hasn't been published yet)."""
    result = extract_interim_quarter(tmp_path / "nonexistent.pdf")
    assert result is None


def test_column_header_parses_six_and_three_month():
    """Mixed-period header (H1 report) should yield 4 column headers
    with the correct period_length_months tags."""
    text = (
        "Unaudited Unaudited\n"
        " -for six months period ended-  -for three months period ended-\n"
        "30-Jun-2025 30-Jun-2024 30-Jun-2025 30-Jun-2024\n"
    )
    headers = _parse_column_headers(text)
    assert len(headers) == 4
    assert headers[0].period_length_months == 6
    assert headers[1].period_length_months == 6
    assert headers[2].period_length_months == 3
    assert headers[3].period_length_months == 3


def test_standalone_column_selection_h1():
    """For an H1 report, the standalone Q2 column is the rightmost 3M
    column with the latest date — index 2."""
    text = (
        " -for six months period ended-  -for three months period ended-\n"
        "30-Jun-2025 30-Jun-2024 30-Jun-2025 30-Jun-2024\n"
    )
    headers = _parse_column_headers(text)
    idx = _identify_standalone_quarter_column(headers)
    assert idx == 2
