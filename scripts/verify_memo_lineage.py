#!/usr/bin/env python3
"""
Verify memo lineage and payload isolation after regeneration.

Checks:
- payload_source_ticker matches the run ticker
- payload_entity_match is true whenever MarketScreener data is rendered
- No cross-company MarketScreener values (company name, ms_lineage.source_company_name,
  or source_url pointing to another company) appear in the payload

Note: If BYD (002594.SZ) fails with "source_url points to wrong entity (slug contains AMD)",
that indicates an entity-resolution or company_master data issue (wrong MarketScreener slug
for BYD), not a payload-isolation bug. Fix by correcting marketscreener_id/slug for that ticker.

Usage:
  python scripts/verify_memo_lineage.py [--outputs-dir OUTPUTS]
  If no dir given, uses project outputs/ and scans *_report_payload.json.
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

# Expected company name fragments per ticker (for cross-company check)
TICKER_EXPECTED = {
    "002594.SZ": ["BYD"],
    "INFY.NS": ["Infosys"],
    "FSR.JO": ["FirstRand"],
    "2010.SR": ["SABIC", "Saudi Basic"],
}

# Names that must NOT appear when run is for a different company
CROSS_NAMES = ["Infosys", "FirstRand", "SABIC", "Saudi Basic", "BYD", "AMD", "Advanced Micro"]


def _latest_report_payloads(outputs_dir: Path) -> dict[str, Path]:
    """Return ticker -> path of latest report_payload.json (by mtime)."""
    pattern = re.compile(r"^([A-Z0-9]+)_[A-Z0-9]+_[a-f0-9]+_report_payload\.json$", re.I)
    by_ticker: dict[str, list[tuple[float, Path]]] = {}
    for p in outputs_dir.glob("*_report_payload.json"):
        m = pattern.match(p.name.replace(".", "_"))
        if not m:
            # Allow 002594_SZ_xxx format
            parts = p.stem.split("_")
            if len(parts) >= 3 and parts[-1] == "report" and "payload" in p.stem:
                continue
            ticker_candidate = p.name.split("_")[0] + "." + (p.name.split("_")[1] if "_" in p.name else "")
            # Reverse: 002594_SZ_xxx -> 002594.SZ
            if "_" in p.name:
                a, b = p.name.split("_", 2)[:2]
                ticker_candidate = f"{a}.{b}"
            try:
                mtime = p.stat().st_mtime
                by_ticker.setdefault(ticker_candidate, []).append((mtime, p))
            except OSError:
                pass
            continue
        ticker_raw = m.group(1)
        ticker = ticker_raw.replace("_", ".") if "_" in ticker_raw else ticker_raw
        try:
            mtime = p.stat().st_mtime
            by_ticker.setdefault(ticker, []).append((mtime, p))
        except OSError:
            pass
    out = {}
    for t, pairs in by_ticker.items():
        pairs.sort(key=lambda x: -x[0])
        out[t] = pairs[0][1]
    return out


def _normalize_ticker_from_path(path: Path) -> str:
    """e.g. 002594_SZ_2d4d02c2_report_payload.json -> 002594.SZ"""
    stem = path.stem  # 002594_SZ_2d4d02c2_report_payload
    parts = stem.split("_")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isalpha():
        return f"{parts[0]}.{parts[1]}"
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return parts[0] if parts else ""


def verify_one(path: Path, ticker: str) -> list[str]:
    errors = []
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    pf = doc.get("payload_fields") or doc
    payload_ticker = (pf.get("payload_source_ticker") or "").strip()
    company_ticker = (pf.get("company") or {}).get("ticker") or ""
    if payload_ticker != ticker:
        errors.append(f"payload_source_ticker '{payload_ticker}' != run ticker '{ticker}'")
    if company_ticker != ticker:
        errors.append(f"company.ticker '{company_ticker}' != run ticker '{ticker}'")

    has_ms = any(
        pf.get(k) for k in (
            "consensus_summary", "ms_summary", "ms_annual_forecasts", "ms_valuation_multiples",
            "ms_calendar_events", "ms_quarterly_results_table"
        )
    )
    if has_ms and not pf.get("payload_entity_match", True):
        errors.append("MarketScreener data present but payload_entity_match is not true")

    # Cross-company: ms_lineage.source_company_name and source_url should not reference other companies
    ms_lineage = pf.get("ms_lineage") or {}
    source_name = (ms_lineage.get("source_company_name") or "").strip()
    source_url = (ms_lineage.get("source_url") or "").strip()
    company_name = (pf.get("company") or {}).get("company_name") or (pf.get("company") or {}).get("company_name_long") or ""
    expected_fragments = TICKER_EXPECTED.get(ticker, [])
    for name in CROSS_NAMES:
        if not expected_fragments or name not in expected_fragments:
            if name.lower() in (company_name or "").lower():
                errors.append(f"Payload company name contains other company '{name}'")
            if name.lower() in (source_name or "").lower():
                errors.append(f"ms_lineage.source_company_name contains other company '{name}'")
    if source_url and "/quote/stock/" in source_url:
        # Slug in URL should not be another company (e.g. AMD for BYD)
        slug = source_url.rstrip("/").split("/")[-1] or ""
        for name in ["AMD-ADVANCED", "INFOSYS", "FIRSTRAND", "SAUDI-BASIC"]:
            if name in slug.upper() and not any(
                n.lower() in (expected_fragments or [""])[0].lower() for n in [name.split("-")[0]]
            ):
                if ticker == "002594.SZ" and "AMD" in slug.upper():
                    errors.append(f"source_url points to wrong entity (slug contains AMD): {source_url[:80]}...")
                elif ticker != "2010.SR" and "SAUDI-BASIC" in slug.upper():
                    errors.append(f"source_url points to SABIC slug for non-SABIC ticker {ticker}")

    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify memo lineage and payload isolation")
    ap.add_argument("--outputs-dir", type=Path, default=None, help="outputs directory (default: project outputs/)")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    outputs_dir = args.outputs_dir or root / "outputs"
    if not outputs_dir.exists():
        print(f"Outputs dir not found: {outputs_dir}", file=sys.stderr)
        return 1

    # Find latest report_payload per ticker by scanning filenames
    by_ticker: dict[str, Path] = {}
    for p in outputs_dir.glob("*_report_payload.json"):
        if "_report_payload" not in p.name:
            continue
        # 002594_SZ_2d4d02c2_report_payload.json
        parts = p.name.replace(".", "_").split("_")
        if len(parts) >= 2:
            t = f"{parts[0]}.{parts[1]}" if parts[1].isalpha() else parts[0]
            try:
                mtime = p.stat().st_mtime
                if t not in by_ticker or by_ticker[t].stat().st_mtime < mtime:
                    by_ticker[t] = p
            except OSError:
                pass

    if not by_ticker:
        print("No *_report_payload.json files found.", file=sys.stderr)
        return 0

    all_ok = True
    for ticker in sorted(by_ticker.keys()):
        path = by_ticker[ticker]
        # Normalize ticker from path
        ticker_norm = _normalize_ticker_from_path(path)
        if not ticker_norm:
            ticker_norm = ticker
        errs = verify_one(path, ticker_norm)
        if errs:
            all_ok = False
            print(f"{ticker_norm} ({path.name}):")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"{ticker_norm}: OK (payload_source_ticker match, entity_match when MS present, no cross-company)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
