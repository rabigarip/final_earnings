"""
South China Morning Post (SCMP) context provider – China.
NewsAPI first (if key set), then scrape/browser/Google fallbacks.
"""

from __future__ import annotations
import os
import time
from datetime import datetime
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from src.config import cfg, root
from src.models.news import NormalizedArticle
from src.providers.context.base import RecentContextProvider
from src.providers.context.provider_helpers import SourceRules, UNIVERSAL_EXCLUDE_PATHS, _merge_excludes
from src.providers.context.provider_helpers import (
    default_enrich_metadata,
    get_scraping_config,
    raw_items_to_articles,
    search_with_short_fallback,
)
from src.providers.context.search_utils import (
    fetch_google_site_search,
    fetch_newsapi_scmp,
    fetch_search_page,
    fetch_search_page_with_browser,
    extract_article_items,
)


class SCMPContextProvider(RecentContextProvider):
    @property
    def provider_id(self) -> str:
        return "scmp"

    def get_source_rules(self) -> SourceRules:
        return SourceRules(
            domains=("scmp.com", "www.scmp.com"),
            valid_url_patterns=(r"/news/", r"/business/", r"/economy/", r"/tech/", r"/opinion/", r"/lifestyle/"),
            exclude_path_patterns=_merge_excludes(("/search", "/section/", "/topic/", "/author/", "/newsletter"), UNIVERSAL_EXCLUDE_PATHS),
            date_confidence_policy="from_source",
        )

    def _user_agent(self) -> str:
        return get_scraping_config()[0]

    def _timeout(self) -> int:
        return get_scraping_config()[1]

    def _newsapi_key(self) -> str:
        try:
            from dotenv import load_dotenv
            load_dotenv(root() / ".env")
        except ImportError:
            pass
        return (
            (cfg().get("news") or {}).get("newsapi_key") or ""
        ).strip() or (os.environ.get("NEWSAPI_KEY") or "").strip()

    def _search(self, query: str, max_items: int) -> list[dict]:
        raw: list[dict] = []
        api_key = self._newsapi_key()
        if api_key:
            raw = fetch_newsapi_scmp(query, api_key, max_items=max_items)
        if len(raw) < max_items:
            url = f"https://www.scmp.com/search?q={quote_plus(query)}"
            html = fetch_search_page(url, self._user_agent(), self._timeout())
            seen = {r["url"] for r in raw}
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for r in extract_article_items(soup, "https://www.scmp.com", "scmp.com", path_min_segments=2):
                    if r["url"] not in seen:
                        raw.append(r)
                        seen.add(r["url"])
            if len(raw) < max_items and len(html or "") < 15000:
                time.sleep(0.3)
                html_js = fetch_search_page_with_browser(url, self._user_agent(), timeout_sec=25)
                if html_js and len(html_js) > len(html or ""):
                    soup_js = BeautifulSoup(html_js, "html.parser")
                    for r in extract_article_items(soup_js, "https://www.scmp.com", "scmp.com", path_min_segments=2):
                        if r["url"] not in seen:
                            raw.append(r)
                            seen.add(r["url"])
            if len(raw) < max_items:
                time.sleep(0.5)
                for r in fetch_google_site_search(query, "scmp.com", self._user_agent(), self._timeout(), max_items=max_items):
                    if r["url"] not in {x["url"] for x in raw}:
                        raw.append(r)
        return raw[:max_items]

    def search_company_articles(
        self,
        company_name: str,
        *,
        since: datetime | None = None,
        max_items: int = 15,
        **kwargs: object,
    ) -> list[NormalizedArticle]:
        raw = search_with_short_fallback(self._search, company_name or "", max_items)
        return raw_items_to_articles(raw, "South China Morning Post", self.provider_id, company_specific=True, sector_relevant=False)

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
            out.extend(raw_items_to_articles(self._search(q, max_items_per_query), "South China Morning Post", self.provider_id, company_specific=False, sector_relevant=True))
            time.sleep(0.5)
        return out

    def enrich_metadata(self, article: NormalizedArticle) -> NormalizedArticle:
        return default_enrich_metadata(article, self._user_agent(), self._timeout())