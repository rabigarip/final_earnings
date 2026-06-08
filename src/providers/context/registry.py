"""Context provider registry and config.

Loads [news.context_providers] from settings; returns enabled providers
ordered by source_priority. Used by the shared pipeline:
retrieve → enrich → validate → dedupe → rank → render.
"""

from __future__ import annotations
from typing import Any

from src.config import cfg
from src.providers.context.base import RecentContextProvider
from src.providers.context.reuters_provider import ReutersContextProvider
from src.providers.context.zawya_provider import ZawyaContextProvider
from src.providers.context.scmp_provider import SCMPContextProvider
from src.providers.context.search_utils import NewsAPIContextProvider
from src.providers.context.google_news_provider import GoogleNewsProvider
from src.providers.context.web_search_provider import WebSearchProvider, WEB_SEARCH_CONFIGS

_PROVIDER_CLASSES: dict[str, type[RecentContextProvider]] = {
    "reuters": ReutersContextProvider,
    "zawya": ZawyaContextProvider,
    "scmp": SCMPContextProvider,
    "newsapi": NewsAPIContextProvider,
    "google_news": GoogleNewsProvider,
}

# Web search providers are instantiated from config, not from class
_WEB_SEARCH_IDS = frozenset(WEB_SEARCH_CONFIGS.keys())


def _get_provider_config() -> list[dict[str, Any]]:
    """Read context_providers from config."""
    try:
        news = cfg().get("news") or {}
        providers = news.get("context_providers")
        if isinstance(providers, list):
            return providers
    except Exception:
        pass
    return []


_COUNTRY_NAME_TO_ISO: dict[str, str] = {
    "china": "CN",
    "india": "IN",
    "south africa": "ZA",
    "saudi arabia": "SA",
    "hong kong": "HK",
    "united arab emirates": "AE",
    "uae": "AE",
    "bahrain": "BH",
    "kuwait": "KW",
    "oman": "OM",
    "qatar": "QA",
    "egypt": "EG",
}


def _country_to_iso(country: str | None) -> str:
    raw = (country or "").strip()
    if not raw:
        return raw
    return _COUNTRY_NAME_TO_ISO.get(raw.lower(), raw)


def _provider_matches_country(config: dict[str, Any], country: str | None) -> bool:
    countries_cfg = config.get("countries")
    if countries_cfg is None:
        return True
    if isinstance(countries_cfg, list):
        if len(countries_cfg) == 0:
            return True
        if not (country or "").strip():
            return True
        cc = _country_to_iso(country).upper()
        if not cc:
            return True
        return any((c or "").strip().upper() == cc for c in countries_cfg)
    return True


def _instantiate_provider(name: str) -> RecentContextProvider | None:
    """Instantiate a provider by name: class-based or config-driven WebSearchProvider."""
    if name in _PROVIDER_CLASSES:
        return _PROVIDER_CLASSES[name]()
    if name in _WEB_SEARCH_IDS:
        return WebSearchProvider(WEB_SEARCH_CONFIGS[name])
    return None


def get_enabled_context_providers(country: str | None = None) -> list[RecentContextProvider]:
    """Return enabled providers ordered by source_priority, filtered by country."""
    configs = _get_provider_config()
    enabled: list[tuple[int, RecentContextProvider]] = []
    for c in configs:
        name = (c.get("provider_name") or "").strip().lower()
        if not name or not c.get("enabled", True):
            continue
        if not _provider_matches_country(c, country):
            continue
        try:
            inst = _instantiate_provider(name)
            if not inst:
                continue
            priority = int(c.get("source_priority", 999))
            enabled.append((priority, inst))
        except Exception:
            continue
    enabled.sort(key=lambda x: x[0])
    return [p for _, p in enabled]


def get_context_provider_config(provider_id: str) -> dict[str, Any]:
    provider_id = (provider_id or "").strip().lower()
    for c in _get_provider_config():
        if (c.get("provider_name") or "").strip().lower() == provider_id:
            return dict(c)
    return {}


def get_source_priority_order(country: str | None = None) -> list[str]:
    providers = get_enabled_context_providers(country=country)
    return [p.provider_id for p in providers]


def register_provider(provider_name: str, provider_class: type[RecentContextProvider]) -> None:
    _PROVIDER_CLASSES[(provider_name or "").strip().lower()] = provider_class
