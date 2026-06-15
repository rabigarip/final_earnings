"""Company-disclosed quarterly financials loader.

Phase 1 of the disclosed-source pipeline. Reads
`data/disclosed/{ticker}.json` if present and returns a dict shaped for
the existing `surprise_history` consumer in `render_jabal_valuation`:

    [{"period": "Q1 2025",
      "revenue_actual": 140675000,     # operating_income in raw units
      "revenue_estimate": None,
      "eps_actual": 0.008, "eps_estimate": None,
      "net_income_actual": 58561000,
      "_source": "company_disclosure",
      "_source_doc": "MSM_0325.pdf", ...}, ...]

When values exist for a period, they OVERRIDE any aggregator value
(MarketScreener / Investing) for the same period. The aggregator is
still consulted for the *estimate* side of the pair so the chart can
render an actual-vs-estimate surprise bar — disclosed reports only
publish actuals.

The JSON schema is intentionally simple so analysts can hand-edit the
file when they pull a new interim report; a downstream PDF-puller
(Phase 2) will populate it automatically.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


# Per-source unit scaling. JSON declares `units: "thousands"` and we
# multiply by 1000 to bring values to raw currency for parity with
# Investing's surprise_history (which stores raw, e.g. 175,220,000).
_UNIT_MULT = {"raw": 1.0, "ones": 1.0, "thousands": 1e3,
              "millions": 1e6, "billions": 1e9}


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_disclosed(ticker: str) -> dict | None:
    """Load and lightly normalise the disclosed JSON for `ticker`.

    Returns the parsed dict with values converted to raw currency, or
    `None` when no file exists / the JSON fails to parse. Safe to call
    unconditionally — every call site treats None as "no override".
    """
    p = _root() / "data" / "disclosed" / f"{ticker}.json"
    _auto = False
    if not p.is_file():
        # Fall back to auto-extracted grounding (Yahoo income statement), staged
        # under _staging/ by scripts/stage_yahoo_grounding.py. Flagged
        # auto_unverified so the deck/provenance present it as model-derived —
        # lower-confidence than a hand-checked IR figure, but it gives the
        # other ~485 names a grounded FY figure to cite instead of nothing.
        sp = _root() / "data" / "disclosed" / "_staging" / f"{ticker}.json"
        if sp.is_file():
            p = sp
            _auto = True
        else:
            return None
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Disclosed loader: %s parse failed: %s", p, exc)
        return None
    if not isinstance(raw, dict):
        return None
    if _auto or str(raw.get("_status", "")).startswith("auto"):
        raw = dict(raw)
        raw["_auto_grounding"] = True
    unit_mult = _UNIT_MULT.get((raw.get("units") or "").lower().strip(), 1.0)
    out = dict(raw)
    quarters = []
    for q in raw.get("quarterly", []) or []:
        if not isinstance(q, dict): continue
        nq = dict(q)
        # Scale every numeric except EPS (per-share — already in absolute
        # units regardless of the surrounding "thousands" / "millions").
        for k in ("interest_income", "net_interest_income",
                    "net_interest_islamic_combined", "fee_income_net",
                    "other_operating_income", "operating_income",
                    "net_income", "revenue"):
            v = nq.get(k)
            if isinstance(v, (int, float)):
                nq[k] = float(v) * unit_mult
        quarters.append(nq)
    out["quarterly"] = quarters
    return out


def overlay_surprise_history(ticker: str,
                                aggregator_rows: list[dict] | None
                                ) -> tuple[list[dict] | None, dict[str, str]]:
    """Merge company-disclosed actuals into an aggregator's
    surprise_history list. Returns `(merged_rows, source_map)`.

    `source_map` is `{period_label: source_label}` — populated only for
    periods where disclosed values overrode the aggregator. Used by the
    provenance.xlsx writer to attribute cells correctly.

    Disclosed values win for `revenue_actual` / `eps_actual` /
    `net_income_actual`. The aggregator value remains for the
    `*_estimate` side of the pair (companies don't disclose consensus
    estimates) and for periods the disclosed JSON doesn't cover.
    """
    d = load_disclosed(ticker)
    if not d:
        return aggregator_rows, {}

    quarterly = d.get("quarterly") or []
    if not quarterly:
        return aggregator_rows, {}

    # Index disclosed by period for O(1) lookup.
    by_period = {str(q.get("period") or "").strip(): q for q in quarterly}

    # Start with the aggregator rows as the base; overlay disclosed onto
    # matching periods. If the aggregator returned None / empty, build a
    # fresh list from the disclosed entries — the chart can still render
    # the actual track even without aggregator estimates.
    rows = list(aggregator_rows) if isinstance(aggregator_rows, list) else []
    rows_by_period = {str(r.get("period") or "").strip(): r for r in rows
                       if isinstance(r, dict)}
    source_map: dict[str, str] = {}

    for period, q in by_period.items():
        src_doc = q.get("source_doc") or ""
        op_income = q.get("operating_income")
        net_inc = q.get("net_income")
        eps = q.get("eps")
        # The surprise_history schema reuses "revenue_actual" for the
        # company's headline top-line metric. For banks that's operating
        # income (net interest income + fee + other) — banks don't have
        # "revenue" in the GAAP sense.
        existing = rows_by_period.get(period)
        if existing is None:
            new_row = {
                "period": period,
                "revenue_actual": op_income,
                "revenue_estimate": None,
                "eps_actual": eps,
                "eps_estimate": None,
                "revenue_surprise_pct": None,
                "eps_surprise_pct": None,
                "net_income_actual": net_inc,
                "_source": "company_disclosure",
                "_source_doc": src_doc,
            }
            rows.append(new_row)
            rows_by_period[period] = new_row
        else:
            # Overlay disclosed actuals onto the aggregator row. KEEP the
            # aggregator's estimate columns + computed surprise % so the
            # chart can still render a surprise bar.
            if isinstance(op_income, (int, float)):
                existing["revenue_actual"] = op_income
                # Recompute revenue_surprise_pct against the existing estimate.
                est = existing.get("revenue_estimate")
                if isinstance(est, (int, float)) and est != 0:
                    existing["revenue_surprise_pct"] = (op_income - est) / est * 100.0
            if isinstance(eps, (int, float)):
                existing["eps_actual"] = eps
                est = existing.get("eps_estimate")
                if isinstance(est, (int, float)) and est != 0:
                    existing["eps_surprise_pct"] = (eps - est) / est * 100.0
            if isinstance(net_inc, (int, float)):
                existing["net_income_actual"] = net_inc
            existing["_source"] = "company_disclosure"
            existing["_source_doc"] = src_doc
        source_map[period] = src_doc or "company_disclosure"

    # Re-sort newest-first so the chart's most-recent quarters render
    # at the right edge (matches the aggregator's existing convention).
    def _sort_key(r):
        s = str(r.get("period") or "")
        # "Q2 2025" → (2025, 2)
        import re as _re
        m = _re.search(r"Q(\d)\s*(\d{4})|(\d{4})\s*Q(\d)", s)
        if not m: return (0, 0)
        yr = int(m.group(2) or m.group(3))
        q = int(m.group(1) or m.group(4))
        return (yr, q)
    rows.sort(key=_sort_key, reverse=True)
    return rows, source_map
