"""
Shared search-page fetch, article-link extraction, and NewsAPI provider for context providers.
"""

from __future__ import annotations
import os
import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.config import cfg, root
from src.providers.context.provider_helpers import (
    DATE_ONLY_PAT, DATE_PAT, parse_iso_date,
    SourceRules, UNIVERSAL_EXCLUDE_PATHS,
)
from src.providers.context.base import SearchBasedContextProvider

# Google SERP snippet dates: "Mar 10, 2026" style
GOOGLE_SNIPPET_DATE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}",
    re.I,
)


def fetch_newsapi_scmp(query: str, api_key: str, max_items: int = 15) -> list[dict[str, str]]:
    """
    Fetch SCMP articles via NewsAPI.org (everything endpoint, domains=scmp.com).
    Returns list of {headline, url, date_str}. Empty if no key, or API error.
    """
    key = (api_key or "").strip()
    if not key:
        return []
    url = "https://newsapi.org/v2/everything"
    # Don't use from/to by default - free tier can return 0 when date range is set
    params = {
        "q": query,
        "domains": "scmp.com",
        "apiKey": key,
        "pageSize": min(max_items, 20),
        "language": "en",
        "sortBy": "publishedAt",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json() if resp.content else {}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("NewsAPI request failed: %s", e)
        return []
    if data.get("status") == "error":
        import logging
        logging.getLogger(__name__).warning(
            "NewsAPI error: %s - %s", data.get("code", "unknown"), data.get("message", "")
        )
        return []
    if resp.status_code != 200:
        return []
    articles = data.get("articles") or []
    out: list[dict[str, str]] = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        title = (a.get("title") or "").strip()
        link = (a.get("url") or "").strip()
        if not link:
            continue
        # API already filtered by domains=scmp.com; keep only SCMP URLs if present
        if link and "scmp.com" not in link:
            continue
        pub = (a.get("publishedAt") or "").strip()
        dt = parse_iso_date(pub) if pub else None
        date_str = dt.strftime("%B %d, %Y") if dt else ""
        if len(title) < 10:
            continue
        out.append({"headline": title, "url": link, "date_str": date_str})
    return out[:max_items]


def fetch_newsapi_any(query: str, api_key: str, max_items: int = 15) -> list[dict[str, str]]:
    """
    Fetch articles via NewsAPI (no domain filter). Use as fallback for a region
    when domain-specific (e.g. SCMP) returns 0. Returns list of {headline, url, date_str}.
    """
    key = (api_key or "").strip()
    if not key:
        return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "apiKey": key,
        "pageSize": min(max_items, 20),
        "language": "en",
        "sortBy": "publishedAt",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json() if resp.content else {}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("NewsAPI request failed: %s", e)
        return []
    if data.get("status") == "error":
        return []
    if resp.status_code != 200:
        return []
    articles = data.get("articles") or []
    out: list[dict[str, str]] = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        title = (a.get("title") or "").strip()
        link = (a.get("url") or "").strip()
        if not link or len(title) < 10:
            continue
        pub = (a.get("publishedAt") or "").strip()
        dt = parse_iso_date(pub) if pub else None
        date_str = dt.strftime("%B %d, %Y") if dt else ""
        out.append({"headline": title, "url": link, "date_str": date_str})
    return out[:max_items]


def fetch_search_page(url: str, user_agent: str, timeout: int = 15) -> str:
    """Fetch search page HTML; return empty string on failure."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text or ""
    except Exception:
        return ""


def fetch_search_page_with_browser(url: str, user_agent: str, timeout_sec: int = 25) -> str:
    """
    Fetch search page using headless browser (Playwright). Use when the site
    is JS-rendered and requests.get returns an empty or minimal HTML.
    Returns empty string if Playwright is unavailable or on error.
    """
    try:
        from src.scraping.browser import fetch_page_with_browser
    except ImportError:
        return ""
    return fetch_page_with_browser(
        url,
        user_agent=user_agent,
        timeout_ms=timeout_sec * 1000,
        wait_selector=None,
        wait_timeout_ms=10000,
    )


def fetch_google_site_search(
    query: str,
    site_domain: str,
    user_agent: str,
    timeout: int = 15,
    max_items: int = 15,
) -> list[dict[str, str]]:
    """
    Fallback when a site's own search is JS-only or unavailable.
    Runs Google search with site:DOMAIN and parses SERP for links to that domain.
    Returns list of {headline, url, date_str}. Use sparingly to avoid rate limits.
    Note: Google may serve a consent/block page instead of SERP; in that case returns [].
    """
    q = f"site:{site_domain} {query}"
    url = f"https://www.google.com/search?q={quote_plus(q)}&num=20"
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        html = resp.text or ""
    except Exception:
        return []
    if not html or site_domain not in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    # Google result blocks: link inside a div; often h3 > a for title
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        # Resolve Google redirect URLs to get real article link
        if "/url?" in href and "url=" in href:
            try:
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                real = (qs.get("url") or [""])[0] or (qs.get("q") or [""])[0]
                if real and site_domain in real:
                    href = real
                else:
                    continue
            except Exception:
                continue
        if site_domain not in href:
            continue
        if "google.com" in href:
            continue
        path = urlparse(href).path.rstrip("/") or "/"
        if path.count("/") < 2:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        headline = (a.get_text(strip=True) or "").strip()
        if len(headline) < 10 or len(headline) > 300:
            continue
        date_str = ""
        parent = a.parent
        for _ in range(5):
            if not parent:
                break
            text = (parent.get_text(separator=" ", strip=True) or "").strip()
            m = GOOGLE_SNIPPET_DATE.search(text) or DATE_PAT.search(text)
            if m:
                date_str = m.group(0)
                break
            parent = getattr(parent, "parent", None)
        items.append({"headline": headline, "url": href, "date_str": date_str})
        if len(items) >= max_items:
            break
    return items


def extract_article_items(
    soup: BeautifulSoup,
    base_url: str,
    domain_in_url: str,
    *,
    min_headline_len: int = 15,
    max_headline_len: int = 300,
    path_min_segments: int = 2,
) -> list[dict[str, str]]:
    """
    Extract list of {headline, url, date_str} from a search results page.
    Uses same strategy as Zawya: date elements, containers with date+link, then all links in domain.
    """
    items: list[dict] = []
    date_elements: list[tuple[str, Any]] = []

    for tag in soup.find_all(string=True):
        text = (tag if isinstance(tag, str) else getattr(tag, "string", "") or "").strip()
        if not text or len(text) > 80:
            continue
        m = DATE_ONLY_PAT.match(text) or DATE_PAT.search(text)
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
        if DATE_PAT.search(text):
            date_elements.append((text, time_tag))

    # Containers that have both date and article link
    for tag in soup.find_all():
        text = (tag.get_text(separator=" ", strip=True) or "").strip()
        if len(text) > 400:
            continue
        dm = DATE_PAT.search(text)
        if not dm:
            continue
        date_str_in_block = dm.group(0)
        for a in tag.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if domain_in_url not in href and not (href.startswith("/") and len(href) > 3):
                continue
            full_url = href if href.startswith("http") else urljoin(base_url, href)
            if domain_in_url not in full_url:
                continue
            headline = (a.get_text(strip=True) or "").strip()
            if len(headline) < min_headline_len or len(headline) > max_headline_len:
                continue
            path = urlparse(full_url).path.rstrip("/") or "/"
            if path.count("/") < path_min_segments:
                continue
            items.append({"headline": headline, "url": full_url, "date_str": date_str_in_block})
            break

    # All links in domain, skipping nav/header/footer/aside/menu elements
    _SKIP_PARENT_TAGS = frozenset(("nav", "header", "footer", "aside"))
    _SKIP_PARENT_ROLES = frozenset(("navigation", "banner", "contentinfo", "complementary", "menu"))

    for a in soup.find_all("a", href=True):
        # Skip links nested inside nav, header, footer, aside
        skip = False
        for parent in a.parents:
            if getattr(parent, "name", None) in _SKIP_PARENT_TAGS:
                skip = True
                break
            role = (parent.get("role") or "").strip().lower() if hasattr(parent, "get") else ""
            if role in _SKIP_PARENT_ROLES:
                skip = True
                break
        if skip:
            continue
        href = (a.get("href") or "").strip()
        if domain_in_url not in href and not (href.startswith("/") and len(href) > 3):
            continue
        full_url = href if href.startswith("http") else urljoin(base_url, href)
        if domain_in_url not in full_url:
            continue
        headline = (a.get_text(strip=True) or "").strip()
        if len(headline) < min_headline_len or len(headline) > max_headline_len:
            continue
        path = urlparse(full_url).path.rstrip("/") or "/"
        if path.count("/") < path_min_segments:
            continue
        date_str = ""
        for r in items:
            if r["url"] == full_url:
                date_str = r.get("date_str", "")
                break
        if not date_str:
            el = a
            for _ in range(10):
                el = el.find_next() if getattr(el, "find_next", None) else None
                if not el or el is a:
                    break
                t = (getattr(el, "get_text", lambda: "")(strip=True) or "").strip()
                if t and len(t) <= 50:
                    dm = DATE_PAT.search(t) or DATE_ONLY_PAT.match(t)
                    if dm:
                        date_str = dm.group(0)
                        break
            if not date_str and getattr(a, "parent", None):
                parent = a.parent
                for _ in range(5):
                    if not parent:
                        break
                    dm = DATE_PAT.search(parent.get_text(separator=" ", strip=True) or "")
                    if dm:
                        date_str = dm.group(0)
                        break
                    parent = getattr(parent, "parent", None)
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
        if not any(r["url"] == full_url for r in items):
            items.append({"headline": headline, "url": full_url, "date_str": date_str})

    seen: set[str] = set()
    unique: list[dict] = []
    for x in items:
        if x["url"] not in seen:
            seen.add(x["url"])
            unique.append(x)
    return unique


# ═══════════════════════════════════════════════════════════════════════════════
# NewsAPI context provider (merged from newsapi_provider.py)
# ═══════════════════════════════════════════════════════════════════════════════

class NewsAPIContextProvider(SearchBasedContextProvider):
    @property
    def provider_id(self) -> str:
        return "newsapi"

    @property
    def publisher(self) -> str:
        return "NewsAPI"

    def get_source_rules(self) -> SourceRules:
        return SourceRules(
            domains=(),
            valid_url_patterns=(),
            exclude_path_patterns=UNIVERSAL_EXCLUDE_PATHS,
            date_confidence_policy="from_source",
        )

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
        key = self._newsapi_key()
        if not key:
            return []
        return fetch_newsapi_any(query, key, max_items=max_items)
