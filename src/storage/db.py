"""
SQLite storage layer.

Why SQLite (not JSON/CSV):
  - Query across runs ("show all failures this week")
  - Foreign keys keep data consistent
  - Transactions for free
  - Migration to Postgres later is one afternoon of work
  - Zero setup, zero cost, single file

company_master.json stays as the human-curated seed. Everything else
lives in SQLite from the start.
"""

from __future__ import annotations
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from src.config import cfg, root, database_path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_master (
    ticker                      TEXT PRIMARY KEY,
    company_name                TEXT NOT NULL,
    company_name_long           TEXT DEFAULT '',
    exchange                    TEXT NOT NULL,
    country                     TEXT NOT NULL,
    currency                    TEXT NOT NULL,
    isin                        TEXT DEFAULT '',
    marketscreener_id           TEXT DEFAULT '',
    marketscreener_company_url  TEXT DEFAULT '',
    marketscreener_symbol       TEXT DEFAULT '',
    marketscreener_status       TEXT DEFAULT '',
    marketscreener_rejection_reason TEXT DEFAULT '',
    last_verified               TEXT DEFAULT '',
    zawya_slug                  TEXT DEFAULT '',
    sector                      TEXT DEFAULT '',
    industry                    TEXT DEFAULT '',
    peer_group                  TEXT DEFAULT '[]',
    is_bank                     INTEGER DEFAULT 0,
    notes                       TEXT DEFAULT '',
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    mode            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    overall_status  TEXT,
    step_results    TEXT,
    memo_path       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS financial_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    source           TEXT NOT NULL,
    period_type      TEXT NOT NULL,
    period_label     TEXT NOT NULL,
    revenue          REAL,
    ebitda           REAL,
    ebit             REAL,
    net_income       REAL,
    eps              REAL,
    dps              REAL,
    announcement_date TEXT,
    is_consensus     INTEGER DEFAULT 0,
    currency         TEXT DEFAULT 'SAR',
    fetched_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS news_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    source                TEXT NOT NULL,
    headline              TEXT NOT NULL,
    url                   TEXT,
    published_at          TEXT,
    snippet               TEXT,
    sentiment             TEXT,
    is_likely_stock_moving INTEGER DEFAULT 0,
    language              TEXT DEFAULT 'en',
    fetched_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ms_fingerprints (
    ticker       TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS report_outputs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS actuals (
    ticker TEXT NOT NULL,
    period TEXT NOT NULL,
    revenue REAL,
    net_income REAL,
    eps REAL,
    ebitda REAL,
    ebitda_margin REAL,
    reported_date TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, period)
);

CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker            TEXT NOT NULL,
    event_date        TEXT NOT NULL,
    event_time        TEXT DEFAULT '',
    confirmed         INTEGER DEFAULT 0,
    period_label      TEXT DEFAULT '',
    fiscal_year_end   TEXT DEFAULT '',
    consensus_revenue REAL,
    consensus_eps     REAL,
    source            TEXT DEFAULT 'yahoo',
    last_checked      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, event_date)
);

CREATE INDEX IF NOT EXISTS idx_earnings_calendar_date
    ON earnings_calendar(event_date);

