"""Earnings preview: PPTX output (Jabal 3-slide deck) + sector helpers
(imported by qa_engine).

Renders the Stage-2 Jabal 3-slide deck (`_write_jabal_preview`) that
reads from `canonical_store`. The legacy 12-slide portrait renderer was
removed; Jabal is the only user-visible product.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path

from src.config import report_output_dir
from src.models.report_payload import ReportPayload
from src.models.step_result import Status, StepResult, StepTimer

STEP = "generate_report"


def _omani_next_earnings(period_label: str, current: str) -> str:
    """Omani MSX disclosure rule: listed companies have up to ~15 days after
    quarter-end to release (unaudited) results. Yahoo/Investing estimated
    dates for .OM names are unreliable (OQEP showed a Q2 print as "17 Aug",
    weeks past the deadline). Return the regulatory window ("by <date>")
    unless `current` is already a plausible date inside that window (i.e. a
    real announcement). Falls back to `current` if the quarter can't be parsed."""
    import re
    from datetime import date, datetime, timedelta
    m = re.search(r"Q([1-4])\s+(\d{4})", period_label or "")
    if not m:
        return current
    q, y = int(m.group(1)), int(m.group(2))
    mm, dd = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
    qend = date(y, mm, dd)
    deadline = qend + timedelta(days=15)
    cur = (current or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%m/%d/%Y", "%b %d, %Y"):
        try:
            d = datetime.strptime(cur, fmt).date()
        except ValueError:
            continue
        # A date already in the plausible window is likely the real release.
        if qend - timedelta(days=3) <= d <= deadline + timedelta(days=7):
            return current
        break
    return "by " + deadline.strftime("%d %b %Y")


def _write_jabal_preview(payload: ReportPayload, out_path: Path,
                          memo_data: dict | None) -> None:
    """Render the 3-slide Jabal deck via the new render_jabal_* modules,
    reading data from canonical_store.

    Bootstrap step: if canonical_store has no rows for this ticker (e.g.
    fresh DB or first run on a new name), we kick off a daily-cadence
    refresh against the standard provider set so the slides aren't empty.
    """
    from pptx import Presentation
    from pptx.util import Inches as _In
    from src.services.canonical_store import get_all_fields
    from src.services.jabal_design_tokens import PAGE_W_IN, PAGE_H_IN
    from src.services.render_jabal_snapshot import (
        render_snapshot_slide, build_snapshot_data,
    )
    from src.services.render_jabal_thesis import (
        render_thesis_slide, build_thesis_data,
    )
    from src.services.render_jabal_valuation import (
        render_valuation_slide, build_valuation_data,
    )

    ticker = payload.company.ticker
    # ── On-demand freshness (the core fix) ──────────────────────────────
    # The Jabal renderer reads exclusively from canonical_store. The prior
    # architecture only bootstrapped the store on a COLD cache, so a shipped
    # DB with a stale/empty consensus cell (e.g. an Investing rating_split
    # that failed at refresh time) persisted forever and the deck shipped
    # half-filled — exactly the "wrong numbers / half-filled" defect.
    #
    # We now ALWAYS refresh the requested ticker on every render. Yahoo
    # Finance is the always-available backbone: NOT Cloudflare-blocked (works
    # from Render's datacenter IPs) and supplies price, market cap, 52-week,
    # performance, trailing+forward P/E, analyst rating split, target price,
    # forward EPS/revenue estimates, and earnings history — everything the
    # Cloudflare-blocked scrapers were relied on for. It runs across all three
    # cadences (~3s) so consensus + estimates + charts always fill. Investing
    # / MarketScreener then run best-effort as enrichment and as the fallback
    # for Yahoo-blind names (Oman MSX, some Gulf); they sit ABOVE Yahoo on the
    # trust ladder, so when they DO return a value they win.
    #
    # Skippable via REFRESH_ON_RENDER=0 for offline fixture tests.
    import subprocess, sys, os
    cv_existing = get_all_fields(ticker)
    if os.environ.get("REFRESH_ON_RENDER", "1") != "0":
        # ONE subprocess, every cadence, all three providers. Previously this
        # spawned FIVE `daily_refresh` processes (3 Yahoo cadences + 2
        # Investing/MS), each paying ~5s of Python startup+import — ~25s of pure
        # overhead that pushed on-demand generation to ~46s and made the browser
        # fetch time out ("Failed to fetch"). The reconciler's trust ladder
        # (Investing ▸ Yahoo ▸ MarketScreener) picks the winner per field
        # regardless of fetch order, so a single combined pass is equivalent and
        # far faster. Best-effort; failures never block a deck.
        try:
            subprocess.run(
                [sys.executable, "-m", "scripts.daily_refresh",
                 "--cadence=all", f"--tickers={ticker}",
                 "--only=yahoo,investing,marketscreener"],
                timeout=110, check=False,
            )
        except Exception:
            pass

    # Stage 2 period defaulting — caller can override via memo_data. When
    # memo_data doesn't carry an explicit period_label / report_date, derive
    # them from payload.memo_computed (preview_quarter_short, next_quarter_label)
    # and payload.yahoo_earnings_date so the snapshot never reads "Earnings
    # Preview · TBA" for a ticker we already have calendar data on.
    mc = getattr(payload, "memo_computed", {}) or {}
    period_label = (memo_data or {}).get("period_label")

    # Investing.com's earnings page has the upcoming-earnings row regardless
    # of whether MS's calendar published it. When MS gives us nothing (the
    # common BKMB-class case), Investing's "next" row is the source of truth.
    # Returns (next_qtr, next_yr) or (None, None).
    def _next_qtr_from_investing(t: str) -> tuple[Optional[int], Optional[int]]:
        try:
            from src.providers.probe_investing import _fetch_earnings_page, _slug
            from datetime import datetime as _dt, timezone as _tz
            slug = _slug(t)
            if not slug:
                return None, None
            state = _fetch_earnings_page(slug)
            if not state:
                return None, None
            es = state.get("earningsStore") or {}
            rows = es.get("earnings") if isinstance(es, dict) else None
            if not isinstance(rows, list):
                return None, None
            today = _dt.now(_tz.utc).date()
            for r in rows:
                if not isinstance(r, dict):
                    continue
                d = (r.get("date") or "")[:10]
                try:
                    rd = _dt.fromisoformat(d).date()
                except Exception:
                    continue
                # The "next" earnings row is the most recent one whose date is
                # in the future. Investing lists them newest-first; the first
                # future row is the next event.
                if rd >= today and r.get("epsActual") is None:
                    rm = r.get("reportMonth") or 0
                    ry = r.get("reportYear")
                    if isinstance(rm, int) and isinstance(ry, int) and rm:
                        return ((rm - 1) // 3 + 1), ry
            return None, None
        except Exception:
            return None, None

    if not period_label:
        # 1. Investing's next-earnings row (most reliable)
        inv_q, inv_y = _next_qtr_from_investing(ticker)
        if inv_q and inv_y:
            period_label = f"Q{inv_q} {inv_y} Earnings Preview"
        else:
            # 2. memo_computed.next_quarter_label (MS-derived)
            nql = mc.get("next_quarter_label") or ""
            import re as _rqp
            m = _rqp.search(r"(\d{4})\s*Q(\d)|Q(\d)\s*(\d{4})", nql, _rqp.I)
            if m:
                yr = m.group(1) or m.group(4)
                qn = m.group(2) or m.group(3)
                period_label = f"Q{qn} {yr} Earnings Preview"
            else:
                period_label = mc.get("preview_quarter_label") or "Earnings Preview"

    report_date = (memo_data or {}).get("report_date")
    if not report_date:
        ms_cal = getattr(payload, "ms_calendar_events", None) or {}
        # Investing's earnings page is the most reliable source for the
        # next-earnings date — MS calendar often returns nothing for
        # thinly-covered names (BKMB, OQEP, etc.).
        inv_next_date = None
        try:
            from src.providers.probe_investing import _fetch_earnings_page, _slug
            from datetime import datetime as _dt, timezone as _tz
            slug = _slug(ticker)
            if slug:
                state = _fetch_earnings_page(slug)
                if state:
                    es = state.get("earningsStore") or {}
                    rows = es.get("earnings") if isinstance(es, dict) else []
                    today = _dt.now(_tz.utc).date()
                    for r in rows or []:
                        if not isinstance(r, dict):
                            continue
                        d = (r.get("date") or "")[:10]
                        try:
                            rd = _dt.fromisoformat(d).date()
                        except Exception:
                            continue
                        if rd >= today and r.get("epsActual") is None:
                            inv_next_date = d
                            break
        except Exception:
            inv_next_date = None
        for path in (
            ms_cal.get("next_expected_earnings_date") if isinstance(ms_cal, dict) else None,
            ms_cal.get("next_expected_earnings_label") if isinstance(ms_cal, dict) else None,
            inv_next_date,
            getattr(payload, "yahoo_earnings_date", None),
            mc.get("next_earnings_date"),
            mc.get("yahoo_earnings_date"),
        ):
            if path:
                report_date = str(path)
                break
        report_date = report_date or "TBA"

    # Omani names: show the regulatory disclosure window instead of an
    # unreliable hard estimate (results due within ~15 days of quarter-end).
    if ticker.upper().endswith(".OM"):
        report_date = _omani_next_earnings(period_label, report_date)

    # Load curated peer tickers (from company_master.peer_group) and
    # enrich them with yfinance — drives slide 3's peer table.
    peer_rows: list[dict] = []
    try:
        from src.storage.db import load_company
        from src.services.fetch_peers import fetch_peer_rows
        from src.services.ticker_registry import registry_peer_set
        company_row = load_company(ticker) or {}
        peer_tickers = company_row.get("peer_group") or []
        # Fall back to the registry's default peer set when the per-ticker
        # company row doesn't carry an explicit list. Registry peers are
        # auto-generated (top-7 by USD market cap within same template
        # family + same region) so they're a reasonable default for any
        # ticker in the 500-name universe.
        if not peer_tickers:
            peer_tickers = registry_peer_set(ticker)
            if peer_tickers:
                import logging
                logging.getLogger(__name__).info(
                    "Using registry peer set for %s: %s", ticker, peer_tickers)
        if peer_tickers:
            peer_rows = fetch_peer_rows(peer_tickers)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("peer enrichment failed for %s: %s", ticker, exc)

    # Investing.com 52w history — drives slide 3's price chart and the
    # snapshot performance row when yfinance has no data. Cached on disk
    # per ticker so subsequent runs cost a single HTTP roundtrip.
    historical_override: dict | None = None
    try:
        from src.providers.probe_investing import fetch_historical_prices
        historical_override = fetch_historical_prices(ticker)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("historical prices fetch failed for %s: %s", ticker, exc)

    snap      = build_snapshot_data(ticker, period_label=period_label,
                                       report_date=report_date,
                                       ms_price_performance=getattr(payload, "ms_price_performance", None),
                                       historical_override=historical_override,
                                       live_quote_record=(memo_data or {}).get("live_quote_record"))
    # Slide-2 heading must agree with slide-1 period_label. Strip the
    # trailing "Earnings Preview" / "Earnings Update" suffix and replace
    # with "Earnings Expectations" so the cover and the estimates section
    # frame the same quarter.
    # Pull the quarter token out of period_label in EITHER format —
    # "Q2 2026 Earnings Preview" or "Earnings Preview — 1Q26" — so the
    # slide-2 estimate-column header always carries the quarter (the missing
    # quarter is what made 2010.SR's header read "EARNINGS EXPECTATIONS").
    period_heading = "Earnings Expectations"
    import re as _re_ph2
    _qm = _re_ph2.search(r"\bQ[1-4]\s*'?\d{2,4}\b|\b[1-4]Q\s*'?\d{2,4}\b",
                          period_label or "")
    if _qm:
        period_heading = f"{_qm.group(0)} Earnings Expectations"
    elif period_label and " Earnings " in period_label:
        _q_prefix = period_label.split(" Earnings ", 1)[0]   # "Q2 2026"
        period_heading = f"{_q_prefix} Earnings Expectations"
    is_bank = bool(getattr(payload.company, "is_bank", False))
    # Pass the memo_computed shape the estimates table reads, but carry the
    # pipeline's draft_pptx_sections output (memo_data["pptx_sections"]) so the
    # thesis slide reuses that single grounded LLM call instead of firing a
    # second one. Copy so we never mutate payload.memo_computed.
    _memo_for_thesis = dict(getattr(payload, "memo_computed", None) or {})
    if isinstance(memo_data, dict) and memo_data.get("pptx_sections"):
        _memo_for_thesis["pptx_sections"] = memo_data["pptx_sections"]
    thesis    = build_thesis_data(ticker,
                                     quarterly=getattr(payload, "quarterly_actuals", None) or [],
                                     is_bank=is_bank,
                                     ms_quarterly_forecasts=getattr(payload, "ms_quarterly_forecasts", None),
                                     ms_annual_forecasts=getattr(payload, "ms_annual_forecasts", None),
                                     ms_eps_dividend_forecasts=getattr(payload, "ms_eps_dividend_forecasts", None),
                                     bloomberg_bundle=getattr(payload, "bloomberg_bundle", None),
                                     period_heading=period_heading,
                                     memo_data=_memo_for_thesis)
    valuation = build_valuation_data(ticker, peers_override=peer_rows or None,
                                       historical_override=historical_override,
                                       ms_calendar_events=getattr(payload, "ms_calendar_events", None))

    prs = Presentation()
    prs.slide_width  = _In(PAGE_W_IN)
    prs.slide_height = _In(PAGE_H_IN)
    render_snapshot_slide(prs, snap)
    render_thesis_slide(prs, thesis)
    render_valuation_slide(prs, valuation)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))

    # Emit the provenance sidecar (.xlsx) alongside the deck so the
    # analyst can audit every number's source / year / fetch time. Best
    # effort — a sidecar failure must not block the deck.
    try:
        from src.services.render_provenance import write_provenance_xlsx
        provenance_path = out_path.with_suffix(".provenance.xlsx")
        write_provenance_xlsx(ticker, provenance_path,
                                memo_data=memo_data,
                                payload=payload,
                                peer_rows=peer_rows or None,
                                historical_override=historical_override)
    except Exception as _exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Provenance sidecar failed for %s: %s", ticker, _exc
        )

