"""Calendar service — produce data/calendar/upcoming.json.

Reads `data/tickers.json` (the registry) and, for each canonical /
active ticker, looks up the next earnings date via a fallback chain:

  1. MarketScreener /calendar/ snapshot (already cached in
     data/marketscreener/ms_<ticker>_calendar.html) — primary source
     for GCC and most other regions.
  2. yfinance — fallback for India / China-HK / South Africa where MS
     coverage is "partial". yfinance's earnings_dates field carries
     1-2 future announces for most names.
  3. None — if both fail, the ticker is omitted from upcoming.json
     rather than guessed. Better an empty calendar than a wrong one.

Output schema (data/calendar/upcoming.json):

  {
    "generated_at": "2026-05-27T10:00:00Z",
    "horizon_days": 14,
    "tickers": [
      {
        "ticker": "BKMB.OM",
        "company_name": "Bank Muscat SAOG",
        "template_family": "bank",
        "exchange_country": "OM",
        "earnings_date": "2026-07-09",
        "earnings_date_source": "marketscreener",
        "days_until": 43,
        "next_quarter_label": "Q2 2026",
        "fiscal_year_end_month": 12,
        "market_cap_usd": 8050000000
      },
      ...
    ]
  }

This file is read by:
  - The frontend dashboard (lists upcoming earnings as Generate buttons)
  - The targeted MS-snapshot refresh GHA (only refreshes tickers in this
    file, not all 500)
  - The disclosed-pipeline trigger (when a ticker enters this file's
    horizon, attempt to download fresh interim PDFs from its IR portal)

Run cadence: GHA cron 1x daily at 04:00 UTC. The downstream MS refresh
runs at 04:30 UTC after this completes.

Design constraint: this script MUST run free of charge. Uses only
yfinance (free), local MS snapshots (free, already cached), and
filesystem I/O.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "data" / "tickers.json"
OUT_PATH = ROOT / "data" / "calendar" / "upcoming.json"
MS_DIR = ROOT / "data" / "marketscreener"

HORIZON_DAYS = 14
TODAY = datetime.now(timezone.utc).date()


# ────────────────────── SOURCE 1: MS CALENDAR SNAPSHOT ──────────────

# The MS /calendar/ HTML contains an "Earnings Announcements" / "Next
# Earnings" table with the upcoming date. We've parsed this in
# `marketscreener_pages.py` for the live pipeline; here we reproduce
# the minimum extraction needed for date-only lookup.

# Tickers MS publishes use slugified names — the snapshot files are
# already keyed by our ticker symbol via the cache_key_prefix convention.

_DATE_RE = re.compile(
    r"\b(0?[1-9]|[12][0-9]|3[01])[/\-](0?[1-9]|1[0-2])[/\-](20\d{2})\b"
)

def _ms_snapshot_path(ticker: str) -> Optional[Path]:
    """Match `ms_<TICKER_NORM>_calendar.html` under data/marketscreener/.
    Ticker normalisation: `.` → `_`, uppercase. Mirrors the runtime
    cache_key_prefix shape."""
    norm = ticker.replace(".", "_").upper()
    p = MS_DIR / f"ms_{norm}_calendar.html"
    return p if p.is_file() else None


def _next_earnings_from_ms(ticker: str) -> Optional[str]:
    """Find the soonest future date in the MS calendar snapshot.
    Returns ISO date string or None."""
    p = _ms_snapshot_path(ticker)
    if not p:
        return None
    try:
        html = p.read_text(errors="ignore")
    except OSError:
        return None
    candidates: list[datetime] = []
    for m in _DATE_RE.finditer(html):
        try:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = datetime(y, mo, d, tzinfo=timezone.utc)
            if dt.date() >= TODAY and dt.date() <= TODAY + timedelta(days=120):
                candidates.append(dt)
        except (ValueError, TypeError):
            continue
    if not candidates:
        return None
    # The earliest future date wins. (MS calendar sometimes lists FY26
    # annual report alongside Q2 earnings; we want the nearest.)
    return min(candidates).date().isoformat()


# ────────────────────── SOURCE 2: YFINANCE ──────────────────────────

def _next_earnings_from_yfinance(ticker: str) -> Optional[str]:
    """Fallback for tickers where MS doesn't carry the calendar (or the
    snapshot is stale). yfinance's `earnings_dates` attribute contains
    1-2 forward dates for most names."""
    try:
        from src.providers._yf import yf
    except ImportError:
        return None
    try:
        t = yf.Ticker(ticker)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return None
        # Filter to future dates.
        future = ed[ed.index.date >= TODAY]
        if future.empty:
            return None
        first = future.index.min()
        return first.date().isoformat()
    except Exception as exc:
        log.debug("yfinance lookup failed for %s: %s", ticker, exc)
        return None


# ────────────────────── QUARTER LABEL FROM DATE ─────────────────────

def _next_quarter_label(earnings_date: str, fy_end_month: int) -> str:
    """Derive the quarter being reported from the announcement date.
    For calendar-year companies (fy_end_month=12): announce in Jan-Apr
    → Q4 prior year; May-Jul → Q1; Aug-Oct → Q2; Nov-Dec → Q3. For
    March-FY-end companies (India, fy_end_month=3): the offset is
    different by 3 months."""
    try:
        d = datetime.fromisoformat(earnings_date).date()
    except (ValueError, TypeError):
        return ""
    yr = d.year
    if fy_end_month == 3:
        # Indian FY: April-Mar. Announcement Apr-Jul → Q4 prior FY ends
        # Mar yr; Aug-Oct → Q1 (FY ends Mar yr+1); etc.
        if 4 <= d.month <= 7:
            return f"Q4 FY{yr % 100:02d}"
        if 8 <= d.month <= 10:
            return f"Q1 FY{(yr + 1) % 100:02d}"
        if 11 <= d.month or d.month <= 1:
            return f"Q2 FY{(yr + 1) % 100:02d}"
        if 2 <= d.month <= 3:
            return f"Q3 FY{yr % 100:02d}"
    else:
        # Calendar FY (Dec).
        if 1 <= d.month <= 4:
            return f"Q4 {yr - 1}"
        if 5 <= d.month <= 7:
            return f"Q1 {yr}"
        if 8 <= d.month <= 10:
            return f"Q2 {yr}"
        if 11 <= d.month <= 12:
            return f"Q3 {yr}"
    return ""


# ────────────────────── BUILD ────────────────────────────────────────

def build_upcoming(horizon_days: int = HORIZON_DAYS) -> dict:
    if not REGISTRY_PATH.is_file():
        raise SystemExit(
            f"Ticker registry not found: {REGISTRY_PATH}. "
            "Run scripts/build_ticker_registry.py first."
        )
    recs = json.loads(REGISTRY_PATH.read_text())
    upcoming: list[dict] = []
    cutoff = TODAY + timedelta(days=horizon_days)

    for r in recs:
        if not r.get("active", True): continue
        # DRs use the underlying's calendar. For now, only generate decks
        # for canonical non-DR entries — the analyst can manually pull a
        # DR-flavored deck via its underlying ticker.
        if not r.get("is_canonical"): continue
        if r.get("is_depositary_receipt"): continue

        ticker = r["ticker"]
        # Tier 1: MS snapshot
        date = _next_earnings_from_ms(ticker)
        source = "marketscreener" if date else None

        # Tier 2: yfinance (only for non-GCC where MS coverage is partial)
        if not date and r.get("providers", {}).get("yfinance") in ("supported", "partial"):
            date = _next_earnings_from_yfinance(ticker)
            source = "yfinance" if date else None

        if not date: continue
        try:
            ed_dt = datetime.fromisoformat(date).date()
        except (ValueError, TypeError):
            continue
        days_until = (ed_dt - TODAY).days
        if not (0 <= days_until <= horizon_days):
            continue

        upcoming.append({
            "ticker": ticker,
            "company_name": r["company_name"],
            "template_family": r["template_family"],
            "exchange_country": r["exchange_country"],
            "earnings_date": date,
            "earnings_date_source": source,
            "days_until": days_until,
            "next_quarter_label": _next_quarter_label(
                date, r.get("fiscal_year_end_month", 12)),
            "fiscal_year_end_month": r.get("fiscal_year_end_month", 12),
            "market_cap_usd": r.get("market_cap_usd"),
        })

    upcoming.sort(key=lambda u: u["days_until"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizon_days": horizon_days,
        "tickers": upcoming,
    }


def main():
    out = build_upcoming()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote {OUT_PATH} ({len(out['tickers'])} tickers in next {out['horizon_days']}d)")
    for t in out["tickers"][:20]:
        print(f"  [{t['days_until']:>3}d]  {t['ticker']:<14}  "
              f"{t['next_quarter_label']:<10}  via {t['earnings_date_source']}  "
              f"({t['company_name'][:45]})")


if __name__ == "__main__":
    main()
