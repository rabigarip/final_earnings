"""Sector-aware grounding schema.

Defines, per sector template family, the FY metrics the deck should ground
its commentary on — and the sanity bounds used to reject a mis-extracted
number. This is what lets EVERY sector get the BKMB treatment (real
balance-sheet + ratio actuals the LLM can cite), not just banks.

The disclosed `fy_highlights` block uses these GENERIC keys (currency-
agnostic; money values are in millions of the reporting currency, with a
sibling "<key>_growth_pct" for YoY). The LLM-context builder iterates the
ticker's family schema to render the FY block and the "not disclosed"
line, so adding a sector is a data change here, not new prompt code.

`kind`:  money_mn (millions, shown with currency) · pct · eps · raw
"""
from __future__ import annotations

from typing import Any

# (key, label, kind)
_BANK = [
    ("net_profit_mn",          "Net profit",            "money_mn"),
    ("nii_mn",                 "Net interest income",   "money_mn"),
    ("non_interest_income_mn", "Non-interest income",   "money_mn"),
    ("net_loans_mn",           "Net loans",             "money_mn"),
    ("customer_deposits_mn",   "Customer deposits",     "money_mn"),
    ("car_pct",                "Capital adequacy (CAR)", "pct"),
    ("roe_pct",                "ROE",                   "pct"),
    ("eps",                    "EPS",                   "eps"),
]
_ENERGY = [
    ("revenue_mn",        "Revenue",         "money_mn"),
    ("net_profit_mn",     "Net profit",      "money_mn"),
    ("ebitda_mn",         "EBITDA",          "money_mn"),
    ("free_cash_flow_mn", "Free cash flow",  "money_mn"),
    ("capex_mn",          "Capex",           "money_mn"),
    ("production_mboed",  "Production (mboe/d)", "raw"),
    ("roe_pct",           "ROE",             "pct"),
    ("eps",               "EPS",             "eps"),
]
_MATERIALS = [
    ("revenue_mn",        "Revenue",        "money_mn"),
    ("net_profit_mn",     "Net profit",     "money_mn"),
    ("ebitda_mn",         "EBITDA",         "money_mn"),
    ("ebitda_margin_pct", "EBITDA margin",  "pct"),
    ("sales_volume_kt",   "Sales volume (kt)", "raw"),
    ("roe_pct",           "ROE",            "pct"),
    ("eps",               "EPS",            "eps"),
]
_TECH = [
    ("revenue_mn",           "Revenue",          "money_mn"),
    ("net_profit_mn",        "Net profit",       "money_mn"),
    ("gross_margin_pct",     "Gross margin",     "pct"),
    ("operating_margin_pct", "Operating margin", "pct"),
    ("free_cash_flow_mn",    "Free cash flow",   "money_mn"),
    ("eps",                  "EPS",              "eps"),
]
_GENERIC = [
    ("revenue_mn",       "Revenue",       "money_mn"),
    ("net_profit_mn",    "Net profit",    "money_mn"),
    ("ebitda_mn",        "EBITDA",        "money_mn"),
    ("operating_margin_pct", "Operating margin", "pct"),
    ("eps",              "EPS",           "eps"),
]

FAMILY_SCHEMA: dict[str, list[tuple[str, str, str]]] = {
    "bank": _BANK, "financial_services": _BANK, "insurance": _BANK,
    "energy": _ENERGY,
    "materials": _MATERIALS,
    "tech": _TECH, "telco": _TECH,
    "industrial": _GENERIC, "healthcare": _GENERIC, "utility": _GENERIC,
    "consumer_staples": _GENERIC, "consumer_discretionary": _GENERIC,
    "retail": _GENERIC, "reit": _GENERIC, "other": _GENERIC,
}

# Sector metrics we typically CAN'T source — the model stays qualitative on
# these (never invents a number), shrunk by whatever the FY block provides.
NOT_DISCLOSED_HINTS: dict[str, list[str]] = {
    "bank": ["NIM (spread %)", "cost of risk / impairment charge",
             "NPL / asset-quality ratios", "buyback / capital-return plans"],
    "energy": ["realized price by stream", "unit lifting / production cost",
               "reserves & reserve-replacement ratio", "downstream margins"],
    "materials": ["realized price by product", "utilization rate",
                  "feedstock cost / spread", "capacity additions"],
    "tech": ["segment revenue mix", "user / engagement metrics",
             "deferred revenue / bookings", "guidance"],
    "_default": ["margin detail", "volume / unit economics", "guidance"],
}

# Per-metric sanity bounds — reject an extracted value outside these so a
# mis-read figure never reaches the deck. (lo, hi) in the metric's unit.
SANITY_BOUNDS: dict[str, tuple[float, float]] = {
    "car_pct": (8.0, 40.0),
    "roe_pct": (-30.0, 60.0),
    "gross_margin_pct": (0.0, 100.0),
    "operating_margin_pct": (-50.0, 80.0),
    "ebitda_margin_pct": (-20.0, 90.0),
    "eps": (-1000.0, 100000.0),
    # growth rates (applied to any *_growth_pct)
    "_growth_pct": (-90.0, 300.0),
}


def schema_for(template_family: str) -> list[tuple[str, str, str]]:
    return FAMILY_SCHEMA.get((template_family or "other").lower(), _GENERIC)


def not_disclosed_for(template_family: str) -> list[str]:
    return NOT_DISCLOSED_HINTS.get((template_family or "").lower(),
                                   NOT_DISCLOSED_HINTS["_default"])


def sanity_ok(key: str, value: Any) -> bool:
    """True if `value` is within bounds for `key` (growth keys share a band).
    Unknown keys pass (we only police metrics we have an opinion about)."""
    if not isinstance(value, (int, float)):
        return False
    if key.endswith("_growth_pct"):
        lo, hi = SANITY_BOUNDS["_growth_pct"]
        return lo <= value <= hi
    b = SANITY_BOUNDS.get(key)
    return True if b is None else (b[0] <= value <= b[1])
