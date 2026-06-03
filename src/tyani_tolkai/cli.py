"""CLI entrypoint: `tyani-tolkai run <config>` and `tyani-tolkai demo`.

Phase 1 ships a runnable `demo` (mock agent, real numeric adapter + sandbox) so the
loop can be watched converging live. `run` wires a real config; real CLI-agent
adapters arrive in Phase 2 (until then it explains that clearly).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .config import load_config
from .orchestrator import Orchestrator
from .registry import AdapterRegistry
from .state import StateStore


def _print_iter(o):
    print(f"  iter {o.n:>3}: {o.verdict:<8} score={o.score}")


def cmd_demo(_args) -> int:
    from ._demo import build_demo

    with tempfile.TemporaryDirectory() as d:
        cfg, state, run_id, orch = build_demo(d)
        print(f"▶ demo: project={cfg.project}  target={cfg.evaluation.target_score}")
        print("  (mock Executor raises VALUE; numeric adapter scores it in a local sandbox)")
        summary = orch.run_loop(on_iteration=_print_iter)
        print(f"✔ finished: reason={summary.reason}  best_score={summary.best_score}  "
              f"iterations={summary.iterations}")
        state.close()
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    base = Path.home() / ".tyani-tolkai" / "projects" / cfg.project
    state = StateStore(base)
    if not (state.artifact_dir / ".git").exists():
        state.git_init()
    if args.resume:
        removed = state.reconcile_all() if hasattr(state, "reconcile_all") else 0
        print(f"resume: reconciled (removed {removed} phantom rows)")

    engine = cfg.agents["executor"].engine
    registry = AdapterRegistry()
    try:
        registry.get(engine)
    except NotImplementedError:
        print(f"✋ engine {engine!r} is a real CLI agent — wired in Phase 2.\n"
              f"   For now, try:  tyani-tolkai demo  (runnable mock-driven loop)\n"
              f"   Project state initialized at: {base}")
        state.close()
        return 2
    print(f"loaded config for {cfg.project!r}; orchestrator wiring ready.")
    state.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tyani-tolkai",
                                description="Adversarial co-evolution agent orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a config (real agents — Phase 2)")
    pr.add_argument("config", help="path to config.yaml")
    pr.add_argument("--resume", action="store_true", help="resume an existing project")
    pr.set_defaults(func=cmd_run)

    pd = sub.add_parser("demo", help="run the built-in mock-driven demo loop")
    pd.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
