"""News data models: items, summaries, and normalized article schema for the context pipeline."""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from pydantic import BaseModel


# ═══════════════════════════════════════════════════════════════════════════════
# Core news models
# ═══════════════════════════════════════════════════════════════════════════════

class NewsItem(BaseModel):
    source:                str
    headline:              str
    url:                   str            = ""
    published_at:          datetime | None = None
    snippet:               str            = ""
    sentiment:             str | None     = None
    is_likely_stock_moving: bool          = False
    language:              str            = "en"
    extracted_fact:        str            = ""
    relevance_tag:         str            = ""


class ReferencedArticle(BaseModel):
    source:       str   = ""
    headline:     str   = ""
    published_at: datetime | None = None
    url:          str   = ""
    extracted_fact: str = ""


class NewsSummary(BaseModel):
    themes:                        list[str] = []
    overall_sentiment:             str       = "neutral"
    key_items:                     list[str] = []
    uncertainty_factors:          list[str] = []
    summary_text:                  str       = ""
    investment_view_bullets:       list[str] = []
    investment_view_paragraph_1:  str       = ""
    investment_view_paragraph_2:  str       = ""
    referenced_articles:           list[ReferencedArticle] = []
    citation_placements:           list[dict] = []


# ═══════════════════════════════════════════════════════════════════════════════
# Normalized article (merged from normalized_article.py)
# ═══════════════════════════════════════════════════════════════════════════════

class ValidationStatus(str, Enum):
    INVALID = "invalid"
    BASIC_VALID = "basic_valid"
    ENRICHED = "enriched"
    FINAL_VALID_HIGH = "final_valid_high"
    FINAL_VALID_MEDIUM = "final_valid_medium"
    FINAL_VALID_LOW = "final_valid_low"


class NormalizedArticle(BaseModel):
    """Single schema for all news/context sources."""
    headline: str = ""
    publisher: str = ""
    url: str = ""
    publication_date: datetime | None = None
    snippet: str = ""
    extracted_fact: str = ""
    relevance_reason: str = ""
    provider: str = ""
    relevance_tag: str = ""
    company_specific: bool = False
    sector_relevant: bool = False
    validation_status: ValidationStatus = ValidationStatus.INVALID
    date_source: str = ""
    date_confidence: str = ""
    date_from_search_card: bool = False

    def to_news_item_source(self) -> str:
        return (self.provider or "").strip().lower() or (self.publisher or "").strip().lower()[:20]

    def to_news_item(self) -> NewsItem:
        return NewsItem(
            source=self.to_news_item_source(),
            headline=self.headline or "",
            url=self.url or "",
            published_at=self.publication_date,
            snippet=self.snippet or "",
            extracted_fact=self.extracted_fact or "",
            relevance_tag=self.relevance_tag or "",
        )
