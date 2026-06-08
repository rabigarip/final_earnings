"""
MarketScreener provider — consensus estimates scraper.

Targets /consensus/ (analyst targets, recommendation, EPS) and /finances/
(historical + forward revenue/earnings). Uses browser headers, delays, and
cache/ for HTML. Set LOGLEVEL=DEBUG to see fetch/parse details.
"""

from __future__ import annotations
import json
import logging
import re
import time
import random
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from src.config import cfg, root
from src.models.financials import FinancialPeriod

log = logging.getLogger(__name__)


# ── Diagnostic result types ───────────────────────────────────

@dataclass
class FetchResult:
    """Result of a fetch with diagnostics for consensus debugging."""
    requested_url: str = ""
    final_url: str = ""
    status_code: int = 0
    title: str = ""
    canonical: str = ""
    first_1000_chars: str = ""
    classification: str = ""   # valid_consensus | redirected_to_homepage | blocked_cookie_wall | anti_bot_page | missing_consensus_markers | wrong_entity_page | stale_cached_url | other
    rule_fired: str = ""       # which detector rule matched (e.g. canonical_homepage, title_generic)
    has_number_of_analysts: bool = False
    has_mean_consensus: bool = False
    soup: BeautifulSoup | None = None
    from_cache: bool = False  # True if URL was from company_master (no re-resolve)


# ── HTTP fetch with caching ──────────────────────────────────

def _fetch_page(url: str, cache_name: str) -> BeautifulSoup | None:
    """Fetch a URL, cache HTML, return parsed soup or None."""
    settings = cfg()

    time.sleep(random.uniform(
        settings["scraping"]["min_delay_seconds"],
        settings["scraping"]["max_delay_seconds"],
    ))

    try:
        from src.providers.marketscreener_pages import _get_session, _is_blocked_response
        session = _get_session()
        session.headers["Sec-Fetch-Site"] = "same-origin"
        session.headers["Referer"] = "https://www.marketscreener.com/"
        resp = session.get(url, timeout=settings["scraping"]["timeout_seconds"],
                           allow_redirects=True)

        if settings["scraping"]["cache_html"]:
            cache_dir = root() / "cache"
            cache_dir.mkdir(exist_ok=True)
            (cache_dir / f"{cache_name}.html").write_text(resp.text, encoding="utf-8")
            log.debug("Cached HTML → cache/%s.html (%s bytes)", cache_name, len(resp.text))

        if resp.status_code != 200:
            log.debug("HTTP %s from %s", resp.status_code, url)
            return None

        if _is_blocked_response(resp.text):
            log.debug("Captcha/block detected on %s", url)
            return None

        return BeautifulSoup(resp.text, "lxml")

    except requests.RequestException as e:
        log.debug("Request failed: %s", e)
        return None


