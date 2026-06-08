"""
Backfill MarketScreener slug + URL in SQLite via ISIN search + validation.

Uses the same path as the pipeline: resolve_marketscreener_by_isin → validate_candidate_page
→ update_company_marketscreener (see src/services/entity_resolution.py).

Does not run Yahoo or the full preview; only MS entity resolution.

Usage (from repo root, venv active):

  # Show first 20 tickers that would be attempted (no network writes to DB)
  python -m scripts.backfill_marketscreener_from_isin --dry-run --limit 20

  # Actually write successful resolutions to the DB (rate-limited)
  python -m scripts.backfill_marketscreener_from_isin --limit 50 --delay 1.5

  # Single ticker
  python -m scripts.backfill_marketscreener_from_isin --ticker 2010.SR

  # Retry even rows that already have marketscreener_status=ok
  python -m scripts.backfill_marketscreener_from_isin --force --limit 5 --dry-run

  # After a successful run, merge slugs from DB into data/company_master.json
  python -m scripts.backfill_marketscreener_from_isin --limit 100 --merge-json

  # Export successes to a JSON file (for review or manual merge)
  python -m scripts.backfill_marketscreener_from_isin --dry-run --limit 10 --export successes.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_MASTER = ROOT / "data" / "company_master.json"


def _load_tickers_from_json(path: Path) -> list[str]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [r["ticker"] for r in rows if r.get("ticker")]


def _dry_run_one(ticker: str) -> tuple[str, str | None, str | None]:
    """Returns (status, slug_or_none, detail). status in ok|no_isin|no_match|invalid|low_confidence|skip_ok."""
    from src.providers.marketscreener_pages import resolve_marketscreener_by_isin
    from src.services.entity_resolution import (
        CONFIDENCE_THRESHOLD_ACCEPT,
        validate_candidate_page,
    )
    from src.storage.db import load_company

    row = load_company(ticker)
    if not row:
        return "no_row", None, "ticker not in DB (run: python -m src.main --init-db)"
    isin = (row.get("isin") or "").strip()
    if not isin:
        return "no_isin", None, ""

    ms_id = (row.get("marketscreener_id") or "").strip()
    st = (row.get("marketscreener_status") or "").strip().lower()
    if ms_id and st == "ok":
        return "skip_ok", ms_id, "already has mapping with status ok"

    resolved = resolve_marketscreener_by_isin(isin)
    if not resolved:
        return "no_match", None, "MarketScreener search returned no equity row for ISIN"

    slug, company_url = resolved
    vr = validate_candidate_page(
        dict(row),
        slug,
        company_url,
        cache_name=f"backfill_dry_{ticker.replace('.', '_')}",
    )
    if not vr.valid:
        return "invalid", slug, vr.rejection_reason or "validation_failed"
    if vr.confidence < CONFIDENCE_THRESHOLD_ACCEPT:
        return "low_confidence", slug, f"confidence {vr.confidence:.2f} < {CONFIDENCE_THRESHOLD_ACCEPT}"
    return "ok", slug, f"would write slug={slug}"


def _should_process(ticker: str, force: bool) -> tuple[bool, str | None]:
    from src.storage.db import load_company

    row = load_company(ticker)
    if not row:
        return False, None
    if not (row.get("isin") or "").strip():
        return False, None
    if force:
        return True, None
    ms_id = (row.get("marketscreener_id") or "").strip()
    st = (row.get("marketscreener_status") or "").strip().lower()
    if ms_id and st == "ok":
        return False, None
    return True, None


def _merge_json_from_db(tickers_updated: list[str], master_path: Path) -> None:
    from src.storage.db import load_company

    if not tickers_updated:
        print("merge-json: nothing to merge.")
        return
    want = set(tickers_updated)
    data = json.loads(master_path.read_text(encoding="utf-8"))
    n = 0
    for i, r in enumerate(data):
        t = r.get("ticker")
        if not t or t not in want:
            continue
        row = load_company(t)
        if not row:
            continue
        slug = (row.get("marketscreener_id") or "").strip()
        st = (row.get("marketscreener_status") or "").strip().lower()
        if not slug or st != "ok":
            continue
        if (r.get("marketscreener_id") or "").strip() == slug:
            continue
        data[i]["marketscreener_id"] = slug
        n += 1
    tmp = master_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(master_path)
    print(f"merge-json: updated marketscreener_id for {n} rows in {master_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill MarketScreener slugs from ISIN (DB + optional JSON merge).")
    ap.add_argument("--dry-run", action="store_true", help="Resolve + validate only; do not write DB")
    ap.add_argument("--limit", type=int, default=0, metavar="N", help="Max tickers to process (0 = no limit)")
    ap.add_argument("--delay", type=float, default=1.25, help="Seconds between tickers (rate limit; default 1.25)")
    ap.add_argument("--force", action="store_true", help="Include rows that already have status ok + slug")
    ap.add_argument("--ticker", action="append", default=[], help="Only this ticker (repeatable)")
    ap.add_argument("--master", type=Path, default=DEFAULT_MASTER, help="Path to company_master.json for ticker order")
    ap.add_argument("--merge-json", action="store_true", help="After run, patch data/company_master.json from DB for updated tickers")
    ap.add_argument("--export", type=Path, metavar="FILE", help="Write JSON list of {ticker, slug, status, detail}")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    from src.services.entity_resolution import ensure_marketscreener_cached
    from src.storage.db import load_company, _db_path

    if not _db_path().exists():
        print("Database missing; run: python -m src.main --init-db", file=sys.stderr)
        return 1

    if args.ticker:
        tickers = [t.strip().upper() for t in args.ticker if t.strip()]
    else:
        tickers = _load_tickers_from_json(args.master)

    # Filter + order
    to_run: list[str] = []
    for t in tickers:
        ok, _ = _should_process(t, args.force)
        if not ok:
            continue
        to_run.append(t)
        if args.limit and len(to_run) >= args.limit:
            break

    print(f"Candidates (after filter, limit={args.limit or 'none'}): {len(to_run)}")
    if not to_run:
        print("Nothing to do. Use --force to retry ok rows, or ensure DB is seeded and ISINs exist.")
        return 0

    results: list[dict] = []
    updated: list[str] = []

    for i, ticker in enumerate(to_run):
        if i > 0 and args.delay > 0:
            time.sleep(args.delay)

        if args.dry_run:
            status, slug, detail = _dry_run_one(ticker)
            line = f"{ticker}\t{status}"
            if slug:
                line += f"\t{slug}"
            if detail:
                line += f"\t{detail}"
            print(line)
            results.append({"ticker": ticker, "slug": slug, "status": status, "detail": detail})
            continue

        before = load_company(ticker)
        before_slug = (before or {}).get("marketscreener_id") or ""
        before_st = ((before or {}).get("marketscreener_status") or "").lower()
        ensure_marketscreener_cached(ticker)
        after = load_company(ticker)
        after_slug = (after or {}).get("marketscreener_id") or ""
        after_st = ((after or {}).get("marketscreener_status") or "").lower()

        if after_st == "ok" and after_slug:
            changed = after_slug != before_slug or before_st != "ok"
            status = "updated" if changed else "unchanged_ok"
            print(f"{ticker}\t{status}\t{after_slug}")
            if changed:
                updated.append(ticker)
        else:
            reason = (after or {}).get("marketscreener_rejection_reason") or after_st or "failed"
            print(f"{ticker}\tfailed\t{reason[:120]}")
            status = "failed"
        detail_out = ""
        if status == "failed":
            detail_out = (after or {}).get("marketscreener_rejection_reason") or after_st or ""
        results.append({
            "ticker": ticker,
            "slug": after_slug if after else None,
            "status": status,
            "detail": detail_out,
        })

    if args.export:
        args.export.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {args.export}")

    if args.merge_json and not args.dry_run and updated:
        _merge_json_from_db(updated, args.master)
    elif args.merge_json and args.dry_run:
        print("merge-json skipped (--dry-run)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
