"""
Tests for the slide-context builders in `src/services/build_extras_context.py`.

These exercise the contract between MS payload dicts and the typed slide
dataclasses (RatingsData / SectorComparisonData / PriceActionData), with
particular attention to edge cases the live MS site is known to produce:

  * a None payload section (entity-mismatch suppression upstream)
  * empty arrays inside an otherwise-populated dict
  * "-" sentinels that must not flow through as data
  * thinly covered tickers with composite_ratings dict full of None
  * peer rows without a corresponding ESG entry on /ratings/

Builder code is the only place that filters / normalizes these — render
modules trust the dataclass — so coverage here protects every slide.
"""

from __future__ import annotations

import pytest

from src.services.build_extras_context import (
    build_income_evolution,
    build_price_action,
    build_ratings,
    build_sector,
)


# ─────────────────────────────────────────────────────────────────────────────
# build_ratings
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildRatings:
    def test_none_input_yields_no_data(self):
        out = build_ratings(None)
        assert out.has_data is False
        assert out.strengths == []
        assert out.weaknesses == []
        # Composites list still defined (renderer iterates without guard).
        assert out.composites == []

    def test_full_payload_round_trip(self):
        payload = {
            "strengths": ["Margins among the highest", "Sound balance sheet"],
            "weaknesses": ["Earnings growth lacks momentum"],
            "composite_ratings": {"Trader": 70, "Investor": 93, "Global": 77, "Quality": 91},
            "esg_msci_rating": "BBB",
        }
        out = build_ratings(payload)
        assert out.has_data is True
        assert out.strengths == ["Margins among the highest", "Sound balance sheet"]
        assert out.weaknesses == ["Earnings growth lacks momentum"]
        # Order is enforced (Trader, Investor, Global, Quality).
        assert [c.label for c in out.composites] == ["Trader", "Investor", "Global", "Quality"]
        assert [c.score for c in out.composites] == [70, 93, 77, 91]
        assert out.esg_msci == "BBB"

    def test_dash_esg_normalized_to_none(self):
        out = build_ratings({"esg_msci_rating": "-"})
        assert out.esg_msci is None

    def test_score_rounded_from_float(self):
        out = build_ratings({"composite_ratings": {"Trader": 70.6, "Investor": None,
                                                    "Global": 0, "Quality": 100}})
        scores = {c.label: c.score for c in out.composites}
        assert scores["Trader"] == 71      # rounded
        assert scores["Investor"] is None  # None passes through
        assert scores["Global"] == 0
        assert scores["Quality"] == 100

    def test_bullets_capped_at_five(self):
        out = build_ratings({"strengths": [f"strength {i}" for i in range(8)]})
        assert len(out.strengths) == 5
        assert out.strengths[0] == "strength 0"
        assert out.strengths[-1] == "strength 4"

    def test_blank_strings_dropped(self):
        out = build_ratings({"strengths": ["  real  ", "   ", "", None, "another"]})
        assert out.strengths == ["real", "another"]

    def test_has_data_false_when_only_dash_esg(self):
        """A ratings page that only has an ESG dash and no other data
        should not push the slide forward — it would show a dash bar
        across the board, which is worse than suppressing."""
        out = build_ratings({
            "strengths": [],
            "weaknesses": [],
            "composite_ratings": {"Trader": None, "Investor": None,
                                   "Global": None, "Quality": None},
            "esg_msci_rating": "-",
        })
        assert out.has_data is False


