"""Deck-readiness scorecard CLI.

Scores every ticker (or one) by data availability so you can see which of
the ~500 names can produce a BKMB-grade deck and what each one still
needs. Offline — reads the registry, disclosed IR files, cached provider
snapshots and the peer set. Run on the box where the snapshots live.

    python -m scripts.score_universe                 # whole universe
    python -m scripts.score_universe --ticker BKMB.OM
    python -m scripts.score_universe --tier A        # only Ready tickers
    python -m scripts.score_universe --csv out.csv   # full table to CSV
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.deck_scorecard import score_ticker, score_universe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", help="Score a single ticker (verbose).")
    ap.add_argument("--tier", help="Filter the table to a tier letter (A/B/C/D).")
    ap.add_argument("--csv", help="Write the full per-ticker table to this CSV.")
    ap.add_argument("--top", type=int, default=40, help="Rows to print (default 40).")
    args = ap.parse_args()

    if args.ticker:
        r = score_ticker(args.ticker)
        print(f"\n{r['ticker']}  —  {r['company']}")
        print(f"  Sector family : {r['sector_family']}")
        print(f"  Score         : {r['score']}/100   →   {r['tier']}")
        print(f"  Components     :")
        for k, v in r["components"].items():
            print(f"      {k:18} {v}")
        if r["missing"]:
            print(f"  To reach A (missing):")
            for m in r["missing"]:
                print(f"      - {m}")
        else:
            print("  Nothing missing — deck-ready.")
        return 0

    res = score_universe()
    print(f"\nScored {res['n']} tickers\n")
    print("Tier distribution:")
    for tier in sorted(res["summary"]):
        n = res["summary"][tier]
        print(f"  {tier:14} {n:4}  ({n / res['n'] * 100:4.1f}%)")

    rows = res["rows"]
    if args.tier:
        rows = [r for r in rows if r["tier"].startswith(args.tier.upper())]

    print(f"\n{'TICKER':14}{'SCORE':>6}  {'TIER':12} {'SECTOR':12} TOP GAPS")
    print("-" * 92)
    for r in rows[: args.top]:
        gaps = "; ".join(r["missing"][:2]) if r["missing"] else "—"
        print(f"{r['ticker']:14}{r['score']:>6}  {r['tier']:12} "
              f"{r['sector_family']:12} {gaps[:46]}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker", "company", "sector_family", "score", "tier", "missing"])
            for r in res["rows"]:
                w.writerow([r["ticker"], r["company"], r["sector_family"],
                            r["score"], r["tier"], " | ".join(r["missing"])])
        print(f"\nWrote {res['n']} rows → {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
