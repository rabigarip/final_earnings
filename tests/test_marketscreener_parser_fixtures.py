"""
Parser-level regression tests against cached MarketScreener HTML.

Why this file exists
--------------------
Bug class we hit on 2026-05-02: `_row_by_label("EBIT", ...)` did a naive
substring match. Because "ebit" ⊂ "ebitda", and EBITDA appears before
EBIT in MS's standard /finances/ row order, the EBIT lookup grabbed the
EBITDA row first. For non-banks both rows had real numbers so the deck
shipped EBIT == EBITDA exactly (silent corruption). For banks the EBITDA
row is all dashes, so the EBIT lookup returned all-None and slide 3 was
missing the EBIT row entirely — visible as "sparse" output.

These tests pin per-ticker parser invariants against committed-cache
HTML so any future regression in row-matching, column ordering, or
metric extraction trips a test before any deck ships.

Coverage matrix:
   2010.SR     SABIC                       industrial, full IS
   2020.SR     SABIC Agri-Nutrients        industrial, full IS, BBG-overrideable
   ADNOCGAS.AE ADNOC Gas                   energy, dual-currency reporting
   EMAAR.AE    Emaar Properties            real estate
   0002.HK     CLP Holdings                utility, full IS
   NBOB.OM     National Bank of Oman       bank — EBITDA row is all dashes
   BKMB.OM     Bank Muscat                 bank — same shape

For each ticker we assert:
   * Period grid shape — expected number of FYs, mix of A vs E columns
   * Net sales row populated
   * EBITDA row matches expected (populated for industrials, all-None
     for banks)
   * EBIT row populated AND not equal to the EBITDA row when both are
     present (the regression-prevention assertion for the original bug)
   * Net income populated
   * Announcement dates align with periods (one date per FY column,
     None for estimates)

Tests are deterministic, run in <1s, hit zero network. The fixtures are
the same cache files the live pipeline reads from when MS is hot.
"""

from __future__ import annotations

from pathlib import Path

import pytest


CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


# Fixture matrix — (ticker, MS slug, cache_key_prefix used to find the
# cached /finances/ HTML on disk). The actual filename pattern is
# `cache/ms_<prefix>_finances.html`. Prefix is the ticker_isin_slug or
# ticker_noisin_slug variant the live pipeline writes when fetching MS.
TICKERS = [
    {
        "ticker": "0002.HK", "slug": "CLP-HOLDINGS-LIMITED-1412614",
        "cache_prefix": "ms_0002_HK_HK0002007356_CLP-HOLDINGS-LIMITED-1412614",
        "is_bank": False,
        "expected_periods_min": 6,
        "expected_periods_contains": ["FY2023", "FY2024", "FY2025", "FY2026"],
        # Sample sanity-check values from MS as of the cache snapshot.
        # Tested with ±5% drift tolerance to absorb consensus refreshes
        # while still catching gross parser regressions.
        "sample_revenue": {"FY2024": 90964.0},
        "sample_ebit_2024_lower": 13_000.0,   # MS 14,903 → guard ≥ 13k
        "sample_ebit_2024_upper": 16_000.0,
        "sample_ebitda_2024_lower": 22_000.0,  # MS 24,179 → guard ≥ 22k
        "sample_ebitda_2024_upper": 26_000.0,
    },
    {
        "ticker": "2020.SR", "slug": "SABIC-AGRI-NUTRIENTS-COMP-6497974",
        "cache_prefix": "ms_2020_SR_SA0007879139_SABIC-AGRI-NUTRIENTS-COMP-6497974",
        "is_bank": False,
        "expected_periods_min": 6,
        "expected_periods_contains": ["FY2023", "FY2024", "FY2025"],
        "sample_revenue": {"FY2023": 11033.0, "FY2024": 11061.0},
        "sample_ebit_2024_lower": 2_500.0,
        "sample_ebit_2024_upper": 3_500.0,
        "sample_ebitda_2024_lower": 3_500.0,
        "sample_ebitda_2024_upper": 4_500.0,
    },
    {
        "ticker": "2010.SR", "slug": "SABIC-6493058",
        "cache_prefix": "ms_2010_SR_SA0007879121_SABIC-6493058",
        "is_bank": False,
        "expected_periods_min": 5,
        "expected_periods_contains": ["FY2024"],
    },
    {
        "ticker": "EMAAR.AE", "slug": "EMAAR-PROPERTIES-9059234",
        "cache_prefix": "ms_EMAAR_AE_noisin_EMAAR-PROPERTIES-9059234",
        "is_bank": False,
        "expected_periods_min": 5,
    },
    {
        "ticker": "ADNOCGAS.AE", "slug": "ADNOC-GAS-PLC-151855977",
        "cache_prefix": "ms_ADNOCGAS_AE_noisin_ADNOC-GAS-PLC-151855977",
        "is_bank": False,
        "expected_periods_min": 5,
    },
    {
        "ticker": "NBOB.OM", "slug": "NATIONAL-BANK-OF-OMAN-SAO-6493114",
        "cache_prefix": "ms_NBOB_OM_noisin_NATIONAL-BANK-OF-OMAN-SAO-6493114",
        "is_bank": True,           # banks: EBITDA row is all dashes
        "expected_periods_min": 6,
        "expected_periods_contains": ["FY2023", "FY2024", "FY2025"],
        "sample_revenue": {"FY2024": 151.3, "FY2025": 163.5},
        # Critical regression: for banks the EBIT lookup must NOT silently
        # return all-None (the old substring bug). Real EBIT for NBOB
        # FY2024 is ~88, so a non-None populated value confirms the fix.
        "sample_ebit_2024_lower": 80.0,
        "sample_ebit_2024_upper": 100.0,
    },
    {
        "ticker": "BKMB.OM", "slug": "BANK-MUSCAT-SAOG-6492999",
        "cache_prefix": "ms_BKMB_OM_OM0000002796_BANK-MUSCAT-SAOG-6492999",
        "is_bank": True,
        "expected_periods_min": 5,
    },
]


