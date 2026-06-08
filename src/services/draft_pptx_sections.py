"""
Service: draft_pptx_sections

Runs Gemini *after* numbers/QA are finalized to draft short text blocks used in
the PPTX: investment thesis, what-to-watch, catalysts, and risks.

Hard rule: the LLM must not introduce or compute financial numbers.
"""

from __future__ import annotations

from src.models.step_result import Status, StepResult, StepTimer

STEP = "draft_pptx_sections"


def _safe_lines(xs, limit: int) -> list[str]:
    out: list[str] = []
    for x in xs or []:
        s = (str(x) if x is not None else "").strip()
        if not s:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _build_prompt(company_name: str, *, ticker: str = "", sector: str = "", quarter: str = "", memo_data: dict | None = None, headlines: list[str] | None = None) -> str:
    md = memo_data or {}
    header = md.get("header") or {}
    memo = md.get("memo_computed") or {}
    rec = header.get("recommendation") or {}
    rec = (rec.get("display_value") if isinstance(rec, dict) else rec) or ""

    facts: list[str] = []
    if quarter:
        facts.append(f"- Quarter: {quarter}")
    if sector:
        facts.append(f"- Sector/Industry: {sector}")
    if rec:
        facts.append(f"- Street consensus rating (verbatim): {rec}")

    # Direction-only signals — the LLM gets the *sign* of each delta but
    # not the magnitude, so it can ground catalysts/risks in real facts
    # without inventing numbers.
    def _direction(v, *, label_pos: str, label_neg: str, label_flat: str = "") -> str | None:
        try:
            x = float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
        if x is None:
            return None
        if x > 0.5:
            return label_pos
        if x < -0.5:
            return label_neg
        return label_flat or None

    rev_surp_dir = _direction(
        memo.get("avg_revenue_surprise_pct"),
        label_pos="Revenue surprise history: trending positive (beat history)",
        label_neg="Revenue surprise history: trending negative (miss history)",
    )
    eps_surp_dir = _direction(
        memo.get("avg_eps_surprise_pct"),
        label_pos="EPS surprise history: trending positive (beat history)",
        label_neg="EPS surprise history: trending negative (miss history)",
    )
    yoy_rev_dir = _direction(
        memo.get("yoy_revenue_pct_table") or memo.get("qoq_revenue_pct"),
        label_pos="Consensus revenue trajectory: YoY growth into the print",
        label_neg="Consensus revenue trajectory: YoY decline into the print (tougher comp)",
    )
    yoy_eps_dir = _direction(
        memo.get("yoy_eps_pct_table") or memo.get("qoq_eps_pct"),
        label_pos="Consensus EPS trajectory: YoY growth into the print",
        label_neg="Consensus EPS trajectory: YoY decline into the print",
    )
    spread_dir = _direction(
        memo.get("spread_pct"),
        label_pos="Street target implies upside vs current price",
        label_neg="Street target implies downside vs current price",
    )
    for line in (rev_surp_dir, eps_surp_dir, yoy_rev_dir, yoy_eps_dir, spread_dir):
        if line:
            facts.append(f"- {line}")

    # Estimate revisions — when scraped MS revisions are present, surface
    # the direction (analysts have been raising / cutting estimates).
    revs = md.get("estimate_revisions") or {}
    if isinstance(revs, dict):
        for label, key in (
            ("EPS revisions (3M direction)", "eps_3m_dir"),
            ("Revenue revisions (3M direction)", "revenue_3m_dir"),
        ):
            d = (revs.get(key) or "").strip()
            if d in ("up", "down"):
                facts.append(f"- {label}: {d}")

    fx = md.get("quality_flags") or []
    if isinstance(fx, list) and fx:
        facts.append("- Data-quality flags (FYI): " + "; ".join(_safe_lines(fx, 6)))

    h = _safe_lines(headlines, 6)
    headlines_block = "\n".join([f"- {x}" for x in h]) if h else "- (none provided)"

    return f"""You are a disciplined equity research analyst writing slide-ready text for an institutional earnings preview.

Company: {company_name}
Ticker: {ticker}

Context facts (the ONLY facts you may rely on):
{chr(10).join(facts) if facts else "- (no additional facts provided)"}

Recent headlines (use only as qualitative thematic context; do NOT quote them, do NOT name specific articles, indices, deals, or analysts):
{headlines_block}

ANTI-HALLUCINATION RULES (highest priority — violating any rule invalidates the output):
1. Do NOT invent or infer numbers, percentages, ratios, multiples, currency amounts, share counts, market caps, EPS, revenue, growth rates, target prices, upside, or dates. Numerical content is generated separately and any number you write WILL conflict with the deck.
2. Do NOT name specific events, M&A deals, share buybacks, dividend changes, regulatory actions, peer companies, indices, or analyst firms unless the exact phrase appears in the Context facts above. The Recent headlines block is theme-only — do NOT quote, paraphrase, or refer to any specific headline.
3. Do NOT use comparative claims that imply hidden numbers (e.g. "outperforming peers", "near multi-year highs", "trading at a premium / discount") unless directly stated in the Context facts.
4. Stay qualitative: focus on operating drivers, competitive position, demand backdrop, cost / margin levers, capital allocation posture, and execution risk — not on quantifying them.
5. Keep phrasing company-specific (avoid generic filler such as "market conditions", "macroeconomic factors").
6. Spell nothing with digits. No "Q1", "FY26", "Q4" — write "the upcoming quarter" or "this fiscal year" instead.

OUTPUT FORMAT:
- `investment_thesis`: EXACTLY FOUR sentences, no line breaks, ~100-130 words total.
   - S1: a qualitative stance on the setup into the print (constructive / cautious / balanced) anchored on the rating provided.
   - S2: three company-specific operating drivers that support the stance.
   - S3: what is most likely to move the stock on the print (qualitative).
   - S4: the single most material uncertainty and how it could flip the narrative.
- `what_to_watch`: array of EXACTLY 4 short bullets (each ≤ 6 words, no wrapping, no numbers). Operational items the print should clarify (e.g. "Volume mix and pricing power", "Margin trajectory through cycle").
- `catalysts`: array of EXACTLY 3 short bullets (each ≤ 5 words, no wrapping, no numbers). Each MUST anchor on either (a) a direction line in the Context facts above, OR (b) a theme present in the Recent headlines block. Do not invent catalysts that aren't grounded in those inputs. If you cannot find three, return an empty array.
- `risks`: array of EXACTLY 3 short bullets (each ≤ 5 words, no wrapping, no numbers). Same grounding rule as catalysts. Do not output generic placeholders like "Macro / demand downside" or "Execution risk" unless a direction line or headline supports them. If you cannot find three, return an empty array.

Output ONLY the JSON object — no preface, no trailing prose, no code fences.
"""


