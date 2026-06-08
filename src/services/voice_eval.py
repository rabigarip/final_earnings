"""Voice eval harness — score a generated thesis paragraph against
the Apple / JPMorgan / Tesla references.

Three scoring axes, each yielding 0–1:

  1. STRUCTURAL — sentence count, word count, S1-S4 markers
  2. LEXICAL    — qualitative-vs-quantitative balance vs references
  3. STYLE      — presence of the four "setup" phrases

Composite score is a weighted average. Threshold for "matches voice"
is 0.70; below that, the prompt needs more tuning.

This is deliberately heuristic — no LLM-as-judge. The user wanted
something that runs free, fast, and deterministic so it can ship as
a pre-deck check and a CI regression test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─────────────────────── REFERENCE EXAMPLES ─────────────────

REFERENCES: dict[str, str] = {
    "apple": (
        "Apple enters earnings with focus on iPhone demand, Services "
        "growth, gross margin, and next-quarter guidance. Recent "
        "performance has been supported by AI upgrade expectations, "
        "while China demand and hardware replacement cycles remain "
        "key concerns. Investors should watch iPhone revenue, Services "
        "growth, gross margin, Greater China performance, and "
        "commentary on AI integration and capital returns. The setup "
        "appears balanced, as valuation remains elevated and "
        "expectations are already fairly high."
    ),
    "jpmorgan": (
        "JPMorgan enters earnings with focus on net interest income, "
        "loan growth, credit quality, and capital returns. Recent "
        "performance has been supported by resilient U.S. economic "
        "data and stronger banking sentiment, while investors remain "
        "focused on whether net interest income has peaked. Key "
        "metrics to watch include NII, provision expense, loan growth, "
        "deposit trends, investment banking fees, and management "
        "commentary on credit and capital deployment. The setup "
        "appears cautiously attractive, supported by strong "
        "profitability and balance sheet strength."
    ),
    "tesla": (
        "Tesla enters earnings with focus on vehicle deliveries, "
        "automotive gross margin, pricing strategy, and demand "
        "outlook. Recent performance has been driven by expectations "
        "around autonomous driving, robotaxi developments, and future "
        "product launches, while margin pressure and softer EV demand "
        "remain concerns. Investors should watch automotive revenue, "
        "gross margin excluding credits, free cash flow, delivery "
        "outlook, energy storage growth, and commentary on pricing "
        "and autonomy. The setup appears high-risk, high-reward "
        "given the stock's reliance on future growth narratives."
    ),
}


# ─────────────────────── METRICS ─────────────────────────────

@dataclass
class VoiceEvalResult:
    ticker: str
    word_count: int
    sentence_count: int
    has_opening_marker: bool        # "enters earnings with focus on"
    has_drivers_marker: bool        # "recent performance" / "supported by"
    has_watch_marker: bool          # "investors should watch" / "key metrics to watch"
    has_setup_marker: bool          # "the setup appears"
    setup_label: Optional[str]      # "balanced" / "cautiously attractive" / etc.
    qualitative_ratio: float        # qualitative words / total words
    numeric_density: float          # numeric tokens / total words
    structural_score: float         # 0-1
    lexical_score: float            # 0-1
    style_score: float              # 0-1
    composite_score: float          # 0-1
    grade: str                      # "matches" / "close" / "diverges"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# Target band derived from the references (Apple/JPM/Tesla each ~80 words,
# 4 sentences). 60-130 words gets full structural credit; outside that
# we taper.
_REF_WORD_BAND = (60, 130)
_REF_SENTENCE_BAND = (3, 5)

_SETUP_PHRASES = (
    "balanced",
    "cautiously attractive",
    "asymmetric",
    "high-risk, high-reward",
    "high-risk high-reward",
    "constructive",
    "challenged",
)

# Qualitative vocabulary that appears in the references. Their thesis
# voice is qualitative-heavy; lexical score boosts when the generated
# paragraph uses these registers.
_QUALITATIVE_TOKENS = {
    # Drivers / pressures
    "supported", "driven", "drove", "remain", "remains", "focus",
    "concerns", "concern", "expectations", "outlook", "guidance",
    "commentary", "trajectory", "headwinds", "tailwinds", "pressure",
    "pressures", "constructive", "cautious", "elevated", "stretched",
    # Operational levers
    "margin", "growth", "demand", "pricing", "deliveries", "income",
    "quality", "returns", "capital", "earnings", "consensus",
    # Setup phrases / verdict words
    "balanced", "attractive", "risky", "rewarding", "high-risk",
    "high-reward", "asymmetric", "premium", "discount", "fairly",
    "modestly", "structurally", "cyclical",
}

# Numeric token pattern — percentages, multiples, money, bare floats.
_NUM_RE = re.compile(
    r"(?<![A-Za-z\d])-?\d+(?:\.\d+)?(?:%|x|X|B|M|K)?(?![A-Za-z])")

# Sentence splitter — naive but reliable on the reference rhythm.
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _structural(text: str, res: VoiceEvalResult) -> None:
    """Sentence/word count + opener / driver / watch / setup markers."""
    words = re.findall(r"[A-Za-z\-']+", text)
    res.word_count = len(words)
    sentences = [s.strip() for s in _SENT_RE.split(text.strip()) if s.strip()]
    res.sentence_count = len(sentences)

    # Opener may now carry the quarter, e.g. "enters Q2 2026 earnings with
    # focus on" — match with an optional period token between the words.
    res.has_opening_marker = bool(
        re.search(r"enters(?:\s+[\w/]+){0,4}\s+earnings with focus on", text.lower()))
    drivers_phrases = ("supported by", "driven by", "recent performance has been")
    res.has_drivers_marker = any(p in text.lower() for p in drivers_phrases)
    watch_phrases = ("investors should watch", "key metrics to watch",
                     "metrics to watch include")
    res.has_watch_marker = any(p in text.lower() for p in watch_phrases)
    res.has_setup_marker = "the setup appears" in text.lower() \
                              or "the setup looks" in text.lower()

    # Identify which setup label the LLM chose.
    setup_label = None
    if res.has_setup_marker:
        for phrase in _SETUP_PHRASES:
            if phrase in text.lower():
                setup_label = phrase
                break
    res.setup_label = setup_label

    # Score: word count → 1 if in band, taper outside.
    wc_score = 1.0
    if not (_REF_WORD_BAND[0] <= res.word_count <= _REF_WORD_BAND[1]):
        # Linear taper at 50% of band edge.
        far = min(abs(res.word_count - _REF_WORD_BAND[0]),
                   abs(res.word_count - _REF_WORD_BAND[1]))
        wc_score = max(0.0, 1.0 - far / 40)
    sc_score = 1.0
    if not (_REF_SENTENCE_BAND[0] <= res.sentence_count <= _REF_SENTENCE_BAND[1]):
        far = min(abs(res.sentence_count - _REF_SENTENCE_BAND[0]),
                   abs(res.sentence_count - _REF_SENTENCE_BAND[1]))
        sc_score = max(0.0, 1.0 - far / 3)
    # FOUR-sentence exec summary: opener (S1), drivers (S2), watch (S3),
    # setup verdict (S4) — all four are required markers.
    marker_bits = (
        res.has_opening_marker, res.has_drivers_marker,
        res.has_watch_marker, res.has_setup_marker,
    )
    marker_score = sum(1 for b in marker_bits if b) / 4

    res.structural_score = round(
        0.30 * wc_score + 0.20 * sc_score + 0.50 * marker_score, 3,
    )
    if not res.has_opening_marker:
        res.notes.append(
            "Missing opener 'enters … earnings with focus on …' — S1 marker")
    if not res.has_drivers_marker:
        res.notes.append("Missing drivers marker (S2)")
    if not res.has_watch_marker:
        res.notes.append("Missing watch-list marker (S3)")
    if not res.has_setup_marker:
        res.notes.append("Missing setup-verdict marker (S4)")


def _lexical(text: str, res: VoiceEvalResult) -> None:
    """Qualitative-vs-quantitative balance. References use almost no
    numbers — generated thesis should match that profile."""
    words = re.findall(r"[A-Za-z\-']+", text.lower())
    n_words = max(len(words), 1)
    qual = sum(1 for w in words if w in _QUALITATIVE_TOKENS)
    res.qualitative_ratio = round(qual / n_words, 3)
    nums = _NUM_RE.findall(text)
    res.numeric_density = round(len(nums) / n_words, 3)

    # Reference profiles average qualitative_ratio ≈ 0.10-0.18 and
    # numeric_density ≈ 0.0-0.03. Score against these targets.
    target_qual_min, target_qual_max = 0.08, 0.22
    target_num_max = 0.06

    if target_qual_min <= res.qualitative_ratio <= target_qual_max:
        qual_score = 1.0
    elif res.qualitative_ratio < target_qual_min:
        qual_score = res.qualitative_ratio / target_qual_min
    else:
        qual_score = max(0.0, 1.0 - (res.qualitative_ratio - target_qual_max) / 0.10)

    if res.numeric_density <= target_num_max:
        num_score = 1.0
    else:
        num_score = max(0.0, 1.0 - (res.numeric_density - target_num_max) / 0.05)

    res.lexical_score = round(0.6 * qual_score + 0.4 * num_score, 3)
    if res.numeric_density > target_num_max:
        res.notes.append(
            f"Numeric density {res.numeric_density:.2%} above target "
            f"{target_num_max:.0%} — voice is too quantitative")
    if res.qualitative_ratio < target_qual_min:
        res.notes.append(
            f"Qualitative vocabulary ratio {res.qualitative_ratio:.2%} "
            f"below target {target_qual_min:.0%} — voice may read flat")


def _style(text: str, res: VoiceEvalResult) -> None:
    """Reward presence of a chosen 'setup appears X' label from the
    canonical list. References each picked one — balanced / cautiously
    attractive / high-risk, high-reward. Bonus for stopping right
    after the setup line (no scenario coda)."""
    text_low = text.lower()
    if res.setup_label:
        label_score = 1.0
    else:
        label_score = 0.0
    # Penalize a trailing "would" / "scenario" sentence.
    coda_phrases = (
        "would confirm", "would suggest", "would trigger",
        "in our view", "in our base case", "would shift",
        "our view would",
    )
    has_coda = any(p in text_low for p in coda_phrases)
    coda_score = 0.0 if has_coda else 1.0

    res.style_score = round(0.6 * label_score + 0.4 * coda_score, 3)
    if has_coda:
        res.notes.append(
            "Scenario coda detected after S4 — references stop at 'The setup appears …'")


def _grade(score: float) -> str:
    if score >= 0.75: return "matches"
    if score >= 0.55: return "close"
    return "diverges"


def evaluate(ticker: str, thesis: str) -> VoiceEvalResult:
    """Score a generated thesis paragraph. Single public entry point."""
    res = VoiceEvalResult(
        ticker=ticker, word_count=0, sentence_count=0,
        has_opening_marker=False, has_drivers_marker=False,
        has_watch_marker=False, has_setup_marker=False,
        setup_label=None, qualitative_ratio=0.0, numeric_density=0.0,
        structural_score=0.0, lexical_score=0.0, style_score=0.0,
        composite_score=0.0, grade="diverges",
    )
    if not thesis:
        res.notes.append("Empty thesis paragraph")
        res.grade = _grade(0.0)
        return res

    _structural(thesis, res)
    _lexical(thesis, res)
    _style(thesis, res)
    # Composite weighting: structural carries most (it's the framework
    # of the voice); lexical second (the register); style third (a
    # specific stylistic choice). All weights ≥ 0 and sum to 1.
    res.composite_score = round(
        0.45 * res.structural_score
        + 0.35 * res.lexical_score
        + 0.20 * res.style_score, 3,
    )
    res.grade = _grade(res.composite_score)
    return res


def evaluate_references() -> dict[str, VoiceEvalResult]:
    """Score the three reference examples themselves — used for
    calibration. Each should score 'matches' (>= 0.75); if a
    threshold tweak makes a reference fall below, the threshold
    is wrong."""
    return {name: evaluate(name, text) for name, text in REFERENCES.items()}


def evaluate_batch(decks: list[tuple[str, str]]) -> dict:
    """Score multiple thesis paragraphs and return aggregate stats.
    `decks` is a list of (ticker, thesis_text) tuples."""
    results = {ticker: evaluate(ticker, text) for ticker, text in decks}
    if not results:
        return {"results": {}, "aggregate": {}}
    n = len(results)
    avg_composite = sum(r.composite_score for r in results.values()) / n
    n_matches = sum(1 for r in results.values() if r.grade == "matches")
    n_close = sum(1 for r in results.values() if r.grade == "close")
    n_diverges = sum(1 for r in results.values() if r.grade == "diverges")
    return {
        "results": {t: r.as_dict() for t, r in results.items()},
        "aggregate": {
            "n_decks": n,
            "avg_composite": round(avg_composite, 3),
            "n_matches": n_matches,
            "n_close": n_close,
            "n_diverges": n_diverges,
        },
    }
