"""
Common interface for regional news providers.

Each source implements:
- candidate discovery (search or listing fetch)
- listing-page parsing → raw items {headline, url, date_str}
- article-page parsing (optional; for enrich)
- date extraction (listing snippet or article HTML)
- extracted_fact generation (from headline/snippet)
- relevance tagging (company vs sector)

Shared pipeline: retrieve → enrich → validate → dedupe → rank → render.
"""

from __future__ import annotations
import time
from abc import ABC, abstractmethod
from datetime import datetime

from src.models.news import NormalizedArticle
from src.providers.context.provider_helpers import (
    default_enrich_metadata,
    get_scraping_config,
    raw_items_to_articles,
    search_with_short_fallback,
)
from src.providers.context.provider_helpers import SourceRules


class RecentContextProvider(ABC):
    """Interface for news/context sources with provider-specific parsing and shared output."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique id for registry and config (e.g. 'reuters', 'zawya')."""
        ...

    # --- Source-specific rules (override for URL validity, exclusions, date policy) ---
    def get_source_rules(self) -> SourceRules:
        """Provider-specific: valid article URL patterns, section exclusion, date_confidence_policy."""
        return SourceRules()

    def is_valid_article_url(self, url: str) -> bool:
        """True if URL is a valid article link for this source (not section/category)."""
        return self.get_source_rules().is_valid_article_url(url or "")

    def should_exclude_url(self, url: str) -> bool:
        """True if URL should be excluded (section/category/tag pages)."""
        return self.get_source_rules().should_exclude_url(url or "")

    def generate_extracted_fact(self, headline: str, snippet: str) -> str:
        """Provider-specific: derive extracted_fact from headline/snippet. Default: first sentence of snippet or headline."""
        s = (snippet or "").strip()
        if s:
            first = s.split(".")[0].strip() + ("." if s.endswith(".") or not s.split(".")[0].endswith(".") else "")
            return first[:500] if first else ""
        return (headline or "").strip()[:300] or ""

    def tag_relevance(self, article: NormalizedArticle, company_name: str, is_bank: bool, country: str) -> str:
        """Provider-specific: set relevance_tag for ranking. Default: use existing tag or company/sector."""
        if article.relevance_tag:
            return article.relevance_tag
        if article.company_specific:
            return "company_specific"
        if article.sector_relevant:
            return "sector"
        return ""

    def extract_publication_date(self, url: str, html: str) -> tuple[datetime | None, str]:
        """Extract publication date from article page HTML. Returns (datetime or None, date_source)."""
        return None, ""

    # --- Discovery and listing (abstract or override) ---
    @abstractmethod
    def search_company_articles(
        self,
        company_name: str,
        *,
        since: datetime | None = None,
        max_items: int = 15,
        **kwargs: object,
    ) -> list[NormalizedArticle]:
        """Candidate discovery + listing parse for company; return normalized articles."""
        ...

    @abstractmethod
    def search_sector_articles(
        self,
        sector_queries: list[str],
        *,
        since: datetime | None = None,
        max_items_per_query: int = 10,
        **kwargs: object,
    ) -> list[NormalizedArticle]:
        """Candidate discovery + listing parse for sector; return normalized articles."""
        ...

    def enrich_metadata(self, article: NormalizedArticle) -> NormalizedArticle:
        """Optionally fetch article page to fill publication_date, snippet, extracted_fact."""
        return article


class SearchBasedContextProvider(RecentContextProvider):
    """
    Base for providers that use _search(query, max_items) -> list[{headline, url, date_str}].
    Subclass defines: provider_id, publisher, _search(), and optionally get_source_rules().
    """

    @property
    @abstractmethod
    def publisher(self) -> str:
        """Display name e.g. 'Business Standard'."""
        ...

    def _user_agent(self) -> str:
        return get_scraping_config()[0]

    def _timeout(self) -> int:
        return get_scraping_config()[1]

    @abstractmethod
    def _search(self, query: str, max_items: int) -> list[dict]:
        """Listing-page fetch + parse; return list of {headline, url, date_str}."""
        ...

    def search_company_articles(
        self,
        company_name: str,
        *,
        since: datetime | None = None,
        max_items: int = 15,
        **kwargs: object,
    ) -> list[NormalizedArticle]:
        raw = search_with_short_fallback(self._search, company_name or "", max_items)
        return raw_items_to_articles(raw, self.publisher, self.provider_id, company_specific=True, sector_relevant=False)

    def search_sector_articles(
        self,
        sector_queries: list[str],
        *,
        since: datetime | None = None,
        max_items_per_query: int = 10,
        **kwargs: object,
    ) -> list[NormalizedArticle]:
        out: list[NormalizedArticle] = []
        for q in (sector_queries or [])[:5]:
            raw = self._search(q, max_items_per_query)
            out.extend(raw_items_to_articles(raw, self.publisher, self.provider_id, company_specific=False, sector_relevant=True))
            time.sleep(0.5)
        return out

    def enrich_metadata(self, article: NormalizedArticle) -> NormalizedArticle:
        return default_enrich_metadata(article, self._user_agent(), self._timeout())
