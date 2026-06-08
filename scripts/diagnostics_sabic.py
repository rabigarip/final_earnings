"""
SABIC (2010.SR) vs working company side-by-side diagnostic.
Called from scripts.diagnostics (sabic subcommand). Compares stages 1–7; output: outputs/sabic_vs_working_diagnostic.md and .json
"""
from __future__ import annotations
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import root
from src.storage.db import init_db, load_company
from src.services.entity_resolution import ensure_marketscreener_cached, get_effective_marketscreener_slug
from src.providers.marketscreener import _fetch_page_with_diagnostics

SABIC_TICKER = "2010.SR"
WORKING_TICKERS = ("BABA", "1120.SR")


def _count_dict(d: dict | None) -> int:
    if d is None:
        return 0
    return sum(1 for v in (d or {}).values() if v is not None and v != "" and v != [])


def _count_list(x: list | None) -> int:
    return len(x) if x else 0


def _periods_count(block: dict | None, key: str = "periods") -> int:
    if not block:
        return 0
    if isinstance(block.get(key), list):
        return len(block[key])
    annual = (block.get("annual") or {}) if isinstance(block.get("annual"), dict) else {}
    return _count_list(annual.get("periods")) or _count_list(block.get(key))


def collect_stages_1_to_4(ticker: str) -> dict:
    row = load_company(ticker)
    if not row:
        return {"error": f"No company_master row for {ticker}"}
    ensure_marketscreener_cached(ticker, company=row)
    row = load_company(ticker) or row
    slug = get_effective_marketscreener_slug(row)
    company_url = (row.get("marketscreener_company_url") or "").strip() or f"https://www.marketscreener.com/quote/stock/{slug}/"
    consensus_url = company_url.rstrip("/") + "/consensus/"
    stage1 = {
        "ticker": row.get("ticker"), "isin": row.get("isin"), "company_name": row.get("company_name"),
        "marketscreener_id": row.get("marketscreener_id"), "marketscreener_company_url": row.get("marketscreener_company_url"),
        "marketscreener_status": row.get("marketscreener_status"),
    }
    stage2 = {"slug": slug, "company_url_requested": company_url, "consensus_url_requested": consensus_url}
    out = StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        company_fr = _fetch_page_with_diagnostics(company_url, f"diag_company_{ticker.replace('.', '_')}")
        consensus_fr = _fetch_page_with_diagnostics(consensus_url, f"diag_consensus_{ticker.replace('.', '_')}")
    stage3 = {"company_final_url": getattr(company_fr, "final_url", company_url), "consensus_final_url": getattr(consensus_fr, "final_url", consensus_url)}
    stage4 = {
        "company_classification": getattr(company_fr, "classification", ""), "company_rule_fired": getattr(company_fr, "rule_fired", ""),
        "consensus_classification": getattr(consensus_fr, "classification", ""), "consensus_rule_fired": getattr(consensus_fr, "rule_fired", ""),
    }
    return {"stage1_identifier_resolution": stage1, "stage2_marketscreener_url_resolution": stage2, "stage3_final_redirected_url": stage3, "stage4_homepage_block_classification": stage4}


def collect_stages_5_to_7_from_pipeline_results(results: list, ticker: str) -> dict:
    by_name = {r.step_name: r for r in results}
    r_ms = by_name.get("fetch_marketscreener_pages")
    ms_blocks = r_ms.data if r_ms and getattr(r_ms, "data", None) and isinstance(r_ms.data, dict) else None

    def page_counts(blocks: dict | None) -> dict:
        if not blocks:
            return {}
        cs, ms, ann = blocks.get("consensus_summary") or {}, blocks.get("ms_summary") or {}, blocks.get("ms_annual_forecasts") or {}
        qtr = blocks.get("ms_quarterly_forecasts") or {}
        eps_div = blocks.get("ms_eps_dividend_forecasts") or {}
        inc = blocks.get("ms_income_statement_actuals") or {}
        cal = blocks.get("ms_calendar_events") or {}
        qrt = blocks.get("ms_quarterly_results_table") or {}
        return {
            "consensus_summary_keys": _count_dict(cs), "ms_summary_keys": _count_dict(ms),
            "ms_annual_forecasts_periods": _periods_count(ann, "annual"), "ms_quarterly_forecasts_periods": _periods_count(qtr),
            "ms_eps_dividend_periods": _periods_count(eps_div), "ms_income_statement_keys": _count_dict(inc),
            "ms_calendar_events_keys": _count_dict(cal),
            "ms_quarterly_results_table_rows": _count_list(qrt.get("quarters")) if isinstance(qrt.get("quarters"), list) else _count_dict(qrt),
        }
    stage5 = {"parsed_field_counts_by_page": page_counts(ms_blocks)}
    r_build = by_name.get("build_report_payload")
    payload = r_build.data if r_build and getattr(r_build, "data", None) else None
    stage6 = {}
    if payload is not None:
        stage6 = {
            "has_company": getattr(payload, "company", None) is not None, "has_quote": getattr(payload, "quote", None) is not None,
            "consensus_estimates_count": _count_list(getattr(payload, "consensus_estimates", None)),
            "consensus_summary_keys": _count_dict(getattr(payload, "consensus_summary", None)),
            "memo_computed_keys": _count_dict(getattr(payload, "memo_computed", None)),
            "quarterly_actuals_count": _count_list(getattr(payload, "quarterly_actuals", None)),
            "annual_actuals_count": _count_list(getattr(payload, "annual_actuals", None)),
        }
    stage7 = {}
    if payload is not None:
        memo = getattr(payload, "memo_computed", None) or {}
        appendix = getattr(payload, "appendix_sections", None) or []
        stage7 = {"memo_computed_non_null_keys": _count_dict(memo), "appendix_sections_count": len(appendix), "appendix_sections": list(appendix)}
    return {"stage5_parsed_field_counts_by_page": stage5, "stage6_mapped_payload_field_counts": stage6, "stage7_rendered_memo_section_counts": stage7}


