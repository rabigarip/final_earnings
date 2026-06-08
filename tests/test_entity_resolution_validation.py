"""
Regression tests for post-resolution entity validation and DB write protection.

- SABIC (2010.SR): when re-resolution returns an obviously wrong entity (e.g. AMD),
  the new mapping must be rejected and the cached SABIC mapping must remain unchanged.

- Stage 4 source_redirect: availability is distinct from wrong_entity/stale/unresolved.
"""

import pytest
from unittest.mock import patch

from bs4 import BeautifulSoup

from src.storage.db import init_db, seed_companies, load_company, invalidate_marketscreener_cache, set_marketscreener_source_redirect
from src.services.entity_resolution import (
    ensure_marketscreener_cached,
    get_marketscreener_availability,
    MS_AVAILABILITY_SOURCE_REDIRECT,
    MS_AVAILABILITY_OK,
    MS_AVAILABILITY_WRONG_ENTITY,
    MS_AVAILABILITY_UNRESOLVED,
    MS_AVAILABILITY_STALE_URL,
)
from src.providers.marketscreener import FetchResult


SABIC_TICKER = "2010.SR"
EXPECTED_SABIC_SLUG = "SABIC-6493058"
AMD_SLUG = "AMD-ADVANCED-MICRO-DEVICE-19475876"
AMD_URL = "https://www.marketscreener.com/quote/stock/AMD-ADVANCED-MICRO-DEVICE-19475876/"


@pytest.fixture
def db_with_sabic():
    """Ensure DB has SABIC (2010.SR) with correct cached slug."""
    init_db()
    seed_companies()
    row = load_company(SABIC_TICKER)
    assert row is not None, "SABIC must be in company_master"
    assert row.get("marketscreener_id") == EXPECTED_SABIC_SLUG, "Seed must provide correct SABIC slug"
    yield
    # teardown: none needed


def test_sabic_re_resolution_rejects_wrong_entity_amd(db_with_sabic):
    """
    When SABIC re-resolution returns AMD (wrong entity), validation must reject it
    and the cached SABIC mapping must remain unchanged.
    """
    # 1. ISIN search lists wrong entity first (AMD) — same as first row in MS results table
    def mock_list_candidates(isin: str, max_results: int = 8):
        if isin and isin.strip():
            return [(AMD_SLUG, AMD_URL)]
        return []

    # 2. Fetch returns a page that looks like a stock page but has no SABIC/Saudi content
    def mock_fetch_with_diagnostics(url: str, cache_name: str):
        html = """<html><head><title>AMD - Advanced Micro Devices</title>
        <link rel="canonical" href="https://www.marketscreener.com/quote/stock/AMD-ADVANCED-MICRO-DEVICE-19475876/"/>
        </head><body>Advanced Micro Devices Inc. United States. Technology.</body></html>"""
        return FetchResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            title="AMD - Advanced Micro Devices",
            canonical=url.rstrip("/"),
            first_1000_chars="Advanced Micro Devices United States Technology",
            classification="",
            rule_fired="",
            has_number_of_analysts=False,
            has_mean_consensus=False,
            soup=BeautifulSoup(html, "lxml"),
            from_cache=False,
        )

    with patch(
        "src.services.entity_resolution.list_marketscreener_candidates_for_isin",
        side_effect=mock_list_candidates,
    ), patch(
        "src.services.entity_resolution._fetch_page_with_diagnostics",
        side_effect=mock_fetch_with_diagnostics,
    ):
        invalidate_marketscreener_cache(SABIC_TICKER)
        ensure_marketscreener_cached(SABIC_TICKER, company=None)

    row = load_company(SABIC_TICKER)
    assert row is not None
    # Cached SABIC mapping must remain unchanged (rejected candidate not written)
    assert row.get("marketscreener_id") == EXPECTED_SABIC_SLUG, (
        "Wrong entity (AMD) must not overwrite SABIC slug"
    )
    assert (row.get("marketscreener_company_url") or "").find(EXPECTED_SABIC_SLUG) >= 0 or (
        row.get("marketscreener_company_url") or ""
    ).find("SAUDI") >= 0, "SABIC URL must be preserved or still point to SABIC"
    # Rejection must be recorded
    assert (row.get("marketscreener_status") or "").strip().lower() in (
        "needs_review",
        "stale",
    ), "Status should be needs_review (rejected) or stale (invalidated)"


def test_sabic_validation_rejects_implausible_slug_even_with_fake_saudi_page(db_with_sabic):
    """
    For Saudi entities, slug plausibility is checked: AMD-* must be rejected.
    """
    def mock_list_candidates(isin: str, max_results: int = 8):
        if isin and isin.strip():
            return [(AMD_SLUG, AMD_URL)]
        return []

    # Page that mentions Saudi (so name/country could pass) but slug is still AMD
    def mock_fetch_with_diagnostics(url: str, cache_name: str):
        html = """<html><head><title>AMD</title></head>
        <body>Saudi Arabia Tadawul SABIC Saudi Basic Industries. Some other text.</body></html>"""
        return FetchResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            soup=BeautifulSoup(html, "lxml"),
            from_cache=False,
        )

    with patch(
        "src.services.entity_resolution.list_marketscreener_candidates_for_isin",
        side_effect=mock_list_candidates,
    ), patch(
        "src.services.entity_resolution._fetch_page_with_diagnostics",
        side_effect=mock_fetch_with_diagnostics,
    ):
        invalidate_marketscreener_cache(SABIC_TICKER)
        ensure_marketscreener_cached(SABIC_TICKER, company=None)

    row = load_company(SABIC_TICKER)
    assert row.get("marketscreener_id") == EXPECTED_SABIC_SLUG
    assert (row.get("marketscreener_status") or "").strip().lower() in ("needs_review", "stale")


def test_marketscreener_availability_distinguishes_source_redirect(db_with_sabic):
    """source_redirect is a distinct category; SABIC is not misclassified as unresolved or wrong_entity."""
    row = load_company(SABIC_TICKER)
    assert get_marketscreener_availability(row) in (MS_AVAILABILITY_OK, MS_AVAILABILITY_UNRESOLVED, MS_AVAILABILITY_STALE_URL)
    set_marketscreener_source_redirect(SABIC_TICKER)
    row = load_company(SABIC_TICKER)
    assert row.get("marketscreener_id") == EXPECTED_SABIC_SLUG
    assert get_marketscreener_availability(row) == MS_AVAILABILITY_SOURCE_REDIRECT
    assert get_marketscreener_availability(row) != MS_AVAILABILITY_WRONG_ENTITY
    assert get_marketscreener_availability(row) != MS_AVAILABILITY_OK
