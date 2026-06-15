"""Tests for src/services/ticker_registry.py."""
from __future__ import annotations

from src.services.ticker_registry import (
    get_ticker_info, is_bank, registry_peer_set, reset_cache,
)


def test_known_ticker_returns_full_record():
    r = get_ticker_info("BKMB.OM")
    assert r["template_family"] == "bank"
    assert r["currency"] == "OMR"
    assert r["currency_unit_scale"] == 1
    assert r["exchange_country"] == "OM"
    assert r["fiscal_year_end_month"] == 12


def test_unknown_ticker_returns_safe_default():
    r = get_ticker_info("ZZZ.UNKNOWN")
    assert r["template_family"] == "other"
    assert r["currency_unit_scale"] == 1
    assert r["fiscal_year_end_month"] == 12
    assert r["is_depositary_receipt"] is False
    assert isinstance(r["peer_set"], list)


def test_is_bank_helper():
    assert is_bank("BKMB.OM") is True
    assert is_bank("2020.SR") is False
    assert is_bank("ZZZ.UNKNOWN") is False


def test_peer_set_helper_returns_list():
    peers = registry_peer_set("BKMB.OM")
    assert isinstance(peers, list)
    assert 4 <= len(peers) <= 6   # spec: 4-6 peers (industry-aware selector)
    assert all(isinstance(p, str) for p in peers)
    # BKMB peers should include other GCC banks.
    assert any(p.endswith(".SR") or p.endswith(".QA") for p in peers)


def test_dr_routing():
    """BDRs/SICs are flagged and carry their underlying ticker so the
    renderer can pull fundamentals from the right source."""
    r = get_ticker_info("AAPL.MX")
    assert r["is_depositary_receipt"] is True
    assert r["underlying_ticker"] == "AAPL"
    assert r["dr_fundamentals_source"] == "underlying"
    # DR inherits sector from the underlying company.
    assert r["sector"] == "Information Technology"


def test_currency_unit_scale_for_zac():
    """South African JSE quotes in ZAc (cents). Without scale 100 the
    displayed price would be 100x wrong."""
    r = get_ticker_info("BHG.JO")
    assert r["currency"] == "ZAc"
    assert r["currency_unit_scale"] == 100


def test_indian_fiscal_year_offset():
    r = get_ticker_info("RELIANCE.NS")
    assert r["fiscal_year_end_month"] == 3


def test_cache_resets_cleanly():
    get_ticker_info("BKMB.OM")
    reset_cache()
    # Second call after reset still works.
    r = get_ticker_info("BKMB.OM")
    assert r["template_family"] == "bank"
