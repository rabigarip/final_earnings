"""
Bulk-add or update companies in data/company_master.json from a CSV.

Use this when you have hundreds or thousands of rows (e.g. from a data export
or screener). Required columns: ticker, company_name, exchange, country, currency.
Optional: company_name_long, isin, marketscreener_id, zawya_slug, sector, industry,
is_bank, notes. MarketScreener slugs can be left empty and filled by the pipeline
via ISIN-based resolution when you run a report.

Usage:
  # Merge CSV into existing company_master.json (existing tickers updated, new added)
  python -m scripts.seed_company_master data/new_companies.csv

  # Dry run: show what would be merged, no file write
  python -m scripts.seed_company_master data/new_companies.csv --dry-run

  # Append only: add new tickers, never overwrite existing
  python -m scripts.seed_company_master data/new_companies.csv --append-only

  # Output to a different file (then manually replace or merge)
  python -m scripts.seed_company_master data/new_companies.csv -o data/company_master_merged.json

CSV format:
  ticker,company_name,exchange,country,currency,isin,company_name_long,marketscreener_id,zawya_slug,sector,industry,is_bank,notes
  ̂2222.SR,Saudi Aramco,Tadawul,SA,SAR,SA14TG012N13,Saudi Arabian Oil Company,ARAMCO-103505448,,Energy,Oil & Gas,0,

Header is required. Order of columns does not matter; missing optional columns are filled with "" or false.
"""

from __future__ import annotations
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MASTER = ROOT / "data" / "company_master.json"

REQUIRED = {"ticker", "company_name", "exchange", "country", "currency"}
OPTIONAL = {
    "company_name_long", "isin", "marketscreener_id", "zawya_slug",
    "sector", "industry", "is_bank", "notes",
}
ALL_COLS = REQUIRED | OPTIONAL


def _norm_bool(v: str) -> bool:
    if v is None or v == "":
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def row_to_company(raw: dict) -> dict:
    """Convert a CSV row (dict) to company_master entry shape."""
    t = raw.get("ticker", "").strip()
    if not t:
        return None
    return {
        "ticker": t,
        "company_name": (raw.get("company_name") or "").strip() or t,
        "company_name_long": (raw.get("company_name_long") or "").strip(),
        "exchange": (raw.get("exchange") or "").strip(),
        "country": (raw.get("country") or "").strip(),
        "currency": (raw.get("currency") or "").strip(),
        "isin": (raw.get("isin") or "").strip(),
        "marketscreener_id": (raw.get("marketscreener_id") or "").strip(),
        "zawya_slug": (raw.get("zawya_slug") or "").strip(),
        "sector": (raw.get("sector") or "").strip(),
        "industry": (raw.get("industry") or "").strip(),
        "is_bank": _norm_bool(raw.get("is_bank")),
        "notes": (raw.get("notes") or "").strip(),
    }


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return []
    missing = REQUIRED - set(rows[0].keys())
    if missing:
        raise SystemExit(f"CSV missing required columns: {missing}. Required: {REQUIRED}")
    return rows


def load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge(existing: list[dict], from_csv: list[dict], append_only: bool) -> list[dict]:
    by_ticker = {c["ticker"]: c for c in existing}
    for row in from_csv:
        company = row_to_company(row)
        if company is None:
            continue
        ticker = company["ticker"]
        if append_only and ticker in by_ticker:
            continue
        by_ticker[ticker] = company
    return sorted(by_ticker.values(), key=lambda c: c["ticker"])


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Merge CSV into company_master.json")
    ap.add_argument("csv_path", type=Path, help="Path to CSV file")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Output JSON path (default: data/company_master.json)")
    ap.add_argument("--dry-run", action="store_true", help="Print merge result only, do not write")
    ap.add_argument("--append-only", action="store_true", help="Only add new tickers; do not overwrite existing")
    args = ap.parse_args()

    csv_path = args.csv_path if args.csv_path.is_absolute() else ROOT / args.csv_path
    if not csv_path.exists():
        print(f"Error: CSV not found: {csv_path}")
        return 1

    out_path = args.output or DEFAULT_MASTER
    if not out_path.is_absolute():
        out_path = ROOT / out_path

    rows = load_csv(csv_path)
    companies_from_csv = [row_to_company(r) for r in rows]
    companies_from_csv = [c for c in companies_from_csv if c is not None]
    if not companies_from_csv:
        print("No valid rows in CSV (need at least ticker + company_name + exchange + country + currency)")
        return 1

    existing = load_existing(out_path)
    merged = merge(existing, rows, append_only=args.append_only)

    if args.dry_run:
        print(json.dumps(merged, indent=2, ensure_ascii=False))
        print(f"\n[dry-run] Would write {len(merged)} companies to {out_path}", file=sys.stderr)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(merged)} companies to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
