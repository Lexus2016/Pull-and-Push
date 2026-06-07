"""CLI entrypoint: `tyani-tolkai run <config>`, `projects`, and `web`.

`run` wires a real config and drives the adversarial loop with the configured
CLI-agent adapters; `projects` is CRUD over saved runs; `web` serves the dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from .config import Config, load_config
from pydantic import ValidationError
from .profile_schema import BotProfile
from .profiler import analyze_bot as _analyze_bot, render_markdown as _render_markdown, ProfileError
from .proposer import propose_evaluation as _propose_evaluation, render_markdown as _render_proposal_md, ProposalError
from .metrics import get_metric_adapter
from .orchestrator import Orchestrator
from .projects import (
    delete_project, export_project, import_project, list_projects,
    project_dir, rename_project, reset_project,
)
from .registry import build_adapter
from .sandbox import get_backend
from .state import StateStore
from .bot_io import load_bars_csv
from .bot_sandbox import score_bot_sandboxed as _score_bot_sandboxed, SandboxUnavailable, docker_available
from .bot_runner import BotProtocolError, score_bot
from .bot_protocol import seeded_oos_start
from . import bot_engine
from .validation import (
    score_controls, beats_controls, check_determinism, hash_artifacts, insample_oos_gap,
    build_evidence_report, evidence_verdict, reverse_oos, anti_lookahead_probe,
)


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
    try:
        profile = _analyze_bot(src, engine=args.engine, model=args.model, timeout=args.timeout)
    except ProfileError as exc:
        print(f"analysis failed: {exc}")
        return 2
    out_dir = Path(args.out) if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "profile.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "profile.md").write_text(_render_markdown(profile), encoding="utf-8")
    print(f"wrote {out_dir / 'profile.json'} and {out_dir / 'profile.md'}")
    return 0


def cmd_propose(args) -> int:
    """Propose evaluation metrics + tunable ranges from a P1 profile.json + a goal."""
    src = Path(args.profile)
    if not src.exists():
        print(f"profile not found: {src}")
        return 2
    try:
        profile = BotProfile.model_validate_json(src.read_text(encoding="utf-8"))
    except ValidationError as exc:
        print(f"invalid profile.json: {exc}")
        return 2
    try:
        proposal = _propose_evaluation(profile, args.goal, engine=args.engine,
                                       model=args.model, timeout=args.timeout)
    except ProposalError as exc:
        print(f"proposal failed: {exc}")
        return 2
    out_dir = Path(args.out) if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "proposal.json").write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "proposal.md").write_text(_render_proposal_md(proposal), encoding="utf-8")
    print(f"wrote {out_dir / 'proposal.json'} and {out_dir / 'proposal.md'}")
    return 0


def cmd_score_bot(args) -> int:
    """Score a bot in the Docker sandbox and print the metrics JSON (stdout only).

    Designed to be the `command` of a `numeric` evaluation adapter: stdout is ONLY the metrics
    dict; diagnostics go to stderr; a failure exits non-zero so the adapter records a failed eval.
    """
    data = Path(args.data)
    if not data.exists():
        print(f"data not found: {data}", file=sys.stderr)
        return 2
    bot_dir = Path(args.bot_dir)
    if not bot_dir.exists():
        print(f"bot-dir not found: {bot_dir}", file=sys.stderr)
        return 2
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"invalid --params JSON: {exc}", file=sys.stderr)
        return 2
    bars = load_bars_csv(data)
    bot_cmd = shlex.split(args.bot_cmd)
    try:
        metrics = _score_bot_sandboxed(bot_cmd, bars, bot_dir=str(bot_dir), seed=args.seed,
                                       params=params, per_read_timeout=args.per_read_timeout,
                                       total_timeout=args.total_timeout)
    except (SandboxUnavailable, BotProtocolError) as exc:
        print(f"scoring failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(metrics))   # stdout = ONLY the metrics dict
    return 0


def cmd_validate(args) -> int:
    """P4 secondary validation → human evidence report (PASS/FLAG).

    Scores the bot in the Docker sandbox (required for an untrusted bot — the isolation IS the
    trust mechanism; ``--trusted`` overrides to a process-separation-only subprocess), then runs
    the secondary checks the ADR keeps as supporting evidence (NOT the trust mechanism): the
    control spectrum + beats-flat/random, two-run determinism, provenance hashing, the
    in-sample/OOS overfit gap, and an anti-look-ahead probe (re-score on a reversed-future
    timeline; the OOS score must react). The report goes to stdout (and --out); exit 0 = PASS,
    3 = FLAG, 1 = scoring error / Docker required, 2 = bad input.
    """
    data = Path(args.data)
    if not data.exists():
        print(f"data not found: {data}", file=sys.stderr)
        return 2
    bot_dir = Path(args.bot_dir)
    if not bot_dir.exists():
        print(f"bot-dir not found: {bot_dir}", file=sys.stderr)
        return 2
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"invalid --params JSON: {exc}", file=sys.stderr)
        return 2

    bars = load_bars_csv(data)
    bot_cmd = shlex.split(args.bot_cmd)
    oos_start = seeded_oos_start(len(bars), seed=args.seed)

    # An UNTRUSTED bot MUST run in the Docker sandbox — the isolation IS the trust mechanism (ADR).
    # `--trusted` is an explicit operator override for a reference/own bot: process-separation only,
    # no OS sandbox (matches score_bot's trusted-only contract), usable where Docker is absent.
    if args.trusted:
        isolation = "subprocess (process-separation only; --trusted asserted by operator)"
        def score_bars(b):
            return score_bot(bot_cmd, b, params=params, seed=args.seed,
                             per_read_timeout=args.per_read_timeout, total_timeout=args.total_timeout)
    elif docker_available():
        isolation = "docker-sandbox"
        def score_bars(b):
            return _score_bot_sandboxed(bot_cmd, b, bot_dir=str(bot_dir), seed=args.seed,
                                        params=params, per_read_timeout=args.per_read_timeout,
                                        total_timeout=args.total_timeout)
    else:
        print("ERROR: validating an UNTRUSTED bot requires the Docker sandbox (the isolation is the "
              "trust mechanism), but Docker is not available. Start Docker, or pass --trusted ONLY if "
              "you fully trust this bot — it then runs with process separation but no OS sandbox.",
              file=sys.stderr)
        return 1

    try:
        determinism_ok, runs = check_determinism(lambda: score_bars(bars), runs=2)
        bot_metrics = runs[0]
        # anti-look-ahead: re-score the SAME bot on a reversed-future timeline; a causal,
        # leak-free OOS score must react (delta != 0).
        bot_perturbed = score_bars(reverse_oos(bars, oos_start=oos_start))
    except (SandboxUnavailable, BotProtocolError) as exc:
        print(f"scoring failed: {exc}", file=sys.stderr)
        return 1

    controls = score_controls(bars, oos_start=oos_start, params=params)
    beats = beats_controls(bot_metrics, controls)
    hashes = hash_artifacts(engine_path=bot_engine.__file__, data_path=data,
                            config={"seed": args.seed, "params": params, "bot_cmd": bot_cmd,
                                    "oos_start": oos_start})
    gap = insample_oos_gap(bot_metrics)
    probe = anti_lookahead_probe(lambda: bot_metrics, lambda: bot_perturbed)

    report = build_evidence_report(
        bot_name=(args.name or bot_dir.name),
        bot_metrics=bot_metrics, control_metrics=controls, beats=beats,
        determinism_ok=determinism_ok, hashes=hashes, gap=gap,
        anti_lookahead=probe, isolation=isolation,
    )
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"evidence report written to {args.out}", file=sys.stderr)
    print(report)

    verdict, _ = evidence_verdict(beats=beats, determinism_ok=determinism_ok, gap=gap,
                                  anti_lookahead=probe)
    return 0 if verdict == "PASS" else 3


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

    pp2 = sub.add_parser("propose", help="propose evaluation metrics + tunable ranges from a profile + goal")
    pp2.add_argument("profile", help="path to a P1 profile.json")
    pp2.add_argument("--goal", required=True, help="free-text optimization goal")
    pp2.add_argument("--engine", default="claude", help="LLM engine (default: claude)")
    pp2.add_argument("--model", default=None, help="optional model override")
    pp2.add_argument("--out", default=None, help="output dir for proposal.json/md (default: cwd)")
    pp2.add_argument("--timeout", type=int, default=180, help="agent timeout seconds")
    pp2.set_defaults(func=cmd_propose)

    ps = sub.add_parser("score-bot", help="score a bot in the Docker sandbox -> metrics JSON (for a numeric adapter command)")
    ps.add_argument("--data", required=True, help="OHLCV CSV (time,open,high,low,close,volume)")
    ps.add_argument("--bot-dir", required=True, help="dir mounted read-only into the sandbox")
    ps.add_argument("--bot-cmd", required=True, help="command to run the bot inside the container (shlex-split)")
    ps.add_argument("--seed", required=True, help="per-project seed for the hidden OOS split")
    ps.add_argument("--params", default=None, help="JSON dict of tunable params")
    ps.add_argument("--per-read-timeout", type=float, default=10.0)
    ps.add_argument("--total-timeout", type=float, default=120.0)
    ps.set_defaults(func=cmd_score_bot)

    pv = sub.add_parser("validate",
                        help="P4 secondary validation of a bot -> human evidence report (PASS/FLAG)")
    pv.add_argument("--data", required=True, help="OHLCV CSV (time,open,high,low,close,volume)")
    pv.add_argument("--bot-dir", required=True, help="dir mounted read-only into the sandbox")
    pv.add_argument("--bot-cmd", required=True, help="command to run the bot (shlex-split)")
    pv.add_argument("--seed", required=True, help="per-project seed for the hidden OOS split")
    pv.add_argument("--params", default=None, help="JSON dict of tunable params")
    pv.add_argument("--name", default=None, help="bot name for the report (default: bot-dir name)")
    pv.add_argument("--out", default=None, help="also write the evidence report (Markdown) to this path")
    pv.add_argument("--trusted", action="store_true",
                    help="bot is trusted/your own — run via subprocess (process separation only, no "
                         "OS sandbox) instead of requiring Docker. Use ONLY if you fully trust the bot.")
    pv.add_argument("--per-read-timeout", type=float, default=10.0)
    pv.add_argument("--total-timeout", type=float, default=120.0)
    pv.set_defaults(func=cmd_validate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
