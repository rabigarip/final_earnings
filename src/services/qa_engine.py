"""
QA engine: source-basis validation layer for earnings preview memos.

Builds source snapshots, normalizes fields with metadata, runs formulas/rules,
valuation basis checks, investment view fact-pack and guardrail, and exports
internal QA audit. The renderer consumes only validated memo_data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    from config.qa_config import (
        PERCENTAGE_TOLERANCE,
        SUMMARY_STRIP_PRICE_BASIS,
        PRIMARY_SOURCE,
        STALE_THRESHOLD_SECONDS,
        TIMESTAMP_MISMATCH_THRESHOLD_SECONDS,
        PRICE_MISMATCH_PCT_THRESHOLD,
        SUPPRESS_ON_AMBIGUOUS_VALUATION,
        SUPPRESS_ON_FAILED_FORMULA,
        RATIO_TOLERANCE,
    )
except Exception:
    PERCENTAGE_TOLERANCE = 0.1
    SUMMARY_STRIP_PRICE_BASIS = "consensus"
    STALE_THRESHOLD_SECONDS = 86400 * 2
    TIMESTAMP_MISMATCH_THRESHOLD_SECONDS = 3600
    PRICE_MISMATCH_PCT_THRESHOLD = 2.0
    SUPPRESS_ON_AMBIGUOUS_VALUATION = True
    SUPPRESS_ON_FAILED_FORMULA = True
    RATIO_TOLERANCE = 0.01
    PRIMARY_SOURCE = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Field metadata
# ═══════════════════════════════════════════════════════════════════════════════

PASS = "pass"
MISSING = "missing"
STALE = "stale"
MISMATCHED = "mismatched"
AMBIGUOUS = "ambiguous"
SUPPRESSED = "suppressed"
MANUALLY_ENTERED = "manually_entered"
ESTIMATED = "estimated"

RENDER_OK = {PASS, STALE, ESTIMATED, MANUALLY_ENTERED}
SUPPRESS_RENDER = {MISMATCHED, AMBIGUOUS, SUPPRESSED}


@dataclass
class QualifiedField:
    value: Any = None
    display_value: Any = None
    source_name: str = ""
    source_url: str = ""
    scrape_timestamp: Any = None
    raw_value: Any = None
    formula: str | None = None
    status: str = PASS
    notes: str = ""

    def should_display(self) -> bool:
        return self.status in RENDER_OK

    def get_display(self, default: str = "—") -> Any:
        if not self.should_display():
            return default
        if self.display_value is not None:
            return self.display_value
        return self.value if self.value is not None else default

    def to_audit_dict(self, section: str, field_name: str) -> dict:
        return {
            "section": section,
            "field_name": field_name,
            "displayed_value": self.get_display(),
            "raw_source_value": self.raw_value,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "scrape_timestamp": str(self.scrape_timestamp) if self.scrape_timestamp else None,
            "formula_used": self.formula,
            "status": self.status,
            "notes": self.notes,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Source snapshots
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SourceSnapshot:
    source_name: str
    source_url: str = ""
    scrape_timestamp: datetime | None = None
    as_of_date: str | None = None
    company: str = ""
    ticker: str = ""
    currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "source_url": self.source_url,
            "scrape_timestamp": self.scrape_timestamp.isoformat() if self.scrape_timestamp else None,
            "as_of_date": self.as_of_date,
            "company": self.company,
            "ticker": self.ticker,
            "currency": self.currency,
            "raw": self.raw,
        }


@dataclass
class SourceSnapshots:
    quote_snapshot: SourceSnapshot | None = None
    consensus_snapshot: SourceSnapshot | None = None
    quarterly_results_snapshot: SourceSnapshot | None = None
    valuation_snapshot: SourceSnapshot | None = None
    dividend_snapshot: SourceSnapshot | None = None
    annual_forecasts_snapshot: SourceSnapshot | None = None
    earnings_date_snapshot: SourceSnapshot | None = None

    def to_dict(self) -> dict:
        return {
            "quote": self.quote_snapshot.to_dict() if self.quote_snapshot else None,
            "consensus": self.consensus_snapshot.to_dict() if self.consensus_snapshot else None,
            "quarterly_results": self.quarterly_results_snapshot.to_dict() if self.quarterly_results_snapshot else None,
            "valuation": self.valuation_snapshot.to_dict() if self.valuation_snapshot else None,
            "dividend": self.dividend_snapshot.to_dict() if self.dividend_snapshot else None,
            "annual_forecasts": self.annual_forecasts_snapshot.to_dict() if self.annual_forecasts_snapshot else None,
            "earnings_date": self.earnings_date_snapshot.to_dict() if self.earnings_date_snapshot else None,
        }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def build_source_snapshots(payload) -> SourceSnapshots:
    ts = _now_utc()
    c = getattr(payload, "company", None)
    company_name = c.company_name if c else ""
    ticker = c.ticker if c else ""
    currency = (c.currency or "USD").strip() if c else "SAR"

    quote = getattr(payload, "quote", None)
    quote_snapshot = None
    if quote:
        quote_snapshot = SourceSnapshot(
            source_name="yahoo", source_url="https://finance.yahoo.com/",
            scrape_timestamp=ts, company=company_name, ticker=ticker, currency=currency,
            raw={
                "price": getattr(quote, "price", None),
                "change": getattr(quote, "change", None),
                "change_pct": getattr(quote, "change_pct", None),
                "market_cap": getattr(quote, "market_cap", None),
                "currency": getattr(quote, "currency", currency),
            },
        )

    cs = getattr(payload, "consensus_summary", None) or {}
    consensus_snapshot = None
    if cs or getattr(payload, "ms_summary", None):
        ms_summary = getattr(payload, "ms_summary", None) or {}
        consensus_snapshot = SourceSnapshot(
            source_name="marketscreener", source_url="/consensus/",
            scrape_timestamp=ts, company=company_name, ticker=ticker, currency=currency,
            raw={
                "consensus_rating": cs.get("consensus_rating") or ms_summary.get("mean_consensus"),
                "analyst_count": cs.get("analyst_count"),
                "average_target_price": cs.get("average_target_price") or cs.get("avg_target"),
                "low_target_price": cs.get("low_target_price"),
                "high_target_price": cs.get("high_target_price"),
                "last_close_price": cs.get("last_close_price"),
                "upside_to_average_target_pct": cs.get("upside_to_average_target_pct"),
                "downside_to_low_target_pct": cs.get("downside_to_low_target_pct"),
            },
        )

    cal = getattr(payload, "ms_calendar_events", None) or {}
    qr = cal.get("quarterly_results", {}) or {}
    quarterly_results_snapshot = None
    if qr or cal.get("next_expected_earnings_date"):
        quarterly_results_snapshot = SourceSnapshot(
            source_name="marketscreener", source_url="/calendar/",
            scrape_timestamp=ts, company=company_name, ticker=ticker, currency=currency,
            raw={
                "quarters": qr.get("quarters", []),
                "rows": qr.get("rows", []),
                "next_expected_earnings_date": cal.get("next_expected_earnings_date"),
                "next_expected_earnings_label": cal.get("next_expected_earnings_label"),
            },
        )

    earnings_date_snapshot = quarterly_results_snapshot

    vm = getattr(payload, "ms_valuation_multiples", None) or {}
    valuation_snapshot = None
    if vm:
        valuation_snapshot = SourceSnapshot(
            source_name="marketscreener", source_url="/valuation/",
            scrape_timestamp=ts, company=company_name, ticker=ticker, currency=currency,
            raw=dict(vm),
        )

    ed = getattr(payload, "ms_eps_dividend_forecasts", None) or {}
    dividend_snapshot = None
    if ed:
        dividend_snapshot = SourceSnapshot(
            source_name="marketscreener", source_url="/valuation-dividend/",
            scrape_timestamp=ts, company=company_name, ticker=ticker, currency=currency,
            raw=dict(ed),
        )

    ann = getattr(payload, "ms_annual_forecasts", None) or {}
    annual_forecasts_snapshot = None
    if ann:
        annual_forecasts_snapshot = SourceSnapshot(
            source_name="marketscreener", source_url="/finances/",
            scrape_timestamp=ts, company=company_name, ticker=ticker, currency=currency,
            raw=dict(ann),
        )

    return SourceSnapshots(
        quote_snapshot=quote_snapshot,
        consensus_snapshot=consensus_snapshot,
        quarterly_results_snapshot=quarterly_results_snapshot,
        valuation_snapshot=valuation_snapshot,
        dividend_snapshot=dividend_snapshot,
        annual_forecasts_snapshot=annual_forecasts_snapshot,
        earnings_date_snapshot=earnings_date_snapshot,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Normalize fields (build_memo_data)
# ═══════════════════════════════════════════════════════════════════════════════

def _field(
    value: Any = None, display_value: Any = None,
    source_name: str = "", source_url: str = "",
    scrape_timestamp: Any = None, raw_value: Any = None,
    formula: str | None = None, status: str = PASS, notes: str = "",
) -> dict:
    return {
        "value": value,
        "display_value": display_value if display_value is not None else value,
        "source_name": source_name, "source_url": source_url,
        "scrape_timestamp": scrape_timestamp, "raw_value": raw_value,
        "formula": formula, "status": status, "notes": notes,
    }


def _ts(snap: Any) -> Any:
    if snap and getattr(snap, "scrape_timestamp", None):
        return getattr(snap, "scrape_timestamp")
    return None


def build_memo_data(payload, snapshots: SourceSnapshots) -> dict:
    c = getattr(payload, "company", None)
    memo = getattr(payload, "memo_computed", None) or {}
    q = getattr(payload, "quote", None)
    currency = (getattr(c, "currency", None) or "USD").strip() if c else "SAR"
    ticker = (getattr(c, "ticker", "") or "").strip() if c else ""
    company_name = getattr(c, "company_name", "") if c else ""
    # Section-level lineage validation: do not use MS-derived data when entity/ticker mismatch or contamination
    payload_entity_match = getattr(payload, "payload_entity_match", True)
    payload_source_ticker = (getattr(payload, "payload_source_ticker", "") or "").strip()
    use_ms_sections = payload_entity_match and (payload_source_ticker == ticker) and not getattr(payload, "cross_company_contamination_detected", False)
    cs = getattr(payload, "consensus_summary", None) or {} if use_ms_sections else {}

    entity = {
        "company_name": company_name, "ticker": ticker, "currency": currency,
        "is_bank": getattr(c, "is_bank", False) if c else False,
        "industry": getattr(c, "industry", "") if c else "",
    }

    quote_price = (getattr(q, "price", None) if q else None) if isinstance(getattr(q, "price", None), (int, float)) else None
    consensus_price = cs.get("last_close_price") if isinstance(cs.get("last_close_price"), (int, float)) else None
    target = cs.get("average_target_price") or cs.get("avg_target")
    exp_date = memo.get("next_earnings_date") or (getattr(payload, "ms_calendar_events", None) or {}).get("next_expected_earnings_date")
    header = {
        "expected_report_date": _field(exp_date, str(exp_date) if exp_date else None, "marketscreener", "/calendar/", _ts(snapshots.earnings_date_snapshot), exp_date, status=MISSING if not exp_date else PASS),
        "recommendation": _field(cs.get("consensus_rating") or cs.get("mean_consensus"), None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), cs.get("consensus_rating"), status=MISSING if not cs.get("consensus_rating") and not cs.get("mean_consensus") else PASS),
        "analyst_count": _field(cs.get("analyst_count"), None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), cs.get("analyst_count"), status=MISSING if cs.get("analyst_count") is None else PASS),
        "quote_price": _field(quote_price, None, "yahoo", "", _ts(snapshots.quote_snapshot), quote_price, status=MISSING if quote_price is None else PASS),
        "consensus_page_price": _field(consensus_price, None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), consensus_price, status=MISSING if consensus_price is None else PASS),
        "average_target_price": _field(target, None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), target, status=MISSING if target is None else PASS),
        "upside_pct": _field(memo.get("spread_pct") or cs.get("upside_to_average_target_pct"), None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), memo.get("spread_pct"), formula="(target/price - 1)*100", status=PASS),
    }

    preview_short = memo.get("preview_quarter_short") or "1Q26"
    prev_q_short = memo.get("prior_quarter_short") or ""
    same_q_short = memo.get("prior_year_same_quarter_short") or ""
    cal_next = memo.get("calendar_next_quarter") or {}
    cal_prior = memo.get("calendar_prior_quarter_released") or {}
    cal_same = memo.get("calendar_same_q_prior_yr_released") or {}
    available = memo.get("calendar_quarterly_available_metrics") or []
    key_preview = []
    for key in ["net_sales", "ebitda", "ebit", "net_income", "eps"]:
        if key not in available and cal_next.get(key) is None:
            continue
        if c and getattr(c, "is_bank", False) and key == "ebitda":
            continue
        cons = cal_next.get(key)
        prior = cal_prior.get(key)
        same_ly = cal_same.get(key)
        key_preview.append({
            "metric_key": key,
            "consensus": _field(cons, None, "marketscreener", "/calendar/", _ts(snapshots.quarterly_results_snapshot), cons),
            "prior_actual": _field(prior, None, "marketscreener", "/calendar/", _ts(snapshots.quarterly_results_snapshot), prior),
            "same_q_prior_yr": _field(same_ly, None, "marketscreener", "/calendar/", _ts(snapshots.quarterly_results_snapshot), same_ly),
            "qoq_pct": _field(memo.get("qoq_revenue_pct") if key == "net_sales" else memo.get("qoq_ni_pct") if key == "net_income" else memo.get("qoq_eps_pct") if key == "eps" else None, None, "computed", "", None, None, formula="(curr-prior)/prior*100"),
            "yoy_pct": _field(memo.get("yoy_revenue_pct_table") if key == "net_sales" else memo.get("yoy_ni_pct_table") if key == "net_income" else memo.get("yoy_eps_pct_table") if key == "eps" else None, None, "computed", "", None, None, formula="(curr-same_ly)/same_ly*100"),
        })

    operating_metrics = {"rows": [], "headers": {"preview_short": preview_short, "prev_q_short": prev_q_short, "same_q_short": same_q_short}}

    vm = getattr(payload, "ms_valuation_multiples", None) or {}
    pe_list = vm.get("pe") or []
    pe_val = pe_list[0] if pe_list and len(pe_list) > 0 else None
    street_snapshot = {
        "recommendation": _field(cs.get("consensus_rating"), None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), cs.get("consensus_rating")),
        "analyst_count": _field(cs.get("analyst_count"), None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), cs.get("analyst_count")),
        "target": _field(target, None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), target),
        "upside_pct": _field(memo.get("spread_pct"), None, "marketscreener", "/consensus/", _ts(snapshots.consensus_snapshot), memo.get("spread_pct")),
        "pe": _field(pe_val, None, "marketscreener", "/valuation/", _ts(snapshots.valuation_snapshot), pe_val, status=MISSING if pe_val is None else PASS, notes="Unlabeled P/E; reconcile before display"),
    }

    recent_execution = {
        "revenue_surprise_history": memo.get("revenue_surprise_history") or [],
        "eps_surprise_history": memo.get("eps_surprise_history") or [],
        "ni_surprise_history": memo.get("ni_surprise_history") or [],
        "avg_revenue_surprise_pct": _field(memo.get("avg_revenue_surprise_pct"), None, "computed", "", None, memo.get("avg_revenue_surprise_pct")),
        "avg_eps_surprise_pct": _field(memo.get("avg_eps_surprise_pct"), None, "computed", "", None, memo.get("avg_eps_surprise_pct")),
        "avg_ni_surprise_pct": _field(memo.get("avg_ni_surprise_pct"), None, "computed", "", None, memo.get("avg_ni_surprise_pct")),
        "consecutive_revenue_beats": memo.get("consecutive_revenue_beats"),
    }

    from src.services.generate_report import _sector_operating_kpis_and_what_matters
    _, what_matters, _ = _sector_operating_kpis_and_what_matters(c)

    appendix_a = _normalize_appendix_a(payload, snapshots) if use_ms_sections else []
    appendix_b = _normalize_appendix_b(payload, snapshots) if use_ms_sections else {"quarters": [], "rows": [], "has_data": False}
    appendix_c = _normalize_appendix_c(payload, snapshots) if use_ms_sections else {"rows": []}
    appendix_d = _normalize_appendix_d(payload, snapshots) if use_ms_sections else []

    if not use_ms_sections:
        # Blank MS-derived header fields only; keep Yahoo quote_price so report can show at least current price
        ms_header_keys = {"expected_report_date", "recommendation", "analyst_count", "consensus_page_price", "average_target_price", "upside_pct"}
        for k in ms_header_keys:
            if k in header:
                header[k] = _field(None, None, "suppressed", "", None, None, status=SUPPRESSED, notes="MS data suppressed (entity mismatch or contamination)")
        key_preview = []
        street_snapshot = {k: _field(None, None, "suppressed", "", None, None, status=SUPPRESSED) for k in list(street_snapshot.keys())}

    return {
        "entity": entity, "header": header, "key_preview": key_preview,
        "operating_metrics": operating_metrics, "street_snapshot": street_snapshot,
        "recent_execution": recent_execution, "what_matters": what_matters,
        "appendix_a": appendix_a, "appendix_b": appendix_b,
        "appendix_c": appendix_c, "appendix_d": appendix_d,
        "memo_raw": memo, "preview_short": preview_short,
        "prior_quarter_short": prev_q_short,
        "prior_year_same_quarter_short": same_q_short,
        "qa": {},
        "ms_section_suppressed_due_to_missing_current_data": getattr(payload, "ms_section_suppressed_due_to_missing_current_data", False),
        "ms_section_suppressed_due_to_entity_mismatch": getattr(payload, "ms_section_suppressed_due_to_entity_mismatch", False),
        "ms_section_suppressed_due_to_contamination": getattr(payload, "ms_section_suppressed_due_to_contamination", False),
        "reused_default_payload_detected": getattr(payload, "reused_default_payload_detected", False),
        "payload_entity_match": payload_entity_match,
        "payload_source_ticker": payload_source_ticker,
    }


def _normalize_appendix_a(payload, snapshots) -> list:
    ann = (getattr(payload, "ms_annual_forecasts", None) or {}).get("annual", {}) or {}
    ed = getattr(payload, "ms_eps_dividend_forecasts", None) or {}
    periods = ann.get("periods", []) or ed.get("periods", [])
    if not periods:
        return []
    rows = []
    for i, p in enumerate(periods[:6]):
        row = {"period": str(p), "source": "marketscreener", "status": PASS}
        if ann.get("net_sales") and i < len(ann["net_sales"]):
            row["net_sales"] = _field(ann["net_sales"][i], None, "marketscreener", "/finances/", _ts(snapshots.annual_forecasts_snapshot), ann["net_sales"][i])
        if ann.get("net_income") and i < len(ann["net_income"]):
            row["net_income"] = _field(ann["net_income"][i], None, "marketscreener", "/finances/", _ts(snapshots.annual_forecasts_snapshot), ann["net_income"][i])
        if ed.get("eps") and i < len(ed["eps"]):
            row["eps"] = _field(ed["eps"][i], None, "marketscreener", "/valuation-dividend/", _ts(snapshots.dividend_snapshot), ed["eps"][i])
        if ed.get("dividend_per_share") and i < len(ed["dividend_per_share"]):
            row["dps"] = _field(ed["dividend_per_share"][i], None, "marketscreener", "/valuation-dividend/", _ts(snapshots.dividend_snapshot), ed["dividend_per_share"][i])
        rows.append(row)
    return rows


def _normalize_appendix_b(payload, snapshots) -> dict:
    qr = (getattr(payload, "ms_calendar_events", None) or {}).get("quarterly_results", {}) or {}
    quarters = qr.get("quarters", [])
    rows_data = qr.get("rows", [])
    if not quarters or not rows_data:
        return {"quarters": [], "rows": [], "has_data": False}
    rows = []
    for r in rows_data:
        key = r.get("metric_key")
        if key == "announcement_date":
            continue
        by_q = r.get("by_quarter", [])
        row = {"metric_key": key, "by_quarter": []}
        for i, cell in enumerate(by_q):
            if i >= len(quarters):
                break
            released = cell.get("released")
            forecast = cell.get("forecast")
            spread = cell.get("spread_pct")
            row["by_quarter"].append({
                "released": _field(released, None, "marketscreener", "/calendar/", _ts(snapshots.quarterly_results_snapshot), released),
                "forecast": _field(forecast, None, "marketscreener", "/calendar/", _ts(snapshots.quarterly_results_snapshot), forecast),
                "surprise_pct": _field(spread, None, "computed", "", None, spread, formula="(actual-forecast)/forecast*100"),
            })
        rows.append(row)
    return {"quarters": quarters, "rows": rows, "has_data": True}


def _normalize_appendix_c(payload, snapshots) -> dict:
    ed = getattr(payload, "ms_eps_dividend_forecasts", None) or {}
    periods = ed.get("periods", []) or []
    eps = ed.get("eps", []) or []
    dps = ed.get("dividend_per_share", []) or []
    has_eps = any(v is not None for v in eps)
    has_dps = any(v is not None for v in dps)
    if not periods or (not has_eps and not has_dps):
        return {"has_data": False, "periods": [], "eps": [], "dps": []}
    return {"has_data": True, "periods": periods, "eps": eps, "dps": dps, "source": "marketscreener", "status": PASS}


def _normalize_appendix_d(payload, snapshots) -> list:
    vm = getattr(payload, "ms_valuation_multiples", None) or {}
    if not vm or not vm.get("periods"):
        return []
    periods = vm.get("periods", [])
    pe = vm.get("pe", []) or []
    pbr = vm.get("pbr", []) or []
    yld = vm.get("yield_pct", []) or []
    ev_ebit = vm.get("ev_ebit", []) or []
    rows = []
    for i, p in enumerate(periods):
        rows.append({
            "period": str(p),
            "pe": _field(pe[i] if i < len(pe) else None, None, "marketscreener", "/valuation/", _ts(snapshots.valuation_snapshot), pe[i] if i < len(pe) else None),
            "pbr": _field(pbr[i] if i < len(pbr) else None, None, "marketscreener", "/valuation/", _ts(snapshots.valuation_snapshot), pbr[i] if i < len(pbr) else None),
            "yield_pct": _field(yld[i] if i < len(yld) else None, None, "marketscreener", "/valuation/", _ts(snapshots.valuation_snapshot), yld[i] if i < len(yld) else None),
            "ev_ebit": _field(ev_ebit[i] if i < len(ev_ebit) else None, None, "marketscreener", "/valuation/", _ts(snapshots.valuation_snapshot), ev_ebit[i] if i < len(ev_ebit) else None),
        })
    return rows


MemoData = dict


# ═══════════════════════════════════════════════════════════════════════════════
# QA formulas
# ═══════════════════════════════════════════════════════════════════════════════

def _get_val(f: dict) -> float | None:
    if not f or not isinstance(f, dict):
        return None
    v = f.get("display_value") if f.get("display_value") is not None else f.get("value")
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _set_recomputed(f: dict, recomputed: float | None, tolerance: float = PERCENTAGE_TOLERANCE, stored: float | None = None) -> None:
    if not isinstance(f, dict):
        return
    if recomputed is None:
        return
    f["recomputed_value"] = recomputed
    if stored is not None and tolerance is not None:
        diff = abs(recomputed - stored) if stored != 0 else (abs(recomputed) if recomputed else 0)
        if diff > tolerance:
            f["status"] = MISMATCHED
            f["notes"] = f.get("notes", "") + f"; recomputed={recomputed}, stored={stored}"
        else:
            f["display_value"] = recomputed
            f["status"] = PASS


def recompute_header_upside(memo_data: dict) -> None:
    header = memo_data.get("header") or {}
    target_f = header.get("average_target_price") or {}
    price_f = header.get("consensus_page_price") if SUMMARY_STRIP_PRICE_BASIS == "consensus" else header.get("quote_price")
    price_f = price_f or {}
    target = _get_val(target_f)
    price = _get_val(price_f)
    upside_f = header.get("upside_pct") or {}
    stored = _get_val(upside_f)
    if target is not None and price is not None and price != 0:
        recomputed = round((target / price - 1) * 100, 1)
        _set_recomputed(upside_f, recomputed, PERCENTAGE_TOLERANCE, stored)
        upside_f["formula"] = "(target/price - 1)*100"
        upside_f["display_value"] = recomputed
    else:
        upside_f["status"] = upside_f.get("status") or "missing"
        upside_f["notes"] = "Cannot recompute: target or price missing"


def recompute_key_preview_qoq_yoy(memo_data: dict) -> None:
    for row in memo_data.get("key_preview") or []:
        cons = _get_val(row.get("consensus") or {})
        prior = _get_val(row.get("prior_actual") or {})
        same_ly = _get_val(row.get("same_q_prior_yr") or {})
        qoq_f = row.get("qoq_pct") or {}
        yoy_f = row.get("yoy_pct") or {}
        if cons is not None and prior is not None and prior != 0:
            recomputed_qoq = round((cons - prior) / prior * 100, 1)
            _set_recomputed(qoq_f, recomputed_qoq, PERCENTAGE_TOLERANCE, _get_val(qoq_f))
            qoq_f["display_value"] = recomputed_qoq
        if cons is not None and same_ly is not None and same_ly != 0:
            recomputed_yoy = round((cons - same_ly) / same_ly * 100, 1)
            _set_recomputed(yoy_f, recomputed_yoy, PERCENTAGE_TOLERANCE, _get_val(yoy_f))
            yoy_f["display_value"] = recomputed_yoy


def recompute_appendix_b_surprise(memo_data: dict) -> None:
    app_b = memo_data.get("appendix_b") or {}
    if not app_b.get("has_data"):
        return
    for row in app_b.get("rows") or []:
        for cell in row.get("by_quarter") or []:
            released = _get_val(cell.get("released") or {})
            forecast = _get_val(cell.get("forecast") or {})
            surprise_f = cell.get("surprise_pct") or {}
            if released is not None and forecast is not None and forecast != 0:
                recomputed = round((released - forecast) / abs(forecast) * 100, 1)
                _set_recomputed(surprise_f, recomputed, PERCENTAGE_TOLERANCE, _get_val(surprise_f))
                surprise_f["display_value"] = recomputed


def recompute_recent_execution(memo_data: dict) -> None:
    rec = memo_data.get("recent_execution") or {}
    rev = rec.get("revenue_surprise_history") or []
    eps = rec.get("eps_surprise_history") or []
    ni = rec.get("ni_surprise_history") or []
    for label, entries in [("revenue", rev), ("eps", eps), ("ni", ni)]:
        if not entries:
            continue
        pcts = [e.get("surprise_pct") for e in entries if e.get("surprise_pct") is not None]
        if not pcts:
            continue
        avg = round(sum(pcts) / len(pcts), 1)
        if label == "revenue":
            key = "avg_revenue_surprise_pct"
        elif label == "eps":
            key = "avg_eps_surprise_pct"
        else:
            key = "avg_ni_surprise_pct"
        f = rec.get(key)
        if isinstance(f, dict):
            f["recomputed_value"] = avg
            f["display_value"] = avg
            stored = _get_val(f)
            if stored is not None and abs(avg - stored) > PERCENTAGE_TOLERANCE:
                f["status"] = MISMATCHED
            else:
                f["status"] = PASS


# ═══════════════════════════════════════════════════════════════════════════════
# QA rules
# ═══════════════════════════════════════════════════════════════════════════════

def _ts_sec(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.timestamp()
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


# Flag surprise % for review when abs(surprise) exceeds this (e.g. 50% or 100%)
EXTREME_SURPRISE_PCT_THRESHOLD = 50.0


def _check_extreme_surprise(memo_data: dict) -> None:
    """Set recent_execution.extreme_surprise_flagged when any surprise % is very large (for QA review)."""
    rec = memo_data.get("recent_execution") or {}
    details = []
    for key, label in [("revenue_surprise_history", "Revenue"), ("eps_surprise_history", "EPS"), ("ni_surprise_history", "Net income")]:
        for e in rec.get(key) or []:
            pct = e.get("surprise_pct")
            if pct is not None and abs(float(pct)) > EXTREME_SURPRISE_PCT_THRESHOLD:
                details.append({"metric": label, "period": e.get("period"), "surprise_pct": pct})
    if details:
        rec["extreme_surprise_flagged"] = True
        rec["extreme_surprise_details"] = details[:10]


def apply_qa_rules(memo_data: dict, snapshots: SourceSnapshots) -> None:
    _check_header_price_mismatch(memo_data, snapshots)
    _check_stale(memo_data, snapshots)
    _suppress_failed_formula(memo_data)
    _check_extreme_surprise(memo_data)


def _check_header_price_mismatch(memo_data: dict, snapshots: SourceSnapshots) -> None:
    header = memo_data.get("header") or {}
    qp = header.get("quote_price") or {}
    cp = header.get("consensus_page_price") or {}
    qv = qp.get("value") if isinstance(qp.get("value"), (int, float)) else None
    cv = cp.get("value") if isinstance(cp.get("value"), (int, float)) else None
    if qv is not None and cv is not None and cv != 0:
        diff_pct = abs(qv - cv) / abs(cv) * 100
        if diff_pct > PRICE_MISMATCH_PCT_THRESHOLD:
            header["_price_mismatch_pct"] = round(diff_pct, 2)
            header["_notes"] = header.get("_notes", "") + f"; price diff {round(diff_pct, 1)}% (quote vs consensus)"


def _check_stale(memo_data: dict, snapshots: SourceSnapshots) -> None:
    now = datetime.now(timezone.utc).timestamp()
    threshold = now - STALE_THRESHOLD_SECONDS
    for section_key in ("header", "street_snapshot"):
        section = memo_data.get(section_key) or {}
        for k, v in section.items():
            if k.startswith("_"):
                continue
            if not isinstance(v, dict) or "scrape_timestamp" not in v:
                continue
            ts = _ts_sec(v.get("scrape_timestamp"))
            if ts is not None and ts < threshold and v.get("status") == PASS:
                v["status"] = STALE
                v["notes"] = (v.get("notes") or "") + "; snapshot older than threshold"


def _suppress_failed_formula(memo_data: dict) -> None:
    if not SUPPRESS_ON_FAILED_FORMULA:
        return
    for section_key in ("header", "key_preview", "street_snapshot", "recent_execution", "appendix_b"):
        section = memo_data.get(section_key)
        if section is None:
            continue
        if section_key == "key_preview":
            for row in section:
                for k in ("consensus", "prior_actual", "same_q_prior_yr", "qoq_pct", "yoy_pct"):
                    f = row.get(k)
                    if isinstance(f, dict) and f.get("status") == MISMATCHED:
                        f["status"] = SUPPRESSED
            continue
        if section_key == "appendix_b":
            for row in section.get("rows") or []:
                for cell in row.get("by_quarter") or []:
                    for k in ("released", "forecast", "surprise_pct"):
                        f = cell.get(k)
                        if isinstance(f, dict) and f.get("status") == MISMATCHED:
                            f["status"] = SUPPRESSED
            continue
        for k, v in (section or {}).items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and v.get("status") == MISMATCHED:
                v["status"] = SUPPRESSED


# ═══════════════════════════════════════════════════════════════════════════════
# Valuation basis
# ═══════════════════════════════════════════════════════════════════════════════

def apply_valuation_basis(memo_data: dict) -> None:
    _label_appendix_d(memo_data)
    _street_snapshot_pe(memo_data)


def _label_appendix_d(memo_data: dict) -> None:
    rows = memo_data.get("appendix_d") or []
    for row in rows:
        period = row.get("period", "")
        for key in ("pe", "pbr", "yield_pct", "ev_ebit"):
            f = row.get(key)
            if not isinstance(f, dict):
                continue
            if f.get("value") is not None or f.get("display_value") is not None:
                f["label"] = f"{_multi_name(key)} {period}"
                f["price_basis"] = "valuation_snapshot"
                if f.get("status") not in (AMBIGUOUS, SUPPRESSED):
                    f["status"] = PASS
            else:
                f["status"] = MISSING


def _multi_name(key: str) -> str:
    return {"pe": "P/E", "pbr": "P/B", "yield_pct": "Div. Yield", "ev_ebit": "EV/EBIT"}.get(key, key)


def _street_snapshot_pe(memo_data: dict) -> None:
    street = memo_data.get("street_snapshot") or {}
    pe_f = street.get("pe")
    if not isinstance(pe_f, dict):
        return
    if not pe_f.get("label"):
        pe_f["label"] = ""
        if SUPPRESS_ON_AMBIGUOUS_VALUATION:
            pe_f["status"] = AMBIGUOUS
            pe_f["notes"] = (pe_f.get("notes") or "") + "; unlabeled P/E suppressed"
            return
    if pe_f.get("value") is not None and not (pe_f.get("label") or "").strip():
        pe_f["status"] = AMBIGUOUS
        pe_f["notes"] = (pe_f.get("notes") or "") + "; P/E not labeled (e.g. Trailing or FY25E) — suppressed"


# ═══════════════════════════════════════════════════════════════════════════════
# Investment view fact pack
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_display(f: Any) -> Any:
    if not isinstance(f, dict):
        return None
    if f.get("status") in ("mismatched", "ambiguous", "suppressed"):
        return None
    return f.get("display_value") if f.get("display_value") is not None else f.get("value")


def build_fact_pack(memo_data: dict, payload: Any) -> dict:
    entity = memo_data.get("entity") or {}
    header = memo_data.get("header") or {}
    key_preview = memo_data.get("key_preview") or []
    recent_execution = memo_data.get("recent_execution") or {}
    appendix_d = memo_data.get("appendix_d") or []

    facts = {
        "company": entity.get("company_name"),
        "ticker": entity.get("ticker"),
        "currency": entity.get("currency"),
        "is_bank": entity.get("is_bank"),
        "industry": entity.get("industry"),
        "expected_report_date": _safe_display(header.get("expected_report_date")),
        "recommendation": _safe_display(header.get("recommendation")),
        "analyst_count": _safe_display(header.get("analyst_count")),
        "current_price": _safe_display(header.get("consensus_page_price") or header.get("quote_price")),
        "target_price": _safe_display(header.get("average_target_price")),
        "upside_pct": _safe_display(header.get("upside_pct")),
        "preview_quarter_short": memo_data.get("preview_short"),
        "key_preview_metrics": [],
        "recent_execution": {},
        "valuation": [],
        "sector_tag": "banks" if entity.get("is_bank") else (entity.get("industry") or "general"),
    }

    for row in key_preview:
        cons = _safe_display(row.get("consensus"))
        prior = _safe_display(row.get("prior_actual"))
        same_ly = _safe_display(row.get("same_q_prior_yr"))
        qoq = _safe_display(row.get("qoq_pct"))
        yoy = _safe_display(row.get("yoy_pct"))
        if cons is not None or prior is not None or same_ly is not None:
            facts["key_preview_metrics"].append({
                "metric": row.get("metric_key"), "consensus": cons,
                "prior_actual": prior, "same_q_prior_yr": same_ly,
                "qoq_pct": qoq, "yoy_pct": yoy,
            })

    for k in ("avg_revenue_surprise_pct", "avg_eps_surprise_pct", "avg_ni_surprise_pct", "consecutive_revenue_beats"):
        v = recent_execution.get(k)
        if isinstance(v, dict):
            v = _safe_display(v)
        if v is not None:
            facts["recent_execution"][k] = v
    facts["recent_execution"]["revenue_surprise_history"] = recent_execution.get("revenue_surprise_history") or []
    facts["recent_execution"]["eps_surprise_history"] = recent_execution.get("eps_surprise_history") or []

    for row in appendix_d:
        period = row.get("period")
        for key in ("pe", "pbr", "yield_pct", "ev_ebit"):
            f = row.get(key)
            if isinstance(f, dict) and f.get("status") == "pass":
                val = f.get("display_value") or f.get("value")
                if val is not None:
                    facts["valuation"].append({"period": period, "metric": key, "value": val, "label": f.get("label")})

    thin = not facts.get("expected_report_date") and not facts.get("recommendation") and not facts.get("key_preview_metrics")
    if thin:
        thin = not (facts.get("recent_execution") or {}).get("revenue_surprise_history")
    facts["_fact_pack_thin"] = thin
    return facts


# ═══════════════════════════════════════════════════════════════════════════════
# Investment view guardrail (uses shared IV quality constants)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_guardrail_re():
    from src.constants.iv_quality import get_guardrail_combined_regex
    return get_guardrail_combined_regex()


def _classify_sentence(s: str) -> str:
    s_clean = (s or "").strip()
    if not s_clean:
        return "empty"
    if _get_guardrail_re().search(s_clean):
        return "unsupported_claim"
    if re.search(r"\d+\.?\d*\s*%|\d+(?:\.\d+)?[xX]|\d+(?:\.\d+)?[bmBM]", s_clean):
        return "numerical_inference"
    return "direct_fact"


def guardrail_paragraphs(p1: str, p2: str) -> tuple[str, str]:
    def _filter_paragraph(para: str) -> str:
        if not para or not isinstance(para, str):
            return ""
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept = []
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if _classify_sentence(s) == "unsupported_claim":
                continue
            kept.append(s)
        return " ".join(kept) if kept else ""
    return _filter_paragraph(p1), _filter_paragraph(p2)


def classify_sentences_for_qa(p1: str, p2: str) -> list[dict]:
    result: list[dict] = []
    for para in (p1 or "", p2 or ""):
        if not para or not isinstance(para, str):
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            classification = _classify_sentence(s)
            status = "removed" if classification == "unsupported_claim" else "kept"
            result.append({
                "sentence": s[:500] + ("…" if len(s) > 500 else ""),
                "classification": classification,
                "status": status,
            })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# QA audit export
# ═══════════════════════════════════════════════════════════════════════════════

def _audit_entry(section: str, field_name: str, f: dict) -> dict:
    return {
        "section": section, "field_name": field_name,
        "displayed_value": f.get("display_value") if f.get("display_value") is not None else f.get("value"),
        "raw_source_value": f.get("raw_value"),
        "source_name": f.get("source_name", ""),
        "source_url": f.get("source_url", ""),
        "scrape_timestamp": str(f.get("scrape_timestamp")) if f.get("scrape_timestamp") else None,
        "formula_used": f.get("formula"),
        "recomputed_value": f.get("recomputed_value"),
        "status": f.get("status", "pass"),
        "notes": f.get("notes", ""),
    }


def export_qa_audit(memo_data: dict, snapshots: Any) -> dict:
    entries: list[dict] = []
    header = memo_data.get("header") or {}
    for k in ("expected_report_date", "recommendation", "analyst_count", "quote_price", "consensus_page_price", "average_target_price", "upside_pct"):
        f = header.get(k)
        if isinstance(f, dict):
            entries.append(_audit_entry("header", k, f))
    street = memo_data.get("street_snapshot") or {}
    for k in ("recommendation", "analyst_count", "target", "upside_pct", "pe"):
        f = street.get(k)
        if isinstance(f, dict):
            entries.append(_audit_entry("street_snapshot", k, f))
    for i, row in enumerate(memo_data.get("key_preview") or []):
        for key in ("consensus", "prior_actual", "same_q_prior_yr", "qoq_pct", "yoy_pct"):
            f = row.get(key)
            if isinstance(f, dict):
                entries.append(_audit_entry("key_preview", f"row_{i}.{key}", f))
    rec = memo_data.get("recent_execution") or {}
    for k in ("avg_revenue_surprise_pct", "avg_eps_surprise_pct", "avg_ni_surprise_pct"):
        f = rec.get(k)
        if isinstance(f, dict):
            entries.append(_audit_entry("recent_execution", k, f))
    app_b = memo_data.get("appendix_b") or {}
    if app_b.get("has_data"):
        for ri, row in enumerate(app_b.get("rows") or []):
            for qi, cell in enumerate(row.get("by_quarter") or []):
                for key in ("released", "forecast", "surprise_pct"):
                    f = cell.get(key)
                    if isinstance(f, dict):
                        entries.append(_audit_entry("appendix_b", f"q{qi}.{row.get('metric_key')}.{key}", f))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entity": memo_data.get("entity"),
        "entries": entries,
        "snapshots_summary": _snapshots_summary(snapshots),
    }


def _snapshots_summary(snapshots: Any) -> dict:
    out = {}
    for name, snap in [
        ("quote", getattr(snapshots, "quote_snapshot", None)),
        ("consensus", getattr(snapshots, "consensus_snapshot", None)),
        ("valuation", getattr(snapshots, "valuation_snapshot", None)),
    ]:
        if snap and getattr(snap, "scrape_timestamp", None):
            out[name] = {"source": getattr(snap, "source_name", ""), "scrape_timestamp": str(getattr(snap, "scrape_timestamp", ""))}
    return out


def write_qa_audit_json(audit: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(audit, fh, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "build_source_snapshots", "SourceSnapshots", "SourceSnapshot",
    "build_memo_data", "MemoData", "QualifiedField",
    "recompute_header_upside", "recompute_key_preview_qoq_yoy",
    "recompute_appendix_b_surprise", "recompute_recent_execution",
    "apply_qa_rules", "apply_valuation_basis",
    "build_fact_pack", "guardrail_paragraphs", "classify_sentences_for_qa",
    "export_qa_audit", "write_qa_audit_json", "run_qa",
    "PASS", "MISSING", "STALE", "MISMATCHED", "AMBIGUOUS", "SUPPRESSED",
]


def run_qa(payload) -> tuple[dict, dict]:
    snapshots = build_source_snapshots(payload)
    memo_data = build_memo_data(payload, snapshots)
    recompute_header_upside(memo_data)
    recompute_key_preview_qoq_yoy(memo_data)
    recompute_appendix_b_surprise(memo_data)
    recompute_recent_execution(memo_data)
    apply_qa_rules(memo_data, snapshots)
    apply_valuation_basis(memo_data)
    qa_audit = export_qa_audit(memo_data, snapshots)
    # Propagate MS section suppression flags for QA doc and debugging
    qa_audit["ms_section_suppressed_due_to_missing_current_data"] = memo_data.get("ms_section_suppressed_due_to_missing_current_data", False)
    qa_audit["ms_section_suppressed_due_to_entity_mismatch"] = memo_data.get("ms_section_suppressed_due_to_entity_mismatch", False)
    qa_audit["ms_section_suppressed_due_to_contamination"] = memo_data.get("ms_section_suppressed_due_to_contamination", False)
    qa_audit["reused_default_payload_detected"] = memo_data.get("reused_default_payload_detected", False)
    qa_audit["payload_entity_match"] = memo_data.get("payload_entity_match", True)
    qa_audit["payload_source_ticker"] = memo_data.get("payload_source_ticker", "")
    memo_data["qa"] = {"audit_available": True}
    return memo_data, qa_audit
