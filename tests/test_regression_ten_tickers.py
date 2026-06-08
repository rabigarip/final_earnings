"""
Regression suite for the 10 canonical tickers.

For each ticker we build a synthetic `ReportPayload` whose values mirror what
the live pipeline would produce (and where we have cached MS HTML, the
numbers are MS-verified). We then assert:

  * `build_report_context.build()` produces a typed `ReportContext` with the
    correct mode (quarterly vs annual), currency, and provenance.
  * Key invariants hold: no EBITDA-from-EBIT mirror, sign-aware colour
    metadata is present, currency labels are self-describing.

These tests are deterministic and run in <1s — no network.

Tickers (as agreed with the user):
   2020.SR     SABIC Agri-Nutrients Co.   (Saudi industrials, MS+BBG)
   2010.SR     SABIC                       (Saudi industrials)
   ADNOCGAS.AE ADNOC Gas                   (UAE, dual-currency)
   IQCD.QA     Industries Qatar            (Qatar, GCC industrials)
   SPNY.AE     Spinneys 1961 Holding       (UAE, retail)
   EMAAR.AE    Emaar Properties            (UAE, real estate)
   4030.SR     SAL Saudi Logistics         (Saudi, services)
   1120.SR     Al Rajhi Bank               (Saudi bank — EBITDA suppressed)
   INFY.NS     Infosys                     (India tech)
   0700.HK     Tencent                     (HK megacap)
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from src.models.company import CompanyMaster
from src.models.financials import QuoteSnapshot
from src.models.report_payload import ReportPayload
from src.services.build_report_context import build


# ── Fixture factory ────────────────────────────────────────────────────────

def _make_payload(
    *, ticker: str, name: str, currency: str, sector: str, industry: str,
    is_bank: bool = False, exchange: str = "", country: str = "",
    price: float = 100.0, target: float = 110.0, mcap: float = 1e10,
    rating: str = "OUTPERFORM",
    annual: dict | None = None, eps_div: dict | None = None,
    valuation: dict | None = None, calendar_quarter: tuple[dict, dict] | None = None,
    bbg: bool = False, ms_currency: str | None = None,
) -> ReportPayload:
    c = CompanyMaster(
        ticker=ticker, company_name=name, exchange=exchange, country=country,
        currency=currency, sector=sector, industry=industry, is_bank=is_bank,
    )
    q = QuoteSnapshot(
        ticker=ticker, price=price, market_cap=mcap,
        target_mean_price=target, recommendation_key="buy",
        forward_pe=15.0, ev_to_ebitda=10.0, price_to_book=2.5, dividend_yield=0.03,
    )
    memo = {}
    if calendar_quarter:
        cp, cn = calendar_quarter
        memo["preview_quarter_short"] = "1Q26"
        memo["calendar_prior_quarter_released"] = cp
        memo["calendar_next_quarter"] = cn
    bundle_dict = None
    if bbg:
        try:
            from src.services.bloomberg_parser import load_bloomberg_bundle
            b = load_bloomberg_bundle(ticker)
            bundle_dict = asdict(b) if b else None
        except Exception:
            bundle_dict = None
    return ReportPayload(
        run_id=f"reg-{ticker}",
        generated_at=datetime(2026, 5, 1, 12, 0),
        company=c, quote=q,
        consensus_summary={
            "last_close_price": price,
            "price_currency": ms_currency or currency,
            "consensus_rating": rating,
            "average_target_price": target,
        },
        ms_annual_forecasts={"annual": annual} if annual else None,
        ms_eps_dividend_forecasts=eps_div,
        ms_valuation_multiples=valuation,
        bloomberg_bundle=bundle_dict,
        memo_computed=memo,
    )


# ── Per-ticker fixtures ────────────────────────────────────────────────────

# All numbers below mirror MS or are plausible-shape for the specific market.
# Where we have cached MS HTML (2020.SR, 2010.SR), the values are exact.
TICKER_CONFIGS: dict[str, dict] = {
    # SABIC Agri-Nutrients — verified against cached MS finances HTML
    "2020.SR": dict(
        ticker="2020.SR", name="SABIC Agri-Nutrients Company",
        exchange="SAU", country="Saudi Arabia", currency="SAR",
        sector="Basic Materials", industry="Agricultural Inputs",
        price=157.0, target=142.19, mcap=74e9, rating="OUTPERFORM",
        annual={
            "periods": ["2023", "2024", "2025", "2026", "2027", "2028"],
            "announcement_dates": ["2024-03-04", "2025-02-17", "2026-03-03", "", "", ""],
            "net_sales":  [11033, 11061, 13077, 12448, 12481, 11398],
            "net_income": [3659, 3327, 4322, 4760, 4425, 3831],
            "ebit":       [3785, 3048, None, 4599, 4142, 3314],
            "ebitda":     [4700, 3985, None, 5518, 5072, 4251],
        },
        eps_div={"periods": ["2023","2024","2025","2026","2027","2028"],
                 "eps": [7.69, 6.99, 9.08, 10.64, 9.273, 7.697]},
        valuation={"periods": ["2023","2024","2025","2026"],
                   "pe": [18.0, 16.0, 12.4, 14.7]},
    ),
    # SABIC parent
    "2010.SR": dict(
        ticker="2010.SR", name="Saudi Basic Industries Corporation",
        exchange="SAU", country="Saudi Arabia", currency="SAR",
        sector="Basic Materials", industry="Chemicals",
        price=85.0, target=78.0, mcap=255e9, rating="HOLD",
        annual={
            "periods": ["2023", "2024", "2025", "2026", "2027"],
            "announcement_dates": ["2024-03-08", "2025-03-03", "2026-03-09", "", ""],
            "net_sales":  [141500, 138700, 145200, 152300, 158900],
            "net_income": [5740, 4280, 6120, 7340, 8150],
            "ebitda":     [22100, 19500, 23800, 26200, 28100],
        },
        eps_div={"periods": ["2023","2024","2025","2026","2027"],
                 "eps": [1.91, 1.43, 2.04, 2.45, 2.72]},
    ),
    # ADNOC Gas — dual-currency: AED listing, USD reporting
    "ADNOCGAS.AE": dict(
        ticker="ADNOCGAS.AE", name="ADNOC Gas plc",
        exchange="ADX", country="UAE", currency="AED",
        sector="Energy", industry="Oil & Gas",
        price=3.85, target=4.20, mcap=300e9, rating="OUTPERFORM",
        annual={
            "periods": ["2023", "2024", "2025", "2026", "2027"],
            "announcement_dates": ["2024-02-20", "2025-02-25", "2026-02-26", "", ""],
            "net_sales":  [24400, 24800, 25600, 27200, 29100],
            "net_income": [4870, 4920, 5080, 5440, 5870],
            "ebitda":     [9100, 9300, 9650, 10200, 11050],
        },
        eps_div={"periods": ["2023","2024","2025","2026","2027"],
                 "eps": [0.063, 0.064, 0.066, 0.071, 0.076]},
    ),
    # Industries Qatar
    "IQCD.QA": dict(
        ticker="IQCD.QA", name="Industries Qatar",
        exchange="QSE", country="Qatar", currency="QAR",
        sector="Basic Materials", industry="Chemicals",
        price=14.50, target=15.20, mcap=87e9, rating="HOLD",
        annual={
            "periods": ["2023", "2024", "2025", "2026"],
            "announcement_dates": ["2024-02-13", "2025-02-12", "2026-02-15", ""],
            "net_sales":  [16400, 14800, 15300, 16100],
            "net_income": [5020, 4280, 4540, 4920],
            "ebitda":     [6810, 5950, 6280, 6620],
        },
        eps_div={"periods": ["2023","2024","2025","2026"],
                 "eps": [0.83, 0.71, 0.75, 0.81]},
    ),
    # Spinneys 1961 Holding — UAE retail, MS may have sparse coverage
    "SPNY.AE": dict(
        ticker="SPNY.AE", name="Spinneys 1961 Holding",
        exchange="DFM", country="UAE", currency="AED",
        sector="Consumer Defensive", industry="Grocery Retail",
        price=1.85, target=2.10, mcap=4.4e9, rating="OUTPERFORM",
        annual={
            "periods": ["2024", "2025", "2026"],
            "announcement_dates": ["2025-03-15", "2026-03-12", ""],
            "net_sales":  [3120, 3420, 3680],
            "net_income": [142, 168, 198],
            # No EBITDA published by MS for this name → row should drop
            "ebitda":     [None, None, None],
        },
        eps_div={"periods": ["2024","2025","2026"], "eps": [0.060, 0.071, 0.084]},
    ),
    # Emaar Properties
    "EMAAR.AE": dict(
        ticker="EMAAR.AE", name="Emaar Properties",
        exchange="DFM", country="UAE", currency="AED",
        sector="Real Estate", industry="Real Estate Development",
        price=8.20, target=9.10, mcap=72e9, rating="OUTPERFORM",
        annual={
            "periods": ["2023", "2024", "2025", "2026"],
            "announcement_dates": ["2024-02-14", "2025-02-19", "2026-02-18", ""],
            "net_sales":  [26500, 30200, 33500, 35800],
            "net_income": [8200, 11300, 13100, 13900],
            "ebitda":     [11500, 14800, 16800, 17900],
        },
        eps_div={"periods": ["2023","2024","2025","2026"],
                 "eps": [0.93, 1.28, 1.48, 1.57]},
    ),
    # SAL Saudi Logistics
    "4030.SR": dict(
        ticker="4030.SR", name="SAL Saudi Logistics Services",
        exchange="SAU", country="Saudi Arabia", currency="SAR",
        sector="Industrials", industry="Air Freight & Logistics",
        price=215.0, target=240.0, mcap=17.2e9, rating="OUTPERFORM",
        annual={
            "periods": ["2023", "2024", "2025", "2026"],
            "announcement_dates": ["2024-03-05", "2025-03-04", "2026-03-10", ""],
            "net_sales":  [2480, 2870, 3220, 3580],
            "net_income": [580, 720, 850, 980],
            "ebitda":     [820, 980, 1140, 1310],
        },
        eps_div={"periods": ["2023","2024","2025","2026"],
                 "eps": [7.25, 9.00, 10.62, 12.25]},
    ),
    # Al Rajhi Bank — banks naturally have no EBITDA
    "1120.SR": dict(
        ticker="1120.SR", name="Al Rajhi Banking and Investment Corp",
        exchange="SAU", country="Saudi Arabia", currency="SAR",
        sector="Financials", industry="Banks",
        is_bank=True,
        price=92.0, target=98.0, mcap=368e9, rating="OUTPERFORM",
        annual={
            "periods": ["2023", "2024", "2025", "2026"],
            "announcement_dates": ["2024-01-23", "2025-01-22", "2026-01-21", ""],
            "net_sales":  [29200, 31800, 35100, 38400],
            "net_income": [16500, 18400, 20300, 22500],
            # Banks don't publish EBITDA — confirms the row drops cleanly
            "ebitda":     [None, None, None, None],
        },
        eps_div={"periods": ["2023","2024","2025","2026"],
                 "eps": [4.13, 4.60, 5.08, 5.63]},
    ),
    # Infosys
    "INFY.NS": dict(
        ticker="INFY.NS", name="Infosys Limited",
        exchange="NSE", country="India", currency="INR",
        sector="Technology", industry="IT Services",
        price=1480.0, target=1620.0, mcap=6.15e12, rating="OUTPERFORM",
        annual={
            "periods": ["2023", "2024", "2025", "2026"],
            "announcement_dates": ["2024-04-18", "2025-04-17", "2026-04-16", ""],
            "net_sales":  [1466700, 1538580, 1645000, 1762400],
            "net_income": [248000, 260700, 280400, 305800],
            "ebitda":     [346800, 369500, 394700, 425300],
        },
        eps_div={"periods": ["2023","2024","2025","2026"],
                 "eps": [60.20, 63.40, 67.92, 74.15]},
    ),
    # Tencent
    "0700.HK": dict(
        ticker="0700.HK", name="Tencent Holdings Limited",
        exchange="HKG", country="Hong Kong", currency="HKD",
        sector="Communication Services", industry="Internet Content",
        price=415.0, target=480.0, mcap=3.85e12, rating="OUTPERFORM",
        annual={
            "periods": ["2023", "2024", "2025", "2026"],
            "announcement_dates": ["2024-03-20", "2025-03-19", "2026-03-19", ""],
            "net_sales":  [609000, 652000, 712400, 776800],
            "net_income": [115000, 142800, 168200, 184500],
            "ebitda":     [218000, 248400, 278900, 305700],
        },
        eps_div={"periods": ["2023","2024","2025","2026"],
                 "eps": [12.11, 15.04, 17.71, 19.43]},
    ),
}


@pytest.fixture(params=list(TICKER_CONFIGS.keys()), ids=lambda x: x)
def ticker_config(request):
    return TICKER_CONFIGS[request.param]


# ── Tests ──────────────────────────────────────────────────────────────────

def test_context_builds_for_all_tickers(ticker_config):
    """Every ticker must produce a typed ReportContext with no exceptions."""
    payload = _make_payload(**ticker_config)
    ctx = build(payload, {})
    assert ctx.cover.ticker == ticker_config["ticker"]
    assert ctx.cover.company_name == ticker_config["name"]
    assert ctx.snapshot.table.mode in ("quarterly", "annual")


def test_currency_matches_seed(ticker_config):
    """Cover currency must respect the seed file's currency tag."""
    payload = _make_payload(**ticker_config)
    ctx = build(payload, {})
    expected = ticker_config["currency"]
    assert ctx.cover.currency == expected, (
        f"{ticker_config['ticker']}: expected {expected}, got {ctx.cover.currency}"
    )
    # Table units label must include the currency for self-describing rendering.
    assert expected in ctx.snapshot.table.units_label


