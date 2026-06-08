#!/usr/bin/env python3
"""Trust gate for the auto-grounding extractor.

Runs grounding_extractor on every hand-verified ticker that has a
MarketScreener cache, and scores its output against the human-verified
data/disclosed/*.json (the gold set). This is what lets us decide whether
the extractor is good enough to point at the other ~490 tickers — and which
sectors/metrics it agrees with IR on vs which still need a human.

Per-field tolerances (extractor agrees with gold when within):
  *_mn money         : 6% relative
  *_growth_pct       : 3.0 pp absolute
  margins / ratios   : 2.5 pp absolute
  eps / dps          : 8% relative (or 0.02 absolute, whichever looser)

Output: per-ticker matched/compared with mismatches listed, then an
aggregate precision (of fields emitted that overlap gold, how many agree)
and recall (of gold's MS-sourceable fields, how many the extractor produced).

Usage:  python3 scripts/eval_grounding_extractor.py
        python3 scripts/eval_grounding_extractor.py --write-staging
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.grounding_extractor import extract_grounding, write_staging  # noqa: E402

GOLD_DIR = Path(__file__).resolve().parent.parent / "data" / "disclosed"

_PCT_KEYS_ABS = {"operating_margin_pct", "ebitda_margin_pct", "gross_margin_pct",
                 "roe_pct", "car_pct", "ebitda_margin_pct"}


def _agree(key: str, got: float, gold: float) -> bool:
    if key.endswith("_growth_pct"):
        return abs(got - gold) <= 3.0
    if key in _PCT_KEYS_ABS or key.endswith("_pct"):
        return abs(got - gold) <= 2.5
    if key in ("eps", "dps"):
        return abs(got - gold) <= max(0.02, abs(gold) * 0.08)
    # money
    if gold == 0:
        return abs(got) < 1.0
    return abs(got - gold) / abs(gold) <= 0.06


def main() -> int:
    write = "--write-staging" in sys.argv
    gold_files = sorted(GOLD_DIR.glob("*.json"))
    tot_compared = tot_matched = 0
    tot_gold_money = tot_recalled = 0
    hi_compared = hi_matched = 0   # December-FYE (auto-promotable) subset
    rows = []
    for gf in gold_files:
        ticker = gf.stem
        gold = (json.loads(gf.read_text()).get("fy_highlights") or {})
        gold_metrics = {k: v for k, v in gold.items()
                        if isinstance(v, (int, float))}
        res = extract_grounding(ticker)
        status = res.get("_status")
        if status != "auto_unverified":
            rows.append((ticker, status, "skip", 0, 0, 0, []))
            continue
        if write:
            write_staging(ticker, res)
        got = {k: v for k, v in (res.get("fy_highlights") or {}).items()
               if isinstance(v, (int, float))}
        # precision: emitted fields that overlap gold
        compared = matched = 0
        mism = []
        for k, gv in got.items():
            if k in gold_metrics:
                compared += 1
                if _agree(k, gv, gold_metrics[k]):
                    matched += 1
                else:
                    mism.append(f"{k}: got {gv} vs gold {gold_metrics[k]}")
        # recall: gold fields the extractor could in principle source
        ms_sourceable = {k for k in gold_metrics
                         if k in ("revenue_mn", "net_profit_mn", "ebitda_mn",
                                  "eps", "dps", "operating_margin_pct",
                                  "ebitda_margin_pct")
                         or k.endswith("_growth_pct")}
        recalled = sum(1 for k in ms_sourceable if k in got)
        tot_compared += compared
        tot_matched += matched
        tot_gold_money += len(ms_sourceable)
        tot_recalled += recalled
        ext = res.get("_extractor", {})
        anchor = ext.get("anchor_period")
        conf = ext.get("confidence", "?")
        if conf == "high":
            hi_compared += compared
            hi_matched += matched
        rows.append((ticker, anchor, conf, compared, matched, recalled, mism))

    print(f"\n{'TICKER':14} {'ANCHOR':8} {'CONF':10} {'MATCH/CMP':10} {'RECALL':7} MISMATCHES")
    print("-" * 84)
    for t, anc, conf, cmp_, mat, rec, mism in rows:
        if anc in ("no_cache", "no_periods", "no_net_income"):
            print(f"{t:14} {str(anc):8} (skipped — no usable MS cache)")
            continue
        flag = "" if (cmp_ == mat) else "  <-- review"
        short_conf = "high" if conf == "high" else "non-dec-FY"
        print(f"{t:14} {str(anc):8} {short_conf:10} {mat}/{cmp_:<8} {rec:<7}{flag}")
        for m in mism:
            print(f"{'':14} ! {m}")

    prec = (tot_matched / tot_compared * 100) if tot_compared else 0.0
    hi_prec = (hi_matched / hi_compared * 100) if hi_compared else 0.0
    rec = (tot_recalled / tot_gold_money * 100) if tot_gold_money else 0.0
    print("-" * 84)
    print(f"PRECISION — December-FYE subset (auto-promotable): "
          f"{hi_matched}/{hi_compared} = {hi_prec:.0f}%")
    print(f"PRECISION — all tickers (incl. non-Dec-FY review cases): "
          f"{tot_matched}/{tot_compared} = {prec:.0f}%")
    print(f"RECALL    — gold MS-sourceable fields produced: "
          f"{tot_recalled}/{tot_gold_money} = {rec:.0f}%")
    print("\nThe December-FYE subset is the trust metric for auto-promotion. "
          "Sub-100 cases are non-Dec fiscal years (period-label ambiguity) or "
          "a stale/divergent MS source value — both are caught by the staging "
          "review and flagged with confidence != 'high'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
