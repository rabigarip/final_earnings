"""
MarketScreener consensus summary scraper — backend-first earnings preview MVP.

Target: consensus page (target price + analyst recommendation).
Example: https://www.marketscreener.com/quote/stock/AL-RAJHI-BANKING-AND-INVE-6497957/consensus/

Scope:
  - This module extracts TOP-LEVEL consensus summary only (rating, targets, analyst count).
  - Detailed EPS/revenue estimate tables are a FUTURE separate module (e.g. consensus-revisions).
  - We explicitly detect sections present on the page and report which are not yet extracted.

Uses requests + BeautifulSoup. Playwright is optional for future use if blocking occurs.
"""

from __future__ import annotations
import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger(__name__)
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

# Optional: use project config if available (graceful fallback)
try:
    from src.config import cfg, root
    _USE_CONFIG = True
except ImportError:
    _USE_CONFIG = False


# ─── Result models ───────────────────────────────────────────────────────────

class ConsensusSummaryData(BaseModel):
    """Extracted consensus summary. All fields optional for defensive parsing."""
    consensus_rating: str | None = None
    analyst_count: int | None = None
    last_close_price: float | None = None
    price_currency: str | None = None
    average_target_price: float | None = None
    upside_to_average_target_pct: float | None = None
    high_target_price: float | None = None
    upside_to_high_target_pct: float | None = None
    low_target_price: float | None = None
    downside_to_low_target_pct: float | None = None
    analyst_firms: list[str] = Field(default_factory=list)

    def to_report_payload(self) -> dict[str, Any]:
        """Stable dict for report/API; omit None and empty lists."""
        out: dict[str, Any] = {}
        for k, v in self.model_dump().items():
            if v is None or (isinstance(v, list) and len(v) == 0):
                continue
            out[k] = v
        return out


class DetectedSection(BaseModel):
    """A section detected on the page; may or may not be extracted."""
    name: str
    present: bool = False
    extracted: bool = False
    status_message: str | None = None  # e.g. "eps_estimates_link_found_but_values_not_extracted"


class StepStatus(BaseModel):
    """Structured status/debug for this step."""
    step: str = "marketscreener_consensus_summary"
    status: str = "failed"  # success | partial | failed
    source: str = "marketscreener"
    fallback_used: bool = False
    message: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    record_count: int | None = None
    elapsed_ms: float = 0.0


class MarketScreenerConsensusResult(BaseModel):
    """Full result from fetch_marketscreener_consensus_summary."""
    extracted_data: ConsensusSummaryData = Field(default_factory=ConsensusSummaryData)
    step_status: StepStatus = Field(default_factory=StepStatus)
    raw_warnings: list[str] = Field(default_factory=list)
    detected_sections: list[DetectedSection] = Field(default_factory=list)


# ─── Fetch (separate from parse) ─────────────────────────────────────────────

