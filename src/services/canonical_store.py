"""
Read-side service for the reconciled canonical store.

The report generator (and any other consumer) calls into here instead
of hitting probe_* providers directly. This is the boundary that
decouples report generation from the network: by the time a deck is
being built, every cell has been resolved, reconciled, and persisted.

Contract:
    get_canonical(ticker, field) -> CanonicalValue | None
    get_all_fields(ticker)        -> dict[str, CanonicalValue]
    get_freshness(ticker, field)  -> timedelta | None

All reads are pure SQL against `reconciled_values`. No network calls,
no provider imports. If a field is missing, the consumer decides how
to render the gap (typically: render a "—" placeholder and flag the
deck footer).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from src.storage.db import get_conn


@dataclass
class CanonicalValue:
    ticker: str
    field: str
    value: Any                       # may be float, str, dict, list — JSON-decoded
    canonical_source: str
    confidence: str                  # High / Medium / Low
    sources_with_value: list[str]
    sources_agreeing: list[str]
    max_disagreement_pct: Optional[float]
    last_refreshed_at: datetime
    notes: str

    @property
    def age(self) -> timedelta:
        return datetime.now(timezone.utc) - self.last_refreshed_at


def _row_to_canonical(row) -> CanonicalValue:
    val_json = row["canonical_value_json"]
    try:
        value = json.loads(val_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Plain string or numeric stored as raw repr — try float first
        try:
            value = float(val_json)
        except (TypeError, ValueError):
            value = val_json
    refreshed = datetime.fromisoformat(row["last_refreshed_at"])
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    return CanonicalValue(
        ticker=row["ticker"],
        field=row["field"],
        value=value,
        canonical_source=row["canonical_source"],
        confidence=row["confidence"],
        sources_with_value=[s for s in (row["sources_with_value"] or "").split(",") if s],
        sources_agreeing=[s for s in (row["sources_agreeing"] or "").split(",") if s],
        max_disagreement_pct=row["max_disagreement_pct"],
        last_refreshed_at=refreshed,
        notes=row["notes"] or "",
    )


def get_canonical(ticker: str, field: str) -> Optional[CanonicalValue]:
    """Single canonical-value lookup. Returns None if the cell isn't
    in the reconciled store yet (e.g. the field was added after the
    last refresh, or the ticker is new)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM reconciled_values WHERE ticker = ? AND field = ?",
            (ticker, field),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_canonical(row) if row else None


def get_all_fields(ticker: str) -> dict[str, CanonicalValue]:
    """Pull every reconciled field for a ticker in one shot.
    This is the report generator's primary call: one query, then build
    the deck off the resulting dict."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM reconciled_values WHERE ticker = ?",
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    return {r["field"]: _row_to_canonical(r) for r in rows}


def get_freshness(ticker: str, field: str) -> Optional[timedelta]:
    """How long ago was this cell refreshed? Used to flag stale data
    in the deck footer."""
    cv = get_canonical(ticker, field)
    return cv.age if cv else None


def get_observations_by_provider(ticker: str, provider: str) -> dict[str, Any]:
    """Return the latest observation per field for a specific provider.

    Used by the slide renderer to pull SUPPLEMENTARY context that lost
    the trust-ladder fight in reconciliation (commodities, ishares ETF
    proxies, macro overlays). The reconciled canonical value reflects
    the primary source; this helper exposes the side data that the
    primary obscured.

    Returns {field: value} where value is the JSON-decoded observation
    payload (or None if the field has only errored observations)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT field, value_json, observed_at
            FROM coverage_observations
            WHERE ticker = ? AND provider = ? AND value_json IS NOT NULL
              AND COALESCE(error, '') = ''
            ORDER BY observed_at DESC
            """,
            (ticker, provider),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, Any] = {}
    for r in rows:
        if r["field"] in out:
            continue   # we want the most-recent per field
        try:
            out[r["field"]] = json.loads(r["value_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            out[r["field"]] = r["value_json"]
    return out


def upsert_reconciled(
    ticker: str, field: str, canonical_value: Any, canonical_source: str,
    confidence: str, sources_with_value: list[str], sources_agreeing: list[str],
    max_disagreement_pct: Optional[float], last_observation_id: Optional[int] = None,
    notes: str = "",
) -> None:
    """Write side. Called by the refresh runner after reconciliation.
    Idempotent: same (ticker, field) replaces the existing row.

    `canonical_value` is JSON-serialized for storage. Numeric and string
    values round-trip fine; dict/list values are stored as JSON and
    re-parsed by `_row_to_canonical()`."""
    payload = json.dumps(canonical_value, default=str)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO reconciled_values (
              ticker, field, canonical_value_json, canonical_source, confidence,
              sources_with_value, sources_agreeing, max_disagreement_pct,
              last_refreshed_at, last_observation_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, field) DO UPDATE SET
              canonical_value_json = excluded.canonical_value_json,
              canonical_source     = excluded.canonical_source,
              confidence           = excluded.confidence,
              sources_with_value   = excluded.sources_with_value,
              sources_agreeing     = excluded.sources_agreeing,
              max_disagreement_pct = excluded.max_disagreement_pct,
              last_refreshed_at    = excluded.last_refreshed_at,
              last_observation_id  = excluded.last_observation_id,
              notes                = excluded.notes
            """,
            (
                ticker, field, payload, canonical_source, confidence,
                ",".join(sources_with_value), ",".join(sources_agreeing),
                max_disagreement_pct, now_iso, last_observation_id, notes,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_observation(
    run_id: str, ticker: str, field: str, provider: str,
    value: Any = None, units: str = "", as_of: str = "",
    raw_response_id: str = "", error: str = "", latency_ms: Optional[int] = None,
) -> int:
    """Append a row to coverage_observations. Returns the new
    observation_id so the reconciler can store it back on the
    reconciled_values row."""
    payload = json.dumps(value, default=str) if value is not None else None
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO coverage_observations (
              run_id, observed_at, ticker, field, provider,
              value_json, units, as_of, raw_response_id, error, latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, now_iso, ticker, field, provider,
             payload, units, as_of, raw_response_id, error, latency_ms),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()
