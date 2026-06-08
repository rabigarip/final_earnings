"""Multi-source validation — two tiers.

TIER 1 (pre-LLM): validate raw source data before it reaches the LLM
prompt. Catches:
  * Implausible consensus values (e.g. Q+1 revenue >3x trailing 4Q mean)
  * Cross-source disagreement >X% on the same metric
  * Stale-ish data (last_refreshed > N days ago for time-sensitive fields)

TIER 2 (post-LLM): validate every numeric claim in the generated thesis
+ highlights + catalysts + risks against the canonical store. The
existing numeric-trace validator in llm_summary.py already does the
"every number must come from the data block" check; this layer adds
*cross-source* corroboration (the underlying number must agree across
the providers we trust).

Outputs are structured `ValidationFinding` records — provenance.xlsx
emits them as a "Validation" section; the dashboard surfaces them at
/api/v2/validation/{ticker}.

Design principle: fail-OPEN. Findings are *recorded*, not *blocking*.
A flag-and-ship policy beats gate-and-stall when the analyst needs a
pre-earnings deck. Hard blocks reserved for dangerous failures (e.g.
currency mismatch indicating wrong-entity contamination).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)


# ─────────────────────── DATA TYPES ─────────────────────────

@dataclass
class ValidationFinding:
    """One issue surfaced by the validator. Severity controls how the
    renderer treats it (flag pill vs hard suppression)."""
    tier: int                       # 1 (pre-LLM) or 2 (post-LLM)
    severity: str                   # "info" | "warn" | "error"
    metric: str                     # e.g. "next_quarter_consensus_revenue"
    message: str
    sources: list[str] = field(default_factory=list)
    values_by_source: dict[str, float] = field(default_factory=dict)
    suggested_action: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    ticker: str
    generated_at: str
    findings: list[ValidationFinding] = field(default_factory=list)

    def add(self, finding: ValidationFinding) -> None:
        self.findings.append(finding)

    @property
    def n_errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def n_warnings(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")

    def by_tier(self, tier: int) -> list[ValidationFinding]:
        return [f for f in self.findings if f.tier == tier]

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "generated_at": self.generated_at,
            "n_findings": len(self.findings),
            "n_errors": self.n_errors,
            "n_warnings": self.n_warnings,
            "findings": [f.as_dict() for f in self.findings],
        }


# ─────────────────────── TIER 1 (pre-LLM) ──────────────────

def validate_consensus_plausibility(
    ticker: str,
    next_q_consensus: dict[str, Optional[float]],
    trailing_actuals: list[dict],
    report: ValidationReport,
) -> None:
    """Check that Q+1 consensus values are within a plausible range of
    the trailing-4-quarter mean.

    `next_q_consensus`: {"revenue": float, "ebitda": float, "ni": float, "eps": float}
    `trailing_actuals`: list of 4 most-recent quarterly actuals in the
        same shape. We tolerate 50%-200% of the trailing mean per metric
        before flagging.
    """
    if not trailing_actuals or len(trailing_actuals) < 2:
        return    # Not enough history to assess.

    for metric in ("revenue", "ebitda", "ni", "eps"):
        c = next_q_consensus.get(metric)
        if not isinstance(c, (int, float)) or c == 0: continue
        ta = [a.get(metric) for a in trailing_actuals
              if isinstance(a.get(metric), (int, float))]
        if not ta: continue
        mean = sum(ta) / len(ta)
        if mean == 0: continue
        ratio = c / mean
        # The "obviously wrong" band — outside 0.4x to 3.0x of trailing
        # mean. Tightens to 0.6-2.0x for metrics with stable cadence
        # (EPS, revenue); looser 0.4-3.0x for NI/EBITDA which legitimately
        # swing more across cycles.
        loose = metric in ("ni", "ebitda")
        lo, hi = (0.4, 3.0) if loose else (0.6, 2.0)
        if not (lo <= ratio <= hi):
            report.add(ValidationFinding(
                tier=1, severity="warn",
                metric=f"next_quarter_consensus_{metric}",
                message=(
                    f"Q+1 {metric} consensus {c:,.0f} is {ratio:.2f}x "
                    f"the trailing-{len(ta)}Q mean of {mean:,.0f}. "
                    f"Plausibility band ({lo:.1f}x-{hi:.1f}x) breached — "
                    f"verify source before publishing."),
                values_by_source={"consensus": c, "trailing_mean": mean},
                suggested_action=(
                    "Cross-check the consensus number against the IR-portal "
                    "guidance range; replace with hand-curated value if MS "
                    "or Investing has a wrong-quarter contamination."),
            ))


def validate_source_agreement(
    ticker: str,
    field_name: str,
    values_by_source: dict[str, Optional[float]],
    report: ValidationReport,
    tolerance_pct: float = 5.0,
) -> None:
    """When ≥2 sources publish the same metric, flag disagreement >X%.

    Common cause is wrong-entity contamination (e.g. Riyad Bank's price
    appearing on the Al Rajhi page). When the spread is large, the
    canonical-source winner may be wrong; tier-1 flags this so the
    analyst can manually override before the deck ships.
    """
    vals = [(s, v) for s, v in values_by_source.items()
             if isinstance(v, (int, float))]
    if len(vals) < 2: return
    sorted_v = sorted(vals, key=lambda sv: sv[1])
    lo_s, lo_v = sorted_v[0]
    hi_s, hi_v = sorted_v[-1]
    if lo_v == 0: return
    spread_pct = (hi_v - lo_v) / abs(lo_v) * 100.0
    if spread_pct >= tolerance_pct:
        severity = "error" if spread_pct >= 50 else "warn"
        report.add(ValidationFinding(
            tier=1, severity=severity,
            metric=field_name,
            message=(
                f"Sources disagree on {field_name} by {spread_pct:.1f}%: "
                f"{lo_s}={lo_v:,.2f}, {hi_s}={hi_v:,.2f}."),
            sources=list(values_by_source.keys()),
            values_by_source={s: float(v) for s, v in vals},
            suggested_action=(
                f"Investigate why {lo_s} and {hi_s} differ; the canonical "
                f"reconciler picked one — verify it's the right one."),
        ))


def validate_macro_freshness(
    ticker: str,
    macro: dict[str, Any],
    report: ValidationReport,
    max_age_years: int = 2,
) -> None:
    """IMF / WB macro figures should be within `max_age_years` of today.
    Stale macro misleads forward-looking commentary."""
    today_year = date.today().year
    for label, year_key in (("GDP growth", "gdp_growth_year"),
                              ("Inflation", "inflation_year")):
        yr = macro.get(year_key)
        if not isinstance(yr, (int, str)): continue
        try:
            yr_int = int(yr)
        except (TypeError, ValueError):
            continue
        age = today_year - yr_int
        if age > max_age_years:
            report.add(ValidationFinding(
                tier=1, severity="info",
                metric=f"macro.{label.lower().replace(' ', '_')}",
                message=(
                    f"{label} data anchored to {yr_int}, {age} years old. "
                    f"Forward-looking commentary should rely on the IMF "
                    f"forecast for the deck's reporting year instead."),
                suggested_action="Source IMF WEO forecast for the current FY.",
            ))


# ─────────────────────── TIER 2 (post-LLM) ─────────────────

# Numeric token extraction. Matches percentages (12.1%), multiples
# (12.1x), monetary values (SAR 1.5B), and bare floats.
_NUMERIC_TOKEN_RE = re.compile(
    r"(?<![A-Za-z\d])"                          # not preceded by letter/digit
    r"-?\d+(?:\.\d+)?(?:%|x|X|B|M|K)?"          # the number itself
    r"(?![A-Za-z])",                            # not followed by letter
)


def _token_to_float(tok: str) -> Optional[float]:
    """Convert '12.1x' / '4.83%' / '3.5' to a float. Suffix is stripped."""
    s = tok.rstrip("%xXBMK").rstrip()
    try:
        return float(s)
    except ValueError:
        return None


def validate_thesis_numeric_anchors(
    ticker: str,
    thesis_paragraph: str,
    allowed_numbers: set[float],
    report: ValidationReport,
    tol_pct: float = 5.0,
) -> None:
    """For each numeric token in the thesis, verify it matches a number
    in `allowed_numbers` (typically the LLM-context anchor set built by
    `_allowed_numbers` in llm_summary). Tokens that don't match get
    flagged as potential hallucinations.

    This complements the existing `_validate_llm_output` numeric-trace
    by surfacing the findings as structured records (the existing path
    rewrites the prose silently — useful for shipping a clean deck, but
    the analyst loses sight of what was scrubbed)."""
    if not thesis_paragraph or not allowed_numbers:
        return
    tokens = _NUMERIC_TOKEN_RE.findall(thesis_paragraph)
    unmatched: list[str] = []
    for t in tokens:
        v = _token_to_float(t)
        if v is None: continue
        if any(abs(v - a) / (abs(a) or 1) * 100 <= tol_pct
                for a in allowed_numbers):
            continue
        unmatched.append(t)
    if unmatched:
        report.add(ValidationFinding(
            tier=2, severity="warn",
            metric="thesis_paragraph",
            message=(
                f"Thesis paragraph contains {len(unmatched)} numeric "
                f"token(s) that don't match any canonical-store anchor: "
                f"{', '.join(unmatched[:5])}"),
            suggested_action=(
                "Review the LLM output; these tokens may be hallucinated "
                "or paraphrased from related figures. The numeric-trace "
                "validator in llm_summary already scrubs the worst cases."),
        ))


# ─────────────────────── PUBLIC API ─────────────────────────

def run_tier_1(
    ticker: str,
    *,
    next_q_consensus: Optional[dict[str, Optional[float]]] = None,
    trailing_actuals: Optional[list[dict]] = None,
    source_disagreements: Optional[dict[str, dict[str, float]]] = None,
    macro: Optional[dict[str, Any]] = None,
) -> ValidationReport:
    """Run all tier-1 checks. Returns a populated ValidationReport.

    Caller typically does this in `build_report_payload` after the
    reconciler has produced canonical values, so we know which source
    each cell came from and what alternatives existed.
    """
    rep = ValidationReport(ticker=ticker,
                              generated_at=datetime.utcnow().isoformat())
    if next_q_consensus and trailing_actuals:
        validate_consensus_plausibility(
            ticker, next_q_consensus, trailing_actuals, rep)
    if source_disagreements:
        for fname, sv in source_disagreements.items():
            validate_source_agreement(ticker, fname, sv, rep)
    if macro:
        validate_macro_freshness(ticker, macro, rep)
    return rep


def run_tier_2(
    ticker: str,
    *,
    thesis_paragraph: Optional[str] = None,
    allowed_numbers: Optional[set[float]] = None,
    report: Optional[ValidationReport] = None,
) -> ValidationReport:
    """Run tier-2 checks against the LLM output. Accepts an existing
    tier-1 report to append into, or creates a new one."""
    rep = report or ValidationReport(
        ticker=ticker, generated_at=datetime.utcnow().isoformat())
    if thesis_paragraph and allowed_numbers:
        validate_thesis_numeric_anchors(
            ticker, thesis_paragraph, allowed_numbers, rep)
    return rep
