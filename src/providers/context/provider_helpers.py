"""Shared helpers for context providers: source rules, date parsing, config, conversion, enrich."""

from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import requests
from bs4 import BeautifulSoup

from src.config import cfg
from src.models.news import NormalizedArticle, ValidationStatus


# ═══════════════════════════════════════════════════════════════════════════════
# Source rules (merged from source_rules.py)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SourceRules:
    """Per-source rules for candidate discovery, URL validity, and date confidence."""
    domains: tuple[str, ...] = ()
    valid_url_patterns: tuple[str, ...] = ()
    exclude_path_patterns: tuple[str, ...] = ()
    date_confidence_policy: str = "from_source"
    listing_path_min_segments: int = 2

    def is_valid_article_url(self, url: str) -> bool:
        if not url or not url.strip():
            return False
        url = url.strip().lower()
        if self.domains:
            if not any(d.lower() in url for d in self.domains):
                return False
        if self.exclude_path_patterns:
            if any(ex in url for ex in self.exclude_path_patterns):
                return False
        if self.valid_url_patterns:
            for pat in self.valid_url_patterns:
                try:
                    if re.search(pat, url):
                        return True
                except re.error:
                    continue
            return False
        return True

    def should_exclude_url(self, url: str) -> bool:
        if not url or not url.strip():
            return True
        url = url.strip().lower()
        for ex in self.exclude_path_patterns:
            if ex in url:
                return True
        return False


UNIVERSAL_EXCLUDE_PATHS: tuple[str, ...] = (
    "/terms", "/terms-of-service", "/terms-and-conditions",
    "/privacy", "/privacy-policy", "/cookie", "/cookies",
    "/about-us", "/about/", "/contact-us", "/contact/",
    "/information/", "/advertise", "/newsletter",
    "/login", "/register", "/registration/", "/sign-in", "/sign-up",
    "/subscribe", "/subscription", "/membership",
    "/sitemap", "/feed/",
)

# Domains never allowed as news/article sources (messaging, etc.)
BLOCKED_NEWS_DOMAINS: tuple[str, ...] = (
    "t.me",
    "telegram.org",
    "telegram.dog",
    "telegram.me",
)


def is_blocked_news_domain(url: str) -> bool:
    """True if URL host is a blocked domain (e.g. Telegram). Use when building candidates."""
    if not url or not isinstance(url, str) or not url.strip().startswith("http"):
        return False
    from urllib.parse import urlparse
    try:
        netloc = urlparse(url.strip()).netloc.lower()
        if not netloc or netloc.startswith("www."):
            netloc = netloc[4:] if netloc else ""
        return any(
            netloc == d or netloc.endswith("." + d)
            for d in BLOCKED_NEWS_DOMAINS
        )
    except Exception:
        return False


def _merge_excludes(*tuples: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tuples:
        for p in t:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return tuple(out)


def default_search_rules(
    domain: str,
    base_domain: str,
    exclude_paths: tuple[str, ...] = ("/section/", "/category/", "/tag/", "/author/", "/topic/"),
) -> SourceRules:
    return SourceRules(
        domains=(domain, base_domain),
        valid_url_patterns=(),
        exclude_path_patterns=_merge_excludes(exclude_paths, UNIVERSAL_EXCLUDE_PATHS),
        date_confidence_policy="from_source",
        listing_path_min_segments=2,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Date parsing
# ═══════════════════════════════════════════════════════════════════════════════

MONTHS_FULL = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_PAT = re.compile(rf"({MONTHS_FULL})\s+\d{{1,2}},?\s+\d{{4}}", re.I)
DATE_ONLY_PAT = re.compile(rf"^({MONTHS_FULL})\s+\d{{1,2}},?\s+\d{{4}}\s*$", re.I)


def parse_date_zawya_style(text: str) -> datetime | None:
    """e.g. 'March 2, 2026' or '2 March 2026'."""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    m = re.search(rf"({MONTHS_FULL})\s+(\d{{1,2}}),?\s+(\d{{4}})", text, re.I)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    m = re.search(rf"(\d{{1,2}})\s+({MONTHS_FULL})\s+(\d{{4}})", text, re.I)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_iso_date(text: str) -> datetime | None:
    """Parse ISO 8601 date/datetime."""
    if not text or not isinstance(text, str):
        return None
    text = text.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            dt = datetime.strptime(text[:26], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def extract_publication_date_from_html(html: str) -> tuple[datetime | None, str]:
    """Extract publication date from article page HTML via <time>, meta tags, and JSON-LD."""
    if not html:
        return None, ""
    soup = BeautifulSoup(html, "html.parser")
    for time_tag in soup.find_all("time"):
        dt = time_tag.get("datetime")
        if dt:
            parsed = parse_iso_date(dt)
            if parsed:
                return parsed, "time_tag"
        text = (time_tag.get_text(strip=True) or "").strip()
        if text:
            parsed = parse_date_zawya_style(text)
            if parsed:
                return parsed, "time_tag"
    for meta in soup.find_all("meta", attrs={"property": re.compile(r"article:published_time", re.I)}):
        c = meta.get("content")
        if c and parse_iso_date(c):
            return parse_iso_date(c), "meta_article"
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"published|date|publish", re.I)}):
        c = meta.get("content")
        if c:
            parsed = parse_iso_date(c) or parse_date_zawya_style(c)
            if parsed:
                return parsed, "meta_date"
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = script.string or ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if data.get("@type") == "Article" or "Article" in (data.get("@type") or []):
                    dp = data.get("datePublished")
                    if dp:
                        parsed = parse_iso_date(str(dp)) or parse_date_zawya_style(str(dp))
                        if parsed:
                            return parsed, "json_ld"
            for item in (data.get("@graph") or []) if isinstance(data.get("@graph"), list) else []:
                if isinstance(item, dict) and (item.get("@type") == "Article" or "Article" in (item.get("@type") or [])):
                    dp = item.get("datePublished")
                    if dp:
                        parsed = parse_iso_date(str(dp)) or parse_date_zawya_style(str(dp))
                        if parsed:
                            return parsed, "json_ld"
        except (json.JSONDecodeError, TypeError):
            continue
    for meta in soup.find_all("meta", content=True):
        c = meta.get("content", "")
        if re.match(r"\d{4}-\d{2}-\d{2}", c):
            parsed = parse_iso_date(c)
            if parsed:
                return parsed, "meta_generic"
    return None, ""