def _fetch_page_with_diagnostics(url: str, cache_name: str) -> FetchResult:
    """Fetch URL and return FetchResult with title, canonical, first 1000 chars, classification."""
    settings = cfg()
    result = FetchResult(requested_url=url)
    try:
        time.sleep(random.uniform(
            settings["scraping"]["min_delay_seconds"],
            settings["scraping"]["max_delay_seconds"],
        ))
        from src.providers.marketscreener_pages import _get_session
        session = _get_session()
        session.headers["Sec-Fetch-Site"] = "same-origin"
        session.headers["Referer"] = "https://www.marketscreener.com/"
        resp = session.get(url, timeout=settings["scraping"]["timeout_seconds"],
                           allow_redirects=True)
        result.final_url = resp.url or url
        result.status_code = resp.status_code

        if settings["scraping"]["cache_html"]:
            cache_dir = root() / "cache"
            cache_dir.mkdir(exist_ok=True)
            (cache_dir / f"{cache_name}.html").write_text(resp.text, encoding="utf-8")
            log.debug("Cached HTML → cache/%s.html (%s bytes)", cache_name, len(resp.text))

        if resp.status_code != 200:
            result.classification = "stale_cached_url" if resp.status_code == 404 else "other"
            result.rule_fired = f"http_{resp.status_code}"
            return result

        from src.providers.marketscreener_pages import _is_blocked_response
        if _is_blocked_response(resp.text):
            result.classification = "anti_bot_page"
            result.rule_fired = "blocked_response"
            return result

        soup = BeautifulSoup(resp.text, "lxml")
        result.soup = soup
        title_el = soup.find("title")
        result.title = title_el.get_text(strip=True) if title_el else ""
        canonical_el = soup.find("link", rel="canonical")
        result.canonical = (canonical_el.get("href") or "").strip() if canonical_el else ""
        text = soup.get_text(" ", strip=True)
        result.first_1000_chars = (text[:1000] + ("…" if len(text) > 1000 else "")) if text else ""
        result.has_number_of_analysts = "Number of Analysts" in text
        result.has_mean_consensus = "Mean consensus" in text

        is_home, rule = _is_homepage_detailed(soup)
        result.rule_fired = rule
        if is_home:
            result.classification = "redirected_to_homepage"
            return result
        if result.has_number_of_analysts or result.has_mean_consensus:
            result.classification = "valid_consensus"
            return result
        # Stock page but no consensus markers — might be wrong layout or wrong entity
        if "/quote/stock/" in (result.canonical or result.final_url) and "/consensus" in (result.final_url or url):
            result.classification = "missing_consensus_markers"
            result.rule_fired = "no_analyst_or_mean_consensus_in_body"
        else:
            result.classification = "wrong_entity_page"
            result.rule_fired = "canonical_or_final_url_not_consensus"
        return result
    except requests.RequestException as e:
        result.classification = "other"
        result.rule_fired = str(e)[:80]
        return result


# ── Helpers ────────────────────────────────────────────────────

_HOMEPAGE_URLS = (
    "https://www.marketscreener.com/",
    "https://www.marketscreener.com",
    "http://www.marketscreener.com/",
    "http://www.marketscreener.com",
    "https://sa.marketscreener.com/",
    "https://sa.marketscreener.com",
)


def _is_homepage_detailed(soup: BeautifulSoup) -> tuple[bool, str]:
    """
    True if the response is the site homepage (redirect/block returned homepage instead of stock page).
    Returns (is_homepage, rule_fired). rule_fired is empty if not homepage.
    """
    try:
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            h = canonical["href"].strip().rstrip("/")
            if h in _HOMEPAGE_URLS or h == "https://www.marketscreener.com":
                return True, "canonical_homepage"
        og_url = soup.find("meta", property="og:url")
        if og_url and og_url.get("content"):
            h = og_url["content"].strip().rstrip("/")
            if h in _HOMEPAGE_URLS or h == "https://www.marketscreener.com":
                return True, "og_url_homepage"
        title = soup.find("title")
        if title:
            t = title.get_text(strip=True)
            if "MarketScreener" in t and "Financial News" in t and "Stock Market Quotes" in t:
                return True, "title_generic_www"
            if "MarketScreener Saudi Arabia" in t and "Financial News" in t:
                return True, "title_generic_sa"
    except Exception:
        pass
    return False, ""


def _is_homepage(soup: BeautifulSoup) -> bool:
    """True if the response is the site homepage."""
    ok, _ = _is_homepage_detailed(soup)
    return ok


def _is_consensus_page(soup: BeautifulSoup) -> bool:
    """True if this looks like a stock consensus page (not homepage/redirect)."""
    if _is_homepage(soup):
        return False
    text = soup.get_text(" ", strip=True)
    return "Number of Analysts" in text or "Mean consensus" in text


# ── Consensus page parser ────────────────────────────────────

