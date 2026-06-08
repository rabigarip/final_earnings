"""
CLI entry point.

Usage:
    # Initialize database (run once, or after editing company_master.json)
    python -m src.main --init-db

    # Earnings preview — SABIC (industrial, non-bank)
    python -m src.main --ticker 2010.SR --mode preview --skip-llm

    # Earnings preview — Al Rajhi Bank (bank, EBITDA skipped)
    python -m src.main --ticker 1120.SR --mode preview --skip-llm

    # Test failure path — invalid ticker
    python -m src.main --ticker ZZZZ.SR --mode preview

    # Test failure path — valid ticker, not in company master
    python -m src.main --ticker 2350.SR --mode preview

    # With Gemini news summarization (needs GEMINI_API_KEY in .env)
    python -m src.main --ticker 2010.SR --mode preview

Flags:
    --ticker     Yahoo-format ticker symbol (required for preview)
    --mode       "preview" (default) | "calendar"
    --skip-llm   Skip Gemini summarization step
    --init-db    Seed database and exit
"""

from __future__ import annotations
import argparse
import sys


def main() -> None:
    # Load .env if present (for GEMINI_API_KEY etc.)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(
        description="Earnings Research Pipeline — Backend MVP",
    )
    ap.add_argument("--ticker",   type=str, help="Yahoo-format ticker")
    ap.add_argument("--mode",     type=str, default="preview",
                    choices=["preview", "calendar", "batch"])
    ap.add_argument("--skip-llm", action="store_true",
                    help="Skip Gemini summarization")
    ap.add_argument("--tickers", type=str, default="",
                    help="Comma-separated tickers for --mode batch (e.g. 2010.SR,1120.SR)")
    ap.add_argument("--days", type=int, default=14,
                    help="Days ahead for --mode calendar (refreshes then lists matches)")
    ap.add_argument("--run-previews", action="store_true",
                    help="With --mode calendar: also run previews for matching tickers")
    ap.add_argument("--init-db",  action="store_true",
                    help="Initialize DB + seed companies, then exit")
    ap.add_argument("--store-actuals", type=str, default="",
                    help="Store latest quarterly actuals for ticker (e.g. 2010.SR)")
    ap.add_argument("--period", type=str, default="",
                    help="Optional period label for --store-actuals (default: latest quarter label)")
    args = ap.parse_args()

    # ── DB init ───────────────────────────────────────────────
    from src.storage.db import init_db, seed_companies, _db_path

    if args.init_db:
        init_db()
        n = seed_companies()
        print(f"\nDone. {n} companies seeded. DB at {_db_path()}")
        sys.exit(0)

    if args.store_actuals:
        from src.providers.yahoo import fetch_financials
        from src.storage.db import load_company
        from src.services.store_actuals import upsert_actuals
        ticker = args.store_actuals.strip().upper()
        row = load_company(ticker)
        if not row:
            print(f"[store-actuals] ticker not in company_master: {ticker}")
            sys.exit(1)
        from src.models.company import CompanyMaster
        company = CompanyMaster(**row)
        fin = fetch_financials(ticker, company.currency, company.is_bank)
        qs = fin.get("quarterly", []) or []
        if not qs:
            print(f"[store-actuals] no quarterly financials for {ticker}")
            sys.exit(1)
        from src.utils.periods import latest_period
        latest = latest_period(qs)
        period = (args.period or latest.period_label or "").strip()
        if not period:
            print("[store-actuals] period is required")
            sys.exit(1)
        ebitda_margin = (latest.ebitda / latest.revenue * 100) if (latest.ebitda is not None and latest.revenue) else None
        upsert_actuals(
            ticker=ticker,
            period=period,
            revenue=latest.revenue,
            net_income=latest.net_income,
            eps=latest.eps,
            ebitda=latest.ebitda,
            ebitda_margin=ebitda_margin,
            reported_date=None,
        )
        print(f"[store-actuals] upserted {ticker} {period}")
        sys.exit(0)

    # Auto-init on first run
    if not _db_path().exists():
        print("[auto] DB not found — initializing …")
        init_db()
        seed_companies()

    # ── Route ─────────────────────────────────────────────────
    if args.mode == "preview":
        if not args.ticker:
            ap.error("--ticker is required for preview mode")
        from src.pipeline import run_preview
        # Do not delete outputs here. Each run writes a unique run_id-based filename,
        # and deleting can break downloads for recent runs.
        _rid, results = run_preview(args.ticker, skip_llm=args.skip_llm)

        from src.models.step_result import Status
        any_fail = any(r.status == Status.FAILED for r in results)
        sys.exit(1 if any_fail else 0)

    elif args.mode == "calendar":
        # Refresh earnings_calendar via the rate-limited service, then (optionally)
        # run previews for tickers whose next event falls inside --days.
        from datetime import date, timedelta
        from src.services.fetch_calendar import refresh_calendar
        from src.storage.db import list_calendar_events

        print(f"[calendar] refreshing earnings_calendar (days={args.days}) …")
        summary = refresh_calendar()
        print(f"[calendar] {summary}")

        today = date.today()
        end = today + timedelta(days=max(0, int(args.days)))
        events = list_calendar_events(start=today.isoformat(), end=end.isoformat())
        matches = [e["ticker"] for e in events]
        print(f"[calendar] Tickers with earnings within {args.days} days: {len(matches)}")

        if args.run_previews:
            from src.pipeline import run_preview
            for t in matches:
                _rid, _res = run_preview(t, skip_llm=args.skip_llm)

        sys.exit(0)

    elif args.mode == "batch":
        tickers_raw = (args.tickers or "").strip()
        if not tickers_raw:
            ap.error("--tickers is required for --mode batch")
        tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]
        from src.pipeline import run_preview
        any_fail = False
        for t in tickers:
            _rid, results = run_preview(t, skip_llm=args.skip_llm)
            from src.models.step_result import Status
            if any(r.status == Status.FAILED for r in results):
                any_fail = True
        sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
