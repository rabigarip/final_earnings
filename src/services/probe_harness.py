"""
Coverage-probe harness for Stage 1 (free-source coverage test).

Runs a fixed panel of tickers against every wired provider and writes
one row per (ticker, provider, field) to a CSV. Output is consumed by
the cross-source reconciler to produce the accuracy report.

Design contract:
- Every provider exposes `fetch(ticker, field) -> ProbeCell`.
- `ProbeCell` is a thin envelope: value, error, latency, raw_response_id.
- All raw responses persist to disk so re-runs of the reconciler don't
  re-hit the network.

The harness itself is dumb: it iterates panel × providers × fields,
calls the provider, writes the cell. Smarts (cross-source agreement,
confidence scoring, ground-truth selection) live in the reconciler.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# ─── Field catalog ──────────────────────────────────────────────────────
#
# These are the 12 fields the Stage 1 plan calls out. Naming is canonical
# across providers — each provider maps its native data to this set.
# Adding a field here means every provider needs a mapping for it
# (provider can return ProbeCell(error="not_implemented") to opt out).

FIELDS = [
    # Identity / market
    "current_price",
    "historical_prices",
    "market_cap",
    "company_profile",     # name, sector, industry, country, currency
    # Backward-looking financials
    "income_statement_annual",
    "income_statement_quarterly",
    "balance_sheet",
    "cash_flow",
    # Valuation
    "valuation_historical",  # P/E, P/B, EV/EBITDA, Yield by year
    "dividend_yield",
    "valuation_forward",     # forward P/E, EV/Sales for current FY-est
    # Analyst-driven
    "target_price",          # mean target + spread vs last close
    "rating_split",          # Buy/Hold/Sell counts
    "broker_actions",        # recent analyst-recommendation rows (date/headline/source)
]


# ─── Test panel ─────────────────────────────────────────────────────────
#
# 10 tickers across GCC, India, China/HK, signed off 2026-05-12.
# Zijin = parent 2899.HK (not the 2259.HK gold subsidiary).

PANEL = [
    "2222.SR",        # Saudi Aramco
    "ADNOCDRILL.AE",  # ADNOC Drilling
    "ADCB.AE",        # Abu Dhabi Commercial Bank
    "BKMB.OM",        # Bank Muscat
    "OQEP.OM",        # OQ Exploration & Production
    "JINDALSTEL.NS",  # Jindal Steel & Power
    "ICICIBANK.BO",   # ICICI Bank
    "0700.HK",        # Tencent
    "2899.HK",        # Zijin Mining Group (parent)
    "1398.HK",        # ICBC
]


# ─── Cell envelope ──────────────────────────────────────────────────────


@dataclass
class ProbeCell:
    """Envelope for one (ticker, provider, field) probe result.

    `value` is the canonicalised value (number, list, dict, or str).
    Each provider's `fetch` is responsible for normalising native output.
    `raw_response_id` points at a JSON file on disk where we persisted
    the unparsed response — keeps the run reproducible.
    """
    ticker:           str
    provider:         str
    field:            str
    value:            Any = None
    error:            str = ""
    latency_ms:       float = 0.0
    fetched_at:       str = ""
    raw_response_id:  str = ""
    # `units` carries currency/scale hints when relevant ("USD-M",
    # "SARM", "%"). Empty for non-numeric fields.
    units:            str = ""
    # `as_of` is the data date when applicable (e.g. quarterly statement
    # end-date). Empty for live-quote fields.
    as_of:            str = ""


# ─── Provider contract ──────────────────────────────────────────────────


class Provider:
    """Stable contract every source must implement.

    Subclasses set `name` (used in CSV column / cache path) and either
    override `fetch` directly OR implement per-field `_fetch_<field>`
    methods that the default `fetch` dispatches to.
    """

    name: str = "base"

    def fetch(self, ticker: str, field: str) -> ProbeCell:
        method_name = f"_fetch_{field}"
        method: Optional[Callable] = getattr(self, method_name, None)
        cell = ProbeCell(
            ticker=ticker, provider=self.name, field=field,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        if method is None:
            cell.error = "not_implemented"
            return cell
        t0 = time.monotonic()
        try:
            value, units, as_of, raw_id = method(ticker)
            cell.value = value
            cell.units = units or ""
            cell.as_of = as_of or ""
            cell.raw_response_id = raw_id or ""
        except NotImplementedError:
            cell.error = "not_implemented"
        except Exception as exc:
            cell.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            cell.latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return cell


# ─── Cache for raw responses ────────────────────────────────────────────


def cache_root() -> Path:
    """Where raw responses go. One subdir per provider per ticker."""
    return Path("cache/probe")


def persist_raw(provider: str, ticker: str, field: str, payload: Any) -> str:
    """Write a raw response to disk and return the cache id.

    The id is the relative path under `cache_root()`, used as
    `ProbeCell.raw_response_id` so the reconciler can re-read it
    without hitting the network.
    """
    safe_ticker = ticker.replace(".", "_").replace("/", "_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rel = Path(provider) / safe_ticker / f"{field}_{ts}.json"
    full = cache_root() / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    try:
        # JSON for everything; non-serializable values get stringified.
        full.write_text(json.dumps(payload, default=str, indent=2))
    except Exception as exc:
        full.write_text(json.dumps({"error": str(exc), "repr": repr(payload)}))
    return str(rel)


# ─── CSV writer ─────────────────────────────────────────────────────────


COVERAGE_CSV_HEADER = [
    "ticker", "provider", "field",
    "value", "units", "as_of",
    "error", "latency_ms", "fetched_at", "raw_response_id",
]


def write_coverage_row(csv_path: Path, cell: ProbeCell) -> None:
    """Append one ProbeCell to the coverage matrix CSV."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COVERAGE_CSV_HEADER)
        # Stringify complex values; raw cache file holds the structured form.
        val = cell.value
        if val is not None and not isinstance(val, (str, int, float, bool)):
            try:
                val = json.dumps(val, default=str)
            except Exception:
                val = str(val)
        w.writerow([
            cell.ticker, cell.provider, cell.field,
            val if val is not None else "",
            cell.units, cell.as_of,
            cell.error, cell.latency_ms, cell.fetched_at,
            cell.raw_response_id,
        ])


# ─── Summary aggregator ─────────────────────────────────────────────────


def summarize(csv_path: Path) -> dict[str, Any]:
    """Read the coverage CSV and compute a per-provider × per-field
    success matrix. Returns nested dict + the global hit rate."""
    by_pf: dict[tuple[str, str], dict[str, int]] = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["provider"], row["field"])
            slot = by_pf.setdefault(key, {"hit": 0, "miss": 0, "ni": 0, "err": 0})
            err = row.get("error") or ""
            val = row.get("value") or ""
            if err == "not_implemented":
                slot["ni"] += 1
            elif err:
                slot["err"] += 1
            elif val:
                slot["hit"] += 1
            else:
                slot["miss"] += 1
    return by_pf
