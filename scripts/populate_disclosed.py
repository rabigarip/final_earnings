"""Auto-populate data/disclosed/{ticker}.json from a ticker's IR portal.

Usage:
    python scripts/populate_disclosed.py BKMB.OM

Reads the ticker's `ir_portal_url` from data/tickers.json; downloads
the most recent N quarterly PDFs from the IR portal; runs the generic
IFRS interim-statement parser; writes the JSON shape that
`disclosed_loader.py` already consumes.

Per-ticker URL pattern discovery is the only piece NOT yet generic.
We carry a small KNOWN_IR_PATTERNS table here for tickers we've
verified; new tickers need one row added (typically 5 minutes per
ticker, then automated forever). When the pattern is unknown, the
script prints a TODO message so the analyst knows what's missing.

This is Phase 2 of the disclosed-source pipeline. Phase 3 will
subscribe to exchange disclosure feeds (Tadawul, MSX, ADX, DFM, QSE)
for automatic URL discovery; Phase 4 will add LLM fallback for the
~5% of layouts the regex parser can't handle.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

# Allow `python scripts/populate_disclosed.py …` to import from src/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parents[1]
DISCLOSED_DIR = ROOT / "data" / "disclosed"
DOWNLOAD_DIR = ROOT / "data" / "_ir_pdfs"   # cache dir for downloaded PDFs


# Per-ticker URL pattern. Each entry knows how to produce the URL for a
# specific quarterly period. Add new tickers as you encounter them.
#
# Function signature: (yyyy: int, qq: int) -> Optional[str]
#   yyyy   — fiscal year
#   qq     — quarter (1, 2, 3, 4)
#   returns the URL or None when the IR portal hasn't published that
#   period yet (e.g. FY25 reports out in early March).

def _bkmb_url(yyyy: int, qq: int) -> Optional[str]:
    """Bank Muscat: MSM_<MMYY>.pdf where MM is the period-end month
    (03 / 06 / 09 / 12) and YY is the 2-digit fiscal year."""
    month_for_q = {1: "03", 2: "06", 3: "09", 4: "12"}.get(qq)
    if not month_for_q: return None
    yy = str(yyyy)[-2:]
    return (f"https://www.bankmuscat.om/en/investorrelations/"
            f"QuarterlyReports/MSM_{month_for_q}{yy}.pdf")


def _nbo_url(yyyy: int, qq: int) -> Optional[str]:
    """National Bank of Oman: similar MSM-style pattern, NBO-prefixed.
    Pattern discovered from public IR-portal sample. Update if format
    changes (rare — Omani banks have very stable IR portals)."""
    month_for_q = {1: "03", 2: "06", 3: "09", 4: "12"}.get(qq)
    if not month_for_q: return None
    yy = str(yyyy)[-2:]
    return (f"https://www.nbo.om/SiteAssets/Investor%20Relations/"
            f"QuarterlyReports/NBO_{month_for_q}{yy}.pdf")


def _bkdb_url(yyyy: int, qq: int) -> Optional[str]:
    """Bank Dhofar: PDFs hosted at bankdhofar.com/InvestorRelations.
    File naming is BD_QQ_YYYY.pdf for quarterly interim statements."""
    return (f"https://www.bankdhofar.com/InvestorRelations/Financials/"
            f"BD_Q{qq}_{yyyy}.pdf")


# Saudi Tadawul tickers — the Saudi Exchange publishes IFRS financial
# statements through Tadawul Disclosures. Each company has a stable
# IR portal URL with English-locale interim PDFs. Patterns below are
# best-guess templates; analyst should verify each.

def _sabic_agrinutrients_url(yyyy: int, qq: int) -> Optional[str]:
    """SABIC Agri-Nutrients (2020.SR). IR portal hosts quarterly PDFs
    under /sites/default/files/<lang>/<yyyy>/Q<q>-<yyyy>-English.pdf.
    Pattern requires verification on first use; the framework downloads
    + validates the PDF magic header, so a bad URL fails gracefully."""
    return (f"https://san.sabic.com/sites/default/files/2024-03/"
            f"Q{qq}-{yyyy}-English.pdf")


def _aramco_url(yyyy: int, qq: int) -> Optional[str]:
    """Saudi Aramco (2222.SR). IR portal hosts interim statements at a
    stable URL keyed by year + quarter."""
    return (f"https://www.aramco.com/-/media/downloads/quarterly-results/"
            f"q{qq}-{yyyy}-interim-condensed-consolidated-financial-statements.pdf")


# Map: ticker → URL-builder function. Each takes (yyyy, qq) and returns
# the URL string or None when that period isn't published yet (e.g.,
# FY reports out in early March). Tested URLs are HIGH-confidence;
# inferred URLs are MEDIUM-confidence — the populate script downloads
# the file and validates the PDF magic header, so a wrong URL produces
# a clean failure rather than a corrupted JSON.

KNOWN_IR_PATTERNS: dict[str, callable] = {
    "BKMB.OM": _bkmb_url,           # HIGH confidence — tested
    "NBO.OM": _nbo_url,              # MEDIUM — pattern inferred
    "BKDB.OM": _bkdb_url,            # MEDIUM — pattern inferred
    "2020.SR": _sabic_agrinutrients_url,  # MEDIUM
    "2222.SR": _aramco_url,          # MEDIUM
    # Add more tickers here as their IR portals are confirmed.
    # Onboarding workflow: (1) find a sample PDF URL on the IR portal,
    # (2) identify how year+quarter map to the URL, (3) add a small
    # function above, (4) test with: `python scripts/populate_disclosed.py
    # <TICKER>`. The PDF magic-header check guards against bad URLs.
}


def _download_pdf(url: str, dest: Path) -> bool:
    """Stream the URL to `dest`. Skips if already cached. Returns True
    on success; False on network errors or 404s."""
    if dest.is_file() and dest.stat().st_size > 1024:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; JabalResearch/1.0)",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        if len(data) < 1024:   # FY25 reports often 302 to an HTML page
            log.warning("%s returned %d bytes — likely not a PDF yet", url, len(data))
            return False
        # Quick PDF sanity check (magic header).
        if not data.startswith(b"%PDF"):
            log.warning("%s is not a PDF (header=%r)", url, data[:8])
            return False
        dest.write_bytes(data)
        log.info("Downloaded %d KB → %s", len(data) // 1024, dest.name)
        return True
    except Exception as exc:
        log.warning("Download failed for %s: %s", url, exc)
        return False


def populate_ticker(ticker: str, n_recent_quarters: int = 6) -> int:
    """Populate data/disclosed/{ticker}.json with extracted quarters.

    Walks BACKWARD from the current quarter, attempting up to
    `n_recent_quarters` periods. Stops cleanly when downloads fail
    (e.g. an unreleased FY report).
    """
    pattern = KNOWN_IR_PATTERNS.get(ticker)
    if not pattern:
        log.error("No URL pattern known for %s. Add a function to "
                  "KNOWN_IR_PATTERNS in this script and retry.", ticker)
        return 1

    # Load ticker registry for company name + IR URL.
    reg_path = ROOT / "data" / "tickers.json"
    try:
        recs = json.loads(reg_path.read_text())
        reg = {r["ticker"]: r for r in recs if "ticker" in r}
    except (OSError, json.JSONDecodeError):
        reg = {}
    info = reg.get(ticker, {})

    # Build the list of (year, quarter) tuples newest-first to attempt.
    today = date.today()
    cur_q = (today.month - 1) // 3 + 1
    cur_y = today.year
    attempts: list[tuple[int, int]] = []
    yy, qq = cur_y, cur_q
    for _ in range(n_recent_quarters):
        attempts.append((yy, qq))
        qq -= 1
        if qq == 0:
            qq = 4; yy -= 1

    from src.services.pdf_interim_parser import (
        extract_interim_quarter, to_disclosed_quarterly_record,
    )

    quarterly: list[dict] = []
    source_docs: dict[str, str] = {}
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    for yyyy, qq in attempts:
        url = pattern(yyyy, qq)
        if not url: continue
        local_name = f"{ticker.replace('.', '_')}_{yyyy}Q{qq}.pdf"
        local_path = DOWNLOAD_DIR / local_name
        if not _download_pdf(url, local_path):
            continue
        ext = extract_interim_quarter(local_path)
        if not ext:
            log.warning("Parser returned nothing for %s", local_path.name)
            continue
        if ext.extraction_confidence == "low":
            log.warning("Low-confidence extraction for %s — review before trust",
                        local_path.name)
        rec = to_disclosed_quarterly_record(ext, Path(url).name)
        quarterly.append(rec)
        source_docs[ext.period] = url

    if not quarterly:
        log.error("No quarters extracted for %s", ticker)
        return 2

    # Compose the output JSON. Schema matches what disclosed_loader expects.
    out = {
        "ticker": ticker,
        "company": info.get("company_name", ""),
        "currency": info.get("currency", ""),
        "units": "thousands",   # IFRS interim PDFs publish in 'RO 000 / SAR M 000 etc.
        "_comment": (
            f"Auto-extracted by scripts/populate_disclosed.py from the "
            f"company IR portal. Re-run quarterly when new reports drop."
        ),
        "_source_documents": source_docs,
        "quarterly": quarterly,
    }
    DISCLOSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DISCLOSED_DIR / f"{ticker}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log.info("Wrote %s (%d quarters)", out_path, len(quarterly))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticker", nargs="?",
                    help="Ticker to populate (e.g. BKMB.OM). "
                         "Omit when using --all-known or --upcoming.")
    ap.add_argument("--quarters", type=int, default=6,
                    help="How many recent quarters to attempt (default 6)")
    ap.add_argument("--all-known", action="store_true",
                    help="Populate every ticker in KNOWN_IR_PATTERNS. "
                         "Used by the daily disclosed-refresh cron.")
    ap.add_argument("--upcoming", action="store_true",
                    help="Populate every known-pattern ticker that is also "
                         "in data/calendar/upcoming.json (the active set). "
                         "Highest-value daily mode.")
    ap.add_argument("--stale-only", action="store_true",
                    help="In bulk modes, skip tickers whose disclosed file "
                         "is already fresh (days_since_period_end < 60).")
    args = ap.parse_args()

    if args.all_known:
        targets = list(KNOWN_IR_PATTERNS.keys())
    elif args.upcoming:
        calendar_path = ROOT / "data" / "calendar" / "upcoming.json"
        if not calendar_path.is_file():
            log.error("--upcoming requires data/calendar/upcoming.json. "
                      "Run scripts/build_earnings_calendar.py first.")
            return 1
        try:
            cal = json.loads(calendar_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to read calendar: %s", exc)
            return 1
        active = {t["ticker"] for t in (cal.get("tickers") or [])
                   if isinstance(t, dict)}
        targets = [t for t in KNOWN_IR_PATTERNS if t in active]
        if not targets:
            log.info("No upcoming-set tickers have known IR patterns.")
            return 0
    elif args.ticker:
        targets = [args.ticker]
    else:
        ap.error("Provide a ticker, --all-known, or --upcoming")
        return 1

    if args.stale_only:
        from src.services.disclosed_status import assess
        fresh_threshold = 60
        skipped = []
        keep = []
        for t in targets:
            cov = assess(t)
            if cov.file_exists and cov.days_since_period_end is not None \
                and cov.days_since_period_end < fresh_threshold:
                skipped.append((t, cov.most_recent_period,
                                cov.days_since_period_end))
            else:
                keep.append(t)
        for t, period, days in skipped:
            log.info("Skipping %s — fresh disclosed (period=%s, %dd old)",
                     t, period, days)
        targets = keep

    if not targets:
        log.info("Nothing to populate after filtering.")
        return 0

    log.info("Populating %d ticker(s): %s", len(targets), ", ".join(targets))
    exit_codes = []
    for t in targets:
        log.info("=== %s ===", t)
        rc = populate_ticker(t, args.quarters)
        exit_codes.append(rc)
    # Successful if at least one ticker succeeded.
    return 0 if any(c == 0 for c in exit_codes) else max(exit_codes)


if __name__ == "__main__":
    sys.exit(main())