def _parse_consensus_summary(soup: BeautifulSoup) -> dict | None:
    """
    Extract the consensus summary block:
      - mean_consensus (e.g. "OUTPERFORM")
      - analyst_count
      - last_close
      - avg_target
      - spread_pct
    """
    summary = {}

    # MarketScreener puts these in text nodes near specific labels.
    # Strategy: search for label text, grab the adjacent value.
    text = soup.get_text(" ", strip=True)

    # Number of Analysts
    m = re.search(r"Number of Analysts\s+(\d+)", text)
    if m:
        summary["analyst_count"] = int(m.group(1))

    # Average target price (currency: SAR, EUR, USD, etc.)
    m = re.search(r"Average target price\s+([\d,.]+)\s*([A-Z]{3})", text)
    if m:
        summary["avg_target"] = float(m.group(1).replace(",", ""))

    # Last Close Price
    m = re.search(r"Last Close Price\s+([\d,.]+)\s*([A-Z]{3})", text)
    if m:
        summary["last_close"] = float(m.group(1).replace(",", ""))

    # Spread
    m = re.search(r"Spread / Average Target\s+([+-]?[\d,.]+)%", text)
    if m:
        summary["spread_pct"] = float(m.group(1).replace(",", ""))

    # Mean consensus
    m = re.search(r"Mean consensus\s+(\w+)", text)
    if m:
        summary["mean_consensus"] = m.group(1)

    if summary:
        log.debug("Consensus summary parsed: %s", summary)
    return summary if summary else None


def _parse_estimates_tables(soup: BeautifulSoup, currency: str,
                            is_bank: bool) -> list[FinancialPeriod]:
    """
    Parse EPS / Revenue estimate tables from the consensus page.

    MarketScreener typically renders estimate tables with year headers
    like 2025e, 2026e, 2027e and rows for various metrics.

    This tries multiple selector strategies because MS changes layout.
    """
    estimates: list[FinancialPeriod] = []

    # Strategy 1: Find all tables, look for ones with estimate-year headers
    tables = soup.find_all("table")
    log.debug("Found %s tables on page", len(tables))

    for i, table in enumerate(tables):
        rows = table.find_all("tr")
        if not rows:
            continue
        # Header row: prefer <th>, else first row's cells
        first_row_cells = rows[0].find_all(["td", "th"])
        headers = [c.get_text(strip=True) for c in first_row_cells]
        if not headers:
            continue
        # Look for year-like headers (2024, 2025e, 2026e, or 2024E etc.)
        year_cols = [h for h in headers if re.match(r"20\d{2}[eE]?\s*$", h.strip())]
        if not year_cols:
            # Also try strict 20XX or 20XXe
            year_cols = [h for h in headers if re.match(r"^20\d{2}[eE]?$", h.replace(" ", ""))]

        if not year_cols:
            continue

        log.debug("Table %s: headers=%s", i, headers[:8])
        log.debug("Table %s: year columns=%s", i, year_cols)

        row_data: dict[str, dict[str, str]] = {}
        data_rows = rows[1:]  # skip header
        for row in data_rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True).lower()
            values = [c.get_text(strip=True) for c in cells[1:]]
            # Pad values to match headers if needed
            while len(values) < len(headers) - 1:
                values.append("")
            row_data[label] = dict(zip(headers[1:], values))

        # Map row labels to our fields
        # MarketScreener uses labels like: "Net sales", "Net income", "EPS"
        rev_keys  = ["net sales", "revenue", "turnover", "net banking income",
                     "total revenue", "net interest income"]
        ni_keys   = ["net income", "net result", "net profit"]
        eps_keys  = ["eps", "earnings per share"]
        ebitda_keys = ["ebitda"]

        def _find_row(candidates: list[str]) -> dict[str, str] | None:
            for k in candidates:
                for rk, rv in row_data.items():
                    if k in rk:
                        return rv
            return None

        rev_row    = _find_row(rev_keys)
        ni_row     = _find_row(ni_keys)
        eps_row    = _find_row(eps_keys)
        ebitda_row = None if is_bank else _find_row(ebitda_keys)

        if not any([rev_row, ni_row, eps_row]):
            continue

        log.debug("Table %s: found rev=%s, ni=%s, eps=%s", i, rev_row is not None, ni_row is not None, eps_row is not None)

        for yr in year_cols:
            def _val(row: dict[str, str] | None, col: str) -> float | None:
                if row is None:
                    return None
                raw = row.get(col, "").strip()
                if not raw or raw in ("-", "N/A", "–"):
                    return None
                # Remove thousands separators, handle M/B suffixes
                clean = raw.replace(",", "").replace(" ", "")
                try:
                    return float(clean)
                except ValueError:
                    return None

            period = yr.rstrip("eE")  # "2025e" → "2025"
            is_est = yr.endswith("e") or yr.endswith("E")

            fp = FinancialPeriod(
                period_label=f"FY{period}",
                period_type="estimate" if is_est else "annual",
                source="marketscreener",
                is_consensus=is_est,
                revenue=_val(rev_row, yr),
                ebitda=None if is_bank else _val(ebitda_row, yr),
                net_income=_val(ni_row, yr),
                eps=_val(eps_row, yr),
                currency=currency,
            )
            # Only add if we got at least one number
            if any([fp.revenue, fp.net_income, fp.eps, fp.ebitda]):
                estimates.append(fp)
                log.debug("Parsed: %s rev=%s ni=%s eps=%s", fp.period_label, fp.revenue, fp.net_income, fp.eps)

    return estimates


