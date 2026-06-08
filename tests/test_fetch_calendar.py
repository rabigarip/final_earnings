"""
Tests for the calendar fetcher extrapolation logic.

The /calendar/ page on MarketScreener publishes confirmed earnings
dates for richly-covered tickers but is silent for most of our seeded
universe (NBOB.OM, Spinneys, Bank Muscat etc. — MS returns
`next_expected_earnings_date=None`). Earlier this meant those tickers
never landed on the calendar at all.

`_estimate_next_from_finances_history` fills the gap by parsing the
historical announcement_dates that MS publishes on /finances/ and
projecting the next release forward by the median quarterly cadence.

These tests pin the projection arithmetic against shaped fixture data
without hitting the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from src.services.fetch_calendar import _estimate_next_from_finances_history


def _payload(dates: list[str]) -> dict:
    """Build a minimal MS /finances/ payload shape consumed by the
    estimator. Only the quarterly.announcement_dates list matters."""
    return {
        "quarterly": {
            "periods": [f"2024Q{i}" for i in range(1, len(dates) + 1)],
            "announcement_dates": dates,
        }
    }


def _patch_fetch(payload):
    """Patch out the live MS fetch so the estimator only sees fixture data."""
    return patch(
        "src.providers.marketscreener_pages.fetch_financial_forecast_series",
        return_value=(payload, None),
    )


class TestEstimateFromHistory:
    def test_projects_one_cadence_forward_from_last_release(self):
        """Standard ~91-day quarterly cadence: 4 prior releases, with
        the most recent ~30 days ago, so the projection lands ~60 days
        forward and falls safely inside the 180-day horizon."""
        today = datetime.now()
        dates = [
            (today - timedelta(days=303)).strftime("%m/%d/%Y"),
            (today - timedelta(days=212)).strftime("%m/%d/%Y"),
            (today - timedelta(days=121)).strftime("%m/%d/%Y"),
            (today - timedelta(days=30)).strftime("%m/%d/%Y"),
        ]
        with _patch_fetch(_payload(dates)):
            out = _estimate_next_from_finances_history("TEST", "url")
        assert out is not None
        assert out["source"] == "marketscreener_estimated"
        assert out["confirmed"] is False
        projected = datetime.strptime(out["event_date"], "%Y-%m-%d")
        days_ahead = (projected.date() - today.date()).days
        # 30 days since last release + 91 cadence = ~61 days ahead.
        assert 55 <= days_ahead <= 67

    def test_returns_none_when_only_one_date(self):
        """Need >= 2 historical dates to compute a cadence."""
        today = datetime.now()
        with _patch_fetch(_payload([
            (today - timedelta(days=30)).strftime("%m/%d/%Y"),
        ])):
            out = _estimate_next_from_finances_history("TEST", "url")
        assert out is None

    def test_filters_dash_and_empty_strings(self):
        """MS leaves forward quarters as '-' or empty — those must
        not contribute to the cadence calculation."""
        today = datetime.now()
        dates = [
            (today - timedelta(days=270)).strftime("%m/%d/%Y"),
            (today - timedelta(days=180)).strftime("%m/%d/%Y"),
            (today - timedelta(days=90)).strftime("%m/%d/%Y"),
            "-", "", "None",
        ]
        with _patch_fetch(_payload(dates)):
            out = _estimate_next_from_finances_history("TEST", "url")
        assert out is not None

    def test_caps_horizon_at_180_days(self):
        """If projection would land more than 180 days out (e.g. an
        annual-only filer), suppress the entry — too unreliable."""
        today = datetime.now()
        # 365-day cadence → projection ~365 days out → over the cap.
        dates = [
            (today - timedelta(days=730)).strftime("%m/%d/%Y"),
            (today - timedelta(days=365)).strftime("%m/%d/%Y"),
        ]
        with _patch_fetch(_payload(dates)):
            out = _estimate_next_from_finances_history("TEST", "url")
        assert out is None

    def test_returns_none_when_projection_in_the_past(self):
        """If the last release was so long ago that one cadence forward
        still lands in the past, suppress — stale data situation."""
        old = datetime.now() - timedelta(days=5 * 365)
        dates = [
            (old - timedelta(days=91)).strftime("%m/%d/%Y"),
            old.strftime("%m/%d/%Y"),
        ]
        with _patch_fetch(_payload(dates)):
            out = _estimate_next_from_finances_history("TEST", "url")
        assert out is None

    def test_parses_short_year_date_format(self):
        """MS commonly emits dates like '4/14/26' (2-digit year). The
        parser must handle both that and the full 4-digit form."""
        today = datetime.now()
        dates = [
            (today - timedelta(days=182)).strftime("%m/%d/%y"),
            (today - timedelta(days=91)).strftime("%m/%d/%y"),
        ]
        with _patch_fetch(_payload(dates)):
            out = _estimate_next_from_finances_history("TEST", "url")
        assert out is not None
