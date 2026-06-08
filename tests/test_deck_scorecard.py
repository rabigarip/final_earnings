"""Tests for the deck-readiness scorecard."""
from src.services.deck_scorecard import score_ticker, score_universe, _WEIGHTS


def test_weights_sum_to_100():
    assert sum(_WEIGHTS.values()) == 100


def test_bkmb_is_ready_tier():
    # BKMB.OM is the hand-curated showcase: disclosed fy_highlights + slug +
    # MS cache + peers + bank template → top tier.
    r = score_ticker("BKMB.OM")
    assert r["score"] >= 80
    assert r["tier"].startswith("A")
    assert r["components"]["grounded_fy"] == _WEIGHTS["grounded_fy"]
    assert r["missing"] == []


def test_score_shape_and_bounds():
    r = score_ticker("BKMB.OM")
    assert 0 <= r["score"] <= 100
    assert set(r["components"]) == set(_WEIGHTS)
    assert isinstance(r["missing"], list)


def test_unknown_ticker_is_thin_with_actionable_gaps():
    r = score_ticker("ZZZZ.XX")
    assert r["tier"].startswith("D")
    assert r["missing"]                       # tells you what to add
    assert r["components"]["grounded_fy"] == 0


def test_universe_summary_counts_match_rows():
    res = score_universe(["BKMB.OM", "ZZZZ.XX"])
    assert res["n"] == 2
    assert sum(res["summary"].values()) == 2
    assert res["rows"][0]["score"] >= res["rows"][1]["score"]   # sorted desc
