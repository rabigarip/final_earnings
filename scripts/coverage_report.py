#!/usr/bin/env python3
"""Grounding-coverage cockpit — the dashboard that drives the scale effort.

Answers, for the whole registry universe: how many tickers are deck-ready,
where each one is stuck in the grounding funnel, and exactly what to do next
to move the needle. Reads the scorecard + the staging dir + the MS cache;
writes nothing.

Funnel stages per ticker:
  GROUNDED   data/disclosed/<t>.json exists (human-verified FY actuals)
  PROMOTABLE staging candidate that passes the promotion gate -> run promote
  REVIEW     staging candidate blocked by the gate (why is shown) -> human
  EXTRACT    has an MS cache but no staging yet -> run extract_grounding
  BARE       no MS cache -> needs a snapshot refresh (GHA) before anything

Usage:
  python3 scripts/coverage_report.py            # summary + top queues
  python3 scripts/coverage_report.py --full      # every non-grounded ticker
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.deck_scorecard import score_universe  # noqa: E402
from src.services.grounding_extractor import _resolve_prefix, promotion_gate  # noqa: E402
from src.services.ticker_registry import _registry_index  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DISCLOSED = ROOT / "data" / "disclosed"
STAGING = DISCLOSED / "_staging"


def _classify(ticker: str) -> tuple[str, str]:
    """Return (stage, detail) for one ticker."""
    if (DISCLOSED / f"{ticker}.json").is_file():
        return "GROUNDED", ""
    sp = STAGING / f"{ticker}.json"
    if sp.is_file():
        try:
            staging = json.loads(sp.read_text())
        except Exception:
            return "REVIEW", "unreadable staging file"
        ok, reasons = promotion_gate(staging)
        if ok:
            ext = staging.get("_extractor", {})
            return "PROMOTABLE", f"{ext.get('metrics_emitted', '?')} metrics, {ext.get('anchor_period', '?')}"
        return "REVIEW", "; ".join(reasons)
    if _resolve_prefix(ticker, "income_statement") or _resolve_prefix(ticker, "finances"):
        return "EXTRACT", "MS cache present, not yet extracted"
    return "BARE", "no MS cache"


def main() -> int:
    full = "--full" in sys.argv
    tickers = sorted(_registry_index().keys())

    uni = score_universe(tickers)
    print("\n=== DECK-READINESS (current) ===")
    order = ["A · Ready", "B · Solid", "C · Partial", "D · Thin"]
    for tier in order:
        n = uni["summary"].get(tier, 0)
        bar = "█" * (n * 40 // max(uni["n"], 1))
        print(f"  {tier:12} {n:4}  {bar}")
    print(f"  {'TOTAL':12} {uni['n']:4}")

    buckets: dict[str, list[tuple[str, str]]] = {
        "GROUNDED": [], "PROMOTABLE": [], "REVIEW": [], "EXTRACT": [], "BARE": []}
    for t in tickers:
        stage, detail = _classify(t)
        buckets[stage].append((t, detail))

    print("\n=== GROUNDING FUNNEL ===")
    for stage in ("GROUNDED", "PROMOTABLE", "REVIEW", "EXTRACT", "BARE"):
        print(f"  {stage:11} {len(buckets[stage]):4}")

    if buckets["PROMOTABLE"]:
        print(f"\n=== READY TO PROMOTE NOW ({len(buckets['PROMOTABLE'])}) "
              f"— run: python3 scripts/promote_grounding.py <ticker> ===")
        for t, d in buckets["PROMOTABLE"][:(None if full else 25)]:
            print(f"  {t:14} {d}")

    if buckets["REVIEW"]:
        print(f"\n=== NEED REVIEW ({len(buckets['REVIEW'])}) — gate blocked ===")
        for t, d in buckets["REVIEW"][:(None if full else 15)]:
            print(f"  {t:14} {d}")

    if buckets["EXTRACT"]:
        print(f"\n=== EXTRACTABLE NOW ({len(buckets['EXTRACT'])}) "
              f"— run: python3 scripts/extract_grounding.py --all-cached ===")
        shown = buckets["EXTRACT"] if full else buckets["EXTRACT"][:25]
        print("  " + ", ".join(t for t, _ in shown)
              + ("" if full or len(buckets["EXTRACT"]) <= 25 else f"  …(+{len(buckets['EXTRACT'])-25})"))

    print(f"\n=== BARE ({len(buckets['BARE'])}) — need an MS snapshot refresh "
          f"(GHA) before they can be grounded ===")

    # Next-action one-liner
    print("\nNEXT: "
          + (f"extract {len(buckets['EXTRACT'])} cached → " if buckets["EXTRACT"] else "")
          + (f"promote {len(buckets['PROMOTABLE'])} ready → " if buckets["PROMOTABLE"] else "")
          + (f"review {len(buckets['REVIEW'])} blocked → " if buckets["REVIEW"] else "")
          + (f"refresh snapshots for {len(buckets['BARE'])} bare." if buckets["BARE"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