def test_ebitda_row_drops_when_unpublished(ticker_config):
    """Banks (1120.SR) and any company where MS publishes None for EBITDA
    must have the row dropped — never a fake EBIT mirror."""
    payload = _make_payload(**ticker_config)
    ctx = build(payload, {})
    annual = ticker_config.get("annual", {}) or {}
    eb = annual.get("ebitda") or []
    # If both prior + est slots are None, the row should be dropped (renderer
    # filters all-None rows). Test the data flow at the dataclass level.
    if eb and all(v is None for v in eb):
        # Both rows in the snapshot must reflect None for ebitda.
        for r in ctx.snapshot.table.rows:
            assert r.ebitda is None, (
                f"{ticker_config['ticker']}: ebitda should be None, got {r.ebitda}"
            )


def test_sign_aware_delta_metadata(ticker_config):
    """Cards must carry both the raw signed delta and its formatted string,
    so the renderer can colour by sign."""
    payload = _make_payload(**ticker_config)
    ctx = build(payload, {})
    for card in ctx.summary.cards:
        if card.delta_str and card.delta_str != "—":
            assert card.delta_pct is not None, (
                f"{ticker_config['ticker']}/{card.label}: delta_str set but delta_pct missing"
            )


def test_no_news_no_sidebar(ticker_config):
    """When no news_items are supplied, headlines list must be empty so the
    renderer suppresses the sidebar (no empty box on slide 2)."""
    payload = _make_payload(**ticker_config)
    ctx = build(payload, {})
    assert ctx.summary.headlines == []