def extract_date_from_snippet_or_url(snippet: str, url: str, headline: str = "") -> tuple[datetime | None, str]:
    """Fallback: try to extract a date from URL path, then snippet, then headline."""
    if url:
        m = re.search(r"(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])", url)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                return dt, "url_fallback"
            except ValueError:
                pass
    for text in (headline, snippet):
        parsed = parse_date_zawya_style(text)
        if parsed:
            return parsed, "snippet"
    for text in (snippet, headline):
        parsed = parse_iso_date(text)
        if parsed:
            return parsed, "snippet"
    text = f"{snippet} {headline}"
    m = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc), "snippet"
        except ValueError:
            pass
    return None, ""


def fetch_article_page(url: str, user_agent: str, timeout: int = 12) -> str:
    """Fetch article page HTML; return empty string on failure."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.text or ""
    except Exception:
        pass
    return ""


# ── Config & conversion ─────────────────────────────────────────────────────

def get_scraping_config() -> tuple[str, int]:
    """Return (user_agent, timeout_seconds) from config."""
    scrap = cfg().get("scraping") or {}
    ua = (scrap.get("user_agent") or "Mozilla/5.0 (compatible; EarningsResearch/1.0)").strip()
    timeout = int(scrap.get("timeout_seconds", 15))
    return ua, timeout


def raw_items_to_articles(
    raw: list[dict],
    publisher: str,
    provider_id: str,
    *,
    company_specific: bool = True,
    sector_relevant: bool = False,
) -> list[NormalizedArticle]:
    """Convert list of {headline, url, date_str} to NormalizedArticle list."""
    out: list[NormalizedArticle] = []
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
        # Use source_name from RSS feeds (Google News) as publisher when available
        source_pub = (r.get("source_name") or "").strip() or publisher
        out.append(NormalizedArticle(
            headline=headline,
            publisher=source_pub,
            url=url,
            publication_date=pub_dt,
            snippet="",
            extracted_fact="",
            provider=provider_id,
            relevance_tag="",
            company_specific=company_specific,
            sector_relevant=sector_relevant,
            validation_status=ValidationStatus.BASIC_VALID,
            date_source=date_source or "",
            date_confidence="medium" if pub_dt else "unknown",
            date_from_search_card=date_from_search,
        ))
    return out


def default_enrich_metadata(article: NormalizedArticle, user_agent: str, timeout: int) -> NormalizedArticle:
    """Fetch article page and set publication_date from HTML if missing."""
    if article.publication_date is not None:
        return article
    url = (article.url or "").strip()
    if not url or not url.startswith("http"):
        return article
    html = fetch_article_page(url, user_agent, timeout)
    pub_dt, date_source = extract_publication_date_from_html(html)
    if pub_dt is not None:
        article.publication_date = pub_dt
        article.date_source = date_source or "article_page"
        article.date_confidence = "high" if date_source in ("time_tag", "meta_article", "json_ld") else "medium"
        article.validation_status = ValidationStatus.ENRICHED
    return article


def search_with_short_fallback(
    search_fn: Callable[[str, int], list[dict]],
    company_name: str,
    max_items: int,
    delay: float = 0.5,
) -> list[dict]:
    """Run search(company_name); if few results and name has multiple words, also search(first_word) and merge."""
    name = (company_name or "").strip()
    if not name:
        return []
    raw = search_fn(name, max_items)
    short = name.split()[0] if " " in name and len(name.split()[0]) > 2 else None
    if short and len(raw) < max_items:
        time.sleep(delay)
        raw2 = search_fn(short, max_items - len(raw))
        seen = {r["url"] for r in raw}
        for r in raw2:
            if r["url"] not in seen:
                raw.append(r)
                seen.add(r["url"])
    return raw
