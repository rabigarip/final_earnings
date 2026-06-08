"""Live source verification — "check the website yourself."

After a deck is built (its numbers come from the reconciled canonical_store),
this module RE-FETCHES the same fields live from the public sources and
confirms the rendered numbers match. It is the trust gate an analyst needs:
proof that what's on the slide agrees with Investing.com / Yahoo right now,
not a stale snapshot.

For each field we compare the canonical (= rendered) value against every
live source that returns it, within a tolerance, and emit a PASS / WARN /
FAIL verdict plus the raw source numbers so any disagreement is auditable.

Run standalone:
    python -m src.services.verify_sources 1180.SR
or import `verify_ticker(ticker)` and inspect the returned dict.
"""
from __future__ import annotations
from typing import Any, Optional
import warnings
warnings.filterwarnings("ignore")


def _pct_diff(a: float, b: float) -> float:
    if a is None or b is None:
        return float("inf")
    if a == 0 and b == 0:
        return 0.0
    base = max(abs(a), abs(b))
    return abs(a - b) / base * 100.0 if base else 0.0


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _live_investing(ticker: str) -> dict:
    """Live Investing.com via curl_cffi (the Cloudflare-bypass path)."""
    out: dict = {}
    try:
        from src.providers.probe_investing import InvestingProvider
        inv = InvestingProvider()
        for field in ("current_price", "rating_split", "target_price",
                       "dividend_yield", "valuation_forward"):
            try:
                cell = inv.fetch(ticker, field)
                if cell and not cell.error and cell.value is not None:
                    out[field] = cell.value
            except Exception:
                pass
    except Exception:
        pass
    return out


def _live_yahoo(ticker: str) -> dict:
    """Live Yahoo via yfinance (not Cloudflare-blocked)."""
    out: dict = {}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        out["current_price"] = info.get("currentPrice") or info.get("regularMarketPrice")
        out["target_mean"] = info.get("targetMeanPrice")
        out["n_analysts"] = info.get("numberOfAnalystOpinions")
        out["trailing_pe"] = info.get("trailingPE")
        out["forward_pe"] = info.get("forwardPE")
        # `dividendYield` is already a percent in current yfinance and is the
        # field the deck uses; `trailingAnnualDividendYield` is an unreliable
        # cross-check (wrong for 9988.HK), so don't reference it here.
        dy = info.get("dividendYield")
        out["dividend_yield_pct"] = float(dy) if dy is not None else None
    except Exception:
        pass
    return out


def _canonical(ticker: str) -> dict:
    """What the DECK shows — the reconciled canonical_store values."""
    out: dict = {}
    try:
        from src.services.canonical_store import get_all_fields
        cv = get_all_fields(ticker)

        def val(f):
            c = cv.get(f)
            return c.value if c else None

        out["current_price"] = _num(val("current_price"))
        rs = val("rating_split") or {}
        if isinstance(rs, dict):
            out["rating_total"] = _num(rs.get("total"))
            out["rating_consensus"] = rs.get("consensus")
        tp = val("target_price") or {}
        if isinstance(tp, dict):
            out["target_mean"] = _num(tp.get("mean"))
            out["n_analysts"] = _num(tp.get("n_analysts"))
        out["dividend_yield_pct"] = _num(val("dividend_yield"))
        vf = val("valuation_forward") or {}
        if isinstance(vf, dict):
            # Yahoo bundles use forward_pe; Investing/MS use pe_fy1.
            out["forward_pe"] = _num(vf.get("forward_pe") or vf.get("pe_fy1") or vf.get("fwd_pe"))
        # canonical source attribution (who won each field)
        out["_sources"] = {f: getattr(cv.get(f), "canonical_source", "")
                           for f in ("current_price", "rating_split", "target_price",
                                     "dividend_yield", "valuation_forward") if cv.get(f)}
    except Exception:
        pass
    return out


# Per-field tolerance (% diff) — prices move intraday, estimates are point-ish.
_TOL = {
    "current_price": 3.0,
    "target_mean": 5.0,
    "n_analysts": 0.0,          # exact-ish; counts shift slowly
    "dividend_yield_pct": 8.0,
    "forward_pe": 8.0,
}


