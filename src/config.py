"""Loads TOML config and resolves project paths."""

from __future__ import annotations
import os
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_cfg: dict | None = None

def cfg() -> dict:
    global _cfg
    if _cfg is None:
        with open(ROOT / "config" / "settings.toml", "rb") as f:
            _cfg = tomllib.load(f)
    return _cfg

def root() -> Path:
    return ROOT


def database_path() -> Path:
    """Database file path. Use DATABASE_PATH env (e.g. /tmp/earnings-data/earnings.db on Render) if set."""
    env_path = os.environ.get("DATABASE_PATH")
    if env_path:
        return Path(env_path)
    return ROOT / cfg()["database"]["path"]


def report_output_dir() -> Path:
    """Report output directory. Use REPORT_OUTPUT_DIR env (e.g. /tmp/earnings-outputs on Render) if set."""
    env_dir = os.environ.get("REPORT_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir)
    return ROOT / cfg()["report"]["output_dir"]
