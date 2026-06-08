"""
ReportContext — single source of truth for every number, label, and series
that any slide of the earnings preview deck consumes.

Why this exists
---------------
The legacy `_write_preview_pptx_portrait` function in `generate_report.py`
is ~1,500 lines and computes its own values for the cover, the thesis
prose, the financial table, the cards, and the charts. Each of those
paths reads from a slightly different source — MS consensus box vs.
MS valuation grid vs. memo_computed vs. Yahoo info — and quietly drifts.
That's how the cover ends up showing UPSIDE −13.3 % while the thesis prose
two inches below says "implying −10.0 % upside".

ReportContext fixes this by collapsing all those reads into ONE typed
object built once per run, in `build_report_context.py`. Every slide
function takes `ctx: ReportContext` and only reads from it. If a number
is missing from the context, that slide MUST render "—", not invent a
fallback.

Migration plan
--------------
- Phase A (this commit): introduce the dataclass + builder. Nothing reads
  from it yet.
- Phase B: swap the cover slide to consume ReportContext only.
- Phase C: thesis & cards.
- Phase D: financial table & valuation cards.
- Phase E: charts.
- Phase F: delete the duplicated computation paths in generate_report.py.

Provenance
----------
Every field that can come from multiple providers carries a
`*_source` companion (e.g. `revenue_source: str`) so the deck footer
can declare "Estimates: Bloomberg" vs. "Estimates: MarketScreener".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Per-period rows (financial snapshot) ──────────────────────────────────

@dataclass(frozen=True)
class PeriodRow:
    """One column on the financial snapshot table.

    `is_estimate` distinguishes (A) vs (E) cells. `announcement_date` is
    the report-released date for actuals; None for estimates.
    """
    label: str                    # "FY2025", "Q1 2026", etc.
    is_estimate: bool
    announcement_date: Optional[str] = None  # ISO date when actual

    revenue:    Optional[float] = None
    ebitda:     Optional[float] = None        # None if MS does not publish
    ebit:       Optional[float] = None
    net_income: Optional[float] = None
    eps:        Optional[float] = None


# ── Cover (slide 1) ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class CoverData:
    """Everything the cover slide needs. No fallback logic in slide code."""
    company_name:   str
    ticker:         str
    sector:         str                       # already mapped per-exchange
    currency:       str                       # ISO-3, e.g. "SAR"
    market_cap:     Optional[float]           # in `currency`, raw units
    report_date:    Optional[str]             # ISO YYYY-MM-DD or None
    period_label:   str                       # "Q1 2026 Preview" / "FY2026 Preview"

    rating:         str                       # "OUTPERFORM" | "—"
    target_price:   Optional[float]
    upside_pct:     Optional[float]           # signed %, e.g. -13.3

    # Quick-stats strip (added 2026-05 for the redesigned cover). Each is
    # optional — the renderer drops the cell when None rather than
    # stretching others to fill the gap.
    last_close:     Optional[float] = None    # in `currency`, raw units
    n_analysts:     Optional[int] = None
    pe_fy_e:        Optional[float] = None    # forward P/E
    div_yield_pct:  Optional[float] = None    # already in % (e.g. 4.8)

    # Performance row + analyst highlights — fill the cover bottom with
    # real data rather than a generic "About this report" abstract.
    # Sourced from ms_price_performance and ms_ratings; the renderer
    # drops each row when its inputs are all None.
    perf_1d_pct:    Optional[float] = None
    perf_1w_pct:    Optional[float] = None
    perf_1m_pct:    Optional[float] = None
    perf_3m_pct:    Optional[float] = None
    perf_6m_pct:    Optional[float] = None
    perf_ytd_pct:   Optional[float] = None
    top_strengths:  list[str] = field(default_factory=list)

    # Provenance — used for the footer "Source: …" line.
    rating_source:        str = ""            # "marketscreener" | "yahoo" | ""
    target_price_source:  str = ""
    market_cap_source:    str = ""


# ── Slide 2: Executive Summary ────────────────────────────────────────────

@dataclass(frozen=True)
class SurpriseHistory:
    """For the thesis and the optional surprise summary box."""
    avg_revenue_surprise_pct: Optional[float] = None
    avg_eps_surprise_pct:     Optional[float] = None
    quarters_observed:        int = 0
    beat_count:               int = 0
    miss_count:               int = 0


@dataclass(frozen=True)
class KeyExpectationCard:
    """One card in the Key Expectations row. `delta_pct` drives sign-color."""
    label:        str                         # "Revenue", "EPS", "EBITDA Margin"
    value_str:    str                         # already formatted
    delta_pct:    Optional[float] = None      # raw signed %; None → no chip
    delta_str:    Optional[str] = None        # already formatted (with "+" or "−")
    unit:         str = ""                    # "SARM", "SAR", "" — rendered as
                                              # a small subscript under value


@dataclass(frozen=True)
class ChartSeries:
    """The Income Statement Evolution chart on slide 2."""
    periods:           list[str] = field(default_factory=list)
    revenue:           list[Optional[float]] = field(default_factory=list)
    ebit:              list[Optional[float]] = field(default_factory=list)
    net_income:        list[Optional[float]] = field(default_factory=list)
    actuals_boundary:  int = -1               # last index that is an actual
    source_label:      str = ""               # "MarketScreener" / "Bloomberg"


@dataclass(frozen=True)
class PEHistory:
    """For the P/E chart on slide 2."""
    periods:        list[str] = field(default_factory=list)
    pe_values:      list[Optional[float]] = field(default_factory=list)
    five_yr_avg:    Optional[float] = None


@dataclass(frozen=True)
class HeadlineRef:
    """One row in the Recent Headlines sidebar."""
    headline:    str
    date:        Optional[str] = None         # ISO YYYY-MM-DD
    source:      str = ""                     # "Reuters", "Argaam", "SCMP"
    url:         str = ""                     # for hover only; not rendered


@dataclass(frozen=True)
class SummaryData:
    """Everything slide 2 reads. Thesis is plain text — generated separately."""
    period_label:        str                  # same as cover, for sub-header
    company_name:        str

    # Investment Thesis prose. Up to 4 sentences. Generated by Gemini under
    # the strict "no invented numbers / facts" prompt; or the analytical
    # fallback when Gemini fails.
    thesis_text:         str
    thesis_source:       str                  # "gemini" | "analytical_fallback"

    surprise:            SurpriseHistory
    cards:               list[KeyExpectationCard]
    income_chart:        Optional[ChartSeries]
    pe_chart:            Optional[PEHistory]

    # Sidebar — bullets generated from news + estimate revisions, capped at 4.
    headlines:           list[HeadlineRef]

    what_to_watch:       list[str]            # 0–4 short bullets; [] hides section
    catalysts:           list[str]            # 0–3
    risks:               list[str]            # 0–3

    # Carry-forward state — set when MS has not yet published a forward
    # quarterly consensus (e.g. NBOB.OM in May 26 has only Q1 26A and no
    # Q2 26E forecast yet). The renderer flips the cards header from
    # "Key Expectations" to "Last Reported · {quarter}" and surfaces a
    # quality flag on slide 3 so a reader doesn't mistake released
    # actuals for an active consensus.
    consensus_unavailable:        bool = False
    last_reported_quarter_label:  str = ""


# ── Slide 3: Financial Snapshot ───────────────────────────────────────────

@dataclass(frozen=True)
class AnnualGrid:
    """Multi-period income-statement grid for slide 3.

    Mirrors what MarketScreener publishes on /finances/: a row per metric
    across 5–6 annual periods, with announcement_dates per period to
    distinguish actuals (A) from estimates (E). When MS lacks a row (e.g.
    EBITDA for a bank), the metric list is all-None and the renderer
    drops it. EPS is in display currency units; everything else in
    `units_label` (e.g. SARM).

    YoY change rows mirror the "Change" italic rows MS shows under each
    metric on /finances/ (PDF page 2). Each list is the same length as
    `periods` — the first entry is always None (no prior to compare),
    subsequent entries are signed % vs the previous period.
    """
    periods:             list[str]            # ["2023", "2024", "2025", "2026E", ...]
    announcement_dates:  list[Optional[str]]  # None for estimate columns
    revenue:             list[Optional[float]] = field(default_factory=list)
    ebitda:              list[Optional[float]] = field(default_factory=list)
    ebit:                list[Optional[float]] = field(default_factory=list)
    net_income:          list[Optional[float]] = field(default_factory=list)
    eps:                 list[Optional[float]] = field(default_factory=list)
    revenue_yoy:         list[Optional[float]] = field(default_factory=list)
    ebitda_yoy:          list[Optional[float]] = field(default_factory=list)
    ebit_yoy:            list[Optional[float]] = field(default_factory=list)
    net_income_yoy:      list[Optional[float]] = field(default_factory=list)
    eps_yoy:             list[Optional[float]] = field(default_factory=list)


@dataclass(frozen=True)
class FinancialTable:
    """The financial snapshot table on slide 3.

    Two shapes are supported:
      - `annual_grid`: 5–6 annual periods with full metric rows (preferred —
        matches the gold-standard deck and shows real multi-year context).
      - `rows`: legacy (prior, est) pair used when only a quarterly snapshot
        is available (e.g. a thin BBG bundle with no full annuals).

    The renderer prefers `annual_grid` when set; falls back to `rows`.
    """
    mode:           str                       # "quarterly" | "annual"
    rows:           list[PeriodRow]
    currency:       str                       # display currency, e.g. "SAR"
    units_label:    str                       # "(SARM)" — used on money rows
    units_label_per_share: str = ""           # "(SAR)"  — used on EPS row
    yoy_by_metric:  dict[str, Optional[float]] = field(default_factory=dict)
    annual_grid:    Optional[AnnualGrid] = None

    # Footer attribution.
    actuals_source:   str = "Yahoo Finance"   # or "Bloomberg" / "MarketScreener"
    estimates_source: str = "MarketScreener"
    estimates_as_of:  Optional[str] = None    # ISO date of consensus snapshot


@dataclass(frozen=True)
class ValuationSummary:
    pe_fy_e:      Optional[float] = None
    ev_ebitda:    Optional[float] = None      # None for banks/insurers
    pb:           Optional[float] = None
    div_yield:    Optional[float] = None      # already in % (e.g. 4.8)
    bank_disclaimer_needed: bool = False      # → footer star/footnote


@dataclass(frozen=True)
class PriceHistorySeries:
    dates:    list[str] = field(default_factory=list)   # ISO
    prices:   list[float] = field(default_factory=list)


@dataclass(frozen=True)
class FinancialSnapshotData:
    table:           FinancialTable
    valuation:       ValuationSummary
    price_history:   Optional[PriceHistorySeries]


# ── Slide 4 (NEW): Income Statement Evolution & Surprise ────────────────


@dataclass(frozen=True)
class QuarterlyIncomeSeries:
    """Quarterly income-statement chart series matching the MS /finances/
    Quarterly view (PDF reference page 2, second screenshot).

    Bar series: Sales / Operating Profit (EBIT) / Net Income.
    Line series: Net Margin and Operating Margin (computed in the builder).
    `actuals_boundary` is the index of the LAST actual quarter — the
    chart renderer uses this to apply the hatched/grey "estimate" pattern
    to forward periods.
    """
    periods:             list[str] = field(default_factory=list)
    revenue:             list[Optional[float]] = field(default_factory=list)
    ebit:                list[Optional[float]] = field(default_factory=list)
    net_income:          list[Optional[float]] = field(default_factory=list)
    operating_margin_pct: list[Optional[float]] = field(default_factory=list)
    net_margin_pct:      list[Optional[float]] = field(default_factory=list)
    actuals_boundary:    int = -1
    units_label:         str = ""


@dataclass(frozen=True)
class QuarterlySurpriseSeries:
    """Quarterly actual-vs-estimate series — the "Rate of Surprise" data
    from MS /calendar/ Quarterly results.

    The chart on slide 4 visualises the SALES line. The Net income series
    is surfaced as a summary chip alongside, since MS often publishes both
    on the same page (and the Net income story is frequently more nuanced
    than the Sales story — e.g. NBOB.OM 2025 Q4 had a Sales beat but a
    -1.93% Net income miss). EBIT is included for tickers where MS
    publishes it (industrials etc.).

    Each list aligns to `periods`. `*_surprise_pct` may differ from
    `(actual - estimate) / estimate * 100` when MS publishes a revised
    surprise figure; we surface MS's value when present.
    """
    periods:        list[str] = field(default_factory=list)
    # Sales (primary chart series)
    actual:         list[Optional[float]] = field(default_factory=list)
    estimate:       list[Optional[float]] = field(default_factory=list)
    surprise_pct:   list[Optional[float]] = field(default_factory=list)
    # Net income (summary chip + secondary series)
    net_income_actual:        list[Optional[float]] = field(default_factory=list)
    net_income_estimate:      list[Optional[float]] = field(default_factory=list)
    net_income_surprise_pct:  list[Optional[float]] = field(default_factory=list)
    # EBIT (sparse — surfaced when present, hidden otherwise)
    ebit_actual:        list[Optional[float]] = field(default_factory=list)
    ebit_estimate:      list[Optional[float]] = field(default_factory=list)
    ebit_surprise_pct:  list[Optional[float]] = field(default_factory=list)
    units_label:    str = ""


@dataclass(frozen=True)
class IncomeEvolutionData:
    """Slide-4 (new) payload — quarterly income statement chart + surprise
    chart, both sourced from MS. The slide is suppressed when neither
    series has enough data to render meaningfully (`has_data=False`)."""
    quarterly_income:    Optional[QuarterlyIncomeSeries] = None
    quarterly_surprise:  Optional[QuarterlySurpriseSeries] = None
    has_data:            bool = False


# ── Slide 4: Ratings & Sentiment ──────────────────────────────────────────

@dataclass(frozen=True)
class CompositeRating:
    """One Surperformance composite rating (Trader/Investor/Global/Quality).

    `score` is on 0-100. `score` may be None when MS hasn't computed the
    rating (thinly covered tickers); the renderer shows a dash bar.
    """
    label:  str
    score:  Optional[int] = None


@dataclass(frozen=True)
class RatingsData:
    """Slide 4 — Strengths/Weaknesses bullets + composite ratings strip.

    Sourced exclusively from `payload.ms_ratings` (which mirrors the MS
    /ratings/ page). Slide is suppressed when both bullet lists AND every
    composite score are missing. ESG MSCI letter (CCC..AAA / "-") is
    surfaced separately because MS lays it out as a letter, not a %.
    """
    strengths:        list[str] = field(default_factory=list)
    weaknesses:       list[str] = field(default_factory=list)
    composites:       list[CompositeRating] = field(default_factory=list)
    esg_msci:         Optional[str] = None
    # Peer-level ESG MSCI + Investor rating mini-table — used to fill the
    # bottom band of slide 5 with comparison context. Each entry shape:
    # {"name": str, "market_cap": str|None, "esg_msci": str|None,
    #  "rating_pct": int|None}
    peer_esg:         list[dict] = field(default_factory=list)
    has_data:         bool = False


# ── Slide 5: Sector Comparison ────────────────────────────────────────────

@dataclass(frozen=True)
class PeerRow:
    """One row in the peer table. Subject company is rendered first and
    visually highlighted by the renderer.
    """
    name:               str
    market_cap_usd:     Optional[str] = None  # MS-formatted, e.g. "1.14B"
    change_ytd_pct:     Optional[float] = None
    change_1y_pct:      Optional[float] = None
    change_3y_pct:      Optional[float] = None
    esg_msci:           Optional[str] = None  # from peer-ESG cross-reference
    is_subject:         bool = False


@dataclass(frozen=True)
class SectorComparisonData:
    """Slide 5 — peer comparison table.

    Pulled from `payload.ms_sector_peers` and joined with ESG letters from
    `payload.ms_ratings.peer_esg`. Capped at the top 12 peers by market
    cap to keep the slide readable; the subject company always appears
    even if it would fall below the cap.
    """
    sector_label:        str = ""
    rows:                list[PeerRow] = field(default_factory=list)
    average_ytd_pct:     Optional[float] = None  # MS-published "Average" row
    has_data:            bool = False


# ── Slide 6: Price Action & Broker Activity ──────────────────────────────

@dataclass(frozen=True)
class PerformanceCell:
    """One band in the performance grid (1d / 1w / MTD / 1m / 3m / 6m / YTD)."""
    label:     str
    value_pct: Optional[float] = None


@dataclass(frozen=True)
class CourseRange:
    """Low/high pair for one of the course-extreme buckets (1w/1m/YTD/1y/3y/5y)."""
    label:  str
    low:    Optional[float] = None
    high:   Optional[float] = None


@dataclass(frozen=True)
class BrokerAction:
    """One row in the recent broker actions list."""
    date:      str = ""
    headline:  str = ""
    source:    str = ""


@dataclass(frozen=True)
class PriceActionData:
    """Slide 6 — price-action grid + recent broker actions.

    Pulled from `payload.ms_price_performance` (perf grid + course extremes)
    and `payload.ms_analyst_recommendations` (broker actions + covering
    brokers list). Either source alone is enough to render the slide;
    each panel suppresses independently when its source is empty.
    """
    performance:        list[PerformanceCell] = field(default_factory=list)
    course_extremes:    list[CourseRange] = field(default_factory=list)
    broker_actions:     list[BrokerAction] = field(default_factory=list)
    covering_brokers:   list[str] = field(default_factory=list)
    # Recent quotes from MS — used as a fallback content source when MS
    # has no broker-actions for the ticker. Each entry: {date, price,
    # change_pct, volume}. Renderer drops the panel if both broker
    # actions AND recent quotes are empty.
    recent_quotes:      list[dict] = field(default_factory=list)
    has_data:           bool = False


# ── The whole context ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReportContext:
    """The root object. Every slide reads from this and ONLY this.

    Build once via `src/services/build_report_context.py::build()`. Pass
    immutably into per-slide render functions. If a value is missing,
    slide code must render the "—" sentinel; it must not invent a fallback.
    """
    # Identity / run metadata
    run_id:               str
    generated_at:         datetime
    ticker:               str
    company_name:         str
    currency:             str

    # Slide payloads
    cover:                CoverData
    summary:              SummaryData
    snapshot:             FinancialSnapshotData

    # Optional MS-extras slides (added 2026-05). Each carries a `has_data`
    # flag — the deck builder skips its slide entirely when that's False.
    income_evolution:     Optional[IncomeEvolutionData] = None
    ratings:              Optional[RatingsData] = None
    sector:               Optional[SectorComparisonData] = None
    price_action:         Optional[PriceActionData] = None

    # Quality flags surfaced on slide 3 footer (e.g. "MS captcha — estimates
    # from Yahoo only"). Empty list = no banner.
    quality_flags:        list[str] = field(default_factory=list)
