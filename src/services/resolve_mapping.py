"""
Service: resolve_mapping

Looks up the ticker (yfinance ticker) in the identifier store (company_master).
Canonical flow: ticker → exact ISIN → MarketScreener entity (by ISIN); company name
is for validation/fallback only.

If a ticker is not in the DB, auto-discovery via yfinance metadata will insert it
so that any globally-traded ticker can be processed without manual seeding.
"""

from __future__ import annotations
import logging

from src.models.company import CompanyMaster
from src.models.step_result import Status, StepResult, StepTimer
from src.storage.db import load_company, insert_discovered_company
from src.services.entity_resolution import ensure_marketscreener_cached

log = logging.getLogger(__name__)
STEP = "resolve_mapping"

BANK_INDUSTRIES = frozenset({
    "banks", "banking", "banks—regional", "banks—diversified",
    "banks - regional", "banks - diversified", "commercial banks",
    "financial services", "money center banks",
})


def _auto_discover(ticker: str) -> dict | None:
    """Use yfinance to discover company metadata and insert into DB.
    Returns the new DB row dict or None on failure."""
    try:
        import yfinance as yf
        yt = yf.Ticker(ticker)
        info = yt.info or {}
        name = info.get("shortName") or info.get("longName")
        if not name:
            return None
        industry = info.get("industry", "")
        is_bank = industry.strip().lower() in BANK_INDUSTRIES
        isin = ""
        try:
            isin = yt.isin or ""
            # yfinance returns '-' when ISIN is unavailable
            if isin in ("-", "N/A", "None"):
                isin = ""
        except Exception:
            pass
        row = insert_discovered_company(
            ticker=ticker,
            company_name=name,
            company_name_long=info.get("longName", name),
            exchange=info.get("exchange", ""),
            country=info.get("country", ""),
            currency=info.get("currency") or "USD",
            isin=isin,
            sector=info.get("sector", ""),
            industry=industry,
            is_bank=is_bank,
        )
        if row:
            log.info("Auto-discovered %s → %s (ISIN=%s)", ticker, name, isin or "—")
        return row
    except Exception as exc:
        log.warning("Auto-discovery failed for %s: %s", ticker, exc)
        return None


def run(ticker: str) -> StepResult:
    with StepTimer() as t:
        row = load_company(ticker)

    # Auto-discover if not in DB
    if row is None:
        row = _auto_discover(ticker)
        if row is None:
            return StepResult(
                step_name=STEP, status=Status.FAILED, source="local",
                message=f"Could not resolve '{ticker}' — not in DB and yfinance auto-discovery failed.",
                elapsed_seconds=t.elapsed,
            )

    # Ensure MarketScreener URL cached (resolve by ISIN if missing/stale)
    updated = ensure_marketscreener_cached(ticker, company=row)
    if updated is not None:
        row = updated
    company = CompanyMaster(**row)

    gaps = []
    if not company.isin:
        gaps.append("ISIN")
    # MS: consider filled if we have a valid cached URL (ISIN-based), not just slug
    has_valid_ms = (
        (company.marketscreener_company_url or "").strip() and
        (company.marketscreener_status or "").strip().lower() == "ok"
    )
    if not has_valid_ms and not company.marketscreener_id:
        gaps.append("MarketScreener ID")
    if not company.zawya_slug:
        gaps.append("Zawya slug")

    status = Status.SUCCESS if not gaps else Status.PARTIAL
    gap_msg = f" — missing: {', '.join(gaps)}" if gaps else ""

    return StepResult(
        step_name=STEP, status=status, source="local",
        message=(
            f"Mapped: {company.company_name} | "
            f"ISIN={company.isin or '—'} | "
            f"bank={company.is_bank}{gap_msg}"
        ),
        data=company, elapsed_seconds=t.elapsed,
    )