def run_pipeline_and_collect(ticker: str) -> list:
    from src.pipeline import run_preview
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return run_preview(ticker, skip_llm=True)


def build_side_by_side(sabic: dict, working: dict, working_ticker: str) -> tuple[str, dict]:
    def tbl(a: dict | None, b: dict | None, title: str) -> str:
        a, b = a or {}, b or {}
        keys = sorted(set((a or {}).keys()) | set((b or {}).keys()))
        lines = [f"### {title}\n", "| Field | SABIC (2010.SR) | " + working_ticker + " |", "|-------|------------------|--------|"]
        for k in keys:
            va = a.get(k) if isinstance(a.get(k), (str, int, float, bool)) else json.dumps(a.get(k))
            vb = b.get(k) if isinstance(b.get(k), (str, int, float, bool)) else json.dumps(b.get(k))
            diverge = " ← **divergence**" if va != vb else ""
            lines.append(f"| {k} | {va} | {vb} |{diverge}")
        return "\n".join(lines)
    s1_s = sabic.get("stage1_identifier_resolution") or {}
    s1_w = working.get("stage1_identifier_resolution") or {}
    s2_s = sabic.get("stage2_marketscreener_url_resolution") or {}
    s2_w = working.get("stage2_marketscreener_url_resolution") or {}
    s3_s = sabic.get("stage3_final_redirected_url") or {}
    s3_w = working.get("stage3_final_redirected_url") or {}
    s4_s = sabic.get("stage4_homepage_block_classification") or {}
    s4_w = working.get("stage4_homepage_block_classification") or {}
    s5_s = (sabic.get("stage5_parsed_field_counts_by_page") or {}).get("parsed_field_counts_by_page") or {}
    s5_w = (working.get("stage5_parsed_field_counts_by_page") or {}).get("parsed_field_counts_by_page") or {}
    s6_s = sabic.get("stage6_mapped_payload_field_counts") or {}
    s6_w = working.get("stage6_mapped_payload_field_counts") or {}
    s7_s = sabic.get("stage7_rendered_memo_section_counts") or {}
    s7_w = working.get("stage7_rendered_memo_section_counts") or {}
    expected_sabic_slug = "SAUDI-BASIC-INDUSTRIES-CORP-6203"
    sabic_slug = (s1_s.get("marketscreener_id") or "").strip()
    wrong_entity_stage1 = sabic_slug != expected_sabic_slug and expected_sabic_slug not in (sabic_slug or "")
    first_divergence = "stage1_identifier_resolution" if wrong_entity_stage1 else None
    if not first_divergence and s4_s.get("consensus_classification") != s4_w.get("consensus_classification"):
        first_divergence = "stage4_homepage_block_classification"
    elif not first_divergence and s5_s != s5_w:
        first_divergence = "stage5_parsed_field_counts_by_page"
    elif not first_divergence and s6_s != s6_w:
        first_divergence = "stage6_mapped_payload_field_counts"
    elif not first_divergence and s7_s != s7_w:
        first_divergence = "stage7_rendered_memo_section_counts"
    j = {
        "sabic_ticker": SABIC_TICKER, "working_ticker": working_ticker, "first_divergence_stage": first_divergence,
        "sabic_resolved_to_wrong_entity": wrong_entity_stage1, "expected_sabic_slug": expected_sabic_slug, "actual_sabic_slug": sabic_slug,
        "stage1_identifier_resolution": {"sabic": sabic.get("stage1_identifier_resolution"), "working": working.get("stage1_identifier_resolution")},
        "stage2_marketscreener_url_resolution": {"sabic": sabic.get("stage2_marketscreener_url_resolution"), "working": working.get("stage2_marketscreener_url_resolution")},
        "stage3_final_redirected_url": {"sabic": sabic.get("stage3_final_redirected_url"), "working": working.get("stage3_final_redirected_url")},
        "stage4_homepage_block_classification": {"sabic": sabic.get("stage4_homepage_block_classification"), "working": working.get("stage4_homepage_block_classification")},
        "stage5_parsed_field_counts_by_page": {"sabic": (sabic.get("stage5_parsed_field_counts_by_page") or {}).get("parsed_field_counts_by_page"), "working": (working.get("stage5_parsed_field_counts_by_page") or {}).get("parsed_field_counts_by_page")},
        "stage6_mapped_payload_field_counts": {"sabic": sabic.get("stage6_mapped_payload_field_counts"), "working": working.get("stage6_mapped_payload_field_counts")},
        "stage7_rendered_memo_section_counts": {"sabic": sabic.get("stage7_rendered_memo_section_counts"), "working": working.get("stage7_rendered_memo_section_counts")},
    }
    md = f"# SABIC (2010.SR) vs {working_ticker} — side-by-side diagnostic\n\nIdentify the **first stage** where SABIC diverges from the working company.\n\n"
    md += tbl(s1_s, s1_w, "1. Identifier resolution") + "\n\n" + tbl(s2_s, s2_w, "2. MarketScreener URL resolution") + "\n\n"
    md += tbl(s3_s, s3_w, "3. Final redirected URL") + "\n\n" + tbl(s4_s, s4_w, "4. Homepage/block classification") + "\n\n"
    md += tbl(s5_s, s5_w, "5. Parsed field counts by page") + "\n\n" + tbl(s6_s, s6_w, "6. Mapped payload field counts") + "\n\n" + tbl(s7_s, s7_w, "7. Rendered memo section counts")
    if wrong_entity_stage1:
        md += f"\n\n---\n**First divergence: Stage 1.** SABIC resolved to `{sabic_slug}` instead of `{expected_sabic_slug}`.\n"
    elif s4_s.get("consensus_classification") != s4_w.get("consensus_classification"):
        md += "\n\n---\n**First divergence:** Stage 4 — consensus page classification.\n"
    elif s5_s != s5_w:
        md += "\n\n---\n**First divergence:** Stage 5 — parsed field counts.\n"
    elif s6_s != s6_w:
        md += "\n\n---\n**First divergence:** Stage 6 — payload field counts.\n"
    elif s7_s != s7_w:
        md += "\n\n---\n**First divergence:** Stage 7 — memo sections.\n"
    else:
        md += "\n\n---\n**First divergence:** Stage 1–3 or none.\n"
    return md, j


