import pytest

from tyani_tolkai.config import MetricCfg
from tyani_tolkai.scorer import decide, normalize, score


def test_normalize_higher():
    # worst=0, target=2.5, value=1.8 -> 72
    assert normalize(1.8, 0.0, 2.5, "higher") == pytest.approx(72.0)


def test_normalize_lower():
    # worst=0.5, target=0.05, value=0.12 -> 84.44
    assert normalize(0.12, 0.5, 0.05, "lower") == pytest.approx(84.444, abs=0.01)


def test_normalize_clamps():
    assert normalize(-1.0, 0.0, 2.5) == 0.0      # below worst
    assert normalize(9.0, 0.0, 2.5) == 100.0     # above target
    assert normalize(0.6, 0.5, 0.05) == 0.0      # lower: above worst → 0


def test_normalize_equal_range_raises():
    with pytest.raises(ValueError):
        normalize(1.0, 2.0, 2.0)


def test_score_weighted_sum():
    metrics = [
        MetricCfg(name="sharpe", dir="higher", weight=0.6, worst=0.0, target=2.5),
        MetricCfg(name="max_dd", dir="lower", weight=0.4, worst=0.5, target=0.05),
    ]
    # sharpe 1.8 -> 72 ; max_dd 0.12 -> 84.444
    # 0.6*72 + 0.4*84.444 = 43.2 + 33.7776 = 76.9776
    s = score({"sharpe": 1.8, "max_dd": 0.12}, metrics)
    assert s == pytest.approx(76.978, abs=0.01)


def test_score_missing_metric_raises():
    metrics = [MetricCfg(name="x", dir="higher", weight=1.0, worst=0.0, target=1.0)]
    with pytest.raises(KeyError):
        score({"y": 1.0}, metrics)


def test_decide_noise_band():
    assert decide(73.0, None, 1.0) == "keep"      # first iteration
    assert decide(74.5, 73.0, 1.0) == "keep"      # +1.5 >= 1.0
    assert decide(73.5, 73.0, 1.0) == "discard"   # +0.5 < 1.0 (noise)
    assert decide(73.0, 73.0, 1.0) == "discard"   # equal
    assert decide(70.0, 73.0, 1.0) == "discard"   # regression
