"""
Builders for the MS-extras slide-context dataclasses.

Lives separate from `build_report_context.py` so the existing 1,300-line
file stays readable and the new contract is easy to test in isolation.
Each builder takes the relevant section dict from `ReportPayload` and
returns the typed slide payload. None inputs map to empty/has_data=False
outputs — never to exceptions.
"""

from __future__ import annotations

from typing import Any, Optional

from src.models.report_context import (
    BrokerAction,
    CompositeRating,
    CourseRange,
    IncomeEvolutionData,
    PeerRow,
    PerformanceCell,
    PriceActionData,
    QuarterlyIncomeSeries,
    QuarterlySurpriseSeries,
    RatingsData,
    SectorComparisonData,
)


# ─────────────────────────────────────────────────────────────────────────────
# Slide 4: Ratings & Sentiment
# ─────────────────────────────────────────────────────────────────────────────

# MS publishes the four composite ratings in this order on /ratings/. We
# render them in the same order so the slide visually matches the website.
_COMPOSITE_ORDER = ("Trader", "Investor", "Global", "Quality")

# Cap bullet counts: MS sometimes returns 7+ strengths and the slide can't
# fit more than five comfortably without shrinking the font. The trim is
# applied in the builder so renderers stay layout-only.
_MAX_BULLETS = 5


