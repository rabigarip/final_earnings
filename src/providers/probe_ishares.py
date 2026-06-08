"""
iShares ETF proxy provider — Stage 2 (PDF fact-sheet edition).

Surfaces regional / asset-class return overlays from BlackRock's
publicly-published iShares fact-sheet PDFs. The fact sheets are the
canonical source for these ETFs: they're updated quarterly, the URLs
are stable, and the layout is consistent across ETFs. This is much
more reliable than scraping the JS-rendered product pages (which
moved their return tables behind client-side rendering some time ago).

ETF → benchmark mapping for our panel:
  EEM    — iShares MSCI Emerging Markets        (broad EM context)
  MCHI   — iShares MSCI China                   (HK/China names)
  INDA   — iShares MSCI India                   (BSE/NSE names)
  KSA    — iShares MSCI Saudi Arabia            (Tadawul names)
  UAE    — iShares MSCI UAE                     (ADX names)
  EMB    — iShares JPM USD EM Bond              (fixed-income overlay)

Each fact sheet contains:
  - Annualised performance (1y, 3y, 5y, 10y, since inception) NAV +
    market price + benchmark
  - Calendar year returns (last 5 years)
  - Top holdings (% of fund) and top sectors (% of fund)
  - Net assets, P/E, P/B, expense ratio

The provider returns historical_prices = full performance dict and
company_profile = a slimmer summary the slide consumes.

PDF cache: cache/ishares/<ETF>_factsheet.pdf, 30-day TTL (fact sheets
update quarterly so 30 days is conservative).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore

from src.services.probe_harness import Provider, persist_raw, cache_root


# ── Country → ETF mapping (drives _etf_for_ticker) ───────────
COUNTRY_TO_ETF: dict[str, str] = {
    "SA":  "KSA",   "SAU": "KSA",   "Saudi Arabia": "KSA",
    "AE":  "UAE",   "ARE": "UAE",   "United Arab Emirates": "UAE",
    "OM":  "EEM",   "OMN": "EEM",   "Oman": "EEM",         # no Oman ETF
    "IN":  "INDA",  "IND": "INDA",  "India": "INDA",
    "CN":  "MCHI",  "CHN": "MCHI",  "China": "MCHI",
    "HK":  "MCHI",  "HKG": "MCHI",  "Hong Kong": "MCHI",
}

_DEFAULT_PROXY = "EEM"

# ETF → fact-sheet URL. URLs are stable; iShares overwrites the file in
# place each quarter. If we ever wanted older snapshots, the
# /literature/fact-sheet/archive/ tree exists but isn't needed for live data.
_FACT_SHEET_URLS: dict[str, str] = {
    "EEM":  "https://www.ishares.com/us/literature/fact-sheet/eem-ishares-msci-emerging-markets-etf-fund-fact-sheet-en-us.pdf",
    "MCHI": "https://www.ishares.com/us/literature/fact-sheet/mchi-ishares-msci-china-etf-fund-fact-sheet-en-us.pdf",
    "INDA": "https://www.ishares.com/us/literature/fact-sheet/inda-ishares-msci-india-etf-fund-fact-sheet-en-us.pdf",
    "KSA":  "https://www.ishares.com/us/literature/fact-sheet/ksa-ishares-msci-saudi-arabia-etf-fund-fact-sheet-en-us.pdf",
    "UAE":  "https://www.ishares.com/us/literature/fact-sheet/uae-ishares-msci-uae-etf-fund-fact-sheet-en-us.pdf",
    "EMB":  "https://www.ishares.com/us/literature/fact-sheet/emb-ishares-jp-morgan-usd-emerging-markets-bond-etf-fund-fact-sheet-en-us.pdf",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}

_MIN_GAP = 1.0
_last_call: float = 0.0


def _rate_limit():
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < _MIN_GAP:
        time.sleep(_MIN_GAP - gap)
    _last_call = time.monotonic()


def _pdf_cache_path(etf: str) -> Path:
    return cache_root() / "ishares" / f"{etf}_factsheet.pdf"


def _is_fresh(path: Path, ttl_days: float = 30) -> bool:
    if not path.exists():
        return False
    age_days = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 86400.0
    return age_days < ttl_days


def _download_fact_sheet(etf: str) -> Optional[Path]:
    """Fetch the PDF into cache if missing or stale. Returns path or None."""
    dest = _pdf_cache_path(etf)
    if _is_fresh(dest):
        return dest
    url = _FACT_SHEET_URLS.get(etf)
    if not url:
        return None
    _rate_limit()
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30, stream=True)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
    # Verify PDF magic
    if dest.read_bytes()[:5] != b"%PDF-":
        dest.unlink(missing_ok=True)
        return None
    return dest


_NUM_PCT = r"[-+]?\d+\.\d+"


def _parse_fact_sheet(path: Path, etf: str) -> dict:
    """Extract the structured data we care about from an iShares fact sheet."""
    if pdfplumber is None:
        return {"_error": "pdfplumber not installed"}
    out: dict = {
        "etf": etf,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "as_of": None,
        "fund_name": None,
        "annualized": {},          # {"1y": ..., "3y": ..., "5y": ..., "10y": ..., "inception": ...}
        "calendar_year_returns": [],  # [{year, nav, market, benchmark}, ...]
        "top_holdings": [],
        "top_sectors": [],
        "characteristics": {},     # pe / pb / yield / beta / etc.
        "net_assets_m_usd": None,
        "expense_ratio_pct": None,
        "benchmark": None,
    }
    try:
        with pdfplumber.open(str(path)) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as exc:
        out["_error"] = f"{type(exc).__name__}: {exc}"
        return out

    # Fund name (first non-empty line)
    for line in full_text.splitlines():
        if line.strip():
            out["fund_name"] = line.strip()
            break

    # As-of date — "Fact Sheet as of <Month DD, YYYY>"
    m = re.search(r"Fact Sheet as of ([A-Z][a-z]+ \d{1,2}, \d{4})", full_text)
    if m:
        out["as_of"] = m.group(1)

    # Benchmark
    m = re.search(r"Benchmark\s*:\s*([^\n]+)", full_text)
    if m:
        out["benchmark"] = m.group(1).strip().split("Fund Launch")[0].strip()

    def _row_numbers(section_text: str, label: str, max_n: int = 5) -> list[float]:
        """Find the line in `section_text` that starts with `label` and
        return up to `max_n` floats from that line. Tolerates right-column
        clutter (e.g. 'P/B Ratio : 1.69x') after the numeric values."""
        for line in section_text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(label.lower()):
                # Trim everything after the first non-numeric token following
                # the leading floats so we don't accidentally include sidebar values.
                rest = stripped[len(label):].lstrip()
                nums: list[float] = []
                for tok in rest.split():
                    if re.fullmatch(_NUM_PCT, tok):
                        nums.append(float(tok))
                        if len(nums) >= max_n:
                            break
                    elif nums:  # we hit a non-number after collecting some — stop
                        break
                return nums
        return []

    # Annualised performance — find the section, parse three labelled rows.
    m = re.search(r"ANNUALIZED PERFORMANCE.*", full_text, re.S)
    if m:
        section = m.group()[:2000]
        nav   = _row_numbers(section, "NAV")
        mkt   = _row_numbers(section, "Market Price")
        bench = _row_numbers(section, "Benchmark")
        cols = ["1y", "3y", "5y", "10y", "inception"]
        for i, col in enumerate(cols):
            if i < len(nav):
                out["annualized"][col] = {
                    "nav":       nav[i],
                    "market":    mkt[i] if i < len(mkt) else None,
                    "benchmark": bench[i] if i < len(bench) else None,
                }

    # Calendar year returns — header is "YYYY YYYY YYYY YYYY YYYY"
    m = re.search(r"CALENDAR YEAR PERFORMANCE.*", full_text, re.S)
    if m:
        section = m.group()[:2000]
        # Find the year header
        yh = re.search(r"(\d{4})\s+(\d{4})\s+(\d{4})\s+(\d{4})\s+(\d{4})", section)
        if yh:
            years = [int(yh.group(i)) for i in range(1, 6)]
            nav   = _row_numbers(section, "NAV")
            mkt   = _row_numbers(section, "Market Price")
            bench = _row_numbers(section, "Benchmark")
            for i, yr in enumerate(years):
                out["calendar_year_returns"].append({
                    "year": yr,
                    "nav":       nav[i] if i < len(nav) else None,
                    "market":    mkt[i] if i < len(mkt) else None,
                    "benchmark": bench[i] if i < len(bench) else None,
                })

    # Top holdings
    m = re.search(r"TOP HOLDINGS\s*\(%\)\s*(.*?)(?:Total of Portfolio|Holdings are subject)", full_text, re.S)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"^(.+?)\s+(\d+\.\d+)%?\s*$", line.strip())
            if mm:
                out["top_holdings"].append({
                    "name": mm.group(1).strip(),
                    "weight_pct": float(mm.group(2)),
                })

    # Top sectors
    m = re.search(r"TOP SECTORS\s*\(%\)\s*Fund\s*(.*?)Allocations are subject", full_text, re.S)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"^(.+?)\s+(\d+\.\d+)\s*$", line.strip())
            if mm:
                out["top_sectors"].append({
                    "name": mm.group(1).strip(),
                    "weight_pct": float(mm.group(2)),
                })

    # Characteristics: net assets, P/E, P/B, beta, 30-day yield, expense ratio
    pat = re.compile(r"(P/B Ratio|P/E Ratio|30 Day SEC Yield|Equity Beta \(3y\)|Standard Deviation \(3y\)|Number of Holdings)\s*:\s*([\d.]+)x?%?")
    for mm in pat.finditer(full_text):
        out["characteristics"][mm.group(1)] = mm.group(2)
    m = re.search(r"Net Assets of Fund \(M\)\s*:\s*\$?([\d,\.]+)", full_text)
    if m:
        out["net_assets_m_usd"] = float(m.group(1).replace(",", ""))
    m = re.search(r"Expense Ratio\s+([\d.]+)%", full_text)
    if m:
        out["expense_ratio_pct"] = float(m.group(1))

    return out


def _etf_for_ticker(ticker: str) -> str:
    """Resolve a panel ticker to its regional iShares proxy."""
    try:
        from src.storage.db import load_company
        row = load_company(ticker)
        if row and row.get("country"):
            etf = COUNTRY_TO_ETF.get(row["country"])
            if etf:
                return etf
    except (ImportError, KeyError, TypeError):
        pass
    suffix = ticker.split(".")[-1].upper() if "." in ticker else ""
    by_suffix = {"SR": "KSA", "AE": "UAE", "HK": "MCHI",
                  "BO": "INDA", "NS": "INDA"}
    return by_suffix.get(suffix, _DEFAULT_PROXY)


class iSharesProvider(Provider):
    name = "ishares"

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def _etf_payload(self, ticker: str) -> dict:
        if ticker in self._cache:
            return self._cache[ticker]
        etf = _etf_for_ticker(ticker)
        path = _download_fact_sheet(etf)
        if not path:
            raise ValueError(f"iShares fact-sheet download failed for {etf}")
        payload = _parse_fact_sheet(path, etf)
        # Sanity check: must have at least annualised 1y to count as a hit
        if not payload.get("annualized", {}).get("1y"):
            raise ValueError(f"iShares fact-sheet parse incomplete for {etf}")
        payload["_etf_chosen"] = etf
        self._cache[ticker] = payload
        return payload

    def _fetch_historical_prices(self, ticker: str):
        payload = self._etf_payload(ticker)
        raw_id = persist_raw(self.name, ticker, "historical_prices", payload)
        return payload, "%", payload.get("as_of", ""), raw_id

    def _fetch_company_profile(self, ticker: str):
        payload = self._etf_payload(ticker)
        raw_id = persist_raw(self.name, ticker, "company_profile", payload)
        ann = payload.get("annualized", {})
        profile = {
            "etf_proxy":   payload["_etf_chosen"],
            "etf_name":    payload.get("fund_name") or "",
            "benchmark":   payload.get("benchmark") or "",
            "as_of":       payload.get("as_of") or "",
            "ytd_pct":     None,  # YTD not on annualised section; pull from cal year
            "1y_nav":      (ann.get("1y") or {}).get("nav"),
            "1y_benchmark": (ann.get("1y") or {}).get("benchmark"),
            "3y_nav":      (ann.get("3y") or {}).get("nav"),
            "5y_nav":      (ann.get("5y") or {}).get("nav"),
            "10y_nav":     (ann.get("10y") or {}).get("nav"),
            "net_assets_m_usd":   payload.get("net_assets_m_usd"),
            "expense_ratio_pct":  payload.get("expense_ratio_pct"),
            "top_holdings":  payload.get("top_holdings", [])[:5],
            "top_sectors":   payload.get("top_sectors", [])[:5],
            "kind":          "regional_proxy",
        }
        # YTD = most recent year's NAV column where available.
        cy = payload.get("calendar_year_returns", [])
        if cy:
            most_recent = max(cy, key=lambda r: r["year"])
            profile["ytd_pct"] = most_recent.get("nav")
            profile["ytd_year"] = most_recent.get("year")
        return profile, "", payload.get("as_of", ""), raw_id
