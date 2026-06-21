"""
LLM-driven executive summary for the Jabal deck.

The auto-templated paragraph on slide 2 (the prior approach) was generic
because it pulled the same boilerplate phrases for every ticker. With
Bloomberg consensus + IR-PDF + free-source backstops feeding the canonical
store, we now have enough structured data per ticker to generate a real
analyst-grade paragraph.

This module builds a rich context dict per ticker, calls Gemini with a
disciplined prompt, and returns:
    {
        thesis_paragraph: str   (4-6 sentences),
        catalysts:        list[str],   (3 items)
        risks:            list[str],   (3 items)
        watch_list:       list[str],   (3 items, framed as questions)
        provider:         "gemini",
        model:            "<actual model name>",
        as_of:            ISO timestamp,
    }

Cached to disk by (ticker, context_hash) so a re-render is free unless
underlying data changes. Falls back gracefully to the prior template when:
  - GEMINI_API_KEY is not set
  - the model call fails / times out
  - returned text fails JSON validation
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.services.canonical_store import (
    get_all_fields, get_observations_by_provider,
)


log = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache" / "llm_summary"


# ── Context builder ──────────────────────────────────────────────

def _pretty_rating_label(raw) -> Optional[str]:
    """Normalise rating enums like 'STRONG_BUY' -> 'Strong Buy' so the LLM
    prompt and the slide labels agree. Returns None for empty input."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return " ".join(p.capitalize() for p in s.replace("_", " ").replace("-", " ").split())


