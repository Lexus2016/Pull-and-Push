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
    project_dir, rename_project, reset_project, RUN_MARKER, STOP_REQUEST,
)
from .registry import build_adapter
from .sandbox import get_backend
from .state import StateStore
from .bot_io import load_bars_csv
from .bot_sandbox import score_bot_sandboxed as _score_bot_sandboxed, SandboxUnavailable, docker_available
from .bot_runner import split_command
from .bot_runner import BotProtocolError, score_bot, drive_bot
from .bot_protocol import seeded_oos_start
from . import bot_engine
from .validation import (
    score_controls, beats_controls, check_determinism, hash_artifacts, insample_oos_gap,
    build_evidence_report, evidence_verdict, reverse_oos, anti_lookahead_probe,
    synth_bars, check_adapter_orders, run_secondary_validation,
)
from .scaffold import scaffold_onboarding
from .proposal_schema import MetricProposal
from .profile_schema import BotProfile
from .adapter_gen import render_adapter_stub


def _print_iter(o):
    print(f"  iter {o.n:>3}: {o.verdict:<8} score={o.score}")


def _seed_artifact(state: StateStore, cfg: Config) -> None:
    """Populate artifact/ per cfg.seed before the first commit."""
    state.artifact_dir.mkdir(parents=True, exist_ok=True)
    if cfg.seed.mode == "copy" and cfg.seed.path:
        src = Path(cfg.seed.path).expanduser()
        if src.exists():
            from .scaffold import USER_CODE_IGNORE
            shutil.copytree(src, state.artifact_dir, dirs_exist_ok=True, ignore=USER_CODE_IGNORE)
        else:
            print(f"⚠ seed copy path not found: {src} (starting empty)")
    # 'empty' starts with no artifact — the first iteration creates the initial code.


def _build_rivals(cfg):
    """Construct the two rival executor adapters (claude/codex in prod). Module-level seam so
    tests can monkeypatch in deterministic scripted rivals. The ``scripted`` engine selects the
    offline CEGIS rivals (the zero-config Co-Evolution Arena demo)."""
    a = cfg.agents["rival_a"]
    b = cfg.agents["rival_b"]
    if "scripted" in (a.engine, b.engine):
        from .agents.scripted_rival import ScriptedAdversaryRival, ScriptedRecognizerRival
        return (ScriptedRecognizerRival(), ScriptedAdversaryRival())
    return (build_adapter(a.engine, a.model, "writeable", a.effort),
            build_adapter(b.engine, b.model, "writeable", b.effort))


def _run_symmetric(cfg, args) -> int:
    import tyani_tolkai.arena.cegis  # noqa: F401  (registers the cegis-recognizer referee)
    from .arena.referee import get_referee
    from .symmetric import SymmetricOrchestrator

    base = project_dir(cfg.project)
    base.mkdir(parents=True, exist_ok=True)
    referee = get_referee(cfg.arena.referee)
    ex_a, ex_b = _build_rivals(cfg)
    sandbox = get_backend(cfg.sandbox.backend, cfg.sandbox)
    print(f"▶ symmetric run: project={cfg.project!r}  referee={cfg.arena.referee}  "
          f"rivals={cfg.agents['rival_a'].engine}/{cfg.agents['rival_b'].engine}  "
          f"generations={cfg.arena.generations}")
    orch = SymmetricOrchestrator(cfg, base, referee, ex_a, ex_b, sandbox)
    try:
        result = orch.run(should_stop=_stop_requested(cfg))
    finally:
        orch.close()
    print(f"✔ finished: reason={result.stop_reason}  generations={result.generations}")
    print(f"  best-A-vs-all-B = champion #{result.best_a_id}")
    print(f"  best-B-vs-all-A = champion #{result.best_b_id}")
    curve = ["%.2f" % s for s in result.stable_a if s is not None]
    print(f"  stable-A curve  = {curve}")
    return 0


def cmd_run(args) -> int:
    """Run (or --resume) a config. A run.pid marker lives in the project for the run's lifetime so a
    dashboard started meanwhile shows it as running instead of 'healing' it to stopped."""
    cfg = load_config(args.config)
    base = project_dir(cfg.project)
    base.mkdir(parents=True, exist_ok=True)
    marker = base / RUN_MARKER
    marker.write_text(str(os.getpid()), encoding="utf-8")
    (base / STOP_REQUEST).unlink(missing_ok=True)       # a stale request must not stop this run
    try:
        if cfg.mode == "symmetric":
            return _run_symmetric(cfg, args)
        return _run_asymmetric(cfg, args)
    finally:
        marker.unlink(missing_ok=True)
        (base / STOP_REQUEST).unlink(missing_ok=True)