def _fetch_consensus_page(url: str, cache_key_prefix: str | None = None) -> tuple[BeautifulSoup | None, str, list[str]]:
    """
    Fetch consensus page HTML. Returns (soup, short_message, errors).
    Does not parse; only retrieves and checks for block/captcha.

    When a cache_key_prefix is supplied, route through the UNIFIED
    marketscreener_pages._fetch_page so consensus gets the same hardened
    path as every other MS page: offline-cache read, curl_cffi Cloudflare
    bypass, and committed-snapshot fallback. Without this the consensus
    summary used plain `requests`, which is 403'd from Render's IPs — so
    the deck's rating/target/analyst-count box came back empty even though
    the rest of the MS data loaded fine.
    """
    errors: list[str] = []
    if not url or "/consensus" not in url.lower():
        errors.append("Invalid or non-consensus URL")
        return None, "Invalid URL", errors

    if cache_key_prefix:
        from src.providers.marketscreener_pages import _fetch_page, _cache_slug
        soup, ferrors = _fetch_page(url, _cache_slug(url, "consensus", cache_key_prefix))
        if soup is None:
            return None, "; ".join(ferrors) or "fetch failed", ferrors
        return soup, "ok", ferrors

    timeout = 15
    if _USE_CONFIG:
        try:
            timeout = cfg().get("scraping", {}).get("timeout_seconds", timeout)
        except Exception:
            pass

    try:
        from src.providers.marketscreener_pages import _get_session
        session = _get_session()
        session.headers["Sec-Fetch-Site"] = "same-origin"
        session.headers["Referer"] = "https://www.marketscreener.com/"
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 200:
            errors.append(f"HTTP {resp.status_code}")
            return None, f"HTTP {resp.status_code}", errors
        text = resp.text
        # Optional: cache HTML for debugging (when project config is used)
        if _USE_CONFIG:
            try:
                if cfg().get("scraping", {}).get("cache_html"):
                    slug = re.sub(r"[^a-zA-Z0-9-]", "_", url.split("/")[-2] or "page")[:80]
                    cache_dir = root() / "cache"
                    cache_dir.mkdir(exist_ok=True)
                    (cache_dir / f"ms_consensus_{slug}.html").write_text(text, encoding="utf-8")
            except Exception:
                pass
        from src.providers.marketscreener_pages import _is_blocked_response
        if _is_blocked_response(text):
            errors.append("Captcha or access denied in response")
            return None, "Block/captcha detected", errors
        # Check if we got a real consensus page (not homepage redirect)
        if "Number of Analysts" not in text and "Mean consensus" not in text:
            errors.append("Response does not look like a consensus page (redirect/block?)")
            # Still return soup so we can detect sections and return PARTIAL if needed
        soup = BeautifulSoup(text, "lxml")
        return soup, "ok", errors
    except requests.RequestException as e:
        errors.append(str(e))
        return None, str(e), errors


# ─── Parse top-level summary (separate from section detection) ────────────────

def _parse_consensus_summary(soup: BeautifulSoup) -> tuple[ConsensusSummaryData, list[str]]:
    """
    Extract consensus summary fields from page text.
    Uses regex on normalized text; handles missing fields gracefully.
    Returns (data, warnings).
    """
    data = ConsensusSummaryData()
    warnings: list[str] = []
    text = soup.get_text(" ", strip=True)

    # Mean consensus -> consensus_rating
    m = re.search(r"Mean consensus\s+(\w+)", text)
    if m:
        data.consensus_rating = m.group(1).upper()
    else:
        warnings.append("consensus_rating not found")

    # Number of Analysts
    m = re.search(r"Number of Analysts\s+(\d+)", text)
    if m:
        data.analyst_count = int(m.group(1))
    else:
        warnings.append("analyst_count not found")

    # Last Close Price + currency
    m = re.search(r"Last Close Price\s+([\d,.]+)\s*([A-Za-z]{2,4})", text)
    if m:
        data.last_close_price = float(m.group(1).replace(",", ""))
        data.price_currency = m.group(2)
    else:
        warnings.append("last_close_price / price_currency not found")

    # Average target price (currency may appear after number)
    m = re.search(r"Average target price\s+([\d,.]+)\s*([A-Za-z]{2,4})?", text)
    if m:
        data.average_target_price = float(m.group(1).replace(",", ""))
        if m.lastindex and m.lastindex >= 2 and m.group(2):
            data.price_currency = data.price_currency or m.group(2)

    # Spread / Average Target -> upside_to_average_target_pct
    m = re.search(r"Spread / Average Target\s+([+-]?[\d,.]+)\s*%", text)
    if m:
        data.upside_to_average_target_pct = float(m.group(1).replace(",", ""))
    else:
        warnings.append("upside_to_average_target_pct not found")

    # High Price Target (optional currency)
    m = re.search(r"High Price Target\s+([\d,.]+)\s*(?:[A-Za-z]{2,4})?", text)
    if m:
        data.high_target_price = float(m.group(1).replace(",", ""))

    # Spread / Highest target
    m = re.search(r"Spread / Highest target\s+([+-]?[\d,.]+)\s*%", text)
    if m:
        data.upside_to_high_target_pct = float(m.group(1).replace(",", ""))

    # Low Price Target (optional currency)
    m = re.search(r"Low Price Target\s+([\d,.]+)\s*(?:[A-Za-z]{2,4})?", text)
    if m:
        data.low_target_price = float(m.group(1).replace(",", ""))

    # Spread / Lowest Target -> downside_to_low_target_pct
    m = re.search(r"Spread / Lowest Target\s+([+-]?[\d,.]+)\s*%", text)
    if m:
        data.downside_to_low_target_pct = float(m.group(1).replace(",", ""))

    # Analyst firms: look for "Analysts covering the company" and a following table
    # MarketScreener often lists firm names in first column of that table
    analyst_firms: list[str] = []
    for h in soup.find_all(["h2", "h3", "h4", "p", "strong"]):
        label = (h.get_text(strip=True) or "").lower()
        if "analysts covering" in label or "analyst" in label and "company" in label:
            table = h.find_next("table")
            if table:
                for row in table.find_all("tr")[1:]:  # skip header
                    cells = row.find_all(["td", "th"])
                    if cells:
                        firm = cells[0].get_text(strip=True)
                        if firm and len(firm) > 1 and firm not in analyst_firms:
                            analyst_firms.append(firm)
                if analyst_firms:
                    break
    if analyst_firms:
        data.analyst_firms = analyst_firms[:50]  # cap for sanity
    else:
        warnings.append("analyst_firms not extracted (section may use different layout)")

    return data, warnings


