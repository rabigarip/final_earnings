#!/usr/bin/env python3
"""Promote an auto-extracted staging candidate to the verified disclosed store.

This is the human-in-the-loop step that turns extractor candidates into real
grounding the deck pipeline trusts. It runs the promotion gate (status,
confidence, sanity, PERIOD CURRENCY vs the reporting calendar), and on pass
writes data/disclosed/<ticker>.json with a `_provenance` block recording how
it was sourced and whether a human reviewed it.

Trust posture:
  default      promote named tickers that pass the gate, marked reviewed=false
               (MS-sourced, structurally validated, NOT IR-verified)
  --reviewed   mark reviewed=true (you eyeballed the numbers vs the IR release)
  --auto       batch-promote EVERY gate-passing staging candidate (reviewed=false)
  --dry-run    show what would happen, write nothing
  --force      promote even if the gate fails / overwrite an existing file

Even reviewed=false promotions are strictly better than a thin deck: the
values pass sanity + period-currency, and the deck's provenance/scorecard
machinery surfaces the 'auto, unverified' status. A later human pass (or a
divergence caught downstream) can flip reviewed=true or correct the file.

Usage:
  python3 scripts/promote_grounding.py EMAAR.AE 2010.SR
  python3 scripts/promote_grounding.py EMAAR.AE --reviewed
  python3 scripts/promote_grounding.py --auto --dry-run
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.grounding_extractor import promotion_gate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DISCLOSED = ROOT / "data" / "disclosed"
STAGING = DISCLOSED / "_staging"


def _to_disclosed(staging: dict, reviewed: bool) -> dict:
    ext = staging.get("_extractor") or {}
    return {
        "ticker": staging.get("ticker"),
        "company": staging.get("company"),
        "currency": staging.get("currency"),
        "units": staging.get("units", "millions"),
        "_provenance": {
            "method": "marketscreener_auto",
            "reviewed": reviewed,
            "promoted_on": date.today().isoformat(),
            "anchor_period": ext.get("anchor_period"),
            "confidence": ext.get("confidence"),
            "field_sources": ext.get("provenance"),
            "needs_ir": ext.get("needs_ir"),
            "note": ("human-reviewed against IR" if reviewed
                     else "auto-extracted from MarketScreener; values not yet "
                          "IR-verified — confirm headline figures before relying"),
        },
        "fy_highlights": staging.get("fy_highlights"),
    }


def _promote_one(ticker: str, *, reviewed: bool, dry: bool, force: bool) -> str:
    sp = STAGING / f"{ticker}.json"
    if not sp.is_file():
        return f"{ticker:14} SKIP — no staging file"
    staging = json.loads(sp.read_text())
    ok, reasons = promotion_gate(staging)
    dest = DISCLOSED / f"{ticker}.json"
    if dest.is_file() and not force:
        return f"{ticker:14} SKIP — already grounded (use --force to overwrite)"
    if not ok and not force:
        return f"{ticker:14} BLOCKED — {'; '.join(reasons)}"
    out = _to_disclosed(staging, reviewed)
    fy = out["fy_highlights"]
    summary = (f"{fy.get('period', '?')} "
               f"np={fy.get('net_profit_mn')} rev={fy.get('revenue_mn')} "
               f"reviewed={reviewed}")
    if dry:
        flag = "" if ok else "  (FORCED past gate)"
        return f"{ticker:14} WOULD PROMOTE — {summary}{flag}"
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sp.unlink()  # consumed
    return f"{ticker:14} PROMOTED → data/disclosed/{ticker}.json — {summary}"


def main() -> int:
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    tickers = [a for a in sys.argv[1:] if not a.startswith("--")]
    reviewed = "--reviewed" in flags
    dry = "--dry-run" in flags
    force = "--force" in flags

    if "--auto" in flags:
        tickers = sorted(p.stem for p in STAGING.glob("*.json"))
        # auto only promotes gate-passing ones (force is ignored for safety)
        force = False
    if not tickers:
        print(__doc__)
        return 1

    promoted = 0
    for t in tickers:
        line = _promote_one(t, reviewed=reviewed, dry=dry, force=force)
        print("  " + line)
        if "PROMOTED" in line:
            promoted += 1

    if not dry and promoted:
        from src.services.deck_scorecard import score_ticker
        print("\nRescored:")
        for t in tickers:
            if (DISCLOSED / f"{t}.json").is_file():
                r = score_ticker(t)
                print(f"  {t:14} {r['score']:>3} {r['tier']}")
    print(f"\n{promoted} promoted. "
          + ("(dry run — nothing written)" if dry else
             "Review reviewed=false files against IR when you can; "
             "commit data/disclosed/*.json to ship."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