# ─────────────────────────────────────────────────────────────────────────────
# build_sector
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildSector:
    def test_none_input_yields_no_data(self):
        out = build_sector(None, None, sector_label="Retail")
        assert out.has_data is False
        assert out.rows == []
        assert out.sector_label == "Retail"

    def test_subject_marked_first(self):
        sector = {
            "rows": [
                {"name": "FOO CORP", "market_cap_usd": "1B",
                 "change_ytd_pct": -10.0, "change_1y_pct": -5.0, "change_3y_pct": None},
                {"name": "BAR INC.", "market_cap_usd": "5B",
                 "change_ytd_pct": 5.0, "change_1y_pct": 8.0, "change_3y_pct": 12.0},
            ],
            "summary_rows": {"average": {"change_ytd_pct": -2.5}},
        }
        out = build_sector(sector, None)
        assert out.has_data is True
        assert len(out.rows) == 2
        assert out.rows[0].is_subject is True
        assert out.rows[1].is_subject is False
        assert out.average_ytd_pct == pytest.approx(-2.5)

    def test_esg_join_from_ratings_payload(self):
        sector = {"rows": [
            {"name": "FOO CORP", "market_cap_usd": "1B"},
            {"name": "BAR INC.", "market_cap_usd": "2B"},
        ]}
        ratings = {"peer_esg": [
            {"name": "FOO CORP", "esg_msci": "AA"},
            {"name": "BAR INC.", "esg_msci": "-"},  # dash → None
        ]}
        out = build_sector(sector, ratings)
        assert out.rows[0].esg_msci == "AA"
        assert out.rows[1].esg_msci is None

    def test_peer_table_capped_with_subject_preserved(self):
        # Cap was raised from 11 to 22 (2026-05) so the slide-6 peer
        # table fills the available vertical real estate without leaving
        # a 6-inch white band below the table.
        rows = [{"name": f"COMPANY {i}", "market_cap_usd": "1B"} for i in range(30)]
        out = build_sector({"rows": rows}, None)
        assert len(out.rows) == 22  # _PEER_TABLE_LIMIT
        # Subject (row 0) is always present.
        assert out.rows[0].is_subject is True
        assert out.rows[0].name == "COMPANY 0"

    def test_duplicate_names_deduped(self):
        sector = {"rows": [
            {"name": "FOO", "market_cap_usd": "1B"},
            {"name": "FOO", "market_cap_usd": "2B"},  # duplicate (case-insensitive)
            {"name": "BAR", "market_cap_usd": "3B"},
        ]}
        out = build_sector(sector, None)
        names = [r.name for r in out.rows]
        assert names == ["FOO", "BAR"]


# ─────────────────────────────────────────────────────────────────────────────
# build_price_action
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildPriceAction:
    def test_none_inputs_yield_no_data(self):
        out = build_price_action(None, None)
        assert out.has_data is False

    def test_perf_grid_only(self):
        perf = {"performance": {
            "perf_1d_pct": -0.85, "perf_1w_pct": -3.33,
            "perf_mtd_pct": -1.69, "perf_1m_pct": -6.45,
            "perf_3m_pct": -26.58, "perf_6m_pct": -27.50,
            "perf_ytd_pct": -23.18,
        }}
        out = build_price_action(perf, None)
        assert out.has_data is True
        assert len(out.performance) == 7
        # Order matches the canonical layout (1D first).
        assert out.performance[0].label == "1 day"
        assert out.performance[0].value_pct == pytest.approx(-0.85)
        assert out.performance[-1].label == "YTD"
        assert out.performance[-1].value_pct == pytest.approx(-23.18)

    def test_course_extremes_with_partial_data(self):
        perf = {"course_extremes": {
            "range_ytd": {"low": 1.12, "high": 1.62},
            "range_1y":  {"low": 1.12, "high": 1.81},
            # range_1w / 1m / 3y / 5y missing → renderer shows them as empty
        }}
        out = build_price_action(perf, None)
        assert out.has_data is True
        ranges = {r.label: r for r in out.course_extremes}
        assert ranges["YTD"].low == pytest.approx(1.12)
        assert ranges["YTD"].high == pytest.approx(1.62)
        assert ranges["1 week"].low is None
        assert ranges["1 week"].high is None

    def test_broker_actions_capped(self):
        items = [{"date": f"day{i}", "headline": f"headline {i}", "source": "MT"}
                 for i in range(10)]
        recs = {"items": items, "covering_brokers": ["JPMorgan", "HSBC", "Citi"]}
        out = build_price_action(None, recs)
        assert out.has_data is True
        assert len(out.broker_actions) == 6  # _MAX_BROKER_ACTIONS
        assert out.broker_actions[0].headline == "headline 0"
        assert out.covering_brokers == ["JPMorgan", "HSBC", "Citi"]

    def test_only_brokers_no_perf(self):
        recs = {"items": [{"date": "Apr 1", "headline": "JP Morgan upgrades", "source": "MT"}]}
        out = build_price_action(None, recs)
        assert out.has_data is True  # broker actions alone is enough
        assert out.broker_actions[0].source == "MT"

    def test_invalid_items_skipped(self):
        recs = {"items": [
            {"date": "Apr 1", "headline": "Real action", "source": "MT"},
            "string-not-dict",
            {"date": "Apr 2", "headline": "Another"},
            None,
        ]}
        out = build_price_action(None, recs)
        assert len(out.broker_actions) == 2
        assert out.broker_actions[0].headline == "Real action"
        assert out.broker_actions[1].headline == "Another"


