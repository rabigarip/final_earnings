from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup


def _fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "marketscreener" / "ARAMCO-103505448"


def test_marketscreener_parsers_match_saved_html(monkeypatch):
    """
    Regression: parse saved MarketScreener HTML fixtures and assert key numeric fields.

    This validates our scrapers against a known-good page snapshot without relying on live HTTP
    (MarketScreener can block/captcha in CI).
    """
    import src.providers.marketscreener_pages as mp
    import src.providers.marketscreener_consensus as mc

    base = "https://www.marketscreener.com/quote/stock/ARAMCO-103505448"
    fix = _fixtures_dir()
    assert fix.is_dir()

    url_to_file = {
        f"{base}/": "summary.html",
        f"{base}/valuation/": "valuation.html",
        f"{base}/calendar/": "calendar.html",
    }

    def fake_fetch_page(url: str, _cache_slug: str):
        fn = url_to_file.get(url)
        if not fn:
            return None, [f"no_fixture_for_url:{url}"]
        html = (fix / fn).read_text(encoding="utf-8")
        return BeautifulSoup(html, "lxml"), []

    monkeypatch.setattr(mp, "_fetch_page", fake_fetch_page)

    # Consensus module uses requests.get directly; patch it to return fixture HTML.
    consensus_html = (fix / "consensus.html").read_text(encoding="utf-8")

    def fake_requests_get(url, headers=None, timeout=None):
        return SimpleNamespace(status_code=200, text=consensus_html)

    monkeypatch.setattr(mc.requests, "get", fake_requests_get)

    summary, st_s = mp.fetch_summary_page(base, cache_key_prefix="x")
    assert st_s.status in ("success", "partial")
    # Must not be cross-company garbage: fields should be present on Aramco snapshot.
    assert summary.get("consensus_rating") is not None
    assert summary.get("analyst_count") is not None

    cons, st_c = mp.fetch_consensus_summary(base, cache_key_prefix="x")
    assert st_c.status in ("success", "partial")
    assert cons.get("consensus_rating") is not None
    assert cons.get("analyst_count") is not None
    assert cons.get("average_target_price") is not None

    val, st_v = mp.fetch_valuation_multiples(base, cache_key_prefix="x")
    assert st_v.status == "success"
    assert (val.get("periods") or [])[:3] == ["FY2021", "FY2022", "FY2023"]
    pe = val.get("pe") or []
    assert len(pe) >= 6
    # Snapshot sanity check: first year P/E should match fixture parse.
    assert pe[0] == 18.1

    cal, st_cal = mp.fetch_calendar_events(base, cache_key_prefix="x")
    assert st_cal.status == "success"
    assert cal.get("next_expected_earnings_date") == "2026-05-10"
    qrt = cal.get("quarterly_results_table") or {}
    assert "net_sales" in (qrt.get("metrics") or {})