-- Stage 2 production wire-up: audit log of every probe attempt.
-- One row per (run, ticker, field, provider). Immutable — never updated
-- in place. The reconciler reads this to compute reconciled_values.
CREATE TABLE IF NOT EXISTS coverage_observations (
    observation_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    observed_at       TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    field             TEXT NOT NULL,
    provider          TEXT NOT NULL,
    value_json        TEXT,
    units             TEXT DEFAULT '',
    as_of             TEXT DEFAULT '',
    raw_response_id   TEXT DEFAULT '',
    error             TEXT DEFAULT '',
    latency_ms        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_obs_ticker_field
    ON coverage_observations(ticker, field);
CREATE INDEX IF NOT EXISTS idx_obs_run
    ON coverage_observations(run_id);

-- Stage 2 production wire-up: canonical view per (ticker, field).
-- This is what the report generator queries — never a probe provider
-- directly. Replaced (upserted) by the refresh runner.
CREATE TABLE IF NOT EXISTS reconciled_values (
    ticker                 TEXT NOT NULL,
    field                  TEXT NOT NULL,
    canonical_value_json   TEXT NOT NULL,
    canonical_source       TEXT NOT NULL,
    confidence             TEXT NOT NULL,
    sources_with_value     TEXT DEFAULT '',
    sources_agreeing       TEXT DEFAULT '',
    max_disagreement_pct   REAL,
    last_refreshed_at      TEXT NOT NULL,
    last_observation_id    INTEGER,
    notes                  TEXT DEFAULT '',
    PRIMARY KEY (ticker, field)
);
"""


def _db_path() -> Path:
    return database_path()


def get_conn() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(_SCHEMA)
    _migrate_company_master_identifier_columns(conn)
    _migrate_pipeline_runs_memo_path(conn)
    _migrate_actuals_table(conn)
    _migrate_earnings_calendar_table(conn)
    conn.commit()
    conn.close()
    log.info("Initialized → %s", _db_path())


def _migrate_pipeline_runs_memo_path(conn: sqlite3.Connection) -> None:
    """Add memo_path to pipeline_runs if missing (for existing DBs)."""
    cur = conn.execute("PRAGMA table_info(pipeline_runs)")
    names = {row[1] for row in cur.fetchall()}
    if "memo_path" not in names:
        conn.execute("ALTER TABLE pipeline_runs ADD COLUMN memo_path TEXT DEFAULT ''")


def ensure_migrations() -> None:
    """Run migrations on existing DB (e.g. add memo_path). Call when app starts if DB exists."""
    conn = get_conn()
    _migrate_pipeline_runs_memo_path(conn)
    _migrate_actuals_table(conn)
    _migrate_earnings_calendar_table(conn)
    conn.commit()
    conn.close()


def _migrate_earnings_calendar_table(conn: sqlite3.Connection) -> None:
    """Create earnings_calendar if missing (for existing DBs)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS earnings_calendar (
            ticker            TEXT NOT NULL,
            event_date        TEXT NOT NULL,
            event_time        TEXT DEFAULT '',
            confirmed         INTEGER DEFAULT 0,
            period_label      TEXT DEFAULT '',
            fiscal_year_end   TEXT DEFAULT '',
            consensus_revenue REAL,
            consensus_eps     REAL,
            source            TEXT DEFAULT 'yahoo',
            last_checked      TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, event_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_earnings_calendar_date ON earnings_calendar(event_date)"
    )


def _migrate_actuals_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS actuals (
            ticker TEXT NOT NULL,
            period TEXT NOT NULL,
            revenue REAL,
            net_income REAL,
            eps REAL,
            ebitda REAL,
            ebitda_margin REAL,
            reported_date TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, period)
        )
        """
    )


def _migrate_company_master_identifier_columns(conn: sqlite3.Connection) -> None:
    """Add identifier-layer columns if missing (for existing DBs)."""
    cur = conn.execute("PRAGMA table_info(company_master)")
    names = {row[1] for row in cur.fetchall()}
    for col, typ in [
        ("marketscreener_company_url", "TEXT DEFAULT ''"),
        ("marketscreener_symbol", "TEXT DEFAULT ''"),
        ("marketscreener_status", "TEXT DEFAULT ''"),
        ("marketscreener_rejection_reason", "TEXT DEFAULT ''"),
        ("last_verified", "TEXT DEFAULT ''"),
        ("peer_group", "TEXT DEFAULT '[]'"),
    ]:
        if col not in names:
            conn.execute(f"ALTER TABLE company_master ADD COLUMN {col} {typ}")


def seed_companies() -> int:
    seed = root() / "data" / "company_master.json"
    if not seed.exists():
        log.warning("Seed file not found: %s", seed)
        return 0
    with open(seed) as f:
        rows = json.load(f)
    conn = get_conn()
    n = 0
    for c in rows:
        slug = c.get("marketscreener_id", "")
        ms_url = (
            f"https://www.marketscreener.com/quote/stock/{slug}/"
            if slug else ""
        )
        ms_status = "ok" if slug else ""
        last_verified = datetime.now(timezone.utc).isoformat() if slug else ""

        conn.execute("""
            INSERT INTO company_master
                (ticker, company_name, company_name_long, exchange, country,
                 currency, isin, marketscreener_id, marketscreener_company_url,
                 marketscreener_symbol, marketscreener_status, last_verified,
                 zawya_slug, sector, industry, peer_group, is_bank, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                company_name=excluded.company_name,
                company_name_long=excluded.company_name_long,
                exchange=excluded.exchange,
                country=excluded.country,
                currency=excluded.currency,
                isin=excluded.isin,
                marketscreener_id=CASE
                    WHEN excluded.marketscreener_id != '' THEN excluded.marketscreener_id
                    ELSE company_master.marketscreener_id
                END,
                marketscreener_company_url=CASE
                    WHEN excluded.marketscreener_company_url != '' THEN excluded.marketscreener_company_url
                    ELSE company_master.marketscreener_company_url
                END,
                marketscreener_symbol=CASE
                    WHEN excluded.marketscreener_symbol != '' THEN excluded.marketscreener_symbol
                    ELSE company_master.marketscreener_symbol
                END,
                marketscreener_status=CASE
                    WHEN excluded.marketscreener_status != '' THEN excluded.marketscreener_status
                    ELSE company_master.marketscreener_status
                END,
                last_verified=CASE
                    WHEN excluded.last_verified != '' THEN excluded.last_verified
                    ELSE company_master.last_verified
                END,
                zawya_slug=excluded.zawya_slug,
                sector=excluded.sector,
                industry=excluded.industry,
                peer_group=excluded.peer_group,
                is_bank=excluded.is_bank,
                notes=excluded.notes,
                updated_at=CURRENT_TIMESTAMP
        """, (
            c["ticker"], c["company_name"], c.get("company_name_long", ""),
            c["exchange"], c["country"], c["currency"],
            c.get("isin", ""), slug, ms_url,
            c.get("marketscreener_symbol", "") or c.get("ticker", ""),
            ms_status, last_verified,
            c.get("zawya_slug", ""),
            c.get("sector", ""), c.get("industry", ""),
            json.dumps(c.get("peer_group", []) if isinstance(c.get("peer_group", []), list) else []),
            c.get("is_bank", False), c.get("notes", ""),
        ))
        n += 1
    conn.commit()
    conn.close()
    log.info("Seeded %s companies", n)
    return n


def insert_discovered_company(
    ticker: str,
    company_name: str,
    company_name_long: str = "",
    exchange: str = "",
    country: str = "",
    currency: str = "",
    isin: str = "",
    sector: str = "",
    industry: str = "",
    is_bank: bool = False,
) -> dict | None:
    """Insert or update a company auto-discovered from yfinance.
    On conflict (ticker exists), updates name/sector/currency/isin
    but preserves manually-curated MarketScreener mappings."""
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO company_master
                (ticker, company_name, company_name_long, exchange, country,
                 currency, isin, sector, industry, is_bank)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                company_name = CASE WHEN excluded.company_name != '' THEN excluded.company_name ELSE company_master.company_name END,
                company_name_long = CASE WHEN excluded.company_name_long != '' THEN excluded.company_name_long ELSE company_master.company_name_long END,
                exchange = CASE WHEN excluded.exchange != '' THEN excluded.exchange ELSE company_master.exchange END,
                country = CASE WHEN excluded.country != '' THEN excluded.country ELSE company_master.country END,
                currency = CASE WHEN excluded.currency != '' THEN excluded.currency ELSE company_master.currency END,
                isin = CASE WHEN excluded.isin != '' THEN excluded.isin ELSE company_master.isin END,
                sector = CASE WHEN excluded.sector != '' THEN excluded.sector ELSE company_master.sector END,
                industry = CASE WHEN excluded.industry != '' THEN excluded.industry ELSE company_master.industry END,
                is_bank = excluded.is_bank,
                updated_at = CURRENT_TIMESTAMP
        """, (
            ticker, company_name, company_name_long, exchange, country,
            currency, isin, sector, industry, int(is_bank),
        ))
        conn.commit()
    except Exception as exc:
        log.warning("insert_discovered_company failed for %s: %s", ticker, exc)
        conn.close()
        return None
    conn.close()
    return load_company(ticker)


