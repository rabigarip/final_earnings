"""
Shared recent-context pipeline: retrieve → normalize → enrich → validate → dedupe → rank → select.

Uses only the context provider registry and normalized article schema.
Memo layer consumes validated NormalizedArticle (converted to NewsItem); no provider-specific logic.
"""

from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from src.config import cfg
from src.models.news import NormalizedArticle, ValidationStatus
from src.providers.context.registry import (
    get_enabled_context_providers,
    get_context_provider_config,
    get_source_priority_order,
)


# Bank relevance: prefer Saudi banking themes over generic Gulf equity headlines
_SAUDI_BANKING_THEMES = (
    "saudi bank", "credit growth", "deposits", "deposit growth", "funding", "lending",
    "margins", "nim", "rates", "banking outlook", "asset quality", "provision",
    "sama", "company-specific lending", "financing", "loan",
)
_GENERIC_GULF_EQUITY = ("mideast stocks", "gulf equities", "gulf bourses", "gulf markets", "uae leads", "dubai recovers", "most gulf")


def _bank_article_tier(a: NormalizedArticle, is_bank: bool, country: str, company_name: str) -> int:
    """
    For banks (e.g. Saudi): 1=company-specific, 2=Saudi banking sector, 3=Saudi market + issuer, 4=Gulf only.
    Lower tier = higher priority.
    """
    if not is_bank or (country or "").upper() not in ("SA", "SAU"):
        return 4
    h = (a.headline or "").lower()
    snip = (a.snippet or "").lower()
    text = f"{h} {snip}"
    name_parts = [p for p in re.split(r"\s+", (company_name or "").strip()) if len(p) > 1]
    if a.company_specific and name_parts and any(p.lower() in text for p in name_parts):
        return 1
    if a.sector_relevant and any(t in text for t in _SAUDI_BANKING_THEMES):
        return 2
    if any(x in text for x in ("saudi", "tadawul", "riyad", "tasi")) and name_parts and any(p.lower() in text for p in name_parts):
        return 3
    if a.sector_relevant:
        return 3
    return 4


def _bank_theme_score(a: NormalizedArticle) -> int:
    """
    Higher = more relevant for banks. Prefer Saudi credit, deposits, margins, banking outlook;
    deprefer generic Gulf equity-move headlines.
    """
    h = (a.headline or "").lower()
    s = (a.snippet or "").lower()
    text = f"{h} {s}"
    score = 5
    if any(t in text for t in _SAUDI_BANKING_THEMES):
        score += 3
    if any(t in text for t in _GENERIC_GULF_EQUITY) and not any(t in text for t in _SAUDI_BANKING_THEMES):
        score -= 2
    if "saudi bank" in text or "credit growth" in text or "deposits" in text:
        score += 2
    return max(0, min(10, score))


def _build_sector_queries(is_bank: bool, country: str) -> list[str]:
    """Sector-relevant search queries (e.g. Saudi banks)."""
    out: list[str] = []
    if is_bank and (country or "").upper() in ("SA", "SAU"):
        out.extend([
            "Saudi bank credit growth",
            "Saudi banks deposits lending",
            "Saudi market banking",
        ])
    return out


def _canonical_url(url: str) -> str:
    if not url or not url.startswith("http"):
        return url or ""
    try:
        p = urlparse(url.strip())
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path.rstrip("/") or "/"
        q = parse_qs(p.query, keep_blank_values=False)
        for k in list(q):
            if k.lower() in ("utm_source", "utm_medium", "utm_campaign", "ref", "fbclid"):
                del q[k]
        new_query = urlencode(q, doseq=True) if q else ""
        return urlunparse((p.scheme.lower(), netloc, path, p.params, new_query, ""))
    except Exception:
        return url or ""


def _normalize_headline(h: str) -> str:
    if not h:
        return ""
    h = h.strip().lower()
    h = re.sub(r"[^\w\s]", " ", h)
    h = re.sub(r"\s+", " ", h)
    return h.strip()


_PIPELINE_URL_BLOCKLIST = (
    "/terms", "/terms-of-service", "/terms-and-conditions",
    "/privacy", "/privacy-policy", "/cookie-policy",
    "/about-us", "/about/", "/contact-us", "/contact/",
    "/information/", "/advertise", "/newsletter",
    "/login", "/register", "/registration/", "/sign-in", "/sign-up",
    "/subscribe", "/subscription", "/membership",
    "/sitemap", "/feed/",
    "/comments-policy", "/comments/",
)

# Domains never allowed as news sources (messaging, social, etc.)
_BLOCKED_NEWS_DOMAINS = (
    "t.me",
    "telegram.org",
    "telegram.dog",
    "telegram.me",
)