# ─── Section detection (present but not necessarily extracted) ────────────────

def _detect_sections(soup: BeautifulSoup) -> list[DetectedSection]:
    """
    Detect which known sections/links exist on the page.
    Sets status_message when section is present but not extracted.
    """
    text = soup.get_text(" ", strip=True)
    text_lower = text.lower()
    sections: list[DetectedSection] = []

    # Analyst Consensus Detail
    present = "Analyst Consensus Detail" in text or "analyst consensus detail" in text_lower
    sections.append(DetectedSection(
        name="Analyst Consensus Detail",
        present=present,
        extracted=present,  # we extract summary from same block
        status_message=None if present else None,
    ))

    # Consensus revision (last 18 months)
    present = "Consensus revision" in text or "consensus revision" in text_lower or "last 18 months" in text
    sections.append(DetectedSection(
        name="Consensus revision (last 18 months)",
        present=present,
        extracted=False,
        status_message="consensus_revision_section_found_but_not_parsed" if present else None,
    ))

    # EPS Estimates (often a link to consensus-revisions)
    eps_link = "EPS Estimates" in text or "eps estimates" in text_lower
    sections.append(DetectedSection(
        name="EPS Estimates",
        present=eps_link,
        extracted=False,
        status_message="eps_estimates_link_found_but_values_not_extracted" if eps_link else None,
    ))

    # Revisions to estimates
    rev_link = "Revisions to estimates" in text or "revisions to estimates" in text_lower
    sections.append(DetectedSection(
        name="Revisions to estimates",
        present=rev_link,
        extracted=False,
        status_message="revisions_to_estimates_found_but_not_parsed" if rev_link else None,
    ))

    # Quarterly revenue - Rate of surprise
    rev_surprise = "Quarterly revenue" in text or "rate of surprise" in text_lower or "revenue" in text and "surprise" in text_lower
    sections.append(DetectedSection(
        name="Quarterly revenue - Rate of surprise",
        present=rev_surprise,
        extracted=False,
        status_message="revenue_surprise_section_found_but_chart_values_not_extracted" if rev_surprise else None,
    ))

    return sections


# ─── Main entry: fetch + parse + status ──────────────────────────────────────

