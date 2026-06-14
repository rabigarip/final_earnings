"""Package init.

Quiets unactionable third-party noise BEFORE the heavy libraries load
(grpc via google-generativeai; pandas via yfinance). Must run first, so it
lives in the package __init__ rather than per-entrypoint.
"""
from __future__ import annotations

import os as _os
import logging as _logging
import warnings as _warnings

# ── gRPC (pulled in by google-generativeai) ─────────────────────────────────
# When the pipeline forks `daily_refresh` subprocesses with an open grpc
# channel, grpc logs dozens of INFO lines: "FD from fork parent still in poll
# list: fd(N)". Lower its verbosity (must be set before grpc imports).
_os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
_os.environ.setdefault("GLOG_minloglevel", "2")

# ── Library-internal deprecations we can't fix from our code ─────────────────
# yfinance/pandas: "Timestamp.utcnow is deprecated" (Pandas4Warning).
# google-generativeai: package-deprecation FutureWarning (its message starts
#   with a newline, so the pattern needs (?s) for `.` to cross it).
# matplotlib: tight_layout incompatibility on the twin-axis earnings chart.
_warnings.filterwarnings("ignore", message=r".*Timestamp\.utcnow.*")
_warnings.filterwarnings("ignore", message=r"(?s).*deprecated-generative-ai.*")
_warnings.filterwarnings("ignore", message=r"(?s).*generativeai.*")
_warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"google(\..*)?")
_warnings.filterwarnings("ignore", message=r".*not compatible with tight_layout.*")

# yfinance prints "HTTP Error 404" for region-gated tickers (e.g. Oman MSX
# names like BKMB.OM that it simply doesn't cover). We handle the absence
# gracefully and fall back to Investing/MarketScreener, so the raw 404 chatter
# is noise — keep only CRITICAL from yfinance's own logger.
for _name in ("yfinance", "peewee", "urllib3.connectionpool"):
    _logging.getLogger(_name).setLevel(_logging.CRITICAL)
