"""
Cross-source reconciler for the Stage 1 coverage matrix.

For each (ticker, field) pair, this module:
  1. Reads every source's value from the coverage_matrix.csv
  2. Compares pairs of numeric sources for agreement (within ±2%)
  3. Assigns a confidence tier:
     - High:    3+ sources agree within ±2% OR an IR-page filing
                value is present
     - Medium:  2 sources agree within ±2%
     - Low:     1 source only, OR sources disagree by >5%
  4. Picks a canonical value following a fixed trust ladder:
     IR-page > exchange > MS-historical > Yahoo > Investing > Macro

The output is `reconciled.csv`: one row per (ticker, field) with the
canonical value, confidence tier, agreeing sources, and the worst-
case disagreement percentage. This is the document the Stage 1
"accuracy report" hangs off — it tells us *for which fields the free
sources match each other closely enough to be Bloomberg-substitute*.

Without a paid ground truth, cross-source agreement is the proxy for
accuracy. The Stage 1 plan calls this out explicitly: if Yahoo, MS,
and an exchange all say the same number, we trust it as well as we
would trust Bloomberg.
"""

from __future__ import annotations

import ast
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Trust ladder for canonical-value selection (left = highest trust).
# When multiple sources have a value, we pick the leftmost present.
#
# MarketScreener is intentionally demoted below both Investing.com and
# Yahoo because:
#   1. MS is web-scraped HTML, brittle to UI changes
#   2. MS's absolute values have shown systematic mismatches vs live
#      data (H-share-only market caps for HK names, M/D/YY-formatted
#      announcement dates that broke the actual/forecast split, etc.)
#   3. Investing.com fronts its data via Next.js __NEXT_DATA__ JSON —
#      structurally cleaner and matches the live equity page exactly.
# MS still wins for fields no other free source covers (multi-year
# /finances/ P/E history, /calendar/ events, /sector/ peer comps).
TRUST_LADDER = [
    "ir_pdf",        # filed-statement PDFs — ground truth (Stage 2 wired)
    "ir_page",       # legacy alias retained for backwards compat
    "bloomberg",     # user-uploaded BEST consensus (when present, wins
                     # over every free source for forward estimates,
                     # target price, and rating split).
    "investing",     # Investing.com — JSON-fronted, matches live exactly
    "yahoo",         # Yahoo Finance — API-fed, authoritative on basics
    "marketscreener",# Scraped HTML, demoted from primary
    "macro",
]

# Sources whose presence alone earns a High-confidence tier — these are
# either filed statements (IR PDFs) or exchange-direct primary feeds.
_FILING_GRADE_SOURCES = {"ir_pdf", "ir_page"}


@dataclass
class ReconciledCell:
    ticker:               str
    field:                str
    canonical_value:      Any = None
    canonical_source:     str = ""
    confidence:           str = "Low"
    sources_with_value:   list[str] = field(default_factory=list)
    sources_agreeing:     list[str] = field(default_factory=list)
    max_disagreement_pct: Optional[float] = None
    notes:                str = ""


def _parse_value(raw: str) -> Any:
    """Coerce a CSV cell back to its native type. Handles numbers,
    JSON-encoded dicts/lists, and bare strings."""
    if not raw:
        return None
    s = raw.strip()
    # try number first
    try:
        v = float(s)
        # integers from float to keep CSV display clean
        return int(v) if v.is_integer() and abs(v) < 1e15 else v
    except (TypeError, ValueError):
        pass
    # try JSON
    if s and s[0] in "{[\"":
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            pass
    # try Python literal (CSV writer may emit Python repr)
    try:
        return ast.literal_eval(s)
    except (SyntaxError, ValueError):
        pass
    return s