def load_company(ticker: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM company_master WHERE ticker = ?", (ticker,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    # Stored as JSON text in SQLite
    try:
        pg = d.get("peer_group")
        if isinstance(pg, str):
            d["peer_group"] = json.loads(pg) if pg else []
    except Exception:
        d["peer_group"] = []
    return d


def list_companies() -> list[dict]:
    """Return all companies for API/UI: ticker, company_name, exchange, country."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker, company_name, exchange, country, currency FROM company_master ORDER BY ticker"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        out.append(d)
    return out


def update_company_marketscreener(
    ticker: str,
    marketscreener_company_url: str,
    marketscreener_symbol: str,
    marketscreener_status: str,
    last_verified: str,
    marketscreener_id: str | None = None,
) -> None:
    """Update cached MarketScreener fields after ISIN-based resolution."""
    slug = marketscreener_id
    if not slug and marketscreener_company_url:
        import re
        m = re.search(r"/quote/stock/([^/]+)/?", marketscreener_company_url)
        if m:
            slug = m.group(1)
    conn = get_conn()
    conn.execute("""
        UPDATE company_master SET
            marketscreener_company_url = ?,
            marketscreener_symbol = ?,
            marketscreener_status = ?,
            marketscreener_rejection_reason = '',
            last_verified = ?,
            marketscreener_id = COALESCE(?, marketscreener_id),
            updated_at = CURRENT_TIMESTAMP
        WHERE ticker = ?
    """, (
        marketscreener_company_url,
        marketscreener_symbol,
        marketscreener_status,
        last_verified,
        slug,
        ticker,
    ))
    conn.commit()
    conn.close()


def invalidate_marketscreener_cache(ticker: str) -> None:
    """Mark cached MarketScreener URL as stale (e.g. after redirect to homepage). Does not clear URL/slug."""
    conn = get_conn()
    conn.execute("""
        UPDATE company_master SET
            marketscreener_status = 'stale',
            last_verified = '',
            updated_at = CURRENT_TIMESTAMP
        WHERE ticker = ?
    """, (ticker,))
    conn.commit()
    conn.close()


def reject_marketscreener_candidate(ticker: str, reason: str, status: str = "needs_review") -> None:
    """
    Record that a re-resolved candidate was rejected. Do not overwrite URL/slug.
    Sets marketscreener_status = status (default needs_review) and stores reason.
    """
    conn = get_conn()
    conn.execute("""
        UPDATE company_master SET
            marketscreener_status = ?,
            marketscreener_rejection_reason = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE ticker = ?
    """, (status, reason[:500] if reason else "", ticker))
    conn.commit()
    conn.close()


def set_marketscreener_source_redirect(ticker: str) -> None:
    """
    Mark MarketScreener as source_redirect: entity/slug is correct but
    company/consensus URLs redirect to homepage (known source-behavior exception).
    Does not clear URL/slug; stops repeated re-resolution attempts.
    """
    conn = get_conn()
    conn.execute("""
        UPDATE company_master SET
            marketscreener_status = 'source_redirect',
            marketscreener_rejection_reason = '',
            updated_at = CURRENT_TIMESTAMP
        WHERE ticker = ?
    """, (ticker,))
    conn.commit()
    conn.close()


def save_run(run_id: str, ticker: str, mode: str, started_at: str,
             finished_at: str, status: str, steps: list[dict],
             memo_path: str | None = None) -> None:
    conn = get_conn()
    conn.execute("""
        INSERT INTO pipeline_runs
            (run_id, ticker, mode, started_at, finished_at, overall_status, step_results, memo_path)
        VALUES (?,?,?,?,?,?,?,?)
    """, (run_id, ticker, mode, started_at, finished_at, status, json.dumps(steps), memo_path or ""))
    conn.commit()
    conn.close()


def list_runs() -> list[dict]:
    """List pipeline runs with company name and country from company_master."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT r.run_id, r.ticker, r.started_at, r.finished_at, r.overall_status, r.step_results, r.memo_path,
               c.company_name, c.country
        FROM pipeline_runs r
        LEFT JOIN company_master c ON c.ticker = r.ticker
        ORDER BY r.started_at DESC
    """).fetchall()
    conn.close()
    out = []
    for row in rows:
        d = dict(row)
        # Parse step_results for warnings count
        steps_raw = d.get("step_results") or "[]"
        try:
            steps = json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw
        except Exception:
            steps = []
        warnings = sum(1 for s in steps if s.get("status") in ("partial", "failed"))
        d["warnings"] = warnings
        d["step_results"] = steps
        out.append(d)
    return out


def upsert_calendar_event(
    ticker: str,
    event_date: str,
    *,
    event_time: str = "",
    confirmed: bool = False,
    period_label: str = "",
    fiscal_year_end: str = "",
    consensus_revenue: float | None = None,
    consensus_eps: float | None = None,
    source: str = "yahoo",
) -> None:
    """Upsert an earnings event. event_date is ISO YYYY-MM-DD."""
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO earnings_calendar
            (ticker, event_date, event_time, confirmed, period_label,
             fiscal_year_end, consensus_revenue, consensus_eps, source, last_checked)
        VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(ticker, event_date) DO UPDATE SET
            event_time      = CASE WHEN excluded.event_time != '' THEN excluded.event_time ELSE earnings_calendar.event_time END,
            confirmed       = MAX(earnings_calendar.confirmed, excluded.confirmed),
            period_label    = CASE WHEN excluded.period_label != '' THEN excluded.period_label ELSE earnings_calendar.period_label END,
            fiscal_year_end = CASE WHEN excluded.fiscal_year_end != '' THEN excluded.fiscal_year_end ELSE earnings_calendar.fiscal_year_end END,
            consensus_revenue = COALESCE(excluded.consensus_revenue, earnings_calendar.consensus_revenue),
            consensus_eps     = COALESCE(excluded.consensus_eps,     earnings_calendar.consensus_eps),
            source          = CASE WHEN excluded.source != '' THEN excluded.source ELSE earnings_calendar.source END,
            last_checked    = datetime('now')
        """,
        (
            ticker, event_date, event_time, int(bool(confirmed)), period_label,
            fiscal_year_end, consensus_revenue, consensus_eps, source,
        ),
    )
    conn.commit()
    conn.close()


