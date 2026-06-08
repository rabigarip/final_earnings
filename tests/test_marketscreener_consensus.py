"""
Tests for MarketScreener consensus fetch: homepage redirect detection and retry with sa. subdomain.
"""

from bs4 import BeautifulSoup

import pytest


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


class TestIsHomepage:
    """Detect when MarketScreener returns homepage instead of stock consensus page."""

    def test_www_canonical_homepage(self):
        from src.providers.marketscreener import _is_homepage
        soup = _soup('<link rel="canonical" href="https://www.marketscreener.com/">')
        assert _is_homepage(soup) is True

    def test_sa_canonical_homepage(self):
        from src.providers.marketscreener import _is_homepage
        soup = _soup('<link rel="canonical" href="https://sa.marketscreener.com/">')
        assert _is_homepage(soup) is True

    def test_consensus_page_not_homepage(self):
        from src.providers.marketscreener import _is_homepage
        soup = _soup(
            '<link rel="canonical" href="https://www.marketscreener.com/quote/stock/AL-RAJHI-BANKING-AND-INVE-6497957/consensus/">'
            '<title>Al Rajhi Banking: Target Price Consensus | MarketScreener</title>'
        )
        assert _is_homepage(soup) is False

    def test_generic_title_homepage(self):
        from src.providers.marketscreener import _is_homepage
        soup = _soup('<title>MarketScreener - Financial News &amp; Stock Market Quotes</title>')
        assert _is_homepage(soup) is True


class TestIsConsensusPage:
    """Consensus page must have analyst content and not be homepage."""

    def test_homepage_soup_not_consensus(self):
        from src.providers.marketscreener import _is_consensus_page
        soup = _soup(
            '<link rel="canonical" href="https://www.marketscreener.com/">'
            '<title>MarketScreener - Financial News</title>'
        )
        assert _is_consensus_page(soup) is False

    def test_consensus_content_is_consensus(self):
        from src.providers.marketscreener import _is_consensus_page
        soup = _soup(
            '<link rel="canonical" href="https://www.marketscreener.com/quote/stock/X/consensus/">'
            '<body>Number of Analysts 16 Mean consensus OUTPERFORM</body>'
        )
        assert _is_consensus_page(soup) is True
