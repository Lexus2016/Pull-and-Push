"""Self-contained demo scenario for `tyani-tolkai demo`.

Builds a tiny numeric task (a `VALUE` the strategy should raise) wired through the
*real* numeric adapter + local sandbox, with a scripted mock Executor that improves
the value each iteration. Lets the operator watch the loop converge live, without
any paid agent. This is the "first working solution to test together".
"""

from __future__ import annotations

import sys
from pathlib import Path

from .agents import MockAdapter
from .config import Config
from .metrics import get_metric_adapter
from .orchestrator import Orchestrator
from .sandbox import get_backend
from .state import StateStore


def build_demo(base_dir: str | Path):
    proj = Path(base_dir) / "demo"
    state = StateStore(proj)
    state.git_init()
    artifact = state.artifact_dir
    (artifact / "strategy.py").write_text("VALUE = 40\n")
    (artifact / "backtest.py").write_text(
        "import json\nfrom strategy import VALUE\nprint(json.dumps({'s': VALUE}))\n"
    )
    state.commit("seed strategy")
    run_id = state.create_run("asymmetric")

    cfg = Config(
        project="demo",
        agents={"executor": {"engine": "mock"}},
        roles={"executor": {"goal": "raise VALUE toward the target"}},
        evaluation={
            "adapter": "numeric",
            "command": f"{sys.executable} backtest.py",
            "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
            "target_score": 100,
            "min_delta": 1.0,
        },
        limits={"max_iterations": 20, "plateau_N": 8},
    )

    def bump(v: int):
        def edit(workdir: Path) -> bool:
            (workdir / "strategy.py").write_text(f"VALUE = {v}\n")
            return True
        return edit

    edits = [bump(v) for v in (55, 70, 85, 100)]
    orch = Orchestrator(
        cfg, state, run_id, MockAdapter(edits),
        get_metric_adapter("numeric"), get_backend(cfg.sandbox.backend),
    )
    return cfg, state, run_id, orch
