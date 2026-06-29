"""Add a ticker to the deck-generator universe — end to end, with guardrails.

Most names are **Yahoo-covered**: Yahoo isn't Cloudflare-blocked, so it works
live on Render and the name only needs a company_master.json record. The hard
case is a **Yahoo-blind** name (Gulf MSX/ADX, some China A-shares): Render can't
fetch MarketScreener/Investing live, so the name also needs resolved slugs and
COMMITTED snapshots. This script automates that path and bakes in the two
things that are easy to get wrong by hand:

  • Identity verification — a resolved MS/Investing slug is only accepted if the
    exchange symbol or ISIN actually appears on the fetched page (a wrong slug
    silently pulls a different company — we hit a SpaceX false-positive once).
  • Peer-data validation — every curated peer is fetched and dropped if it would
    render an empty row (the ADNOCGAS lesson).

It writes nothing until you confirm, and leaves changes STAGED-BUT-UNCOMMITTED
so you eyeball the peers + identity before `git commit`.

Usage:
    python -m scripts.add_ticker ADNOCLS.AE "ADNOC Logistics & Services" \
        --peers 4030.SR,QGTS.QA,QNNS.QA,ADNOCDRILL.AE \
        --sector Industrials --industry "Marine Shipping"

    python -m scripts.add_ticker ADNOCLS.AE "ADNOC Logistics & Services" --dry-run
        # resolve + verify + validate peers and PRINT a report; touch nothing.

Flags: --dry-run (report only), --yes (skip the confirm prompt),
       --skip-snapshots (write records but don't fetch snapshots),
       --force (overwrite an existing record).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CM_PATH = ROOT / "data" / "company_master.json"
IV_PATH = ROOT / "data" / "investing_slugs.json"
MS_DIR = ROOT / "data" / "marketscreener"

# Exchange-suffix → (country, currency, exchange). Covers the EM/frontier book.
_SUFFIX_META = {
    "AE": ("UAE", "AED", "ADX"), "SR": ("Saudi Arabia", "SAR", "Tadawul"),
    "QA": ("Qatar", "QAR", "QSE"), "OM": ("Oman", "OMR", "MSX"),
    "KW": ("Kuwait", "KWD", "Boursa Kuwait"), "BH": ("Bahrain", "BHD", "BHB"),
    "SZ": ("China", "CNY", "SZSE"), "SS": ("China", "CNY", "SSE"),
    "HK": ("Hong Kong", "HKD", "HKEX"), "NS": ("India", "INR", "NSE"),
    "BO": ("India", "INR", "BSE"), "SA": ("Brazil", "BRL", "B3"),
    "MX": ("Mexico", "MXN", "BMV"), "JO": ("South Africa", "ZAR", "JSE"),
}


def _eprint(*a):
    print(*a, file=sys.stderr)


# ── identity verification ─────────────────────────────────────────────

def _fetch(url: str) -> str | None:
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=25, allow_redirects=True)
        return r.text if r.status_code == 200 else None
    except Exception as exc:
        _eprint(f"   fetch failed: {type(exc).__name__}: {exc}")
        return None


def _verify_ms(slug: str, symbol: str) -> tuple[bool, str | None]:
    """Confirm the MS page is really this company; return (ok, isin_found)."""
    html = _fetch(f"https://www.marketscreener.com/quote/stock/{slug}/")
    if not html:
        return False, None
    isin_m = re.search(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b", html)
    has_symbol = f"({symbol}" in html or symbol in html
    return bool(has_symbol), (isin_m.group(1) if isin_m else None)


def _verify_investing(slug: str, symbol: str) -> bool:
    html = _fetch(f"https://www.investing.com/equities/{slug}")
    return bool(html) and symbol in html


def _slugify_candidates(name: str) -> list[str]:
    base = name.lower().replace("&", " and ").replace("/", " ")
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    cands = [base, base.replace("-and-", "-"), re.sub(r"-(plc|p-j-s-c|pjsc|q-p-s-c|saog|psc)$", "", base)]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out


# ── data-file mutations ───────────────────────────────────────────────

def _load_companies() -> list[dict]:
    return json.loads(CM_PATH.read_text(encoding="utf-8"))


def _append_company_record(rec: dict) -> None:
    """Append as the LAST list element via text insertion, so every existing
    record stays byte-for-byte identical (no whole-file reserialization of a
    600-record hand-curated file)."""
    text = CM_PATH.read_text(encoding="utf-8")
    idx = text.rstrip().rfind("]")
    head = text[:idx].rstrip()  # ends with the previous last record's "}"
    body = json.dumps(rec, indent=2, ensure_ascii=True)
    body = "\n".join("  " + ln for ln in body.splitlines())  # list-item indent
    CM_PATH.write_text(head + ",\n" + body + "\n]\n", encoding="utf-8")


def _add_investing_slug(ticker: str, slug: str) -> None:
    s = json.loads(IV_PATH.read_text(encoding="utf-8"))
    s[ticker] = slug
    IV_PATH.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ── peer validation ───────────────────────────────────────────────────

def _validate_peers(peers: list[str]) -> tuple[list[str], list[str]]:
    """Return (good, dropped). A peer is good if fetch_peer_rows yields a row
    with a usable P/E or market cap — otherwise it renders an empty line."""
    from src.services.fetch_peers import fetch_peer_rows
    good, dropped = [], []
    for p in peers:
        try:
            rows = fetch_peer_rows([p]) or []
            r = rows[0] if rows else {}
            usable = isinstance(r.get("pe"), (int, float)) or isinstance(r.get("mcap_usd"), (int, float))
            (good if usable else dropped).append(p)
        except Exception:
            dropped.append(p)
    return good, dropped


# ── main ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker")
    ap.add_argument("name", nargs="?", default="", help="Company name (improves slug resolution)")
    ap.add_argument("--peers", default="", help="Comma-separated curated peer tickers")
    ap.add_argument("--sector", default="")
    ap.add_argument("--industry", default="")
    ap.add_argument("--dry-run", action="store_true", help="Resolve + verify + report only; write nothing")
    ap.add_argument("--yes", action="store_true", help="Skip the confirm prompt")
    ap.add_argument("--skip-snapshots", action="store_true")
    ap.add_argument("--force", action="store_true", help="Overwrite an existing record")
    args = ap.parse_args()

    ticker = args.ticker.strip().upper()
    name = args.name.strip()
    symbol = ticker.split(".")[0]
    suffix = ticker.split(".")[-1] if "." in ticker else ""
    country, currency, exchange = _SUFFIX_META.get(suffix, ("", "", ""))

    companies = _load_companies()
    if any((c.get("ticker") or "").upper() == ticker for c in companies) and not args.force:
        _eprint(f"{ticker} already in company_master.json (use --force to overwrite). Aborting.")
        return 1

    # ── Path A vs B: is the name Yahoo-covered? ──
    print(f"▸ Probing Yahoo coverage for {ticker} …")
    yahoo_ok = False
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        yahoo_ok = bool(info.get("currentPrice") or info.get("regularMarketPrice"))
        if yahoo_ok and not name:
            name = info.get("longName") or info.get("shortName") or ""
    except Exception:
        pass

    if yahoo_ok:
        print(f"✓ Yahoo COVERS {ticker} (Path A). It works live on Render — no snapshots needed.")
        print("  Add a company_master.json record (sector/industry/peers) and you're done;")
        print("  this script's snapshot machinery is only for Yahoo-blind names. Re-run with")
        print("  --skip-snapshots to register the record, or add it by hand.")
        if not args.force:
            return 0

    print(f"✓ Yahoo-BLIND {ticker} (Path B) — resolving MarketScreener + Investing slugs.\n")

    # ── resolve MS slug + verify identity ──
    from src.providers.marketscreener_pages import resolve_slug_from_search
    ms_slug = resolve_slug_from_search(symbol, company_name=name) or \
        (resolve_slug_from_search(name, company_name=name) if name else None)
    if not ms_slug:
        _eprint("✗ Could not resolve a MarketScreener slug. Aborting.")
        return 1
    ms_ok, isin = _verify_ms(ms_slug, symbol)
    print(f"  MarketScreener: {ms_slug}")
    print(f"     identity: symbol_on_page={ms_ok}  isin={isin or '—'}")
    if not ms_ok:
        _eprint("✗ MS identity check FAILED (symbol not on page) — refusing to use a possibly-wrong slug.")
        return 1

    # ── resolve Investing slug + verify identity ──
    from src.providers.probe_investing import resolve_investing_slug
    iv_slug = resolve_investing_slug(ticker, company_name=name)
    if iv_slug and not _verify_investing(iv_slug, symbol):
        iv_slug = None
    if not iv_slug and name:
        for cand in _slugify_candidates(name):
            if _verify_investing(cand, symbol):
                iv_slug = cand
                break
    print(f"  Investing: {iv_slug or '— (none verified; deck will price off MS only)'}")

    # ── peers ──
    peers = [p.strip().upper() for p in args.peers.split(",") if p.strip()]
    good_peers, dropped = ([], [])
    if peers:
        print(f"\n▸ Validating {len(peers)} peers (dropping any that render empty) …")
        good_peers, dropped = _validate_peers(peers)
        print(f"  keep: {good_peers or '—'}")
        if dropped:
            print(f"  DROP (no usable data): {dropped}")
    else:
        print("\n⚠ No --peers given. Slide 3 will be empty without a curated peer_group.")

    # ── assemble the record ──
    rec = {
        "ticker": ticker,
        "company_name": name or symbol,
        "company_name_long": name or symbol,
        "exchange": exchange, "country": country, "currency": currency,
        "isin": isin or "", "marketscreener_id": ms_slug, "zawya_slug": "",
        "sector": args.sector, "industry": args.industry,
        "peer_group": good_peers,
        "is_bank": bool(args.sector and "financ" in args.sector.lower()),
        "notes": f"Yahoo-blind. Estimates via MarketScreener; "
                 f"price via Investing slug '{iv_slug}'." if iv_slug else
                 "Yahoo-blind. Estimates/price via MarketScreener.",
    }

    print("\n── proposed company_master record ──")
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    for warn, val in [("sector", args.sector), ("industry", args.industry)]:
        if not val:
            print(f"⚠ {warn} is empty — pass --{warn} for a correct slide-1 header & peer cluster.")

    if args.dry_run:
        print("\n[dry-run] Nothing written. Re-run without --dry-run to apply.")
        return 0
    if not args.yes:
        try:
            if input("\nWrite this record + slug and fetch snapshots? [y/N] ").strip().lower() != "y":
                print("Aborted; nothing written.")
                return 1
        except EOFError:
            _eprint("No TTY for confirmation; re-run with --yes. Aborting."); return 1

    # ── write data files ──
    _append_company_record(rec)
    if iv_slug:
        _add_investing_slug(ticker, iv_slug)
    print(f"✓ wrote record to {CM_PATH.name}" + (f" + slug to {IV_PATH.name}" if iv_slug else ""))

    # ── snapshots (so it works on Render's Cloudflare-blocked IP) ──
    if not args.skip_snapshots:
        print("\n▸ Fetching MarketScreener snapshots …")
        subprocess.run([sys.executable, "-m", "scripts.refresh_marketscreener_cache",
                        "--tickers", ticker, "--delay", "4"], check=False)
        for h in MS_DIR.glob(f"ms_{ticker.replace('.', '_')}_*.html"):
            h.with_suffix(".html.gz").write_bytes(gzip.compress(h.read_bytes()))
            h.unlink()
        print("  gzipped MS snapshots.")
        if iv_slug:
            print("▸ Fetching Investing snapshots …")
            subprocess.run([sys.executable, "-m", "scripts.refresh_investing_cache",
                            "--tickers", ticker, "--delay", "2"], check=False)

    # ── offline verification build (simulate Render) ──
    print("\n▸ Offline verification build (simulating Render's blocked IP) …")
    env = {**os.environ, "MS_OFFLINE_CACHE_FIRST": "1", "DISABLE_LLM": "1"}
    env.pop("MS_USE_CURL_CFFI", None)
    code = (
        "from src.storage.db import init_db, seed_companies; init_db(); seed_companies();"
        "from src.pipeline import run_preview;"
        f"rid,res=run_preview('{ticker}', skip_llm=True);"
        "import glob,os; from pptx import Presentation;"
        f"f=sorted(glob.glob('outputs/{ticker}_*earnings_preview.pptx'), key=os.path.getmtime)[-1];"
        "p=Presentation(f); t=[s.text_frame.text.strip() for s in p.slides[0].shapes if s.has_text_frame];"
        "g=lambda k:(t[t.index(k)+1] if k in t and t.index(k)+1<len(t) else '??');"
        "print('  TARGET', g('TARGET PRICE'),'| UPSIDE', g('UPSIDE TO TARGET'),'| LAST', g('LAST CLOSE'),'| P/E', g('P/E (FY26E)'));"
        "print('  deck:', os.path.basename(f))"
    )
    subprocess.run([sys.executable, "-c", code], env=env, check=False)

    print(f"\n✅ {ticker} added. Review the staged changes (esp. peers + sector), then:")
    print(f"   git add -A && git commit -m 'Add {ticker} ({name})'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