def _fmt_num(v: Any, *, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 1e12: return f"{f/1e12:.2f}T{suffix}"
    if abs(f) >= 1e9:  return f"{f/1e9:.1f}B{suffix}"
    if abs(f) >= 1e6:  return f"{f/1e6:.0f}M{suffix}"
    if abs(f) < 1:     return f"{f:.3f}{suffix}"
    return f"{f:,.2f}{suffix}"


def build_context(ticker: str) -> dict:
    """Pull every datum the LLM might reasonably need from canonical_store
    + observation backfills, into a single flat-ish dict. The prompt
    references this verbatim — keys are deliberately readable."""
    cv = get_all_fields(ticker)
    investing_obs   = get_observations_by_provider(ticker, "investing")
    commodities_obs = get_observations_by_provider(ticker, "commodities")
    macro_obs       = get_observations_by_provider(ticker, "macro")
    bloomberg_obs   = get_observations_by_provider(ticker, "bloomberg")

    def _val(field):
        c = cv.get(field)
        return c.value if c else None

    profile = _val("company_profile") or {}
    if not isinstance(profile, dict):
        profile = {}

    target = _val("target_price") or {}
    if not isinstance(target, dict):
        target = {}

    rating = _val("rating_split") or {}
    if not isinstance(rating, dict):
        rating = {}

    val_fwd = _val("valuation_forward") or {}
    if not isinstance(val_fwd, dict):
        val_fwd = {}

    val_hist = _val("valuation_historical") or {}
    if not isinstance(val_hist, dict):
        val_hist = {}

    hist_prices = _val("historical_prices") or {}
    if not isinstance(hist_prices, dict):
        hist_prices = {}

    # P/E recent vs 5y average
    pe_recent = pe_avg = None
    pe_list = val_hist.get("pe") if isinstance(val_hist, dict) else []
    if isinstance(pe_list, list):
        nums = [v for v in pe_list if isinstance(v, (int, float))]
        if nums:
            pe_recent = nums[-1]
            pe_avg = sum(nums) / len(nums)

    # Surprise history (from Investing) — most useful for the "track record"
    # angle. CRITICAL: count beats ONLY across rows with a real surprise_pct
    # value. Without this guard, names where Investing never published an
    # epsForecast (BKMB-class thinly-covered banks) produce "0 of last 4
    # quarters above consensus EPS" — a false claim, not a missing-data note.
    raw_surprise = ((investing_obs.get("income_statement_quarterly") or {})
                     .get("surprise_history", []))
    surprise = [r for r in raw_surprise if isinstance(r, dict)]
    valid_surprises = [r for r in surprise
                         if isinstance(r.get("eps_surprise_pct"), (int, float))]
    if len(valid_surprises) >= 3:
        beats = sum(1 for r in valid_surprises[:4] if r["eps_surprise_pct"] > 0)
        n_recent = min(4, len(valid_surprises))
        last_surprise = valid_surprises[0]
    else:
        # Insufficient surprise data — pass empty values so the prompt's
        # template doesn't generate "0 of last 4 above consensus".
        beats = None
        n_recent = 0
        last_surprise = None

    # Broker actions (from MS canonical)
    ba_val = _val("broker_actions") or {}
    broker_items = ba_val.get("items", []) if isinstance(ba_val, dict) else []

    # Commodity context (from commodities provider observations)
    commodities = ((commodities_obs.get("company_profile") or {})
                   .get("industry_commodities", {}))

    # Macro context
    macro = macro_obs.get("company_profile") or {}

    current_price = _val("current_price")
    target_mean = target.get("mean")
    upside_pct = None
    try:
        if target_mean and current_price and float(current_price) > 0:
            upside_pct = (float(target_mean) / float(current_price) - 1.0) * 100
    except (TypeError, ValueError):
        pass

    # MarketScreener reports market_cap in millions of local currency; the
    # other providers report raw. Normalize to raw before passing to prompt.
    raw_mcap = _val("market_cap")
    mcap_source = (cv.get("market_cap").canonical_source
                    if cv.get("market_cap") else None)
    if mcap_source == "marketscreener" and isinstance(raw_mcap, (int, float)):
        raw_mcap = raw_mcap * 1_000_000

    # Disclosed quarterly actuals (company IR) — grounds {latest_actuals}
    # and the NII / fee-income trend the senior-analyst prompt leans on.
    # NIM, cost of risk, loan/deposit and capital are NOT in the disclosed
    # feed yet, so those stay absent and the prompt marks them "not
    # disclosed" (the model must stay qualitative on them, never invent).
    disclosed_quarters = []
    bank_fy = None
    try:
        from src.services.disclosed_loader import load_disclosed
        _dl = load_disclosed(ticker) or {}
        for q in (_dl.get("quarterly") or [])[-4:]:
            if not isinstance(q, dict):
                continue
            disclosed_quarters.append({
                "period": q.get("period"),
                "nii": q.get("net_interest_income") or q.get("net_interest_islamic_combined"),
                "fees": q.get("fee_income_net"),
                "operating_income": q.get("operating_income"),
                "net_income": q.get("net_income"),
                "eps": q.get("eps") or q.get("eps_actual"),
            })
        # Full-year IR metrics (loans/deposits/CAR/ROE/growth) — these
        # quantify what would otherwise be in the "NOT IN AVAILABLE DATA"
        # block, letting the bank prompts cite real balance-sheet trends.
        _fy = _dl.get("fy_highlights")
        if isinstance(_fy, dict):
            bank_fy = _fy
    except Exception:
        disclosed_quarters = []

    # Peer / industry P/E median — the comparator the VALUATION pill
    # should cite ("vs the GCC bank peer median near 9.0x"). Computed from
    # the SAME registry peer set the slide-3 table uses, so the deck and
    # the prose agree. Best-effort: thin coverage / network failure leaves
    # it None and the prompt degrades to the own-history comparator rather
    # than falling back to the bare template.
    # Peer P/E for the VALUATION pill — computed IDENTICALLY to the slide-3
    # peer table's "Peer Average" so the prose and the table never disagree:
    # same peer set (curated peer_group first, else registry) and the same
    # MEAN statistic (_peer_avg_row uses a mean, not a median).
    peer_pe_avg = None
    try:
        from src.services.ticker_registry import registry_peer_set
        from src.services.fetch_peers import fetch_peer_rows
        from src.storage.db import load_company as _lc_peer
        _crow = _lc_peer(ticker) or {}
        _peers = _crow.get("peer_group") or registry_peer_set(ticker) or []
        if _peers:
            _rows = fetch_peer_rows(_peers) or []
            _pes = [r.get("pe") for r in _rows
                    if isinstance(r.get("pe"), (int, float)) and r.get("pe") > 0]
            if len(_pes) >= 3:
                peer_pe_avg = round(sum(_pes) / len(_pes), 1)
    except Exception:
        peer_pe_avg = None

    # Normalise FY year labels (strip an existing "FY" prefix so the prompt
    # doesn't end up with "FYFY2026").
    def _norm_fy(v):
        s = str(v or "").strip()
        if s.upper().startswith("FY"):
            s = s[2:].lstrip()
        return s

    try:
        from src.services.ticker_registry import get_ticker_info as _gti
        _template_family = _gti(ticker).get("template_family") or "other"
    except Exception:
        _template_family = "other"

    return {
        "peer_pe_avg": peer_pe_avg,
        "disclosed_quarters": disclosed_quarters,
        "bank_fy": bank_fy,
        "template_family": _template_family,
        "ticker": ticker,
        "company_name": profile.get("name") or ticker,
        "sector": profile.get("sector") or "—",
        "industry": profile.get("industry") or "—",
        "country": profile.get("country") or "—",
        "currency": profile.get("currency") or "",
        # Live snapshot
        "current_price": current_price,
        "market_cap": raw_mcap,
        "dividend_yield_pct": _val("dividend_yield"),
        # Bloomberg / consensus (canonical source where present)
        "consensus_source": (cv.get("valuation_forward").canonical_source
                              if cv.get("valuation_forward") else None),
        "next_q_period": val_fwd.get("next_q_period"),
        "next_q_report_date": val_fwd.get("next_q_report_date"),
        "eps_next_q": val_fwd.get("eps_next_q"),
        "revenue_next_q": val_fwd.get("revenue_next_q"),
        "fy1_year": _norm_fy(val_fwd.get("fy1_year")),
        "eps_fy1": val_fwd.get("eps_fy1"),
        "revenue_fy1": val_fwd.get("revenue_fy1"),
        "fy2_year": _norm_fy(val_fwd.get("fy2_year")),
        "eps_fy2": val_fwd.get("eps_fy2"),
        "revenue_fy2": val_fwd.get("revenue_fy2"),
        # Target + rating
        "target_mean": target_mean,
        "target_high": target.get("high"),
        "target_low":  target.get("low"),
        "n_analysts":  rating.get("total") or target.get("n_analysts"),
        "rating_consensus": _pretty_rating_label(rating.get("consensus")),
        "buy_count":  rating.get("buy"),
        "hold_count": rating.get("hold"),
        "sell_count": rating.get("sell"),
        "upside_pct": upside_pct,
        # Valuation history (MS multi-year P/E series, last + avg)
        "pe_recent": pe_recent,
        "pe_5y_avg": pe_avg,
        # Forward P/E from Investing — the value that drives slide 1's
        # FY P/E chip. Including it here so the LLM's thesis paragraph
        # can cite the SAME number ("12.4x FY26") that the rest of the
        # deck displays, not the MS historical reversion-mean.
        "pe_fy1": val_fwd.get("pe_fy1"),
        "pe_fy2": val_fwd.get("pe_fy2"),
        # Surprise track record
        "surprise_beats_last4": beats,
        "surprise_n_recent": n_recent,
        "last_surprise": last_surprise,
        # Broker actions (3 most recent)
        "recent_broker_actions": broker_items[:3],
        # Commodity / macro overlays (optional)
        "commodities": commodities,
        # Macro context for the LLM prompt. We prefer IMF WEO forecasts
        # over World Bank historical actuals because the deck is forward-
        # looking — feeding Gemini the WB 2024 actual when the print is
        # Q2 2026 anchors the thesis on stale conditions (e.g. Oman GDP
        # growth was 1.6% in 2024 but IMF expects 3.5% in 2026, and
        # inflation 0.6%→1.7%). World Bank stays as a fallback when IMF
        # has no series for the country.
        "macro": {
            # Preferred: IMF current-year forecast (matches/leads the
            # company's reporting cycle).
            "gdp_growth_pct":      macro.get("gdp_growth_fcst_pct")
                                       or macro.get("gdp_growth_pct"),
            "gdp_growth_year":     macro.get("gdp_growth_fcst_year")
                                       or macro.get("macro_year"),
            "gdp_growth_source":   ("IMF" if macro.get("gdp_growth_fcst_pct") is not None
                                     else "WB"),
            # Forward year (IMF next-year forecast) — useful for swing-factor sentences.
            "gdp_growth_fcst_next_pct":  macro.get("gdp_growth_fcst_next_pct"),
            "gdp_growth_fcst_next_year": macro.get("gdp_growth_fcst_next_year"),
            "inflation_pct":       macro.get("inflation_fcst_pct")
                                       or macro.get("inflation_pct"),
            "inflation_year":      macro.get("inflation_fcst_year")
                                       or macro.get("macro_year"),
            "inflation_source":    ("IMF" if macro.get("inflation_fcst_pct") is not None
                                     else "WB"),
            "inflation_fcst_next_pct":  macro.get("inflation_fcst_next_pct"),
            "inflation_fcst_next_year": macro.get("inflation_fcst_next_year"),
        },
    }


# ── Prompt ───────────────────────────────────────────────────────

_SYSTEM = (
    "You are a senior sell-side equity research analyst writing an "
    "institutional earnings-preview note. Audience: portfolio managers, not "
    "retail. Voice: concise, balanced, skeptical, declarative — a morning "
    "research note. "
    "Every point must explain a MECHANISM: what changes, why it matters, and "
    "how it flows through to earnings, valuation, or the share-price reaction. "
    "For banks, use the right vocabulary where the data supports it — NII, "
    "NIM, deposit costs, loan growth, fee income, impairments, cost of risk, "
    "capital generation, ROE, funding mix, asset quality, dividends — but "
    "never attach a NUMBER to a metric the data block doesn't provide. "
    "BANNED unless tied to a specific metric in the data: 'strong "
    "fundamentals', 'positive outlook', 'solid performance', 'robust growth', "
    "'well-positioned', 'attractive valuation', 'we believe', 'appears to', "
    "'suggests', 'could potentially', 'may benefit'. "
    "Do not repeat an idea across sections. Do not sound promotional. Do not "
    "overstate confidence. Every number must come from the DATA block — never "
    "invent numbers, dates, estimates, or management guidance. If a fact is "
    "not in the data, do not state it."
)


def _sector_playbook(fam: str) -> dict:
    """Sector-appropriate analyst vocabulary, levers, and few-shot examples
    for the catalysts/risks/watch prompts. Without this the prompt only ever
    showed BANK levers (NIM, cost of risk, loan growth) — so a fertilizer or
    energy name got bank commentary stamped onto it. Keyed by the same
    template_family the grounding schema uses; falls back to a generic
    industrial/consumer playbook."""
    f = (fam or "other").lower()
    BANK = {
        "persona": "bank",
        "cat_levers": "loan growth, NII/NIM, fee income, credit costs, dividend sustainability, capital generation, valuation re-rating",
        "risk_levers": "NIM compression, funding costs, credit-cost normalization, slower loan growth, weak fee income, asset quality, dividend risk",
        "watch_metrics": "NIM/deposit costs, asset quality/cost of risk, loan & deposit growth, fee income, capital return",
        "cat_examples": (
            '     - "A buyback authorization on the call that lifts the payout\n'
            '        beyond the current 4.22% dividend yield."\n'
            '     - "Loan-growth guidance confirmed at the prior 5-7% range despite\n'
            '        deposit-cost pressure."\n'
            '     - "Fee income climbing toward 25% of revenue, cutting\n'
            '        spread-business dependence."'),
        "risk_examples": (
            '     - "Asset-quality drift from the current sub-2% NPL ratio as the\n'
            '        rate-cut cycle pressures provisioning expense."\n'
            '     - "A larger one-off provision charge resetting the cost-of-risk\n'
            '        run-rate higher."\n'
            '     - "Share loss to digital challengers accelerating cost-to-income\n'
            '        deterioration."'),
        "watch_examples": (
            '     - "What is the full-year NIM guidance range, and how much deposit\n'
            '        repricing is already assumed in it?"\n'
            '     - "Where does the loan-growth pipeline sit versus the prior 5-7%\n'
            '        guidance, and which segments are driving it?"\n'
            '     - "Is the dividend payout sustainable if cost-of-risk normalizes\n'
            '        from today\'s low base?"'),
    }
    ENERGY = {
        "persona": "energy / oil & gas",
        "cat_levers": "production volumes, realized prices, unit lifting/operating costs, free cash flow, capex discipline, dividend/buyback capacity, reserve replacement",
        "risk_levers": "a realized-price downcycle, unit-cost inflation, production outages, capex overruns, dividend coverage at lower prices",
        "watch_metrics": "production & realized prices, unit operating cost, free cash flow & capex, dividend coverage at the strip",
        "cat_examples": (
            '     - "A buyback top-up on the call that lifts the payout beyond the\n'
            '        current dividend yield as free cash flow recovers."\n'
            '     - "Full-year production guidance held despite natural field decline."\n'
            '     - "Free-cash-flow breakeven falling, widening the dividend cushion."'),
        "risk_examples": (
            '     - "A sustained pullback in realized prices compressing the free\n'
            '        cash flow that funds the dividend."\n'
            '     - "Unit operating costs drifting higher as mature fields decline,\n'
            '        squeezing upstream margins."\n'
            '     - "A capex overrun deferring the promised free-cash-flow inflection."'),
        "watch_examples": (
            '     - "What is full-year production guidance, and how much price\n'
            '        sensitivity is embedded in the free-cash-flow outlook?"\n'
            '     - "Where is the unit lifting cost trending versus the prior run-rate?"\n'
            '     - "Is the dividend covered by free cash flow at the strip?"'),
    }
    MATERIALS = {
        "persona": "materials & chemicals",
        "cat_levers": "product spreads, realized selling prices, feedstock/input costs, plant utilization, sales volumes, EBITDA margin, capacity additions, dividend",
        "risk_levers": "product-spread contraction, feedstock-cost inflation, new-capacity oversupply, demand softness lowering utilization, operating deleverage",
        "watch_metrics": "realized product spreads & prices, feedstock-cost pass-through, plant utilization & volumes, EBITDA margin, dividend",
        "cat_examples": (
            '     - "Product spreads widening off the trough as feedstock costs ease,\n'
            '        lifting the EBITDA margin from the FY25 level."\n'
            '     - "Utilization stepping up after turnaround season, driving\n'
            '        operating leverage on higher volumes."\n'
            '     - "A dividend top-up signalled as free cash flow recovers with prices."'),
        "risk_examples": (
            '     - "A renewed contraction in product spreads as new capacity floods\n'
            '        the market, reversing prior-year profit growth."\n'
            '     - "Feedstock/gas-cost inflation outpacing selling prices,\n'
            '        compressing the EBITDA margin."\n'
            '     - "Demand softness in a key export region forcing lower utilization\n'
            '        and operating deleverage."'),
        "watch_examples": (
            '     - "Where are realized product spreads versus last quarter, and what\n'
            '        is the feedstock-cost pass-through assumption?"\n'
            '     - "What plant utilization rate is assumed for the year, and how\n'
            '        does turnaround timing affect volumes?"\n'
            '     - "Is the dividend sustainable at mid-cycle spreads?"'),
    }
    TECH = {
        "persona": "technology",
        "cat_levers": "revenue growth, segment mix, gross/operating margin, bookings/deferred revenue, user & engagement metrics, capex/AI investment, free cash flow",
        "risk_levers": "decelerating segment growth, competitive pricing, margin compression from reinvestment, a bookings/backlog slowdown",
        "watch_metrics": "core-segment revenue growth, gross/operating margin, bookings, capex/AI spend, free cash flow",
        "cat_examples": (
            '     - "Revenue growth re-accelerating off the FY25 pace as the core\n'
            '        segment reaccelerates."\n'
            '     - "Operating-margin expansion as cost discipline outpaces reinvestment."\n'
            '     - "A buyback expansion as free-cash-flow conversion improves."'),
        "risk_examples": (
            '     - "Decelerating segment growth as competition intensifies,\n'
            '        pressuring the revenue trajectory."\n'
            '     - "Margin compression as AI/capacity investment outruns monetization."\n'
            '     - "A bookings/backlog slowdown signalling softer forward revenue."'),
        "watch_examples": (
            '     - "What is the growth trajectory of the core segment versus last\n'
            '        quarter?"\n'
            '     - "Where is the operating margin heading as reinvestment scales?"\n'
            '     - "How is capex/AI spend tracking against the monetization timeline?"'),
    }
    GENERIC = {
        "persona": "equity",
        "cat_levers": "revenue growth, volume/price mix, operating margin, input costs, free cash flow, capex efficiency, dividend/buyback",
        "risk_levers": "volume softness, input-cost inflation outpacing pricing, margin compression, a working-capital/capex step-up",
        "watch_metrics": "revenue growth & volume/price mix, operating margin, input costs, free cash flow, capital return",
        "cat_examples": (
            '     - "Revenue growth holding the prior pace as volume and price both\n'
            '        contribute."\n'
            '     - "Operating-margin expansion as input costs ease faster than\n'
            '        pricing resets."\n'
            '     - "A capital-return step-up as free-cash-flow conversion improves."'),
        "risk_examples": (
            '     - "Volume softening in the core market, pressuring operating\n'
            '        leverage and the margin."\n'
            '     - "Input-cost inflation outpacing pricing, compressing the\n'
            '        operating margin."\n'
            '     - "A working-capital or capex step-up eroding the free-cash-flow\n'
            '        outlook."'),
        "watch_examples": (
            '     - "What is the volume/price split behind the revenue outlook?"\n'
            '     - "Where is the operating margin heading as input costs move?"\n'
            '     - "Is the dividend/buyback sustainable on current free cash flow?"'),
    }
    return {
        "bank": BANK, "financial_services": BANK, "insurance": BANK,
        "energy": ENERGY,
        "materials": MATERIALS,
        "tech": TECH, "telco": TECH,
    }.get(f, GENERIC)


def _prompt(ctx: dict) -> str:
    # Pre-format numeric anchors so the model sees clean strings, not raw floats.
    cur = ctx.get("currency") or ""
    fmt_money = lambda v: f"{cur} {_fmt_num(v)}".strip()
    cur_price = fmt_money(ctx.get("current_price"))
    target = fmt_money(ctx.get("target_mean"))
    upside = (f"{ctx['upside_pct']:+.1f}%" if isinstance(ctx.get("upside_pct"), (int, float))
              else "—")
    next_q_eps = _fmt_num(ctx.get("eps_next_q"))
    next_q_rev = fmt_money(ctx.get("revenue_next_q"))
    fy1_eps = _fmt_num(ctx.get("eps_fy1"))
    fy1_rev = fmt_money(ctx.get("revenue_fy1"))

    surprise_lines = []
    for r in (ctx.get("last_surprise") and [ctx["last_surprise"]] or []):
        if r:
            s = r.get("eps_surprise_pct")
            if isinstance(s, (int, float)):
                surprise_lines.append(
                    f"Last quarter ({r.get('period','?')}): EPS "
                    f"{r.get('eps_actual')} vs estimate {r.get('eps_estimate')} "
                    f"({s:+.1f}%)."
                )
    beats_line = (
        f"{ctx['surprise_beats_last4']} of last {ctx['surprise_n_recent']} "
        f"quarters above consensus EPS."
    ) if ctx.get("surprise_n_recent") else ""

    pe_parts = []
    if isinstance(ctx.get("pe_fy1"), (int, float)):
        fy1 = ctx["pe_fy1"]
        yr = ctx.get("fy1_year") or ""
        pe_parts.append(f"Forward P/E {fy1:.1f}x (FY{yr})" if yr else f"Forward P/E {fy1:.1f}x")
    if isinstance(ctx.get("pe_recent"), (int, float)) and isinstance(ctx.get("pe_5y_avg"), (int, float)):
        avg = ctx["pe_5y_avg"]
        rec = ctx["pe_recent"]
        delta_pct = (rec / avg - 1.0) * 100 if avg else 0
        pe_parts.append(
            f"trailing P/E {rec:.1f}x vs 5-year average {avg:.1f}x "
            f"({delta_pct:+.0f}% relative)"
        )
    # Peer comparator — the preferred anchor for the VALUATION pill. This is
    # the SAME peer-set average shown on slide 3, so prose and table agree.
    if isinstance(ctx.get("peer_pe_avg"), (int, float)):
        ppm = ctx["peer_pe_avg"]
        rec = ctx.get("pe_recent")
        if isinstance(rec, (int, float)) and ppm:
            prem = (rec / ppm - 1.0) * 100
            pe_parts.append(
                f"peer-set average P/E {ppm:.1f}x "
                f"(subject {prem:+.0f}% vs peers)")
        else:
            pe_parts.append(f"peer-set average P/E {ppm:.1f}x")
    pe_line = "; ".join(pe_parts) + "." if pe_parts else ""

    # LATEST ACTUALS — company-disclosed quarterly trend ({latest_actuals}).
    # Grounds NII / fee-income / EPS direction. Values are raw currency; show
    # in millions for readability (EPS per-share).
    _act_lines = []
    for q in (ctx.get("disclosed_quarters") or []):
        seg = []
        if isinstance(q.get("nii"), (int, float)):        seg.append(f"NII {q['nii']/1e6:.1f}m")
        if isinstance(q.get("fees"), (int, float)):       seg.append(f"fees {q['fees']/1e6:.1f}m")
        if isinstance(q.get("net_income"), (int, float)): seg.append(f"NI {q['net_income']/1e6:.1f}m")
        if isinstance(q.get("eps"), (int, float)):        seg.append(f"EPS {q['eps']:.3f}")
        if seg:
            _act_lines.append(f"  {q.get('period','?')}: " + ", ".join(seg))
    actuals_block = (
        f"LATEST ACTUALS (company-disclosed / IR; {cur} millions unless noted)\n"
        + "\n".join(_act_lines)
    ) if _act_lines else ""

    # Full-year IR metrics — SECTOR-AWARE: iterate the ticker's family
    # grounding schema so banks get loans/deposits/CAR/ROE, energy gets
    # production/FCF/capex, materials gets volumes/EBITDA margin, etc.
    from src.services.grounding_schema import schema_for, not_disclosed_for
    fy = ctx.get("bank_fy") or {}
    fam = ctx.get("template_family") or "other"
    pb = _sector_playbook(fam)  # sector-appropriate levers + examples
    fy_block = ""
    if isinstance(fy, dict) and fy:
        # Financials are reported in the company's FUNCTIONAL currency, which
        # can differ from the trading currency (Tencent/ICBC report RMB but
        # trade in HKD). Label money with the filing's reporting currency.
        _rc = fy.get("reporting_currency") or cur
        fl = []
        for key, label, kind in schema_for(fam):
            v = fy.get(key)
            if not isinstance(v, (int, float)):
                continue
            # growth key is named off the BASE metric: net_profit_mn ->
            # net_profit_growth_pct (the "_mn" suffix is dropped).
            _base = key[:-3] if key.endswith("_mn") else key
            g = fy.get(f"{_base}_growth_pct") or fy.get(f"{key}_growth_pct")
            gs = f" ({g:+.1f}% YoY)" if isinstance(g, (int, float)) else ""
            if kind == "money_mn":
                disp = f"{v:,.0f} {_rc}m{gs}"
            elif kind == "pct":
                disp = f"{v:.2f}%{gs}"
            elif kind == "eps":
                disp = f"{v:g} {_rc}/sh{gs}"
            else:
                disp = f"{v:g}{gs}"
            fl.append(f"  {label}: {disp}")
        if fl:
            _fyp = fy.get("period", "FY")
            fy_block = (
                f"{_fyp} REPORTED ACTUALS (company IR — already published; this is "
                f"the most recent COMPLETED FULL YEAR, the prior-year baseline, "
                f"NOT guidance for the upcoming print)\n"
                + "\n".join(fl))

    # Sector-appropriate "not disclosed" list (qualitative-only metrics).
    not_disclosed_block = (
        "NOT IN AVAILABLE DATA (discuss qualitatively, never cite a number for "
        "these): " + ", ".join(not_disclosed_for(fam)) + "."
    )

    # Short commodity tags (e.g. "hh" for Henry Hub) get truncated by the
    # LLM into garbage strings — pre-expand to human-readable labels so
    # the prompt block reads cleanly and the model never invents a
    # short-form.
    _COMMODITY_LABELS = {
        "hh":          "Henry Hub natural gas",
        "wti":         "WTI crude",
        "brent":       "Brent crude",
        "urea":        "Urea",
        "ammonia":     "Ammonia",
        "copper":      "Copper",
        "gold":        "Gold",
        "coking_coal": "Coking coal",
        "iron_ore":    "Iron ore",
    }
    commodity_lines = []
    for tag, info in (ctx.get("commodities") or {}).items():
        if not isinstance(info, dict):
            continue
        val = info.get("value")
        yoy = info.get("yoy_pct")
        unit = info.get("unit", "")
        if val is None:
            continue
        label = _COMMODITY_LABELS.get(tag, tag.replace("_", " ").title())
        yoy_str = f" ({yoy:+.1f}% YoY)" if isinstance(yoy, (int, float)) else ""
        commodity_lines.append(f"{label}: {val} {unit}{yoy_str}")
    commodity_block = ("Commodity context: " + "; ".join(commodity_lines) + ".") if commodity_lines else ""

    # Macro block. Every figure is year + source stamped so Gemini cannot
    # confuse a 2024 World Bank actual with a 2026 IMF forecast. We lead
    # with the IMF forecast for the company's reporting year because the
    # deck is forward-looking — WB actuals fall back only when IMF lacks a
    # series for the country.
    macro = ctx.get("macro") or {}
    macro_parts = []
    if isinstance(macro.get("gdp_growth_pct"), (int, float)):
        src = macro.get("gdp_growth_source") or "IMF"
        yr  = macro.get("gdp_growth_year") or "?"
        macro_parts.append(f"GDP growth {macro['gdp_growth_pct']:.1f}% ({src} {yr})")
    if isinstance(macro.get("gdp_growth_fcst_next_pct"), (int, float)):
        ny = macro.get("gdp_growth_fcst_next_year") or "next year"
        macro_parts.append(
            f"GDP growth forecast {macro['gdp_growth_fcst_next_pct']:.1f}% (IMF {ny})"
        )
    if isinstance(macro.get("inflation_pct"), (int, float)):
        src = macro.get("inflation_source") or "IMF"
        yr  = macro.get("inflation_year") or "?"
        macro_parts.append(f"inflation {macro['inflation_pct']:.1f}% ({src} {yr})")
    if isinstance(macro.get("inflation_fcst_next_pct"), (int, float)):
        ny = macro.get("inflation_fcst_next_year") or "next year"
        macro_parts.append(
            f"inflation forecast {macro['inflation_fcst_next_pct']:.1f}% (IMF {ny})"
        )
    macro_block = ("Country macro: " + "; ".join(macro_parts) + ".") if macro_parts else ""

    broker_block = ""
    if ctx.get("recent_broker_actions"):
        rows = [
            f"  - {b.get('date','?')}: {b.get('headline','')[:120]}"
            for b in ctx["recent_broker_actions"][:3]
        ]
        broker_block = "Recent broker actions:\n" + "\n".join(rows)

    rating = ctx.get("rating_consensus") or "—"
    n_an = ctx.get("n_analysts") or 0
    b = ctx.get("buy_count") or 0
    h = ctx.get("hold_count") or 0
    s = ctx.get("sell_count") or 0

    return f"""{_SYSTEM}

COMPANY: {ctx['company_name']} ({ctx['ticker']})
SECTOR / INDUSTRY: {ctx['sector']} / {ctx['industry']} ({ctx['country']})

MARKET SNAPSHOT
  Last close: {cur_price}
  Market cap: {fmt_money(ctx.get('market_cap'))}
  Dividend yield: {_fmt_num(ctx.get('dividend_yield_pct'))}%

CONSENSUS (source: {ctx.get('consensus_source') or '—'}, {n_an} analysts)
  Next print: {ctx.get('next_q_report_date') or '—'} (period {ctx.get('next_q_period') or '—'})
  Next-Q  EPS: {next_q_eps} · Revenue: {next_q_rev}
  FY{ctx.get('fy1_year') or '—'}  EPS: {fy1_eps} · Revenue: {fy1_rev}
  Average target: {target} ({upside} vs last close), range {fmt_money(ctx.get('target_low'))}-{fmt_money(ctx.get('target_high'))}
  Rating: {rating} ({b}/{h}/{s} Buy/Hold/Sell)

VALUATION CONTEXT
  {pe_line}

{actuals_block}

{fy_block}

EARNINGS TRACK RECORD
  {beats_line}
  {' '.join(surprise_lines)}

{commodity_block}

{macro_block}

{broker_block}

{not_disclosed_block}

TASK
Write an institutional earnings-preview note for {ctx['company_name']}.
The audience already sees the raw numbers on the slide. Your job is
to INTERPRET — give the analyst a qualitative read on what matters
heading into the print. Anchor every claim to a MECHANISM and, where a
number exists in the data above, cite it; stay qualitative on anything
in the "NOT IN AVAILABLE DATA" line.

PERIOD DISCIPLINE (critical): the "REPORTED ACTUALS" block is the most
recent COMPLETED period the company has ALREADY published — it is the
prior-year baseline, NOT a forecast. NEVER call a reported actual a
"target", "guidance", "guide", "guided", or "ambitious goal". Frame it as
achieved ("FY2025 DELIVERED +13.3% net-profit growth / a 13.57% ROE") and
discuss the UPCOMING print RELATIVE to it ("the upcoming print will test
whether that pace holds / whether deposit costs are now eroding it"). The
forward question is about the NEXT print, never about the year already
reported.

Deliver a JSON object with these keys. No markdown, no preface, no
trailing prose. JSON only.

═══════════════════════════════════════════════════════════════════
"thesis_paragraph" — Executive Summary. FOUR sentences, 80–110 words.
Start with "{{Company}} enters [the upcoming quarter] earnings with focus
on …". Cover the main operating drivers, recent support factors, the key
investor concern, and the overall setup judgment. Do NOT include a
"what to watch" sentence (those are a separate card).
═══════════════════════════════════════════════════════════════════

   THE VOICE WE WANT. Three reference examples — match this rhythm,
   word count, and qualitative register. Notice: almost no numbers,
   no Mohamed-pleasing acronyms, no macro filler. Just a clean
   interpretive read.

   ┌─ Apple ───────────────────────────────────────────────────────┐
   │ Apple enters earnings with focus on iPhone demand, Services   │
   │ growth, gross margin, and next-quarter guidance. Recent       │
   │ performance has been supported by AI upgrade expectations,    │
   │ while China demand and hardware replacement cycles remain     │
   │ key concerns. Investors should watch iPhone revenue,          │
   │ Services growth, gross margin, Greater China performance,    │
   │ and commentary on AI integration and capital returns. The     │
   │ setup appears balanced, as valuation remains elevated and     │
   │ expectations are already fairly high.                         │
   └───────────────────────────────────────────────────────────────┘

   ┌─ JPMorgan ────────────────────────────────────────────────────┐
   │ JPMorgan enters earnings with focus on net interest income,   │
   │ loan growth, credit quality, and capital returns. Recent      │
   │ performance has been supported by resilient U.S. economic    │
   │ data and stronger banking sentiment, while investors remain   │
   │ focused on whether net interest income has peaked. Key        │
   │ metrics to watch include NII, provision expense, loan         │
   │ growth, deposit trends, investment banking fees, and          │
   │ management commentary on credit and capital deployment. The   │
   │ setup appears cautiously attractive, supported by strong      │
   │ profitability and balance sheet strength.                     │
   └───────────────────────────────────────────────────────────────┘

   ┌─ Tesla ───────────────────────────────────────────────────────┐
   │ Tesla enters earnings with focus on vehicle deliveries,       │
   │ automotive gross margin, pricing strategy, and demand         │
   │ outlook. Recent performance has been driven by expectations   │
   │ around autonomous driving, robotaxi developments, and future  │
   │ product launches, while margin pressure and softer EV demand  │
   │ remain concerns. Investors should watch automotive revenue,   │
   │ gross margin excluding credits, free cash flow, delivery      │
   │ outlook, energy storage growth, and commentary on pricing     │
   │ and autonomy. The setup appears high-risk, high-reward       │
   │ given the stock's reliance on future growth narratives.       │
   └───────────────────────────────────────────────────────────────┘

   STRUCTURE — FOUR sentences total (matches the reference boxes):
     S1. "{{Company}} enters [the upcoming quarter] earnings with focus
         on [4 operational levers most central to the print]." Pick the
         levers that define how investors model THIS business. For banks:
         net interest income, loan growth, credit quality, capital
         returns. For oil & gas: production volumes, realized prices,
         capex, free cash flow. For tech: revenue growth, gross margin,
         AI/product momentum, guidance.
     S2. "Recent performance has been supported by [drivers],
         while [pressures] remain key concerns." REQUIRED. This sentence
         MUST cite at least one CONCRETE COMPANY figure from the data
         block (a sector-relevant operating metric: revenue/volume growth %,
         a margin trend, net-profit growth %, ROE, or one of {pb['cat_levers']})
         — NOT a macro stat, NOT generic prose. A reader must be able to tell
         this is {{Company}} and not "any peer". If the only numbers in the
         whole paragraph are a P/E and a yield, the summary has failed.
     S3. "Investors should watch [4-6 specific things, including
         management commentary on X]." A concise list of the concrete
         metrics/lines the print will turn on — the high-level read; the
         slide-2 card asks the detailed questions.
     S4. "The setup appears [balanced / cautiously attractive /
         asymmetric / high-risk-high-reward], [one-clause
         reason]." Stop here. No scenario coda.

   ALL FOUR sentences are MANDATORY (~80-110 words total). The #1 failure
   mode is dropping S2 to make room for the S3 watch sentence — DO NOT.
   S2 (the drivers sentence with a concrete company figure) is the most
   important; if space is tight, shorten S1 and S3, never omit S2. A
   four-sentence summary whose ONLY numbers are a P/E and a yield has
   FAILED — it must carry a real company figure (loan growth, NII trend,
   ROE, profit growth) in S2. Do not stuff numbers beyond that one
   required figure plus, optionally, one valuation multiple.

═══════════════════════════════════════════════════════════════════
"highlights" — 5 short interpretive sentences (one per category).
═══════════════════════════════════════════════════════════════════

   Schema: list of {{"category": str, "body": str}}. Categories in
   order: EARNINGS, VALUATION, POSITIONING, WATCH, RISK. Each body is
   ONE sentence, ≤22 words. Each must frame an INVESTOR DEBATE (not a
   generic observation) and turn on a mechanism — what changes and why
   it matters for this name. No repetition across the five.

   EARNINGS — the company-specific operational lever this print will
   test. NEVER frame it around GDP / inflation / "macro backdrop" —
   that reads as filler and isn't a print-level driver. Name the lever.
   KEEP IT TIGHT: ≤18 words (this pill tends to run long and get cut).
   Use one of this company's own levers ({pb['cat_levers']}).
     Good (bank): "Q2 print will test whether loan growth holds the prior
            5-7% pace as deposit competition bites."
     Good (materials): "Q2 print will test whether product spreads have
            troughed after the FY25 margin squeeze."
     Weak: "Growth amid 1.6% GDP growth." (macro is backdrop,
            not the driver; says nothing about the print)

   VALUATION — ALWAYS lead with the LIVE multiple (the number on the
   slide). Then add a comparator: prefer the PEER-SET AVERAGE when the
   data block gives one ("peer-set average P/E" — the SAME figure shown
   on the slide-3 peer table; cite it as "peer average", never "median");
   if it doesn't, the 5-year own-average is acceptable. Hard rule: a
   comparator NEVER stands alone — the live multiple must be present.
     Good: "Trades at 11.1x trailing earnings, a ~14% premium to the
            GCC bank peer average near 9.7x."
     Also good (no peer data): "Forward P/E 12.1x, a 4% premium to its
            own 5-year average of 10.7x."
     Weak: "Trades at a premium to its 5-year average." (no live
            multiple — comparator standing alone)

   POSITIONING — the Street's posture. Rating split, target
   dispersion, beat history. Interpretive, not restated.
     Good: "Street is constructive with 2 of 3 analysts at Buy,
            though target range OMR 0.39-0.52 signals thin
            conviction."

   WATCH — the single highest-conviction thing to listen for. One.
     Good (bank): "Whether management raises mid-cycle NIM guidance on
            the call." Good (materials): "Whether product spreads have
            troughed and management signals a margin recovery."

   RISK — a SPECIFIC downside MECHANISM with a magnitude or baseline,
   not a generic worry. Bad risks are vanilla ("slower activity could
   pressure growth"); good risks name the channel AND a number/threshold.
   Use the sector's own levers ({pb['risk_levers']}).
     Good (bank): "A 25-30bp NIM slip as deposits reprice faster than the
            ~60% CASA book would cut pre-provision profit mid-single
            digits."
     Weak: "Slower economic activity could pressure loan growth and
            asset quality." (no mechanism, no magnitude, says nothing
            a peer's risk pill couldn't)

═══════════════════════════════════════════════════════════════════
"catalysts" — 3 forward-looking drivers specific to the company.
═══════════════════════════════════════════════════════════════════

   What could make investors MORE POSITIVE after the print. Each bullet
   is ≤24 words and MUST carry an explicit cause→effect (the trigger AND
   the read-through to earnings, capital, valuation, or the share price).
   For this company ({fam} sector), draw from: {pb['cat_levers']}.
   Anchor to a number where the data supports it. If swapping the company
   name with a peer would still make the bullet read true, it's too
   generic — rewrite.

   Rules:
     - Do NOT mention "macro backdrop", GDP, or inflation in a catalyst
       at all. Anchor on a company lever (e.g. "loan growth above the
       FY25 +4.8% pace", NOT "outpaces the macro backdrop").
     - ONLY cite a number that appears in the DATA block above (the FY
       metrics, valuation multiples, yield, growth rates). For a
       magnitude the data does NOT carry — NIM in bps, a guidance range,
       a cost-of-risk level — describe it QUALITATIVELY ("a sharp step-up",
       "above the recent run-rate"); NEVER invent a specific figure. An
       invented number gets flagged and undermines the whole bullet.
     - No vague nouns like "updates", "developments", "progress" — say
       WHAT specifically (a buyback authorization, a guidance raise, a
       fee-income mix shift).

   Examples of the right shape:
{pb['cat_examples']}

═══════════════════════════════════════════════════════════════════
"risks" — 3 company-specific downside paths.
═══════════════════════════════════════════════════════════════════

   What could cause a NEGATIVE share-price reaction after the print.
   Each bullet is ≤26 words and MUST explain BOTH the mechanism AND the
   impact (on earnings, capital generation, valuation, or investor
   confidence). For this company ({fam} sector), draw from: {pb['risk_levers']}.
   Avoid generic macro risk unless directly linked to this company's
   earnings. The 3 must be DISTINCT channels, no
   overlap with one another or with the slide-1 RISK pill, and must not
   restate valuation if that already lives in the VALUATION pill.

   Rules:
     - BANNED filler — NEVER use these, they say nothing: "unexpected",
       "unforeseen", "higher-than-expected", "lower-than-expected",
       "slower-than-expected", "better-than-expected", "faster-than-
       expected". Name the actual trigger (a repricing channel, a specific
       exposure, a one-off charge) and its direction instead.
     - ONLY cite a number that appears in the DATA block. For a magnitude
       the data doesn't carry (NIM bps, cost-of-risk level, a guidance
       range) describe it QUALITATIVELY ("a sharp normalization", "off the
       current low base") — never invent a figure.
     - Don't repeat the same worry (e.g. "slower loan growth") across
       multiple bullets or across slides; if it's already on the deck,
       pick a different downside path.

   Examples of the right shape:
{pb['risk_examples']}

═══════════════════════════════════════════════════════════════════
"watch_list" — 3 questions to put to management on the call.
═══════════════════════════════════════════════════════════════════

   Sharp, specific questions ending in "?". These live in their own
   "What to Watch" card and are NOT echoed in the thesis paragraph
   (the exec summary no longer carries a watch sentence). Because this
   card now stands alone, make each question substantive — name the
   metric AND the threshold/context that makes the answer actionable,
   not just a one-line ask.

   Examples:
{pb['watch_examples']}

═══════════════════════════════════════════════════════════════════
NUMERIC DISCIPLINE
═══════════════════════════════════════════════════════════════════

  Every number you cite must come from the DATA block above. No
  rounding inconsistencies, no hallucinated comparators.

  When citing a P/E, keep the qualifier ("forward", "trailing",
  "FY26E") so the reader doesn't conflate them.

  When citing macro, keep the "(IMF YYYY)" / "(WB YYYY)" tag.

MACRO DISCIPLINE
  The macro block is BACKGROUND only. Do NOT make GDP or inflation the
  driver of an EARNINGS pill, a catalyst, or a risk — those must anchor
  on company-specific operational levers ({pb['cat_levers']}). Macro may
  appear at most once, in the thesis S2
  "drivers/pressures" clause, and only if it genuinely moved the stock.
"""


# ── Numeric-trace validator ───────────────────────────────────────
#
# Even with a tight grounding contract, the LLM occasionally invents
# numbers (rounds inconsistently, hallucinates a "5-year average" the
# context never carried, etc.). The validator extracts every numeric
# token from each LLM-generated sentence and confirms it matches a value
# we actually have in the context (or a stated derivation: surprise
# averages, target spreads, P/E premium to history). Sentences whose
# numbers don't trace are dropped — the renderer falls back to the
# deterministic catalyst/risk defaults for any bullets that drop out.
#
# Tolerance: ±5% relative for matches (covers reasonable rounding); a
# table of common derivations is computed once from the context.

import re as _re


def _allowed_numbers(ctx: dict) -> set[float]:
    """Build the whitelist of numeric values the LLM may cite. Includes
    raw fields, simple derivations (% changes, averages), and a few
    rounded variants so a "12.4x" match doesn't fail against 12.39."""
    vals: set[float] = set()

    def _add(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                f = float(v)
                vals.add(round(f, 2))
                # Integer rounding ONLY for values that are conceptually
                # integers (analyst counts, beat ratios). Continuous
                # values like 4.83% yield shouldn't unlock 5.0 in the
                # allow-set — that's how "5.2% average surprise" slips
                # past the validator.
                if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
                    vals.add(round(f, 0))
            except (ValueError, OverflowError):
                pass

    # Raw scalars from context (anything that's a number).
    def _walk(d):
        if isinstance(d, dict):
            for v in d.values():
                _walk(v)
        elif isinstance(d, list):
            for v in d:
                _walk(v)
        else:
            _add(d)
    _walk(ctx)

    # Derived: P/E premium/discount to 5y average.
    pe_recent = ctx.get("pe_recent")
    pe_avg = ctx.get("pe_5y_avg")
    if isinstance(pe_recent, (int, float)) and isinstance(pe_avg, (int, float)) and pe_avg:
        delta = (pe_recent / pe_avg - 1.0) * 100
        _add(delta)
        _add(-delta)

    # Derived: subject P/E premium/discount to the PEER median — the
    # comparator the VALUATION highlight is now told to cite. Without this
    # the validator dropped any "X% premium to peers" pill, forcing the
    # template fallback. Allow both forward and trailing vs the peer median.
    peer_med = ctx.get("peer_pe_avg")
    if isinstance(peer_med, (int, float)) and peer_med:
        for own in (pe_recent, ctx.get("pe_fy1")):
            if isinstance(own, (int, float)):
                d = (own / peer_med - 1.0) * 100
                _add(d)
                _add(-d)

    # Derived: target upside/downside (already in ctx as upside_pct, but
    # also surface the absolute pct for downside framing).
    up = ctx.get("upside_pct")
    if isinstance(up, (int, float)):
        _add(abs(up))

    # Derived: average surprise pct across valid surprises.
    last = ctx.get("last_surprise") or {}
    sp = last.get("eps_surprise_pct") if isinstance(last, dict) else None
    if isinstance(sp, (int, float)):
        _add(abs(sp))

    # Beat ratio numerator/denominator (e.g. "3 of 4 quarters").
    if isinstance(ctx.get("surprise_beats_last4"), int):
        _add(ctx["surprise_beats_last4"])
    if isinstance(ctx.get("surprise_n_recent"), int):
        _add(ctx["surprise_n_recent"])

    # Rating split + total
    for k in ("buy_count", "hold_count", "sell_count", "n_analysts"):
        _add(ctx.get(k))
    if all(isinstance(ctx.get(k), int) for k in ("buy_count", "hold_count", "sell_count")):
        _add(ctx["buy_count"] + ctx["hold_count"] + ctx["sell_count"])

    # Commodity YoY values
    for tag, info in (ctx.get("commodities") or {}).items():
        if isinstance(info, dict):
            _add(info.get("value"))
            _add(info.get("yoy_pct"))
            yoy = info.get("yoy_pct")
            if isinstance(yoy, (int, float)):
                _add(abs(yoy))

    return vals


_NUM_PATTERN = _re.compile(
    # Match signed/unsigned decimals, optionally followed by %, x, B, M, T, bps
    r"[+-]?\d+(?:[,\d]*\.?\d+|\.\d+)?(?:\s*(?:%|x|bps|B|M|T|K))?"
)
_INTEGER_NUM = _re.compile(r"\b\d{1,2}\b")


def _extract_numbers(text: str) -> list[float]:
    """Pull every numeric literal out of an LLM sentence. Strips
    commas, percentage signs, and unit suffixes; returns floats."""
    out = []
    for m in _NUM_PATTERN.finditer(text or ""):
        raw = m.group(0).strip()
        # strip suffix unit
        token = _re.sub(r"\s*(?:%|x|bps|B|M|T|K)$", "", raw, flags=_re.IGNORECASE)
        token = token.replace(",", "").rstrip(".")
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def _number_matches(n: float, allowed: set[float], *, tol_pct: float = 5.0) -> bool:
    """Is n within tol_pct of any allowed value? Also accepts exact small
    integers (analyst counts, beat ratios) — those have to match exactly
    because rounding 2.0 to 3 changes the meaning."""
    if not allowed:
        return True  # nothing to validate against
    n_abs = abs(n)
    if n_abs == 0:
        return 0.0 in allowed or any(abs(a) < 0.01 for a in allowed)
    # Exact integer match (analyst counts, beat ratios)
    if n.is_integer() and 0 <= n <= 50:
        if n in allowed or -n in allowed:
            return True
    for a in allowed:
        a_abs = abs(a)
        denom = max(n_abs, a_abs)
        if denom < 0.01:
            continue
        if abs(n - a) / denom * 100 <= tol_pct:
            return True
        # Try sign-flipped (writer says "+5%" against a stored -5% value).
        if abs(n + a) / denom * 100 <= tol_pct:
            return True
    return False


def _validate_sentence(text: str, allowed: set[float]) -> bool:
    """Sentence passes if every numeric token in it traces to an allowed
    value. A sentence with no numeric tokens passes trivially (we want
    qualitative analytical voice to survive)."""
    nums = _extract_numbers(text)
    if not nums:
        return True
    return all(_number_matches(n, allowed) for n in nums)


_THAN_EXPECTED_RE = _re.compile(
    r"\b(faster|slower|higher|lower|better|weaker|stronger|larger|smaller)"
    r"[ -]than[ -]expected\b", _re.IGNORECASE)


def _strip_banned_qualifiers(text: str) -> str:
    """Deterministically remove the empty '*-than-expected' qualifier the
    model occasionally slips in despite the prompt ban, keeping the
    adjective so the sentence still reads: 'faster-than-expected deposit
    repricing' -> 'faster deposit repricing'. Cheap, meaning-preserving,
    reliable — no second LLM call."""
    if not text:
        return text
    out = _THAN_EXPECTED_RE.sub(lambda m: m.group(1), text)
    # tidy any double spaces the substitution leaves behind
    return _re.sub(r"\s{2,}", " ", out).strip()


def _focused_sections_prompt(ctx: dict, which: list[str]) -> str:
    """A SHORT, truncation-proof prompt that re-requests only the sections
    the main call left empty (the big prompt occasionally truncates before
    catalysts/risks/watch). Compact data block + just those rules."""
    cur = ctx.get("currency") or ""
    fy = ctx.get("bank_fy") or {}
    pb = _sector_playbook(ctx.get("template_family") or "other")
    L = [f"{ctx.get('company_name')} ({ctx.get('ticker')}) — {ctx.get('sector') or ''}."]
    if isinstance(ctx.get("pe_recent"), (int, float)):
        L.append(f"Trailing P/E {ctx['pe_recent']:.1f}x; peer-set average P/E "
                 f"{ctx.get('peer_pe_avg','—')}x; dividend yield "
                 f"{_fmt_num(ctx.get('dividend_yield_pct'))}% TTM.")
    L.append(f"Consensus {ctx.get('rating_consensus') or '—'} "
             f"({ctx.get('buy_count',0)}/{ctx.get('hold_count',0)}/{ctx.get('sell_count',0)} "
             f"B/H/S); target {cur} {_fmt_num(ctx.get('target_mean'))} "
             f"({_fmt_num(ctx.get('upside_pct'))}% upside).")
    if isinstance(fy, dict) and fy:
        # Schema-driven: list the family's grounded actuals (growth where we
        # have it, else the level) so non-bank sectors get a real FY line too.
        from src.services.grounding_schema import schema_for
        seg = []
        for key, label, kind in schema_for(ctx.get("template_family") or "other"):
            _base = key[:-3] if key.endswith("_mn") else key
            g = fy.get(f"{_base}_growth_pct")
            v = fy.get(key)
            if isinstance(g, (int, float)):
                seg.append(f"{label} {g:+.1f}% YoY")
            elif isinstance(v, (int, float)) and kind == "pct":
                seg.append(f"{label} {v:.2f}%")
        if seg:
            L.append(f"{fy.get('period','FY')} REPORTED ACTUALS (already "
                     "published; prior-year baseline, NOT guidance): "
                     + ", ".join(seg) + ".")
    data = "\n".join(L)

    period_rule = (
        "PERIOD DISCIPLINE: the reported-actuals figures are the COMPLETED "
        "prior year — NEVER call them a 'target'/'guidance'/'guided'. Frame as "
        "achieved ('FY2025 delivered +X%') and discuss the UPCOMING print "
        "relative to them.\n")

    rules = {
        "catalysts": ('"catalysts": exactly 3 bullets, <=24 words each, what would make '
                      'investors MORE POSITIVE after the print. Each = trigger -> effect on '
                      'earnings/capital/valuation. Cite ONLY numbers above; describe other '
                      'magnitudes qualitatively. No macro driver, no vague "update".'),
        "risks": ('"risks": exactly 3 bullets, <=26 words each, what would cause a NEGATIVE '
                  'share-price reaction. Each = mechanism + impact. 3 distinct channels. '
                  'Cite ONLY numbers above. BANNED: "unexpected", "*-than-expected".'),
        "watch_list": ('"watch_list": exactly 3 sharp questions to management ending in "?", '
                       'each naming a metric + threshold (' + pb["watch_metrics"] + ').'),
    }
    want = [k for k in ("catalysts", "risks", "watch_list") if k in which]
    body = "\n".join(rules[k] for k in want)
    keys = ", ".join(f'"{k}": [...]' for k in want)
    return (
        f"You are a senior sell-side {pb['persona']} analyst. Use ONLY the data "
        "below; never invent numbers. Mechanism-driven and sector-specific "
        f"(draw from: {pb['cat_levers']}), no generic filler. Do NOT use banking "
        "vocabulary (NIM, cost of risk, loans/deposits) unless this is a bank.\n\n"
        f"DATA\n{data}\n\n{period_rule}\nWrite these sections:\n{body}\n\n"
        f"Return JSON only: {{{keys}}}."
    )


def _validate_llm_output(payload: dict, ctx: dict) -> dict:
    """Drop sentences / bullets whose numbers don't trace to the context.

    Loose by design: we only police hallucinated numbers (the one thing
    Gemini can do that would mislead the analyst). Voice, phrasing,
    bullet count, and analytical framing are the model's leeway. A
    bullet with NO numbers passes trivially — interpretation without
    a number is fine and often better than a number with no insight.
    """
    allowed = _allowed_numbers(ctx)

    # Strip the empty "*-than-expected" qualifier FIRST — before the
    # highlight word-cap check — so (a) shortening a pill can rescue it from
    # the cap (and the datapoint-template fallback) and (b) it's gone
    # everywhere downstream. Operate on a stripped copy of the payload.
    payload = dict(payload)
    payload["thesis_paragraph"] = _strip_banned_qualifiers(payload.get("thesis_paragraph", ""))
    payload["highlights"] = [
        {**h, "body": _strip_banned_qualifiers(h.get("body", ""))}
        for h in (payload.get("highlights") or []) if isinstance(h, dict)
    ]
    for _k in ("catalysts", "risks", "watch_list"):
        payload[_k] = [_strip_banned_qualifiers(x)
                       for x in (payload.get(_k) or []) if isinstance(x, str)]
    cleaned = dict(payload)

    # Thesis: drop sentences whose numbers don't trace. Re-join the rest.
    # IMPORTANT: split on a terminator followed by whitespace + capital
    # letter so decimal numbers stay intact. The earlier `findall` regex
    # had two failure modes: (1) split "3.5%" into "3" / "5%", (2)
    # silently SKIP regions it couldn't match (the bytes between matches
    # were dropped entirely), amputating prose mid-clause. `re.split` on
    # an actual sentence boundary preserves every byte and only splits
    # at true sentence ends.
    thesis = payload.get("thesis_paragraph") or ""
    if thesis:
        # Non-destructive (same policy as catalysts/risks below). Dropping a
        # sentence whose number didn't trace used to amputate S2 — the
        # REQUIRED company-figure sentence (e.g. "net profit growth of
        # +13.3%") — leaving a generic ~38-word stub. The prompt already
        # forces S2's figure to come from the data block; a near-miss on
        # rounding shouldn't gut the Executive Summary. Keep the prose; the
        # tier-3 QA flags any untraceable number instead of deleting it.
        cleaned["thesis_paragraph"] = thesis.strip()
        sentences = _re.split(r"(?<=[.!?])\s+(?=[A-Z])", thesis.strip())
        ungrounded = [s for s in sentences if not _validate_sentence(s, allowed)]
        if ungrounded:
            log.info("[validate] %d thesis sentence(s) carry an ungrounded "
                     "number (kept; flagged by QA)", len(ungrounded))

    # Highlights: number-trace + ≤20-word cap so the pill doesn't overflow.
    hl = payload.get("highlights") or []
    hl_kept: list[dict] = []
    for it in hl:
        if not isinstance(it, dict):
            continue
        body = (it.get("body") or "").strip()
        if not body:
            continue
        if not _validate_sentence(body, allowed):
            continue
        # Word cap MUST match the prompt's "≤22 words" — an earlier 20-word
        # cap silently dropped valid 21-22 word highlights, which is why the
        # slide-1 pills fell back to the deterministic datapoint template.
        if len(body.split()) > 22:
            continue
        hl_kept.append(it)
    cleaned["highlights"] = hl_kept

    # Catalysts / risks / watch_list: KEEP every non-empty bullet. We used
    # to DROP any bullet citing an untraceable number, but that meant a
    # single analytical magnitude (e.g. "cost of risk above 50bp") wiped the
    # whole bullet and the deck fell back to a GENERIC hardcoded default —
    # far worse than the LLM's real, mechanism-driven bullet. The
    # number-trace guard is meant for the thesis paragraph's REPORTED
    # figures, not for analyst framing in catalysts/risks. Ungrounded
    # numbers here are surfaced by the tier-3 QA flag instead of deleted.
    for key in ("catalysts", "risks", "watch_list"):
        items = payload.get(key) or []
        kept = [it for it in items if isinstance(it, str) and it.strip()]
        cleaned[key] = kept
        ungrounded = [it for it in kept if not _validate_sentence(it, allowed)]
        if ungrounded:
            log.info("[validate] %d %s bullet(s) carry an ungrounded number "
                     "(kept; flagged by QA)", len(ungrounded), key)

    return cleaned


# ── Tier-3 QA review (deterministic, non-destructive) ────────────
#
# Runs AFTER the number-trace validator. It does not delete content
# (deleting a weak highlight only forces the datapoint template, which is
# worse) — it FLAGS quality problems so they surface in logs and the
# provenance audit. Catches the mechanical violations the analyst review
# called out: generic filler, datapoint restatement, banned hedges,
# length overflow, and the same idea repeated across sections.

_BANNED_PHRASES = (
    "strong fundamentals", "solid fundamentals", "solid performance",
    "strong performance", "positive outlook", "stable results",
    "growth momentum", "robust growth", "well-positioned", "well positioned",
    "attractive valuation", "compelling valuation", "healthy",
    "we believe", "appears to", "higher-than-expected", "lower-than-expected",
    "slower-than-expected", "better-than-expected", "unexpected",
)
# Fragments that only appear in the deterministic _derive_highlights
# template — their presence means the LLM pill was dropped and we fell
# back to a datapoint restatement.
_TEMPLATE_FRAGMENTS = (
    "supports income mandate fit", "anchor for re-rate / de-rate debate",
    "(buy/hold/sell) — view dispersion", "entry-point context",
    "institutional-grade liquidity", "index-eligible scale",
    "consensus build-up tracked", "refer to the 52-week band",
    # EARNINGS-pill template fallbacks (LLM version was dropped).
    "sets a high bar the print must clear",
    "must validate the earnings trajectory already priced in",
    "tests whether the recent earnings trajectory can be sustained",
    "analysts covering",
)
_WORD_CAPS = {"highlights": 22, "catalysts": 24, "risks": 26}


def qa_review(payload: dict) -> list[str]:
    """Return a list of human-readable QA findings (empty = clean)."""
    findings: list[str] = []

    def _scan_banned(label: str, text: str):
        low = text.lower()
        for ph in _BANNED_PHRASES:
            if ph in low:
                findings.append(f"{label}: banned phrase '{ph}'")

    # Executive summary: banned phrases + 80-110 word band.
    thesis = (payload.get("thesis_paragraph") or "").strip()
    if thesis:
        _scan_banned("exec_summary", thesis)
        wc = len(thesis.split())
        if not (75 <= wc <= 115):
            findings.append(f"exec_summary: {wc} words (target 80-110)")

    # Highlights: template fallback, banned phrases, datapoint restatement, length.
    for it in (payload.get("highlights") or []):
        if not isinstance(it, dict):
            continue
        body = (it.get("body") or "").strip()
        cat = (it.get("category") or "?").strip()
        if not body:
            continue
        low = body.lower()
        _scan_banned(f"highlight[{cat}]", body)
        for frag in _TEMPLATE_FRAGMENTS:
            if frag in low:
                findings.append(f"highlight[{cat}]: datapoint/template restatement ('{frag}')")
        if len(body.split()) > _WORD_CAPS["highlights"]:
            findings.append(f"highlight[{cat}]: {len(body.split())} words (>22)")

    # Catalysts / risks: banned phrases + length + (risks) require a mechanism cue.
    for key in ("catalysts", "risks"):
        for i, it in enumerate(payload.get(key) or [], 1):
            if not isinstance(it, str) or not it.strip():
                continue
            _scan_banned(f"{key}[{i}]", it)
            if len(it.split()) > _WORD_CAPS[key]:
                findings.append(f"{key}[{i}]: {len(it.split())} words (>{_WORD_CAPS[key]})")

    # Cross-section repetition: distinctive 3-word phrases shared across sections.
    buckets: dict[str, str] = {
        "exec": thesis,
        "highlights": " ".join((it.get("body") or "") for it in (payload.get("highlights") or []) if isinstance(it, dict)),
        "catalysts": " ".join(payload.get("catalysts") or []),
        "risks": " ".join(payload.get("risks") or []),
        "watch": " ".join(payload.get("watch_list") or []),
    }
    _STOP = {"the", "and", "with", "from", "that", "this", "into", "over",
             "for", "are", "has", "have", "its", "a", "an", "of", "to", "in",
             "on", "as", "is", "at", "by", "or", "if"}
    def _trigrams(t: str) -> set:
        ws = [w for w in _re.findall(r"[a-z']+", t.lower()) if w not in _STOP and len(w) > 2]
        return {" ".join(ws[i:i+3]) for i in range(len(ws) - 2)}
    seen: dict[str, str] = {}
    for name, txt in buckets.items():
        for tg in _trigrams(txt):
            if tg in seen and seen[tg] != name:
                findings.append(f"repetition: '{tg}' in both {seen[tg]} and {name}")
            else:
                seen.setdefault(tg, name)

    return findings


# ── Cache ────────────────────────────────────────────────────────

def _cache_key(ctx: dict) -> str:
    """Hash the context (excluding volatile fields) so equal data → same key."""
    stable = {k: v for k, v in ctx.items() if k not in ("ticker",)}
    payload = json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _cache_path(ticker: str, key: str) -> Path:
    safe = ticker.replace("/", "_").replace(".", "_")
    return _CACHE_DIR / f"{safe}__{key}.json"


def _read_cache(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


# ── Main entry point ─────────────────────────────────────────────

def generate_summary(ticker: str, *, force_refresh: bool = False) -> Optional[dict]:
    """Build context → optionally call Gemini → return structured summary
    or None on failure. Caller is responsible for fallback behaviour.

    Cached by (ticker, context_hash) — re-rendering the deck without
    refreshing canonical_store is a free hit."""
    # Explicit LLM-off mode: deterministic template prose, no Gemini call,
    # and ignore any stale cached LLM output. Set DISABLE_LLM=1 to run the
    # whole product number-first without a Gemini key/billing.
    if os.environ.get("DISABLE_LLM", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    ctx = build_context(ticker)
    key = _cache_key(ctx)
    path = _cache_path(ticker, key)
    if not force_refresh:
        cached = _read_cache(path)
        if cached:
            return cached

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        log.info("GEMINI_API_KEY not set; LLM summary unavailable for %s", ticker)
        return None

    try:
        from src.providers.gemini import _call_gemini  # reuse the wrapper
        prompt = _prompt(ctx)
        out = _call_gemini(prompt, for_investment_view=True)
    except Exception as exc:
        log.warning("Gemini summary failed for %s: %s", ticker, exc)
        return None

    if not out or not isinstance(out, dict):
        return None

    # Validate shape — only the thesis is mandatory. If a truncated response
    # dropped the catalysts/risks/watch KEYS entirely, don't fail the whole
    # summary (that wastes the good thesis+highlights); the focused retry
    # below regenerates the missing sections.
    if not (out.get("thesis_paragraph") or "").strip():
        log.warning("Gemini summary missing thesis_paragraph for %s", ticker)
        return None

    def _build_payload(raw: dict) -> dict:
        # Normalise highlights into 5-tuple-shaped dicts, preserving the
        # category order the prompt asked for. Bad inputs are dropped — the
        # templated `_derive_highlights` covers the missing slot.
        hl_in = raw.get("highlights") or []
        hl_out = []
        for it in hl_in[:5]:
            if isinstance(it, dict):
                cat = str(it.get("category") or "").strip().upper()
                body = str(it.get("body") or "").strip()
                if cat and body:
                    hl_out.append({"category": cat, "body": body})
        return {
            "thesis_paragraph": (raw.get("thesis_paragraph") or "").strip(),
            "catalysts":  list(raw.get("catalysts")  or [])[:3],
            "risks":      list(raw.get("risks")      or [])[:3],
            "watch_list": list(raw.get("watch_list") or [])[:3],
            "highlights": hl_out,
            "provider":   "gemini",
            "model":      "auto",
            "ticker":     ticker,
            "as_of":      datetime.now(timezone.utc).isoformat(),
            "context_hash": key,
        }

    payload = _build_payload(out)
    # Numeric-trace validator: drop any sentence / bullet that cites a
    # number we can't trace back to the context. Voice and bullet count
    # are the model's leeway — only hallucinated numbers are policed.
    payload = _validate_llm_output(payload, ctx)

    # FOCUSED RETRY — if the main (long, thinking-model) call truncated
    # before catalysts/risks/watch and they came back empty, re-request
    # JUST those with a short prompt so they're always real LLM output
    # rather than the deterministic fallback templates.
    _empty = [k for k in ("catalysts", "risks", "watch_list") if not payload.get(k)]
    if _empty:
        try:
            from src.providers.gemini import _call_gemini as _cg
            log.info("[llm] %s: focused retry for empty sections: %s", ticker, _empty)
            _r = _cg(_focused_sections_prompt(ctx, _empty), for_investment_view=True) or {}
            for k in _empty:
                vals = _r.get(k)
                if isinstance(vals, list) and vals:
                    payload[k] = [_strip_banned_qualifiers(str(x).strip())
                                  for x in vals[:3] if str(x).strip()]
        except Exception as exc:
            log.warning("[llm] %s: focused retry failed: %s", ticker, exc)

    # TIER-2 STRUCTURED VALIDATION — produces ValidationFinding records
    # alongside the scrubbed payload. The numeric-trace path above
    # silently rewrites the prose; tier-2 KEEPS a record of which
    # tokens were unmatched so the provenance.xlsx audit trail can
    # surface them to the analyst.
    try:
        from src.services.validation_layer import run_tier_2
        anchors = _allowed_numbers(ctx)
        v_rep = run_tier_2(
            ticker,
            thesis_paragraph=payload.get("thesis_paragraph"),
            allowed_numbers=anchors,
        )
        if v_rep.findings:
            payload["validation_tier2"] = v_rep.as_dict()
    except Exception as exc:
        log.debug("Tier-2 validation skipped: %s", exc)

    # TIER-3 QA REVIEW — deterministic quality flags (generic filler,
    # datapoint/template restatement, banned hedges, length, repetition).
    # Non-destructive: recorded for the audit trail, not deleted.
    try:
        qa = qa_review(payload)
        if qa:
            payload["qa_findings"] = qa
            log.warning("[qa] %s: %d quality flag(s): %s",
                        ticker, len(qa), "; ".join(qa[:6]))
    except Exception as exc:
        log.debug("QA review skipped: %s", exc)

    _write_cache(path, payload)
    return payload
