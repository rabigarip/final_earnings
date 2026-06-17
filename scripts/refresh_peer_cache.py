"""Pre-warm the peer-fundamentals cache for the whole universe and commit it.

Yahoo's `.info` (quoteSummary) endpoint — the source of peer P/E, P/B,
EV/EBITDA, dividend yield and 1Y return — is rate-limited from Render's
datacenter IP. Peers that have no Investing snapshot (global / US names like
NTR, MOS, CF, YAR.OL) therefore come back EMPTY on Render and the slide-3
peer-comparables table renders a row of dashes.

This script runs from a NON-blocked IP (locally, or the GitHub Action),
fetches each distinct peer's full row, and writes data/peers_cache.json.
`fetch_peers.fetch_peer_rows()` reads it as a last-resort fallback, so the
peer table fills on Render too.

The peer set for each name mirrors generate_report exactly:
    load_company(ticker).peer_group  OR  registry_peer_set(ticker)

Usage:
    python -m scripts.refresh_peer_cache                 # whole universe
    python -m scripts.refresh_peer_cache --tickers NTR,MOS,CF
    python -m scripts.refresh_peer_cache --for 2020.SR,1180.SR   # just these names' peers
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

_CACHE_PATH = ROOT / "data" / "peers_cache.json"

# Bookkeeping keys we don't need to persist per row (name/ticker/*_fmt/values
# are all kept; only transient ones stripped). We keep everything fetch_peer_rows
# returns plus an `as_of` stamp.


def _all_universe_tickers() -> list[str]:
    from src.storage.db import get_conn
    conn = get_conn()
    try:
        rows = conn.execute("SELECT ticker FROM company_master").fetchall()
    finally:
        conn.close()
    return [r["ticker"] for r in rows]


def _peers_for(ticker: str) -> list[str]:
    """Same resolution generate_report uses: curated peer_group, else the
    auto registry peer set."""
    from src.storage.db import load_company
    from src.services.ticker_registry import registry_peer_set
    c = load_company(ticker) or {}
    return list(c.get("peer_group") or registry_peer_set(ticker) or [])


def _distinct_peer_set(for_tickers: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for t in for_tickers:
        for p in _peers_for(t):
            pp = (p or "").strip()
            if pp:
                seen.setdefault(pp.upper(), None)
    return sorted(seen.keys())


def _row_has_data(row: dict) -> bool:
    return (row.get("market_cap_usd") is not None
            or row.get("pe") is not None
            or row.get("pb") is not None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="Comma-separated peer tickers to fetch directly")
    ap.add_argument("--for", dest="for_names",
                    help="Comma-separated subject tickers; fetch THEIR peers only")
    ap.add_argument("--chunk", type=int, default=15,
                    help="Peers per fetch batch (smaller = gentler on Yahoo)")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="Seconds between batches")
    args = ap.parse_args()

    from src.services.fetch_peers import fetch_peer_rows
    from src.services.fetch_peers import _iso_now

    if args.tickers:
        peers = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})
    else:
        subjects = ([t.strip() for t in args.for_names.split(",") if t.strip()]
                    if args.for_names else _all_universe_tickers())
        peers = _distinct_peer_set(subjects)

    if not peers:
        print("No peers to fetch.")
        return 0

    # Merge into any existing cache so a partial run never erases prior good rows.
    cache: dict[str, dict] = {}
    if _CACHE_PATH.exists():
        try:
            cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}

    print(f"Refreshing peer cache for {len(peers)} distinct peers "
          f"(chunk={args.chunk}, delay={args.delay}s)...")
    stamp = _iso_now()
    ok = miss = 0
    for i in range(0, len(peers), args.chunk):
        batch = peers[i:i + args.chunk]
        try:
            rows = fetch_peer_rows(batch)
        except Exception as exc:
            print(f"  [batch {i // args.chunk + 1}] ERROR {type(exc).__name__}: {exc}")
            rows = []
        by_ticker = {(r.get("ticker") or "").upper(): r for r in rows}
        for p in batch:
            row = by_ticker.get(p)
            if row and _row_has_data(row):
                cache[p] = {**row, "as_of": stamp}
                ok += 1
            else:
                miss += 1
        done = min(i + args.chunk, len(peers))
        print(f"  [{done}/{len(peers)}] {ok} cached, {miss} missing")
        if done < len(peers):
            time.sleep(args.delay)

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nDone. {ok}/{len(peers)} peers fetched this run; "
          f"{len(cache)} total in {_CACHE_PATH}.")
    print("Commit data/peers_cache.json to make it live on Render.")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
