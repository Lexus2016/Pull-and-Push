from tyani_tolkai.arena.cegis import CegisReferee
from tyani_tolkai.metrics.arena import ArenaMetricAdapter
from tyani_tolkai.sandbox import LocalBackend


class _EvalStub:
    metrics = ()


def _mkA(p, body):
    p.mkdir(parents=True, exist_ok=True)
    (p / "recognizer.py").write_text(body, encoding="utf-8")


def _mkB(p, lines):
    p.mkdir(parents=True, exist_ok=True)
    (p / "strings.txt").write_text(lines, encoding="utf-8")


def test_adapter_scores_A_against_two_B_opponents(tmp_path):
    a = tmp_path / "a"
    _mkA(a, "def accepts(s):\n    return 'ab' in s\n")
    b1 = tmp_path / "b1"
    _mkB(b1, "ab\nba\n")
    b2 = tmp_path / "b2"
    _mkB(b2, "b\naab\n")
    adapter = ArenaMetricAdapter(CegisReferee(), side="A", opponents=[b1, b2],
                                 aggregate="mean_gated", min_floor=0.5, penalty=1.0, seed=0)
    res = adapter.run(a, LocalBackend(), _EvalStub(), 60)
    assert res.ok
    fit = next(m for m in res.metrics if m["name"] == "arena_fitness")
    assert fit["value"] == 1.0
    assert res.data["mean"] == 1.0 and res.data["min"] == 1.0


def test_adapter_mean_gated_penalizes_low_min(tmp_path):
    a = tmp_path / "a"
    _mkA(a, "def accepts(s):\n    return True\n")   # accept-all
    b1 = tmp_path / "b1"
    _mkB(b1, "ab\n")            # 'ab'∈L → correct → 1.0
    b2 = tmp_path / "b2"
    _mkB(b2, "ba\nb\n")         # both ∉L, accept-all wrong → 0.0
    adapter = ArenaMetricAdapter(CegisReferee(), side="A", opponents=[b1, b2],
                                 aggregate="mean_gated", min_floor=0.5, penalty=1.0, seed=0)
    res = adapter.run(a, LocalBackend(), _EvalStub(), 60)
    fit = next(m for m in res.metrics if m["name"] == "arena_fitness")
    # mean = (1.0 + 0.0)/2 = 0.5 ; min = 0.0 < floor 0.5 → 0.5 - 1.0*(0.5-0.0) = 0.0
    assert res.data["mean"] == 0.5 and res.data["min"] == 0.0
    assert fit["value"] == 0.0


def test_adapter_mean_mode_ignores_min(tmp_path):
    a = tmp_path / "a"
    _mkA(a, "def accepts(s):\n    return True\n")
    b1 = tmp_path / "b1"
    _mkB(b1, "ab\n")
    b2 = tmp_path / "b2"
    _mkB(b2, "ba\nb\n")
    adapter = ArenaMetricAdapter(CegisReferee(), side="A", opponents=[b1, b2], aggregate="mean", seed=0)
    res = adapter.run(a, LocalBackend(), _EvalStub(), 60)
    fit = next(m for m in res.metrics if m["name"] == "arena_fitness")
    assert fit["value"] == 0.5


def test_adapter_no_opponents_fails(tmp_path):
    a = tmp_path / "a"
    _mkA(a, "def accepts(s):\n    return True\n")
    adapter = ArenaMetricAdapter(CegisReferee(), side="A", opponents=[], seed=0)
    res = adapter.run(a, LocalBackend(), _EvalStub(), 60)
    assert res.ok is False
