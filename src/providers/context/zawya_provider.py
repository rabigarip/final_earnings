"""
ZAWYA context provider.

Searches zawya.com; parses results; returns NormalizedArticle.
Enrichment fetches article page for publication date when missing.
"""

from __future__ import annotations
import re
import time
from typing import Any
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.config import cfg
from src.models.news import NormalizedArticle, ValidationStatus
from src.providers.context.base import RecentContextProvider
from src.providers.context.provider_helpers import (
    SourceRules,
    UNIVERSAL_EXCLUDE_PATHS,
    _merge_excludes,
    extract_date_from_snippet_or_url,
    extract_publication_date_from_html,
    fetch_article_page,
    parse_date_zawya_style,
    parse_iso_date,
)


class ZawyaContextProvider(RecentContextProvider):

    @property
    def provider_id(self) -> str:
        return "zawya"

    def get_source_rules(self) -> SourceRules:
        return SourceRules(
            domains=("zawya.com", "www.zawya.com"),
            valid_url_patterns=(r"/en/", r"/story/", r"/article/"),
            exclude_path_patterns=_merge_excludes(("/search", "/section/", "/tag/", "/author/", "/newsletter"), UNIVERSAL_EXCLUDE_PATHS),
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
        """Fetch ZAWYA search page; return list of {headline, url, date_str}."""
        url = f"https://www.zawya.com/en/search?q={quote_plus(query)}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": self._user_agent(), "Accept": "text/html,application/xhtml+xml"},
                timeout=self._timeout(),
            )
            resp.raise_for_status()
        except Exception:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        items: list[dict] = []
        date_pat = re.compile(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            re.I,
        )
        date_only_pat = re.compile(
            r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\s*$",
            re.I,
        )
        date_elements: list[tuple[str, Any]] = []
        for tag in soup.find_all(string=True):
            text = (tag if isinstance(tag, str) else getattr(tag, "string", "") or "").strip()
            if not text or len(text) > 80:
                continue
            m = date_only_pat.match(text) or date_pat.search(text)
            if m:
                parent = tag.parent if hasattr(tag, "parent") else None
                date_elements.append((m.group(0).strip(), parent))
        for time_tag in soup.find_all("time"):
            dt = time_tag.get("datetime")
            if dt:
                parsed = parse_iso_date(dt)
                if parsed:
                    date_elements.append((parsed.strftime("%B %d, %Y"), time_tag))
            text = (time_tag.get_text(strip=True) or "").strip()
            if date_pat.search(text):
                date_elements.append((text, time_tag))
        # Strategy: find containers that have both a date-like text and an article link (card-level, not whole page)
        items_from_containers: list[dict] = []
        for tag in soup.find_all():
            text = (tag.get_text(separator=" ", strip=True) or "").strip()
            if len(text) > 400:
                continue
            dm = date_pat.search(text)
            if not dm:
                continue
            date_str_in_block = dm.group(0)
            for a in tag.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if "zawya.com" not in href and not href.startswith("/en/"):
                    continue
                full_url = href if href.startswith("http") else urljoin("https://www.zawya.com", href)
                if "zawya.com" not in full_url:
                    continue
                headline = (a.get_text(strip=True) or "").strip()
                if len(headline) < 20 or len(headline) > 300:
                    continue
                path = urlparse(full_url).path.rstrip("/") or "/"
                if path.count("/") <= 2:
                    continue
                items_from_containers.append({"headline": headline, "url": full_url, "date_str": date_str_in_block})
                break  # one link per container
        article_links: list[tuple[Any, str, str]] = []
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if "zawya.com" in href or href.startswith("/en/"):
                full_url = href if href.startswith("http") else urljoin("https://www.zawya.com", href)
                if "zawya.com" not in full_url:
                    continue
                headline = (a.get_text(strip=True) or "").strip()
                if len(headline) < 20 or len(headline) > 300:
                    continue
                path = urlparse(full_url).path.rstrip("/") or "/"
                if path.count("/") <= 2:
                    continue
                article_links.append((a, full_url, headline))
        # Prefer date from container-based extraction (same block has date + link)
        url_to_date: dict[str, str] = {r["url"]: r["date_str"] for r in items_from_containers}
        for a, full_url, headline in article_links:
            date_str = url_to_date.get(full_url, "")
            if not date_str:
                # Walk next elements in document order
                el = a
                for _ in range(12):
                    el = el.find_next() if getattr(el, "find_next", None) else None
                    if not el or el is a:
                        break
                    t = (getattr(el, "get_text", lambda: "")(strip=True) or "").strip()
                    if not t or len(t) > 50:
                        continue
                    dm = date_pat.search(t)
                    if dm:
                        date_str = dm.group(0)
                        break
                    if date_only_pat.match(t):
                        date_str = t
                        break
            if not date_str:
                for sib in [getattr(a, "next_sibling", None), getattr(a, "previous_sibling", None)]:
                    if sib and getattr(sib, "get_text", None):
                        t = (sib.get_text(separator=" ", strip=True) or "").strip()
                        dm = date_pat.search(t)
                        if dm or date_only_pat.match(t):
                            date_str = date_pat.search(t).group(0) if date_pat.search(t) else t
                            break
            if not date_str:
                parent = getattr(a, "parent", None)
                for _ in range(6):
                    if not parent:
                        break
                    text = parent.get_text(separator=" ", strip=True)
                    dm = date_pat.search(text)
                    if dm:
                        date_str = dm.group(0)
                        break
                    parent = getattr(parent, "parent", None)
            if not date_str:
                p = getattr(a, "parent", None)
                for _ in range(4):
                    if not p:
                        break
                    for sib in [getattr(p, "next_sibling", None), getattr(p, "previous_sibling", None)]:
                        if sib and getattr(sib, "get_text", None):
                            t = (sib.get_text(separator=" ", strip=True) or "").strip()
                            dm = date_pat.search(t)
                            if dm:
                                date_str = dm.group(0)
                                break
                    if date_str:
                        break
                    p = getattr(p, "parent", None)
            if not date_str and getattr(a, "parent", None):
                for child in getattr(a.parent, "children", []):
                    if child is a:
                        continue
                    t = (getattr(child, "get_text", lambda: "")(strip=True) or "").strip()
                    dm = date_pat.search(t) if t else None
                    if date_only_pat.match(t) or (len(t) < 25 and dm):
                        date_str = dm.group(0) if dm else t
                        break
            if not date_str and date_elements:
                link_ancestors = set()
                p = a
                for _ in range(8):
                    if not p:
                        break
                    link_ancestors.add(id(p))
                    p = getattr(p, "parent", None)
                for d, elem in date_elements:
                    if not elem:
                        continue
                    q = elem
                    for _ in range(8):
                        if not q:
                            break
                        if id(q) in link_ancestors:
                            date_str = d
                            break
                        q = getattr(q, "parent", None)
                    if date_str:
                        break
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
            out.append(NormalizedArticle(
                headline=headline,
                publisher="ZAWYA",
                url=url,
                publication_date=pub_dt,
                snippet="",
                extracted_fact="",
                provider=self.provider_id,
                relevance_tag="",
                company_specific=True,
                sector_relevant=False,
                validation_status=ValidationStatus.BASIC_VALID,
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
                    publisher="ZAWYA",
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
