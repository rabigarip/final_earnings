"""
Reuters context provider.

Searches reuters.com; parses results; returns NormalizedArticle.
Often returns empty (JS-rendered pages). Enrichment can fetch article page for date.
"""

from __future__ import annotations
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from src.config import cfg
from src.models.news import NormalizedArticle, ValidationStatus
from src.providers.context.base import RecentContextProvider
from src.providers.context.provider_helpers import SourceRules, UNIVERSAL_EXCLUDE_PATHS, _merge_excludes
from src.providers.context.provider_helpers import (
    fetch_article_page,
    parse_date_zawya_style,
    extract_publication_date_from_html,
    extract_date_from_snippet_or_url,
)


class ReutersContextProvider(RecentContextProvider):

    @property
    def provider_id(self) -> str:
        return "reuters"

    def get_source_rules(self) -> SourceRules:
        return SourceRules(
            domains=("reuters.com", "www.reuters.com"),
            valid_url_patterns=(r"/[a-z]+/article/", r"/markets/", r"/business/", r"/technology/", r"/world/"),
            exclude_path_patterns=_merge_excludes(("/site-search", "/authors/", "/topic/", "/section/"), UNIVERSAL_EXCLUDE_PATHS),
            date_confidence_policy="from_source",
        )

    def _user_agent(self) -> str:
        try:
            return (cfg().get("scraping", {}).get("user_agent") or
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/124.0.0.0 Safari/537.36")
        except Exception:
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/124.0.0.0 Safari/537.36"

    def _timeout(self) -> int:
        try:
            return int(cfg().get("scraping", {}).get("timeout_seconds", 15))
        except Exception:
            return 15

    def _search(self, query: str, max_items: int) -> list[dict]:
        """Fetch Reuters site-search page; return list of {headline, url, date_str}."""
        url = f"https://www.reuters.com/site-search/?query={quote_plus(query)}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": self._user_agent(), "Accept": "text/html"},
                timeout=self._timeout(),
            )
            if resp.status_code != 200:
                return []
        except Exception:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        items: list[dict] = []
        date_pat = re.compile(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            re.I,
        )
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if "reuters.com" not in href:
                continue
            full_url = href if href.startswith("http") else urljoin("https://www.reuters.com", href)
            headline = (a.get_text(strip=True) or "").strip()
            if len(headline) < 15 or len(headline) > 300:
                continue
            date_str = ""
            parent = a.parent
            for _ in range(5):
                if not parent:
                    break
                text = parent.get_text(separator=" ", strip=True)
                dm = date_pat.search(text)
                if dm:
                    date_str = dm.group(0)
                    break
                parent = getattr(parent, "parent", None)
            items.append({"headline": headline, "url": full_url, "date_str": date_str})
        seen = set()
        unique = []
        for x in items:
            if x["url"] not in seen:
                seen.add(x["url"])
                unique.append(x)
        return unique[:max_items]

    def search_company_articles(
        self,
        company_name: str,
        *,
        since: datetime | None = None,
        max_items: int = 15,
        **kwargs: object,
    ) -> list[NormalizedArticle]:
        out: list[NormalizedArticle] = []
        name = (company_name or "").strip()
        if not name:
            return out
        raw = self._search(name, max_items)
        short = name.split()[0] if " " in name and len(name.split()[0]) > 2 else None
        if short and len(raw) < max_items:
            time.sleep(0.5)
            raw2 = self._search(short, max_items - len(raw))
            seen = {r["url"] for r in raw}
            for r in raw2:
                if r["url"] not in seen:
                    raw.append(r)
                    seen.add(r["url"])
        for r in raw:
            headline = (r.get("headline") or "").strip()
            url = (r.get("url") or "").strip()
            date_str = (r.get("date_str") or "").strip()
            pub_dt = parse_date_zawya_style(date_str) if date_str else None
            date_source = "search_card" if date_str else ""
            date_from_search = bool(pub_dt and date_source)
            if not pub_dt:
                pub_dt, date_source = extract_date_from_snippet_or_url("", url, headline)
                date_from_search = bool(pub_dt)
            status = ValidationStatus.BASIC_VALID if (headline and url) else ValidationStatus.INVALID
            out.append(NormalizedArticle(
                headline=headline,
                publisher="Reuters",
                url=url,
                publication_date=pub_dt,
                snippet="",
                extracted_fact="",
                provider=self.provider_id,
                relevance_tag="",
                company_specific=True,
                sector_relevant=False,
                validation_status=status,
                date_source=date_source or "",
                date_confidence="medium" if pub_dt else "unknown",
                date_from_search_card=date_from_search,
            ))
        return out

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
            for r in raw:
                headline = (r.get("headline") or "").strip()
                url = (r.get("url") or "").strip()
                date_str = (r.get("date_str") or "").strip()
                pub_dt = parse_date_zawya_style(date_str) if date_str else None
                date_source = "search_card" if date_str else ""
                date_from_search = bool(pub_dt and date_source)
                if not pub_dt:
                    pub_dt, date_source = extract_date_from_snippet_or_url("", url, headline)
                    date_from_search = bool(pub_dt)
                out.append(NormalizedArticle(
                    headline=headline,
                    publisher="Reuters",
                    url=url,
                    publication_date=pub_dt,
                    snippet="",
                    extracted_fact="",
                    provider=self.provider_id,
                    relevance_tag="",
                    company_specific=False,
                    sector_relevant=True,
                    validation_status=ValidationStatus.BASIC_VALID,
                    date_source=date_source or "",
                    date_confidence="medium" if pub_dt else "unknown",
                    date_from_search_card=date_from_search,
                ))
            time.sleep(0.5)
        return out

    def enrich_metadata(self, article: NormalizedArticle) -> NormalizedArticle:
        if article.publication_date is not None:
            return article
        url = (article.url or "").strip()
        if not url or not url.startswith("http"):
            return article
        html = fetch_article_page(url, self._user_agent(), self._timeout())
        pub_dt, date_source = extract_publication_date_from_html(html)
        if pub_dt is not None:
            article.publication_date = pub_dt
            article.date_source = date_source or "article_page"
            article.date_confidence = "high" if date_source in ("time_tag", "meta_article", "json_ld") else "medium"
            article.validation_status = ValidationStatus.ENRICHED
        return article

    def extract_publication_date(self, url: str, html: str) -> tuple[datetime | None, str]:
        return extract_publication_date_from_html(html)
