"""
MarketScreener probe provider (Stage 1).

Thin adapter around the existing `marketscreener_pages` fetchers we
already use to drive the deck. Each `_fetch_<field>` method maps the
MS payload onto the canonical field name + units expected by the
coverage probe harness.

Caching: we delegate to the existing MS cache (24h HTML cache in
`cache/ms_*.html`) so probe re-runs of the same panel are network-free.

Coverage prior: MS has nearly the full set of fields for our universe
*as data*, but the user's Bloomberg comparison showed forward
estimates and target prices drift materially. The probe captures the
raw values; the reconciler's job is to flag the disagreement vs other
sources.
"""

from __future__ import annotations

from typing import Any, Optional

from src.providers import marketscreener_pages as ms
from src.services.probe_harness import Provider, persist_raw
from src.storage.db import load_company
from src.services.entity_resolution import (
    ensure_marketscreener_cached, get_effective_marketscreener_slug,
)


def _base_url_for(ticker: str) -> Optional[str]:
    """Resolve the MS company URL using the same chain the deck pipeline
    uses: stored slug -> ISIN-based candidate -> ticker/name search.

    Returns None if no slug could be resolved (those tickers will show
    `error="slug_not_resolved"` in the coverage matrix)."""
    row = load_company(ticker) or {}
    slug = get_effective_marketscreener_slug(row)
    if not slug and row.get("isin"):
        updated = ensure_marketscreener_cached(ticker, row)
        if updated:
            slug = get_effective_marketscreener_slug(updated)
    if not slug:
        name = (row.get("company_name") or "").strip()
        try:
            slug = ms.resolve_slug_from_search(ticker, company_name=name) or ""
        except Exception:
            slug = ""
    if not slug:
        return None
    return f"https://www.marketscreener.com/quote/stock/{slug}"