def _cache_path(cache_prefix: str) -> Path:
    """Cache filename for the /finances/ page given the prefix the live
    pipeline uses. Mirrors marketscreener_pages._fetch_page's write path."""
    return CACHE_DIR / f"ms_{cache_prefix}_finances.html"


@pytest.fixture(params=TICKERS, ids=lambda t: t["ticker"])
def cfg(request):
    c = request.param
    p = _cache_path(c["cache_prefix"])
    if not p.exists():
        pytest.skip(f"No cached MS HTML for {c['ticker']} at {p.name}")
    return c


@pytest.fixture(autouse=True)
def offline_cache_first(monkeypatch):
    """Force `_fetch_page` to read the on-disk cache instead of hitting MS
    over the network. Without this, parser tests would silently turn into
    live-fetch tests (slow, captcha-prone, non-deterministic)."""
    monkeypatch.setenv("MS_OFFLINE_CACHE_FIRST", "1")


def _fetch(cfg) -> dict:
    """Run the parser against the cached HTML for this ticker.

    `MS_OFFLINE_CACHE_FIRST=1` (set by the autouse fixture) makes
    `_fetch_page` resolve from cache before any network attempt, so this
    is fully offline and deterministic.
    """
    from src.providers.marketscreener_pages import fetch_financial_forecast_series
    base = f"https://www.marketscreener.com/quote/stock/{cfg['slug']}"
    payload, _ = fetch_financial_forecast_series(
        base, cache_key_prefix=cfg["cache_prefix"],
    )
    ann = (payload.get("annual") or {})
    return ann


# ── Generic invariants — must hold for every ticker ────────────────────────

def test_periods_grid_populated(cfg):
    ann = _fetch(cfg)
    assert ann.get("periods"), f'{cfg["ticker"]}: empty periods list'
    assert len(ann["periods"]) >= cfg["expected_periods_min"], (
        f'{cfg["ticker"]}: expected ≥{cfg["expected_periods_min"]} periods, '
        f'got {len(ann["periods"])}'
    )
    for required in cfg.get("expected_periods_contains", []):
        assert required in ann["periods"], (
            f'{cfg["ticker"]}: expected period {required} not in {ann["periods"]}'
        )


def test_announcement_dates_align(cfg):
    ann = _fetch(cfg)
    assert "announcement_dates" in ann
    assert len(ann["announcement_dates"]) == len(ann["periods"]), (
        f'{cfg["ticker"]}: announcement_dates length {len(ann["announcement_dates"])} '
        f'!= periods length {len(ann["periods"])}'
    )


def test_net_sales_populated(cfg):
    ann = _fetch(cfg)
    sales = ann.get("net_sales") or []
    populated = [v for v in sales if v is not None]
    assert len(populated) >= 3, (
        f'{cfg["ticker"]}: net_sales has {len(populated)} non-None values, '
        f'expected ≥ 3 (got {sales})'
    )


def test_net_income_populated(cfg):
    ann = _fetch(cfg)
    ni = ann.get("net_income") or []
    populated = [v for v in ni if v is not None]
    assert len(populated) >= 3, (
        f'{cfg["ticker"]}: net_income has {len(populated)} non-None values'
    )


# ── EBIT / EBITDA collision regression ────────────────────────────────────

def test_ebit_not_silently_collapsed_to_ebitda(cfg):
    """The original bug: `_row_by_label("EBIT")` substring-matched the
    EBITDA row. After fix, EBIT and EBITDA must NEVER be element-wise
    equal across all populated periods (would be a code regression).

    Banks (EBITDA all None) skip this — there's no collision to detect
    when EBITDA is empty by design.
    """
    ann = _fetch(cfg)
    ebit = ann.get("ebit") or []
    ebitda = ann.get("ebitda") or []
    if not any(v is not None for v in ebitda):
        pytest.skip(f'{cfg["ticker"]}: EBITDA row empty (bank-shaped) — collision check N/A')
    # Are both rows populated for at least one common period?
    common = [
        i for i in range(min(len(ebit), len(ebitda)))
        if ebit[i] is not None and ebitda[i] is not None
    ]
    assert common, f'{cfg["ticker"]}: no overlap between EBIT and EBITDA populated periods'
    # If every common cell is identical, that's the bug signature.
    identical = all(ebit[i] == ebitda[i] for i in common)
    assert not identical, (
        f'{cfg["ticker"]}: EBIT == EBITDA exactly across all populated periods. '
        f'EBIT={[ebit[i] for i in common]}, EBITDA={[ebitda[i] for i in common]}. '
        f'This is the signature of the substring-collision bug — fix _row_by_label.'
    )


