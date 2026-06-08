"""Tests for sector classification in scripts/build_ticker_registry.py.

Spot-checks the regex patterns and manual overrides to prevent
silent re-classification regressions when the patterns get tuned.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "build_ticker_registry",
    Path(__file__).resolve().parents[1] / "scripts" / "build_ticker_registry.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_banks():
    cases = [
        ("Bank Muscat SAOG", ("Financials", "Diversified Banks")),
        ("Industrial and Commercial Bank of China Limited",
            ("Financials", "Diversified Banks")),
        ("HDFC Bank Limited", ("Financials", "Diversified Banks")),
        ("Standard Chartered PLC", ("Financials", "Diversified Banks")),
    ]
    for name, expected in cases:
        actual = _mod.classify_sector(name)
        assert actual == expected, f"{name}: got {actual}, expected {expected}"


def test_insurance():
    assert _mod.classify_sector("Bupa Arabia for Cooperative Insurance Company") == \
        ("Financials", "Insurance")
    assert _mod.classify_sector("Manulife Financial Corporation") == \
        ("Financials", "Insurance")


def test_oil_and_gas():
    cases = [
        "Saudi Arabian Oil Company",
        "PetroChina Company Limited",
        "Reliance Industries Limited",
    ]
    for name in cases:
        sec, ind = _mod.classify_sector(name)
        assert sec == "Energy", f"{name}: sector={sec}"


def test_chemicals_pattern():
    cases = [
        "SABIC Agri-Nutrients Company",
        "Wanhua Chemical Group Co., Ltd.",
        "Saudi Kayan Petrochemical Company",
    ]
    for name in cases:
        sec, ind = _mod.classify_sector(name)
        assert sec == "Materials" and ind == "Chemicals", (
            f"{name}: ({sec}, {ind})")


def test_pharma_plural_form_matches():
    """The regex bug we fixed: 'pharmaceuticals' (plural) must match."""
    sec, ind = _mod.classify_sector("Jiangsu Hengrui Pharmaceuticals Co.,Ltd")
    assert sec == "Health Care", f"got {sec}"


def test_manual_override_sabic_parent():
    """The 'industries' generic pattern would otherwise classify SABIC
    (2010.SR, 'Saudi Basic Industries') as Industrials/Capital Goods.
    The manual override correctly tags it as Chemicals."""
    sec, ind = _mod.classify_sector("Saudi Basic Industries Corporation", "2010.SR")
    assert sec == "Materials" and ind == "Chemicals"


def test_template_family_dispatch():
    """Sector / industry mapping to template family — sample coverage."""
    assert _mod.template_family("Diversified Banks") == "bank"
    assert _mod.template_family("Insurance") == "insurance"
    assert _mod.template_family("Chemicals") == "materials"
    assert _mod.template_family("Pharmaceuticals & Biotech") == "healthcare"
    assert _mod.template_family("Integrated Oil & Gas") == "energy"
    assert _mod.template_family("Semiconductors") == "tech"
    # Unknown industry falls back to 'other'.
    assert _mod.template_family("This Does Not Exist") == "other"


def test_dr_underlying_inheritance():
    """BDR/SIC tickers should inherit sector from the underlying via
    the UNDERLYING_SECTORS table — 'Apple Inc.' alone wouldn't match
    any pattern, but the table maps AAPL → Software & Services."""
    assert _mod.UNDERLYING_SECTORS["AAPL"] == \
        ("Information Technology", "Software & Services")
    assert _mod.UNDERLYING_SECTORS["NVDA"] == \
        ("Information Technology", "Semiconductors")
    assert _mod.UNDERLYING_SECTORS["TSLA"] == \
        ("Consumer Discretionary", "Automobiles")
