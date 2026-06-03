from pathlib import Path

from tyani_tolkai.agents import MockAdapter
from tyani_tolkai.config import Config
from tyani_tolkai.metrics.base import MetricResult
from tyani_tolkai.orchestrator import Orchestrator
from tyani_tolkai.sandbox import LocalBackend
from tyani_tolkai.state import StateStore


class FakeMetric:
    """Reads the first token of artifact/val.txt as the metric 's'."""

    def run(self, artifact_dir, sandbox, evaluation, timeout) -> MetricResult:
        p = Path(artifact_dir) / "val.txt"
        if not p.exists():
            return MetricResult([], "no val.txt", False)
        v = float(p.read_text().split()[0])
        return MetricResult([{"name": "s", "value": v, "dir": "higher", "weight": 1}], "", True)


def _cfg(target=100, plateau_N=3, max_iterations=20, min_delta=1.0):
    return Config(
        project="p",
        agents={"executor": {"engine": "mock"}},
        roles={"executor": {"goal": "raise s"}},
        evaluation={
            "adapter": "numeric",
            "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
            "target_score": target,
            "min_delta": min_delta,
        },
        limits={"max_iterations": max_iterations, "plateau_N": plateau_N},
    )


def _edit_val(v, tag=""):
    def e(workdir: Path) -> bool:
        (workdir / "val.txt").write_text(f"{v}\n# {tag}")
        return True
    return e


def _orch(tmp_path, edits, cfg):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    return Orchestrator(cfg, s, run_id, MockAdapter(edits), FakeMetric(), LocalBackend()), s, run_id


def test_improving_run_reaches_target(tmp_path):
    edits = [_edit_val(v) for v in (70, 80, 90, 100)]
    orch, s, run_id = _orch(tmp_path, edits, _cfg())
    summary = orch.run_loop()
    assert summary.reason == "target"
    assert summary.best_score == 100.0
    scores = [r.score for r in s.last_iterations(run_id, 10)]
    assert scores == sorted(scores)            # monotonic non-decreasing
    assert all(r.verdict == "keep" for r in s.last_iterations(run_id, 10))


def test_regression_is_discarded_and_reverted(tmp_path):
    orch, s, run_id = _orch(tmp_path, [_edit_val(70), _edit_val(60)], _cfg())
    o1 = orch.run_iteration()
    assert o1.verdict == "keep"
    o2 = orch.run_iteration()
    assert o2.verdict == "discard"
    assert (s.artifact_dir / "val.txt").read_text().split()[0] == "70"   # reverted
    assert s.best_score(run_id) == 70.0


def test_no_op_does_not_count_as_plateau(tmp_path):
    orch, s, run_id = _orch(tmp_path, [], _cfg())   # no scripted edits → no_op
    o = orch.run_iteration()
    assert o.verdict == "no_op"
    assert orch.no_op_count == 1
    assert orch.plateau_count == 0


def test_plateau_stops_the_loop(tmp_path):
    # first keeps 70, then meaningful-but-non-improving changes → plateau
    edits = [_edit_val(70, tag=i) for i in range(6)]
    orch, s, run_id = _orch(tmp_path, edits, _cfg(plateau_N=3))
    summary = orch.run_loop()
    assert summary.reason == "plateau"
    assert summary.best_score == 70.0
