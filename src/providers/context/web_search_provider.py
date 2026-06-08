"""Configurable web search provider — consolidates Business Standard, Economic Times,
Business Day, Daily Investor, and Moneyweb into a single data-driven class.

Each instance is configured with domain, search URL template(s), and exclude paths.
Search flow: native site search → Google site: search fallback.
"""

from __future__ import annotations
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from src.providers.context.base import SearchBasedContextProvider
from src.providers.context.search_utils import (
    fetch_search_page,
    extract_article_items,
    fetch_google_site_search,
)
from src.providers.context.provider_helpers import SourceRules, default_search_rules


class WebSearchProvider(SearchBasedContextProvider):
    """Generic search-based news provider driven by a config dict."""

    def __init__(self, config: dict):
        self._config = config

    @property
    def provider_id(self) -> str:
        return self._config["provider_id"]

    @property
    def publisher(self) -> str:
        return self._config["publisher"]

    def get_source_rules(self) -> SourceRules:
        return default_search_rules(
            self._config["domain"],
            self._config["base_domain"],
            exclude_paths=tuple(self._config.get("exclude_paths", ())),
        )

    def _search(self, query: str, max_items: int) -> list[dict]:
        ua, timeout = self._user_agent(), self._timeout()
        rules = self.get_source_rules()

        for url_template in self._config.get("search_urls", []):
            url = url_template.replace("{query}", quote_plus(query))
            html = fetch_search_page(url, ua, timeout)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                raw = extract_article_items(
                    soup,
                    f"https://{self._config['domain']}",
                    self._config["base_domain"],
                    path_min_segments=rules.listing_path_min_segments,
                )
                results = [
                    r for r in raw[:max_items]
                    if self.is_valid_article_url(r.get("url") or "")
                    and not self.should_exclude_url(r.get("url") or "")
                ]
                if results:
                    return results

        results = fetch_google_site_search(
            query, self._config["base_domain"], ua, timeout, max_items,
        )
        return [
            r for r in results
            if self.is_valid_article_url(r.get("url") or "")
            and not self.should_exclude_url(r.get("url") or "")
        ]


# ── Pre-defined configurations ──────────────────────────────────────────────

WEB_SEARCH_CONFIGS: dict[str, dict] = {
    "business_standard": {
        "provider_id": "business_standard",
        "publisher": "Business Standard",
        "domain": "www.business-standard.com",
        "base_domain": "business-standard.com",
        "search_urls": [
            "https://www.business-standard.com/search?q={query}&type=news",
        ],
        "exclude_paths": (
            "/section/", "/category/", "/tag/", "/author/", "/topic/", "/search",
        ),
    },
    "economic_times": {
        "provider_id": "economic_times",
        "publisher": "Economic Times",
        "domain": "economictimes.indiatimes.com",
        "base_domain": "economictimes.indiatimes.com",
        "search_urls": [
            "https://economictimes.indiatimes.com/topic/{query}",
            "https://economictimes.indiatimes.com/search?q={query}",
        ],
        "exclude_paths": (
            "/section/", "/category/", "/tag/", "/author/", "/topic/", "/search",
            "/slideshows/", "/markets/stock-market-gpt", "/markets/alpha-trades",
            "/markets/top-india-investors", "/markets/benefits/",
            "/prime/investment-ideas", "/prime/", "/markets/expert-view",
            "/marketstats/", "/masterclass/", "/markets/etmarkets-live/",
            "/markets/stocks/stock-screener/", "/markets/stocks/live-blog/",
            "/etmarkets/", "/markets/stocks/recos/", "/markets/commoditysummary/",
            "/markets/forex/", "/markets/cryptocurrency/", "/newshour/",
        ),
    },
    "business_day": {
        "provider_id": "business_day",
        "publisher": "Business Day",
        "domain": "www.businesslive.co.za",
        "base_domain": "businesslive.co.za",
        "search_urls": [
            "https://www.businesslive.co.za/search/?q={query}",
        ],
        "exclude_paths": (
            "/section/", "/category/", "/tag/", "/author/", "/topic/", "/search",
            "/news/science-and-environment", "/news/south-africa",
            "/companies/company-strategy", "/world/international-companies",
            "/lifestyle/", "/sport/",
        ),
    },
    "daily_investor": {
        "provider_id": "daily_investor",
        "publisher": "Daily Investor",
        "domain": "dailyinvestor.com",
        "base_domain": "dailyinvestor.com",
        "search_urls": [
            "https://dailyinvestor.com/?s={query}",
        ],
        "exclude_paths": (
            "/section/", "/category/", "/tag/", "/author/", "/topic/", "/page/",
        ),
    },
    "moneyweb": {
        "provider_id": "moneyweb",
        "publisher": "Moneyweb",
        "domain": "www.moneyweb.co.za",
        "base_domain": "moneyweb.co.za",
        "search_urls": [
            "https://www.moneyweb.co.za/?s={query}",
            "https://www.moneyweb.co.za/search?q={query}",
        ],
        "exclude_paths": (
            "/section/", "/category/", "/tag/", "/author/", "/topic/", "/page/",
            "/product/", "/tools-and-data/", "/webinars/", "/podcasts/",
        ),
    },
}