def _stop_requested(cfg):
    """should_stop hook: the dashboard asks a CLI run to stop by dropping a file in the project."""
    path = project_dir(cfg.project) / STOP_REQUEST
    return lambda: path.exists()


def _run_asymmetric(cfg, args) -> int:
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
    executor = build_adapter(ex.engine, ex.model, "writeable", ex.effort)
    validator = None
    if "validator" in cfg.agents:
        va = cfg.agents["validator"]
        validator = build_adapter(va.engine, va.model, "read-only", va.effort)

    metric_adapter = get_metric_adapter(cfg.evaluation.adapter)
    sandbox = get_backend(cfg.sandbox.backend, cfg.sandbox)

    orch = Orchestrator(cfg, state, run_id, executor, metric_adapter, sandbox, validator)
    print(f"▶ run: project={cfg.project!r}  executor={ex.engine}  "
          f"validator={cfg.agents.get('validator').engine if validator else 'none'}  "
          f"target={cfg.evaluation.target_score}")
    summary = orch.run_loop(on_iteration=_print_iter, should_stop=_stop_requested(cfg))
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
    bot_cmd = split_command(args.bot_cmd)
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
    bot_cmd = split_command(args.bot_cmd)

    # An UNTRUSTED bot MUST run in the Docker sandbox — the isolation IS the trust mechanism (ADR).
    # `--trusted` is an explicit operator override for a reference/own bot: process-separation only,
    # no OS sandbox (matches score_bot's trusted-only contract), usable where Docker is absent.
    if args.trusted:
        isolation = "subprocess (process-separation only; --trusted asserted by operator)"
        def score_bars(b):
            return score_bot(bot_cmd, b, params=params, seed=args.seed, cwd=str(bot_dir),
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
        result = run_secondary_validation(
            bars=bars, score_bars=score_bars, isolation=isolation, seed=args.seed, params=params,
            name=(args.name or bot_dir.name), data_path=data, engine_path=bot_engine.__file__,
            config_extra={"bot_cmd": bot_cmd})
    except (SandboxUnavailable, BotProtocolError) as exc:
        print(f"scoring failed: {exc}", file=sys.stderr)
        return 1

    report = result["report"]
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"evidence report written to {args.out}", file=sys.stderr)
    print(report)
    return 0 if result["verdict"] == "PASS" else 3


def cmd_onboard(args) -> int:
    """Turn an existing bot + its data + a P2 proposal into a runnable optimization project.

    Lays the bot into the artifact, the data into metrics/ (outside the artifact), and writes a
    config that scores the bot via the vetted `score-bot` numeric adapter. Then `run` it.
    """
    bot_dir = Path(args.bot_dir)
    if not bot_dir.is_dir():
        print(f"bot-dir not found: {bot_dir}", file=sys.stderr)
        return 2
    data = Path(args.data)
    if not data.is_file():
        print(f"data not found: {data}", file=sys.stderr)
        return 2
    proposal_path = Path(args.proposal)
    if not proposal_path.exists():
        print(f"proposal not found: {proposal_path}", file=sys.stderr)
        return 2
    try:
        proposal = MetricProposal.model_validate_json(proposal_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        print(f"invalid proposal.json: {exc}", file=sys.stderr)
        return 2
    try:
        res = scaffold_onboarding(args.name, bot_dir=bot_dir, data_path=data, proposal=proposal,
                                  bot_cmd=args.bot_cmd, seed_token=args.seed, goal=args.goal)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"onboarding failed: {exc}", file=sys.stderr)
        return 2
    cfg_path = project_dir(args.name) / "config.yaml"
    print(f"created project {res['created']!r} (bot → artifact, data → {res['metrics']}/data.csv)")
    print(f"next: pull-and-push run {cfg_path}")
    print("notes: a real run needs Docker (score-bot sandboxes the bot); and --bot-cmd must speak "
          "the P3 bot protocol — wrap a raw bot with an adapter (see bot_adapter.py).")
    return 0


