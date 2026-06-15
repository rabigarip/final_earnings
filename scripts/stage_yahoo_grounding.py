"""Stage Yahoo auto-grounding for the universe → data/disclosed/_staging/*.json.

Gives the ~485 names without a hand-verified data/disclosed/*.json a grounded
FY actual (revenue/net-profit/EBITDA/EPS + YoY) the deck/LLM can cite. Output
is flagged `auto_unverified` and is NEVER auto-promoted; a human can review a
staged file and move it into data/disclosed/ to upgrade it to A-grade.

Yahoo is not Cloudflare-blocked, so this also runs fine on Render — but
pre-staging + committing keeps generation fast and offline-reproducible.

Usage:
  python -m scripts.stage_yahoo_grounding --tickers 2010.SR,0700.HK
  python -m scripts.stage_yahoo_grounding --limit 50
  python -m scripts.stage_yahoo_grounding            # whole universe
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from src.services.yahoo_grounding import stage_yahoo_grounding

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", help="Comma-separated subset")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    cm = json.loads((ROOT / "data" / "company_master.json").read_text(encoding="utf-8"))
    hand_verified = {p.stem.upper() for p in (ROOT / "data" / "disclosed").glob("*.json")}

    rows = cm
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        rows = [c for c in cm if (c.get("ticker") or "").upper() in want]

    n_ok = n_skip = n_miss = 0
    for c in rows:
        t = (c.get("ticker") or "").upper()
        if not t:
            continue
        if t in hand_verified:
            n_skip += 1
            continue                      # don't shadow a hand-verified file
        p = stage_yahoo_grounding(t)
        if p:
            n_ok += 1
            print(f"  ✓ {t}")
        else:
            n_miss += 1
            print(f"  · {t} (no usable Yahoo statement)")
        time.sleep(args.delay)
        if args.limit and n_ok >= args.limit:
            break

    print(f"\nStaged {n_ok} (skipped {n_skip} hand-verified, {n_miss} no-data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
