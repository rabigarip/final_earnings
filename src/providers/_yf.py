"""yfinance import shim that keeps its deprecation noise quiet.

yfinance/__init__.py force-enables its OWN DeprecationWarnings via
`filterwarnings('default', category=DeprecationWarning, module='^yfinance')`,
which shadows the package-level ignore set in src/__init__ (yfinance's filter
is registered later, so it wins). Importing yfinance through this shim
re-applies the ignore AFTER yfinance's, so the Pandas4Warning spam from
`Timestamp.utcnow` inside yfinance stays silent.

Usage:  from src.providers._yf import yf
"""
from __future__ import annotations

import warnings as _warnings

import yfinance as yf  # noqa: F401  — triggers yfinance.__init__ (adds its filter)

# Re-assert the ignore so it sits in front of yfinance's 'default' filter.
_warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module=r"yfinance",
)
_warnings.filterwarnings(
    "ignore", message=r".*Timestamp\.utcnow.*",
)

__all__ = ["yf"]