def run(company_name: str, *, ticker: str = "", sector: str = "", quarter: str = "", memo_data: dict | None = None, news_headlines: list[str] | None = None) -> StepResult:
    with StepTimer() as t:
        try:
            from src.providers.gemini import _call_gemini  # type: ignore

            prompt = _build_prompt(
                company_name,
                ticker=ticker,
                sector=sector,
                quarter=quarter,
                memo_data=memo_data,
                headlines=news_headlines,
            )
            out = _call_gemini(prompt, for_investment_view=True) or {}
            if not isinstance(out, dict):
                out = {}

            thesis = (out.get("investment_thesis") or "").strip()
            wtw = out.get("what_to_watch") if isinstance(out.get("what_to_watch"), list) else []
            cat = out.get("catalysts") if isinstance(out.get("catalysts"), list) else []
            risks = out.get("risks") if isinstance(out.get("risks"), list) else []

            sections = {
                "investment_thesis": thesis,
                "what_to_watch": _safe_lines(wtw, 4),
                "catalysts": _safe_lines(cat, 3),
                "risks": _safe_lines(risks, 3),
            }
            ok = bool(sections["investment_thesis"]) and len(sections["what_to_watch"]) == 4 and len(sections["catalysts"]) == 3 and len(sections["risks"]) == 3
            return StepResult(
                step_name=STEP,
                status=Status.SUCCESS if ok else Status.PARTIAL,
                source="gemini",
                message="Drafted PPTX sections" if ok else "Drafted PPTX sections (incomplete)",
                data=sections,
                elapsed_seconds=t.elapsed,
            )
        except Exception as exc:
            return StepResult(
                step_name=STEP,
                status=Status.FAILED,
                source="gemini",
                message="Gemini draft sections failed",
                error_detail=str(exc),
                elapsed_seconds=t.elapsed,
            )