# ── SABIC (2010.SR) diagnostics and debug artifact ───────────

def _run_sabic_diagnostics(
    ticker: str,
    company_name: str,
    isin: str,
    marketscreener_id: str,
    currency: str,
    is_bank: bool,
) -> dict:
    """
    For 2010.SR: fetch company page and consensus page with full diagnostics,
    compare, and return a dict suitable for the debug artifact.
    """
    base = f"https://www.marketscreener.com/quote/stock/{marketscreener_id}"
    company_url = base + "/"
    consensus_url = base + "/consensus/"

    # 1. Identifier resolution log
    resolution = {
        "input_ticker": ticker,
        "resolved_isin": isin,
        "resolved_company_name": company_name,
        "cached_marketscreener_company_url": company_url,
        "url_source": "company_master",
        "marketscreener_id": marketscreener_id,
    }

    # 2. Fetch company (summary) page with diagnostics
    log.debug("Fetching company page: %s", company_url)
    company_result = _fetch_page_with_diagnostics(company_url, f"ms_company_{marketscreener_id}_debug")
    company_result.from_cache = True
    company_result.requested_url = company_url

    # 3. Fetch consensus page with diagnostics
    log.debug("Fetching consensus page: %s", consensus_url)
    consensus_result = _fetch_page_with_diagnostics(consensus_url, f"ms_consensus_{marketscreener_id}_debug")
    consensus_result.from_cache = True
    consensus_result.requested_url = consensus_url

    # 4. Compare and suggest root cause
    company_valid = (
        company_result.status_code == 200
        and company_result.soup is not None
        and not _is_homepage_detailed(company_result.soup)[0]
    )
    consensus_valid = consensus_result.classification == "valid_consensus"
    consensus_redirecting = consensus_result.classification == "redirected_to_homepage"

    if consensus_valid:
        suggested_root_cause = "none"
    elif consensus_redirecting:
        suggested_root_cause = "consensus_url_returns_homepage"
    elif company_valid and not consensus_valid:
        suggested_root_cause = "company_page_ok_consensus_page_not"
    elif not company_valid:
        suggested_root_cause = "company_page_also_invalid"
    else:
        suggested_root_cause = consensus_result.classification or "unknown"

    artifact = {
        "ticker": ticker,
        "identifier_resolution": resolution,
        "company_page": {
            "requested_url": company_result.requested_url,
            "final_url": company_result.final_url,
            "status_code": company_result.status_code,
            "title": company_result.title,
            "canonical": company_result.canonical,
            "classification": company_result.classification,
            "rule_fired": company_result.rule_fired,
            "first_1000_chars": company_result.first_1000_chars[:500],
            "is_valid": company_valid,
        },
        "consensus_page": {
            "requested_url": consensus_result.requested_url,
            "final_url": consensus_result.final_url,
            "status_code": consensus_result.status_code,
            "title": consensus_result.title,
            "canonical": consensus_result.canonical,
            "classification": consensus_result.classification,
            "rule_fired": consensus_result.rule_fired,
            "has_number_of_analysts": consensus_result.has_number_of_analysts,
            "has_mean_consensus": consensus_result.has_mean_consensus,
            "first_1000_chars": consensus_result.first_1000_chars[:500],
            "is_valid": consensus_valid,
            "is_redirecting_to_homepage": consensus_redirecting,
        },
        "comparison": {
            "company_page_valid": company_valid,
            "consensus_page_valid": consensus_valid,
            "consensus_redirecting": consensus_redirecting,
        },
        "suggested_root_cause": suggested_root_cause,
    }
    return artifact


