"""
Unit tests for recent-context pipeline: bank article ranking (tier + theme).
Ensures company-specific and Saudi banking themes rank above generic Gulf headlines.
"""

from datetime import datetime, timezone

import pytest

from src.models.news import NormalizedArticle, ValidationStatus
from src.services.recent_context_pipeline import (
    _bank_article_tier,
    _bank_theme_score,
    _ensure_extracted_fact_and_relevance,
    _rank_and_select,
)


def _article(headline: str, snippet: str = "", company_specific: bool = False, sector_relevant: bool = False):
    return NormalizedArticle(
        headline=headline,
        snippet=snippet,
        url="https://example.com/1",
        publisher="ZAWYA",
        publication_date=datetime.now(timezone.utc),
        validation_status=ValidationStatus.FINAL_VALID_MEDIUM,
        company_specific=company_specific,
        sector_relevant=sector_relevant,
    )


class TestBankArticleTier:
    """Tier order: 1=company, 2=Saudi banking, 3=Saudi+issuer, 4=Gulf."""

    def test_company_specific_ranks_tier1(self):
        a = _article("Al Rajhi Bank raises lending in Saudi", company_specific=True)
        assert _bank_article_tier(a, True, "SA", "Al Rajhi Bank") == 1

    def test_saudi_banking_sector_ranks_tier2(self):
        a = _article("Saudi bank credit growth slows", "deposits and margins", sector_relevant=True)
        assert _bank_article_tier(a, True, "SA", "Al Rajhi Bank") == 2

    def test_generic_gulf_ranks_tier4(self):
        a = _article("Most Gulf equities rise on oil", sector_relevant=False)
        assert _bank_article_tier(a, True, "SA", "Al Rajhi Bank") == 4

    def test_non_bank_ignores_tier(self):
        a = _article("SABIC earnings", company_specific=True)
        # For non-Saudi or non-bank, tier is not used in key; this just checks tier helper
        assert _bank_article_tier(a, False, "SA", "SABIC") == 4


class TestBankThemeScore:
    """Higher score = more relevant (Saudi credit, deposits, margins)."""

    def test_saudi_credit_scores_high(self):
        a = _article("Saudi bank credit growth", "lending")
        assert _bank_theme_score(a) >= 7

    def test_generic_gulf_scores_lower(self):
        a = _article("Mideast stocks", "Gulf equities")
        assert _bank_theme_score(a) <= 6


class TestRankAndSelectBanks:
    """For Saudi banks, company-specific and Saudi banking themes come first."""

    def test_company_specific_before_gulf(self):
        company = _article("Al Rajhi Bank Q4 lending", company_specific=True)
        gulf = _article("Most Gulf equities rise", snippet="Gulf bourses")
        ranked = _rank_and_select(
            [gulf, company],
            company_name="Al Rajhi Bank",
            is_bank=True,
            country="SA",
            max_n=5,
            min_n=1,
        )
        assert len(ranked) >= 1
        assert ranked[0].company_specific is True
        assert "Al Rajhi" in ranked[0].headline

    def test_saudi_banking_before_generic_gulf(self):
        saudi_bank = _article("Saudi banks deposits growth", "credit and margins", sector_relevant=True)
        gulf = _article("Gulf markets", "UAE leads")
        ranked = _rank_and_select(
            [gulf, saudi_bank],
            company_name="Al Rajhi Bank",
            is_bank=True,
            country="SAU",
            max_n=5,
            min_n=1,
        )
        assert len(ranked) >= 1
        # First should be Saudi banking theme (tier 2) not Gulf (tier 4)
        assert ranked[0].headline == saudi_bank.headline


class TestEnsureExtractedFactAndRelevance:
    """Selected articles must get extracted_fact and relevance_reason so IV can consume them."""

    def test_sets_extracted_fact_from_snippet(self):
        a = _article("Saudi bank credit growth", "Credit growth slowed in January. Deposits outpaced lending.")
        _ensure_extracted_fact_and_relevance(a, "Al Rajhi Bank", True, "SA")
        assert (a.extracted_fact or "").strip()
        assert "credit" in a.extracted_fact.lower() or "January" in a.extracted_fact.lower()
        assert a.relevance_reason in ("company_specific", "Saudi_banking_sector", "Saudi_market_issuer", "Gulf_market")

    def test_sets_relevance_reason_from_tier(self):
        a = _article("Al Rajhi Bank Q4 results", company_specific=True)
        _ensure_extracted_fact_and_relevance(a, "Al Rajhi Bank", True, "SA")
        assert a.relevance_reason == "company_specific"
        a2 = _article("Gulf equities rise", snippet="Most Gulf markets")
        _ensure_extracted_fact_and_relevance(a2, "Al Rajhi Bank", True, "SA")
        assert a2.relevance_reason == "Gulf_market"
