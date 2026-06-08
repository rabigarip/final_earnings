"""
Investment View (IV) quality constants — single source of truth for validation and guardrails.

Used by:
- src.providers.gemini: validation and prompt (banned phrases, confidence words, reaction markers)
- src.services.qa_engine: guardrail_paragraphs (banned + unsupported patterns)
"""

from __future__ import annotations

import re

# ─── Banned phrases (no generic boilerplate in IV) ───────────────────────────
# Exact substring match (case-insensitive) in Gemini validator.
BANNED_PHRASES_IV = [
    "investors will focus on key metrics",
    "guidance will be closely watched",
    "earnings quality matters",
    "market participants will monitor",
    "investors will monitor",
    "stock reaction will hinge on",
    "a strong quarter would support the case",
    "investors will focus on",
    "the market will closely watch",
    "key metrics will be in focus",
    "all eyes will be on",
]

# Regex patterns for guardrail (qa_engine): strip sentences matching these.
BANNED_IV_PHRASES_REGEX = [
    r"investors\s+will\s+focus\s+on\s+key\s+metrics",
    r"guidance\s+will\s+be\s+closely\s+watched",
    r"earnings\s+quality\s+matters",
    r"market\s+participants\s+will\s+monitor",
    r"investors\s+will\s+monitor",
    r"stock\s+reaction\s+will\s+hinge\s+on",
    r"a\s+strong\s+quarter\s+would\s+support\s+the\s+case",
    r"all\s+eyes\s+will\s+be\s+on",
    r"key\s+metrics\s+will\s+be\s+in\s+focus",
    r"the\s+market\s+will\s+closely\s+watch",
]

# Additional guardrail-only patterns (content we don't want in IV sentences).
UNSUPPORTED_CONTENT_PATTERNS = [
    r"\bvision\s+2030\b",
    r"\boil\s+price",
    r"\bmacro\s+environment",
    r"\bmortgage\s+growth",
    r"\bsme\s+momentum",
    r"\bpremium\s+valuation",
    r"\bdominant\s+franchise",
    r"\bdigital\s+leadership",
    r"\bmanagement\s+confidence",
    r"\bstrategy\s+shift",
    r"\bguidance\s+(?:raise|cut|maintain)",
    r"\boutlook\s+for\s+the\s+year",
    r"\bcompetitive\s+position",
    r"\bmarket\s+share\s+gain",
]

# Combined for qa_engine guardrail (banned + unsupported).
def get_guardrail_combined_regex():
    combined = BANNED_IV_PHRASES_REGEX + UNSUPPORTED_CONTENT_PATTERNS
    return re.compile("|".join(f"(?:{p})" for p in combined), re.I)


# ─── Confidence words (require quantitative context in validator) ─────────────
UNSUPPORTED_CONFIDENCE_WORDS = [
    "supportive", "encouraging", "robust", "stellar",
    "impressive", "outstanding", "excellent", "remarkable",
]

# ─── Reaction framing (validator requires at least one) ──────────────────────
REACTION_MARKERS = [
    r"stock\s+(reaction|move|response|price)",
    r"share\s+price",
    r"market\s+(reaction|response)",
    r"(drive|determine|shape)\s+(the\s+)?(stock|share|trading)",
    r"sentiment",
    r"re-?rat(e|ing)",
    r"matter(s)?\s+for\s+the\s+stock",
    r"(clean|weak|strong)er?[\s-]+than[\s-]+(feared|expected|hoped)",
    r"(up|down)side\s+(risk|surprise|from\s+here)",
]

# ─── Word count bounds (can be overridden by config) ────────────────────────
IV_MIN_TOTAL_WORDS = 80
IV_MAX_TOTAL_WORDS = 280
IV_MIN_PARAGRAPH_WORDS = 25
