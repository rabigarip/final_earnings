"""Calendar-aware currency for grounding data.

Grounding metrics (the disclosed FY actuals + quarterly history) have a
calendar-bound shelf life: FY2025 is "current" only until FY2026 files. So
currency is not a property we set once at extraction — it is RECOMPUTED at
generation time against the company's reporting calendar and today's date.

This module answers, for any ticker + as-of date:
  - what is the latest ANNUAL and QUARTER period the company should have
    filed by now (`expected_latest`), and
  - is the disclosed grounding behind that (`grounding_staleness`)?

Drivers: `fiscal_year_end_month` from the registry (402 names Dec, 100 Mar)
and a conservative reporting lag (annual filings are out well within ~90
days of year-end; quarters within ~60). The earnings calendar, when
present, gives the precise next-filing date and overrides the estimate.

Comparisons are done on period-END DATES, not parsed labels, so they're
robust to "FY 2025" vs "Q1 2026" vs Indian "Q4 FY26" formatting.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any, Optional

from src.services.ticker_registry import get_ticker_info

# Conservative — a real filing is always out by these lags, so we never
# flag "stale" before the newer period could plausibly exist.
ANNUAL_LAG_DAYS = 90
QUARTER_LAG_DAYS = 60


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _fye_month(ticker: str) -> int:
    m = get_ticker_info(ticker).get("fiscal_year_end_month") or 12
    return int(m) if m in range(1, 13) else 12


def _fy_year_of_quarter(cy: int, cm: int, fye_m: int) -> int:
    """The fiscal-year label-year a calendar quarter-end (cy, cm) belongs to.
    For a Dec year-end every quarter is FY=cy; for a March year-end the
    Jun/Sep/Dec quarters roll into the next March-ending FY."""
    return cy if cm <= fye_m else cy + 1


def _quarter_num(cm: int, fye_m: int) -> int:
    return 4 - (((fye_m - cm) % 12) // 3)


def _quarter_label(qend: date, fye_m: int) -> str:
    q = _quarter_num(qend.month, fye_m)
    fyy = _fy_year_of_quarter(qend.year, qend.month, fye_m)
    # Dec year-ends use the calendar-style "Q1 2026"; off-calendar fiscal
    # years use the Indian-style "Q1 FY26" so the period is unambiguous.
    return f"Q{q} {fyy}" if fye_m == 12 else f"Q{q} FY{fyy % 100:02d}"


def _fy_label(y: int, fye_m: int) -> str:
    return f"FY{y}" if fye_m == 12 else f"FY{y % 100:02d}"


def _latest_quarter_end_on_or_before(d: date, fye_m: int) -> date:
    """Most recent fiscal quarter-end (month ≡ fye_m mod 3) whose month-end
    is ≤ d."""
    y, m = d.year, d.month
    for _ in range(15):
        if (m - fye_m) % 3 == 0:
            me = _month_end(y, m)
            if me <= d:
                return me
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return _month_end(d.year, d.month)


def _next_quarter_end(after: date, fye_m: int) -> date:
    y, m = after.year, after.month
    for _ in range(15):
        m += 1
        if m == 13:
            m, y = 1, y + 1
        if (m - fye_m) % 3 == 0:
            return _month_end(y, m)
    return after


def expected_latest(ticker: str, as_of: Optional[date] = None) -> dict[str, Any]:
    """What the company should have filed by `as_of`."""
    as_of = as_of or date.today()
    fye_m = _fye_month(ticker)

    # ANNUAL — newest FY whose year-end + lag has passed.
    y = as_of.year + 1
    for _ in range(6):
        if _month_end(y, fye_m) + timedelta(days=ANNUAL_LAG_DAYS) <= as_of:
            break
        y -= 1
    ann_end = _month_end(y, fye_m)
    next_ann_end = _month_end(y + 1, fye_m)

    # QUARTER — newest fiscal quarter-end whose end + lag has passed.
    q_end = _latest_quarter_end_on_or_before(as_of - timedelta(days=QUARTER_LAG_DAYS), fye_m)
    next_q_end = _next_quarter_end(q_end, fye_m)

    # The earnings calendar, when it carries this ticker, is the precise
    # next-filing truth; fall back to the lag estimate otherwise.
    cal_next = _calendar_next_filing(ticker)

    return {
        "as_of": as_of.isoformat(),
        "fiscal_year_end_month": fye_m,
        "annual_period": _fy_label(y, fye_m),
        "annual_period_end": ann_end.isoformat(),
        # when the CURRENT expected annual was itself due (for the stale msg)
        "annual_filing_due": (ann_end + timedelta(days=ANNUAL_LAG_DAYS)).isoformat(),
        "quarter_period": _quarter_label(q_end, fye_m),
        "quarter_period_end": q_end.isoformat(),
        "next_annual_filing_due": (next_ann_end + timedelta(days=ANNUAL_LAG_DAYS)).isoformat(),
        "next_quarter_filing_due": (cal_next
                                     or (next_q_end + timedelta(days=QUARTER_LAG_DAYS)).isoformat()),
        "next_filing_source": "earnings_calendar" if cal_next else "fiscal_estimate",
    }


def _calendar_next_filing(ticker: str) -> Optional[str]:
    try:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "data" / "calendar" / "upcoming.json"
        if not p.is_file():
            return None
        for t in (json.loads(p.read_text()).get("tickers") or []):
            if isinstance(t, dict) and (t.get("ticker") or "").upper() == ticker.upper():
                return t.get("next_earnings_date") or t.get("date")
    except Exception:
        return None
    return None


def period_end_date(label: str, fye_m: int) -> Optional[date]:
    """Parse a disclosed period label ("FY 2025", "Q1 2026", "Q4 FY26") to
    its period-END date so it can be compared against `expected_latest`."""
    s = (label or "").strip()
    m = re.search(r"(\d{4})", s)
    yy = re.search(r"FY\s?(\d{2})\b", s, re.I)
    qm = re.search(r"Q([1-4])", s, re.I)
    year = None
    if m:
        year = int(m.group(1))
    elif yy:
        year = 2000 + int(yy.group(1))
    if year is None:
        return None
    if qm:
        q = int(qm.group(1))
        # quarter-end month, counting back from the fiscal year-end.
        qe_month = ((fye_m - 3 * (4 - q) - 1) % 12) + 1
        # the calendar year of that quarter-end (roll back when it sits in
        # the prior calendar year for an off-December fiscal year).
        cy = year if qe_month <= fye_m else year - 1
        if fye_m == 12:
            cy = year
        return _month_end(cy, qe_month)
    return _month_end(year, fye_m)


def grounding_staleness(ticker: str, *, disclosed: Optional[dict] = None,
                        as_of: Optional[date] = None) -> dict[str, Any]:
    """Compare the disclosed grounding's as-of vs what should be filed now.

    Returns annual/quarter currency flags, the gap, and a human status. A
    deck consumer uses `annual_current` to decide whether the FY baseline is
    safe to present as current or must be labelled stale."""
    as_of = as_of or date.today()
    fye_m = _fye_month(ticker)
    exp = expected_latest(ticker, as_of)

    if disclosed is None:
        try:
            from src.services.disclosed_loader import load_disclosed
            disclosed = load_disclosed(ticker) or {}
        except Exception:
            disclosed = {}

    fy = (disclosed.get("fy_highlights") or {}) if isinstance(disclosed, dict) else {}
    disc_ann_label = fy.get("period") if isinstance(fy, dict) else None
    disc_ann_end = period_end_date(disc_ann_label, fye_m) if disc_ann_label else None
    exp_ann_end = date.fromisoformat(exp["annual_period_end"])

    quarters = disclosed.get("quarterly") or [] if isinstance(disclosed, dict) else []
    disc_q_ends = [period_end_date(q.get("period"), fye_m)
                   for q in quarters if isinstance(q, dict) and q.get("period")]
    disc_q_ends = [d for d in disc_q_ends if d]
    disc_q_end = max(disc_q_ends) if disc_q_ends else None
    exp_q_end = date.fromisoformat(exp["quarter_period_end"])

    annual_current = bool(disc_ann_end and disc_ann_end >= exp_ann_end)
    quarter_current = bool(disc_q_end and disc_q_end >= exp_q_end)
    have_annual = disc_ann_end is not None

    if not have_annual:
        status = "no grounded FY metrics on file"
    elif annual_current and quarter_current:
        status = f"current — {disc_ann_label} is the latest filed"
    elif not annual_current:
        status = (f"STALE — {exp['annual_period']} expected (due by "
                  f"{exp['annual_filing_due']}), but only {disc_ann_label} "
                  f"on file → re-extract the new annual")
    else:
        status = (f"annual current ({disc_ann_label}); quarter behind — "
                  f"{exp['quarter_period']} expected")

    return {
        "ticker": ticker,
        "annual_current": annual_current,
        "quarter_current": quarter_current,
        "have_grounded_fy": have_annual,
        "disclosed_annual": disc_ann_label,
        "expected_annual": exp["annual_period"],
        "disclosed_latest_quarter": _quarter_label(disc_q_end, fye_m) if disc_q_end else None,
        "expected_quarter": exp["quarter_period"],
        "next_annual_filing_due": exp["next_annual_filing_due"],
        "next_quarter_filing_due": exp["next_quarter_filing_due"],
        "status": status,
    }