def verify_ticker(ticker: str) -> dict:
    """Cross-check the canonical (rendered) numbers against live sources.

    Returns {ticker, checks:[{field, deck, investing, yahoo, verdict, note}],
    summary:{pass, warn, fail}}.
    """
    ticker = ticker.strip().upper()
    deck = _canonical(ticker)
    inv = _live_investing(ticker)
    yh = _live_yahoo(ticker)

    inv_target = (inv.get("target_price") or {}).get("mean") if isinstance(inv.get("target_price"), dict) else None
    inv_div = inv.get("dividend_yield")
    checks = []

    def add(field, deck_v, live_pairs):
        """live_pairs: list of (source_name, value)."""
        deck_n = _num(deck_v)
        lives = [(s, _num(v)) for s, v in live_pairs if _num(v) is not None]
        tol = _TOL.get(field, 5.0)
        verdict, note = "NO-DATA", ""
        if deck_n is None and not lives:
            verdict = "NO-DATA"
        elif deck_n is None:
            verdict = "WARN"; note = "deck blank but a live source has it"
        elif not lives:
            verdict = "WARN"; note = "no live source to cross-check"
        else:
            diffs = [(s, _pct_diff(deck_n, lv)) for s, lv in lives]
            best = min(d for _, d in diffs)
            if best <= max(tol, 0.01) or (field == "n_analysts" and best <= 12.5):
                verdict = "PASS"
            else:
                verdict = "FAIL"
                note = "; ".join(f"{s} {lv} vs deck {deck_n} ({d:.1f}%)"
                                 for (s, lv), (_, d) in zip(lives, diffs))
        checks.append({
            "field": field, "deck": deck_n,
            "investing": next((v for s, v in live_pairs if s == "investing"), None),
            "yahoo": next((v for s, v in live_pairs if s == "yahoo"), None),
            "verdict": verdict, "note": note,
        })

    add("current_price", deck.get("current_price"),
        [("investing", inv.get("current_price")), ("yahoo", yh.get("current_price"))])
    add("target_mean", deck.get("target_mean"),
        [("investing", inv_target), ("yahoo", yh.get("target_mean"))])
    add("n_analysts", deck.get("n_analysts"),
        [("investing", (inv.get("target_price") or {}).get("n_analysts") if isinstance(inv.get("target_price"), dict) else None),
         ("yahoo", yh.get("n_analysts"))])
    add("dividend_yield_pct", deck.get("dividend_yield_pct"),
        [("investing", inv_div), ("yahoo", yh.get("dividend_yield_pct"))])
    inv_vf = inv.get("valuation_forward") if isinstance(inv.get("valuation_forward"), dict) else {}
    inv_fpe = (inv_vf or {}).get("forward_pe") or (inv_vf or {}).get("pe_fy1")
    add("forward_pe", deck.get("forward_pe"),
        [("investing", inv_fpe), ("yahoo", yh.get("forward_pe"))])

    summary = {"pass": sum(c["verdict"] == "PASS" for c in checks),
               "warn": sum(c["verdict"] in ("WARN", "NO-DATA") for c in checks),
               "fail": sum(c["verdict"] == "FAIL" for c in checks)}
    return {"ticker": ticker, "sources": deck.get("_sources", {}),
            "checks": checks, "summary": summary}


def format_report(rep: dict) -> str:
    lines = [f"\n=== SOURCE VERIFICATION · {rep['ticker']} ===",
             f"canonical sources: {rep.get('sources', {})}"]
    lines.append(f"{'FIELD':<20}{'DECK':>12}{'INVESTING':>14}{'YAHOO':>12}  VERDICT")
    for c in rep["checks"]:
        d = "—" if c["deck"] is None else f"{c['deck']:.4g}"
        iv = "—" if c["investing"] is None else f"{_num(c['investing']):.4g}" if _num(c["investing"]) is not None else "—"
        yv = "—" if c["yahoo"] is None else f"{_num(c['yahoo']):.4g}" if _num(c["yahoo"]) is not None else "—"
        mark = {"PASS": "✓ PASS", "FAIL": "✗ FAIL", "WARN": "⚠ WARN", "NO-DATA": "· n/a"}[c["verdict"]]
        lines.append(f"{c['field']:<20}{d:>12}{iv:>14}{yv:>12}  {mark}"
                     + (f"  [{c['note']}]" if c["note"] else ""))
    s = rep["summary"]
    lines.append(f"SUMMARY: {s['pass']} pass · {s['warn']} warn · {s['fail']} fail")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for tk in sys.argv[1:] or ["1180.SR"]:
        print(format_report(verify_ticker(tk)))
