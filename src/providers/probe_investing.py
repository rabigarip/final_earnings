"""Investing.com probe provider — HTTP-only via curl_cffi.

Rewritten 2026-05 to drop Playwright. Cloudflare's bot challenge is bypassed
by curl_cffi's Chrome TLS impersonation, which means this module works on
any Python host (Render, local, CI) without a Chromium install.

Source of truth on each Investing equity page is the `<script id="__NEXT_DATA__">`
JSON blob — every store (equityStore, companyProfileStore, consensusEstimatesStore,
earningsStore) is serialized inside it. We parse the JSON directly rather than
scraping rendered text, which makes parsing far more stable.

Caching: 24h disk cache keyed by slug + page-kind so re-probes are zero-network.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from src.services.probe_harness import Provider, persist_raw, cache_root


# ── Slug map ─────────────────────────────────────────────────────────────
# Curated Investing.com slug per Yahoo ticker. Add new tickers here after
# verifying the slug at https://www.investing.com/equities/<slug>.
_SLUGS: dict[str, str] = {
    # Saudi / Tadawul
    "2222.SR":        "saudi-aramco",
    "2020.SR":        "sa-fertilizers",
    "1180.SR":        "national-com-bnk",        # Saudi National Bank (peer)
    # UAE / ADX + DFM (subject + peer tickers used in slide-3 peer table)
    "ADCB.AE":        "ad-commercial",
    "ADNOCDRILL.AE":  "adnoc-drilling",
    "ENBD.AE":        "emirates-nbd",            # Emirates NBD (peer)
    "FAB.AE":         "natl-bk-of-ad",           # First Abu Dhabi Bank (peer)
    "DIB.AE":         "db-islamic-bk",           # Dubai Islamic Bank (peer)
    # Oman / MSM
    "BKMB.OM":        "bank-muscat",
    "OQEP.OM":        "oq-exploration-and-production-cjsc",
    # Qatar / Kuwait (peer tickers)
    "QNBK.QA":        "qnb",                    # Qatar National Bank
    "NBKK.KW":        "national-bank-kt",       # National Bank of Kuwait
    # India / NSE
    "JINDALSTEL.NS":  "jindal-steel---power",
    "ICICIBANK.NS":   "icici-bank-ltd",   # NSE listing in INR (not 'icici-bank' = IBN ADR in USD)
    "ICICIBANK.BO":   "icici-bank-ltd",
    # China / Hong Kong
    "0700.HK":        "tencent-holdings-hk",
    "2899.HK":        "zijin-mining-group",
    "1398.HK":        "icbc",
}


from functools import lru_cache as _lru_cache


@_lru_cache(maxsize=1)
def _resolved_slugs() -> dict[str, str]:
    """Auto-resolved Investing slugs committed by scripts/resolve_investing_slugs.py.

    Investing's search API is Cloudflare-blocked from Render's datacenter IP,
    so slugs are resolved offline (locally / GitHub Actions) and committed to
    data/investing_slugs.json; the runtime just reads them here.
    """
    try:
        from src.config import root
        p = root() / "data" / "investing_slugs.json"
        if p.is_file():
            return {k.upper(): v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if v}
    except Exception:
        pass
    return {}


def _slug(ticker: str) -> Optional[str]:
    t = ticker.upper()
    # Hand-curated _SLUGS win (highest trust); then the auto-resolved cache.
    return _SLUGS.get(t) or _resolved_slugs().get(t)


# Investing labels exchange/country by full name; map our exchange suffixes to
# the country string the search returns so we can require BOTH symbol AND
# country to match (numeric tickers collide across markets — 2010 is SABIC on
# Tadawul AND a Taiwan steel co; 1211 is Maaden AND BYD in HK).
_SUFFIX_COUNTRY = {
    "SR": "saudi arabia", "QA": "qatar", "AE": "united arab emirates",
    "OM": "oman", "KW": "kuwait", "BH": "bahrain",
    "HK": "hong kong", "SS": "china", "SZ": "china",
    "NS": "india", "BO": "india", "JO": "south africa",
    "SA": "brazil", "MX": "mexico",
}


def resolve_investing_slug(ticker: str, company_name: str | None = None,
                            country: str | None = None) -> Optional[str]:
    """Resolve an Investing.com slug via the search API, gated on an entity
    match: the result's numeric symbol must equal the ticker's base AND its
    exchange/country must match the ticker's market — so we never bind to a
    same-numbered stock on another exchange. Network call — used by the offline
    resolver script, not the Render runtime. Returns the slug or None.
    """
    base = ticker.split(".")[0].upper()
    base_nz = base.lstrip("0") or base
    suffix = ticker.split(".")[-1].upper() if "." in ticker else ""
    want = (country or "").strip().lower() or _SUFFIX_COUNTRY.get(suffix, "")
    queries = [q for q in (company_name, base) if q]
    try:
        from curl_cffi import requests as cr
        from urllib.parse import quote as _q
    except Exception:
        return None
    for query in queries:
        try:
            r = cr.get(
                f"https://api.investing.com/api/search/v2/search?q={_q(str(query))}",
                impersonate="chrome120", timeout=12, headers={"domain-id": "www"},
            )
            for item in (r.json().get("quotes") or []):
                url = (item.get("url") or "")
                if not url.startswith("/equities/"):
                    continue
                sym = str(item.get("symbol") or "").upper()
                exch = (item.get("exchange") or "").strip().lower()
                sym_ok = sym == base or (sym.lstrip("0") or sym) == base_nz
                # Require the market to match (when we know it). This is the
                # gate that rejects the cross-exchange numeric collisions.
                country_ok = (not want) or (want in exch) or (exch and exch in want)
                if sym_ok and country_ok:
                    return url.rsplit("/equities/", 1)[-1].strip("/")
        except Exception:
            continue
    return None


# ── HTTP layer (curl_cffi) ───────────────────────────────────────────────

_BASE = "https://www.investing.com/equities"


def _get(url: str, *, timeout: float = 15.0) -> Optional[str]:
    """Single HTTP GET via curl_cffi with Chrome120 TLS fingerprint. Returns
    the response body string on 200, or None on any failure. Cloudflare's
    automated-traffic check is satisfied by the TLS fingerprint alone — no
    cookie or challenge solver is needed."""
    try:
        from curl_cffi import requests as cr
    except ImportError:
        return None
    try:
        r = cr.get(url, impersonate="chrome120", timeout=timeout,
                   headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception:
        return None
    if r.status_code != 200:
        return None
    from src.utils.sanitize import cap_text
    return cap_text(r.text)  # bound untrusted body before JSON/regex parsing


def _next_data(html: str) -> Optional[dict]:
    """Parse the __NEXT_DATA__ JSON blob; return the `pageProps.state` dict
    (with every store JSON-decoded). Returns None on shape problems."""
    if not html:
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        root = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    state = (root.get("props") or {}).get("pageProps", {}).get("state") or {}
    out: dict[str, Any] = {}
    for key, raw in state.items():
        if isinstance(raw, str):
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = raw
        else:
            out[key] = raw
    return out


# ── Disk cache ───────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    return cache_root() / "investing"


def _tracked_dir() -> Path:
    """Repo-tracked Investing snapshot dir. Pre-warmed locally and committed
    so the Render deploy (whose egress IP is Cloudflare-blocked by
    Investing.com) reads from it instead of failing on the network call.
    Refreshed via `python -m scripts.refresh_investing_cache`."""
    from src.config import root
    return root() / "data" / "investing"


def _cache_path(slug: str, kind: str) -> Path:
    return _cache_dir() / f"{slug}__{kind}.json"


def _tracked_path(slug: str, kind: str) -> Path:
    return _tracked_dir() / f"{slug}__{kind}.json"


def _read_cache(slug: str, kind: str, ttl_hours: float = 24) -> Optional[dict]:
    p = _cache_path(slug, kind)
    if not p.exists():
        return None
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    if age > timedelta(hours=ttl_hours):
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_tracked(slug: str, kind: str) -> Optional[dict]:
    """Read the repo-committed snapshot for a slug/kind, or None if absent.
    No TTL — the snapshot is treated as the authoritative-for-now fallback
    when the network is unreachable from this host."""
    p = _tracked_path(slug, kind)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(slug: str, kind: str, payload: dict) -> None:
    p = _cache_path(slug, kind)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, default=str))
    except (OSError, PermissionError):
        # Render's project dir is read-only; cache write failure is non-fatal.
        pass


def _write_tracked(slug: str, kind: str, payload: dict) -> None:
    """Write a snapshot to the repo-tracked dir (used by the refresh script
    only — not from request-path code, since the deployed host is read-only)."""
    p = _tracked_path(slug, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str))


# ── Page-level fetchers (cached) ─────────────────────────────────────────

def _fetch_with_fallback(slug: str, kind: str, url_suffix: str) -> Optional[dict]:
    """Three-layer fetch:
      1. 24h fresh cache (cache/probe/investing/...)
      2. Network via curl_cffi (Cloudflare bypass via TLS impersonation)
      3. Repo-tracked snapshot (data/investing/...) — used when the runtime
         egress IP is blocked by Cloudflare (e.g. Render's cloud range).
    Returns the decoded __NEXT_DATA__ state dict or None when all three miss.
    """
    cached = _read_cache(slug, kind)
    if cached:
        return cached
    html = _get(f"{_BASE}/{slug}{url_suffix}")
    state = _next_data(html) if html else None
    if state:
        _write_cache(slug, kind, state)
        return state
    # Network failed (Cloudflare 403 etc.). Try the repo-tracked snapshot.
    tracked = _read_tracked(slug, kind)
    if tracked:
        return tracked
    return None


def _fetch_equity_page(slug: str) -> Optional[dict]:
    return _fetch_with_fallback(slug, "equity", "")


def _fetch_consensus_page(slug: str) -> Optional[dict]:
    return _fetch_with_fallback(slug, "consensus", "-consensus-estimates")


def _fetch_earnings_page(slug: str) -> Optional[dict]:
    return _fetch_with_fallback(slug, "earnings", "-earnings")


# ── Field extractors ─────────────────────────────────────────────────────

def _equity_instrument(state: dict) -> dict:
    eq = state.get("equityStore") or {}
    return (eq.get("instrument") or {}) if isinstance(eq, dict) else {}


def _equity_price(state: dict) -> dict:
    instr = _equity_instrument(state)
    return instr.get("price") or {}


def _equity_fundamental(state: dict) -> dict:
    instr = _equity_instrument(state)
    return instr.get("fundamental") or {}


def _equity_key_metrics(state: dict) -> dict:
    eq = state.get("equityStore") or {}
    return (eq.get("keyMetrics") or {}) if isinstance(eq, dict) else {}


def _equity_price_changes(state: dict) -> dict:
    eq = state.get("equityStore") or {}
    return (eq.get("priceChanges") or {}) if isinstance(eq, dict) else {}


def _equity_dividends(state: dict) -> list[dict]:
    """Investing's declared-dividend history: a list of
    {div_amount, split_adj_div_amount, div_date, div_payment_type, yield}.
    Newest-first. Used to compute a trailing-12-month yield from ACTUAL
    declared cash dividends rather than Investing's rounded `dividend`
    field or its internally-inconsistent `yield` field."""
    ds = state.get("dividendsStore") or {}
    ed = ds.get("equityDividends") if isinstance(ds, dict) else None
    return ed if isinstance(ed, list) else []


def _trailing_12m_declared_dividend(state: dict) -> Optional[float]:
    """Sum of actual declared cash dividends whose ex-date falls in the
    trailing 12 months. Split-adjusted amounts preferred. Returns None
    when no dated dividend is available.

    This is the institutional trailing-dividend definition (TTM cash
    dividends ÷ price). For annual payers (e.g. BKMB.OM) it's the single
    most-recent declared dividend; for semi/quarterly payers it sums the
    last year's distributions."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    rows = _equity_dividends(state)
    if not rows:
        return None
    now = _dt.now(_tz.utc)
    cutoff = now - _td(days=366)
    total = 0.0
    found = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        amt = r.get("split_adj_div_amount")
        if not isinstance(amt, (int, float)):
            amt = r.get("div_amount")
        if not isinstance(amt, (int, float)) or amt <= 0:
            continue
        d = (r.get("div_date") or "")[:10]
        try:
            ex = _dt.fromisoformat(d).replace(tzinfo=_tz.utc)
        except Exception:
            continue
        # Only count dividends that have already gone ex (declared) and
        # fall inside the trailing-12-month window.
        if cutoff <= ex <= now:
            total += float(amt)
            found = True
    return total if found else None


def _company_profile(state: dict) -> dict:
    cp = state.get("companyProfileStore") or {}
    return (cp.get("profile") or {}) if isinstance(cp, dict) else {}


def _forecast_summary(state: dict) -> dict:
    ce = state.get("consensusEstimatesStore") or {}
    return (ce.get("forecastSummary") or {}) if isinstance(ce, dict) else {}


def _earnings_forecasts(state: dict) -> list[dict]:
    es = state.get("earningsStore") or {}
    fc = es.get("forecasts") if isinstance(es, dict) else None
    return fc if isinstance(fc, list) else []


def _earnings_history(state: dict) -> list[dict]:
    """Historical earnings rows with surprise %, used for the surprise-track
    line. Investing's `earnings` list includes reported vs estimated EPS."""
    es = state.get("earningsStore") or {}
    eh = es.get("earnings") if isinstance(es, dict) else None
    return eh if isinstance(eh, list) else []


# ── Historical-prices helper (used by the deck writer directly, not as a
#   Provider field; lives here for proximity to the rest of the Investing
#   plumbing). ─────────────────────────────────────────────────────────────

def fetch_historical_prices(ticker: str, *, days: int = 380) -> Optional[dict]:
    """Return a 52-week close series for `ticker` from Investing.com.

    Output matches the canonical `historical_prices` shape the slide
    renderers already consume:
        {
          "close_series": [{"date": "YYYY-MM-DD", "close": float}, ...],
          "range_52w_low":  float,
          "range_52w_high": float,
          "perf_1d":  float, "perf_1w": float, "perf_1m": float,
          "perf_3m":  float, "perf_6m": float, "perf_ytd": float,
        }

    Returns None when the ticker has no Investing.com slug or the API
    rejects the request.
    """
    slug = _slug(ticker)
    if not slug:
        return None
    # Disk cache (24h) -> tracked snapshot (no TTL) -> network.
    cached = _read_cache(slug, "historical")
    if cached:
        return cached
    # First fetch the equity page once to grab the instrumentId.
    state = _fetch_equity_page(slug)
    if not state:
        # No equity page at all — fall back to tracked historical if present.
        return _read_tracked(slug, "historical")
    eq = state.get("equityStore") or {}
    iid = eq.get("instrumentId") if isinstance(eq, dict) else None
    if not iid:
        instr = _equity_instrument(state)
        iid = (instr.get("price") or {}).get("instrumentId")
    if not iid:
        return _read_tracked(slug, "historical")
    from datetime import datetime as _dt, timedelta as _td
    end = _dt.utcnow().date()
    start = end - _td(days=days)
    # api.investing.com requires a `domain-id` header (the public endpoints
    # validate against the WAF "Request.Domain" field) — calling without it
    # returns 400. Bypass _get and set the header explicitly.
    url = (
        f"https://api.investing.com/api/financialdata/historical/{iid}"
        f"?start-date={start.isoformat()}&end-date={end.isoformat()}"
        f"&time-frame=Daily&add-missing-rows=false"
    )
    try:
        from curl_cffi import requests as cr
    except ImportError:
        return None
    try:
        r = cr.get(url, impersonate="chrome120", timeout=15,
                   headers={"domain-id": "www",
                            "Accept-Language": "en-US,en;q=0.9"})
    except Exception:
        return _read_tracked(slug, "historical")
    if r.status_code != 200:
        return _read_tracked(slug, "historical")
    try:
        rows = (json.loads(r.text).get("data") or [])
    except json.JSONDecodeError:
        return _read_tracked(slug, "historical")
    def _to_float(v):
        if v is None: return None
        if isinstance(v, (int, float)): return float(v)
        try: return float(str(v))
        except (TypeError, ValueError): return None

    series: list[dict] = []
    for r in rows:
        ts = r.get("rowDateTimestamp") or ""
        close = _to_float(r.get("last_closeRaw"))
        if not ts or close is None:
            continue
        # API returns newest-first; sort oldest-first below.
        series.append({"date": ts[:10], "close": close})
    series.sort(key=lambda x: x["date"])
    if not series:
        return None
    closes = [x["close"] for x in series]
    out: dict[str, Any] = {
        "close_series":   series,
        "range_52w_low":  min(closes),
        "range_52w_high": max(closes),
    }
    # Perf bands — anchor everything off the latest close to keep them
    # internally consistent. Falls back silently when a band is shorter
    # than the requested window (e.g. recent IPO).
    today_close = closes[-1]
    today_date  = series[-1]["date"]
    def _close_at_or_before(target_date_str: str) -> Optional[float]:
        for entry in reversed(series):
            if entry["date"] <= target_date_str:
                return entry["close"]
        return None
    from datetime import date as _date
    today_d = _date.fromisoformat(today_date)
    deltas = {
        "perf_1d":  1, "perf_1w": 7, "perf_1m": 30,
        "perf_3m":  90, "perf_6m": 182, "perf_ytd": 0,
    }
    for key, delta_days in deltas.items():
        if key == "perf_ytd":
            anchor_date = _date(today_d.year, 1, 1).isoformat()
        else:
            anchor_date = (today_d - _td(days=delta_days)).isoformat()
        prev = _close_at_or_before(anchor_date)
        if prev and prev > 0:
            out[key] = (today_close / prev - 1.0) * 100

    # Prefer Investing's OWN published performance percentages over the
    # close-series recomputation above. These are the exact figures the
    # analyst sees on the equity page; recomputing from daily closes uses a
    # calendar-day window that drifts from Investing's definition (most
    # visibly 1W / 1M), so the deck would disagree with the site the user
    # cross-checks against. Published values win when present; the computed
    # band remains the fallback for any null field.
    pc = _equity_price_changes(state)
    if isinstance(pc, dict):
        for okey, pkey in (("perf_1d", "pct_1d"), ("perf_1w", "pct_1w"),
                            ("perf_1m", "pct_1m"), ("perf_3m", "pct_3m"),
                            ("perf_6m", "pct_6m"), ("perf_ytd", "pct_ytd")):
            v = pc.get(pkey)
            if isinstance(v, (int, float)):
                out[okey] = float(v)
        out["perf_source"] = "investing_published"
        upd = pc.get("updated_at")
        if upd:
            out["perf_updated_at"] = upd
    # Persist successful network fetches to the disk cache so subsequent
    # calls hit (free local) instead of (paid network) — and so the local
    # refresh script can collect them all for committing to data/investing.
    try:
        _write_cache(slug, "historical", out)
    except Exception:
        pass
    return out


# ── Public Provider ──────────────────────────────────────────────────────

class InvestingProvider(Provider):
    name = "investing"

    def __init__(self):
        # Per-process in-memory cache. Disk cache (24h) sits underneath.
        self._mem: dict[str, dict] = {}

    def _state(self, ticker: str, kind: str) -> dict:
        slug = _slug(ticker)
        if not slug:
            raise NotImplementedError(f"No Investing.com slug for {ticker}")
        key = f"{slug}::{kind}"
        if key in self._mem:
            return self._mem[key]
        if kind == "equity":
            state = _fetch_equity_page(slug)
        elif kind == "consensus":
            state = _fetch_consensus_page(slug)
        elif kind == "earnings":
            state = _fetch_earnings_page(slug)
        else:
            raise ValueError(f"unknown kind {kind!r}")
        if not state:
            raise ValueError(f"Investing.com {kind} page returned no usable data for {ticker}")
        self._mem[key] = state
        return state

    # ── Required value-fetching methods (Provider interface) ─────────────

    def _fetch_current_price(self, ticker: str):
        state = self._state(ticker, "equity")
        price = _equity_price(state)
        last = price.get("last")
        if not isinstance(last, (int, float)):
            raise ValueError("equity page had no `last` price")
        raw_id = persist_raw(self.name, ticker, "current_price", price)
        return float(last), (price.get("currency") or ""), "", raw_id

    def _fetch_market_cap(self, ticker: str):
        """Market cap in raw listing-currency units (e.g. HKD for HK names).

        Important for HK tickers where MarketScreener appears to report only
        the H-share count, underestimating total market cap by ~13%.
        Investing's fundamental.marketCapRaw includes both A- and H-share
        floats and matches the live equity page.
        """
        state = self._state(ticker, "equity")
        fund = _equity_fundamental(state)
        mcap = fund.get("marketCapRaw")
        if not isinstance(mcap, (int, float)) or mcap <= 0:
            raise ValueError("equity page had no marketCapRaw")
        price = _equity_price(state)
        currency = (price.get("currency") or "").upper()
        raw_id = persist_raw(self.name, ticker, "market_cap", fund)
        return float(mcap), currency, "", raw_id

    def _fetch_dividend_yield(self, ticker: str):
        state = self._state(ticker, "equity")
        fundamental = _equity_fundamental(state)
        price_block = _equity_price(state)

        last = price_block.get("last")

        # PRIMARY: trailing-12-month ACTUAL DECLARED cash dividends ÷ price.
        # This is the standard institutional trailing-yield definition and
        # the most defensible number when sources disagree. Investing's own
        # `yield` field is internally inconsistent (e.g. BKMB.OM 2026-05:
        # reports 6.90% while its dividend 0.02 / price 0.414 = 4.83%), and
        # its `dividend` field is rounded (0.02 vs the actual declared 0.018).
        # Using the dated dividend history avoids both distortions.
        ttm_div = _trailing_12m_declared_dividend(state)
        if (isinstance(last, (int, float)) and last > 0
            and isinstance(ttm_div, (int, float)) and ttm_div > 0):
            ttm_yield = ttm_div / last * 100
            return ttm_yield, "%", "", persist_raw(
                self.name, ticker, "dividend_yield",
                {**fundamental, "ttm_declared_dividend": ttm_div,
                 "ttm_yield_computed": ttm_yield,
                 "method": "trailing_12m_declared_dividend_over_price"})

        # FALLBACK 1: Investing's `dividend` field ÷ price (rounded run-rate).
        dividend = fundamental.get("dividend")
        if (isinstance(last, (int, float)) and last > 0
            and isinstance(dividend, (int, float)) and dividend > 0):
            computed_yield = dividend / last * 100
            raw_yield = fundamental.get("yield")
            # Within 25% — trust the raw field (Investing's `yield` often
            # uses TTM-inclusive-of-special-dividends, while `dividend` is
            # the regular annual run-rate; small divergences are convention
            # rather than data bugs).
            if (isinstance(raw_yield, (int, float)) and raw_yield > 0
                and abs(raw_yield - computed_yield) / max(raw_yield, computed_yield) < 0.25):
                return float(raw_yield), "%", "", persist_raw(self.name, ticker, "dividend_yield", fundamental)
            # Gap > 25% — Investing's yield field is internally inconsistent
            # with its own dividend/price. Fall back to the computed value.
            return computed_yield, "%", "", persist_raw(self.name, ticker, "dividend_yield",
                                                          {**fundamental, "yield_computed": computed_yield})

        # No dividend/price pair — fall back to the raw yield field.
        for key in ("yield", "dividend_yield", "dividendYield", "div_yield"):
            v = fundamental.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v), "%", "", persist_raw(self.name, ticker, "dividend_yield", fundamental)
        km = _equity_key_metrics(state)
        for key in ("dividendYield", "dividend_yield", "yield"):
            v = km.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v), "%", "", persist_raw(self.name, ticker, "dividend_yield", km)
        raise ValueError("dividend yield not in equity page fundamentals")

    def _fetch_target_price(self, ticker: str):
        state = self._state(ticker, "consensus")
        fs = _forecast_summary(state)
        mean = fs.get("target_price_consensus_mean")
        if not isinstance(mean, (int, float)):
            raise ValueError("Investing.com consensus page had no target_price_consensus_mean")
        raw_id = persist_raw(self.name, ticker, "target_price", fs)
        return {
            "mean": float(mean),
            "high": fs.get("target_price_consensus_high"),
            "low":  fs.get("target_price_consensus_low"),
            "n_analysts": fs.get("number_of_estimates")
                          or sum(int(fs.get(k) or 0) for k in (
                              "number_of_analysts_buy", "number_of_analysts_hold",
                              "number_of_analysts_sell")),
        }, "", "", raw_id

    def _fetch_rating_split(self, ticker: str):
        state = self._state(ticker, "consensus")
        fs = _forecast_summary(state)
        buy  = int(fs.get("number_of_analysts_buy")  or 0)
        hold = int(fs.get("number_of_analysts_hold") or 0)
        sell = int(fs.get("number_of_analysts_sell") or 0)
        if not (buy or hold or sell):
            raise ValueError("Investing.com consensus page had no analyst counts")
        consensus = (fs.get("consensus_recommendation") or "").strip() or None
        if not consensus:
            total = max(1, buy + hold + sell)
            if buy / total >= 0.6:   consensus = "BUY"
            elif sell / total >= 0.4: consensus = "SELL"
            elif buy > sell:          consensus = "OUTPERFORM"
            else:                     consensus = "HOLD"
        raw_id = persist_raw(self.name, ticker, "rating_split", fs)
        return {
            "buy": buy, "hold": hold, "sell": sell,
            "total": buy + hold + sell, "consensus": consensus,
        }, "", "", raw_id

    def _fetch_valuation_forward(self, ticker: str):
        state = self._state(ticker, "earnings")
        forecasts = _earnings_forecasts(state)
        if not forecasts:
            raise ValueError("Investing.com earnings page had no forecasts")
        # Also grab the equity-page price so we can derive forward P/E
        # ratios. The snapshot writer reads `pe_fy1` etc. from
        # valuation_forward; since Investing wins canonical above MS now,
        # this dict needs those keys too — otherwise the slide-1 chip
        # blanks.
        last_price = None
        try:
            equity = self._state(ticker, "equity")
            last_price = _equity_price(equity).get("last")
        except Exception:
            last_price = None
        # Sort forecasts in calendar order so we know which is the next print
        # and which roll up into FY+1 / FY+2 totals.
        rows = sorted(
            (f for f in forecasts if isinstance(f.get("reportYear"), int)),
            key=lambda f: (f["reportYear"], f.get("reportMonth") or 0),
        )
        if not rows:
            raise ValueError("Investing.com forecasts had no parseable rows")
        nxt = rows[0]
        # Pull historical actuals from the same earnings page so FY1
        # aggregates include quarters that already reported. Without this,
        # a forecast list that only covers Q2-Q4 of the current FY produces
        # a 3-quarter EPS sum that inflates derived P/E by ~33% (SABIC's
        # P/E (FY EST) showed 17.0x when the correct FY P/E was ~12.7x).
        history = _earnings_history(state)
        actuals_by_qp: dict[tuple[int, int], dict] = {}
        for r in history or []:
            if not isinstance(r, dict): continue
            yr = r.get("reportYear"); mo = r.get("reportMonth") or 0
            if not (isinstance(yr, int) and isinstance(mo, int) and mo): continue
            qn_ = (mo - 1) // 3 + 1
            # Only keep rows with an actual EPS reported.
            if isinstance(r.get("epsActual"), (int, float)):
                actuals_by_qp[(yr, qn_)] = r

        def _fy_agg(year: int) -> tuple[Optional[float], Optional[float]]:
            # Walk Q1-Q4 explicitly so we know which quarters we're missing.
            seen_quarters: set[int] = set()
            rev_total = eps_total = 0.0
            for qn_ in (1, 2, 3, 4):
                # Forecast first (most recent), then actual.
                fc = next((r for r in rows if r.get("reportYear") == year
                            and ((r.get("reportMonth") or 0) - 1) // 3 + 1 == qn_), None)
                ac = actuals_by_qp.get((year, qn_))
                rev_val = (fc.get("revenue") if fc else None) \
                           or (ac.get("revenueActual") if ac else None)
                eps_val = (fc.get("eps") if fc else None) \
                           or (ac.get("epsActual") if ac else None)
                if isinstance(rev_val, (int, float)): rev_total += rev_val
                if isinstance(eps_val, (int, float)): eps_total += eps_val
                if rev_val is not None or eps_val is not None:
                    seen_quarters.add(qn_)
            # Only return numbers when we have at least 3 quarters covered;
            # otherwise the partial sum produces a misleading P/E.
            if len(seen_quarters) < 3:
                return None, None
            return (rev_total or None), (eps_total or None)

        fy1_year = nxt["reportYear"]
        fy2_year = fy1_year + 1
        rev_fy1, eps_fy1 = _fy_agg(fy1_year)
        rev_fy2, eps_fy2 = _fy_agg(fy2_year)
        # "Period" string Investing implies: Q from reportMonth.
        rm = nxt.get("reportMonth") or 0
        qn = ((rm - 1) // 3 + 1) if rm else 0
        next_q_period = f"Q{qn} {nxt['reportYear']}" if qn else ""
        # Forward P/E ratios — derived from last_price / EPS forecast. The
        # snapshot writer reads these on slide 1 ("P/E (FY EST)" chip).
        def _pe(eps_val):
            try:
                if isinstance(eps_val, (int, float)) and eps_val > 0 and last_price:
                    return float(last_price) / float(eps_val)
            except Exception:
                pass
            return None
        pe_fy1 = _pe(eps_fy1)
        pe_fy2 = _pe(eps_fy2)
        raw_id = persist_raw(self.name, ticker, "valuation_forward", {"forecasts": rows})
        return {
            "fy1_year":   fy1_year,
            "eps_fy1":    eps_fy1,
            "revenue_fy1": rev_fy1,
            "pe_fy1":     pe_fy1,
            "fy2_year":   fy2_year,
            "eps_fy2":    eps_fy2,
            "revenue_fy2": rev_fy2,
            "pe_fy2":     pe_fy2,
            "next_q_period":  next_q_period,
            "next_q_report_date": None,
            "eps_next_q":     nxt.get("eps"),
            "revenue_next_q": nxt.get("revenue"),
        }, "", "", raw_id

    def _fetch_income_statement_quarterly(self, ticker: str):
        """Surprise history — used by the thesis renderer as a track-record
        anchor. Investing's `earnings` list pre-computes the surprise%."""
        state = self._state(ticker, "earnings")
        history = _earnings_history(state)
        if not history:
            raise ValueError("Investing.com earnings page had no history rows")
        # Normalize to the shape the renderer already consumes (period_end,
        # eps_actual, eps_estimate, eps_surprise_pct, revenue_actual,
        # revenue_estimate, revenue_surprise_pct).
        out: list[dict] = []
        for row in history:
            if not isinstance(row, dict):
                continue
            yr = row.get("reportYear")
            mo = row.get("reportMonth") or 0
            qn = ((mo - 1) // 3 + 1) if isinstance(mo, int) and mo else 0
            period = f"Q{qn} {yr}" if qn and yr else ""
            out.append({
                "period": period,
                "eps_actual":    row.get("eps") or row.get("epsActual"),
                "eps_estimate":  row.get("epsForecast") or row.get("epsEstimate"),
                # Investing returns the full word `Percent`, not abbreviated.
                # Previous key (`epsSurprisePct`) always returned None, so the
                # downstream "N of last 4 above consensus" line defaulted to 0.
                "eps_surprise_pct":      row.get("epsSurprisePercent") or row.get("epsSurprisePct"),
                "revenue_actual":        row.get("revenue") or row.get("revenueActual"),
                "revenue_estimate":      row.get("revenueForecast") or row.get("revenueEstimate"),
                "revenue_surprise_pct":  row.get("revenueSurprisePercent") or row.get("revenueSurprisePct"),
            })
        if not out:
            raise ValueError("Investing.com history could not be normalised")
        raw_id = persist_raw(self.name, ticker, "income_statement_quarterly", {"surprise_history": out})
        return {"surprise_history": out}, "", "", raw_id
