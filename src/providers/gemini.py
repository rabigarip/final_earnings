"""
Gemini LLM provider — used ONLY for:
  • Investment View generation (disciplined, evidence-based)
  • news summarisation & theme extraction
  • relevance classification

NEVER used for:
  • numeric truth (revenue, EPS, market cap)
  • ticker validation
  • primary reconciliation
  • guessing missing values

Model version is pinned in config/settings.toml for governance.
"""

from __future__ import annotations

import json
import logging
import os
import re

from src.config import cfg

log = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is not None:
        return _model

    import google.generativeai as genai

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "GEMINI_API_KEY not set. "
            "Copy .env.example → .env and fill in your key."
        )
    genai.configure(api_key=key)
    settings = cfg()
    model_name = settings["gemini"]["model"]
    # Try primary model, fall back to alternatives if deprecated
    _fallback_models = [model_name, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    for m in _fallback_models:
        try:
            _model = genai.GenerativeModel(m)
            if m != model_name:
                log.warning("Primary Gemini model '%s' unavailable, using fallback '%s'", model_name, m)
            return _model
        except Exception:
            continue
    # Last resort: use whatever was configured
    _model = genai.GenerativeModel(model_name)
    return _model


# ═══════════════════════════════════════════════════════════════════════════════
# Evidence brief builder
# ═══════════════════════════════════════════════════════════════════════════════

def _build_evidence_brief(
    company_name: str,
    memo_fact_pack: dict | None,
    articles: list[dict] | None,
) -> tuple[str, str]:
    """
    Build a structured evidence document from memo data and articles.
    Returns (evidence_text, data_density).
    data_density: "rich" (>=8 data points) | "moderate" (4–7) | "sparse" (<4).
    """
    fp = memo_fact_pack or {}
    sections: list[str] = []
    data_points = 0

    # ── Company identity ──
    identity = [f"COMPANY: {company_name}"]
    ticker = fp.get("ticker", "")
    if ticker:
        identity[0] += f" ({ticker})"
    sector = (fp.get("sector") or "").strip()
    industry = (fp.get("industry") or "").strip()
    if sector:
        identity.append(f"SECTOR: {sector}")
    if industry:
        identity.append(f"INDUSTRY: {industry}")
    is_bank = fp.get("is_bank", False)
    identity.append(f"TYPE: {'Bank / Financial institution' if is_bank else 'Industrial / Corporate'}")
    currency = fp.get("currency", "")
    if currency:
        identity.append(f"REPORTING CURRENCY: {currency}")
    sections.append("\n".join(identity))

    # ── Quarter context ──
    quarter_parts: list[str] = []
    pq = fp.get("preview_quarter_short")
    if pq:
        quarter_parts.append(f"PREVIEW QUARTER: {pq}")
        data_points += 1
    ned = fp.get("next_earnings_date")
    if ned:
        quarter_parts.append(f"EXPECTED EARNINGS DATE: {ned}")
        data_points += 1
    if quarter_parts:
        sections.append("\n".join(quarter_parts))

    # ── Consensus & valuation ──
    cons: list[str] = []
    rec = fp.get("consensus_recommendation")
    if rec is not None:
        try:
            rv = float(rec)
            if rv >= 4.5:
                label = "Strong Buy"
            elif rv >= 3.5:
                label = "Buy / Outperform"
            elif rv >= 2.5:
                label = "Hold"
            elif rv >= 1.5:
                label = "Underperform"
            else:
                label = "Sell"
            cons.append(f"Consensus recommendation: {label} ({rv:.1f}/5)")
        except (ValueError, TypeError):
            cons.append(f"Consensus recommendation: {rec}")
        data_points += 1

    ac = fp.get("consensus_analyst_count")
    if ac:
        cons.append(f"Analyst coverage: {ac} analysts")
        data_points += 1

    tp = fp.get("consensus_target_price")
    qp = fp.get("quote_price") or fp.get("consensus_last_close")
    if tp and qp:
        upside = fp.get("spread_pct") or fp.get("implied_upside_pct")
        line = f"Target price: {currency} {tp:,.1f} vs current {currency} {qp:,.1f}"
        if upside is not None:
            line += f" → implied upside {upside:+.1f}%"
        cons.append(line)
        data_points += 1
    elif tp:
        cons.append(f"Average target price: {currency} {tp:,.1f}")
        data_points += 1

    rev_cons = fp.get("next_quarter_consensus_revenue")
    eps_cons = fp.get("next_quarter_consensus_eps")
    if rev_cons is not None:
        cons.append(f"Revenue consensus ({pq or 'next Q'}): {currency} {rev_cons:,.0f}")
        data_points += 1
    if eps_cons is not None:
        cons.append(f"EPS consensus ({pq or 'next Q'}): {currency} {eps_cons:,.2f}")
        data_points += 1

    qoq_rev = fp.get("qoq_revenue_pct")
    yoy_rev = fp.get("yoy_revenue_pct_table")
    qoq_eps = fp.get("qoq_eps_pct")
    yoy_eps = fp.get("yoy_eps_pct_table")
    if qoq_rev is not None:
        cons.append(f"Consensus revenue QoQ: {qoq_rev:+.1f}%")
        data_points += 1
    if yoy_rev is not None:
        cons.append(f"Consensus revenue YoY: {yoy_rev:+.1f}%")
        data_points += 1
    if qoq_eps is not None:
        cons.append(f"Consensus EPS QoQ: {qoq_eps:+.1f}%")
    if yoy_eps is not None:
        cons.append(f"Consensus EPS YoY: {yoy_eps:+.1f}%")

    if cons:
        sections.append("CONSENSUS & VALUATION:\n" + "\n".join(f"- {c}" for c in cons))

    # ── Recent execution (beat/miss history) ──
    exec_parts: list[str] = []
    avg_rev_s = fp.get("avg_revenue_surprise_pct")
    avg_eps_s = fp.get("avg_eps_surprise_pct")
    consec = fp.get("consecutive_revenue_beats")
    rev_hist = fp.get("revenue_surprise_history") or []
    eps_hist = fp.get("eps_surprise_history") or []

    if avg_rev_s is not None:
        beat_count = sum(1 for e in rev_hist if (e.get("surprise_pct") or 0) >= 0)
        exec_parts.append(f"Avg revenue surprise: {avg_rev_s:+.1f}% (beats: {beat_count}/{len(rev_hist)})")
        data_points += 1
    if avg_eps_s is not None:
        beat_count = sum(1 for e in eps_hist if (e.get("surprise_pct") or 0) >= 0)
        exec_parts.append(f"Avg EPS surprise: {avg_eps_s:+.1f}% (beats: {beat_count}/{len(eps_hist)})")
        data_points += 1
    if consec is not None:
        exec_parts.append(f"Consecutive revenue beats: {consec}")
    if rev_hist:
        recent = rev_hist[-4:]
        exec_parts.append("Recent revenue surprises: " + ", ".join(
            f"{e.get('period', '?')}: {e.get('surprise_pct', 0):+.1f}%" for e in recent))
    if eps_hist:
        recent = eps_hist[-4:]
        exec_parts.append("Recent EPS surprises: " + ", ".join(
            f"{e.get('period', '?')}: {e.get('surprise_pct', 0):+.1f}%" for e in recent))

    if exec_parts:
        sections.append("RECENT EXECUTION (beat/miss history):\n" + "\n".join(f"- {e}" for e in exec_parts))

    # ── Recent news facts ──
    if articles:
        news_lines: list[str] = []
        for a in articles[:10]:
            idx = a.get("index", "?")
            src = a.get("source", "Unknown")
            date = a.get("date", "")
            headline = a.get("headline", "")
            snippet = (a.get("snippet") or "")[:200]
            line = f'[{idx}] "{headline}" ({src}, {date})'
            if snippet:
                line += f" — {snippet}"
            news_lines.append(line)
        sections.append("RECENT NEWS:\n" + "\n".join(news_lines))
        data_points += min(len(articles), 3)

    # ── Data gaps ──
    gaps: list[str] = []
    if not fp.get("next_earnings_date"):
        gaps.append("No confirmed earnings date")
    if rev_cons is None:
        gaps.append("No revenue consensus available")
    if eps_cons is None:
        gaps.append("No EPS consensus available")
    if not rev_hist:
        gaps.append("No beat/miss history available")
    if not fp.get("consensus_recommendation"):
        gaps.append("No consensus recommendation available")
    if not articles:
        gaps.append("No recent news articles available")
    if gaps:
        sections.append("DATA GAPS (do NOT fabricate these — acknowledge them):\n" + "\n".join(f"- {g}" for g in gaps))

    density = "rich" if data_points >= 8 else ("moderate" if data_points >= 4 else "sparse")
    return "\n\n".join(sections), density


# ═══════════════════════════════════════════════════════════════════════════════
# Post-generation validation (banned phrases, reaction markers from shared constants)
# ═══════════════════════════════════════════════════════════════════════════════

from src.constants.iv_quality import (
    BANNED_PHRASES_IV,
    UNSUPPORTED_CONFIDENCE_WORDS,
    REACTION_MARKERS,
    IV_MIN_TOTAL_WORDS,
    IV_MAX_TOTAL_WORDS,
    IV_MIN_PARAGRAPH_WORDS,
)


def _get_iv_word_bounds():
    """Word count bounds for IV validation; override from config if present."""
    try:
        s = cfg().get("gemini", {})
        return (
            s.get("iv_min_total_words", IV_MIN_TOTAL_WORDS),
            s.get("iv_max_total_words", IV_MAX_TOTAL_WORDS),
            s.get("iv_min_paragraph_words", IV_MIN_PARAGRAPH_WORDS),
        )
    except Exception:
        return IV_MIN_TOTAL_WORDS, IV_MAX_TOTAL_WORDS, IV_MIN_PARAGRAPH_WORDS


def _validate_iv_output(
    p1: str,
    p2: str,
    company_name: str,
    evidence_brief: str,
    data_density: str,
) -> tuple[bool, list[str]]:
    """
    Validate Investment View quality.
    Returns (is_valid, list_of_issues).
    """
    combined = f"{p1} {p2}".strip()
    if not combined:
        return False, ["empty output"]

    # Reject instruction/placeholder leakage (model returned prompt text instead of analyst content)
    lower = combined.lower()
    if "paragraph 1:" in lower and "stance + drivers" in lower and len(combined.split()) < 30:
        return False, ["output looks like instructions or placeholder, not analyst text"]
    if "paragraph 2:" in lower and "reaction driver + risk" in lower and len(combined.split()) < 30:
        return False, ["output looks like instructions or placeholder, not analyst text"]
    if "respond with only a json" in lower or "you are a disciplined equity research" in lower:
        return False, ["output contains prompt leakage; must be analyst text only"]

    issues: list[str] = []
    min_total, max_total, min_para = _get_iv_word_bounds()

    # 1. Banned generic phrases
    for phrase in BANNED_PHRASES_IV:
        if phrase.lower() in lower:
            issues.append(f"banned phrase: '{phrase}'")

    # 2. Unsupported confidence words without nearby quantitative backing
    for word in UNSUPPORTED_CONFIDENCE_WORDS:
        for m in re.finditer(rf"\b{word}\b", combined, re.I):
            ctx = combined[max(0, m.start() - 80):min(len(combined), m.end() + 80)]
            if not re.search(r"\d+\.?\d*\s*%|\d+(?:\.\d+)?[xX]", ctx):
                issues.append(f"unsupported confidence word '{word}' without quantitative context")
                break

    # 3. Word count (configurable bounds); each paragraph must be substantive
    wc = len(combined.split())
    w1, w2 = len((p1 or "").split()), len((p2 or "").split())
    if wc < min_total:
        issues.append(f"too short ({wc} words, need >={min_total})")
    elif wc > max_total:
        issues.append(f"too long ({wc} words, need <={max_total})")
    if (p1 and w1 < min_para) or (p2 and w2 < min_para):
        issues.append(f"each paragraph must be at least ~{min_para} words (substantive analyst text, not a label)")

    # 4. Company-specific content (>=3 identifiable terms)
    specific = 0
    name_tokens = {t.lower() for t in company_name.split()
                   if len(t) > 2 and t.lower() not in {
                       "the", "of", "and", "for", "in", "a", "an", "co", "inc",
                       "ltd", "corp", "plc", "group", "company", "corporation", "limited"}}
    if any(t in lower for t in name_tokens):
        specific += 1
    if re.search(r"\d{1,2}Q\d{2}", combined):
        specific += 1
    if re.search(r"(?:revenue|eps|net\s+income|earnings|net\s+sales)", lower):
        specific += 1
    if re.search(r"\d+\.?\d*\s*%", combined):
        specific += 1
    if re.search(r"(?:qoq|yoy|quarter[- ]over[- ]quarter|year[- ]over[- ]year)", lower):
        specific += 1
    if re.search(
        r"(?:margin|profitability|operating\s+income|EBIT|EBITDA|capacity|"
        r"utilization|ASP|volume|yield|NIM|NPL|cost[- ]to[- ]income|provisions|"
        r"impairment|backlog|occupancy|load\s+factor|ARPU|subscriber|"
        r"production|throughput|pricing|spread)", combined, re.I,
    ):
        specific += 1
    if specific < 3:
        issues.append(f"insufficient company-specific content ({specific} terms, need >=3)")

    # 5. Reaction-function sentence
    if not any(re.search(p, combined, re.I) for p in REACTION_MARKERS):
        issues.append("missing stock-reaction framing (what drives the stock this quarter)")

    # 6. Clear stance in paragraph 1
    stance_words = [
        "constructive", "cautious", "balanced", "negative", "neutral",
        "bullish", "bearish", "defensive", "optimistic", "skeptic",
        "positive", "weak", "challenged", "demanding", "low bar", "high bar",
        "into the print",
    ]
    if not any(s in p1.lower() for s in stance_words):
        issues.append("paragraph 1 lacks a clear stance word (constructive/cautious/balanced/etc.)")

    # 7. Evidence contradiction: if evidence shows negative growth but text sounds bullish
    if data_density != "sparse":
        ev_lower = evidence_brief.lower()
        has_negative_growth = bool(re.search(r"(?:qoq|yoy).*?-\d+\.?\d*%", ev_lower))
        uses_bullish = any(w in lower for w in ["strong growth", "accelerating", "outperformance", "beat expectations"])
        if has_negative_growth and uses_bullish:
            issues.append("tone mismatch: evidence shows negative growth but text uses bullish language without explaining why")

    return len(issues) == 0, issues


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt builders
# ═══════════════════════════════════════════════════════════════════════════════

def _sector_instruction(sector: str, industry: str, is_bank: bool) -> str:
    """Return strict sector-specific language rules so IV uses correct drivers (oil & gas vs telecom vs bank)."""
    s = (sector or "").lower()
    ind = (industry or "").lower()
    if is_bank:
        return "SECTOR: Banks. Use only bank-appropriate drivers: NIM, loan growth, asset quality, funding mix, capital. Do NOT use oil & gas (production, lifting costs) or telecom (ARPU, subscribers) or industrial (backlog, utilization) language."
    if "oil" in ind or "gas" in ind or "energy" in s or "exploration" in ind or "petroleum" in ind:
        return "SECTOR: Oil & Gas. Use ONLY oil & gas drivers: production volumes, realized oil/gas prices, lifting costs, capex, reserve replacement, field startup. Do NOT use: asset quality, demand and orders, backlog/utilization, NIM, subscribers, ARPU."
    if "telecom" in ind or "communication" in s:
        return "SECTOR: Telecom. Use ONLY telecom drivers: subscriber additions, ARPU, churn, capex intensity, wireless competition, enterprise/data centre. Do NOT use: asset quality (bank), production volumes (energy), backlog (industrial)."
    if "industrial" in s or "capital good" in ind or "aerospace" in ind or "machinery" in ind:
        return "SECTOR: Industrials. Use demand, orders, backlog, utilization, margin, guidance. Do NOT use asset quality (banks) or production volumes (energy)."
    return "SECTOR: General. Use only drivers appropriate to the company's sector and industry. Do NOT use bank-only language (e.g. asset quality) for non-banks, or oil & gas language for non-energy names."


def _build_main_prompt(company_name: str, evidence_brief: str, data_density: str, memo_fact_pack: dict | None = None) -> str:
    sparse_block = ""
    if data_density == "sparse":
        sparse_block = """
SPARSE DATA MODE — data is limited. You MUST:
- Explicitly acknowledge limited disclosure in your opening stance.
- Focus on scenario framing and reaction drivers, not point estimates.
- Include one sentence starting with "With limited pre-release disclosure…" or similar.
- Avoid false precision. Frame around what KIND of outcome matters.
"""
    fp = memo_fact_pack or {}
    sector_rule = _sector_instruction(
        fp.get("sector") or "",
        fp.get("industry") or "",
        fp.get("is_bank", False),
    )

    return f"""You are a disciplined equity research analyst writing the "Investment View" for an earnings preview memo on {company_name}. You write with incomplete data. You NEVER invent what you do not have.

{sector_rule}

═══ EVIDENCE BRIEF ═══
{evidence_brief}
═══ END EVIDENCE ═══

TASK: Write the Investment View as exactly 2 paragraphs, totalling 120–180 words.
{sparse_block}
REQUIRED STRUCTURE:

Paragraph 1 — Stance + Drivers (~80–100 words):
• Sentence 1: Clear stance into the print (constructive / cautious / balanced / negative / neutral) with a brief WHY grounded in the evidence.
• Sentences 2–3: 2–4 company-specific drivers for this quarter. Each MUST reference a concrete metric, business line, or operating concept from the evidence brief (e.g. consensus QoQ/YoY direction, beat history, a news fact). Do not list generic headings.

Paragraph 2 — Reaction driver + Risk (~40–80 words):
• Sentence 4: What will MOST LIKELY drive the stock reaction this quarter? Name the specific metric or outcome, not a generic list.
• Sentence 5: Key risk or what would INVALIDATE this view. Name a concrete scenario.
• If data is sparse, add one explicit uncertainty sentence acknowledging the limitation.

REASONING FRAMEWORK — apply internally before writing:
• FACT = directly stated in the evidence brief above.
• INFERENCE = reasonable interpretation from the evidence.
• UNKNOWN = not supported by the evidence.

Only FACT and INFERENCE may appear as statements.
UNKNOWN may ONLY appear as explicit uncertainty — NEVER as a disguised fact.

STRICT RULES:
1. Minimum 3 company-specific nouns, drivers, or operating concepts relevant to {company_name}.
2. Do NOT invent KPIs, consensus values, guidance, margins, management comments, product trends, or catalysts not in the evidence.
3. Do NOT say "supportive," "encouraging," "strong," or "improving" UNLESS the evidence quantitatively supports it (e.g. avg surprise > 0, YoY growth positive).
4. BANNED phrases — do NOT use:
   "investors will focus on key metrics" / "guidance will be closely watched" / "earnings quality matters" / "market participants will monitor" / "stock reaction will hinge on" / "a strong quarter would support the case" / "all eyes will be on"
5. Tone MUST match the evidence: weak numbers → do not sound bullish without explaining why weakness may be priced or non-recurring. Constructive setup → say so directly.
6. When citing a news article, include its index in brackets, e.g. [0].

SELF-CHECK — before returning, score 0–5 on:
1. Company specificity (3+ company-specific concepts?)
2. Consistency with evidence (every claim traceable?)
3. Clear stance (opening sentence unambiguous?)
4. Stock-reaction framing (what drives the stock?)
5. Honest uncertainty handling (admits what it doesn't know?)
If total < 20/25, rewrite once.

OUTPUT FORMAT: Return ONLY a JSON object. No markdown fences, no commentary. The values for investment_view_paragraph_1 and investment_view_paragraph_2 must be the actual analyst prose only (2–4 sentences each, 40+ words per paragraph). Never output placeholder labels like "Paragraph 1: Stance + drivers" or instruction text—only publishable analyst content suitable for the memo.
{{
  "themes": ["theme1", "theme2"],
  "overall_sentiment": "positive" | "negative" | "neutral" | "mixed",
  "key_items": ["most important observation"],
  "uncertainty_factors": ["key uncertainty"],
  "summary_text": "One-sentence summary.",
  "investment_view_paragraph_1": "Your first paragraph of analyst text here (stance and drivers).",
  "investment_view_paragraph_2": "Your second paragraph of analyst text here (reaction driver and risk).",
  "referenced_article_indices": [0],
  "citation_placements": [{{"paragraph": 1, "after_sentence": 1, "article_index": 0}}],
  "self_check_score": 22
}}

citation_placements: paragraph 1 or 2, after_sentence 0-based, article_index from RECENT NEWS. Cite at least one article when the evidence brief contains articles."""


def _build_retry_prompt(
    company_name: str,
    evidence_brief: str,
    data_density: str,
    original_p1: str,
    original_p2: str,
    issues: list[str],
) -> str:
    issues_text = "\n".join(f"- {i}" for i in issues)
    return f"""Your previous Investment View draft for {company_name} failed quality validation.

═══ EVIDENCE BRIEF ═══
{evidence_brief}
═══ END EVIDENCE ═══

YOUR PREVIOUS OUTPUT:
Paragraph 1: {original_p1}
Paragraph 2: {original_p2}

VALIDATION ISSUES:
{issues_text}

REWRITE both paragraphs to fix ALL issues above:
- Paragraph 1: clear stance + 2–4 company-specific drivers (80–100 words)
- Paragraph 2: what drives the stock + key risk (40–80 words)
- Total 120–180 words
- Use ONLY evidence from the brief
- Include 3+ company-specific terms
- Be specific about what drives the stock reaction
- Admit uncertainty where evidence is thin

Respond with ONLY a JSON object. Values must be the actual rewritten analyst text (no labels or instructions):
{{
  "investment_view_paragraph_1": "Your rewritten first paragraph text.",
  "investment_view_paragraph_2": "Your rewritten second paragraph text.",
  "referenced_article_indices": [],
  "citation_placements": []
}}"""


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini call helper
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from possibly wrapped text (fences, preamble, thinking, etc.)."""
    text = text.strip()
    if not text:
        return None

    # Strip markdown code fences (one or more blocks)
    fenced = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()

    # Direct parse
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass

    # Find the outermost { ... } block (handles preamble/postscript text)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidate = text[first:last + 1]
        try:
            out = json.loads(candidate)
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            # Try cleaning common issues: trailing commas, unescaped newlines
            cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                out = json.loads(cleaned)
                return out if isinstance(out, dict) else None
            except json.JSONDecodeError:
                pass

    return None


def _get_iv_model():
    """Return model for Investment View: use investment_view_model if set, else default."""
    settings = cfg()
    iv_model = (settings["gemini"].get("investment_view_model") or "").strip()
    if iv_model:
        import google.generativeai as genai
        import os
        key = os.environ.get("GEMINI_API_KEY", "")
        if key:
            genai.configure(api_key=key)
            return genai.GenerativeModel(iv_model)
    return _get_model()


def _call_gemini(prompt: str, for_investment_view: bool = True) -> dict | None:
    """Call Gemini and parse JSON response. Returns None on failure. Uses investment_view_model when set and for_investment_view=True."""
    settings = cfg()
    try:
        model = _get_iv_model() if for_investment_view else _get_model()
        resp = model.generate_content(
            prompt,
            generation_config={
                "max_output_tokens": settings["gemini"]["max_tokens"],
                "temperature": settings["gemini"]["temperature"],
            },
        )
        text = (resp.text or "").strip()
        if not text:
            log.warning("Gemini returned empty response")
            return None
        out = _extract_json(text)
        if out is None:
            log.warning("Gemini returned non-JSON response: %s…", text[:200])
        return out
    except EnvironmentError:
        raise
    except Exception as exc:
        log.warning("Gemini call failed: %s", exc, exc_info=True)
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def summarize_news(
    company_name: str,
    articles: list[dict] | None = None,
    headlines_fallback: list[str] | None = None,
    memo_fact_pack: dict | None = None,
) -> dict:
    """Send evidence to Gemini, get structured Investment View back."""
    if articles:
        return _summarize_with_evidence(company_name, articles, memo_fact_pack)
    if memo_fact_pack:
        return _summarize_with_evidence(company_name, None, memo_fact_pack)
    headlines = headlines_fallback or []
    if not headlines:
        return _empty_summary("No headlines or fact pack provided.")
    return _summarize_headlines_only(company_name, headlines)


def _summarize_with_evidence(
    company_name: str,
    articles: list[dict] | None,
    memo_fact_pack: dict | None,
) -> dict:
    """Primary path: evidence-based IV with validation and one retry."""
    evidence_brief, data_density = _build_evidence_brief(company_name, memo_fact_pack, articles)
    log.info("Evidence brief: %d chars, density=%s", len(evidence_brief), data_density)

    prompt = _build_main_prompt(company_name, evidence_brief, data_density, memo_fact_pack)
    out = _call_gemini(prompt)
    if not out:
        return _empty_summary("Gemini returned empty or unparseable response.")

    _normalize_summary_out(out)
    p1 = out.get("investment_view_paragraph_1", "")
    p2 = out.get("investment_view_paragraph_2", "")

    # Post-generation validation → retry once if issues found
    is_valid, issues = _validate_iv_output(p1, p2, company_name, evidence_brief, data_density)
    if not is_valid and p1 and p2:
        log.info("IV validation failed (%d issues), retrying: %s", len(issues), "; ".join(issues))
        retry_prompt = _build_retry_prompt(company_name, evidence_brief, data_density, p1, p2, issues)
        retry_out = _call_gemini(retry_prompt)
        if retry_out:
            rp1 = (retry_out.get("investment_view_paragraph_1") or "").strip()
            rp2 = (retry_out.get("investment_view_paragraph_2") or "").strip()
            if rp1 and rp2:
                _, issues2 = _validate_iv_output(rp1, rp2, company_name, evidence_brief, data_density)
                if len(issues2) < len(issues):
                    log.info("Retry improved: %d → %d issues", len(issues), len(issues2))
                    out["investment_view_paragraph_1"] = rp1
                    out["investment_view_paragraph_2"] = rp2
                    if retry_out.get("referenced_article_indices"):
                        out["referenced_article_indices"] = retry_out["referenced_article_indices"]
                    if retry_out.get("citation_placements"):
                        out["citation_placements"] = retry_out["citation_placements"]
                else:
                    log.info("Retry did not improve; keeping original")

    ref_indices = out.get("referenced_article_indices")
    out["referenced_article_indices"] = [int(x) for x in ref_indices] if isinstance(ref_indices, list) else []
    placements = out.get("citation_placements")
    out["citation_placements"] = placements if isinstance(placements, list) else []
    return out


def _summarize_headlines_only(company_name: str, headlines: list[str]) -> dict:
    """Fallback: headlines only — sparse-data aware prompt."""
    evidence_brief = (
        f"COMPANY: {company_name}\nTYPE: Unknown\n\n"
        "HEADLINES:\n" + "\n".join(f"- {h}" for h in headlines[:30]) +
        "\n\nDATA GAPS:\n"
        "- No financial data, consensus, or beat/miss history available\n"
        "- Only headlines provided — treat all claims as LOW confidence"
    )
    prompt = _build_main_prompt(company_name, evidence_brief, "sparse", None)
    out = _call_gemini(prompt)
    if not out:
        return _empty_summary("Gemini returned empty response.")
    _normalize_summary_out(out)
    out["referenced_article_indices"] = []
    out["citation_placements"] = []
    return out


# Placeholder/instruction text that must never appear in final memo (from our prompt schema or model leakage)
IV_PLACEHOLDER_OR_INSTRUCTION = [
    "paragraph 1: stance + drivers",
    "paragraph 2: reaction driver + risk",
    "paragraph 1: stance + drivers.",
    "paragraph 2: reaction driver + risk.",
    "respond with only a json",
    "no markdown fences",
    "no commentary",
    "you are a disciplined equity research analyst",
    "task: write the investment view",
    "required structure:",
    "self-check — before returning",
    "citation_placements:",
    "you must cite at least one article",
    "rewritten paragraph 1.",
    "rewritten paragraph 2.",
    "investment_view_paragraph_1",
    "investment_view_paragraph_2",
]


def _sanitize_iv_paragraph(text: str) -> str:
    """
    Remove instruction/placeholder leakage from Investment View paragraph.
    Returns clean, factual text only; empty string if content is only instructions.
    """
    if not text or not isinstance(text, str):
        return ""
    s = text.strip()
    if not s:
        return ""
    lower = s.lower()
    # Exact match to schema placeholders → treat as empty
    for ph in ["paragraph 1: stance + drivers.", "paragraph 2: reaction driver + risk.", "paragraph 1: stance + drivers", "paragraph 2: reaction driver + risk", "rewritten paragraph 1.", "rewritten paragraph 2."]:
        if lower == ph or lower.startswith(ph + " ") or lower == ph.rstrip("."):
            return ""
    # If the whole paragraph is just instruction-like (one of the placeholder phrases), empty
    if len(s.split()) <= 12 and any(ph in lower for ph in IV_PLACEHOLDER_OR_INSTRUCTION if len(ph) > 15):
        return ""
    # Strip leading "Paragraph 1:" / "Paragraph 2:" label; keep the rest if it's real content
    for prefix in (r"^paragraph\s+1\s*:\s*", r"^paragraph\s+2\s*:\s*"):
        m = re.match(prefix, s, re.I)
        if m:
            rest = s[m.end():].strip()
            rest_lower = rest.lower()
            if not rest:
                return ""
            if any(rest_lower == ph or rest_lower.startswith(ph + " ") for ph in ["stance + drivers", "reaction driver + risk", "rewritten paragraph"]):
                return ""
            s = rest
            break
    # Remove lines that are clearly instruction blocks (often model echoes the prompt)
    lines = s.split("\n")
    kept = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        line_lower = line_stripped.lower()
        if any(inst in line_lower for inst in ["respond with only", "no markdown", "you are a disciplined", "task:", "required structure", "self-check", "citation_placements:", "you must cite", "evidence brief", "end evidence", "strict rules", "reasoning framework"]):
            continue
        kept.append(line_stripped)
    s = " ".join(kept).strip()
    # If after stripping we're left with placeholder-only, return empty
    if not s or len(s) < 50:
        if any(ph in s.lower() for ph in IV_PLACEHOLDER_OR_INSTRUCTION):
            return ""
    return s


def _normalize_summary_out(out: dict) -> None:
    bullets = out.get("investment_view_bullets")
    if isinstance(bullets, list):
        out["investment_view_bullets"] = [str(b).strip() for b in bullets if b]
    else:
        out["investment_view_bullets"] = []
    p1 = out.get("investment_view_paragraph_1")
    p2 = out.get("investment_view_paragraph_2")
    p1 = (p1 and str(p1).strip()) or ""
    p2 = (p2 and str(p2).strip()) or ""
    # Strip any LLM instruction/placeholder leakage so memo is factual only
    p1 = _sanitize_iv_paragraph(p1)
    p2 = _sanitize_iv_paragraph(p2)
    out["investment_view_paragraph_1"] = p1
    out["investment_view_paragraph_2"] = p2


def _empty_summary(reason: str = "") -> dict:
    return {
        "themes": [],
        "overall_sentiment": "unknown",
        "key_items": [],
        "uncertainty_factors": [],
        "summary_text": reason or "",
        "investment_view_bullets": [],
        "investment_view_paragraph_1": "",
        "investment_view_paragraph_2": "",
        "referenced_article_indices": [],
        "citation_placements": [],
    }