def list_calendar_events(
    start: str | None = None,
    end: str | None = None,
    countries: list[str] | None = None,
    confirmed_only: bool = False,
) -> list[dict]:
    """List earnings events joined with company metadata.

    Dates are ISO YYYY-MM-DD; start/end are inclusive.
    """
    q = [
        "SELECT e.ticker, e.event_date, e.event_time, e.confirmed, e.period_label,",
        "       e.consensus_revenue, e.consensus_eps, e.source, e.last_checked,",
        "       c.company_name, c.country, c.sector, c.currency",
        "FROM earnings_calendar e",
        "LEFT JOIN company_master c ON c.ticker = e.ticker",
    ]
    where: list[str] = []
    args: list = []
    if start:
        where.append("e.event_date >= ?")
        args.append(start)
    if end:
        where.append("e.event_date <= ?")
        args.append(end)
    if countries:
        placeholders = ",".join("?" for _ in countries)
        where.append(f"c.country IN ({placeholders})")
        args.extend(countries)
    if confirmed_only:
        where.append("e.confirmed = 1")
    if where:
        q.append("WHERE " + " AND ".join(where))
    q.append("ORDER BY e.event_date ASC, e.ticker ASC")
    conn = get_conn()
    rows = conn.execute(" ".join(q), args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_calendar_for_ticker(ticker: str) -> list[dict]:
    """Upcoming + historical events for one ticker, newest first."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT ticker, event_date, event_time, confirmed, period_label,
               fiscal_year_end, consensus_revenue, consensus_eps, source, last_checked
        FROM earnings_calendar WHERE ticker = ?
        ORDER BY event_date DESC
        """,
        (ticker,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_run(run_id: str) -> dict | None:
    """Load one run by run_id with step_results parsed."""
    conn = get_conn()
    row = conn.execute("""
        SELECT r.run_id, r.ticker, r.mode, r.started_at, r.finished_at, r.overall_status, r.step_results, r.memo_path,
               c.company_name, c.country
        FROM pipeline_runs r
        LEFT JOIN company_master c ON c.ticker = r.ticker
        WHERE r.run_id = ?
    """, (run_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    steps_raw = d.get("step_results") or "[]"
    try:
        steps = json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw
    except Exception:
        steps = []
    d["step_results"] = steps
    d["warnings"] = sum(1 for s in steps if s.get("status") in ("partial", "failed"))
    return d
