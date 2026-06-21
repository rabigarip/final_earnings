"""Resolve MarketScreener slugs for ISIN-carrying names that lack one, and
persist them into data/company_master.json.

China A-shares (Shanghai .SS / Shenzhen .SZ) ship with an ISIN but no
marketscreener_id, so the MS provider can't find their page — and on Render
the live ISIN search is Cloudflare-blocked, so it can never self-resolve.
This script runs from a NON-blocked IP (locally / GitHub Action) with
MS_USE_CURL_CFFI=1, resolves each name's slug from its ISIN (a globally
unique identifier — a single ISIN hit is authoritative), verifies the page
actually carries financial data, and writes the slug to the committed
company_master so Render knows it without a live lookup.

Run the MS snapshot capture (scripts.refresh_marketscreener_cache) afterward
so the committed snapshots back it on Render.

Usage:
    MS_USE_CURL_CFFI=1 python -m scripts.resolve_china_ms_slugs --china
    MS_USE_CURL_CFFI=1 python -m scripts.resolve_china_ms_slugs --tickers 000063.SZ,600036.SS
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CM_PATH = ROOT / "data" / "company_master.json"


def _verify(slug: str, isin: str, symbol: str = "") -> bool:
    """Identity + data check. The ISIN search can fall through to a trending
    sidebar link (e.g. SpaceX) when MS doesn't index an ISIN, so we REQUIRE
    the resolved page to actually carry the ISIN (authoritative identity) AND
    real financial rows. This rejects wrong-entity matches the resolver alone
    would accept."""
    from curl_cffi import requests as creq
    try:
        r = creq.get(f"https://www.marketscreener.com/quote/stock/{slug}/",
                     impersonate="chrome", timeout=25)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    # Identity gate: the page must carry the ISIN, OR (for A-shares whose
    # ISIN MS doesn't index) the exchange symbol, e.g. "(000333)" in the
    # title. Both are strong, unique identifiers.
    has_isin = bool(isin) and isin in r.text
    has_symbol = bool(symbol) and (f"({symbol})" in r.text or f"({symbol}." in r.text)
    if not (has_isin or has_symbol):
        return False
    try:
        rf = creq.get(f"https://www.marketscreener.com/quote/stock/{slug}/finances/",
                      impersonate="chrome", timeout=25)
        txt = rf.text if rf.status_code == 200 else r.text
    except Exception:
        txt = r.text
    markers = sum(1 for m in ("Net sales", "EBITDA", "Net income", "EPS", "Net Debt") if m in txt)
    return markers >= 2


def _resolve_one(ticker: str, isin: str, company_name: str):
    """Resolve a verified slug: ISIN (authoritative) → exchange symbol →
    company name. Verify identity (ISIN or symbol on page) before accepting."""
    from src.providers.marketscreener_pages import (
        resolve_marketscreener_by_isin, resolve_slug_from_search,
    )
    symbol = ticker.split(".")[0].lstrip("0") or ticker.split(".")[0]
    symbol_raw = ticker.split(".")[0]
    try:
        res = resolve_marketscreener_by_isin(isin)
        slug = res[0] if res else None
    except Exception:
        slug = None
    if slug and _verify(slug, isin, symbol_raw):
        return slug
    # ISIN not indexed on MS — the numeric A-share code resolves these
    # (000333 → Midea, 000338 → Weichai) where the ISIN search fell through.
    try:
        slug_s = resolve_slug_from_search(symbol_raw, company_name=company_name)
    except Exception:
        slug_s = None
    if slug_s and _verify(slug_s, isin, symbol_raw):
        return slug_s
    # Finally, by name.
    try:
        slug2 = resolve_slug_from_search(ticker, company_name=company_name)
    except Exception:
        slug2 = None
    if slug2 and _verify(slug2, isin, symbol_raw):
        return slug2
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--china", action="store_true",
                    help="All Shanghai (.SS) + Shenzhen (.SZ) names missing a slug")
    ap.add_argument("--tickers", help="Comma-separated explicit ticker list")
    ap.add_argument("--delay", type=float, default=4.0, help="Seconds between names")
    ap.add_argument("--limit", type=int, default=0, help="Cap names this run (0 = all)")
    args = ap.parse_args()

    if os.environ.get("MS_USE_CURL_CFFI") != "1":
        print("WARNING: MS_USE_CURL_CFFI != 1 — MarketScreener will likely 403. "
              "Re-run with MS_USE_CURL_CFFI=1.", file=sys.stderr)

    from src.providers.marketscreener_pages import resolve_marketscreener_by_isin

    companies = json.loads(_CM_PATH.read_text(encoding="utf-8"))
    by_ticker = {c.get("ticker"): c for c in companies}

    if args.tickers:
        targets = [t.strip() for t in args.tickers.split(",") if t.strip()]
    elif args.china:
        targets = [c["ticker"] for c in companies
                   if c.get("ticker", "").endswith((".SS", ".SZ"))
                   and not (c.get("marketscreener_id") or "").strip()
                   and (c.get("isin") or "").strip() not in ("", "noisin")]
    else:
        print("Pass --china or --tickers."); return 2
    if args.limit:
        targets = targets[:args.limit]

    print(f"Resolving MS slugs for {len(targets)} names (delay {args.delay}s)...")
    resolved = skipped = 0
    for i, tk in enumerate(targets, 1):
        c = by_ticker.get(tk)
        isin = (c.get("isin") if c else "") or ""
        if not c or not isin.strip() or isin == "noisin":
            print(f"  [{i}/{len(targets)}] {tk:12s} -> SKIP (no isin)"); skipped += 1; continue
        if (c.get("marketscreener_id") or "").strip():
            print(f"  [{i}/{len(targets)}] {tk:12s} -> already has slug"); continue
        slug = _resolve_one(tk, isin.strip(), c.get("company_name") or "")
        if slug:
            c["marketscreener_id"] = slug
            resolved += 1
            print(f"  [{i}/{len(targets)}] {tk:12s} -> {slug}")
        else:
            skipped += 1
            print(f"  [{i}/{len(targets)}] {tk:12s} -> unresolved (ISIN+name search missed/identity-failed)")
        # Flush periodically so a long (rate-limit-prone) batch survives an
        # interruption — resolved slugs are persisted as we go.
        if i % 10 == 0:
            _CM_PATH.write_text(json.dumps(companies, indent=2, ensure_ascii=False), encoding="utf-8")
        if i < len(targets):
            time.sleep(args.delay)

    # Persist in place (preserve list order/formatting reasonably).
    _CM_PATH.write_text(json.dumps(companies, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResolved {resolved}, skipped {skipped}. Wrote {_CM_PATH}.")
    print("Next: MS_USE_CURL_CFFI=1 python -m scripts.refresh_marketscreener_cache "
          "--tickers <resolved> ; then commit company_master.json + data/marketscreener/.")
    return 0 if resolved > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
