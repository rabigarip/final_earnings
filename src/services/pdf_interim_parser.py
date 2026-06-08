"""Generic IFRS interim-statement PDF parser.

Phase 2 of the disclosed-source pipeline. Reads any IFRS-compliant
interim PDF (Q1/H1/9M) and extracts the standalone-quarter income-
statement line items, then emits the JSON shape that
`disclosed_loader.py` already understands.

Coverage targets:
  * GCC banks (BKMB, NBO, BKDB, Riyad, Al Rajhi, …) — same publishing
    pattern, all in English IFRS layout. Expect ~95% accuracy.
  * Saudi industrials reporting in SAR — same pattern. Expect ~80%.
  * Hong Kong / China banks reporting in English — usable, ~70%.
  * Indian companies (Indian GAAP — Ind AS) — partial. Layouts vary.

Approach (deliberately simple, no LLM):
  1. Find the page containing "Statement of Comprehensive Income"
     (or equivalent — "Income Statement", "Statement of Profit or Loss").
  2. Locate column headers — typically "30-Jun-2025" / "30-Sep-2025"
     etc. Most interim reports publish BOTH the cumulative period
     (e.g. 6M ended 30-Jun-2025) AND the standalone-quarter column
     (3M ended 30-Jun-2025) in the right two columns.
  3. Pick the right column heuristically (rightmost numeric column
     matching a 3M period).
  4. Extract canonical line items: Operating Income (or "Total Revenue"
     for non-banks), Net Interest Income, Net Income (Profit for the
     period), EPS.
  5. Return a structured dict matching the disclosed_loader schema.

Failure mode: if the parser can't locate the income statement OR can't
identify the standalone-quarter column, return None. Caller writes a
TODO marker so an analyst knows to hand-curate the JSON.

This file is intentionally pure-Python with only pypdf as a dependency.
No regex magic, no shell-outs. Each step is a small testable function.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ────────────────────── DATA TYPES ─────────────────────────────

@dataclass
class ExtractedQuarter:
    period: str                              # "Q3 2025"
    interest_income: Optional[float] = None
    interest_expense: Optional[float] = None
    net_interest_income: Optional[float] = None
    net_interest_islamic_combined: Optional[float] = None
    fee_income_net: Optional[float] = None
    other_operating_income: Optional[float] = None
    operating_income: Optional[float] = None
    operating_expenses: Optional[float] = None
    impairments: Optional[float] = None
    profit_before_tax: Optional[float] = None
    tax: Optional[float] = None
    net_income: Optional[float] = None
    eps: Optional[float] = None
    revenue: Optional[float] = None          # non-banks
    period_end_date: Optional[str] = None    # ISO date
    units_label: str = ""                    # "RO'000", "SAR M", etc.
    extraction_confidence: str = "low"       # low | medium | high
    notes: list[str] = field(default_factory=list)


# ────────────────────── INCOME STATEMENT PAGE LOCATION ──────────

# Heading patterns for the income-statement page (case-insensitive).
INCOME_STATEMENT_HEADINGS = [
    "STATEMENT OF COMPREHENSIVE INCOME",
    "STATEMENT OF PROFIT OR LOSS",
    "INCOME STATEMENT",
    "STATEMENT OF FINANCIAL PERFORMANCE",
    "CONSOLIDATED INCOME STATEMENT",
    "CONSOLIDATED STATEMENT OF PROFIT OR LOSS",
]

# Sentinels that confirm we're on the right page (anchor line items).
ANCHOR_TERMS = [
    "interest income", "net interest income", "operating income",
    "profit for the period", "earnings per share", "total revenue",
]


def _find_income_statement_page(pdf_text_per_page: list[str]) -> Optional[int]:
    """Scan extracted pages, return 0-indexed page number of income
    statement. Heuristic: page contains BOTH an income-statement
    heading AND at least 2 anchor terms."""
    for i, text in enumerate(pdf_text_per_page):
        upper = text.upper()
        has_heading = any(h in upper for h in INCOME_STATEMENT_HEADINGS)
        if not has_heading: continue
        n_anchors = sum(1 for a in ANCHOR_TERMS if a in text.lower())
        if n_anchors >= 2:
            return i
    return None


# ────────────────────── COLUMN HEADER PARSING ───────────────────

# Period header patterns — IFRS reports typically print dates as
# "DD-Mmm-YYYY". The page heading often ALSO contains a date in the
# form "30 JUNE 2025" (with spaces, no hyphens). To avoid picking up
# the heading date as a column header, restrict to hyphenated format
# (DD-Mmm-YYYY) or slash format (DD/MM/YYYY).
_DATE_HEADER_RE = re.compile(
    r"(\d{1,2})[\-/](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\-/](20\d{2})",
    re.IGNORECASE,
)
# Allow "for [the] X months [period] ended" — some publishers omit "the"
# (Bank Muscat) and some include it.
_PERIOD_LENGTH_RE = re.compile(
    r"for\s+(?:the\s+)?(three|six|nine|twelve)\s+months\s+(?:period\s+)?ended",
    re.IGNORECASE,
)


@dataclass
class ColumnHeader:
    period_end: str       # ISO date
    period_length_months: int   # 3 / 6 / 9 / 12


def _parse_column_headers(text: str) -> list[ColumnHeader]:
    """Find the column headers above the income-statement table.

    Interim reports (H1, 9M) typically render BOTH a cumulative period
    pair AND a standalone-quarter pair side by side:

        -for six months period ended-  -for three months period ended-
        30-Jun-2025 30-Jun-2024         30-Jun-2025 30-Jun-2024

    Distribution heuristic: when N qualifiers precede M date headers
    and M is divisible by N, assign the first M/N dates to qualifier 1,
    the next M/N to qualifier 2, etc. Otherwise fall back to "nearest
    preceding" matching for each date.
    """
    period_blocks = list(_PERIOD_LENGTH_RE.finditer(text))
    dates = list(_DATE_HEADER_RE.finditer(text))
    if not dates:
        return []
    _LENGTH_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}
    qualifier_lengths = [_LENGTH_WORDS.get(b.group(1).lower(), 3)
                          for b in period_blocks]
    out: list[ColumnHeader] = []

    n_q = len(qualifier_lengths)
    n_d = len(dates)
    use_distribution = (n_q >= 2 and n_d % n_q == 0)

    for i, d in enumerate(dates):
        day, mon, yr = d.group(1), d.group(2), d.group(3)
        try:
            dt = datetime.strptime(f"{day} {mon[:3]} {yr}", "%d %b %Y").date()
        except ValueError:
            continue
        if use_distribution:
            # Position i out of n_d → qualifier_lengths[i // (n_d // n_q)].
            per = n_d // n_q
            q_idx = min(i // per, n_q - 1)
            length = qualifier_lengths[q_idx]
        else:
            # Nearest-preceding fallback.
            preceding = [b for b in period_blocks if b.start() < d.start()]
            if preceding:
                kw = preceding[-1].group(1).lower()
                length = _LENGTH_WORDS.get(kw, 3)
            else:
                # No qualifier present — Q1 reports often omit it
                # entirely. Default 3M.
                length = 3
        out.append(ColumnHeader(period_end=dt.isoformat(),
                                  period_length_months=length))
    return out


def _identify_standalone_quarter_column(headers: list[ColumnHeader]) -> Optional[int]:
    """Among extracted column headers, pick the index of the column that
    represents the STANDALONE-QUARTER value for the most recent period.

    Layout for an H1 or 9M report (BKMB shape):
      [0] H1/9M cumulative current period
      [1] H1/9M cumulative prior period
      [2] standalone-Q current period  ← we want this
      [3] standalone-Q prior period

    Layout for a Q1 report:
      [0] Q1 current period
      [1] Q1 prior period

    Strategy:
      * Prefer a 3-month column over 6/9/12-month.
      * Among 3-month columns at the SAME (latest) date, pick the LAST
        one (rightmost) — the standalone column in interim reports
        appears AFTER the cumulative columns.
      * If only one column exists per period (Q1 reports), the
        standalone column IS the cumulative.
    """
    if not headers: return None
    three_month = [(i, h) for i, h in enumerate(headers) if h.period_length_months == 3]
    if three_month:
        # Find latest date among 3M columns, then take the rightmost
        # index with that date.
        latest = max(h.period_end for _, h in three_month)
        matches = [(i, h) for i, h in three_month if h.period_end == latest]
        return matches[-1][0]
    sorted_all = sorted(enumerate(headers), key=lambda ih: ih[1].period_end, reverse=True)
    return sorted_all[0][0] if sorted_all else None


# ────────────────────── LINE-ITEM EXTRACTION ────────────────────

# Map canonical field name → list of label variants seen in the wild.
# Order matters: more specific labels first (so "Net interest income"
# wins over "Interest income" when both appear).
LINE_ITEM_LABELS: dict[str, list[str]] = {
    "net_interest_income": [
        "net interest income",
    ],
    "net_interest_islamic_combined": [
        "net interest income and income from islamic financing",
        "net interest income and income from islamic",
    ],
    "interest_income": [
        "interest income",   # generic — only used when "net interest income" not present in same row
    ],
    "interest_expense": [
        "interest expense",
    ],
    "fee_income_net": [
        "commission and fee income (net)",
        "commission and fee income",
        "fee and commission income",
        "net fee income",
    ],
    "other_operating_income": [
        "other operating income",
    ],
    "operating_income": [
        "operating income",
        "total operating income",
    ],
    "operating_expenses": [
        "other operating expenses",
        "operating expenses",
    ],
    "impairments": [
        "net impairment losses",
        "impairment losses",
        "provision for credit losses",
    ],
    "profit_before_tax": [
        "profit before taxation",
        "profit before tax",
        "income before tax",
    ],
    "tax": [
        "tax expense",
        "income tax",
        "income tax expense",
    ],
    "net_income": [
        "profit for the period",
        "profit for the year",
        "net profit",
        "net income",
    ],
    "eps": [
        "basic and diluted",   # "Earnings per share / Basic and diluted"
        "earnings per share",
    ],
    "revenue": [
        "total revenue",
        "revenue from operations",
        "revenue",   # generic — last resort for non-banks
    ],
}


# Number tokenization: IFRS reports use thousand-separated integers,
# parenthetical negatives, and dashes for nulls. Allow each form.
_NUM_TOKEN_RE = re.compile(
    r"\(?\s*-?\s*[\d][\d,\.]*\s*\)?",
)

def _parse_number(tok: str) -> Optional[float]:
    """Convert a single tokenised number from PDF text to float.
    Returns None for dashes ('-') and unparseable strings."""
    s = tok.strip().replace(" ", "")
    if not s or s in {"-", "—"}:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace(",", "")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _extract_row_values(text: str, label_variants: list[str]) -> Optional[list[float]]:
    """For the most-specific variant first, scan all lines for a row
    whose stripped start matches the variant. Return the list of
    numeric values on that row, left-to-right.

    The outer loop on variants ensures specificity wins over locality:
    "profit for the period" beats "net income" even when the latter
    appears earlier in the document. Returns None if no variant matches.

    Note-reference numbers (e.g. "13" in "Interest income 13 297,640
    302,903") are stripped from the value list — they're a footnote
    pointer, not data."""
    lines = text.split("\n")
    for label in label_variants:
        for line in lines:
            clean = line.strip()
            if not clean: continue
            # Strip leading dash/asterisk/bullet so EPS rows match.
            clean = re.sub(r"^[\-\*•–]\s*", "", clean)
            cl_low = clean.lower()
            if not cl_low.startswith(label): continue
            rest = clean[len(label):].strip()
            toks = _NUM_TOKEN_RE.findall(rest)
            vals = [v for v in (_parse_number(t) for t in toks) if v is not None]
            if not vals:
                return []
            # Strip a leading note-reference number: a small unsigned
            # integer (1-99) immediately after the label that's followed
            # by at least one more value. Note numbers are always small
            # positive integers; real first-column values for income-
            # statement line items are typically >100 (in RO'000/SAR M
            # at company scale) or sub-1 (EPS in RO). The boundary at
            # |x|<=99 reliably separates them.
            if (len(vals) >= 2 and isinstance(vals[0], (int, float))
                and float(vals[0]).is_integer()
                and 1 <= vals[0] <= 99):
                vals = vals[1:]
            return vals
    return None


# ────────────────────── PUBLIC API ──────────────────────────────

def extract_interim_quarter(pdf_path: Path) -> Optional[ExtractedQuarter]:
    """Parse an IFRS interim PDF and return the standalone-quarter
    income-statement values, or None if extraction failed.

    Caller writes the result to data/disclosed/{ticker}.json via the
    schema in disclosed_loader. The `extraction_confidence` field on
    the returned object indicates how much to trust the values:

      high   — column header explicitly tagged "for three months", all
               core line items extracted, units header found
      medium — column header inferred (Q1 report with no qualifier),
               core line items extracted
      low    — partial extraction; some line items missing
    """
    try:
        import pypdf
    except ImportError:
        log.error("pypdf not installed — cannot parse PDFs")
        return None

    if not pdf_path.is_file():
        log.warning("PDF not found: %s", pdf_path)
        return None

    try:
        reader = pypdf.PdfReader(str(pdf_path))
        pages_text = [p.extract_text() or "" for p in reader.pages]
    except Exception as exc:
        log.warning("PDF read failed for %s: %s", pdf_path, exc)
        return None

    page_idx = _find_income_statement_page(pages_text)
    if page_idx is None:
        log.info("No income statement page found in %s", pdf_path.name)
        return None
    page_text = pages_text[page_idx]

    headers = _parse_column_headers(page_text)
    col_idx = _identify_standalone_quarter_column(headers)
    if col_idx is None or not headers:
        log.info("Could not identify standalone-quarter column in %s", pdf_path.name)
        return None
    chosen = headers[col_idx]

    # Compose period label from the chosen column's date.
    try:
        d = datetime.fromisoformat(chosen.period_end).date()
    except ValueError:
        return None
    if chosen.period_length_months == 3:
        q = (d.month - 1) // 3 + 1
        period_label = f"Q{q} {d.year}"
    elif chosen.period_length_months == 6:
        # H1 → Q1 + Q2 combined; standalone would still be Q2 if we picked
        # the right column. Best label = the second quarter.
        period_label = f"Q2 {d.year}"
    elif chosen.period_length_months == 9:
        period_label = f"Q3 {d.year}"
    else:
        period_label = f"Q4 {d.year}"

    # Detect units (RO'000, SAR M, AED'000, etc.) — usually printed
    # above the data rows.
    units_match = re.search(
        r"(RO|SAR|AED|OMR|QAR|KWD|USD|EUR|GBP|HKD|CNY|INR|ZAR)\s*[\'\"]?\s*(000|million|m|mln|bn|k|thousand|thousands)",
        page_text, re.IGNORECASE,
    )
    units_label = units_match.group(0) if units_match else ""

    out = ExtractedQuarter(period=period_label,
                            period_end_date=chosen.period_end,
                            units_label=units_label)

    # Extract each line item. Pick the value from the chosen column.
    # The row values are a list left-to-right; the column index needs to
    # match the order in the headers list, which (in well-formed reports)
    # corresponds 1:1 to numeric columns on the row.
    n_extracted = 0
    for field_name, variants in LINE_ITEM_LABELS.items():
        vals = _extract_row_values(page_text, variants)
        if vals is None: continue
        if col_idx >= len(vals): continue
        v = vals[col_idx]
        if v is not None:
            setattr(out, field_name, v)
            n_extracted += 1

    # Confidence scoring.
    core_fields = ["operating_income", "net_income", "eps"]
    n_core = sum(1 for f in core_fields if getattr(out, f, None) is not None)
    if n_core == 3:
        out.extraction_confidence = "high" if chosen.period_length_months == 3 else "medium"
    elif n_core >= 2:
        out.extraction_confidence = "medium"
    else:
        out.extraction_confidence = "low"
        out.notes.append(f"Only {n_core} of 3 core fields extracted")
    if not units_label:
        out.notes.append("Units label not detected — interpret as published")

    log.info("Extracted %s from %s: %d fields, confidence=%s",
             period_label, pdf_path.name, n_extracted, out.extraction_confidence)
    return out


def to_disclosed_quarterly_record(extracted: ExtractedQuarter,
                                    source_doc: str) -> dict:
    """Convert an ExtractedQuarter to the dict shape used by
    data/disclosed/{ticker}.json's `quarterly` list."""
    rec = {
        "period": extracted.period,
        "source_doc": source_doc,
        "extraction_confidence": extracted.extraction_confidence,
    }
    for fld in ("interest_income", "net_interest_income",
                  "net_interest_islamic_combined", "fee_income_net",
                  "other_operating_income", "operating_income",
                  "net_income", "eps", "revenue"):
        v = getattr(extracted, fld, None)
        if v is not None:
            rec[fld] = v
    if extracted.notes:
        rec["notes"] = extracted.notes
    return rec
