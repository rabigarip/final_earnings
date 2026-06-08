"""MarketScreener payload fingerprinting — detect identical MS payloads across
unrelated tickers (cross-company contamination).

Fingerprints are stored in SQLite (ms_fingerprints table) for cross-run comparison.
"""

from __future__ import annotations
import hashlib
import json
from typing import Any

from src.storage.db import get_conn


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def compute_fingerprint(
    consensus_summary: dict | None = None,
    ms_summary: dict | None = None,
    ms_annual_forecasts: dict | None = None,
    ms_quarterly_forecasts: dict | None = None,
    ms_eps_dividend_forecasts: dict | None = None,
    ms_income_statement_actuals: dict | None = None,
    ms_valuation_multiples: dict | None = None,
    ms_calendar_events: dict | None = None,
    ms_quarterly_results_table: dict | None = None,
    ms_ratings: dict | None = None,
    ms_sector_peers: dict | None = None,
    ms_price_performance: dict | None = None,
    ms_analyst_recommendations: dict | None = None,
) -> str:
    """SHA256 hex digest of all MS-derived sections. Empty string if no MS data."""
    parts = [
        consensus_summary, ms_summary, ms_annual_forecasts,
        ms_quarterly_forecasts, ms_eps_dividend_forecasts,
        ms_income_statement_actuals, ms_valuation_multiples,
        ms_calendar_events, ms_quarterly_results_table,
        ms_ratings, ms_sector_peers, ms_price_performance, ms_analyst_recommendations,
    ]
    if all(p is None or (isinstance(p, dict) and len(p) == 0) for p in parts):
        return ""
    blob = _canonical({
        "consensus_summary": consensus_summary,
        "ms_summary": ms_summary,
        "ms_annual_forecasts": ms_annual_forecasts,
        "ms_quarterly_forecasts": ms_quarterly_forecasts,
        "ms_eps_dividend_forecasts": ms_eps_dividend_forecasts,
        "ms_income_statement_actuals": ms_income_statement_actuals,
        "ms_valuation_multiples": ms_valuation_multiples,
        "ms_calendar_events": ms_calendar_events,
        "ms_quarterly_results_table": ms_quarterly_results_table,
        "ms_ratings": ms_ratings,
        "ms_sector_peers": ms_sector_peers,
        "ms_price_performance": ms_price_performance,
        "ms_analyst_recommendations": ms_analyst_recommendations,
    })
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def check_fingerprint(current_ticker: str, fingerprint: str) -> tuple[bool, bool]:
    """Returns (cross_company_contamination, identical_to_previous_for_same_ticker)."""
    if not fingerprint or not (current_ticker or "").strip():
        return False, False
    current_ticker = current_ticker.strip()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT fingerprint FROM ms_fingerprints WHERE ticker = ?",
            (current_ticker,),
        ).fetchone()
        identical = bool(row and row["fingerprint"] == fingerprint)
        cross = conn.execute(
            "SELECT 1 FROM ms_fingerprints WHERE fingerprint = ? AND ticker != ? LIMIT 1",
            (fingerprint, current_ticker),
        ).fetchone()
        return bool(cross), identical
    finally:
        conn.close()


def save_fingerprint(ticker: str, run_id: str, fingerprint: str) -> None:
    if not (ticker or "").strip() or not fingerprint:
        return
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO ms_fingerprints (ticker, fingerprint, run_id, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(ticker) DO UPDATE SET
                 fingerprint = excluded.fingerprint,
                 run_id = excluded.run_id,
                 updated_at = CURRENT_TIMESTAMP""",
            (ticker.strip(), fingerprint, run_id or ""),
        )
        conn.commit()
    finally:
        conn.close()
