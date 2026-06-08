from pathlib import Path

import pytest

from tyani_tolkai.arena.referee import MatchOutcome, get_referee


def test_match_outcome_zero_sum_helper():
    o = MatchOutcome.zero_sum(a_score=0.7)
    assert o.a_score == 0.7
    assert o.b_score == pytest.approx(0.3)
    assert o.detail == {}


def test_get_referee_unknown_raises():
    with pytest.raises(KeyError):
        get_referee("nope")


def test_get_referee_returns_cegis():
    # importing cegis registers the referee
    import tyani_tolkai.arena.cegis  # noqa: F401

    ref = get_referee("cegis-recognizer")
    assert ref.name == "cegis-recognizer"
    assert ref.opponent_strategy == "accumulate"


def test_referee_zero_sum_invariant(tmp_path):
    import tyani_tolkai.arena.cegis  # noqa: F401
    from tyani_tolkai.sandbox import LocalBackend

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "recognizer.py").write_text("def accepts(s):\n    return 'a' in s\n", encoding="utf-8")
    (b / "strings.txt").write_text("ab\nba\nb\n", encoding="utf-8")
    out = get_referee("cegis-recognizer").play(a, b, LocalBackend(), seed=1)
    assert abs((out.a_score + out.b_score) - 1.0) < 1e-9