def cmd_gen_adapter(args) -> int:
    """Scaffold a starter adapter.py (vetted protocol plumbing + a decide() stub to fill)."""
    bot_dir = Path(args.bot_dir)
    if not bot_dir.is_dir():
        print(f"bot-dir not found: {bot_dir}", file=sys.stderr)
        return 2
    profile = None
    if args.profile:
        pp = Path(args.profile)
        if not pp.exists():
            print(f"profile not found: {pp}", file=sys.stderr)
            return 2
        try:
            profile = BotProfile.model_validate_json(pp.read_text(encoding="utf-8"))
        except ValidationError as exc:
            print(f"invalid profile.json: {exc}", file=sys.stderr)
            return 2
    out = Path(args.out) if args.out else bot_dir / "adapter.py"
    if out.exists() and not args.force:
        print(f"{out} already exists; pass --force to overwrite", file=sys.stderr)
        return 2
    out.write_text(render_adapter_stub(profile=profile), encoding="utf-8")
    print(f"wrote {out}")
    print(f'fill in decide(), then: pull-and-push check-adapter --bot-dir {bot_dir} '
          f'--bot-cmd "python {out.name}"')
    return 0


def cmd_check_adapter(args) -> int:
    """Prove a bot adapter speaks the P3 protocol: well-formed + deterministic + non-degenerate.

    Drives the adapter over the real protocol on a synthetic series, twice. PASS means it is
    protocol-sound — NOT that it is semantically correct (sign/scale): run `validate` next for that.
    Exit 0=PASS, 3=FLAG, 1=adapter protocol/timeout error, 2=bad input.
    """
    bot_dir = Path(args.bot_dir)
    if not bot_dir.is_dir():
        print(f"bot-dir not found: {bot_dir}", file=sys.stderr)
        return 2
    bars = synth_bars(args.n)
    bot_cmd = split_command(args.bot_cmd)
    try:
        run1 = drive_bot(bot_cmd, bars, params={}, per_read_timeout=args.per_read_timeout,
                         total_timeout=args.total_timeout, cwd=str(bot_dir))
        run2 = drive_bot(bot_cmd, bars, params={}, per_read_timeout=args.per_read_timeout,
                         total_timeout=args.total_timeout, cwd=str(bot_dir))
    except BotProtocolError as exc:
        print(f"adapter failed the protocol: {exc}", file=sys.stderr)
        return 1
    v = check_adapter_orders(run1, run2, len(bars))
    print(f"adapter check: {'PASS' if v['ok'] else 'FLAG'}")
    print(f"- well-formed:    {v['well_formed']}")
    print(f"- deterministic:  {v['deterministic']}")
    print(f"- non-degenerate: {not v['degenerate']}")
    for r in v["reasons"]:
        print(f"- {r}")
    if v["ok"]:
        print("next: `validate` — check-adapter proves the protocol; validate checks semantics "
              "(sign/scale) via the control spectrum + the human evidence report.")
    return 0 if v["ok"] else 3


def _web_socket(host: str, port: str):
    """A bound listening socket, handed to uvicorn as is — probing a port and binding it later
    races with anything else starting up. ``auto``: 8765 when free (the address CLI commands try
    first), else any free port."""
    import socket
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    for want in ((8765, 0) if port == "auto" else (int(port),)):
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            if os.name != "nt":      # on Windows it lets a second socket take a port in use
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, want))
            s.listen(128)
            return s
        except OSError:
            s.close()
            if port != "auto" or want == 0:
                raise
    raise OSError(f"no free port on {host}")


def _exit_when_stdin_closes() -> None:
    """--parent-pipe: the macOS app keeps our stdin open for its lifetime. EOF = the app is gone
    (quit, crash, force-quit): shut down like on SIGTERM, which also kills live agents — they run
    in their own process groups and would otherwise outlive both of us, still spending."""
    import signal
    import threading

    def watch():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=watch, name="parent-pipe", daemon=True).start()