def _company_attr(company, key: str, default: str = ""):
    """Read sector/industry/etc. from company whether it's a model instance or a dict (e.g. after serialization)."""
    if company is None:
        return default
    if isinstance(company, dict):
        return (company.get(key) or default) if isinstance(default, str) else company.get(key, default)
    return (getattr(company, key, None) or default) if isinstance(default, str) else getattr(company, key, default)


def _sector_operating_kpis_and_what_matters(company) -> tuple[list[str], list[str], str]:
    """
    Return (operating_metrics_kpis[4], what_matters_bullets[5], fallback_para2_snippet).
    fallback_para2 is publishable analyst prose only (no "Focus on...", "Do not use..." instructions).
    """
    sector = (_company_attr(company, "sector", "") or "").strip().lower()
    industry = (_company_attr(company, "industry", "") or "").strip().lower()
    ind = industry or sector
    is_bank = bool(_company_attr(company, "is_bank", False))

    if is_bank:
        kpis = ["Loans", "Deposits", "NIM", "Cost of Risk"]
        matters = ["Loan / financing growth", "NIM / margin", "Asset quality", "Funding mix", "Capital return"]
        p2 = "For banks, the story usually turns on NIM, loan growth, and asset quality. Earnings quality—recurring versus one-offs—and any weakness versus consensus or deterioration in asset quality are key for the multiple."
        return kpis, matters, p2

    if "oil" in ind or "gas" in ind or "energy" in sector or "exploration" in ind or "petroleum" in ind:
        kpis = ["Production volumes", "Realized oil/gas prices", "Lifting costs", "Capex / project ramp-up"]
        matters = ["Production volumes", "Realized oil/gas prices", "Lifting costs", "Reserve replacement / field startup", "Capex and project ramp-up"]
        p2 = "For oil and gas, the narrative typically turns on production volumes, realized prices, and lifting costs; reserve replacement and field startup impact also matter, and capex and project ramp-up often drive the story."
        return kpis, matters, p2

    if "telecom" in ind or "communication" in sector or "communication" in ind:
        kpis = ["Subscribers", "ARPU", "Churn", "Capex intensity"]
        matters = ["Subscriber additions", "ARPU trend", "Churn", "Capex intensity", "India wireless competition" if "india" in ((_company_attr(company, "country", "") or "").lower()) else "Wireless competition", "Enterprise / data centre contribution"]
        p2 = "For telecoms and communication equipment, subscriber trends, ARPU, churn, and capex intensity are central where relevant; enterprise and product-cycle dynamics often drive the story."
        return kpis, matters[:5], p2

    if "technology" in sector or "software" in ind or "semiconductor" in ind or "equipment" in ind:
        kpis = ["Revenue growth", "Margin", "Guidance", "Key product metrics"]
        matters = ["Revenue mix and growth", "Margin and profitability", "Guidance", "Product cycles", "Competitive dynamics"]
        p2 = "For technology and communication equipment names, the narrative typically turns on revenue mix, margins, and guidance; product cycles and competitive dynamics often drive the stock."
        return kpis, matters[:5], p2

    if "industrial" in sector or "capital good" in ind or "aerospace" in ind or "machinery" in ind:
        kpis = ["Orders / backlog", "Utilization", "Pricing", "Guidance"]
        matters = ["Demand and orders", "Backlog / utilization", "Margin and pricing", "Guidance", "Key metrics"]
        p2 = "For industrials, demand, orders, backlog, and utilization drive the story; margin and pricing matter, and guidance and key metrics often move the stock."
        return kpis, matters, p2

    if "internet" in ind or "e-commerce" in ind or "retail" in ind:
        kpis = ["GMV", "Cloud revenue growth", "International commerce", "Customer management revenue"]
        matters = ["GMV and engagement", "Margin and pricing", "Guidance", "Key metrics"]
        p2 = "Sector operating metrics and headline results versus consensus are key; guidance and main metrics typically drive the stock."
        return kpis, matters[:5], p2

    if "mining" in ind or "metals" in ind:
        kpis = ["Production / throughput", "Commodity prices", "Costs", "Guidance"]
        matters = ["Production and sales volume", "Commodity price realizations", "Cost and margin", "Guidance", "Key metrics"]
        p2 = "For metals and mining, the narrative typically turns on production, realized commodity prices, and costs; guidance and key operating metrics often drive the stock."
        return kpis, matters[:5], p2

    if "chem" in ind or "material" in ind or "material" in sector:
        kpis = ["Volume", "Realized price", "Utilization", "Feedstock spread"]
        matters = ["Volume and realized price", "Utilization", "Feedstock spread", "Guidance", "Key metrics"]
        p2 = "Volume, realized price, utilization, and feedstock spread are the main levers; guidance and key metrics drive the story."
        return kpis, matters[:5], p2

    if "real estate" in sector or "real estate" in ind or "reit" in ind or "property" in ind:
        kpis = ["Occupancy", "Rental rates", "Recurring income mix", "Development pipeline"]
        matters = ["Occupancy trend", "Rental and sales prices", "Recurring vs development income", "Development pipeline and pre-sales", "Leverage and cost of debt"]
        p2 = "For real estate, the narrative typically turns on occupancy, rental rates, and the mix of recurring versus development income; pipeline execution, pre-sales, and leverage drive the stock."
        return kpis, matters[:5], p2

    if "insurance" in sector or "insurance" in ind:
        kpis = ["Gross written premium", "Combined ratio", "Investment yield", "Solvency"]
        matters = ["Premium growth", "Underwriting margin and combined ratio", "Investment income and asset mix", "Claims experience", "Capital and solvency"]
        p2 = "For insurers, the story centers on premium growth, the combined ratio, and investment yield; claims experience and solvency drive the multiple."
        return kpis, matters[:5], p2

    if "financial" in sector or "capital markets" in ind or "asset management" in ind or "investment" in ind:
        kpis = ["AUM / assets", "Fee rate", "Cost/income", "Capital return"]
        matters = ["Asset growth", "Fee rate and revenue mix", "Cost efficiency", "Asset quality / risk-weighted assets", "Capital return"]
        p2 = "For diversified financials, the narrative turns on asset growth, fee rate, and cost/income; risk posture and capital return drive the valuation."
        return kpis, matters[:5], p2

    if "utilities" in sector or "utility" in ind or "water" in ind or "electric" in ind or ("gas" in ind and "oil" not in ind):
        kpis = ["Regulated asset base", "Tariff / allowed return", "Volumes", "Capex"]
        matters = ["Tariff and allowed return", "Regulated asset base growth", "Demand / volumes", "Capex delivery", "Guidance"]
        p2 = "For utilities, the story typically turns on regulated asset base growth, tariffs, and volumes; capex execution and any guidance changes drive the stock."
        return kpis, matters[:5], p2

    if "consumer staples" in sector or "food" in ind or "beverage" in ind or "household" in ind or "personal product" in ind:
        kpis = ["Volume", "Price/mix", "Gross margin", "Guidance"]
        matters = ["Volume and price/mix", "Gross margin and input costs", "Market share", "Marketing / promotional intensity", "Guidance"]
        p2 = "For consumer staples, the narrative is driven by volume and price/mix; input cost and promotional intensity set margins, and guidance anchors the multiple."
        return kpis, matters[:5], p2

    if "consumer discretionary" in sector or "apparel" in ind or "auto" in ind or "restaurant" in ind or "lodging" in ind or "specialty retail" in ind:
        kpis = ["Same-store sales / volumes", "Price/mix", "Margin", "Guidance"]
        matters = ["Same-store sales and unit growth", "Price/mix and discounting", "Gross and operating margin", "Inventory position", "Guidance"]
        p2 = "For consumer discretionary, same-store sales, price/mix, and margin are the core drivers; inventory discipline and guidance shape the stock."
        return kpis, matters[:5], p2

    if "healthcare" in sector or "pharma" in ind or "biotech" in ind or "medical" in ind or "drug" in ind:
        kpis = ["Revenue by franchise", "Gross margin", "R&D progress", "Guidance"]
        matters = ["Franchise performance", "Pricing and volume", "Gross and operating margin", "Pipeline / R&D milestones", "Guidance"]
        p2 = "For healthcare names, franchise revenue trends, margins, and pipeline milestones dominate; pricing pressure and guidance anchor sentiment."
        return kpis, matters[:5], p2

    # Default: labeled rows for manual entry. Kept generic on purpose so the report
    # makes clear the section is a placeholder rather than claiming sector-specific focus.
    kpis = ["Key metric 1", "Key metric 2", "Key metric 3", "Key metric 4"]
    matters = ["Headline vs consensus", "Margin and pricing", "Guidance", "Key metrics"]
    p2 = "This quarter, sector operating metrics and headline results versus consensus matter most; earnings quality—whether a beat or miss is recurring or one-off—and guidance drive the narrative."
    return kpis, matters[:5], p2


