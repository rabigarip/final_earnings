"""
ReportPayload — the full data contract between the pipeline and the
report generator. Every field is explicit. No global/shared mutable state;
each run gets a new payload. MarketScreener-derived data carries lineage.

Field-level provenance: optional SourcedValue for key fields so we do not
mix MarketScreener and Yahoo silently.
"""

from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field

from src.models.company    import CompanyMaster
from src.models.financials import DerivedMetrics, FinancialPeriod, QuoteSnapshot
from src.models.news       import NewsItem, NewsSummary


class MSLineage(BaseModel):
    """Source lineage for MarketScreener-derived data. Every MS block must reference this."""
    source_ticker: str = ""
    source_company_name: str = ""
    source_url: str = ""
    source_page_type: str = ""  # e.g. "consensus", "finances", "calendar"
    final_url: str = ""


class SourcedValue(BaseModel):
    """Provenance for a single field: value, source, source_page, etc."""
    value:          Any = None
    source:         str = ""           # "marketscreener" | "yahoo"
    source_page:    str = ""           # e.g. "/consensus/", "/calendar/"
    frequency:      str = ""           # "quarterly" | "annual" | "point"
    period_label:   str = ""           # e.g. "2026Q1", "FY2026"
    status:         str = ""           # "ok" | "missing" | "fallback"
    fallback_used:  bool = False
    warning:        str = ""


class ReportPayload(BaseModel):
    # Run metadata
    run_id:               str
    generated_at:         datetime
    mode:                 str = "preview"

    # Company
    company:              CompanyMaster

    # Market
    quote:                QuoteSnapshot | None     = None

    # Financials (default_factory to avoid mutable default)
    quarterly_actuals:    list[FinancialPeriod]     = Field(default_factory=list)
    annual_actuals:       list[FinancialPeriod]     = Field(default_factory=list)
    consensus_estimates:  list[FinancialPeriod]     = Field(default_factory=list)
    consensus_summary:    dict | None                = None  # Source: /consensus/

    # MarketScreener lineage (one per run; applied to all MS blocks when entity valid)
    ms_lineage:           MSLineage | None          = None

    # MarketScreener page-specific blocks (source labels required)
    ms_summary:                dict | None = None   # Source: /{SLUG}/ (consensus box, valuation snapshot)
    ms_annual_forecasts:       dict | None = None   # Source: /finances/
    ms_quarterly_forecasts:    dict | None = None   # Source: /finances/
    ms_eps_dividend_forecasts: dict | None = None   # Source: /valuation-dividend/
    ms_income_statement_actuals: dict | None = None # Source: /finances-income-statement/
    ms_valuation_multiples:    dict | None = None   # Source: /valuation/
    ms_calendar_events:        dict | None = None   # Source: /calendar/
    ms_quarterly_results_table: dict | None = None  # Source: /calendar/ (metrics-dict shape: quarters, metrics)

    # Additional MS pages added 2026-05 to close the PDF-vs-pipeline gap.
    # Each carries source_page lineage and may be None on entity-mismatch /
    # fetch failure. The deck builder treats these as optional sections.
    ms_ratings:                dict | None = None   # Source: /ratings/  (strengths, weaknesses, composite ratings, peer ESG)
    ms_sector_peers:           dict | None = None   # Source: /sector/   (peer comparison: multi-period % + USD market cap)
    ms_price_performance:      dict | None = None   # Source: /{SLUG}/   (perf grid, course extremes, recent quotes)
    ms_analyst_recommendations: dict | None = None  # Source: /consensus/ (recent broker actions, covering brokers)

    # Bloomberg manual export (dict-of-dataclasses as JSON). Present only when
    # data/bloomberg/<TICKER>_cons_q.xlsx and/or _FA.xlsx exist. Shape:
    #   {"ticker", "bbg_ticker", "company_name", "currency",
    #    "consensus_quarterly": [{period_label, period_end, is_estimate,
    #                             currency, metrics: {key: [mean, n_analysts]}}],
    #    "annuals": [{period_label, period_end, is_estimate, is_ltm,
    #                 currency, metrics: {key: value}}],
    #    "warnings": [...]}
    bloomberg_bundle:           dict | None = None

    # Derived
    derived:              DerivedMetrics | None     = None

    # News
    news_items:           list[NewsItem]            = Field(default_factory=list)
    news_summary:         NewsSummary | None        = None

    # Manual KPIs (from analyst memory layer)
    manual_kpis:          list[dict]                = Field(default_factory=list)

    # Audit
    step_results:         list[dict]                = Field(default_factory=list)
    duplicate_screening_log: list[dict]             = Field(default_factory=list)  # news dedupe
    # Recent-context retrieval QA
    recent_context_query_log: list[dict]           = Field(default_factory=list)
    recent_context_candidate_count: int            = 0
    recent_context_valid_count: int                = 0
    recent_context_rejected_reasons: list[str]     = Field(default_factory=list)
    # Enrichment and partial-status QA
    candidate_valid_basic: bool                    = False
    candidate_has_date_before_enrichment: int      = 0
    candidate_has_extracted_fact: int               = 0
    final_article_valid_count: int                = 0
    date_parse_attempted: int                     = 0
    date_parse_source: list[str]                  = Field(default_factory=list)
    date_parse_success: int                       = 0
    candidates_rejected_for_missing_date: int     = 0
    candidates_recovered_after_article_fetch: int  = 0
    recent_context_enrichment_log: list[dict]     = Field(default_factory=list)
    rejected_candidates_top_10: list[dict]       = Field(default_factory=list)
    recent_context_articles_qa: list[dict]       = Field(default_factory=list)

    # Memo-specific computed (for front-page memo only)
    memo_computed:        dict | None = None  # next_earnings_*, next_quarter_*, key_metrics, yoy_*, implied_upside

    # Optional field-level provenance (field_name -> SourcedValue)
    field_provenance:     dict[str, dict] | None = None  # serializable as SourcedValue.model_dump()

    # Appendix / section suppression (builder sets these)
    appendix_sections:    list[str] = Field(default_factory=lambda: ["annual_forecasts", "quarterly_detail", "eps_dividend", "valuation", "audit"])

    # MarketScreener availability (for QA and report honesty)
    # ok | wrong_entity | stale_url | source_redirect | unresolved
    marketscreener_availability: str = ""

    # Data-lineage QA (strict isolation and contamination checks)
    payload_source_ticker: str = ""
    payload_entity_match: bool = True
    cross_company_contamination_detected: bool = False
    identical_to_previous_ticker_payload: bool = False
    ms_payload_fingerprint: str = ""  # SHA256 of MS sections; used for persistence and contamination check
    # Section-level suppression reasons (better blank than wrong)
    ms_section_suppressed_due_to_missing_current_data: bool = False
    ms_section_suppressed_due_to_entity_mismatch: bool = False
    ms_section_suppressed_due_to_contamination: bool = False
    reused_default_payload_detected: bool = False

    # Flags
    has_consensus:        bool       = False
    has_news:             bool       = False
    warnings:             list[str]  = Field(default_factory=list)