def fetch_marketscreener_consensus_summary(url: str, cache_key_prefix: str | None = None) -> MarketScreenerConsensusResult:
    """
    Fetch a MarketScreener consensus page URL, extract summary, detect sections.
    Returns structured result with extracted_data, step_status, raw_warnings, detected_sections.

    `cache_key_prefix` routes the fetch through the unified hardened fetcher
    (curl_cffi + snapshot fallback + offline cache) — pass it from the
    pipeline so consensus survives Render's Cloudflare block.
    """
    result = MarketScreenerConsensusResult()
    step = StepStatus(step="marketscreener_consensus_summary", source="marketscreener")
    start_ms = time.perf_counter() * 1000

    # ─── Fetch ─────────────────────────────────────────────────────────────
    soup, fetch_msg, fetch_errors = _fetch_consensus_page(url, cache_key_prefix)
    if soup is None:
        step.status = "failed"
        step.message = fetch_msg
        step.errors = fetch_errors
        step.elapsed_ms = (time.perf_counter() * 1000) - start_ms
        result.step_status = step
        log.info("[MarketScreener] Fetching consensus page... FAILED")
        return result
    log.info("[MarketScreener] Fetching consensus page... SUCCESS")

    # ─── Parse summary ────────────────────────────────────────────────────
    data, parse_warnings = _parse_consensus_summary(soup)
    result.extracted_data = data
    result.raw_warnings = parse_warnings
    has_core = (
        data.consensus_rating is not None
        or data.analyst_count is not None
        or data.last_close_price is not None
        or data.average_target_price is not None
    )
    if has_core and not fetch_errors:
        step.status = "success" if len(parse_warnings) <= 2 else "partial"
        step.message = "Summary extracted" + (" with some fields missing" if parse_warnings else "")
    else:
        step.status = "partial" if has_core else "failed"
        step.message = fetch_msg if fetch_errors else "No core consensus fields found"
    step.warnings = parse_warnings
    if fetch_errors:
        step.errors = fetch_errors
    step.record_count = 1 if has_core else 0
    log.info("[MarketScreener] Consensus summary extracted... %s", step.status.upper())

    # ─── Analyst firms (optional) ───────────────────────────────────────────
    if data.analyst_firms:
        log.info("[MarketScreener] Analyst firms extracted... SUCCESS (%s firms)", len(data.analyst_firms))
    else:
        log.info("[MarketScreener] Analyst firms extracted... PARTIAL (none or not found)")

    # ─── Section detection ──────────────────────────────────────────────────
    result.detected_sections = _detect_sections(soup)
    for sec in result.detected_sections:
        if sec.status_message:
            step.warnings.append(sec.status_message)
            log.warning("[MarketScreener] %s detected but not parsed", sec.name)

    step.elapsed_ms = (time.perf_counter() * 1000) - start_ms
    result.step_status = step
    return result


# ─── Future extension points (TODO) ──────────────────────────────────────────
# def fetch_marketscreener_eps_estimates(url: str) -> ...:
#     """TODO: Parse consensus-revisions or EPS block; may require Playwright if in chart/JS."""
# def fetch_marketscreener_revisions(url: str) -> ...:
#     """TODO: Consensus revision (last 18 months) table/chart."""
# def fetch_marketscreener_revenue_surprise(url: str) -> ...:
#     """TODO: Quarterly revenue rate of surprise; likely chart/JS."""


# ─── Example usage ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    example_url = "https://www.marketscreener.com/quote/stock/AL-RAJHI-BANKING-AND-INVE-6497957/consensus/"
    print("Running MarketScreener consensus summary (example)...")
    res = fetch_marketscreener_consensus_summary(example_url)
    print("\n--- Step status ---")
    print(res.step_status.model_dump_json(indent=2))
    print("\n--- Extracted data (report payload) ---")
    print(res.extracted_data.to_report_payload())
    print("\n--- Detected sections ---")
    for s in res.detected_sections:
        print(f"  {s.name}: present={s.present}, extracted={s.extracted}, msg={s.status_message}")
    print("\n--- Raw warnings ---")
    print(res.raw_warnings)