def build_ratings(ms_ratings: dict | None) -> RatingsData:
    """Build slide 4 payload from `payload.ms_ratings`.

    The dict shape comes from `marketscreener_pages.fetch_ratings_page`.
    `has_data` is True iff at least one bullet OR one composite score is
    populated; the deck builder uses this to decide whether to render the
    slide at all (suppress > render-empty).
    """
    if not ms_ratings or not isinstance(ms_ratings, dict):
        return RatingsData(has_data=False)

    raw_strengths = ms_ratings.get("strengths") or []
    raw_weaknesses = ms_ratings.get("weaknesses") or []
    composite_dict = ms_ratings.get("composite_ratings") or {}
    esg = ms_ratings.get("esg_msci_rating") or None

    strengths = [s.strip() for s in raw_strengths if isinstance(s, str) and s.strip()][:_MAX_BULLETS]
    weaknesses = [w.strip() for w in raw_weaknesses if isinstance(w, str) and w.strip()][:_MAX_BULLETS]

    composites: list[CompositeRating] = []
    for label in _COMPOSITE_ORDER:
        raw_score = composite_dict.get(label)
        score: Optional[int] = None
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            try:
                score = int(round(float(raw_score)))
            except (TypeError, ValueError):
                score = None
        composites.append(CompositeRating(label=label, score=score))

    has_data = bool(strengths or weaknesses or any(c.score is not None for c in composites))
    # Normalize the ESG dash to None for cleaner downstream rendering.
    esg_clean = (esg or "").strip()
    if esg_clean in {"", "-", "N/A"}:
        esg_clean = None

    # Pass through the peer ESG list verbatim — slide 5's renderer uses
    # it to draw the bottom mini-table. Cap at 10 to fit the visible
    # band; subject row (always first) is preserved.
    raw_peer_esg = ms_ratings.get("peer_esg") or []
    peer_esg = [
        {
            "name": (r.get("name") or "").strip(),
            "market_cap": r.get("market_cap"),
            "esg_msci": r.get("esg_msci"),
            "rating_pct": r.get("rating_pct"),
        }
        for r in raw_peer_esg if isinstance(r, dict) and (r.get("name") or "").strip()
    ][:10]

    return RatingsData(
        strengths=strengths,
        weaknesses=weaknesses,
        composites=composites,
        esg_msci=esg_clean,
        peer_esg=peer_esg,
        has_data=has_data,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Slide 5: Sector Comparison
# ─────────────────────────────────────────────────────────────────────────────

_PEER_TABLE_LIMIT = 22  # subject + ~21 peers; tighter row height (0.32")
                        # in render_peers fits ~22 rows plus a header
                        # in the available vertical (2.0–9.5"). Was 11
                        # which left 6+ inches of empty whitespace below
                        # the table — adding more peers fills that with
                        # actual sector context.


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_peer_name(name: str) -> str:
    """Lowercase + strip for cross-table matching (sector ↔ ratings ESG)."""
    return (name or "").strip().lower()


def build_sector(
    ms_sector_peers: dict | None,
    ms_ratings: dict | None,
    *,
    sector_label: str = "",
) -> SectorComparisonData:
    """Build slide 5 payload from `ms_sector_peers` joined with `ms_ratings.peer_esg`.

    The peer table on /sector/ has rich performance + market cap; the
    /ratings/ page peer table contributes the ESG MSCI letter for each
    peer. Names are normalized for the join. Missing ESG → None
    (renderer shows "—").
    """
    if not ms_sector_peers or not isinstance(ms_sector_peers, dict):
        return SectorComparisonData(has_data=False, sector_label=sector_label)

    raw_rows = ms_sector_peers.get("rows") or []
    summary_rows = ms_sector_peers.get("summary_rows") or {}

    # Build the peer-ESG lookup from the ratings page (when available).
    esg_by_name: dict[str, Optional[str]] = {}
    if ms_ratings and isinstance(ms_ratings, dict):
        for r in (ms_ratings.get("peer_esg") or []):
            if not isinstance(r, dict):
                continue
            name = _normalize_peer_name(r.get("name") or "")
            if not name:
                continue
            esg = (r.get("esg_msci") or "").strip()
            esg_by_name[name] = esg if esg and esg != "-" else None

    rows: list[PeerRow] = []
    seen_names: set[str] = set()
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        name = (r.get("name") or "").strip()
        if not name:
            continue
        norm = _normalize_peer_name(name)
        if norm in seen_names:
            continue
        seen_names.add(norm)
        rows.append(PeerRow(
            name=name,
            market_cap_usd=r.get("market_cap_usd") or None,
            change_ytd_pct=_to_float(r.get("change_ytd_pct")),
            change_1y_pct=_to_float(r.get("change_1y_pct")),
            change_3y_pct=_to_float(r.get("change_3y_pct")),
            esg_msci=esg_by_name.get(norm),
            is_subject=(r is raw_rows[0]),  # MS always lists the subject first
        ))

    # Trim to the visible window. Always keep the subject row; if it would
    # fall outside the cap (rare — MS sorts by mcap and the subject is
    # often a smallcap), surface it via insertion at index 0.
    if len(rows) > _PEER_TABLE_LIMIT:
        subject = next((r for r in rows if r.is_subject), None)
        rows = rows[:_PEER_TABLE_LIMIT]
        if subject and subject not in rows:
            rows = [subject] + rows[: _PEER_TABLE_LIMIT - 1]

    # MS publishes an "Average" summary row alongside the peer table; we
    # surface only its YTD figure on the slide footer for context.
    average_ytd: Optional[float] = None
    avg_row = (summary_rows or {}).get("average") if isinstance(summary_rows, dict) else None
    if isinstance(avg_row, dict):
        average_ytd = _to_float(avg_row.get("change_ytd_pct"))

    return SectorComparisonData(
        sector_label=sector_label.strip(),
        rows=rows,
        average_ytd_pct=average_ytd,
        has_data=bool(rows),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Slide 6: Price Action & Broker Activity
# ─────────────────────────────────────────────────────────────────────────────

# Order matches the PDF layout. The renderer respects this order; the
# fetcher emits a dict so we re-key explicitly here for stable output.
_PERFORMANCE_LABELS = (
    ("perf_1d_pct",  "1 day"),
    ("perf_1w_pct",  "1 week"),
    ("perf_mtd_pct", "MTD"),
    ("perf_1m_pct",  "1 month"),
    ("perf_3m_pct",  "3 months"),
    ("perf_6m_pct",  "6 months"),
    ("perf_ytd_pct", "YTD"),
)

_RANGE_LABELS = (
    ("range_1w",  "1 week"),
    ("range_1m",  "1 month"),
    ("range_ytd", "YTD"),
    ("range_1y",  "1 year"),
    ("range_3y",  "3 years"),
    ("range_5y",  "5 years"),
)

_MAX_BROKER_ACTIONS = 6  # most-recent slice fits the panel without scroll


def build_price_action(
    ms_price_performance: dict | None,
    ms_analyst_recommendations: dict | None,
) -> PriceActionData:
    """Build slide 6 payload from price-perf + analyst-recs sections.

    Either source alone is enough to render the slide. has_data is True
    when at least one panel has content (perf grid OR broker actions).
    """
    perf_cells: list[PerformanceCell] = []
    if isinstance(ms_price_performance, dict):
        perf_dict = ms_price_performance.get("performance") or {}
        for key, label in _PERFORMANCE_LABELS:
            perf_cells.append(PerformanceCell(label=label, value_pct=_to_float(perf_dict.get(key))))

    extremes: list[CourseRange] = []
    if isinstance(ms_price_performance, dict):
        ext_dict = ms_price_performance.get("course_extremes") or {}
        for key, label in _RANGE_LABELS:
            entry = ext_dict.get(key)
            if isinstance(entry, dict):
                extremes.append(CourseRange(
                    label=label,
                    low=_to_float(entry.get("low")),
                    high=_to_float(entry.get("high")),
                ))
            else:
                extremes.append(CourseRange(label=label))

    broker_actions: list[BrokerAction] = []
    covering_brokers: list[str] = []
    if isinstance(ms_analyst_recommendations, dict):
        for item in (ms_analyst_recommendations.get("items") or [])[:_MAX_BROKER_ACTIONS]:
            if not isinstance(item, dict):
                continue
            broker_actions.append(BrokerAction(
                date=(item.get("date") or "").strip(),
                headline=(item.get("headline") or "").strip(),
                source=(item.get("source") or "").strip(),
            ))
        for name in (ms_analyst_recommendations.get("covering_brokers") or []):
            if isinstance(name, str) and name.strip():
                covering_brokers.append(name.strip())

    # Recent quotes — surfaced as a fallback panel on slide 6 when
    # broker actions are absent (NBOB.OM-shaped tickers).
    recent_quotes: list[dict] = []
    if isinstance(ms_price_performance, dict):
        for q in (ms_price_performance.get("recent_quotes") or [])[:8]:
            if isinstance(q, dict) and (q.get("date") or q.get("price")):
                recent_quotes.append({
                    "date": q.get("date"),
                    "price": q.get("price"),
                    "change_pct": q.get("change_pct"),
                    "volume": q.get("volume"),
                })

    has_perf = any(c.value_pct is not None for c in perf_cells)
    has_actions = bool(broker_actions)
    has_extremes = any(r.low is not None or r.high is not None for r in extremes)

    return PriceActionData(
        performance=perf_cells,
        course_extremes=extremes,
        broker_actions=broker_actions,
        covering_brokers=covering_brokers,
        recent_quotes=recent_quotes,
        has_data=has_perf or has_actions or has_extremes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Slide 4 (NEW): Income Statement Evolution & Surprise
# ─────────────────────────────────────────────────────────────────────────────

# Trim the quarterly grid to the last N quarters that actually fit on the
# slide. MS publishes 20+ quarters; the chart becomes unreadable beyond
# ~16 quarter labels in our portrait layout.
_MAX_QUARTERS_INCOME = 18


def _last_actual_index(periods: list[str], dates: list) -> int:
    """Return the index of the LAST quarter with a non-empty announcement_date.

    Mirrors `_resolve_annual_indices`'s actual-detection rule. Returns -1
    when every period is an estimate (no actuals).
    """
    last = -1
    for i, _ in enumerate(periods):
        d = dates[i] if i < len(dates) else None
        has_date = d and str(d).strip() not in ("", "-", "None")
        if has_date:
            last = i
    return last


def _safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or not den:
        return None
    try:
        return float(num) / float(den) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def build_income_evolution(
    ms_quarterly_forecasts: dict | None,
    ms_calendar_events: dict | None,
    units_label: str = "",
) -> IncomeEvolutionData:
    """Build the new "Income Statement Evolution & Surprise" slide payload.

    Two independent panels:

    1. Quarterly income series — from `ms_annual_forecasts.quarterly`
       (which `fetch_financial_forecast_series` emits). Trimmed to the
       most-recent _MAX_QUARTERS_INCOME quarters so the chart fits.
       Margins are derived (net = NI/Sales, operating = EBIT/Sales).

    2. Quarterly surprise series — from
       `ms_calendar_events.quarterly_results`. Surfaces only the Sales
       (net_sales) row's `released` / `forecast` / `spread_pct`.

    Either source alone is enough to render the slide.
    """
    quarterly_income: Optional[QuarterlyIncomeSeries] = None
    quarterly_surprise: Optional[QuarterlySurpriseSeries] = None

    # ── Quarterly income chart ──
    if isinstance(ms_quarterly_forecasts, dict):
        qb = ms_quarterly_forecasts.get("quarterly", {}) or {}
        periods = list(qb.get("periods") or [])
        if periods:
            dates = list(qb.get("announcement_dates") or [])
            sales = list(qb.get("net_sales") or [None] * len(periods))
            ebit = list(qb.get("ebit") or [None] * len(periods))
            net_income = list(qb.get("net_income") or [None] * len(periods))

            # Trim to most-recent N quarters
            n = min(_MAX_QUARTERS_INCOME, len(periods))
            sl = slice(-n, None)
            tp = periods[sl]
            td = dates[sl] if dates else [None] * n
            ts = sales[sl] if sales else [None] * n
            te = ebit[sl] if ebit else [None] * n
            tn = net_income[sl] if net_income else [None] * n

            op_margin = [_safe_div(e, s) for e, s in zip(te, ts)]
            net_margin = [_safe_div(ni, s) for ni, s in zip(tn, ts)]

            quarterly_income = QuarterlyIncomeSeries(
                periods=tp,
                revenue=ts,
                ebit=te,
                net_income=tn,
                operating_margin_pct=op_margin,
                net_margin_pct=net_margin,
                actuals_boundary=_last_actual_index(tp, td),
                units_label=units_label,
            )

    # ── Quarterly surprise chart ──
    # Pull surprise data for all three metrics MS publishes on /calendar/:
    # net_sales (primary chart), net_income (secondary summary chip), and
    # EBIT (sparse — only some industrials have it). Quarters are aligned
    # against the SALES row so the periods axis stays consistent; missing
    # values for net_income/EBIT in a given quarter just mean that
    # metric's chip is blank for that period.
    if isinstance(ms_calendar_events, dict):
        qr = ms_calendar_events.get("quarterly_results") or {}
        if isinstance(qr, dict):
            quarters = list(qr.get("quarters") or [])
            rows = qr.get("rows") or []

            def _row_by_key(key: str) -> dict | None:
                return next(
                    (r for r in rows
                     if isinstance(r, dict) and r.get("metric_key") == key),
                    None,
                )

            sales_row = _row_by_key("net_sales")
            ni_row = _row_by_key("net_income")
            ebit_row = _row_by_key("ebit")

            if quarters and sales_row:
                def _extract(row: dict | None, idx: int) -> tuple[Optional[float], Optional[float], Optional[float]]:
                    """Return (actual, estimate, surprise_pct) for one quarter."""
                    if not row:
                        return (None, None, None)
                    cells = row.get("by_quarter") or []
                    cell = cells[idx] if idx < len(cells) else None
                    if not isinstance(cell, dict):
                        return (None, None, None)
                    return (cell.get("released"), cell.get("forecast"), cell.get("spread_pct"))

                kept_periods: list[str] = []
                s_act: list[Optional[float]] = []
                s_est: list[Optional[float]] = []
                s_sp: list[Optional[float]] = []
                ni_act: list[Optional[float]] = []
                ni_est: list[Optional[float]] = []
                ni_sp: list[Optional[float]] = []
                eb_act: list[Optional[float]] = []
                eb_est: list[Optional[float]] = []
                eb_sp: list[Optional[float]] = []

                for i, q in enumerate(quarters):
                    sa, se, ssp = _extract(sales_row, i)
                    # Skip quarters where Sales (the anchor metric) has
                    # no data on either side; they would render as empty
                    # bars and confuse the chart.
                    if sa is None and se is None:
                        continue
                    kept_periods.append(str(q))
                    s_act.append(sa)
                    s_est.append(se)
                    s_sp.append(ssp)
                    na, ne, nsp = _extract(ni_row, i)
                    ni_act.append(na)
                    ni_est.append(ne)
                    ni_sp.append(nsp)
                    ea, ee, esp = _extract(ebit_row, i)
                    eb_act.append(ea)
                    eb_est.append(ee)
                    eb_sp.append(esp)

                if kept_periods:
                    quarterly_surprise = QuarterlySurpriseSeries(
                        periods=kept_periods,
                        actual=s_act,
                        estimate=s_est,
                        surprise_pct=s_sp,
                        net_income_actual=ni_act,
                        net_income_estimate=ni_est,
                        net_income_surprise_pct=ni_sp,
                        ebit_actual=eb_act,
                        ebit_estimate=eb_est,
                        ebit_surprise_pct=eb_sp,
                        units_label=units_label,
                    )

    has_income = quarterly_income is not None and any(
        v is not None for v in quarterly_income.revenue
    )
    has_surprise = quarterly_surprise is not None and any(
        v is not None for v in quarterly_surprise.actual
    )

    return IncomeEvolutionData(
        quarterly_income=quarterly_income if has_income else None,
        quarterly_surprise=quarterly_surprise if has_surprise else None,
        has_data=has_income or has_surprise,
    )