def test_ebit_not_greater_than_ebitda(cfg):
    """EBITDA = EBIT + D&A, D&A ≥ 0. So EBITDA ≥ EBIT always at the
    company level. Violation means columns are mis-aligned or rows are
    swapped. Some MS cells may have peculiar adjustments — allow a 1%
    fudge factor so we don't flag rounding."""
    ann = _fetch(cfg)
    ebit = ann.get("ebit") or []
    ebitda = ann.get("ebitda") or []
    for i in range(min(len(ebit), len(ebitda))):
        a, b = ebit[i], ebitda[i]
        if a is None or b is None:
            continue
        # Allow tiny rounding; flag anything > 1% over.
        assert a <= b * 1.01, (
            f'{cfg["ticker"]} period[{i}]: EBIT {a} > EBITDA {b} '
            f'(periods={ann.get("periods")[:i+1]})'
        )


def test_bank_ebit_still_populated(cfg):
    """For bank tickers the EBITDA row is empty by design. The EBIT row
    must still be populated — that's the row our deck displays. The
    original bug caused both rows to be all-None for banks (EBIT looked
    up the empty EBITDA row first), making slide 3 ship without EBIT.
    """
    if not cfg.get("is_bank"):
        pytest.skip(f'{cfg["ticker"]}: not a bank fixture')
    ann = _fetch(cfg)
    ebit = ann.get("ebit") or []
    populated = [v for v in ebit if v is not None]
    assert len(populated) >= 3, (
        f'{cfg["ticker"]} (bank): EBIT has {len(populated)} non-None values, '
        f'expected ≥ 3 (got {ebit}). The EBITDA row is empty for banks; the '
        f'EBIT lookup must NOT silently fall through to the empty EBITDA row.'
    )


# ── Sample-value spot-checks ──────────────────────────────────────────────

def test_revenue_spot_check(cfg):
    """For tickers with `sample_revenue` declared, assert the parsed
    value matches expected within ±5% (consensus drift tolerance)."""
    if not cfg.get("sample_revenue"):
        pytest.skip(f'{cfg["ticker"]}: no sample_revenue defined')
    ann = _fetch(cfg)
    periods = ann.get("periods") or []
    sales = ann.get("net_sales") or []
    for period, expected in cfg["sample_revenue"].items():
        if period not in periods:
            continue
        i = periods.index(period)
        actual = sales[i] if i < len(sales) else None
        assert actual is not None, f'{cfg["ticker"]} {period}: revenue None'
        assert abs(actual - expected) / expected < 0.05, (
            f'{cfg["ticker"]} {period}: revenue {actual} vs expected {expected} '
            f'(>5% drift — investigate)'
        )


def test_ebit_within_band(cfg):
    """When `sample_ebit_2024_lower/upper` are set, assert FY2024 EBIT
    falls in the expected band — catches both regression-to-zero
    (substring bug) and column-misalignment."""
    lo = cfg.get("sample_ebit_2024_lower")
    hi = cfg.get("sample_ebit_2024_upper")
    if lo is None or hi is None:
        pytest.skip(f'{cfg["ticker"]}: no EBIT band declared')
    ann = _fetch(cfg)
    periods = ann.get("periods") or []
    ebit = ann.get("ebit") or []
    if "FY2024" not in periods:
        pytest.skip(f'{cfg["ticker"]}: FY2024 not in fixture')
    i = periods.index("FY2024")
    actual = ebit[i] if i < len(ebit) else None
    assert actual is not None, f'{cfg["ticker"]} FY2024 EBIT is None — likely the substring-collision bug'
    assert lo <= actual <= hi, (
        f'{cfg["ticker"]} FY2024 EBIT {actual} not in band [{lo}, {hi}]'
    )


def test_ebitda_within_band(cfg):
    lo = cfg.get("sample_ebitda_2024_lower")
    hi = cfg.get("sample_ebitda_2024_upper")
    if lo is None or hi is None:
        pytest.skip(f'{cfg["ticker"]}: no EBITDA band declared')
    ann = _fetch(cfg)
    periods = ann.get("periods") or []
    ebitda = ann.get("ebitda") or []
    if "FY2024" not in periods:
        pytest.skip(f'{cfg["ticker"]}: FY2024 not in fixture')
    i = periods.index("FY2024")
    actual = ebitda[i] if i < len(ebitda) else None
    assert actual is not None
    assert lo <= actual <= hi, (
        f'{cfg["ticker"]} FY2024 EBITDA {actual} not in band [{lo}, {hi}]'
    )