def run(payload: ReportPayload, memo_data: dict | None = None, qa_audit: dict | None = None, data_warnings: list[str] | None = None) -> StepResult:
    with StepTimer() as t:
        try:
            ticker = payload.company.ticker
            out_dir = report_output_dir()
            # Microsecond precision guarantees two same-second runs for the
            # same ticker never collide / overwrite each other.
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            suffix = f"{ticker}_{ts}_earnings_preview.pptx"
            out_path = out_dir / suffix
            quality_flags: list[str] = []
            if qa_audit:
                # The legacy flags are too granular and overlap — for tickers
                # where MS was rate-limited or unreachable mid-run, the deck
                # rendered "MS entity mismatch suppressed; MS suppressed:
                # missing current data; MS suppressed: entity mismatch" which
                # made it look like a data-quality problem with the ticker
                # rather than a transient outage. Collapse those into one
                # honest message based on whether MS data was FETCHED at all
                # (no lineage = unavailable) vs FETCHED-BUT-REJECTED (lineage
                # present but entity check failed).
                no_lineage = qa_audit.get(
                    "ms_section_suppressed_due_to_missing_current_data", False
                )
                entity_mismatch = (
                    qa_audit.get("ms_section_suppressed_due_to_entity_mismatch", False)
                    or not qa_audit.get("payload_entity_match", True)
                )
                contamination = qa_audit.get("ms_section_suppressed_due_to_contamination", False)
                if contamination:
                    quality_flags.append("MarketScreener data flagged as cross-company duplicate")
                elif no_lineage:
                    # Most common cause: MS was unavailable (rate limit / network /
                    # captcha) when this run executed. NOT a data-correctness issue.
                    quality_flags.append("MarketScreener data unavailable for this run — falling back to Yahoo Finance")
                elif entity_mismatch:
                    # MS data WAS fetched but lineage failed entity validation —
                    # genuine wrong-entity case that needs the ticker mapping fixed.
                    quality_flags.append("MarketScreener data rejected (entity mismatch)")
                if qa_audit.get("reused_default_payload_detected"):
                    quality_flags.append("Default payload reused")
            # Add automated data validation warnings
            if data_warnings:
                quality_flags.extend(data_warnings)
            # 3-slide Jabal deck — reads exclusively from canonical_store.
            # Bootstraps a quick refresh if the store has no rows for this ticker.
            _write_jabal_preview(payload, out_path, memo_data)
            source_tag = "pptx-jabal"
            return StepResult(step_name=STEP, status=Status.SUCCESS, source=source_tag, message=f"Report saved → {out_path}", data=str(out_path), elapsed_seconds=t.elapsed)
        except Exception as exc:
            return StepResult(step_name=STEP, status=Status.FAILED, source="pptx", message="Report generation failed", error_detail=str(exc), elapsed_seconds=t.elapsed)
