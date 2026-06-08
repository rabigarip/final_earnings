"""Disclosed-pipeline coverage tracker.

Reads `data/disclosed/{ticker}.json` files and reports:
  * coverage: how many recent quarters have published actuals
  * freshness: how stale the most-recent entry is
  * completeness: which of the standard fields populated

Output is a structured dict per ticker, intended for:
  * provenance.xlsx (new "Disclosed Coverage" section)
  * /api/v2/disclosed_status (dashboard surface)
  * the daily disclosed-refresh GHA (skip tickers with current coverage)

This is the analyst-visible counterpart to populate_disclosed.py:
populate_disclosed PRODUCES the files; disclosed_status READS them and
tells the analyst whether what we have is enough.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_ROOT = Path(__file__).resolve().parents[2]
_DISCLOSED_DIR = _ROOT / "data" / "disclosed"

# Standard fields we report on. A ticker's disclosed entry is
# "complete" when these are all populated.
_TRACKED_FIELDS = (
    "operating_income", "net_income", "eps",
)
# Banks additionally publish these. Non-banks may not.
_BANK_FIELDS = (
    "net_interest_income", "fee_income_net",
)


@dataclass
class DisclosedCoverage:
    ticker: str
    file_exists: bool
    n_quarters: int
    most_recent_period: Optional[str]
    most_recent_source_doc: Optional[str]
    fields_complete: bool           # all _TRACKED_FIELDS populated in latest quarter
    coverage_pct: float             # % of last-N-quarters that have all _TRACKED_FIELDS
    extraction_confidence: Optional[str]   # "high"/"medium"/"low" from the latest extraction
    issues: list[str]
    # Days since the most recent quarter's period_end. Lower is fresher.
    days_since_period_end: Optional[int] = None

    def as_dict(self) -> dict:
        return asdict(self)


def _period_to_quarter_end(period: str) -> Optional[date]:
    """Convert 'Q3 2025' / 'Q1 2026' → date(2025, 9, 30) / date(2026, 3, 31)."""
    import re as _re
    m = _re.match(r"^Q([1-4])\s*(\d{4})$", period.strip())
    if not m: return None
    q = int(m.group(1)); yr = int(m.group(2))
    last_month = {1: 3, 2: 6, 3: 9, 4: 12}[q]
    last_day = {3: 31, 6: 30, 9: 30, 12: 31}[last_month]
    return date(yr, last_month, last_day)


def assess(ticker: str, recent_n: int = 4) -> DisclosedCoverage:
    """Inspect data/disclosed/{ticker}.json and return a coverage record."""
    p = _DISCLOSED_DIR / f"{ticker}.json"
    if not p.is_file():
        return DisclosedCoverage(
            ticker=ticker, file_exists=False, n_quarters=0,
            most_recent_period=None, most_recent_source_doc=None,
            fields_complete=False, coverage_pct=0.0,
            extraction_confidence=None,
            issues=["No data/disclosed/{ticker}.json file present"])
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return DisclosedCoverage(
            ticker=ticker, file_exists=True, n_quarters=0,
            most_recent_period=None, most_recent_source_doc=None,
            fields_complete=False, coverage_pct=0.0,
            extraction_confidence=None,
            issues=[f"JSON parse failed: {exc}"])
    quarters = raw.get("quarterly") or []
    if not quarters:
        return DisclosedCoverage(
            ticker=ticker, file_exists=True, n_quarters=0,
            most_recent_period=None, most_recent_source_doc=None,
            fields_complete=False, coverage_pct=0.0,
            extraction_confidence=None,
            issues=["File exists but `quarterly` list is empty"])
    # Sort quarters newest-first.
    def _sort_key(q):
        d = _period_to_quarter_end(q.get("period", ""))
        return d or date(1900, 1, 1)
    quarters_sorted = sorted(quarters, key=_sort_key, reverse=True)
    latest = quarters_sorted[0]

    # Field completeness across the last N quarters.
    issues: list[str] = []
    n_check = min(recent_n, len(quarters_sorted))
    complete_count = 0
    for q in quarters_sorted[:n_check]:
        if all(isinstance(q.get(f), (int, float)) for f in _TRACKED_FIELDS):
            complete_count += 1
    coverage_pct = (complete_count / n_check * 100.0) if n_check else 0.0

    fields_complete = all(isinstance(latest.get(f), (int, float))
                           for f in _TRACKED_FIELDS)
    if not fields_complete:
        missing = [f for f in _TRACKED_FIELDS
                    if not isinstance(latest.get(f), (int, float))]
        issues.append(f"Latest quarter missing fields: {', '.join(missing)}")

    # Freshness — days since the period_end of the latest quarter.
    today = date.today()
    days_since = None
    pe = _period_to_quarter_end(latest.get("period", ""))
    if pe:
        days_since = (today - pe).days
        # IFRS interim reports typically publish ~45 days after period
        # end. If we're > 90 days past the period end without a newer
        # entry, the file is stale.
        if days_since > 90:
            issues.append(
                f"Most recent disclosed quarter ({latest['period']}) is "
                f"{days_since} days behind today — likely a new period "
                f"has published and we haven't ingested it yet")

    return DisclosedCoverage(
        ticker=ticker,
        file_exists=True,
        n_quarters=len(quarters_sorted),
        most_recent_period=latest.get("period"),
        most_recent_source_doc=latest.get("source_doc"),
        fields_complete=fields_complete,
        coverage_pct=round(coverage_pct, 1),
        extraction_confidence=latest.get("extraction_confidence"),
        issues=issues,
        days_since_period_end=days_since,
    )


def assess_universe(tickers: list[str], recent_n: int = 4) -> list[DisclosedCoverage]:
    """Bulk version. Used by the dashboard to render a coverage matrix."""
    return [assess(t, recent_n=recent_n) for t in tickers]
