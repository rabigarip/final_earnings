"""
Stage 1 coverage probe runner.

Drives the panel of 10 tickers through every wired provider × every
field, writes the coverage matrix + a summary table. Idempotent: each
run appends to the CSV with a fresh timestamp, but the cache prevents
duplicate network calls within a TTL window.

Run:
    python -m scripts.probe_sources                  # all providers
    python -m scripts.probe_sources --only yahoo,macro  # subset
    python -m scripts.probe_sources --tickers 2222.SR,0700.HK
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow running as a script as well as a module.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.probe_harness import (
    FIELDS, PANEL, write_coverage_row, summarize,
)
from src.storage.db import init_db, seed_companies


def _load_providers(only: set[str] | None):
    """Lazy-import provider modules so a missing optional dep
    (e.g. Playwright for Investing) doesn't kill unrelated providers."""
    providers = {}
    if not only or "yahoo" in only:
        try:
            from src.providers.probe_yahoo import YahooProvider
            providers["yahoo"] = YahooProvider()
        except Exception as exc:
            print(f"  [skip] yahoo: {exc}", file=sys.stderr)
    if not only or "marketscreener" in only:
        try:
            from src.providers.probe_marketscreener import MarketScreenerProvider
            providers["marketscreener"] = MarketScreenerProvider()
        except Exception as exc:
            print(f"  [skip] marketscreener: {exc}", file=sys.stderr)
    if not only or "macro" in only:
        try:
            from src.providers.probe_macro import MacroProvider
            providers["macro"] = MacroProvider()
        except Exception as exc:
            print(f"  [skip] macro: {exc}", file=sys.stderr)
    # iShares: regional ETF proxy returns for emerging-market overlays.
    # Opt-in (--only ishares) by default.
    if "ishares" in (only or set()):
        try:
            from src.providers.probe_ishares import iSharesProvider
            providers["ishares"] = iSharesProvider()
        except Exception as exc:
            print(f"  [skip] ishares: {exc}", file=sys.stderr)
    # Bloomberg consensus is now sourced from the per-ticker upload flow
    # (src/services/bloomberg_parser.py reads <TICKER>_cons_q.xlsx +
    # <TICKER>_FA.xlsx from data/bloomberg/). The legacy probe_bloomberg
    # CSV reader was removed.
    if not only or "commodities" in only:
        try:
            from src.providers.probe_commodities import CommoditiesProvider
            providers["commodities"] = CommoditiesProvider()
        except Exception as exc:
            print(f"  [skip] commodities: {exc}", file=sys.stderr)
    # Investing.com: HTTP-only via curl_cffi (Cloudflare-bypass through
    # Chrome TLS impersonation). Cheap to run, default-on. The provider
    # short-circuits on tickers with no curated slug.
    if not only or "investing" in only:
        try:
            from src.providers.probe_investing import InvestingProvider
            providers["investing"] = InvestingProvider()
        except Exception as exc:
            print(f"  [skip] investing: {exc}", file=sys.stderr)
    if "ir_pdf" in (only or set()):  # only on explicit opt-in (needs cached PDFs)
        try:
            from src.providers.probe_ir_pdf import IRPDFProvider
            providers["ir_pdf"] = IRPDFProvider()
        except Exception as exc:
            print(f"  [skip] ir_pdf: {exc}", file=sys.stderr)
    return providers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="Comma-separated provider names to include")
    ap.add_argument("--tickers", help="Comma-separated tickers to probe (defaults to PANEL)")
    ap.add_argument("--fields", help="Comma-separated fields to probe (defaults to all)")
    ap.add_argument("--out", default="outputs/coverage_matrix.csv",
                    help="Coverage matrix CSV path")
    args = ap.parse_args()

    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else PANEL
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else FIELDS

    print(f"Tickers: {len(tickers)}")
    print(f"Fields:  {len(fields)}")

    init_db()
    seed_companies()  # ensures the 3 new entries land in the DB

    providers = _load_providers(only)
    print(f"Providers wired: {list(providers.keys())}\n")

    csv_path = Path(args.out)
    if csv_path.exists():
        csv_path.unlink()  # fresh file per run

    total = len(tickers) * len(providers) * len(fields)
    done = 0
    t0 = time.monotonic()

    for ticker in tickers:
        for pname, provider in providers.items():
            for field in fields:
                cell = provider.fetch(ticker, field)
                write_coverage_row(csv_path, cell)
                done += 1
                status = "ok" if (cell.value and not cell.error) else (
                    "ni" if cell.error == "not_implemented" else "miss"
                )
                # One terse line per cell so the runner shows progress.
                short_val = (str(cell.value)[:40] + "…") if cell.value and len(str(cell.value)) > 40 else cell.value
                print(f"[{done}/{total}] {ticker:14s} {pname:14s} {field:30s} {status:4s} {short_val if status=='ok' else cell.error}")

    elapsed = time.monotonic() - t0
    print(f"\nDone in {elapsed:.1f}s. Wrote {done} cells to {csv_path}")

    # Print the summary matrix.
    summary = summarize(csv_path)
    print("\n=== Coverage summary (hit / miss / not_impl / err) ===")
    fields_order = fields
    provs = sorted({p for (p, _f) in summary.keys()})
    # Header
    print(f"{'field':32s} " + " ".join(f"{p:^16s}" for p in provs))
    for field in fields_order:
        row = []
        for p in provs:
            s = summary.get((p, field), {"hit": 0, "miss": 0, "ni": 0, "err": 0})
            row.append(f"{s['hit']:>2}/{s['miss']:>2}/{s['ni']:>2}/{s['err']:>2}")
        print(f"{field:32s} " + " ".join(f"{r:^16s}" for r in row))


if __name__ == "__main__":
    main()
