from src.services.fetch_peers import fetch_peer_multiples


class _DummyTicker:
    def __init__(self, info):
        self.info = info


def test_fetch_peer_multiples_median(monkeypatch):
    sample = {
        "AAA": {"forwardPE": 10.0, "enterpriseValue": 200.0, "ebitda": 20.0},
        "BBB": {"forwardPE": 20.0, "enterpriseValue": 330.0, "ebitda": 30.0},
        "CCC": {"forwardPE": 30.0, "enterpriseValue": 520.0, "ebitda": 40.0},
    }

    def _ticker(sym):
        return _DummyTicker(sample.get(sym, {}))

    import src.services.fetch_peers as fp
    monkeypatch.setattr(fp.yf, "Ticker", _ticker)

    out = fetch_peer_multiples(["AAA", "BBB", "CCC"])
    assert out["pe_sector_median"] == 20.0
    # EV/EBITDA: [10,11,13] => median 11
    assert out["ev_ebitda_sector_median"] == 11.0
    assert out["peer_count"] == 3

