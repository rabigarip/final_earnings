"""Regression tests for the auto-grounding extractor.

Pins the extractor's numeric output against the hand-verified gold files
(data/disclosed/*.json) for the December-fiscal-year tickers that have a
committed MarketScreener snapshot. These are the auto-promotable common
case; the test guards against parser/units/mapping regressions silently
corrupting the scaled grounding for the other ~490 tickers.

Non-December-FYE tickers (e.g. ICICIBANK.NS) are deliberately NOT pinned to
exact agreement — their MS period labels are offset by design and the
extractor flags them confidence != 'high' for human review.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.grounding_extractor import extract_grounding

GOLD_DIR = Path(__file__).resolve().parent.parent / "data" / "disclosed"

# December-FYE gold tickers with committed MS snapshots → must agree exactly.
CLEAN = ["BKMB.OM", "ADCB.AE", "2020.SR", "0700.HK", "1398.HK"]


def _gold(ticker: str) -> dict:
    return json.loads((GOLD_DIR / f"{ticker}.json").read_text()).get("fy_highlights", {})


@pytest.mark.parametrize("ticker", CLEAN)
def test_extractor_agrees_with_gold_on_clean_tickers(ticker):
    res = extract_grounding(ticker)
    assert res["_status"] == "auto_unverified", (
        f"{ticker}: expected a usable extraction, got {res['_status']}")
    assert res["_extractor"]["confidence"] == "high", (
        f"{ticker}: December-FYE should be high confidence")
    got = res["fy_highlights"]
    gold = _gold(ticker)
    # Every money/eps/dps field the extractor emits that also exists in gold
    # must agree within tolerance (the same definition the eval harness uses).
    checked = 0
    for k, gv in got.items():
        if k not in gold or not isinstance(gv, (int, float)):
            continue
        ref = gold[k]
        if k.endswith("_pct"):
            ok = abs(gv - ref) <= 3.0
        elif k in ("eps", "dps"):
            ok = abs(gv - ref) <= max(0.02, abs(ref) * 0.08)
        else:
            ok = (abs(gv) < 1.0) if ref == 0 else abs(gv - ref) / abs(ref) <= 0.06
        assert ok, f"{ticker}.{k}: extractor {gv} disagrees with gold {ref}"
        checked += 1
    assert checked >= 2, f"{ticker}: only {checked} overlapping fields checked"


def test_no_cache_ticker_returns_clean_status():
    # A ticker with no MS snapshot must degrade gracefully, not raise.
    res = extract_grounding("NONEXISTENT.XX")
    assert res["_status"] in ("no_cache", "no_periods", "no_net_income")
    assert res["fy_highlights"] == {}


def test_plain_millions_ashare_units():
    """Regression guard for the units bug: Chinese A-shares display
    income-statement values in plain millions (no B/M suffix). A fixed
    /1e6 produced net_profit_mn=0.04 for Midea. The FIN-scale calibration
    must keep it in the tens-of-thousands-of-millions band (~CNY 40bn)."""
    res = extract_grounding("000333.SZ")
    if res["_status"] != "auto_unverified":
        import pytest
        pytest.skip("no MS cache for 000333.SZ in this checkout")
    np_mn = res["fy_highlights"].get("net_profit_mn")
    assert np_mn is not None and np_mn > 1000, (
        f"Midea net_profit_mn={np_mn} — units mis-scaled (the 0.04 bug)")


def test_emitted_values_pass_sanity_bounds():
    from src.services.grounding_schema import sanity_ok
    res = extract_grounding("BKMB.OM")
    for k, v in res["fy_highlights"].items():
        if isinstance(v, (int, float)):
            assert sanity_ok(k, v), f"BKMB.OM.{k}={v} violates sanity bounds"