# ─────────────────────────────────────────────────────────────────────────────
# build_income_evolution
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildIncomeEvolution:
    """The new slide-4 panel: quarterly income series + revenue surprise.

    Two independent inputs (ms_quarterly_forecasts, ms_calendar_events).
    Either alone is enough to set has_data=True; both None must yield
    has_data=False without raising.
    """

    def test_none_inputs_yield_no_data(self):
        out = build_income_evolution(None, None)
        assert out.has_data is False
        assert out.quarterly_income is None
        assert out.quarterly_surprise is None

    def test_quarterly_income_only(self):
        ms_q = {"quarterly": {
            "periods": ["2025Q3", "2025Q4", "2026Q1"],
            "announcement_dates": ["10/14/25", "1/13/26", "4/14/26"],
            "net_sales":  [41.66, 42.4, 46.24],
            "ebit":       [24.0, 24.5, 27.0],
            "net_income": [17.77, 18.4, 19.47],
        }}
        out = build_income_evolution(ms_q, None)
        assert out.has_data is True
        qi = out.quarterly_income
        assert qi is not None
        assert qi.periods == ["2025Q3", "2025Q4", "2026Q1"]
        # Margins computed: Op = EBIT/Sales*100, Net = NI/Sales*100
        assert qi.operating_margin_pct[0] == pytest.approx(57.6, abs=0.5)
        assert qi.net_margin_pct[0] == pytest.approx(42.7, abs=0.5)
        # Last period has a date so actuals_boundary = 2.
        assert qi.actuals_boundary == 2
        assert out.quarterly_surprise is None  # no calendar input

    def test_surprise_only(self):
        cal = {"quarterly_results": {
            "quarters": ["2025 Q3", "2025 Q4", "2026 Q1"],
            "rows": [{"metric_key": "net_sales", "by_quarter": [
                {"released": 41.66, "forecast": 39.0, "spread_pct": 6.8},
                {"released": 42.4,  "forecast": 42.4, "spread_pct": 0.0},
                {"released": 46.24, "forecast": 43.84, "spread_pct": 5.5},
            ]}],
        }}
        out = build_income_evolution(None, cal)
        assert out.has_data is True
        qs = out.quarterly_surprise
        assert qs is not None
        assert qs.periods == ["2025 Q3", "2025 Q4", "2026 Q1"]
        assert qs.actual == [41.66, 42.4, 46.24]
        assert qs.estimate == [39.0, 42.4, 43.84]
        assert qs.surprise_pct == [6.8, 0.0, 5.5]
        assert out.quarterly_income is None

    def test_quarters_with_neither_value_skipped(self):
        """MS sometimes lists a future quarter in the table but with both
        released/forecast empty (placeholder). Those rows must drop out
        rather than render an empty pair of bars."""
        cal = {"quarterly_results": {
            "quarters": ["2025 Q4", "2026 Q1", "2026 Q2"],
            "rows": [{"metric_key": "net_sales", "by_quarter": [
                {"released": 42.4,  "forecast": 42.4, "spread_pct": 0.0},
                {"released": 46.24, "forecast": 43.84, "spread_pct": 5.5},
                {"released": None, "forecast": None, "spread_pct": None},
            ]}],
        }}
        out = build_income_evolution(None, cal)
        qs = out.quarterly_surprise
        assert qs is not None
        # The empty Q2 row was dropped.
        assert qs.periods == ["2025 Q4", "2026 Q1"]
        assert len(qs.actual) == 2

    def test_quarterly_grid_capped_to_18(self):
        """Long quarterly grids (20+ quarters) trim to the most recent
        18 so the chart renders legibly."""
        n = 25
        periods = [f"2020Q{((i % 4) + 1)}" for i in range(n)]
        sales = [100.0 + i for i in range(n)]
        ms_q = {"quarterly": {
            "periods": periods,
            "announcement_dates": ["x"] * n,  # all actuals
            "net_sales": sales,
            "ebit": [50.0] * n,
            "net_income": [40.0] * n,
        }}
        out = build_income_evolution(ms_q, None)
        qi = out.quarterly_income
        assert qi is not None
        assert len(qi.periods) == 18  # _MAX_QUARTERS_INCOME
        # Most recent 18 — the last entry should be the last in the input.
        assert qi.revenue[-1] == sales[-1]

    def test_surprise_includes_net_income_and_ebit(self):
        """MS publishes surprise data for three metrics on /calendar/:
        net_sales (anchor for the chart), net_income (rendered as a
        secondary chip), and EBIT (sparse — surfaced when present).

        Reference: NBOB.OM 2025 Q4 had a Sales beat (+0.02%) alongside
        a Net income miss (-1.93%). Collapsing both into one Sales-only
        chip would hide the divergence — which is exactly the kind of
        signal a reader needs from an earnings preview."""
        cal = {"quarterly_results": {
            "quarters": ["2025 Q3", "2025 Q4", "2026 Q1"],
            "rows": [
                {"metric_key": "net_sales", "by_quarter": [
                    {"released": 41.66, "forecast": 39.0, "spread_pct": 6.8},
                    {"released": 42.4,  "forecast": 42.4, "spread_pct": 0.02},
                    {"released": 46.24, "forecast": 43.84, "spread_pct": 5.5},
                ]},
                {"metric_key": "net_income", "by_quarter": [
                    {"released": 17.77, "forecast": 16.2, "spread_pct": 9.84},
                    {"released": 18.4,  "forecast": 18.8, "spread_pct": -1.93},
                    {"released": 19.47, "forecast": 18.9, "spread_pct": 3.01},
                ]},
                {"metric_key": "ebit", "by_quarter": [
                    {"released": None, "forecast": None, "spread_pct": None},
                    {"released": 25.2, "forecast": 25.1, "spread_pct": 0.4},
                    {"released": None, "forecast": None, "spread_pct": None},
                ]},
            ],
        }}
        out = build_income_evolution(None, cal)
        qs = out.quarterly_surprise
        assert qs is not None
        # Sales row anchors the periods list.
        assert qs.periods == ["2025 Q3", "2025 Q4", "2026 Q1"]
        # Net income data flows through with the right sign.
        assert qs.net_income_actual == [17.77, 18.4, 19.47]
        assert qs.net_income_surprise_pct == [9.84, -1.93, 3.01]
        # EBIT is sparse but the populated cell came through.
        assert qs.ebit_surprise_pct[1] == pytest.approx(0.4)
        assert qs.ebit_surprise_pct[0] is None
        assert qs.ebit_surprise_pct[2] is None

    def test_surprise_alignment_when_net_income_row_missing(self):
        """Tickers MS thinly covers may publish only the Sales row.
        The Net income / EBIT lists must still align to periods (all
        Nones) so the renderer can index into them safely."""
        cal = {"quarterly_results": {
            "quarters": ["2025 Q3", "2025 Q4"],
            "rows": [
                {"metric_key": "net_sales", "by_quarter": [
                    {"released": 100.0, "forecast": 95.0, "spread_pct": 5.3},
                    {"released": 110.0, "forecast": 105.0, "spread_pct": 4.8},
                ]},
            ],
        }}
        out = build_income_evolution(None, cal)
        qs = out.quarterly_surprise
        assert qs is not None
        assert len(qs.net_income_actual) == len(qs.periods)
        assert all(v is None for v in qs.net_income_actual)
        assert all(v is None for v in qs.net_income_surprise_pct)

    def test_safe_div_handles_zero_revenue(self):
        """A ticker reporting zero revenue (rare for upstream/oil-bust
        scenarios) must not divide-by-zero on margin calc — the
        margin lists carry None, the chart still renders."""
        ms_q = {"quarterly": {
            "periods": ["2025Q1"],
            "announcement_dates": ["4/14/25"],
            "net_sales":  [0.0],
            "ebit":       [-5.0],
            "net_income": [-10.0],
        }}
        out = build_income_evolution(ms_q, None)
        # Margins computed safely (None, no crash).
        assert out.quarterly_income is not None
        assert out.quarterly_income.operating_margin_pct == [None]
        assert out.quarterly_income.net_margin_pct == [None]