_PIPELINE_HEADLINE_BLOCKLIST = (
    "terms & conditions", "terms of service", "privacy policy",
    "cookie policy", "about us", "comments policy", "advertise with",
)


def _is_blocked_news_domain(url: str) -> bool:
    """True if URL belongs to a blocked domain (e.g. Telegram)."""
    if not url or not url.strip().startswith("http"):
        return False
    try:
        netloc = urlparse(url.strip()).netloc.lower()
        if not netloc:
            return False
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return any(
            netloc == d or netloc.endswith("." + d)
            for d in _BLOCKED_NEWS_DOMAINS
        )
    except Exception:
        return False


def _is_junk_url(url: str, headline: str) -> bool:
    """Pipeline-level filter: reject non-article URLs that slip past provider rules."""
    u = (url or "").strip().lower()
    h = (headline or "").strip().lower()
    if _is_blocked_news_domain(url or ""):
        return True
    if any(pat in u for pat in _PIPELINE_URL_BLOCKLIST):
        return True
    if any(pat in h for pat in _PIPELINE_HEADLINE_BLOCKLIST):
        return True
    return False


def _apply_basic_validation(articles: list[NormalizedArticle]) -> None:
    """Set validation_status to BASIC_VALID where headline + url + publisher present."""
    for a in articles:
        if _is_junk_url(a.url or "", a.headline or ""):
            a.validation_status = ValidationStatus.INVALID
            continue
        if (a.headline or "").strip() and (a.url or "").strip().startswith("http") and (a.publisher or a.provider or "").strip():
            if a.validation_status == ValidationStatus.INVALID:
                a.validation_status = ValidationStatus.BASIC_VALID


# Article-page date sources → high confidence; search/snippet → medium (no article fetch required)
_ARTICLE_PAGE_DATE_SOURCES = frozenset(("time_tag", "meta_article", "json_ld", "meta_date", "meta_generic", "article_page"))
_SEARCH_SNIPPET_DATE_SOURCES = frozenset(("search_card", "snippet", "url_fallback"))


def _apply_final_validation(articles: list[NormalizedArticle]) -> None:
    """
    High/medium/low policy:
    - final_valid_high = article-page date found (time_tag, meta, JSON-LD).
    - final_valid_medium = reliable search-card or snippet date.
    - final_valid_low = no date but headline+URL are valid (still usable, ranked last).
    """
    for a in articles:
        if a.validation_status not in (ValidationStatus.BASIC_VALID, ValidationStatus.ENRICHED):
            continue
        if a.publication_date is None:
            a.validation_status = ValidationStatus.FINAL_VALID_LOW
            a.date_confidence = "unknown"
            continue
        src = (a.date_source or "").strip().lower()
        if src in _ARTICLE_PAGE_DATE_SOURCES:
            a.validation_status = ValidationStatus.FINAL_VALID_HIGH
            a.date_confidence = a.date_confidence or "high"
        elif src in _SEARCH_SNIPPET_DATE_SOURCES or a.date_from_search_card:
            a.validation_status = ValidationStatus.FINAL_VALID_MEDIUM
            a.date_confidence = a.date_confidence or "medium"
        else:
            a.validation_status = ValidationStatus.FINAL_VALID_MEDIUM
            a.date_confidence = a.date_confidence or "medium"


_VALIDATION_RANK = {
    ValidationStatus.FINAL_VALID_HIGH: 0,
    ValidationStatus.FINAL_VALID_MEDIUM: 1,
    ValidationStatus.FINAL_VALID_LOW: 2,
}


