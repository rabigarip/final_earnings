"""
Smoke-test driver: render the new Jabal Slide 1 from canonical_store.

Usage:
    python -m scripts.render_jabal_preview --ticker 2222.SR
    python -m scripts.render_jabal_preview --ticker BKMB.OM --period "Q1 2026 Update"

If the canonical_store has no rows for the ticker, the script runs a
quick daily-cadence refresh first so the slide has something to draw.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.canonical_store import get_all_fields
from src.services.render_jabal_snapshot import (
    render_snapshot_slide, build_snapshot_data,
)
from src.services.render_jabal_thesis import (
    render_thesis_slide, build_thesis_data,
)
from src.services.render_jabal_valuation import (
    render_valuation_slide, build_valuation_data,
)
from src.services.jabal_design_tokens import PAGE_W_IN, PAGE_H_IN


def _bootstrap_if_empty(ticker: str, providers: str):
    """If canonical_store has no rows for the ticker, run a quick refresh
    across every cadence so the deck has rating/target/forward data too."""
    cv = get_all_fields(ticker)
    if cv and len(cv) >= 8:
        return
    print(f"[bootstrap] canonical_store thin for {ticker}; refreshing all cadences...")
    import subprocess
    for cadence in ("daily", "weekly", "quarterly"):
        subprocess.run(
            [sys.executable, "-m", "scripts.daily_refresh",
             f"--cadence={cadence}", f"--tickers={ticker}",
             f"--only={providers}"],
            check=False,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--period", default="Q2 2026 Earnings Preview")
    ap.add_argument("--report-date", default="TBA")
    ap.add_argument("--analyst", default="Jabal Research")
    ap.add_argument("--providers", default="yahoo,marketscreener,macro",
                    help="Providers to refresh from if canonical_store is empty")
    ap.add_argument("--out", help="Output PPTX path (default: outputs/<ticker>_jabal_slide1.pptx)")
    args = ap.parse_args()

    _bootstrap_if_empty(args.ticker, args.providers)

    snap = build_snapshot_data(
        args.ticker, analyst_name=args.analyst,
        period_label=args.period, report_date=args.report_date,
    )
    # Mirror generate_report's wiring so the smoke test exercises the real
    # is_bank + period_heading code paths.
    period_heading = "Earnings Expectations"
    if args.period and " Earnings " in args.period:
        period_heading = f"{args.period.split(' Earnings ', 1)[0]} Earnings Expectations"
    is_bank = False
    try:
        from src.storage.db import load_company as _load_company
        _cm = _load_company(args.ticker) or {}
        is_bank = bool(_cm.get("is_bank"))
        if not is_bank:
            ind = (_cm.get("industry") or _cm.get("sector") or "").lower()
            is_bank = ("bank" in ind)
    except Exception:
        pass
    thesis = build_thesis_data(args.ticker, analyst_name=args.analyst,
                                  is_bank=is_bank, period_heading=period_heading)
    valuation = build_valuation_data(args.ticker, analyst_name=args.analyst)

    prs = Presentation()
    prs.slide_width = Inches(PAGE_W_IN)
    prs.slide_height = Inches(PAGE_H_IN)
    render_snapshot_slide(prs, snap)
    render_thesis_slide(prs, thesis)
    render_valuation_slide(prs, valuation)

    out_path = Path(args.out) if args.out else (
        ROOT / "outputs" / f"{args.ticker}_jabal_preview.pptx"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