def test_quarterly_mode_when_calendar_present(ticker_config):
    """Calendar-source quarterly path: when memo has both prior + next q,
    table.mode must flip to quarterly and cover label to 'Q… Earnings Preview'."""
    cfg = dict(ticker_config)
    cfg["calendar_quarter"] = (
        {"net_sales": 1000, "ebitda": 200, "net_income": 150, "eps": 1.5},
        {"net_sales": 1100, "ebitda": 220, "net_income": 165, "eps": 1.7},
    )
    payload = _make_payload(**cfg)
    ctx = build(payload, {"preview_short": "1Q26"})
    assert ctx.snapshot.table.mode == "quarterly"
    assert "Q1 2026" in ctx.cover.period_label or "Earnings Preview" in ctx.cover.period_label


def test_bbg_override_when_bundle_present():
    """SABIC AN has a real BBG xlsx in data/bloomberg/. When loaded, the
    table source must read 'Bloomberg' for both actuals and estimates."""
    cfg = TICKER_CONFIGS["2020.SR"]
    payload = _make_payload(**cfg, bbg=True)
    if payload.bloomberg_bundle is None:
        pytest.skip("Bloomberg bundle for 2020.SR not on disk")
    ctx = build(payload, {})
    assert ctx.snapshot.table.actuals_source == "Bloomberg"
    assert ctx.snapshot.table.estimates_source == "Bloomberg"