def _comparable_number(v: Any) -> Optional[float]:
    """Extract a single comparable number from a value when possible.
    For dicts (like profile / valuation_forward), we don't compare —
    return None and let the cross-source check skip the cell."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if not (isinstance(v, float) and math.isnan(v)) else None
    return None


def _pct_diff(a: float, b: float) -> float:
    """Symmetric percentage difference: |a-b| / max(|a|,|b|) × 100.
    Avoids the "which is base" ambiguity that signed YoY uses."""
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 0.0
    return abs(a - b) / denom * 100


def reconcile(matrix_csv: Path, out_csv: Path) -> dict:
    """Read the coverage matrix and emit a reconciled CSV.

    Returns a summary dict suitable for printing or embedding in the
    accuracy report."""
    rows = list(csv.DictReader(matrix_csv.open(newline="", encoding="utf-8")))

    # Group cells by (ticker, field)
    cells: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
    for r in rows:
        if r.get("error") or not r.get("value"):
            continue
        cells[(r["ticker"], r["field"])][r["provider"]] = _parse_value(r["value"])

    summary = {
        "total_cells": 0,
        "by_confidence": {"High": 0, "Medium": 0, "Low": 0},
        "by_field":     defaultdict(lambda: {"High": 0, "Medium": 0, "Low": 0, "n": 0}),
    }

    out_rows: list[ReconciledCell] = []
    for (ticker, field_name), sources in sorted(cells.items()):
        rc = ReconciledCell(ticker=ticker, field=field_name)
        rc.sources_with_value = sorted(sources.keys(), key=lambda s: TRUST_LADDER.index(s) if s in TRUST_LADDER else 999)
        rc.canonical_source = rc.sources_with_value[0]
        rc.canonical_value = sources[rc.canonical_source]

        # Cross-source agreement check (numeric fields only)
        comparable = {s: _comparable_number(v) for s, v in sources.items()}
        numeric_pairs = [(s, n) for s, n in comparable.items() if n is not None]
        if len(numeric_pairs) >= 2:
            # Pairwise: find the group of sources within ±2% of canonical.
            canonical_num = _comparable_number(rc.canonical_value)
            if canonical_num is not None:
                agreeing = []
                max_diff = 0.0
                for s, n in numeric_pairs:
                    d = _pct_diff(canonical_num, n)
                    max_diff = max(max_diff, d)
                    if d <= 2.0:
                        agreeing.append(s)
                rc.sources_agreeing = agreeing
                rc.max_disagreement_pct = round(max_diff, 2)
                # IR-PDF / IR-page presence wins regardless of disagreement
                if any(s in sources for s in _FILING_GRADE_SOURCES):
                    rc.confidence = "High"
                elif len(agreeing) >= 3:
                    rc.confidence = "High"
                elif len(agreeing) >= 2:
                    rc.confidence = "Medium"
                elif max_diff > 5.0:
                    rc.confidence = "Low"
                    rc.notes = f"Sources disagree by {max_diff:.1f}%"
                else:
                    rc.confidence = "Low"
            else:
                rc.confidence = "Low"
                rc.notes = "Mixed numeric/non-numeric cells"
        elif len(sources) == 1:
            # IR-PDF as a sole source is still ground truth — it IS the
            # filed statement Bloomberg derives from. Mark High.
            if rc.canonical_source in _FILING_GRADE_SOURCES:
                rc.confidence = "High"
                rc.notes = "Filing-grade single source (IR PDF)"
            else:
                rc.confidence = "Low"
                rc.notes = "Single source — no cross-check"
        else:
            # Non-numeric fields (profile dicts, IS dicts) — we currently
            # don't deep-compare. Mark Medium if multiple sources present.
            rc.confidence = "Medium" if len(sources) >= 2 else "Low"
            rc.notes = "Non-numeric — counted by presence, not value"

        out_rows.append(rc)
        summary["total_cells"] += 1
        summary["by_confidence"][rc.confidence] += 1
        summary["by_field"][field_name][rc.confidence] += 1
        summary["by_field"][field_name]["n"] += 1

    # Write CSV
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "ticker", "field",
            "canonical_value", "canonical_source", "confidence",
            "sources_with_value", "sources_agreeing",
            "max_disagreement_pct", "notes",
        ])
        for rc in out_rows:
            val = rc.canonical_value
            if isinstance(val, (dict, list)):
                val = json.dumps(val, default=str)
            w.writerow([
                rc.ticker, rc.field, val, rc.canonical_source, rc.confidence,
                ",".join(rc.sources_with_value),
                ",".join(rc.sources_agreeing),
                rc.max_disagreement_pct if rc.max_disagreement_pct is not None else "",
                rc.notes,
            ])

    return summary


def format_summary_md(summary: dict, fields_order: Optional[list[str]] = None) -> str:
    """Render a markdown summary table from reconcile()'s return."""
    by_conf = summary["by_confidence"]
    total = max(1, summary["total_cells"])
    lines = []
    lines.append("## Confidence distribution\n")
    lines.append(f"| Tier | Count | Share |")
    lines.append(f"|---|---|---|")
    for tier in ("High", "Medium", "Low"):
        n = by_conf[tier]
        lines.append(f"| **{tier}** | {n} | {n / total * 100:.0f}% |")
    lines.append("")
    lines.append("## Per-field confidence\n")
    lines.append("| Field | High | Medium | Low | Cells |")
    lines.append("|---|---|---|---|---|")
    fields = fields_order or sorted(summary["by_field"].keys())
    for f in fields:
        row = summary["by_field"][f]
        lines.append(f"| {f} | {row['High']} | {row['Medium']} | {row['Low']} | {row['n']} |")
    return "\n".join(lines)
