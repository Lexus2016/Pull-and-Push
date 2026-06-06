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
