"""
Download curated IR-PDF filings into the local cache.

Reads `_IR_PDF_URLS` from `src/providers/probe_ir_pdf.py` and writes
each PDF to `cache/ir_pdfs/<ticker>.pdf`. Idempotent: re-running
overwrites any existing cached copy (which is what we want when a
new filing replaces the old one in the config).

The download itself is a plain `requests.get(stream=True)` — none of
the curated IR sites guard their PDF URLs (the SPA wall is on the
*discovery* side, not the file delivery side). So this is fast and
no Playwright is needed.

Run:
    python -m scripts.download_ir_pdfs
    python -m scripts.download_ir_pdfs --only BKMB.OM
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.providers.probe_ir_pdf import _IR_PDF_URLS, _pdf_cache_path


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}


def download_one(ticker: str, entry: dict) -> tuple[bool, str]:
    url = entry["url"]
    dest = _pdf_cache_path(ticker)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get(url, headers=_HEADERS, timeout=60, stream=True)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    ctype = r.headers.get("content-type", "")
    # Some IR servers don't set the right MIME but still return a PDF;
    # we check the magic bytes below.
    total = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
                total += len(chunk)
    # Verify magic bytes — guards against landing-page HTML masquerading as PDF.
    head = dest.read_bytes()[:5]
    if not head.startswith(b"%PDF-"):
        dest.unlink(missing_ok=True)
        return False, f"not a PDF (got {head!r}, ctype={ctype!r})"
    return True, f"{total:,} bytes"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="Comma-separated tickers to fetch")
    args = ap.parse_args()

    only = {s.strip().upper() for s in args.only.split(",")} if args.only else None
    targets = {t: v for t, v in _IR_PDF_URLS.items() if not only or t in only}

    if not targets:
        print("No tickers matched. Configured:", list(_IR_PDF_URLS.keys()))
        return

    print(f"Downloading {len(targets)} IR PDF(s) into cache/ir_pdfs/")
    for ticker, entry in targets.items():
        ok, msg = download_one(ticker, entry)
        status = "ok" if ok else "FAIL"
        print(f"  [{status}] {ticker:10s} {entry.get('period_label', ''):12s} {msg}")
        if ok:
            print(f"           -> {_pdf_cache_path(ticker)}")


if __name__ == "__main__":
    main()
