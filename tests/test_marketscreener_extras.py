"""
Parser tests for the new MarketScreener fetchers added 2026-05 to close
the PDF-vs-pipeline gap (ratings, sector peers, price performance,
analyst recommendations) and the extended /finances/ rows (EBT,
Interest Paid).

All tests are deterministic, fixture-driven, network-free. The fixtures
live in tests/fixtures/marketscreener/<SLUG>/<page>.html and were
captured against live MS (May 2026) for two issuers:

  * SPINNEYS-1961-HOLDING-PLC-169525612  — UAE retail; reference for
    the user-supplied PDF report.
  * SABIC-6493058                         — Saudi industrial; cross-check
    that the parsers don't over-fit to one ticker's layout.

Each test patches `_fetch_page` so the parser reads the fixture HTML
instead of hitting the network. The patch maps a request URL's last
path segment to the matching fixture filename.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src.providers import marketscreener_pages as ms


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "marketscreener"

_PAGE_NAME_MAP = {
    "ratings":   "ratings",
    "consensus": "consensus",
    "company":   "company",
    "finances":  "finances",
    "valuation": "valuation",
    "sector":    "sector",
    "revisions": "revisions",
}


def _patch_fetch_page(monkeypatch: pytest.MonkeyPatch, slug: str) -> None:
    """Make `marketscreener_pages._fetch_page` read from a fixture dir.

    The MS code calls `_fetch_page(url, cache_slug)` where `url` ends in
    one of {/ratings/, /consensus/, /, ...}. We inspect the last path
    segment, pick the matching fixture, and return BeautifulSoup. URLs
    ending in the slug itself (i.e. the summary page) fall through to
    "summary".
    """
    fixture_dir = FIXTURE_ROOT / slug

    def fake_fetch(url: str, cache_slug: str):
        last = url.rstrip("/").split("/")[-1]
        page_name = _PAGE_NAME_MAP.get(last, "summary")
        fp = fixture_dir / f"{page_name}.html"
        if not fp.exists():
            return None, [f"fixture missing: {fp}"]
        return BeautifulSoup(fp.read_text(encoding="utf-8"), "lxml"), []

    monkeypatch.setattr(ms, "_fetch_page", fake_fetch)


# ─────────────────────────────────────────────────────────────────────────────
# fetch_ratings_page
# ─────────────────────────────────────────────────────────────────────────────


class TestRatingsPage:
    BASE_SPINNEYS = "https://www.marketscreener.com/quote/stock/SPINNEYS-1961-HOLDING-PLC-169525612"
    BASE_SABIC = "https://www.marketscreener.com/quote/stock/SABIC-6493058"

    def test_strengths_and_weaknesses_extracted(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, status = ms.fetch_ratings_page(self.BASE_SPINNEYS)
        assert status.status == "success"
        # 7 strengths and 5 weaknesses match the live HTML snapshot (see PDF).
        assert len(payload["strengths"]) >= 4
        assert len(payload["weaknesses"]) >= 3
        joined = " ".join(payload["strengths"]).lower()
        assert "margins" in joined or "yield" in joined or "valuation" in joined
        joined_w = " ".join(payload["weaknesses"]).lower()
        assert "growth" in joined_w or "valuation" in joined_w

    def test_composite_ratings_match_pdf_values(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_ratings_page(self.BASE_SPINNEYS)
        composite = payload["composite_ratings"]
        # PDF reference: Trader 70%, Investor 93%, Global 77%, Quality 91%.
        assert composite["Trader"] == 70
        assert composite["Investor"] == 93
        assert composite["Global"] == 77
        assert composite["Quality"] == 91

    def test_peer_esg_table_includes_subject_and_letters(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_ratings_page(self.BASE_SPINNEYS)
        peers = payload["peer_esg"]
        # Subject is the first row and has its known mcap.
        assert peers, "expected at least one peer row"
        assert peers[0]["name"] == "SPINNEYS 1961 HOLDING PLC"
        assert peers[0]["market_cap"] == "1.14B"
        # ESG letters appear for at least one peer.
        letters = {p["esg_msci"] for p in peers if p["esg_msci"]}
        assert letters & {"AAA", "AA", "A", "BBB", "BB", "B"}
        # Investor rating star comes through as 0..100 int.
        rating_pcts = [p["rating_pct"] for p in peers if p["rating_pct"] is not None]
        assert rating_pcts and all(0 <= r <= 100 for r in rating_pcts)

    def test_ratings_works_for_second_ticker(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SABIC-6493058")
        payload, status = ms.fetch_ratings_page(self.BASE_SABIC)
        assert status.status == "success"
        # SABIC has known composite ratings in the fixture (Trader 93,
        # Investor 26, Global 31, Quality 22).
        composite = payload["composite_ratings"]
        assert all(0 <= v <= 100 for v in composite.values() if v is not None)
        assert composite["Trader"] is not None
        # SABIC's row in the peer table.
        assert any(p["name"].startswith("SABIC") for p in payload["peer_esg"])


# ─────────────────────────────────────────────────────────────────────────────
# fetch_sector_peers
# ─────────────────────────────────────────────────────────────────────────────


class TestSectorPeers:
    BASE = "https://www.marketscreener.com/quote/stock/SPINNEYS-1961-HOLDING-PLC-169525612"

    def test_subject_row_first_with_pdf_perf_values(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, status = ms.fetch_sector_peers(self.BASE)
        assert status.status == "success"
        rows = payload["rows"]
        assert payload["subject_name"] == "SPINNEYS 1961 HOLDING PLC"
        subject = rows[0]
        # PDF page 5 reference performance values for Spinneys.
        assert subject["change_1d_pct"] == pytest.approx(-0.85, abs=0.01)
        assert subject["change_5d_pct"] == pytest.approx(-3.33, abs=0.01)
        assert subject["change_ytd_pct"] == pytest.approx(-23.18, abs=0.01)
        assert subject["market_cap_usd"] == "1.14B"

    def test_at_least_ten_peers(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_sector_peers(self.BASE)
        # MS sector page lists ~20 peers for global retail.
        assert len(payload["rows"]) >= 10
        # Every row has a name and market cap.
        for r in payload["rows"]:
            assert r["name"]
            assert r["market_cap_usd"]

    def test_dash_values_become_none(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_sector_peers(self.BASE)
        # Spinneys IPO'd in 2024, so 3y/5y/10y bands are "-" in MS.
        subject = payload["rows"][0]
        assert subject["change_3y_pct"] is None
        assert subject["change_5y_pct"] is None
        assert subject["change_10y_pct"] is None


# ─────────────────────────────────────────────────────────────────────────────
# fetch_price_performance
# ─────────────────────────────────────────────────────────────────────────────


class TestPricePerformance:
    BASE = "https://www.marketscreener.com/quote/stock/SPINNEYS-1961-HOLDING-PLC-169525612"

    def test_perf_grid_matches_pdf(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, status = ms.fetch_price_performance(self.BASE)
        assert status.status == "success"
        perf = payload["performance"]
        # PDF reference performance grid (page 5): 1d -0.85%, 1w -3.33%,
        # MTD -1.69%, 1m -6.45%, 3m -26.58%, 6m -27.50%, YTD -23.18%.
        assert perf["perf_1d_pct"] == pytest.approx(-0.85, abs=0.01)
        assert perf["perf_1w_pct"] == pytest.approx(-3.33, abs=0.01)
        assert perf["perf_mtd_pct"] == pytest.approx(-1.69, abs=0.01)
        assert perf["perf_1m_pct"] == pytest.approx(-6.45, abs=0.01)
        assert perf["perf_3m_pct"] == pytest.approx(-26.58, abs=0.01)
        assert perf["perf_6m_pct"] == pytest.approx(-27.50, abs=0.01)
        assert perf["perf_ytd_pct"] == pytest.approx(-23.18, abs=0.01)

    def test_course_extremes_have_low_high(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_price_performance(self.BASE)
        ranges = payload["course_extremes"]
        # We expect at least ytd + 1y to be populated.
        for key in ("range_ytd", "range_1y"):
            entry = ranges[key]
            assert entry is not None
            assert isinstance(entry["low"], float)
            assert isinstance(entry["high"], float)
            assert entry["low"] <= entry["high"]

    def test_recent_quotes_capped_at_ten(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_price_performance(self.BASE)
        quotes = payload["recent_quotes"]
        assert quotes, "expected non-empty recent_quotes table"
        assert len(quotes) <= 10
        # First row has the canonical shape.
        first = quotes[0]
        assert first["date"]
        assert first["price"]
        assert isinstance(first["change_pct"], float) or first["change_pct"] is None


# ─────────────────────────────────────────────────────────────────────────────
# fetch_analyst_recommendations
# ─────────────────────────────────────────────────────────────────────────────


class TestAnalystRecommendations:
    BASE = "https://www.marketscreener.com/quote/stock/SPINNEYS-1961-HOLDING-PLC-169525612"

    def test_broker_actions_extracted(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, status = ms.fetch_analyst_recommendations(self.BASE)
        assert status.status == "success"
        items = payload["items"]
        assert items, "expected at least one broker action"
        # Each item has date + headline + source. Headlines mention real brokers.
        joined = " ".join(i["headline"] for i in items)
        assert "JP Morgan" in joined or "HSBC" in joined or "Citi" in joined

    def test_does_not_match_unrelated_table(self, monkeypatch):
        """Regression: the page <title> contains "Analysts Recommendations",
        which earlier versions of the parser wrongly matched to anchor a
        table lookup. The first <a>/<h*> heading containing the phrase
        should be the actual section."""
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_analyst_recommendations(self.BASE)
        items = payload["items"]
        # the bug returned a single item with date='1.160 AED' (the
        # current price row from the price table).
        assert all(i["date"] != "1.160 AED" for i in items)

    def test_covering_brokers_list(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, _ = ms.fetch_analyst_recommendations(self.BASE)
        brokers = payload["covering_brokers"]
        # MS lists JPMorgan, HSBC, Citigroup, Securities & Investment for Spinneys.
        assert len(brokers) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# fetch_financial_forecast_series — extended for EBT and Interest Paid
# ─────────────────────────────────────────────────────────────────────────────


class TestFinancialForecastExtended:
    BASE_SPINNEYS = "https://www.marketscreener.com/quote/stock/SPINNEYS-1961-HOLDING-PLC-169525612"
    BASE_SABIC = "https://www.marketscreener.com/quote/stock/SABIC-6493058"

    def test_ebt_and_interest_paid_present_for_spinneys(self, monkeypatch):
        _patch_fetch_page(monkeypatch, "SPINNEYS-1961-HOLDING-PLC-169525612")
        payload, status = ms.fetch_financial_forecast_series(self.BASE_SPINNEYS)
        assert status.status == "success"
        ann = payload["annual"]
        assert "interest_paid" in ann
        assert "ebt" in ann
        # Reference values from the user-supplied PDF (page 2):
        #  FY24 Interest Paid -50.98, FY25 -32, FY26 -31.5
        #  FY24 EBT 322.6,    FY25 394.8, FY26 377
        # PDF grid is 2023-2028 (6 columns); FY24 is index 1.
        assert ann["periods"][1] == "FY2024"
        assert ann["interest_paid"][1] == pytest.approx(-50.98, abs=0.01)
        assert ann["interest_paid"][2] == pytest.approx(-32.0, abs=0.5)
        assert ann["ebt"][1] == pytest.approx(322.6, abs=0.5)
        assert ann["ebt"][3] == pytest.approx(377.0, abs=0.5)

    def test_existing_rows_unchanged_for_sabic(self, monkeypatch):
        """Adding EBT/Interest must not clobber net_sales/EBIT/EBITDA/NI."""
        _patch_fetch_page(monkeypatch, "SABIC-6493058")
        payload, status = ms.fetch_financial_forecast_series(self.BASE_SABIC)
        assert status.status in ("success", "partial")
        ann = payload["annual"]
        # Old contract still holds.
        for key in ("periods", "net_sales", "ebitda", "ebit", "net_income", "announcement_dates"):
            assert key in ann
            assert isinstance(ann[key], list)
        # New keys exist with same length as periods (or empty if MS row absent).
        for key in ("interest_paid", "ebt"):
            assert key in ann
            assert isinstance(ann[key], list)
            if ann[key]:
                assert len(ann[key]) == len(ann["periods"])

    def test_quarterly_unchanged(self, monkeypatch):
        """The quarterly block should be untouched by the EBT/Interest change."""
        _patch_fetch_page(monkeypatch, "SABIC-6493058")
        payload, _ = ms.fetch_financial_forecast_series(self.BASE_SABIC)
        q = payload["quarterly"]
        for key in ("periods", "net_sales", "ebit", "ebitda", "net_income", "eps", "announcement_dates"):
            assert key in q