def _dashboard_answers(live: dict) -> bool:
    """Is the dashboard named by a marker really up (not just a reused PID)?"""
    import urllib.parse
    import urllib.request
    q = f"?token={urllib.parse.quote(live['token'])}" if live.get("token") else ""
    try:
        with urllib.request.urlopen(f"{live['url']}/api/meta{q}", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def cmd_web(args) -> int:
    from .projects import (clear_dashboard_marker, home_root, read_dashboard_marker, web_token,
                           write_dashboard_marker)
    from .web.server import LOOPBACK_HOSTS, create_app
    import logging
    import uvicorn

    # operational logging (run start/end/checkpoint/errors). User-facing CLI output stays print().
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = args.password or os.environ.get("TYANI_TOLKAI_WEB_PASSWORD")
    if not token and not args.no_auth:
        # the API starts paid agent runs and executes scorers: other local users and processes
        # must not reach it with a bare GET/POST. The token is stable (bookmarks keep working).
        token = web_token()
    live = read_dashboard_marker()
    if live and live.get("pid") != os.getpid() and _dashboard_answers(live):
        # one dashboard per data dir: a second one would mark the first one's live runs "stopped"
        # at startup and could start the same project twice (two loops on one git repo)
        print(f"✖ a dashboard for {home_root()} is already running: {live['url']} "
              f"(pid {live['pid']}) — open that one, or stop it first", file=sys.stderr)
        return 3
    sock = _web_socket(args.host, str(args.port))
    port = sock.getsockname()[1]
    loopback = args.host in LOOPBACK_HOSTS
    app = create_app(token, allowed_hosts=LOOPBACK_HOSTS if loopback else None)
    app.state.shutdown_hooks.append(clear_dashboard_marker)
    shown = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{shown}:{port}/" + (f"?token={token}" if token else "")
    local = url if loopback else f"http://127.0.0.1:{port}/"
    write_dashboard_marker(local.split("?")[0].rstrip("/"), token)   # for `research start`
    print(f"▶ WebUI ready: {url}", flush=True)                       # the macOS app reads this line
    if token:
        print("  this link signs the browser in; afterwards the plain address is enough "
              "(lost it? `pull-and-push url --open`)")
    else:
        print("  ⚠ --no-auth: anyone who can reach this port can start runs and execute scorers")
    if args.parent_pipe:
        _exit_when_stdin_closes()
    import threading
    from .settings import latest_release
    threading.Thread(target=latest_release, name="update-check", daemon=True).start()   # ≤1/day
    # no access log: every request carries ?token= in its URL
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    try:
        server.run(sockets=[sock])
    finally:
        clear_dashboard_marker()
    return 0


def cmd_url(args) -> int:
    """The running dashboard's sign-in link (the macOS app's or the terminal's)."""
    from .projects import read_dashboard_marker
    live = read_dashboard_marker()
    if not live:
        print("✖ no dashboard is running — start the app, or: pull-and-push web", file=sys.stderr)
        return 1
    import urllib.parse
    link = live["url"] + "/" + (f"?token={urllib.parse.quote(live['token'])}" if live.get("token") else "")
    print(link)
    if args.open:
        import webbrowser
        webbrowser.open(link)
    return 0


def cmd_research(args) -> int:
    """Research kits (docs/design/p8-research-kits.md): new / check / create / start / save."""
    from . import research as rs
    a = args.action
    try:
        if a == "new":
            kit = rs.new_kit(args.target, name=args.name)
            print(f"✔ kit skeleton → {kit}\n  edit research.yaml, seed/ and scorer/, then: "
                  f"pull-and-push research check {kit}")
        elif a == "check":
            rep = rs.check_kit(args.target)
            print(json.dumps(rep, indent=2, default=str) if args.json else rs.format_check(rep))
            return 0 if rep["ok"] else 3
        elif a == "create":
            out = rs.create_from_kit(args.target, name=args.name, check=not args.no_check)
            if out.get("check"):
                print(rs.format_check(out["check"]))
            print(f"✔ project {out['created']!r} created → pull-and-push research start {out['created']}")
        elif a == "start":
            name = args.target
            if not args.foreground:
                from .projects import read_dashboard_marker
                live = read_dashboard_marker() or {}           # the running dashboard / macOS app
                url = args.url or live.get("url") or "http://127.0.0.1:8765"
                token = (args.token or os.environ.get("TYANI_TOLKAI_WEB_PASSWORD")
                         or (live.get("token") if url == live.get("url") else None))
                try:
                    rs.start_via_dashboard(name, url, token)
                    print(f"✔ started in the dashboard ({url}) — watch it there; "
                          f"poll with: pull-and-push status {name}")
                    return 0
                except OSError as e:                      # URLError / HTTPError / refused
                    if getattr(e, "code", None) is not None:   # the dashboard answered with an error
                        print(f"✖ dashboard refused: {e}", file=sys.stderr)
                        return 1
                    print(f"· no dashboard at {url} — running in the foreground")
            return cmd_run(argparse.Namespace(config=str(project_dir(name) / "config.yaml"),
                                              resume=True))
        elif a == "save":
            if not args.to:
                raise ValueError("research save needs --to <kit dir>")
            kit = rs.save_kit(args.target, args.to, source=args.source, name=args.name)
            print(f"✔ kit saved → {kit} (seed = the project's {args.source}); edit it, then "
                  f"pull-and-push research create {kit} --name <new-name>")
        return 0
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2


def cmd_status(args) -> int:
    from . import research as rs
    try:
        s = rs.project_status(args.name, last=args.last)
    except (FileNotFoundError, ValueError) as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    print(json.dumps(s, indent=2, default=str) if args.json else rs.format_status(s))
    return 0


def cmd_report(args) -> int:
    from . import research as rs
    try:
        r = rs.project_report(args.name)
    except (FileNotFoundError, ValueError) as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2, default=str) if args.json else rs.format_report(r))
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
    pw.add_argument("--port", default="8765", help="a port, or 'auto' (8765 if free, else any)")
    pw.add_argument("--no-auth", action="store_true",
                    help="no token (anyone who reaches the port can use the API) — not advised")
    pw.add_argument("--parent-pipe", action="store_true",
                    help="exit when stdin closes (the macOS app holds it open for its lifetime)")
    pw.add_argument("--password", default=None, help="protect the UI (else open locally)")
    pw.set_defaults(func=cmd_web)

    pu = sub.add_parser("url", help="print the running dashboard's sign-in link")
    pu.add_argument("--open", action="store_true", help="also open it in the browser")
    pu.set_defaults(func=cmd_url)

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

    po = sub.add_parser("onboard",
                        help="turn an existing bot + data + a P2 proposal into a runnable optimization project")
    po.add_argument("--bot-dir", required=True, help="the bot's source dir (copied into the artifact)")
    po.add_argument("--data", required=True, help="OHLCV CSV (time,open,high,low,close,volume)")
    po.add_argument("--proposal", required=True, help="path to a P2 proposal.json")
    po.add_argument("--name", required=True, help="new project name")
    po.add_argument("--bot-cmd", required=True, help="command to run the bot in the sandbox (shlex-split)")
    po.add_argument("--seed", default=None, help="OOS-split seed token (default: the project name)")
    po.add_argument("--goal", default=None, help="optimization goal (default: the proposal's goal)")
    po.set_defaults(func=cmd_onboard)

    pg = sub.add_parser("gen-adapter",
                        help="scaffold a starter adapter.py (vetted plumbing + a decide() stub to fill)")
    pg.add_argument("--bot-dir", required=True, help="dir to write adapter.py into")
    pg.add_argument("--profile", default=None, help="optional P1 profile.json for decide() hints")
    pg.add_argument("--out", default=None, help="output path (default: <bot-dir>/adapter.py)")
    pg.add_argument("--force", action="store_true", help="overwrite an existing file")
    pg.set_defaults(func=cmd_gen_adapter)

    pc = sub.add_parser("check-adapter",
                        help="prove an adapter speaks the P3 protocol (well-formed + deterministic + non-degenerate)")
    pc.add_argument("--bot-dir", required=True, help="the adapter/bot dir")
    pc.add_argument("--bot-cmd", required=True, help="command to run the adapter (shlex-split)")
    pc.add_argument("--n", type=int, default=40, help="number of synthetic probe bars")
    pc.add_argument("--per-read-timeout", type=float, default=10.0)
    pc.add_argument("--total-timeout", type=float, default=120.0)
    pc.set_defaults(func=cmd_check_adapter)

    prs = sub.add_parser("research", help="research kits: an experiment as research.yaml + seed/ + "
                                          "scorer/ (new / check / create / start / save)")
    prs.add_argument("action", choices=["new", "check", "create", "start", "save"])
    prs.add_argument("target", help="kit dir (new / check / create) or project name (start / save)")
    prs.add_argument("--name", default=None, help="project / kit name (default: from the kit)")
    prs.add_argument("--to", default=None, help="save: the kit dir to write")
    prs.add_argument("--source", choices=["best", "seed"], default="best",
                     help="save: seed the new kit with the project's best result or its original seed")
    prs.add_argument("--no-check", action="store_true", help="create: skip the pre-flight (not advised)")
    prs.add_argument("--json", action="store_true", help="check: machine-readable report")
    prs.add_argument("--url", default=None, help="start: the dashboard to run it in (default: the "
                                                 "running dashboard or app, else 127.0.0.1:8765)")
    prs.add_argument("--token", default=None, help="start: dashboard token (default: env password)")
    prs.add_argument("--foreground", action="store_true", help="start: run here, not in the dashboard")
    prs.set_defaults(func=cmd_research)

    pst = sub.add_parser("status", help="compact state of a project's latest run")
    pst.add_argument("name")
    pst.add_argument("--last", type=int, default=5, help="how many recent iterations to show")
    pst.add_argument("--json", action="store_true")
    pst.set_defaults(func=cmd_status)

    prp = sub.add_parser("report", help="what a run achieved: metrics start→best→target, kept steps, diff")
    prp.add_argument("name")
    prp.add_argument("--json", action="store_true")
    prp.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
