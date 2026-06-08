"""Google News RSS context provider — universal, reliable, no auth required.

Fetches Google News RSS feeds for company-specific and sector articles.
Works for any country/language without API keys or JS rendering.
Only articles from trusted financial/business publishers are returned.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

from src.providers.context.base import SearchBasedContextProvider
from src.providers.context.provider_helpers import (
    SourceRules,
    UNIVERSAL_EXCLUDE_PATHS,
    get_scraping_config,
    parse_iso_date,
)

logger = logging.getLogger(__name__)

# Trusted financial/business publishers — only articles from these sources are kept.
# Matched case-insensitively against the RSS entry's source name.
# Covers global wires, major business press, and reputable regional outlets.
TRUSTED_PUBLISHERS: frozenset[str] = frozenset(s.lower() for s in (
    # ── Global wires & agencies ──
    "Reuters", "AP News", "Associated Press", "AFP",
    "Bloomberg", "Bloomberg.com", "Bloomberg Law News", "BNN Bloomberg",
    # ── Major business/financial press ──
    "CNBC", "CNBC TV18", "Financial Times", "The Wall Street Journal",
    "The New York Times", "The Washington Post", "The Guardian",
    "BBC", "BBC News", "Al Jazeera", "France 24",
    "Forbes", "Fortune", "Business Insider", "Business Insider Africa",
    "Yahoo Finance", "Yahoo News", "MSN",
    "MarketWatch", "Barron's", "Investor's Business Daily",
    "The Motley Fool", "Seeking Alpha", "Zacks Investment Research",
    "MarketBeat", "TipRanks", "Investing.com", "TradingView",
    # ── Asia-Pacific ──
    "South China Morning Post", "SCMP", "Caixin Global", "China Daily",
    "Nikkei Asia", "The Japan Times", "Yonhap News Agency",
    "The Straits Times", "Channel NewsAsia", "CNA",
    "The Economic Times", "The Times of India", "Moneycontrol",
    "Moneycontrol.com", "Mint", "NDTV", "NDTV Profit",
    "Business Standard", "BusinessLine", "ET Now", "ET Manufacturing",
    "LiveMint", "Financial Express", "The Hindu BusinessLine",
    # ── Middle East & Africa ──
    "ZAWYA", "Zawya", "Gulf News", "Arabian Business", "Arab News",
    "Argaam", "ارقام", "Asharq Al-Awsat", "Khaleej Times",
    "Business Day", "Businessday NG", "Daily Investor", "Moneyweb",
    "IOL", "News24", "Fin24", "TimesLIVE",
    # ── Europe ──
    "The Economist", "The Telegraph", "City A.M.",
    "Handelsblatt", "Les Echos",
    # ── Americas ──
    "Semafor", "The Globe and Mail", "BNN",
    "Valor Econômico", "Valor Internacional",
    # ── Industry / sector press ──
    "Upstream Online", "World Oil", "Offshore Energy", "Offshore Magazine",
    "Automotive News", "Electrek", "TechCrunch",
    "The Loadstar", "Global Trade Review (GTR)", "gCaptain",
    "Rigzone", "S&P Global", "IHS Markit",
    "PR Newswire", "GlobeNewswire", "Business Wire",
))


def _is_trusted(source_name: str) -> bool:
    return (source_name or "").strip().lower() in TRUSTED_PUBLISHERS


class GoogleNewsProvider(SearchBasedContextProvider):
    @property
    def provider_id(self) -> str:
        return "google_news"

    @property
    def publisher(self) -> str:
        return "Google News"

    def get_source_rules(self) -> SourceRules:
        return SourceRules(
            domains=(),
            valid_url_patterns=(),
            exclude_path_patterns=UNIVERSAL_EXCLUDE_PATHS,
            date_confidence_policy="from_source",
        )

    def _search(self, query: str, max_items: int) -> list[dict]:
        rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en&gl=US&ceid=US:en"
        ua, timeout = get_scraping_config()
        try:
            resp = requests.get(rss_url, headers={"User-Agent": ua}, timeout=timeout)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            logger.warning("Google News RSS fetch failed: %s", e)
            return []

        items: list[dict] = []
        # Over-fetch to compensate for untrusted source filtering
        for entry in (feed.entries or [])[:max_items * 4]:
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            if not title or not link or len(title) < 10:
                continue

            source_name = ""
            source_obj = getattr(entry, "source", None)
            if source_obj and hasattr(source_obj, "get"):
                source_name = (source_obj.get("title", "") or "").strip()
            elif source_obj and hasattr(source_obj, "title"):
                source_name = (source_obj.title or "").strip()

            if not _is_trusted(source_name):
                continue

            date_str = ""
            pp = getattr(entry, "published_parsed", None)
            if pp:
                try:
                    dt = datetime(*pp[:6], tzinfo=timezone.utc)
                    date_str = dt.strftime("%B %d, %Y")
                except (ValueError, TypeError):
                    pass
            if not date_str:
                published = getattr(entry, "published", "") or ""
                if published:
                    parsed = parse_iso_date(published)
                    if parsed:
                        date_str = parsed.strftime("%B %d, %Y")

            items.append({
                "headline": title,
                "url": link,
                "date_str": date_str,
                "source_name": source_name,
            })
            if len(items) >= max_items:
                break

        return items