class MarketScreenerProvider(Provider):
    name = "marketscreener"

    def __init__(self):
        # Cache the resolved base URL across fields within one probe run
        # so we don't re-resolve 12 times per ticker.
        self._url_cache: dict[str, Optional[str]] = {}

    def _base(self, ticker: str) -> str:
        if ticker not in self._url_cache:
            self._url_cache[ticker] = _base_url_for(ticker)
        url = self._url_cache[ticker]
        if not url:
            raise RuntimeError("slug_not_resolved")
        return url

    # ── Identity / market ──

    def _fetch_current_price(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_summary_page(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "current_price", payload)
        price = payload.get("last_close_price")
        if price is None:
            raise ValueError("no last_close_price in summary")
        return float(price), (payload.get("price_currency") or ""), "", raw_id

    def _fetch_market_cap(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_valuation_multiples(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "market_cap", payload)
        # cap series is published in millions of currency; pick the most
        # recent non-None value (typically FY-current cap).
        caps = payload.get("capitalization") or []
        cap = next((c for c in reversed(caps) if c is not None), None)
        if cap is None:
            raise ValueError("no capitalization in valuation grid")
        return float(cap), "M", "", raw_id

    def _fetch_company_profile(self, ticker: str):
        # MS summary page carries enough to populate the same profile
        # shape we use for Yahoo. We don't fetch /company/ here because
        # the harness wants one canonical profile, not nested pages.
        base = self._base(ticker)
        payload, _ = ms.fetch_summary_page(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "company_profile", payload)
        row = load_company(ticker) or {}
        profile = {
            "name":     row.get("company_name"),
            "sector":   row.get("sector"),
            "industry": row.get("industry"),
            "country":  row.get("country"),
            "currency": payload.get("price_currency") or row.get("currency"),
            "summary":  "",  # MS doesn't put the description on /summary/
        }
        return profile, "", "", raw_id

    # ── Backward-looking financials ──

    def _fetch_income_statement_annual(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_financial_forecast_series(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "income_statement_annual", payload)
        ann = payload.get("annual") or {}
        if not ann.get("periods"):
            raise ValueError("annual block empty")
        return ({
            "n_periods": len(ann["periods"]),
            "periods":   ann["periods"],
            "metrics_present": [
                k for k in ("net_sales", "ebitda", "ebit", "ebt", "interest_paid", "net_income")
                if any(v is not None for v in (ann.get(k) or []))
            ],
        }, payload.get("unit_currency") or "", ann["periods"][-1] if ann.get("periods") else "", raw_id)

    def _fetch_income_statement_quarterly(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_financial_forecast_series(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "income_statement_quarterly", payload)
        q = payload.get("quarterly") or {}
        if not q.get("periods"):
            raise ValueError("quarterly block empty")
        return ({
            "n_periods": len(q["periods"]),
            "periods":   q["periods"][-8:],  # tail for readability
            "metrics_present": [
                k for k in ("net_sales", "ebitda", "ebit", "net_income", "eps")
                if any(v is not None for v in (q.get(k) or []))
            ],
        }, payload.get("unit_currency") or "", q["periods"][-1] if q.get("periods") else "", raw_id)

    def _fetch_balance_sheet(self, ticker: str):
        # MS doesn't publish a full balance sheet on /finances/.
        # The income_statement_actuals page has *some* balance-sheet
        # rows for banks but it's incomplete. Marked not_implemented
        # so the matrix is honest about MS coverage.
        raise NotImplementedError

    def _fetch_cash_flow(self, ticker: str):
        # Same as balance_sheet — MS doesn't publish a free cash-flow
        # statement, only FCF chart data on /finances/.
        raise NotImplementedError

    # ── Valuation ──

    def _fetch_valuation_historical(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_valuation_multiples(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "valuation_historical", payload)
        if not payload.get("periods"):
            raise ValueError("valuation periods empty")
        return ({
            "periods":  payload["periods"],
            "pe":       payload.get("pe"),
            "pbr":      payload.get("pbr"),
            "ev_ebitda": payload.get("ev_ebitda"),
            "yield_pct": payload.get("yield_pct"),
        }, "ratio", payload["periods"][-1] if payload.get("periods") else "", raw_id)

    def _fetch_dividend_yield(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_valuation_multiples(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "dividend_yield", payload)
        # Pick the most recent forward yield (typically FY-est).
        ys = payload.get("yield_pct") or []
        y = next((v for v in reversed(ys) if v is not None), None)
        if y is None:
            raise ValueError("no yield_pct populated")
        return float(y), "%", "", raw_id

    def _fetch_valuation_forward(self, ticker: str):
        base = self._base(ticker)
        s_payload, _ = ms.fetch_summary_page(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "valuation_forward", s_payload)
        vals = {
            "pe_2026":      s_payload.get("pe_2026"),
            "pe_2027":      s_payload.get("pe_2027"),
            "ev_sales_2026": s_payload.get("ev_sales_2026"),
            "yield_2026":   s_payload.get("yield_2026"),
        }
        if all(v is None for v in vals.values()):
            raise ValueError("forward valuation fields all None")
        return vals, "ratio", "", raw_id

    # ── Analyst ──

    def _fetch_target_price(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_consensus_summary(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "target_price", payload)
        mean = payload.get("average_target_price")
        if mean is None:
            raise ValueError("no average_target_price in consensus")
        return ({
            "mean": mean,
            "high": payload.get("high_target_price"),
            "low":  payload.get("low_target_price"),
            "n_analysts": payload.get("analyst_count"),
        }, payload.get("price_currency") or "", "", raw_id)

    def _fetch_rating_split(self, ticker: str):
        base = self._base(ticker)
        payload, _ = ms.fetch_consensus_summary(base, cache_key_prefix=ticker)
        raw_id = persist_raw(self.name, ticker, "rating_split", payload)
        details = payload.get("rating_details") or {}
        if not details:
            # Fall back to the headline rating from summary.
            rating = payload.get("consensus_rating")
            if not rating:
                raise ValueError("no consensus_rating or rating_details")
            return ({"consensus": rating}, "", "", raw_id)
        return (details, "", "", raw_id)

    # historical_prices isn't on any MS endpoint we currently parse.
    def _fetch_historical_prices(self, ticker: str):
        raise NotImplementedError

    # ── Broker actions (recent analyst recommendations) ─────────
    def _fetch_broker_actions(self, ticker: str):
        """Surface the "Analyst's recommendations" table from MS /consensus/.
        Returns a list of (date, headline, source) dicts that the deck
        renders as the "Last 3 broker actions" card on slide 3."""
        base = self._base(ticker)
        payload, _ = ms.fetch_analyst_recommendations(base, cache_key_prefix=ticker)
        items = payload.get("items") or []
        brokers = payload.get("covering_brokers") or []
        if not items and not brokers:
            raise ValueError("no broker actions or covering brokers in consensus payload")
        raw_id = persist_raw(self.name, ticker, "broker_actions", payload)
        return ({
            "items": items[:10],            # cap for slide consumption
            "covering_brokers": brokers,
        }, "", "", raw_id)
