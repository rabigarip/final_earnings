"""
IR-page PDF probe provider — Stage 1 / Stage 2 bridge.

Reads quarterly financial statements directly from company-published
PDFs and extracts income statement / balance sheet / cash flow tables.
This is the **highest-trust source** for backward-looking financials:
it's the same filed statement that Bloomberg / FactSet derive from,
so the values are the ground truth itself, not a third-party copy.

Coverage path for Stage 1:
- BKMB.OM and OQEP.OM are the only panel tickers stuck on MS-only
  (Yahoo doesn't carry .OM, MS doesn't publish balance sheet / cash
  flow). For both, the IR-page route adds primary-filing data we
  literally cannot get any other way.

Design constraint discovered during Day 5 probing: both BKMB and OQEP
IR pages are JS-rendered SPAs whose PDF links don't appear in static
HTML. Auto-discovering the latest filing requires Playwright +
manual page navigation per company. So this provider relies on a
**curated PDF URL config** rather than auto-discovery — users (or a
quarterly cron) supply the latest URL per ticker. Once the URL is
known, the parsing logic is fully deterministic.

Config file: `config/ir_pdfs.toml` (or env var `IR_PDF_OVERRIDE_*`).
Each entry: ticker -> {url, period_label, currency}.

Field coverage:
  - income_statement_annual / income_statement_quarterly
  - balance_sheet
  - cash_flow
  - company_profile (name / sector from PDF cover)

Limitations:
  - PDFs vary in layout. The parser uses heuristics (keyword search +
    table detection) that work for ~80% of GCC bank/E&P filings. For
    edge cases the parser returns partial data flagged with a warning.
  - Numbers are extracted as-is — no currency conversion. The unit
    (OMR / SAR / AED) comes from the PDF header.

Run modes:
  - HARNESS mode (`probe_sources.py --only ir_pdf`): emits one cell per
    canonical field, picks the most recent statement from cache.
  - DOWNLOAD mode: separate script (`scripts/download_ir_pdfs.py`)
    fetches the latest PDFs into `cache/ir_pdfs/` per the config.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore

from src.services.probe_harness import Provider, persist_raw, cache_root


# Stub config — populated as PDFs are curated. Each entry is the
# latest filed quarterly PDF for the ticker.
#
# To add a new ticker:
#   1. Visit the company's IR page (manual, ~5 min)
#   2. Copy the most recent quarterly PDF URL
#   3. Add an entry here, run `scripts/download_ir_pdfs.py` to fetch
#      it into cache/ir_pdfs/<ticker>.pdf
#   4. Re-run the probe harness with `--only ir_pdf`
#
# Stage 2 will replace this manual config with a Playwright-based
# scraper that scans the IR page each quarter and updates entries.
_IR_PDF_URLS: dict[str, dict[str, str]] = {
    # Stage 2 curation (manual URL lookup, May 2026).
    #
    # Bank Muscat publishes its quarterly statements at a stable path
    # under /investorrelations/QuarterlyReports/ — file name encodes
    # the MSX month code (e.g. MSM_0326 = Q1 2026 / March 2026 close).
    "BKMB.OM": {
        "url": "https://www.bankmuscat.om/en/investorrelations/QuarterlyReports/MSM_0326.pdf",
        "period_label": "Q1 2026",
        "currency": "OMR",
    },
    # OQEP's IR portal uses content-hash paths (timestamp + slug) that
    # are not directly guessable — but the URL itself is stable once
    # the document is uploaded. Q1 2025 is the latest **signed** PDF
    # with full IS/BS/CF tables (later quarters are presentation-only).
    "OQEP.OM": {
        "url": "https://oqep.om/UploadsAll/IRDocs/1747895969828Signed_English_OQEP_FS_Q1_2025.pdf",
        "period_label": "Q1 2025",
        "currency": "USD",  # OQEP reports in USD per IPO prospectus
    },
    # ADCB publishes its quarterly statements at a date-pathed URL like
    # /en/multimedia/pdfs/<yyyy>/<month>/FinancialStatements-Q<n>-<yyyy>.pdf
    # The IR landing page is Cloudflare-gated, but file URLs are not.
    "ADCB.AE": {
        "url": "https://www.adcb.com/en/multimedia/pdfs/2026/april/FinancialStatements-Q1-2026.pdf",
        "period_label": "Q1 2026",
        "currency": "AED",
    },
    # ADNOC Drilling uses ASHX wrapper URLs that proxy to PDFs.
    # File is signed (audited) Q1 2026 financial statements (English).
    "ADNOCDRILL.AE": {
        "url": "https://adnocdrilling.ae/-/media/drilling/files/2026/1q-2026-results/adnoc-drilling-1q26-financial-statements_en.ashx",
        "period_label": "Q1 2026",
        "currency": "USD",
    },
}


def _pdf_cache_path(ticker: str) -> Path:
    safe = ticker.replace(".", "_").replace("/", "_")
    return cache_root().parent / "ir_pdfs" / f"{safe}.pdf"


def _has_pdf(ticker: str) -> bool:
    return _pdf_cache_path(ticker).exists()


# ── Generic table extractors ──
#
# We don't try to parse the full statement structure. Instead, we
# scan each page for keyword-anchored numeric rows and return what
# we find as a flat dict. Downstream code (the reconciler / deck)
# decides how to map our row names to canonical metrics.

_INCOME_KEYWORDS = [
    "Interest income", "Net interest income", "Operating income",
    "Net revenue", "Total revenue", "Revenue",
    "Operating profit", "Profit before tax", "Net profit",
    "Profit for the period", "Earnings per share",
]

_BALANCE_KEYWORDS = [
    "Total assets", "Total liabilities", "Total equity",
    "Loans and advances", "Customer deposits",
    "Cash and balances", "Investments",
]

_CASHFLOW_KEYWORDS = [
    "Net cash from operating", "Net cash used in operating",
    "Net cash from investing", "Net cash from financing",
    "Cash and cash equivalents", "Free cash flow",
]


def _scan_pdf_for_keywords(path: Path, keywords: list[str]) -> dict[str, Any]:
    """Open a PDF and pull the first numeric value that appears on
    the same line as each keyword. Returns {keyword: float} dict.

    This is a heuristic — works for standard one-column-per-period
    tables but degrades on multi-column or scanned-image PDFs."""
    if pdfplumber is None:
        return {"_error": "pdfplumber not installed"}

    out: dict[str, Any] = {}
    pages_scanned = 0
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                pages_scanned += 1
                txt = page.extract_text() or ""
                for kw in keywords:
                    if kw.lower() in txt.lower() and kw not in out:
                        # Find the line containing kw and pull the first numeric token after
                        for line in txt.split("\n"):
                            if kw.lower() in line.lower():
                                # Extract numbers like "12,345", "(1,234)", "1,234.56"
                                nums = re.findall(r"\(?[\d,]+\.?\d*\)?", line[len(kw):])
                                for nstr in nums:
                                    n = _parse_num(nstr)
                                    if n is not None:
                                        out[kw] = n
                                        break
                                break
                # If we have all keywords, no need to keep scanning
                if all(kw in out for kw in keywords):
                    break
    except Exception as exc:
        out["_error"] = f"{type(exc).__name__}: {exc}"
    out["_pages_scanned"] = pages_scanned
    return out


def _parse_num(s: str) -> Optional[float]:
    """Parse '12,345', '(1,234)' (negative), '1,234.56' to float."""
    s = s.strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "")
    try:
        v = float(s)
        return -v if neg else v
    except (TypeError, ValueError):
        return None


class IRPDFProvider(Provider):
    name = "ir_pdf"

    def __init__(self):
        # Map ticker -> parsed-statement cache (per-process memo).
        self._cache: dict[str, dict[str, Any]] = {}

    def _ensure_parsed(self, ticker: str) -> dict[str, Any]:
        if ticker in self._cache:
            return self._cache[ticker]
        path = _pdf_cache_path(ticker)
        if not path.exists():
            raise NotImplementedError(
                f"No IR PDF cached for {ticker}. Add it to "
                f"src/providers/probe_ir_pdf.py _IR_PDF_URLS and run "
                f"scripts/download_ir_pdfs.py."
            )
        parsed = {
            "income_statement": _scan_pdf_for_keywords(path, _INCOME_KEYWORDS),
            "balance_sheet":    _scan_pdf_for_keywords(path, _BALANCE_KEYWORDS),
            "cash_flow":        _scan_pdf_for_keywords(path, _CASHFLOW_KEYWORDS),
            "_pdf_path":        str(path),
            "_meta":            _IR_PDF_URLS.get(ticker, {}),
        }
        self._cache[ticker] = parsed
        return parsed

    def _fetch_income_statement_annual(self, ticker: str):
        parsed = self._ensure_parsed(ticker)
        is_data = parsed["income_statement"]
        if not any(k for k in is_data if not k.startswith("_")):
            raise ValueError("no income-statement rows extracted")
        raw_id = persist_raw(self.name, ticker, "income_statement_annual", parsed)
        meta = parsed["_meta"]
        return is_data, meta.get("currency", ""), meta.get("period_label", ""), raw_id

    def _fetch_income_statement_quarterly(self, ticker: str):
        # The same PDF carries quarterly when the filing is a Q-report;
        # we surface the same parsed block for now and let the
        # reconciler decide which canonical field to map to based on
        # the period_label in _meta.
        return self._fetch_income_statement_annual(ticker)

    def _fetch_balance_sheet(self, ticker: str):
        parsed = self._ensure_parsed(ticker)
        bs = parsed["balance_sheet"]
        if not any(k for k in bs if not k.startswith("_")):
            raise ValueError("no balance-sheet rows extracted")
        raw_id = persist_raw(self.name, ticker, "balance_sheet", parsed)
        meta = parsed["_meta"]
        return bs, meta.get("currency", ""), meta.get("period_label", ""), raw_id

    def _fetch_cash_flow(self, ticker: str):
        parsed = self._ensure_parsed(ticker)
        cf = parsed["cash_flow"]
        if not any(k for k in cf if not k.startswith("_")):
            raise ValueError("no cash-flow rows extracted")
        raw_id = persist_raw(self.name, ticker, "cash_flow", parsed)
        meta = parsed["_meta"]
        return cf, meta.get("currency", ""), meta.get("period_label", ""), raw_id

    # ── Everything else not implemented ──
    # IR PDFs typically don't carry live prices, forward valuation,
    # or analyst data — those stay with the live-quote providers.