def _dedupe_across_providers(
    articles: list[NormalizedArticle],
    priority_order: list[str],
) -> list[NormalizedArticle]:
    """Dedupe by canonical URL and (provider, norm_headline); keep best by priority, newest, company_specific, date_confidence."""
    if not articles:
        return []
    order_idx = {p: i for i, p in enumerate(priority_order)}

    def sort_key(a: NormalizedArticle) -> tuple:
        vr = _VALIDATION_RANK.get(a.validation_status, 3)
        return (
            order_idx.get(a.provider, 999),
            vr,
            -(a.publication_date or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            not a.company_specific,
            not a.sector_relevant,
            0 if (a.date_confidence or "").lower() == "high" else 1,
        )

    sorted_articles = sorted(articles, key=sort_key)
    seen_canonical: set[str] = set()
    seen_provider_headline: set[tuple[str, str]] = set()
    kept: list[NormalizedArticle] = []
    for a in sorted_articles:
        canon = _canonical_url(a.url or "")
        ph = (a.provider, _normalize_headline(a.headline or ""))
        if canon and canon in seen_canonical:
            continue
        if ph[1] and ph in seen_provider_headline:
            continue
        kept.append(a)
        if canon:
            seen_canonical.add(canon)
        if ph[1]:
            seen_provider_headline.add(ph)
    return kept


def _rank_and_select(
    articles: list[NormalizedArticle],
    company_name: str,
    is_bank: bool,
    country: str,
    max_n: int = 7,
    min_n: int = 3,
) -> list[NormalizedArticle]:
    """
    Rank: for banks use tier and theme score; otherwise prefer high > medium > low
    confidence, company_specific first, then newest. Returns up to max_n articles.
    """
    def key(a: NormalizedArticle):
        vr = _VALIDATION_RANK.get(a.validation_status, 3)
        if is_bank and (country or "").upper() in ("SA", "SAU"):
            tier = _bank_article_tier(a, is_bank, country, company_name)
            theme = _bank_theme_score(a)
            return (
                tier,
                -theme,
                vr,
                -(a.publication_date or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            )
        return (
            vr,
            not a.company_specific,
            not a.sector_relevant,
            -(a.publication_date or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        )
    sorted_articles = sorted(articles, key=key)
    n = min(max_n, max(min_n, len(sorted_articles)))
    return sorted_articles[:n]


_RELEVANCE_REASONS = {1: "company_specific", 2: "Saudi_banking_sector", 3: "Saudi_market_issuer", 4: "Gulf_market"}


def _ensure_extracted_fact_and_relevance(
    a: NormalizedArticle,
    company_name: str,
    is_bank: bool,
    country: str,
) -> None:
    """For every selected article: set extracted_fact (from snippet/headline if empty) and relevance_reason."""
    if not (a.extracted_fact or "").strip():
        raw = (a.snippet or "").strip() or (a.headline or "").strip()
        if raw:
            # First sentence or first 300 chars
            first_sent = re.split(r"(?<=[.!?])\s+", raw, maxsplit=1)[0].strip()
            a.extracted_fact = (first_sent[:300] + ("…" if len(first_sent) > 300 else "")) if first_sent else raw[:300]
        else:
            a.extracted_fact = (a.headline or "")[:300]
    tier = _bank_article_tier(a, is_bank, country, company_name)
    a.relevance_reason = _RELEVANCE_REASONS.get(tier, "Gulf_market")


def run(
    company_name: str,
    is_bank: bool = False,
    country: str = "",
    since: datetime | None = None,
) -> tuple[list[NormalizedArticle], dict[str, Any]]:
    """
    Run the shared pipeline. Returns (final validated articles for memo, qa_data).
    Only articles with validation_status FINAL_VALID_HIGH or FINAL_VALID_MEDIUM are returned.
    """
    try:
        recent_days = int(cfg().get("news", {}).get("recent_days", 30))
    except Exception:
        recent_days = 30
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=recent_days)
    try:
        max_enrich = int(cfg().get("news", {}).get("recent_context_enrichment_max_fetch", 15))
    except Exception:
        max_enrich = 15

    # Per-country news: only use providers that are global or list this country (e.g. Zawya for SA)
    providers = get_enabled_context_providers(country=country or None)
    priority_order = get_source_priority_order(country=country or None)
    allowed_sources = [s.strip().lower() for s in (cfg().get("news", {}).get("recent_context_sources") or ["reuters", "zawya"])]

    all_articles: list[NormalizedArticle] = []
    query_log: list[dict] = []

    for prov in providers:
        pid = prov.provider_id
        if pid not in allowed_sources:
            continue
        conf = get_context_provider_config(pid)
        try:
            if conf.get("allowed_for_company_facts", True):
                company_items = prov.search_company_articles(company_name, since=since, max_items=20)
                query_log.append({"query": company_name, "source": pid, "count": len(company_items)})
                all_articles.extend(company_items)
            if conf.get("allowed_for_sector_context", True):
                sector_q = _build_sector_queries(is_bank, country)
                if sector_q:
                    sector_items = prov.search_sector_articles(sector_q, since=since, max_items_per_query=10)
                    for q in sector_q:
                        query_log.append({"query": q, "source": pid, "count": len([i for i in sector_items if i.sector_relevant])})
                    all_articles.extend(sector_items)
        except Exception:
            query_log.append({"query": company_name, "source": pid, "status": "error", "count": 0})

    _apply_basic_validation(all_articles)
    basic_count = sum(1 for a in all_articles if a.validation_status == ValidationStatus.BASIC_VALID)
    candidate_has_date_before = sum(1 for a in all_articles if a.validation_status == ValidationStatus.BASIC_VALID and a.publication_date is not None)
    need_date = [a for a in all_articles if a.validation_status in (ValidationStatus.BASIC_VALID,) and a.publication_date is None]
    enrichment_log: list[dict] = []
    for a in need_date[:max_enrich]:
        try:
            prov = next((p for p in providers if p.provider_id == a.provider), None)
            if prov:
                prov.enrich_metadata(a)
                enrichment_log.append({
                    "url": (a.url or "").strip(),
                    "date_parse_attempted": True,
                    "date_parse_source": a.date_source or "",
                    "date_parse_success": a.publication_date is not None,
                })
        except Exception:
            enrichment_log.append({"url": (getattr(a, "url", None) or "").strip(), "date_parse_attempted": True, "date_parse_success": False})

    _apply_final_validation(all_articles)
    _VALID_STATUSES = (ValidationStatus.FINAL_VALID_HIGH, ValidationStatus.FINAL_VALID_MEDIUM, ValidationStatus.FINAL_VALID_LOW)
    final = [a for a in all_articles if a.validation_status in _VALID_STATUSES]
    # Relevance filter: prefer articles that mention the company; keep sector articles too
    company_tokens = {w.lower() for w in re.split(r"\s+", company_name.strip()) if len(w) > 2}
    company_tokens -= {"the", "and", "for", "inc", "ltd", "limited", "corp", "corporation", "group", "holding", "plc", "company"}
    if company_tokens:
        relevant = []
        for a in final:
            text = f"{(a.headline or '').lower()} {(a.snippet or '').lower()} {(a.publisher or '').lower()}"
            if any(tok in text for tok in company_tokens):
                a.company_specific = True
                relevant.append(a)
            elif a.sector_relevant:
                relevant.append(a)
        if len(relevant) >= 3:
            final = relevant
    deduped = _dedupe_across_providers(final, priority_order)
    selected = _rank_and_select(deduped, company_name, is_bank, country)

    # Require extracted_fact and relevance_reason for every selected article
    for a in selected:
        _ensure_extracted_fact_and_relevance(a, company_name, is_bank, country)
    candidate_has_extracted_fact = sum(1 for a in selected if (a.extracted_fact or "").strip())

    # Top 10 rejected candidates debug export
    by_url = {e.get("url", ""): e for e in enrichment_log if e.get("url")}
    rejected = [a for a in all_articles if a.validation_status == ValidationStatus.INVALID and ((a.headline or "").strip() or (a.url or "").strip())]
    rejected_candidates_top_10: list[dict] = []
    for a in rejected[:10]:
        url = (a.url or "").strip()
        log_entry = by_url.get(url, {})
        rejected_candidates_top_10.append({
            "provider": a.provider or "",
            "headline": (a.headline or "")[:120],
            "url": url[:200],
            "search_card_date_found": a.date_from_search_card,
            "article_fetch_succeeded": bool(log_entry.get("date_parse_success")),
            "date_selectors_tried": log_entry.get("date_parse_source") or ("article_page" if log_entry.get("date_parse_attempted") else "none"),
            "rejection_reason": "missing_publication_date",
        })

    date_parse_attempted = len(enrichment_log)
    date_parse_success = sum(1 for e in enrichment_log if e.get("date_parse_success"))
    date_parse_sources = [e.get("date_parse_source") or "" for e in enrichment_log if e.get("date_parse_source")]

    # Per-article QA: date_source, date_confidence, extracted_fact, relevance_reason
    recent_context_articles_qa = [
        {
            "headline": (a.headline or "")[:100],
            "url": (a.url or "")[:150],
            "date_source": a.date_source or "",
            "date_confidence": a.date_confidence or "",
            "extracted_fact": (a.extracted_fact or "")[:300],
            "relevance_reason": a.relevance_reason or "",
        }
        for a in selected
    ]

    qa_data: dict[str, Any] = {
        "recent_context_query_log": query_log,
        "recent_context_candidate_count": basic_count,
        "recent_context_valid_count": len(selected),
        "recent_context_rejected_reasons": ["missing_publication_date"] * max(0, basic_count - len(final)),
        "candidate_valid_basic": basic_count > 0,
        "candidate_has_date_before_enrichment": candidate_has_date_before,
        "candidate_has_extracted_fact": candidate_has_extracted_fact,
        "final_article_valid_count": len(selected),
        "date_parse_attempted": date_parse_attempted,
        "date_parse_source": date_parse_sources[:20],
        "date_parse_success": date_parse_success,
        "candidates_rejected_for_missing_date": max(0, basic_count - len(final)),
        "candidates_recovered_after_article_fetch": date_parse_success,
        "recent_context_enrichment_log": enrichment_log[:30],
        "rejected_candidates_top_10": rejected_candidates_top_10,
        "recent_context_articles_qa": recent_context_articles_qa,
    }
    return selected, qa_data
