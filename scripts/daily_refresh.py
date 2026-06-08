"""
Daily refresh runner — Stage 2 production scaffold.

Iterates the wired providers across the company_master ticker universe
(or `--tickers` subset), records every fetch into `coverage_observations`,
runs the reconciler per-cell, and upserts `reconciled_values`. The
report generator reads from `reconciled_values` only — never from a
probe provider directly.

Cadences (per-field refresh frequency):
  hourly:    current_price
  daily:     prices, market_cap, dividend_yield, valuation_historical,
             company_profile, historical_prices
  weekly:    valuation_forward, target_price, rating_split
  quarterly: income_statement_*, balance_sheet, cash_flow

Lock: cache/refresh.lock (PID + start_ts). A second run-in-progress
exits with a friendly message.

Usage:
    python -m scripts.daily_refresh --cadence=daily
    python -m scripts.daily_refresh --cadence=daily --tickers BKMB.OM,OQEP.OM
    python -m scripts.daily_refresh --cadence=daily --only yahoo,marketscreener
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.probe_harness import FIELDS, PANEL
from src.services.canonical_store import record_observation, upsert_reconciled
from src.services.reconcile_sources import (
    TRUST_LADDER, _FILING_GRADE_SOURCES, _comparable_number, _pct_diff,
)
from src.storage.db import init_db, get_conn


_LOCK_FILE = ROOT / "cache" / "refresh.lock"


# Cadence → set of fields. Fields refresh under exactly one cadence.
CADENCE_FIELDS = {
    "hourly": {"current_price"},
    "daily": {
        "current_price",          # also refreshed hourly during market hours
        "market_cap",
        "historical_prices",
        "dividend_yield",
        "valuation_historical",
        "company_profile",
    },
    "weekly": {
        "valuation_forward",
        "target_price",
        "rating_split",
        "broker_actions",
    },
    "quarterly": {
        "income_statement_annual",
        "income_statement_quarterly",
        "balance_sheet",
        "cash_flow",
    },
}


def acquire_lock() -> bool:
    """Best-effort single-flight protection. Returns True if acquired."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _LOCK_FILE.exists():
        # Stale lock? If older than 2 hours, take it over.
        try:
            age = time.time() - _LOCK_FILE.stat().st_mtime
        except OSError:
            age = 0
        if age < 7200:
            return False
    _LOCK_FILE.write_text(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")
    return True


def release_lock() -> None:
    _LOCK_FILE.unlink(missing_ok=True)


def _load_providers(only: set[str] | None):
    """Reuse the probe_sources loader pattern."""
    from scripts.probe_sources import _load_providers as _lp
    return _lp(only)


def _ticker_universe(arg: str | None) -> list[str]:
    if arg:
        return [t.strip() for t in arg.split(",") if t.strip()]
    conn = get_conn()
    try:
        rows = conn.execute("SELECT ticker FROM company_master").fetchall()
    finally:
        conn.close()
    if rows:
        return [r["ticker"] for r in rows]
    return list(PANEL)


def reconcile_cell(observations: dict[str, dict]) -> dict:
    """Reconcile one (ticker, field)'s observations into a canonical record.

    `observations` is {provider_name: {value, source, observation_id, ...}}.
    Returns dict suitable for upsert_reconciled()."""
    if not observations:
        return None
    by_trust = sorted(
        observations.keys(),
        key=lambda s: TRUST_LADDER.index(s) if s in TRUST_LADDER else 999,
    )
    canonical_source = by_trust[0]
    canonical_value = observations[canonical_source]["value"]

    # Numeric agreement check
    comparable = {s: _comparable_number(obs["value"]) for s, obs in observations.items()}
    numeric = {s: n for s, n in comparable.items() if n is not None}
    sources_agreeing: list[str] = []
    max_disagreement = None
    confidence = "Low"
    notes = ""

    if len(numeric) >= 2:
        cn = _comparable_number(canonical_value)
        if cn is not None:
            max_diff = 0.0
            for s, n in numeric.items():
                d = _pct_diff(cn, n)
                max_diff = max(max_diff, d)
                if d <= 2.0:
                    sources_agreeing.append(s)
            max_disagreement = round(max_diff, 2)
            if any(s in observations for s in _FILING_GRADE_SOURCES):
                confidence = "High"
            elif len(sources_agreeing) >= 3:
                confidence = "High"
            elif len(sources_agreeing) >= 2:
                confidence = "Medium"
            elif max_diff > 5.0:
                confidence = "Low"
                notes = f"Sources disagree by {max_diff:.1f}%"
    elif len(observations) == 1:
        if canonical_source in _FILING_GRADE_SOURCES:
            confidence = "High"
            notes = "Filing-grade single source (IR PDF)"
        else:
            confidence = "Low"
            notes = "Single source — no cross-check"
    else:
        # Multi-source non-numeric (e.g. company_profile dict)
        confidence = "Medium"

    return {
        "canonical_value": canonical_value,
        "canonical_source": canonical_source,
        "confidence": confidence,
        "sources_with_value": list(observations.keys()),
        "sources_agreeing": sources_agreeing,
        "max_disagreement_pct": max_disagreement,
        "last_observation_id": observations[canonical_source]["observation_id"],
        "notes": notes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cadence", default="daily",
                    choices=list(CADENCE_FIELDS.keys()),
                    help="Which fields to refresh (default: daily)")
    ap.add_argument("--only", help="Provider subset, comma-separated")
    ap.add_argument("--tickers", help="Ticker subset, comma-separated")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write to DB; just print what would happen")
    args = ap.parse_args()

    if not acquire_lock():
        print(f"[skip] another refresh appears to be running (lock at {_LOCK_FILE})")
        return 0

    try:
        init_db()
        run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
        cadence_fields = CADENCE_FIELDS[args.cadence]
        fields = [f for f in FIELDS if f in cadence_fields]
        tickers = _ticker_universe(args.tickers)
        only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
        providers = _load_providers(only)

        print(f"run_id={run_id} cadence={args.cadence}")
        print(f"tickers={len(tickers)} fields={len(fields)} providers={list(providers.keys())}")

        t0 = time.monotonic()
        observations_for: dict[tuple[str, str], dict[str, dict]] = {}
        per_provider = {p: {"hit": 0, "miss": 0, "err": 0} for p in providers}

        for ticker in tickers:
            for field in fields:
                key = (ticker, field)
                observations_for.setdefault(key, {})
                for pname, provider in providers.items():
                    cell = provider.fetch(ticker, field)
                    obs_id = None
                    if not args.dry_run:
                        obs_id = record_observation(
                            run_id=run_id, ticker=ticker, field=field, provider=pname,
                            value=cell.value, units=cell.units or "",
                            as_of=cell.as_of or "", raw_response_id=cell.raw_response_id or "",
                            error=cell.error or "", latency_ms=int(cell.latency_ms or 0),
                        )
                    if cell.value is not None and not cell.error:
                        observations_for[key][pname] = {
                            "value": cell.value, "source": pname,
                            "observation_id": obs_id,
                        }
                        per_provider[pname]["hit"] += 1
                    elif cell.error == "not_implemented":
                        per_provider[pname]["miss"] += 1
                    else:
                        per_provider[pname]["err"] += 1

        # Reconcile every (ticker, field). Critical: a refresh that only
        # runs SOME providers (e.g. `--only yahoo,marketscreener`) must NOT
        # overwrite a canonical cell that a different provider populated in
        # a previous run. We merge this-run observations with the latest
        # observation from each OTHER provider stored in coverage_observations.
        #
        # Concretely: if last week's `--only investing` run made
        # rating_split canonical=investing, today's `--only yahoo,ms` run
        # should still respect Investing's cell as a source — and the
        # trust ladder picks Investing as canonical.
        from src.services.canonical_store import (
            get_observations_by_provider as _obs_by_prov,
        )
        from src.services.reconcile_sources import TRUST_LADDER

        # Discover every provider that has historical observations for
        # any (ticker, field) in this batch — including providers we
        # didn't run this time.
        all_known_providers = set(observations_for) and set(TRUST_LADDER)
        all_known_providers = set(TRUST_LADDER)

        reconciled_count = 0
        for (ticker, field), obs in observations_for.items():
            # Backfill from coverage_observations: most-recent value per
            # other provider for THIS field on THIS ticker.
            for provider in all_known_providers:
                if provider in obs:
                    continue
                prior = _obs_by_prov(ticker, provider)
                if not prior or field not in prior:
                    continue
                value = prior[field]
                obs[provider] = {
                    "value": value, "source": provider,
                    "observation_id": None,   # historical
                }

            if not obs:
                continue
            rc = reconcile_cell(obs)
            if rc is None:
                continue
            if not args.dry_run:
                upsert_reconciled(
                    ticker=ticker, field=field,
                    canonical_value=rc["canonical_value"],
                    canonical_source=rc["canonical_source"],
                    confidence=rc["confidence"],
                    sources_with_value=rc["sources_with_value"],
                    sources_agreeing=rc["sources_agreeing"],
                    max_disagreement_pct=rc["max_disagreement_pct"],
                    last_observation_id=rc["last_observation_id"],
                    notes=rc["notes"],
                )
            reconciled_count += 1

        elapsed = time.monotonic() - t0

        # Refresh summary
        summary = {
            "run_id": run_id,
            "cadence": args.cadence,
            "duration_s": round(elapsed, 1),
            "tickers": len(tickers),
            "fields_in_cadence": fields,
            "cells_reconciled": reconciled_count,
            "per_provider": per_provider,
        }
        out_path = ROOT / "outputs" / "refresh_summary.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2))
        print()
        print(f"Done in {elapsed:.1f}s. Reconciled {reconciled_count} cells.")
        print(f"Summary -> {out_path}")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
