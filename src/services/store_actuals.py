"""Store and fetch prior actuals for trend and YoY fallback logic."""

from __future__ import annotations

from typing import Any

from src.storage.db import get_conn


def upsert_actuals(
    ticker: str,
    period: str,
    *,
    revenue: float | None = None,
    net_income: float | None = None,
    eps: float | None = None,
    ebitda: float | None = None,
    ebitda_margin: float | None = None,
    reported_date: str | None = None,
) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO actuals
            (ticker, period, revenue, net_income, eps, ebitda, ebitda_margin, reported_date)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, period) DO UPDATE SET
            revenue=excluded.revenue,
            net_income=excluded.net_income,
            eps=excluded.eps,
            ebitda=excluded.ebitda,
            ebitda_margin=excluded.ebitda_margin,
            reported_date=excluded.reported_date
        """,
        (ticker, period, revenue, net_income, eps, ebitda, ebitda_margin, reported_date),
    )
    conn.commit()
    conn.close()


def get_actual(ticker: str, period: str) -> dict[str, Any] | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT ticker, period, revenue, net_income, eps, ebitda, ebitda_margin, reported_date FROM actuals WHERE ticker=? AND period=?",
        (ticker, period),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def latest_actuals(ticker: str, limit: int = 8) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker, period, revenue, net_income, eps, ebitda, ebitda_margin, reported_date FROM actuals WHERE ticker=? ORDER BY period DESC LIMIT ?",
        (ticker, max(1, int(limit))),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

