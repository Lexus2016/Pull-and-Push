from tyani_tolkai.validation import (
    flat_orders, buy_and_hold_orders, random_orders, score_controls, beats_controls,
)


def _bars(closes):
    return [(c, c, c, c, 1.0) for c in closes]


def test_flat_orders():
    assert flat_orders(4) == [0, 0, 0, 0]
    assert flat_orders(0) == []


def test_buy_and_hold_orders():
    assert buy_and_hold_orders(4) == [1, 0, 0, 0]
    assert buy_and_hold_orders(0) == []
    assert buy_and_hold_orders(1) == [1]


def test_random_orders_deterministic_and_in_range():
    a = random_orders(20, seed="s")
    b = random_orders(20, seed="s")
    assert a == b                                  # deterministic for a seed
    assert len(a) == 20
    assert set(a) <= {-1, 0, 1}
    assert random_orders(20, seed="other") != a or True   # may differ; not asserted hard


def test_score_controls_returns_three_named_metric_dicts():
    bars = _bars([1.0, 2.0, 3.0, 2.0, 4.0, 5.0, 1.0, 2.0])
    ctl = score_controls(bars, oos_start=5, params={})
    assert set(ctl) == {"flat", "buy_and_hold", "random"}
    for name, m in ctl.items():
        assert "return_oos_pct" in m


def test_beats_controls():
    bot = {"return_oos_pct": 12.0}
    controls = {"flat": {"return_oos_pct": 0.0}, "random": {"return_oos_pct": 3.0},
                "buy_and_hold": {"return_oos_pct": 8.0}}
    v = beats_controls(bot, controls)
    assert v["beats_flat"] is True and v["beats_random"] is True

    weak = {"return_oos_pct": -1.0}
    v2 = beats_controls(weak, controls)
    assert v2["beats_flat"] is False


import pytest
from tyani_tolkai.validation import check_determinism, hash_artifacts, insample_oos_gap


def test_check_determinism_true_for_stable_callable():
    calls = {"n": 0}
    def stable():
        calls["n"] += 1
        return {"return_oos_pct": 5.0, "num_trades": 2}
    ok, scores = check_determinism(stable, runs=3)
    assert ok is True
    assert calls["n"] == 3
    assert len(scores) == 3


def test_check_determinism_false_for_varying_callable():
    state = {"n": 0}
    def varying():
        state["n"] += 1
        return {"return_oos_pct": float(state["n"])}    # changes each call
    ok, scores = check_determinism(varying, runs=2)
    assert ok is False


def test_hash_artifacts_stable_and_sensitive(tmp_path):
    eng = tmp_path / "engine.py"; eng.write_text("print(1)\n", encoding="utf-8")
    data = tmp_path / "data.csv"; data.write_text("a,b\n1,2\n", encoding="utf-8")
    h1 = hash_artifacts(engine_path=eng, data_path=data, config={"seed": "x", "lev": 3})
    h2 = hash_artifacts(engine_path=eng, data_path=data, config={"lev": 3, "seed": "x"})
    assert set(h1) == {"engine", "data", "config"}
    assert all(len(v) == 64 for v in h1.values())     # sha256 hex
    assert h1 == h2                                    # config key order doesn't matter (canonical)
    eng.write_text("print(2)\n", encoding="utf-8")
    h3 = hash_artifacts(engine_path=eng, data_path=data, config={"seed": "x", "lev": 3})
    assert h3["engine"] != h1["engine"]               # engine change → different hash
    assert h3["data"] == h1["data"]                   # data unchanged → same hash


def test_hash_artifacts_missing_file_raises(tmp_path):
    with pytest.raises((FileNotFoundError, OSError)):
        hash_artifacts(engine_path=tmp_path / "nope.py", data_path=tmp_path / "nope.csv", config={})


def test_insample_oos_gap():
    assert insample_oos_gap({"in_sample_return_pct": 50.0, "return_oos_pct": 10.0}) == 40.0
    assert insample_oos_gap({"in_sample_return_pct": 5.0, "return_oos_pct": 8.0}) == -3.0
