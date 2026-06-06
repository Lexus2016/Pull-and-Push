"""CLI entrypoint: `tyani-tolkai run <config>`, `projects`, and `web`.

`run` wires a real config and drives the adversarial loop with the configured
CLI-agent adapters; `projects` is CRUD over saved runs; `web` serves the dashboard.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .config import Config, load_config
from .profiler import analyze_bot as _analyze_bot, render_markdown as _render_markdown
from .metrics import get_metric_adapter
from .orchestrator import Orchestrator
from .projects import (
    delete_project, export_project, import_project, list_projects,
    project_dir, rename_project, reset_project,
)
from .registry import build_adapter
from .sandbox import get_backend
from .state import StateStore


def _print_iter(o):
    print(f"  iter {o.n:>3}: {o.verdict:<8} score={o.score}")


def _seed_artifact(state: StateStore, cfg: Config) -> None:
    """Populate artifact/ per cfg.seed before the first commit."""
    state.artifact_dir.mkdir(parents=True, exist_ok=True)
    if cfg.seed.mode == "copy" and cfg.seed.path:
        src = Path(cfg.seed.path).expanduser()
        if src.exists():
            shutil.copytree(src, state.artifact_dir, dirs_exist_ok=True)
        else:
            print(f"⚠ seed copy path not found: {src} (starting empty)")
    # 'empty' starts with no artifact — the first iteration creates the initial code.


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    base = project_dir(cfg.project)
    state = StateStore(base)
    fresh = not (state.artifact_dir / ".git").exists()
    if fresh:
        _seed_artifact(state, cfg)
        state.git_init()
        shutil.copy2(args.config, base / "config.yaml")  # self-contained project

    if args.resume:
        run_id = state.conn.execute(
            "SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()
        if run_id is None:
            print("nothing to resume; starting a new run")
            run_id = state.create_run(cfg.mode)
        else:
            run_id = run_id["id"]
            removed = state.reconcile(run_id)
            if state.has_changes():            # discard a candidate left dirty by a crash
                state.revert_uncommitted()
            state.set_status(run_id, "running")
            print(f"resume run #{run_id} (reconciled {removed} phantom rows)")
    else:
        run_id = state.create_run(cfg.mode)

    ex = cfg.agents["executor"]
    executor = build_adapter(ex.engine, ex.model, "writeable")
    validator = None
    if "validator" in cfg.agents:
        va = cfg.agents["validator"]
        validator = build_adapter(va.engine, va.model, "read-only")

    metric_adapter = get_metric_adapter(cfg.evaluation.adapter)
    sandbox = get_backend(cfg.sandbox.backend, cfg.sandbox)

    orch = Orchestrator(cfg, state, run_id, executor, metric_adapter, sandbox, validator)
    print(f"▶ run: project={cfg.project!r}  executor={ex.engine}  "
          f"validator={cfg.agents.get('validator').engine if validator else 'none'}  "
          f"target={cfg.evaluation.target_score}")
    summary = orch.run_loop(on_iteration=_print_iter)
    print(f"✔ finished: reason={summary.reason}  best_score={summary.best_score}  "
          f"iterations={summary.iterations}")
    state.close()
    return 0


def cmd_projects(args) -> int:
    action = args.action
    if action == "list":
        names = list_projects()
        print("\n".join(names) if names else "(no projects)")
    elif action == "delete":
        delete_project(args.name)
        print(f"deleted {args.name!r}")
    elif action == "rename":
        rename_project(args.name, args.to)
        print(f"renamed {args.name!r} → {args.to!r}")
    elif action == "reset":
        reset_project(args.name)
        print(f"reset {args.name!r} to its seed")
    elif action == "export":
        dest = export_project(args.name, args.to)
        print(f"exported {args.name!r} → {dest}")
    elif action == "import":
        new = import_project(args.name, args.to)   # name = .zip path, --to = new name
        print(f"imported → project {new!r}")
    return 0


def cmd_profile(args) -> int:
    """Analyze an existing bot (read-only) and write profile.json + profile.md."""
    src = Path(args.path)
    if not src.exists():
        print(f"path not found: {src}")
        return 2
    profile = _analyze_bot(src, engine=args.engine, model=args.model, timeout=args.timeout)
    out_dir = Path(args.out) if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "profile.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "profile.md").write_text(_render_markdown(profile), encoding="utf-8")
    print(f"wrote {out_dir / 'profile.json'} and {out_dir / 'profile.md'}")
    return 0


def cmd_web(args) -> int:
    from .web.server import create_app
    import logging
    import uvicorn

    # operational logging (run start/end/checkpoint/errors). User-facing CLI output stays print().
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = args.password or os.environ.get("TYANI_TOLKAI_WEB_PASSWORD")
    app = create_app(token)
    url = f"http://{args.host}:{args.port}/" + (f"?token={token}" if token else "")
    print(f"▶ WebUI ready: {url}")
    if not token:
        print("  (no password set — open locally; set TYANI_TOLKAI_WEB_PASSWORD to protect)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pull-and-push",
                                description="Pull-and-Push — adversarial co-evolution agent orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a config (real agents — Phase 2)")
    pr.add_argument("config", help="path to config.yaml")
    pr.add_argument("--resume", action="store_true", help="resume an existing project")
    pr.set_defaults(func=cmd_run)

    pp = sub.add_parser("projects", help="manage projects")
    pp.add_argument("action", choices=["list", "delete", "rename", "reset", "export", "import"])
    pp.add_argument("name", nargs="?", help="project name (or tar path for import)")
    pp.add_argument("--to", help="new name (rename), dest tar (export), or new name (import)")
    pp.set_defaults(func=cmd_projects)

    pw = sub.add_parser("web", help="launch the WebUI dashboard")
    pw.add_argument("--host", default="127.0.0.1")
    pw.add_argument("--port", type=int, default=8765)
    pw.add_argument("--password", default=None, help="protect the UI (else open locally)")
    pw.set_defaults(func=cmd_web)

    pf = sub.add_parser("profile", help="analyze an existing bot (read-only) -> BotProfile")
    pf.add_argument("path", help="path to the bot file or directory")
    pf.add_argument("--engine", default="claude", help="LLM engine (default: claude)")
    pf.add_argument("--model", default=None, help="optional model override")
    pf.add_argument("--out", default=None, help="output dir for profile.json/md (default: cwd)")
    pf.add_argument("--timeout", type=int, default=180, help="agent timeout seconds")
    pf.set_defaults(func=cmd_profile)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
