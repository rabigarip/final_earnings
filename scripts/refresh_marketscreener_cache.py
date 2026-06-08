"""Pre-warm MarketScreener HTML snapshots for every curated slug and commit
them to data/marketscreener/.

WHY THIS EXISTS
---------------
Cloudflare blocks Render's egress IPs from reaching marketscreener.com —
every page returns HTTP 403 in production. Same problem the
data/investing/ snapshot pattern solves for Investing.com.

This script fetches every MS page we use for every curated ticker, then
writes the raw HTML to data/marketscreener/ms_<safe-cache-slug>.html.
The fetch must run from a non-Cloudflare-flagged IP (a developer's
laptop or, in production, a GitHub Actions runner).

`marketscreener_pages._fetch_page` falls back to these snapshots
whenever the live network call fails on Render (HTTP 403 / captcha).

USAGE
-----
    # Refresh every ticker that has a `marketscreener_id` in
    # data/company_master.json (currently 17 tickers, panel + adjacents):
    python -m scripts.refresh_marketscreener_cache

    # Just the 10-ticker panel:
    python -m scripts.refresh_marketscreener_cache --panel

    # A subset:
    python -m scripts.refresh_marketscreener_cache --tickers BKMB.OM,OQEP.OM

    # Be politer to Cloudflare (default 3.0 s between page fetches):
    python -m scripts.refresh_marketscreener_cache --delay 6

OUTPUT
------
data/marketscreener/ms_<ticker>_<page>.html  (one file per page kind)

After a successful run, `git status data/marketscreener/` shows the diff.
Commit + push and Render picks up the new snapshots on the next deck run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


# ── Which MS pages we want to snapshot per ticker ───────────────────
#
# Each entry: (page_name_for_cache_slug, fetcher_function). The fetcher
# functions all take (base_url, cache_key_prefix) and call _fetch_page
# under the hood with cache_slug = f"{cache_key_prefix}_{page_name}",
# so writing the snapshot happens automatically when MS_TRACKED_REFRESH=1.

def _build_page_fetchers():
    """Imported lazily so the script can print a clean error if MS
    parsing deps are missing on the host."""
    from src.providers.marketscreener_pages import (
        fetch_summary_page,
        fetch_consensus_summary,
        fetch_financial_forecast_series,
        fetch_valuation_multiples,
        fetch_calendar_events,
        fetch_ratings_page,
        fetch_sector_peers,
        fetch_price_performance,
        fetch_analyst_recommendations,
        fetch_income_statement_actuals,
        fetch_dividend_eps_page,
        fetch_quarterly_results_table,
    )
    return [
        ("summary",           fetch_summary_page),
        ("consensus",         fetch_consensus_summary),
        ("finances",          fetch_financial_forecast_series),
        ("valuation",         fetch_valuation_multiples),
        ("calendar",          fetch_calendar_events),
        ("ratings",           fetch_ratings_page),
        ("sector",            fetch_sector_peers),
        ("perf",              fetch_price_performance),
        ("recommendations",   fetch_analyst_recommendations),
        ("income_statement",  fetch_income_statement_actuals),
        ("dividend_eps",      fetch_dividend_eps_page),
        ("quarterly_results", fetch_quarterly_results_table),
    ]


def _load_company_master() -> list[dict]:
    """Load curated companies (the source of truth for marketscreener_id)."""
    path = Path("data") / "company_master.json"
    with open(path) as f:
        data = json.load(f)
    # company_master is a flat list in this repo.
    return data if isinstance(data, list) else (data.get("companies") or [])


# Panel = the 10 production tickers we ship (matches probe_harness.PANEL).
_PANEL = {
    "2222.SR", "ADNOCDRILL.AE", "ADCB.AE", "BKMB.OM", "OQEP.OM",
    "JINDALSTEL.NS", "ICICIBANK.BO", "0700.HK", "2899.HK", "1398.HK",
}


def _ms_slug_for(ticker: str, companies: list[dict]) -> str | None:
    """Return the curated marketscreener_id (slug) for `ticker`, or None."""
    for c in companies:
        if (c.get("ticker") or "").upper() == ticker.upper():
            return (c.get("marketscreener_id") or "").strip() or None
    return None


def _isin_for(ticker: str, companies: list[dict]) -> str:
    """Return the curated ISIN for `ticker`, or empty string."""
    for c in companies:
        if (c.get("ticker") or "").upper() == ticker.upper():
            return (c.get("isin") or "").strip()
    return ""


def _base_url(ms_slug: str) -> str:
    return f"https://www.marketscreener.com/quote/stock/{ms_slug}/"


def _refresh_ticker(ticker: str, ms_slug: str, isin: str, fetchers,
                     delay: float) -> dict[str, bool]:
    """Run every page-fetcher for one ticker, capture HTML to the
    repo-tracked snapshot dir. Returns a per-page ok/fail map.

    CRITICAL: the runtime pipeline builds its cache_key_prefix as
      `ms_<ticker>_<isin>_<slug>`
    (see `_cache_key_prefix` in src/services/fetch_marketscreener_pages.py).
    The snapshot files we write here MUST use the same prefix or the
    runtime won't find them. Earlier versions of this script used a
    ticker-only prefix and the snapshots silently never loaded —
    leaving the Q+1 table empty on BKMB even after a successful refresh.
    """
    base = _base_url(ms_slug)
    # Mirror src/services/fetch_marketscreener_pages._cache_key_prefix.
    t = (ticker or "").replace(".", "_").strip() or "unknown"
    i = (isin or "noisin").strip() or "noisin"
    s = (ms_slug or "").strip() or "unknown"
    cache_prefix = f"ms_{t}_{i}_{s}"
    out: dict[str, bool] = {}
    for page_name, fn in fetchers:
        try:
            # The fetcher writes to data/marketscreener/ via _fetch_page
            # whenever MS_TRACKED_REFRESH=1 (we set it in main()).
            _, status = fn(base, cache_key_prefix=cache_prefix)
            ok = status.status in ("success", "partial")
            out[page_name] = ok
        except Exception as exc:
            print(f"      ! {page_name}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            out[page_name] = False
        if delay > 0:
            time.sleep(delay)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", help="Comma-separated ticker list")
    ap.add_argument("--panel", action="store_true",
                    help="Refresh only the 10-ticker production panel")
    ap.add_argument("--active-set", action="store_true",
                    help="Refresh only tickers in data/calendar/upcoming.json "
                         "(set by the daily calendar GHA). Default mode for the "
                         "daily refresh cron — avoids hammering MS for tickers "
                         "no one is generating decks for today.")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="Seconds between page fetches (default 3.0)")
    args = ap.parse_args()

    companies = _load_company_master()

    # Resolve target tickers.
    if args.tickers:
        targets = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.panel:
        targets = sorted(_PANEL & {(c.get("ticker") or "").upper() for c in companies})
    elif args.active_set:
        # Read data/calendar/upcoming.json — the narrow active set produced
        # by the daily build_earnings_calendar GHA. Only refresh tickers
        # whose earnings are within the 14-day horizon. The pre-existing
        # full set (170 tickers) was wasteful — most of them had no
        # upcoming earnings event, so the snapshots aged in place anyway.
        calendar_path = Path(__file__).resolve().parents[1] / "data" / "calendar" / "upcoming.json"
        if not calendar_path.is_file():
            print(f"--active-set requested but {calendar_path} missing. "
                  "Run scripts/build_earnings_calendar.py first.", file=sys.stderr)
            return 2
        try:
            cal = json.loads(calendar_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Failed to parse {calendar_path}: {exc}", file=sys.stderr)
            return 2
        active_tickers = {t["ticker"].upper() for t in (cal.get("tickers") or [])
                           if isinstance(t, dict) and t.get("ticker")}
        # Intersect with tickers we have MS slugs for — calendar may include
        # names whose MS coverage we haven't curated yet.
        targets = sorted(active_tickers & {(c.get("ticker") or "").upper()
                                            for c in companies
                                            if (c.get("marketscreener_id") or "").strip()})
        print(f"[active-set] {len(active_tickers)} tickers in 14-day horizon, "
              f"{len(targets)} have curated MS slugs — refreshing those.")
    else:
        targets = sorted(
            (c.get("ticker") or "").upper()
            for c in companies
            if (c.get("marketscreener_id") or "").strip()
        )

    if not targets:
        print("No tickers resolved; aborting.", file=sys.stderr)
        return 1

    # CRITICAL: these env vars tell _fetch_page in marketscreener_pages.py to:
    #  (1) commit each successful HTML response to data/marketscreener/, AND
    #  (2) use curl_cffi (Chrome 120 TLS fingerprint) instead of `requests`.
    # MS's Cloudflare blocks plain `requests` from datacenter IPs even
    # with proper User-Agent headers; curl_cffi mimics Chrome's TLS
    # ClientHello which passes the JS challenge. Same trick we use for
    # Investing.com. Production runs (Render) never set these — they
    # read snapshots from data/marketscreener/ instead.
    os.environ["MS_TRACKED_REFRESH"] = "1"
    os.environ["MS_USE_CURL_CFFI"] = "1"

    fetchers = _build_page_fetchers()
    n_targets = len(targets)
    n_pages = len(fetchers)
    print(f"Refreshing MarketScreener snapshots for {n_targets} tickers × "
          f"{n_pages} pages = {n_targets * n_pages} page fetches.")
    print(f"Delay between page fetches: {args.delay}s")
    print(f"Output dir: data/marketscreener/\n")

    summary: dict[str, dict[str, bool]] = {}
    for i, tk in enumerate(targets, 1):
        slug = _ms_slug_for(tk, companies)
        if not slug:
            print(f"[{i}/{n_targets}] {tk:18} -> SKIP (no marketscreener_id)")
            continue
        print(f"[{i}/{n_targets}] {tk:18} -> {slug}")
        isin = _isin_for(tk, companies)
        result = _refresh_ticker(tk, slug, isin, fetchers, args.delay)
        ok = sum(1 for v in result.values() if v)
        kinds = " ".join(k for k, v in result.items() if v)
        print(f"      {ok}/{n_pages} pages ok: {kinds}")
        summary[tk] = result

    total_attempts = sum(len(r) for r in summary.values())
    total_ok = sum(sum(1 for v in r.values() if v) for r in summary.values())
    print(f"\nDone. {total_ok}/{total_attempts} page fetches succeeded across "
          f"{len(summary)} tickers.")
    if total_ok == 0:
        print("WARNING: no pages came back successfully. Cloudflare may be "
              "blocking this host too — verify with a manual curl from the "
              "same IP.", file=sys.stderr)
        return 2
    print("Commit data/marketscreener/ to make snapshots live on Render.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
