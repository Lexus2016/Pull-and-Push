"""Golden run — the honest proof the loop raises real quality (spec §14).

A solution starts passing 1/10 hidden tests; a scripted mock widens it each
iteration; the pytest-pass adapter scores it. Asserts the score climbs to 100,
the loop stops on target, and the harness (in metrics/, invisible to the Executor)
is untouched — the anti-collusion guarantee.
"""

from pathlib import Path

from tyani_tolkai.agents import MockAdapter
from tyani_tolkai.config import Config
from tyani_tolkai.metrics import get_metric_adapter
from tyani_tolkai.orchestrator import Orchestrator
from tyani_tolkai.sandbox import LocalBackend
from tyani_tolkai.state import StateStore

SOLUTION = "LIMIT = {limit}\ndef f(n):\n    return n * n if n < LIMIT else 0\n"


def _bump(limit):
    def edit(workdir: Path) -> bool:
        (workdir / "solution.py").write_text(SOLUTION.format(limit=limit))
        return True
    return edit


def test_golden_run_pytest_pass(tmp_path):
    proj = tmp_path / "proj"
    state = StateStore(proj)
    state.git_init()
    art = state.artifact_dir
    (art / "solution.py").write_text(SOLUTION.format(limit=1))
    state.commit("seed solution")

    # hidden harness — lives OUTSIDE artifact/, invisible to the Executor (lock #4)
    metrics_dir = proj / "metrics"
    metrics_dir.mkdir()
    tests_src = "from solution import f\n" + "".join(
        f"def test_{i}():\n    assert f({i}) == {i * i}\n" for i in range(10)
    )
    (metrics_dir / "test_solution.py").write_text(tests_src)

    run_id = state.create_run("asymmetric")
    cfg = Config(
        project="golden",
        agents={"executor": {"engine": "mock"}},
        roles={"executor": {"goal": "make all tests pass"}},
        evaluation={
            "adapter": "pytest-pass",
            "metrics": [{"name": "pass_pct", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
            "target_score": 100,
            "min_delta": 1.0,
            "harness_dir": "metrics",
        },
        limits={"max_iterations": 10, "plateau_N": 5},
    )

    orch = Orchestrator(
        cfg, state, run_id, MockAdapter([_bump(4), _bump(7), _bump(10)]),
        get_metric_adapter("pytest-pass"), LocalBackend(),
    )
    summary = orch.run_loop()

    # 1) converged to target
    assert summary.reason == "target"
    assert summary.best_score == 100.0

    # 2) monotonic non-decreasing across kept iterations (hill-climbing)
    kept = [r.score for r in state.last_iterations(run_id, 20) if r.verdict == "keep"]
    assert kept == sorted(kept)
    assert kept[-1] == 100.0

    # 3) anti-collusion: the harness was never touched (Executor can't reach metrics/)
    assert (metrics_dir / "test_solution.py").read_text() == tests_src
    assert not str(metrics_dir).startswith(str(art))   # harness lives outside artifact

    # 4) no reverted/uncommitted state leaked into HEAD
    assert state.diff_uncommitted() == ""
