from src.storage.db import init_db
from src.services.store_actuals import upsert_actuals, get_actual


def test_upsert_and_get_actual_roundtrip():
    init_db()
    upsert_actuals(
        ticker="TEST.ACT",
        period="2026-Q1",
        revenue=100.0,
        net_income=10.0,
        eps=0.5,
        ebitda=20.0,
        ebitda_margin=20.0,
        reported_date="2026-04-30",
    )
    row = get_actual("TEST.ACT", "2026-Q1")
    assert row is not None
    assert row["revenue"] == 100.0
    assert row["eps"] == 0.5