def _write_sabic_debug_artifact(artifact: dict, out_dir: Path) -> None:
    """Write SABIC consensus debug artifact (JSON + markdown summary)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ticker = artifact.get("ticker", "2010.SR")
    base_name = f"{ticker}_consensus_debug"
    json_path = out_dir / f"{base_name}.json"
    md_path = out_dir / f"{base_name}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2, default=str)

    # Markdown summary
    res = artifact.get("identifier_resolution", {})
    cp = artifact.get("company_page", {})
    cs = artifact.get("consensus_page", {})
    cmp_ = artifact.get("comparison", {})
    cause = artifact.get("suggested_root_cause", "")
    md = f"""# {ticker} MarketScreener consensus debug

## 1. Identifier resolution
- **Input ticker:** {res.get('input_ticker', '')}
- **Resolved ISIN:** {res.get('resolved_isin', '')}
- **Resolved company name:** {res.get('resolved_company_name', '')}
- **Cached MarketScreener company URL:** {res.get('cached_marketscreener_company_url', '')}
- **URL source:** {res.get('url_source', '')}

## 2. Company page fetch
- **Requested URL:** {cp.get('requested_url', '')}
- **Final URL (after redirects):** {cp.get('final_url', '')}
- **HTTP status:** {cp.get('status_code', '')}
- **Title:** {cp.get('title', '')}
- **Canonical:** {cp.get('canonical', '')}
- **Classification:** {cp.get('classification', '')}
- **Rule fired:** {cp.get('rule_fired', '')}
- **Valid:** {cp.get('is_valid', False)}

## 3. Consensus page fetch
- **Requested URL:** {cs.get('requested_url', '')}
- **Final URL (after redirects):** {cs.get('final_url', '')}
- **HTTP status:** {cs.get('status_code', '')}
- **Title:** {cs.get('title', '')}
- **Canonical:** {cs.get('canonical', '')}
- **Classification:** {cs.get('classification', '')}
- **Rule fired:** {cs.get('rule_fired', '')}
- **Has 'Number of Analysts':** {cs.get('has_number_of_analysts', False)}
- **Has 'Mean consensus':** {cs.get('has_mean_consensus', False)}
- **Valid:** {cs.get('is_valid', False)}
- **Redirecting to homepage:** {cs.get('is_redirecting_to_homepage', False)}

## 4. Comparison
- Company page valid: {cmp_.get('company_page_valid', False)}
- Consensus page valid: {cmp_.get('consensus_page_valid', False)}
- Consensus redirecting: {cmp_.get('consensus_redirecting', False)}

## 5. Suggested root cause
{cause}

## 6. First 500 chars (consensus page)
```
{cs.get('first_1000_chars', '')[:500]}
```
"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    log.debug("Artifact written → %s", json_path)
    log.debug("Summary → %s", md_path)


# ── Public interface ──────────────────────────────────────────

