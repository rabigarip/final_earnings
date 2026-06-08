"""
Entity resolution and post-resolution validation for MarketScreener.

Canonical flow:
  1. Input key is yfinance ticker only.
  2. Resolve ticker → one exact ISIN via identifier store (company_master).
  3. Use ISIN as primary MarketScreener search key (search/?q={ISIN}).
  4. Cache resolved MarketScreener company URL only after validating candidate
     against expected security (company name, country, exchange).
  5. When cached URL is stale or redirects to homepage, invalidate and re-resolve.
  6. DB write protection: do not replace a known-good mapping with a lower-confidence candidate.
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from src.storage.db import (
    load_company,
    update_company_marketscreener,
    invalidate_marketscreener_cache as db_invalidate_marketscreener_cache,
    reject_marketscreener_candidate as db_reject_marketscreener_candidate,
)
from src.providers.marketscreener_pages import list_marketscreener_candidates_for_isin
from src.providers.marketscreener import _fetch_page_with_diagnostics, _is_homepage_detailed


# ═══════════════════════════════════════════════════════════════════════════════
# Candidate validation (merged from marketscreener_validation.py)
# ═══════════════════════════════════════════════════════════════════════════════

CONFIDENCE_THRESHOLD_OVERWRITE = 0.8
CONFIDENCE_THRESHOLD_ACCEPT = 0.6


@dataclass
class ValidationResult:
    valid: bool
    confidence: float
    rejection_reason: str = ""


SABIC_NAME_MARKERS = ("sabic", "saudi basic industries", "saudi basic industry", "basic industries corp")
SAUDI_COUNTRY_MARKERS = ("saudi", "saudi arabia", "tadawul", "riyadh")
IMPLAUSIBLE_SLUG_PREFIXES_FOR_SAUDI = ("amd-", "apple-", "microsoft-", "advanced micro", "alibaba-", "al-rajhi")


def _normalize_str(s: str) -> str:
    return (s or "").strip().lower()


def _page_text(soup) -> str:
    if soup is None:
        return ""
    return soup.get_text(" ", strip=True).lower()


def _page_title(soup) -> str:
    if soup is None:
        return ""
    t = soup.find("title")
    return _normalize_str(t.get_text(strip=True)) if t else ""


def _has_isin_on_page(page_text: str, isin: str) -> bool:
    if not isin or not page_text:
        return False
    clean_isin = (isin or "").strip().upper()
    if len(clean_isin) < 9:
        return False
    return clean_isin in page_text.upper() or clean_isin.replace(" ", "") in page_text.upper().replace(" ", "")


def validate_candidate_page(
    company: dict,
    candidate_slug: str,
    candidate_url: str,
    *,
    cache_name: str | None = None,
) -> ValidationResult:
    company_name = _normalize_str(company.get("company_name") or "")
    company_name_long = _normalize_str(company.get("company_name_long") or "")
    country = _normalize_str(company.get("country") or "")
    exchange = _normalize_str(company.get("exchange") or "")
    isin = (company.get("isin") or "").strip()
    ticker = (company.get("ticker") or "").strip()

    if not candidate_url:
        return ValidationResult(valid=False, confidence=0.0, rejection_reason="empty_candidate_url")

    name = cache_name or f"validate_{ticker.replace('.', '_')}_{candidate_slug[:30]}"
    result = _fetch_page_with_diagnostics(candidate_url, name)

    if result.soup is None:
        return ValidationResult(valid=False, confidence=0.0, rejection_reason="fetch_failed_or_non_200")

    is_home, rule = _is_homepage_detailed(result.soup)
    if is_home:
        return ValidationResult(valid=False, confidence=0.0, rejection_reason=f"page_is_homepage_or_interstitial ({rule})")

    text = _page_text(result.soup)
    title = _page_title(result.soup)
    combined = f"{title} {text}"

    # Strong match: ISIN appears on the quote page (slug often omits English tokens for Asian listings).
    if isin and _has_isin_on_page(combined, isin):
        if country == "sa" or "saudi" in country or "tadawul" in exchange:
            if not any(m in combined for m in SAUDI_COUNTRY_MARKERS):
                return ValidationResult(
                    valid=False, confidence=0.0,
                    rejection_reason="expected_saudi_tadawul_not_found_on_page",
                )
        return ValidationResult(valid=True, confidence=0.88, rejection_reason="")

    if country == "sa" or "saudi" in country or "tadawul" in exchange:
        if not any(m in combined for m in SAUDI_COUNTRY_MARKERS):
            return ValidationResult(valid=False, confidence=0.0, rejection_reason="expected_saudi_tadawul_not_found_on_page")
        slug_lower = (candidate_slug or "").lower()
        if any(slug_lower.startswith(p) or p in slug_lower for p in IMPLAUSIBLE_SLUG_PREFIXES_FOR_SAUDI):
            return ValidationResult(valid=False, confidence=0.0, rejection_reason=f"slug_implausible_for_saudi_entity ({candidate_slug})")

    if "sabic" in company_name or "sabic" in company_name_long or "2010.sr" in ticker.lower():
        if not any(m in combined for m in SABIC_NAME_MARKERS):
            return ValidationResult(valid=False, confidence=0.0, rejection_reason="expected_sabic_or_saudi_basic_industries_not_found_on_page")

    name_words = set(re.findall(r"[a-z0-9]+", company_name)) | set(re.findall(r"[a-z0-9]+", company_name_long))
    name_words -= {"", "inc", "ltd", "limited", "corp", "corporation", "group", "holding", "co", "plc"}
    if name_words and not any(w in combined for w in name_words if len(w) > 2):
        return ValidationResult(valid=False, confidence=0.0, rejection_reason="company_name_mismatch_no_overlap")

    slug_lower = _normalize_str(candidate_slug).replace("-", " ")
    significant_name_words = {w for w in name_words if len(w) > 2}
    if significant_name_words and not any(w in slug_lower for w in significant_name_words):
        return ValidationResult(valid=False, confidence=0.0, rejection_reason=f"slug_has_no_company_name_overlap ({candidate_slug})")

    confidence = 0.5
    if any(m in combined for m in SABIC_NAME_MARKERS) or any(m in combined for m in SAUDI_COUNTRY_MARKERS):
        confidence += 0.2
    if company_name and any(w in combined for w in company_name.split() if len(w) > 2):
        confidence += 0.15
    if isin and _has_isin_on_page(combined, isin):
        confidence += 0.15
    if result.classification == "valid_consensus" or (getattr(result, "has_number_of_analysts", False) or getattr(result, "has_mean_consensus", False)):
        confidence += 0.1
    confidence = min(1.0, confidence)

    return ValidationResult(valid=True, confidence=confidence, rejection_reason="")


def should_overwrite_existing_mapping(current_row: dict, candidate_confidence: float) -> tuple[bool, str]:
    status = (current_row.get("marketscreener_status") or "").strip().lower()
    existing_url = (current_row.get("marketscreener_company_url") or "").strip()
    if not existing_url or status not in ("ok",):
        return True, "no_known_good_mapping"
    if candidate_confidence < CONFIDENCE_THRESHOLD_OVERWRITE:
        return False, f"candidate_confidence_{candidate_confidence:.2f}_below_threshold_{CONFIDENCE_THRESHOLD_OVERWRITE}"
    return True, "above_threshold"


# ═══════════════════════════════════════════════════════════════════════════════
# Entity resolution
# ═══════════════════════════════════════════════════════════════════════════════

MS_AVAILABILITY_OK = "ok"
MS_AVAILABILITY_WRONG_ENTITY = "wrong_entity"
MS_AVAILABILITY_STALE_URL = "stale_url"
MS_AVAILABILITY_SOURCE_REDIRECT = "source_redirect"
MS_AVAILABILITY_UNRESOLVED = "unresolved"


def get_marketscreener_availability(company: dict) -> str:
    status = (company.get("marketscreener_status") or "").strip().lower()
    url = (company.get("marketscreener_company_url") or "").strip()
    slug = (company.get("marketscreener_id") or "").strip()
    if status == "ok" and (url or slug):
        return MS_AVAILABILITY_OK
    if status == "source_redirect":
        return MS_AVAILABILITY_SOURCE_REDIRECT
    if status == "stale":
        return MS_AVAILABILITY_STALE_URL
    if status in ("needs_review", "invalid"):
        return MS_AVAILABILITY_WRONG_ENTITY
    if not url and not slug:
        return MS_AVAILABILITY_UNRESOLVED
    return MS_AVAILABILITY_UNRESOLVED


def get_effective_marketscreener_slug(company: dict) -> str:
    url = (company.get("marketscreener_company_url") or "").strip()
    status = (company.get("marketscreener_status") or "").strip().lower()
    if url and status == "ok":
        m = re.search(r"/quote/stock/([^/]+)/?", url)
        if m:
            return m.group(1)
    return (company.get("marketscreener_id") or "").strip()


def ensure_marketscreener_cached(ticker: str, company: dict | None = None) -> dict | None:
    row = company if company is not None else load_company(ticker)
    if row is None:
        return None
    isin = (row.get("isin") or "").strip()
    if not isin:
        return row
    url = (row.get("marketscreener_company_url") or "").strip()
    status = (row.get("marketscreener_status") or "").strip().lower()
    if url and status == "ok":
        return row
    candidates = list_marketscreener_candidates_for_isin(isin, max_results=8)
    if not candidates:
        return row
    last_reason = ""
    for slug, company_url in candidates:
        cache_name = f"validate_{ticker.replace('.', '_')}_{slug[:24]}"
        validation = validate_candidate_page(row, slug, company_url, cache_name=cache_name)
        if not validation.valid:
            last_reason = validation.rejection_reason or "validation_failed"
            continue
        if validation.confidence < CONFIDENCE_THRESHOLD_ACCEPT:
            last_reason = f"confidence_{validation.confidence:.2f}_below_accept_threshold"
            continue
        allow, overwrite_reason = should_overwrite_existing_mapping(row, validation.confidence)
        if not allow:
            last_reason = overwrite_reason
            continue
        now = datetime.now(timezone.utc).isoformat()
        update_company_marketscreener(
            ticker=ticker, marketscreener_company_url=company_url,
            marketscreener_symbol=row.get("ticker") or ticker,
            marketscreener_status="ok", last_verified=now, marketscreener_id=slug,
        )
        return load_company(ticker)
    if last_reason:
        db_reject_marketscreener_candidate(ticker, reason=last_reason[:500], status="needs_review")
    return load_company(ticker)


def invalidate_marketscreener_cache(ticker: str) -> None:
    db_invalidate_marketscreener_cache(ticker)


def re_resolve_marketscreener_after_invalidate(ticker: str) -> dict | None:
    invalidate_marketscreener_cache(ticker)
    return ensure_marketscreener_cached(ticker, company=None)
