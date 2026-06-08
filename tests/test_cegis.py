from pathlib import Path

import pytest

from tyani_tolkai.arena.cegis import CegisReferee, in_L, min_consistent_substring
from tyani_tolkai.sandbox import LocalBackend


def test_in_L_is_contains_ab():
    assert in_L("ab") and in_L("aab") and in_L("aabb")
    assert not in_L("a") and not in_L("ba") and not in_L("")


def test_min_consistent_substring_converges_to_ab():
    ex = [("ab", True), ("ba", False), ("a", False), ("b", False), ("aab", True)]
    assert min_consistent_substring(ex, alphabet="ab", max_len=3) == "ab"


def test_min_consistent_substring_none_when_inconsistent():
    # 'ab' labeled both True and False → no substring rule can be consistent
    assert min_consistent_substring([("ab", True), ("ab", False)], alphabet="ab", max_len=3) is None


def _write(p: Path, name: str, body: str):
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(body, encoding="utf-8")


def test_cegis_play_perfect_recognizer_scores_one(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a, "recognizer.py", "def accepts(s):\n    return 'ab' in s\n")
    _write(b, "strings.txt", "ab\nba\naab\nb\n")
    out = CegisReferee().play(a, b, LocalBackend(), seed=0)
    assert out.a_score == 1.0 and out.b_score == 0.0
    assert out.detail["counterexamples"] == []


def test_cegis_play_accept_all_gives_counterexamples(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a, "recognizer.py", "def accepts(s):\n    return True\n")
    _write(b, "strings.txt", "ab\nba\nb\n")          # 'ba','b' ∉ L but accept-all says True
    out = CegisReferee().play(a, b, LocalBackend(), seed=0)
    assert out.a_score == pytest.approx(1 / 3)
    labels = {c["s"]: c["label"] for c in out.detail["counterexamples"]}
    assert labels == {"ba": False, "b": False}


def test_cegis_play_deterministic(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a, "recognizer.py", "def accepts(s):\n    return s.endswith('b')\n")
    _write(b, "strings.txt", "ab\nba\nb\naab\n")
    ref = CegisReferee()
    o1 = ref.play(a, b, LocalBackend(), seed=0)
    o2 = ref.play(a, b, LocalBackend(), seed=0)
    assert o1 == o2


def test_cegis_play_no_probes_is_trivial_win(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a, "recognizer.py", "def accepts(s):\n    return 'ab' in s\n")
    _write(b, "strings.txt", "")
    out = CegisReferee().play(a, b, LocalBackend(), seed=0)
    assert out.a_score == 1.0 and out.detail["n"] == 0


def test_cegis_play_broken_recognizer_loses_but_teaches(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a, "recognizer.py", "def accepts(s):\n    raise RuntimeError('boom')\n")
    _write(b, "strings.txt", "ab\nba\n")
    out = CegisReferee().play(a, b, LocalBackend(), seed=0)
    assert out.a_score == 0.0
    labels = {c["s"]: c["label"] for c in out.detail["counterexamples"]}
    assert labels == {"ab": True, "ba": False}
