"""
Diagnostics runner. Usage:
  python -m scripts.diagnostics newsapi   # Test NewsAPI key and BYD/SCMP
  python -m scripts.diagnostics sabic [--working 1120.SR]   # SABIC vs working company

Output for sabic: outputs/sabic_vs_working_diagnostic.md and .json
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def cmd_newsapi() -> int:
    """Test NewsAPI key and BYD/SCMP response."""
    key = (
        os.environ.get("NEWSAPI_KEY", "").strip()
        or (__import__("src.config", fromlist=["cfg"]).cfg().get("news") or {}).get("newsapi_key", "").strip()
    )
    print("NEWSAPI_KEY set:", bool(key))
    if not key:
        print("Add NEWSAPI_KEY to .env or config/settings.toml [news] newsapi_key")
        return 1
    import requests
    url = "https://newsapi.org/v2/everything"
    params1 = {"q": "BYD", "domains": "scmp.com", "apiKey": key, "pageSize": 5, "language": "en", "sortBy": "publishedAt"}
    resp1 = requests.get(url, params=params1, timeout=15)
    data1 = resp1.json() if resp1.content else {}
    print("1) BYD + domains=scmp.com: totalResults:", data1.get("totalResults", 0), "| status:", data1.get("status"))
    params2 = {"q": "BYD", "apiKey": key, "pageSize": 5, "language": "en", "sortBy": "publishedAt"}
    resp2 = requests.get(url, params=params2, timeout=15)
    data2 = resp2.json() if resp2.content else {}
    articles2 = data2.get("articles") or []
    print("2) BYD (any source): totalResults:", data2.get("totalResults", 0), "| articles:", len(articles2))
    for i, a in enumerate(articles2[:3]):
        src = (a.get("source") or {}).get("name") or ""
        print(f"   [{i+1}]", (a.get("title") or "")[:50], "|", src)
    print("Done.")
    return 0


def cmd_sabic(working_ticker: str | None) -> int:
    """SABIC (2010.SR) vs working company side-by-side diagnostic."""
    from scripts.diagnostics_sabic import run as sabic_run
    return sabic_run(working_ticker)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run diagnostics (newsapi, sabic)")
    ap.add_argument("command", choices=["newsapi", "sabic"], help="Diagnostic to run")
    ap.add_argument("--working", type=str, default=None, help="For sabic: working ticker (default: BABA or 1120.SR)")
    args = ap.parse_args()
    if args.command == "newsapi":
        return cmd_newsapi()
    if args.command == "sabic":
        return cmd_sabic(args.working)
    return 0


if __name__ == "__main__":
    sys.exit(main())
