"""
Batch-render Jabal preview decks for every panel ticker.

Designed to run nightly on Render as a cron-job (or locally before a
demo). Output structure:

    static/decks/<TICKER>.pptx        ← downloadable deck
    static/decks/<TICKER>.meta.json   ← metadata (name, sources, last_refreshed,
                                        confidence summary, deck size)
    static/decks/index.json           ← panel-wide listing for /api/jabal/panel

The deck builder reads from `canonical_store`. Before each render we
trigger a quick refresh across daily + weekly + quarterly cadences
(yahoo / marketscreener / macro / ishares / commodities — Investing.com
is optional via --include-investing).

This is the **only** path the hosted site exposes for the panel — when
a consumer clicks a panel ticker they get the static file directly
(zero compute on the request path). Off-panel tickers still go through
the slow `/api/reports` route.

Run:
    python -m scripts.render_panel_decks
    python -m scripts.render_panel_decks --include-investing
    python -m scripts.render_panel_decks --tickers 2222.SR,0700.HK   # subset
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation
from pptx.util import Inches

from src.services.canonical_store import get_all_fields
from src.services.jabal_design_tokens import PAGE_W_IN, PAGE_H_IN
from src.services.probe_harness import PANEL
from src.services.render_jabal_snapshot import (
    render_snapshot_slide, build_snapshot_data,
)
from src.services.render_jabal_thesis import (
    render_thesis_slide, build_thesis_data,
)
from src.services.render_jabal_valuation import (
    render_valuation_slide, build_valuation_data,
)


_OUT_DIR = ROOT / "static" / "decks"


def _refresh(ticker: str, providers: str) -> None:
    """Run daily/weekly/quarterly refresh against `ticker`. Quiet on failure."""
    for cadence in ("daily", "weekly", "quarterly"):
        try:
            subprocess.run(
                [sys.executable, "-m", "scripts.daily_refresh",
                 f"--cadence={cadence}", f"--tickers={ticker}",
                 f"--only={providers}"],
                timeout=180, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            continue


def _render_one(ticker: str, *, period: str | None = None) -> dict:
    """Render a single deck. Returns metadata dict (also written next to PPTX)."""
    cv = get_all_fields(ticker)
    profile = cv.get("company_profile")
    company_name = ticker
    sector = currency = "—"
    if profile and isinstance(profile.value, dict):
        company_name = profile.value.get("name") or ticker
        sector = profile.value.get("sector") or sector
        currency = profile.value.get("currency") or currency

    snap = build_snapshot_data(
        ticker,
        period_label=period or "Earnings Preview",
        report_date="TBA",
    )
    # Mirror generate_report so the panel decks exercise bank schema +
    # dynamic period label.
    period_heading = "Earnings Expectations"
    if period and " Earnings " in period:
        period_heading = f"{period.split(' Earnings ', 1)[0]} Earnings Expectations"
    else:
        # No explicit period passed → derive current next quarter so the
        # slide-2 table header reads e.g. "Q2 2026E" rather than the
        # generic "ESTIMATE".
        from datetime import datetime as _dt
        _now = _dt.now()
        _q = (_now.month - 1) // 3 + 1
        period_heading = f"Q{_q} {_now.year} Earnings Expectations"
    is_bank = False
    try:
        from src.storage.db import load_company as _load_company
        _cm = _load_company(ticker) or {}
        is_bank = bool(_cm.get("is_bank"))
        if not is_bank:
            ind = (_cm.get("industry") or _cm.get("sector") or "").lower()
            is_bank = ("bank" in ind)
    except Exception:
        pass
    thesis = build_thesis_data(ticker, is_bank=is_bank, period_heading=period_heading)
    valuation = build_valuation_data(ticker)

    prs = Presentation()
    prs.slide_width = Inches(PAGE_W_IN)
    prs.slide_height = Inches(PAGE_H_IN)
    render_snapshot_slide(prs, snap)
    render_thesis_slide(prs, thesis)
    render_valuation_slide(prs, valuation)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    pptx_path = _OUT_DIR / f"{ticker}.pptx"
    prs.save(str(pptx_path))

    # Per-cell confidence summary for the consumer's "data quality" footer.
    by_conf = {"High": 0, "Medium": 0, "Low": 0}
    for c in cv.values():
        by_conf[c.confidence] = by_conf.get(c.confidence, 0) + 1
    sources = sorted({c.canonical_source for c in cv.values()})

    meta = {
        "ticker": ticker,
        "company_name": company_name,
        "sector": sector,
        "currency": currency,
        "cells_in_canonical_store": len(cv),
        "by_confidence": by_conf,
        "sources": sources,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "pptx_bytes": pptx_path.stat().st_size,
        "pptx_filename": pptx_path.name,
    }
    (_OUT_DIR / f"{ticker}.meta.json").write_text(
        json.dumps(meta, indent=2, default=str))
    return meta


def _write_index(metas: list[dict]) -> None:
    """Write static/decks/index.json — the directory listing the panel
    endpoint serves to the frontend."""
    index = {
        "panel_size": len(metas),
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "decks": metas,
    }
    (_OUT_DIR / "index.json").write_text(
        json.dumps(index, indent=2, default=str))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="Comma-separated subset (default: full PANEL)")
    ap.add_argument("--include-investing", action="store_true",
                    help="Run Investing.com refresh too (Playwright, slower)")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="Skip the canonical_store refresh; render straight from cache")
    ap.add_argument("--period", default=None,
                    help="Override the slide-1 period label, e.g. 'Q2 2026 Earnings Preview'")
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else list(PANEL)
    providers = "yahoo,marketscreener,macro,ishares,commodities"
    if args.include_investing:
        providers = providers + ",investing"

    print(f"Rendering {len(tickers)} panel decks → {_OUT_DIR}")
    print(f"Providers: {providers}")
    print(f"Refresh: {'SKIPPED' if args.skip_refresh else 'enabled'}")

    metas: list[dict] = []
    t_start = time.monotonic()
    for ticker in tickers:
        print(f"\n=== {ticker} ===")
        if not args.skip_refresh:
            print(f"  refresh ...")
            _refresh(ticker, providers)
        try:
            meta = _render_one(ticker, period=args.period)
            print(f"  rendered: {meta['company_name']} | "
                  f"{meta['cells_in_canonical_store']} cells, "
                  f"sources={meta['sources']}, "
                  f"size={meta['pptx_bytes']:,}B")
            metas.append(meta)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")

    _write_index(metas)
    print(f"\nDone in {time.monotonic() - t_start:.1f}s. "
          f"Index at {_OUT_DIR}/index.json ({len(metas)}/{len(tickers)} decks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