def fetch_consensus(marketscreener_id: str, currency: str,
                    is_bank: bool,
                    ticker: str | None = None,
                    company_name: str | None = None,
                    isin: str | None = None,
) -> tuple[list[FinancialPeriod] | None, dict | None]:
    """
    Scrape consensus estimates from MarketScreener.
    Returns (estimates or None, diagnostic or None).
    diagnostic is set when the requested URL was classified as redirected_to_homepage
    (so caller can invalidate cache and re-resolve by ISIN).
    Optional ticker, company_name, isin enable SABIC (2010.SR) diagnostics and debug artifact.
    """
    if not marketscreener_id:
        log.debug("No MarketScreener ID — skipping")
        return None, None

    is_sabic = ticker == "2010.SR"
    if is_sabic and company_name and isin:
        # Run full SABIC diagnostics and write artifact
        artifact = _run_sabic_diagnostics(
            ticker=ticker,
            company_name=company_name,
            isin=isin,
            marketscreener_id=marketscreener_id,
            currency=currency,
            is_bank=is_bank,
        )
        # Log specific failure reason from consensus page
        cs = artifact.get("consensus_page", {})
        cl = cs.get("classification", "")
        rule = cs.get("rule_fired", "")
        if cl and cl != "valid_consensus":
            log.debug("Consensus page classification: %s (rule: %s)", cl, rule)

    base = f"https://www.marketscreener.com/quote/stock/{marketscreener_id}"
    base_sa = f"https://sa.marketscreener.com/quote/stock/{marketscreener_id}"
    estimates: list[FinancialPeriod] = []
    redirect_diagnostic: dict | None = None  # set when consensus URL redirected to homepage

    # Alternate-access (limited, polite): www → sa. subdomain → consensus-revisions → finances → company page
    # Caller may set source_redirect and skip further retries when entity is correct but source redirects.
    # ── Try consensus page first ──────────────────────────────
    consensus_url = f"{base}/consensus/"
    log.debug("Fetching consensus: %s", consensus_url)
    soup = _fetch_page(consensus_url, f"ms_consensus_{marketscreener_id}")

    if soup:
        is_home, rule_fired = _is_homepage_detailed(soup)
        if is_home:
            redirect_diagnostic = {"classification": "redirected_to_homepage", "rule_fired": rule_fired}
            log.debug("Response classified as redirected_to_homepage (rule: %s); retrying sa.marketscreener.com", rule_fired)
            soup = _fetch_page(f"{base_sa}/consensus/", f"ms_consensus_{marketscreener_id}_sa")
            if soup:
                is_home, rule_fired = _is_homepage_detailed(soup)
                if is_home:
                    log.debug("sa. subdomain also returned homepage (rule: %s)", rule_fired)

    if soup:
        if not _is_consensus_page(soup):
            _, rule_fired = _is_homepage_detailed(soup)
            if rule_fired:
                log.debug("Response not consensus: redirected_to_homepage (rule: %s)", rule_fired)
            else:
                log.debug("Response not consensus: missing_consensus_markers")
        else:
            _parse_consensus_summary(soup)
            estimates = _parse_estimates_tables(soup, currency, is_bank)

    # ── Try consensus-revisions (estimate tables often here) ───
    if not estimates:
        rev_url = f"{base}/consensus-revisions/"
        log.debug("No estimates from consensus, trying: %s", rev_url)
        soup_rev = _fetch_page(rev_url, f"ms_revisions_{marketscreener_id}")
        if soup_rev and _is_homepage(soup_rev):
            soup_rev = _fetch_page(f"{base_sa}/consensus-revisions/", f"ms_revisions_{marketscreener_id}_sa")
        if soup_rev:
            estimates = _parse_estimates_tables(soup_rev, currency, is_bank)

    # ── Try finances page as supplement ───────────────────────
    if not estimates:
        finances_url = f"{base}/finances/"
        log.debug("Trying finances: %s", finances_url)
        soup2 = _fetch_page(finances_url, f"ms_finances_{marketscreener_id}")
        if soup2 and _is_homepage(soup2):
            soup2 = _fetch_page(f"{base_sa}/finances/", f"ms_finances_{marketscreener_id}_sa")
        if soup2:
            estimates = _parse_estimates_tables(soup2, currency, is_bank)

    # ── Retry: if SABIC and still no estimates, try parsing company (summary) page for tables ───
    if not estimates and is_sabic:
        company_url = base + "/"
        log.debug("Retry: fetching company page for consensus/estimates: %s", company_url)
        soup_company = _fetch_page(company_url, f"ms_company_{marketscreener_id}_retry")
        if soup_company and not _is_homepage(soup_company):
            estimates = _parse_estimates_tables(soup_company, currency, is_bank)
            if estimates:
                log.debug("Extracted %s estimate periods from company page", len(estimates))

    if estimates:
        log.debug("MarketScreener returned %s estimate periods", len(estimates))
        return estimates, redirect_diagnostic

    log.debug("MarketScreener: no parseable estimates found")
    return None, redirect_diagnostic
