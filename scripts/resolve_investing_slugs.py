"""Resolve Investing.com slugs for the universe → data/investing_slugs.json.

Investing's search API is Cloudflare-blocked from Render's datacenter IP, so
run this from a NON-BLOCKED IP (locally or GitHub Actions). The runtime reads
the committed cache via probe_investing._resolved_slugs(), giving Investing
coverage to names that have no hand-curated slug (e.g. SABIC 2010.SR).

Each resolution is entity-gated: the search result's numeric symbol must equal
the ticker's base, so we never bind to a same-named REIT/insurer.

Usage:
  python -m scripts.resolve_investing_slugs --tickers 2010.SR,7010.SR
  python -m scripts.resolve_investing_slugs --gulf          # Gulf only (Yahoo-thin)
  python -m scripts.resolve_investing_slugs                 # whole universe
  python -m scripts.resolve_investing_slugs --refresh       # re-resolve cached too
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from src.providers.probe_investing import resolve_investing_slug, _SLUGS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "investing_slugs.json"
GULF_SUFFIXES = {"SR", "QA", "AE", "OM", "KW", "BH"}


def _universe() -> list[dict]:
    return json.loads((ROOT / "data" / "company_master.json").read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", help="Comma-separated subset")
    ap.add_argument("--gulf", action="store_true", help="Only Gulf exchanges")
    ap.add_argument("--delay", type=float, default=1.2, help="Seconds between calls")
    ap.add_argument("--refresh", action="store_true", help="Re-resolve already-cached tickers")
    args = ap.parse_args()

    existing: dict[str, str] = {}
    if OUT.exists():
        try:
            existing = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    rows = _universe()
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        rows = [c for c in rows if (c.get("ticker") or "").upper() in want]
    elif args.gulf:
        rows = [c for c in rows
                if (c.get("ticker") or "").upper().split(".")[-1] in GULF_SUFFIXES]

    n_new = n_miss = 0
    for c in rows:
        t = (c.get("ticker") or "").upper()
        if not t:
            continue
        if t in _SLUGS:
            continue                              # hand-curated already
        if t in existing and not args.refresh:
            continue
        slug = resolve_investing_slug(t, c.get("company_name"), c.get("country"))
        if slug:
            existing[t] = slug
            n_new += 1
            print(f"  ✓ {t:14} -> {slug}")
        else:
            n_miss += 1
            print(f"  · {t:14} -> (no confident match)")
        time.sleep(args.delay)

    OUT.write_text(json.dumps(dict(sorted(existing.items())), indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {len(existing)} slugs ({n_new} new, {n_miss} unresolved) → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
