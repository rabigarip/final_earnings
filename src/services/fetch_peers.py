"""Peer-group valuation helper."""

from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path
import yfinance as yf

logger = logging.getLogger(__name__)


# ── Persistent P/B cache ─────────────────────────────────────────
#
# Yahoo's `priceToBook` field flakes intermittently for our GCC peer
# universe (QNBK.QA, 1180.SR, ENBD.AE, etc.) — same ticker returns a
# real value one call and `null` the next. Investing's equity page
# doesn't carry book value at all. The fix: cache the most recent
# successful value per peer and serve it when the live call returns
# nothing usable. The cache file is in /tmp on Render (writable) or
# the repo's `cache/` dir locally.

def _peer_cache_path() -> Path:
    """Resolve a writable cache file. Falls back to /tmp on read-only
    Render filesystems."""
    try:
        from src.config import root
        p = root() / "cache" / "peer_pb_cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        # Probe write — bail to /tmp if read-only.
        if not p.exists():
            p.write_text("{}", encoding="utf-8")
        return p
    except Exception:
        return Path("/tmp/earnings-peer-pb-cache.json")


def _read_peer_cache() -> dict[str, dict]:
    try:
        return json.loads(_peer_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_peer_cache(cache: dict[str, dict]) -> None:
    try:
        _peer_cache_path().write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def _iso_now() -> str:
    """UTC ISO-8601 timestamp for cache annotation."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _derive_pb(info: dict, current_price: float | None) -> float | None:
    """Pull P/B from yfinance info with two derivations as fallback.

    1. `priceToBook` — the official field
    2. `priceToBookRatio` — older yfinance / alternate naming
    3. `current_price / bookValue` — compute when only book-per-share is given

    Returns None when none of those produce a positive ratio.
    """
    raw = info.get("priceToBook")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    alt = info.get("priceToBookRatio")
    if isinstance(alt, (int, float)) and alt > 0:
        return float(alt)
    bv = info.get("bookValue")
    if (isinstance(bv, (int, float)) and bv > 0
            and isinstance(current_price, (int, float)) and current_price > 0):
        return float(current_price) / float(bv)
    return None


def fetch_peer_multiples(peer_tickers: list[str]) -> dict:
    """Fetch peer forward P/E and EV/EBITDA medians from Yahoo info."""
    pe_values: list[float] = []
    ev_ebitda_values: list[float] = []
    for t in peer_tickers or []:
        tt = (t or "").strip().upper()
        if not tt:
            continue
        try:
            info = yf.Ticker(tt).info or {}
        except Exception:
            continue
        pe = info.get("forwardPE") or info.get("trailingPE")
        if isinstance(pe, (int, float)) and pe > 0:
            pe_values.append(float(pe))
        ev = info.get("enterpriseValue")
        ebitda = info.get("ebitda")
        if isinstance(ev, (int, float)) and isinstance(ebitda, (int, float)) and ebitda > 0:
            ev_ebitda_values.append(float(ev) / float(ebitda))
    return {
        "pe_sector_median": round(statistics.median(pe_values), 1) if pe_values else None,
        "ev_ebitda_sector_median": round(statistics.median(ev_ebitda_values), 1) if ev_ebitda_values else None,
        "peer_count": len(pe_values),
    }


def _fmt_mcap_usd(v) -> str:
    if not isinstance(v, (int, float)) or v <= 0:
        return "—"
    if v >= 1e12: return f"${v/1e12:,.2f}T"
    if v >= 1e9:  return f"${v/1e9:,.1f}B"
    if v >= 1e6:  return f"${v/1e6:,.0f}M"
    return f"${v:,.0f}"


# USD conversion. GCC currencies are USD-pegged (fixed), so these rates are
# stable and need no live FX feed; the few floating majors are approximate
# and only affect the relative SIZE gauge, not the per-share ratios. We
# unify all peer market caps to USD so the column is comparable and a peer
# AVERAGE market cap can be shown.
_USD_PER_UNIT = {
    "USD": 1.0, "OMR": 2.6008, "AED": 0.2723, "SAR": 0.2667, "QAR": 0.2747,
    "BHD": 2.6596, "KWD": 3.26, "JOD": 1.41, "EGP": 0.0205, "HKD": 0.1282,
    "CNY": 0.139, "CNH": 0.139, "INR": 0.0120, "GBP": 1.27, "GBX": 0.0127,
    "EUR": 1.08, "JPY": 0.0064,
    # Peer sets reach beyond the GCC (e.g. Yara in NOK, EM/DM comparables) —
    # without these a peer's market cap fell back to its native currency under
    # a "MCAP (USD)" header. Approximate spot rates; peer-cap display only.
    "NOK": 0.092, "SEK": 0.094, "DKK": 0.145, "CHF": 1.11, "CAD": 0.73,
    "AUD": 0.65, "NZD": 0.60, "TWD": 0.0312, "KRW": 0.00072, "SGD": 0.74,
    "THB": 0.0285, "MYR": 0.222, "IDR": 0.0000615, "PHP": 0.0175,
    "BRL": 0.18, "MXN": 0.058, "ZAR": 0.055, "TRY": 0.030, "PLN": 0.25,
    "NGN": 0.00065, "PKR": 0.0036, "VND": 0.0000393,
}


def _to_usd(v, currency) -> float | None:
    """Convert a market cap in `currency` to USD. None when not convertible."""
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    rate = _USD_PER_UNIT.get((currency or "").upper())
    return v * rate if rate else None


def _fmt_mcap_labeled(v, currency: str) -> str:
    """Fallback label for a market cap we can't convert to USD (unknown
    currency): show the native magnitude with its ISO code so it's at least
    not a bare unlabeled number."""
    base = _fmt_mcap_usd(v)
    if base == "—":
        return "—"
    cur = (currency or "").upper()
    if cur == "USD" or not cur:
        return base
    return f"{cur} {base.replace('$', '')}"


def _mcap_usd_fields(mcap, currency) -> tuple:
    """(usd_value, display_str) for a peer market cap. Converts to USD when
    the currency is known ('$8.1B'); otherwise falls back to a native
    labeled value and a None usd (so it's excluded from the USD average)."""
    usd = _to_usd(mcap, currency)
    if usd is not None:
        return usd, _fmt_mcap_usd(usd)
    return None, _fmt_mcap_labeled(mcap, currency)


def _peer_row_from_investing(ticker: str) -> dict | None:
    """Build a peer-table row from Investing.com using the same three-layer
    fallback as probe_investing (24h cache → live network → repo-tracked
    snapshot under data/investing/). Returns None when the ticker has no
    curated slug AND no snapshot.

    Critically, this routes through _fetch_equity_page rather than its own
    curl_cffi call so it picks up the data/investing/ snapshot on Render
    (Cloudflare blocks Render's egress IP from reaching Investing live).
    """
    try:
        from src.providers.probe_investing import (
            _slug, _fetch_equity_page, _equity_instrument,
        )
    except ImportError:
        return None
    slug = _slug(ticker)
    if not slug:
        # Search the API only as last resort — it also fails from Render's
        # blocked IP, so most non-curated tickers can't be resolved live.
        try:
            from curl_cffi import requests as cr
            r = cr.get(
                f"https://api.investing.com/api/search/v2/search?q={ticker.upper().replace('.','+')}&page=1&size=10",
                impersonate="chrome120", timeout=10, headers={"domain-id": "www"},
            )
            quotes = (r.json() or {}).get("quotes") or []
            for it in quotes:
                url = it.get("url") or ""
                if "/equities/" in url:
                    slug = url.replace("/equities/", "")
                    break
        except Exception:
            return None
    if not slug:
        return None
    state = _fetch_equity_page(slug)
    if not state:
        return None
    instr = _equity_instrument(state)
    if not instr:
        return None
    price_block = instr.get("price") or {}
    fund = instr.get("fundamental") or {}
    name = (instr.get("englishName") or {}).get("shortName") \
        or (instr.get("englishName") or {}).get("fullName") \
        or ticker
    currency = (price_block.get("currency") or "").upper()
    mcap = fund.get("marketCapRaw")
    mcap_usd, mcap_fmt = _mcap_usd_fields(mcap, currency)
    pe = fund.get("ratio") if isinstance(fund.get("ratio"), (int, float)) else None
    pe_fmt = f"{pe:.1f}x" if pe else "—"
    div = fund.get("yield")
    div_fmt = f"{float(div):.2f}%" if isinstance(div, (int, float)) and div > 0 else "—"
    ret_1y = fund.get("oneYearReturn") if isinstance(fund.get("oneYearReturn"), (int, float)) else None
    ret_1y_fmt = f"{ret_1y:+.1f}%" if isinstance(ret_1y, (int, float)) else "—"
    pb = fund.get("priceToBook") if isinstance(fund.get("priceToBook"), (int, float)) else None
    pb_fmt = f"{pb:.1f}x" if pb else "—"
    ev_ebitda = fund.get("enterpriseToEbitda") if isinstance(fund.get("enterpriseToEbitda"), (int, float)) else None
    ev_ebitda_fmt = f"{ev_ebitda:.1f}x" if ev_ebitda else "—"
    return {
        "name": name, "ticker": ticker,
        "market_cap_fmt": mcap_fmt, "market_cap_usd": mcap_usd,
        "pe": pe, "pe_fmt": pe_fmt,
        "pb": pb, "pb_fmt": pb_fmt,
        "ev_ebitda": ev_ebitda, "ev_ebitda_fmt": ev_ebitda_fmt,
        "div_yield_fmt": div_fmt,
        "ret_1y": ret_1y, "ret_1y_fmt": ret_1y_fmt,
    }


# Per-process memo so the slide-3 peer table and the LLM context's
# peer-average (computed in two separate call sites within one render)
# share ONE fetch — otherwise live-price drift between the two calls made
# the highlight cite e.g. 9.1x while the table showed 9.0x.
_PEER_ROWS_MEMO: dict[tuple, list[dict]] = {}


def fetch_peer_rows(peer_tickers: list[str]) -> list[dict]:
    """Fetch one peer-table row per ticker.

    Primary: yfinance (`Ticker(t).info` + 1y history). Falls back to
    Investing.com for tickers yfinance can't resolve (UAE / Oman peers
    like ENBD.AE, FAB.AE, ENBD.AD). Tickers that fail both sources are
    skipped with a log warning.

    Used by the slide-3 peer comparables table AND the VALUATION-pill
    peer average; memoized per process so both see identical numbers.
    """
    _memo_key = tuple(peer_tickers or [])
    if _memo_key in _PEER_ROWS_MEMO:
        return _PEER_ROWS_MEMO[_memo_key]
    rows: list[dict] = []
    pb_cache = _read_peer_cache()
    cache_dirty = False
    for t in peer_tickers or []:
        tt = (t or "").strip()
        if not tt:
            continue
        try:
            tk = yf.Ticker(tt)
            info = tk.info or {}
        except Exception as exc:
            logger.warning("peer fetch failed for %s: %s", tt, exc)
            info = {}
            tk = None
        # If yfinance returned nothing usable, try Investing.com.
        if not (info.get("longName") or info.get("shortName") or info.get("marketCap")):
            inv_row = _peer_row_from_investing(tt)
            if inv_row:
                rows.append(inv_row)
            else:
                # Last resort: ticker-only row so the table layout doesn't break.
                rows.append({
                    "name": tt, "ticker": tt,
                    "market_cap_fmt": "—", "pe": None, "pe_fmt": "—",
                    "pb": None, "pb_fmt": "—",
                    "ev_ebitda": None, "ev_ebitda_fmt": "—",
                    "div_yield_fmt": "—", "ret_1y": None, "ret_1y_fmt": "—",
                })
            continue
        name = info.get("longName") or info.get("shortName") or tt
        _mcap_native = info.get("marketCap")
        # Yahoo's marketCap is in the listed currency; convert to USD via the
        # peg/FX table so the peer column is comparable and averageable.
        currency = (info.get("currency") or "").upper()
        mcap_usd, mcap_fmt = _mcap_usd_fields(_mcap_native, currency)
        pe = info.get("trailingPE") or info.get("forwardPE")
        pe_val = float(pe) if isinstance(pe, (int, float)) and pe > 0 else None
        pe_fmt = f"{pe_val:.1f}x" if pe_val else "—"
        div_y = info.get("dividendYield")
        div_fmt = "—"
        if isinstance(div_y, (int, float)) and div_y > 0:
            # Yahoo gives dividendYield as a decimal (0.0472) for some,
            # but as a percentage (4.72) for others. Heuristic: <1 → decimal.
            div_pct = div_y * 100 if div_y < 1 else div_y
            div_fmt = f"{div_pct:.2f}%"
        # 1Y return: derive from 52w high/low if not in info, or use history
        ret_1y = None
        try:
            hist = tk.history(period="1y")
            if not hist.empty and "Close" in hist.columns:
                first = float(hist["Close"].iloc[0])
                last = float(hist["Close"].iloc[-1])
                if first > 0:
                    ret_1y = (last / first - 1.0) * 100
        except Exception:
            ret_1y = None
        ret_1y_fmt = f"{ret_1y:+.1f}%" if isinstance(ret_1y, (int, float)) else "—"
        # P/B (Yahoo: priceToBook). For banks this doubles as our P/TBV
        # proxy — yfinance doesn't expose tangibleBookValue separately, so
        # bank decks render P/B in the P/TBV column with that caveat.
        # Yahoo flakes on `priceToBook` for GCC peers — `_derive_pb`
        # tries the field, then the legacy field name, then computes from
        # price ÷ bookValue. If that ALSO fails, fall back to the most
        # recent cached value (preserves continuity across runs and
        # prevents the table from flipping from 1.5x → '—' → 1.5x on
        # successive generations).
        cur_price_for_pb = info.get("currentPrice") or info.get("regularMarketPrice")
        pb_val = _derive_pb(info, cur_price_for_pb)
        if pb_val is not None:
            # Persist the fresh value so next run's flake doesn't surface.
            pb_cache[tt] = {"pb": pb_val, "as_of": _iso_now()}
            cache_dirty = True
        else:
            cached = pb_cache.get(tt) or {}
            cached_pb = cached.get("pb")
            if isinstance(cached_pb, (int, float)) and cached_pb > 0:
                pb_val = float(cached_pb)
                logger.info("peer %s: priceToBook unavailable live — using cached %.2fx (as of %s)",
                            tt, pb_val, cached.get("as_of") or "?")
        pb_fmt = f"{pb_val:.1f}x" if pb_val else "—"
        # EV/EBITDA (Yahoo: enterpriseToEbitda). Meaningless for banks.
        ev_raw = info.get("enterpriseToEbitda")
        ev_ebitda_val = float(ev_raw) if isinstance(ev_raw, (int, float)) and ev_raw > 0 else None
        ev_ebitda_fmt = f"{ev_ebitda_val:.1f}x" if ev_ebitda_val else "—"
        rows.append({
            "name": name,
            "ticker": tt,
            "market_cap_fmt": mcap_fmt,
            "market_cap_usd": mcap_usd,
            "pe": pe_val,
            "pe_fmt": pe_fmt,
            "pb": pb_val,
            "pb_fmt": pb_fmt,
            "ev_ebitda": ev_ebitda_val,
            "ev_ebitda_fmt": ev_ebitda_fmt,
            "div_yield_fmt": div_fmt,
            "ret_1y": ret_1y,
            "ret_1y_fmt": ret_1y_fmt,
        })
    if cache_dirty:
        _write_peer_cache(pb_cache)
    _PEER_ROWS_MEMO[_memo_key] = rows
    return rows