def run(working_ticker: str | None = None) -> int:
    """Run SABIC vs working diagnostic. working_ticker: optional; default first of BABA, 1120.SR."""
    if not working_ticker:
        for t in WORKING_TICKERS:
            if load_company(t):
                working_ticker = t
                break
    if not working_ticker or not load_company(working_ticker):
        print("No working ticker found. Use --working BABA or 1120.SR")
        return 1
    init_db()
    out_dir = root() / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    print("Collecting stages 1–4 for SABIC (2010.SR)...")
    sabic_1_4 = collect_stages_1_to_4(SABIC_TICKER)
    if sabic_1_4.get("error"):
        print("SABIC:", sabic_1_4["error"])
        return 1
    print("Collecting stages 1–4 for", working_ticker, "...")
    working_1_4 = collect_stages_1_to_4(working_ticker)
    if working_1_4.get("error"):
        print("Working:", working_1_4["error"])
        return 1
    print("Running full pipeline for SABIC...")
    sabic_5_7 = collect_stages_5_to_7_from_pipeline_results(run_pipeline_and_collect(SABIC_TICKER), SABIC_TICKER)
    print("Running full pipeline for", working_ticker, "...")
    working_5_7 = collect_stages_5_to_7_from_pipeline_results(run_pipeline_and_collect(working_ticker), working_ticker)
    md, j = build_side_by_side({**sabic_1_4, **sabic_5_7}, {**working_1_4, **working_5_7}, working_ticker)
    json_path = out_dir / "sabic_vs_working_diagnostic.json"
    md_path = out_dir / "sabic_vs_working_diagnostic.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(j, f, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Wrote {json_path}\nWrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(run(None))
