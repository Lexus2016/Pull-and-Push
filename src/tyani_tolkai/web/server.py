"""FastAPI backend for the Pull-and-Push dashboard.

Live updates use polling (GET /live), not WebSocket — simpler and robust across the
run thread boundary while giving the same live evolution chart. A background thread
runs the orchestrator; the UI polls its outcomes. Optional token auth (from the
TYANI_TOLKAI_WEB_PASSWORD env) protects the API when set.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("pull_and_push")   # operational events; configured by the CLI's basicConfig

import yaml
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from ..config import Config, load_config
from ..metrics import get_metric_adapter
from ..orchestrator import Orchestrator
from ..projects import (
    cli_run_alive, delete_project, export_project, fork_project, home_root, list_projects, project_dir,
    rename_project, reset_project, valid_name,
)
from ..registry import build_adapter
from ..sandbox import get_backend
from ..state import StateStore, _now

STATIC = Path(__file__).parent / "static"


def _fire_webhook(cfg, name: str, payload: dict) -> None:
    """Call the user-configured completion webhook (their own URL, opt-in). Best-effort:
    a webhook failure must never affect the run. GET appends project/status as query
    params; POST sends the payload as JSON."""
    n = getattr(cfg, "notify", None)
    if not (n and n.enabled and n.url):
        return
    import json as _json
    import urllib.parse
    import urllib.request
    try:
        if n.method == "GET":
            sep = "&" if "?" in n.url else "?"
            url = (n.url + sep + urllib.parse.urlencode(
                {"project": name, "status": payload.get("status", "")}))
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                n.url, data=_json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:
        pass


def _mark_arena_manifest(base: Path, status: str) -> None:
    """Rewrite a symmetric project's arena.json status when its run thread is gone (crash, or a
    server restart mid-run). Otherwise the manifest keeps saying 'running' and the dashboard polls
    a dead run forever. Only a 'running' manifest is touched; resume treats any non-'finished'
    status the same, so this never changes what the next Run does."""
    mpath = base / "arena.json"
    try:
        m = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(m, dict) or m.get("status") != "running":
        return
    m["status"] = status
    tmp = mpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=2), encoding="utf-8")
    os.replace(tmp, mpath)


def _select_run_id(state):
    """Pick the run to (re)use when Run is pressed: ALWAYS RESUME the latest run that has history,
    so pressing Run CONTINUES from the last iteration — the iteration count carries on and previous
    iterations are NEVER lost. This holds after every finish reason (plateau / max-iterations /
    budget / even a target win): raising plateau_N / max_iterations / target_score in Settings and
    pressing Run extends the SAME run. A genuinely fresh start is a separate, explicit, confirmed
    action (Reset / Fork) — never a side effect of Run. An empty zombie run (created but with no
    iterations) is skipped so it can't strand real history behind it. Returns (run_id | None,
    resuming: bool); None means there is no prior history yet, so the caller creates the first run."""
    last = state.conn.execute(
        "SELECT id FROM run WHERE id IN (SELECT DISTINCT run_id FROM iteration) "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        return int(last["id"]), True
    return None, False


class RunManager:
    """Tracks background runs and their streamed outcomes (in memory)."""

    def __init__(self):
        self._runs: dict[str, dict] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()
        self._epoch = 0              # bumped (under the lock) whenever a run's outcome list is replaced
        self._events: list[dict] = []   # runs that ended here, oldest first (see ended / events)
        self._seq = 0

    def ended(self, name: str, status: str, **info) -> None:
        """Record that a run ended — the macOS app turns these into notifications."""
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "project": name, "status": status, "ts": _now(),
                                 **info})
            del self._events[:-200]

    def events(self, since: int = 0) -> dict:
        with self._lock:
            return {"seq": self._seq, "events": [e for e in self._events if e["seq"] > since]}

    def stop(self, name: str) -> None:
        self._stop.add(name)

    def mark(self, name: str, status: str) -> None:
        """Set the in-memory status (e.g. resolve a checkpoint to 'finished') so /live agrees with
        the DB without waiting for a restart."""
        with self._lock:
            if name in self._runs:
                self._runs[name]["status"] = status
                self._runs[name]["checkpoint"] = None

    def force_stop(self, name: str) -> bool:
        """Kill the live agent and abort the loop NOW, without waiting for the boundary."""
        with self._lock:
            r = self._runs.get(name)
            orch = r.get("orch") if r else None
            self._stop.add(name)
        if orch is None:
            return False
        orch.force_kill()          # outside the lock: SIGKILLs the agent's process group
        return True

    def shutdown(self) -> None:
        """The server is going away: kill every live agent. Agents run in their own process
        groups (for Force-Stop), so they would otherwise outlive the server and keep spending."""
        with self._lock:
            names = list(self._runs)
        for name in names:
            self.force_stop(name)

    def snapshot(self, name: str, since: int | None = None, epoch: int | None = None) -> dict | None:
        """The live view of a run. With ``since`` + the ``epoch`` the client last saw, only the
        outcomes after iteration ``since`` are returned (``delta``: true) — the UI polls every
        second, and resending the whole history (feedback + diffs) grows to megabytes per poll on
        a long run. Outcomes are append-only within an epoch; the epoch changes whenever the list
        is replaced wholesale (a new Run, or the history re-seeded on a re-baseline), and then the
        full list is sent so the client can't keep stale scores."""
        with self._lock:
            r = self._runs.get(name)
            if r is None:
                return None          # no in-memory run → caller falls back to idle
            ep = r.get("epoch", 0)
            delta = since is not None and epoch == ep
            outs = [o for o in r["outcomes"] if o["n"] > since] if delta else list(r["outcomes"])
            return {"status": r["status"], "summary": r["summary"],
                    "outcomes": outs, "delta": delta, "epoch": ep, "phase": r.get("phase"),
                    "baseline": dict(r.get("baseline") or {}), "cost": r.get("cost", 0.0),
                    "best": r.get("best"),   # scale-correct bar from the DB (not max over mixed scales)
                    "checkpoint": r.get("checkpoint")}

    def active(self) -> list[str]:
        """Projects whose loop runs in this server right now (agents may be spending)."""
        with self._lock:
            return sorted(n for n, r in self._runs.items() if r.get("status") == "running")

    def is_running(self, name: str) -> bool:
        with self._lock:
            r = self._runs.get(name)
            return bool(r and r["status"] == "running")

    def start_run(self, name: str) -> None:
        if cli_run_alive(project_dir(name)):
            raise HTTPException(409, "a command-line run of this project is in progress")
        with self._lock:
            if self._runs.get(name, {}).get("status") == "running":
                raise HTTPException(409, "run already in progress")
            self._stop.discard(name)   # clear inside the lock so a racing /stop isn't lost
            self._epoch += 1
            self._runs[name] = {"status": "running", "summary": None, "outcomes": [],
                                "epoch": self._epoch,
                                "phase": None, "baseline": {}, "cost": 0.0, "best": None,
                                "checkpoint": None}
        threading.Thread(target=self._run, args=(name,), daemon=True).start()

    def _run(self, name: str) -> None:
        state = None
        run_id = None
        cfg = None
        try:
            base = project_dir(name)
            cfg = load_config(base / "config.yaml")
            if cfg.mode == "symmetric":
                self._run_symmetric(name, cfg, base)
                return
            state = StateStore(base)
            # Run = CONTINUE: resume the latest run that has history (any finish reason), so previous
            # iterations are never lost and the iteration count carries on. A fresh start is a
            # separate explicit action (Reset / Fork), never a side effect of pressing Run.
            run_id, resuming = _select_run_id(state)
            if resuming:
                state.reconcile(run_id)
                if state.has_changes():
                    state.revert_uncommitted()
                state.set_status(run_id, "running")
            else:
                run_id = state.create_run(cfg.mode)
            log.info("run start: project=%s run_id=%s executor=%s", name, run_id,
                     cfg.agents["executor"].engine)
            ex = cfg.agents["executor"]
            executor = build_adapter(ex.engine, ex.model, "writeable")
            validator = None
            if "validator" in cfg.agents:
                va = cfg.agents["validator"]
                validator = build_adapter(va.engine, va.model, "read-only")
            orch = Orchestrator(cfg, state, run_id, executor,
                                get_metric_adapter(cfg.evaluation.adapter),
                                get_backend(cfg.sandbox.backend, cfg.sandbox), validator)
            with self._lock:
                self._runs[name]["orch"] = orch   # so Force-Stop can reach the live agent

            # If the objective changed since this run last ran, re-score the WHOLE history onto the
            # new scale BEFORE seeding the live view — so the chart/log/bar all match the new metrics
            # (no mixed-scale curve, no stale headline). Idempotent when nothing changed.
            orch._rebaseline_if_metrics_changed(lambda *_: None)
            hist = state.last_iterations(run_id, 100000)        # persisted history, now current-scale
            with self._lock:
                if name in self._runs:
                    self._epoch += 1                      # history replaced (re-scored) → full resend
                    self._runs[name]["epoch"] = self._epoch
                    self._runs[name]["outcomes"] = [
                        {"n": it.n, "verdict": it.verdict, "score": it.score,
                         "feedback": it.feedback, "change": it.change_summary, "ts": it.ts,
                         "metrics": [{"name": m["name"], "value": m["value"]} for m in it.metrics]}
                        for it in hist]
                    self._runs[name]["best"] = state.best_score(run_id)
                    self._runs[name]["baseline"] = dict(orch._baseline)

            def on_iter(o):
                with self._lock:
                    self._runs[name]["outcomes"].append(
                        {"n": o.n, "verdict": o.verdict, "score": o.score,
                         "feedback": o.feedback, "change": o.change, "ts": _now(),
                         "metrics": [{"name": m["name"], "value": m["value"]} for m in (o.metrics or [])]})
                    self._runs[name]["baseline"] = dict(orch._baseline)   # resolved zero-points (live cards)
                    self._runs[name]["cost"] = orch.cost_total            # estimated spend so far
                    self._runs[name]["best"] = state.best_score(run_id)   # current-scale bar (DB truth)

            def on_ph(p):
                with self._lock:
                    if name in self._runs:
                        self._runs[name]["phase"] = p

            summary = orch.run_loop(on_iteration=on_iter, on_phase=on_ph,
                                    should_stop=lambda: name in self._stop)
            st = {"stopped": "stopped", "rate_limited": "error", "agent_error": "error",
                  "checkpoint": "awaiting_review"}.get(summary.reason, "finished")
            cp_row = state.open_checkpoint(run_id) if summary.reason == "checkpoint" else None
            log.info("run end: project=%s status=%s reason=%s best=%s iters=%s cost=%.4f",
                     name, st, summary.reason, summary.best_score, summary.iterations, orch.cost_total)
            with self._lock:
                self._runs[name]["status"] = st
                self._runs[name]["summary"] = {"reason": summary.reason,
                    "best_score": summary.best_score, "iterations": summary.iterations}
                self._runs[name]["checkpoint"] = ({"reason": cp_row["reason"], "iter": cp_row["iter"]}
                                                  if cp_row else None)
            self.ended(name, st, reason=summary.reason, best_score=summary.best_score,
                       target=cfg.evaluation.target_score, iterations=summary.iterations,
                       cost=round(orch.cost_total, 4))
            _fire_webhook(cfg, name, {"project": name, "status": st, "reason": summary.reason,
                                      "best_score": summary.best_score,
                                      "iterations": summary.iterations})
        except Exception as e:  # surface failures to the UI rather than dying silently
            log.exception("run crashed: project=%s", name)
            with self._lock:
                self._runs[name]["status"] = "error"
                self._runs[name]["summary"] = {"error": str(e)}
            self.ended(name, "error", reason="crash", error=str(e)[:300])
            if state is not None and run_id is not None:
                try:
                    state.set_status(run_id, "error")   # no zombie 'running' in the db
                except Exception:
                    pass
            _fire_webhook(cfg, name, {"project": name, "status": "error", "error": str(e)})
        finally:
            if state is not None:
                state.close()                            # never leak the connection

    def _run_symmetric(self, name: str, cfg, base) -> None:
        """Symmetric (Rival↔Rival) runs use the SymmetricOrchestrator instead of the asymmetric
        iteration loop. The arena result (stop reason, deliverable champions, the two stable
        curves) is stored on the in-memory run so the status endpoint surfaces it."""
        try:
            import tyani_tolkai.arena.cegis  # noqa: F401  (registers cegis-recognizer)
            from ..arena.referee import get_referee
            from ..symmetric import SymmetricOrchestrator
            referee = get_referee(cfg.arena.referee)
            ex_a, ex_b = _symmetric_rivals(cfg)
            orch = SymmetricOrchestrator(cfg, base, referee, ex_a, ex_b,
                                         get_backend(cfg.sandbox.backend, cfg.sandbox))
            with self._lock:
                self._runs[name]["orch"] = orch          # so Stop / Force-Stop reach the live run
            try:
                result = orch.run(should_stop=lambda: name in self._stop)   # UI Stop between turns
            finally:
                orch.close()
            term = "stopped" if result.stop_reason == "stopped" else "finished"
            with self._lock:
                self._runs[name]["status"] = term
                self._runs[name]["summary"] = {
                    "mode": "symmetric", "reason": result.stop_reason,
                    "generations": result.generations, "best_a_id": result.best_a_id,
                    "best_b_id": result.best_b_id, "stable_a": result.stable_a,
                    "stable_b": result.stable_b}
            self.ended(name, term, reason=result.stop_reason, generations=result.generations)
            _fire_webhook(cfg, name, {"project": name, "status": term,
                                      "reason": result.stop_reason})
        except Exception as e:
            log.exception("symmetric run crashed: project=%s", name)
            with self._lock:
                self._runs[name]["status"] = "error"
                self._runs[name]["summary"] = {"error": str(e)}
            self.ended(name, "error", reason="crash", error=str(e)[:300])
            _mark_arena_manifest(base, "error")      # don't leave the manifest saying 'running'
            _fire_webhook(cfg, name, {"project": name, "status": "error", "error": str(e)})


def _symmetric_rivals(cfg):
    """Build the two rival executor adapters (claude/codex in prod). Module-level seam so tests
    inject deterministic scripted rivals.

    The ``scripted`` engine selects the deterministic, offline CEGIS rivals — this is what powers
    the zero-config "Co-Evolution Arena demo" so anyone can watch the arena converge in the
    dashboard without installing or paying for an LLM."""
    a = cfg.agents["rival_a"]
    b = cfg.agents["rival_b"]
    if "scripted" in (a.engine, b.engine):
        from ..agents.scripted_rival import ScriptedAdversaryRival, ScriptedRecognizerRival
        return (ScriptedRecognizerRival(), ScriptedAdversaryRival())
    return (build_adapter(a.engine, a.model, "writeable"),
            build_adapter(b.engine, b.model, "writeable"))


def _assert_scorer_exists(base: Path, cfg) -> None:
    """A project must ship a working SCORER from the start — the loop can't run without one. Verify
    the harness the eval references actually exists, so a project can NEVER be created non-runnable
    (e.g. a generated config naming a backtest.py that nobody wrote). Raises HTTPException(422)."""
    ev = cfg.evaluation
    artifact = base / "artifact"
    if ev.adapter in ("numeric", "command-exit"):
        # the scorer is the first *.py token in the command (the script; later .py are arg values).
        script = next((tk for tk in (ev.command or "").split() if tk.endswith(".py")), None)
        if script and not (artifact / script).resolve().exists():   # command cwd = artifact
            raise HTTPException(422,
                f"this project has no scorer: the eval command points to {script!r}, which does not "
                f"exist. A project must create everything it needs and run from the start — use "
                f"'Start from a template' (it ships a vetted scorer), or add the harness yourself.")
    elif ev.adapter == "pytest-pass":
        hd = base / (ev.harness_dir or "tests")
        if not hd.is_dir() or not any(hd.glob("*.py")):
            raise HTTPException(422,
                f"this project has no tests: harness_dir '{ev.harness_dir or 'tests'}' has no test "
                f"files. Use 'Start from a template', or add the hidden test suite.")


def _persisted_state(name: str) -> dict:
    base = project_dir(name)
    if not (base / "state.db").exists():
        return {"iterations": [], "best_score": None, "baseline": {}}
    state = StateStore(base)
    try:
        # prefer the latest run that actually has history (skip empty/zombie runs)
        run = state.conn.execute(
            "SELECT * FROM run WHERE id IN (SELECT DISTINCT run_id FROM iteration) "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            run = state.conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            return {"iterations": [], "best_score": None, "baseline": {}}
        iters = state.last_iterations(run["id"], 100000)   # oldest-first, with metrics
        baseline = {}
        if "baseline_json" in run.keys() and run["baseline_json"]:
            try:
                baseline = json.loads(run["baseline_json"])   # resolved metric zero-points
            except ValueError:
                baseline = {}
        cost = run["cost_total"] if "cost_total" in run.keys() else 0.0
        cp = state.open_checkpoint(run["id"])
        # NEAR-MISS PLATEAU diagnosis: a finished run stuck at plateau (not target / max_iter / error)
        # where a candidate actually beat the best but by < min_delta, so it was discarded as noise.
        # Surfaced so the UI can explain it and offer to lower min_delta instead of leaving the user
        # staring at an instant "finished plateau".
        plateau_hint = None
        try:
            cfg = load_config(base / "config.yaml")
            bn = run["best_score"]
            stuck = (run["status"] in ("finished", "plateau") and bn is not None
                     and bn < cfg.evaluation.target_score
                     and (run["iter_count"] or 0) < cfg.limits.max_iterations
                     and (run["plateau_count"] or 0) >= cfg.limits.plateau_N)
            if stuck:
                gain = state.max_gain_over_best(run["id"], bn)
                md = cfg.evaluation.min_delta
                if 0 < gain <= md + 1e-9:
                    plateau_hint = {"gain": round(gain, 4), "min_delta": md,
                                    "suggest_min_delta": max(0.01, round(min(md / 5, gain / 2), 4)),
                                    "plateau_n": cfg.limits.plateau_N,
                                    "plateau_count": run["plateau_count"] or 0}
        except Exception:
            plateau_hint = None
        return {
            "status": run["status"], "best_score": run["best_score"], "baseline": baseline,
            "cost": cost or 0.0,
            "checkpoint": ({"reason": cp["reason"], "iter": cp["iter"]} if cp else None),
            "plateau_hint": plateau_hint,
            "iterations": [{"n": it.n, "score": it.score, "verdict": it.verdict,
                            "change": it.change_summary, "feedback": it.feedback, "ts": it.ts,
                            "metrics": [{"name": m["name"], "value": m["value"]} for m in it.metrics]}
                           for it in iters],
        }
    finally:
        state.close()


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_TOOLS: dict = {}                         # tool → (checked_at, {path, version}) for diagnostics


def _tool_versions(names, refresh: bool = False) -> dict:
    """{tool: {path, version}} — `<tool> --version`, run in parallel, cached for 10 minutes."""
    import subprocess
    import time
    from concurrent.futures import ThreadPoolExecutor

    def probe(n: str) -> dict:
        path = shutil.which(n)
        if not path:
            return {"path": None, "version": None}
        try:
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=8,
                               stdin=subprocess.DEVNULL)
            first = (r.stdout or r.stderr).strip().splitlines()
            return {"path": path, "version": first[0][:120] if first else None}
        except (OSError, subprocess.TimeoutExpired):
            return {"path": path, "version": None}

    now = time.time()
    todo = [n for n in names if refresh or n not in _TOOLS or now - _TOOLS[n][0] > 600]
    with ThreadPoolExecutor(max_workers=max(1, len(todo))) as ex:
        for n, info in zip(todo, ex.map(probe, todo)):
            _TOOLS[n] = (now, info)
    return {n: _TOOLS[n][1] for n in names}


def _hostname(host: str) -> str:
    """'127.0.0.1:8765' → '127.0.0.1', '[::1]:8765' → '::1' (the Host header, lower-cased)."""
    h = host.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h[1:]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _cookie(request) -> str:
    """Per-port session cookie name (cookies ignore ports: two dashboards must not clash)."""
    return f"pp_token_{request.url.port or 80}"


def create_app(token: str | None = None, allowed_hosts: frozenset[str] | None = None) -> FastAPI:
    """``allowed_hosts``: Host header names to accept (None = any). A dashboard bound to loopback
    passes LOOPBACK_HOSTS, which blocks DNS rebinding (a web page whose domain resolves to
    127.0.0.1 would otherwise be same-origin with the API)."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # graceful shutdown (SIGTERM / SIGINT / the app's parent pipe). Cleanup lives here, not in
        # a `finally` around the server: uvicorn re-raises the signal once it has stopped.
        app.state.runs.shutdown()
        for hook in app.state.shutdown_hooks:
            hook()

    app = FastAPI(title="Pull-and-Push", lifespan=lifespan)
    app.state.token = token if token is not None else os.environ.get("TYANI_TOLKAI_WEB_PASSWORD")
    app.state.runs = RunManager()
    app.state.shutdown_hooks = []

    @app.middleware("http")
    async def same_origin_only(request, call_next):
        # The API runs agents that spend money and scorers that execute code. A browser sends
        # cross-site "simple" POSTs (no body, form or text/plain — e.g. /run) without asking,
        # so any web page could trigger them; it always sends an Origin header with them, though.
        # CLI clients (urllib, curl) send none and pass.
        host = request.headers.get("host", "")
        if allowed_hosts is not None and _hostname(host) not in allowed_hosts:
            return JSONResponse(status_code=403, content={"detail": f"host {host!r} refused"})
        origin = request.headers.get("origin")
        if (request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None
                and urlsplit(origin).netloc.lower() != host.strip().lower()):
            return JSONResponse(status_code=403,
                                content={"detail": f"cross-origin request from {origin!r} refused"})
        if app.state.token and request.url.path.startswith("/api/") and not _authorized(request):
            return JSONResponse(status_code=401, content={"detail": "bad or missing token"})
        return await call_next(request)

    def _authorized(request) -> bool:
        """The token as ?token= (CLI, first page load), the session cookie the first page load
        sets, or an Authorization: Bearer header."""
        bearer = request.headers.get("authorization", "")
        given = [request.query_params.get("token"), request.cookies.get(_cookie(request)),
                 bearer[7:] if bearer.lower().startswith("bearer ") else None]
        return any(g and hmac.compare_digest(g, str(app.state.token)) for g in given)

    # On startup, no background run can be alive yet — any DB run still marked 'running'
    # is an orphan from a previous process (e.g. the server was restarted mid-run). Heal it
    # so the UI doesn't show a zombie 'running' with no history.
    for _name in list_projects():
        _b = project_dir(_name)
        if cli_run_alive(_b):                   # a live CLI run owns it — not an orphan
            continue
        if (_b / "state.db").exists():
            _st = StateStore(_b)
            try:
                _st.conn.execute("UPDATE run SET status='stopped' WHERE status='running'")
                _st.conn.commit()
            finally:
                _st.close()
        _mark_arena_manifest(_b, "stopped")     # same orphan, symmetric flavour (arena.json)

    @app.exception_handler(ValueError)
    async def _value_error(request, exc):       # invalid project name etc → 400, not 500
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def _not_while_running(name: str) -> None:
        if app.state.runs.is_running(name) or cli_run_alive(project_dir(name)):
            raise HTTPException(409, "a run is in progress; stop it first")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")   # vendored libs + fonts

    @app.get("/")
    def index(request: Request):
        # the printed link carries ?token= once: trade it for an HttpOnly cookie and drop it from
        # the address bar — it then stays out of history, logs and bookmarks, and a bookmark of
        # the plain address keeps working (the terminal's token is stable across restarts)
        tok = request.query_params.get("token")
        if app.state.token and tok and hmac.compare_digest(tok, str(app.state.token)):
            r = RedirectResponse("/", status_code=303)
            r.set_cookie(_cookie(request), tok, max_age=365 * 86400, httponly=True,
                         samesite="strict", path="/")
            return r
        # never cache the SPA shell, so UI updates show up without a hard refresh
        return FileResponse(STATIC / "index.html",
                            headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/projects")
    def api_projects():
        import sqlite3
        items = []
        for n in list_projects():
            st = "idle"
            db = project_dir(n) / "state.db"
            if db.exists():
                try:
                    con = sqlite3.connect(str(db)); con.row_factory = sqlite3.Row
                    r = con.execute("SELECT status FROM run ORDER BY id DESC LIMIT 1").fetchone()
                    if r:
                        st = r["status"]
                    con.close()
                except Exception:
                    pass
            items.append({"name": n, "status": st})
        return {"projects": [i["name"] for i in items], "items": items}

    @app.get("/api/runs/active")
    def api_runs_active():
        """Runs this server is executing — the macOS app asks before Quit and badges its Dock icon."""
        return {"active": app.state.runs.active()}

    @app.get("/api/settings")
    def api_settings():
        from .. import settings as st
        return {"settings": st.load(), "scorer_python": st.python_info(st.scorer_python()),
                "env_override": os.environ.get(st.PYTHON_ENV),
                "candidates": [st.python_info(p) for p in st.python_candidates()]}

    @app.put("/api/settings")
    def api_put_settings(payload: dict = Body(...)):
        from .. import settings as st
        try:
            st.save(payload)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return api_settings()

    @app.get("/api/diagnostics")
    def api_diagnostics(refresh: bool = Query(False)):
        """Everything worth checking when something is off — versions, agent CLIs, git, the scorer
        Python, the data dir, update status, security. Shown in Settings; handy for an agent too."""
        import sys
        from .. import __version__
        from .. import settings as st
        rel = st.latest_release(force=refresh)
        tools = _tool_versions(("claude", "codex", "opencode", "agy", "git", "docker"), refresh)
        return {
            "version": __version__, "app": os.environ.get("PULL_AND_PUSH_APP"),
            "engine_python": {"version": sys.version.split()[0], "path": sys.executable},
            "scorer_python": {**st.python_info(st.scorer_python()),
                              "source": ("env" if os.environ.get(st.PYTHON_ENV) else
                                         "setting" if st.load()["scorer_python"] else "default")},
            "agents": {e: tools[e] for e in ("claude", "codex", "opencode", "agy")},
            "git": tools["git"], "docker": tools["docker"],
            "data_dir": str(home_root()),
            "update": ({"latest": rel.get("tag"), "url": rel.get("url"),
                        "newer": st.newer(rel.get("tag"), __version__)} if rel else None),
            "security": {"token": bool(app.state.token),
                         "loopback_only": allowed_hosts is not None},
        }

    @app.get("/api/runs/events")
    def api_runs_events(since: int = Query(0)):
        """Runs that ended in this server after event `since` (status, reason, best vs target,
        iterations, cost) — the app's notifications; also handy for an agent watching runs."""
        return app.state.runs.events(since)

    @app.get("/api/projects/{name}")
    def api_project(name: str):
        return _persisted_state(name)

    @app.get("/api/projects/{name}/arena")
    def api_arena(name: str):
        """Symmetric (Co-Evolution Arena) state from the arena.json manifest: status, the two
        stable curves, champions, deliverables, stop reason. The orchestrator rewrites it after
        every generation, so polling this gives live generation progress + the final result.
        Returns {mode, manifest}; manifest is null for an asymmetric or not-yet-run project."""
        base = project_dir(name)
        mpath = base / "arena.json"
        if mpath.exists():
            try:
                return {"mode": "symmetric", "manifest": json.loads(mpath.read_text(encoding="utf-8"))}
            except ValueError:
                return {"mode": "symmetric", "manifest": None}
        mode = None
        cfgp = base / "config.yaml"
        if cfgp.exists():
            try:
                import yaml
                mode = (yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}).get("mode")
            except Exception:
                mode = None
        return {"mode": mode, "manifest": None}

    @app.get("/api/projects/{name}/files")
    def api_files(name: str):
        art = project_dir(name) / "artifact"
        if not (art / ".git").exists():
            return {"files": []}
        import subprocess
        # -z: NUL-separated and unquoted, so names with spaces or non-ASCII letters survive
        out = subprocess.run(["git", "-C", str(art), "ls-files", "-z"],
                             capture_output=True).stdout.decode("utf-8", errors="replace")
        tracked = [f for f in out.split("\0") if f]
        files = []
        for f in tracked[:40]:
            if f == ".gitignore":
                continue
            p = art / f
            try:
                txt = p.read_text(encoding="utf-8")[:20000]
            except Exception:
                txt = "(binary or unreadable)"
            files.append({"path": f, "content": txt})
        return {"files": files}

    @app.get("/api/projects/{name}/live")
    def api_live(name: str, since: int | None = Query(None), epoch: int | None = Query(None)):
        snap = app.state.runs.snapshot(name, since=since, epoch=epoch)
        return snap or {"status": "idle", "outcomes": [], "summary": None}

    @app.get("/api/projects/{name}/agent-log")
    def api_agent_log(name: str):
        p = project_dir(name) / "agent.log"   # the executor's real output (tee'd live)
        if not p.exists():
            return {"log": ""}
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            txt = ""
        return {"log": txt[-40000:]}          # tail — enough to follow without flooding

    @app.post("/api/projects/{name}/run")
    def api_run(name: str):
        if not (project_dir(name) / "config.yaml").exists():
            raise HTTPException(404, "project has no config.yaml; create it via `tyani-tolkai run`")
        app.state.runs.start_run(name)
        return {"started": name}

    @app.post("/api/projects/{name}/checkpoint/continue")
    def api_cp_continue(name: str):
        """Operator chose to keep going at a human checkpoint: resolve it, give a fresh plateau
        budget, and resume the run. (For a 'target' checkpoint, raise target_score first via the
        config, otherwise it will pause again next boundary.)"""
        _not_while_running(name)          # before touching the DB, not only inside start_run
        base = project_dir(name)
        if not (base / "config.yaml").exists():
            raise HTTPException(404, "no such project")
        state = StateStore(base)
        try:
            last = state.conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()
            if last:
                state.resolve_checkpoint(last["id"], "continue")
                state.update_run(last["id"], plateau_count=0)   # fresh attempts before next plateau
        finally:
            state.close()
        app.state.runs.start_run(name)                          # resume (awaiting_review != finished)
        return {"continued": name}

    @app.post("/api/projects/{name}/checkpoint/accept")
    def api_cp_accept(name: str):
        """Operator accepted the result at a checkpoint: resolve it and finish the run."""
        base = project_dir(name)
        if not (base / "config.yaml").exists():
            raise HTTPException(404, "no such project")
        state = StateStore(base)
        try:
            last = state.conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()
            if last:
                state.resolve_checkpoint(last["id"], "accept")
                state.set_status(last["id"], "finished")
        finally:
            state.close()
        app.state.runs.mark(name, "finished")
        return {"accepted": name}

    @app.post("/api/projects/{name}/command")
    def api_command(name: str, text: str = Query(...), role: str = Query("executor")):
        if role not in ("executor", "validator"):
            raise HTTPException(400, "role must be executor or validator")
        ctx = project_dir(name) / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        with (ctx / f"{role}.md").open("a", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        return {"ok": True}

    # ---- server-side directory browser (for the seed-path picker) ----
    @app.get("/api/fs")
    def api_fs(path: str | None = Query(None)):
        """List a directory on the server so the UI can offer a file-manager-style
        picker (the browser sandbox can't hand us a real server path otherwise).
        Read-only: lists names, never file contents. Localhost tool — the user owns
        the machine; we just hide dotfiles and fail soft on unreadable dirs."""
        base = Path(path).expanduser() if path else Path.home()
        try:
            base = base.resolve()
        except Exception:
            base = Path.home()
        if not base.is_dir():
            base = base.parent if base.parent.is_dir() else Path.home()
        entries = []
        try:
            for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith("."):
                    continue
                entries.append({"name": p.name, "path": str(p), "dir": p.is_dir()})
        except (PermissionError, OSError):
            pass
        parent = str(base.parent) if base.parent != base else None
        return {"path": str(base), "parent": parent, "entries": entries}

    # ---- meta for the config form ----
    @app.get("/api/meta")
    def api_meta():
        import tyani_tolkai.arena.cegis  # noqa: F401  (registers the cegis referee)
        from ..arena.referee import list_referees
        from .. import __version__
        from ..settings import cached_release, newer
        engines = ["claude", "codex", "opencode", "agy"]
        rel = cached_release()
        return {
            "version": __version__,
            "app": os.environ.get("PULL_AND_PUSH_APP"),   # set inside the macOS app (Sparkle updates it)
            "update": ({"latest": rel.get("tag"), "url": rel.get("url")}
                       if rel and newer(rel.get("tag"), __version__) else None),
            "engines": engines,
            # on PATH? An app started from Finder sees only the PATH it was given — tell the UI
            "installed": {e: shutil.which(e) is not None for e in engines},
            "home": str(home_root()),              # the app's "Show in Finder" builds paths from it
            "adapters": ["numeric", "command-exit", "pytest-pass"],
            "seeds": ["empty", "copy"],
            "modes": ["asymmetric", "symmetric"],
            "referees": list_referees(),
            "dirs": ["higher", "lower"],
        }

    # ---- research + scaffold phase: vetted templates → runnable project ----
    @app.get("/api/templates")
    def api_templates():
        from ..scaffold import list_templates
        return {"templates": list_templates()}

    @app.post("/api/scaffold")
    def api_scaffold(payload: dict = Body(...)):
        """Build a ready-to-run project from a template (the scoring harness ships vetted,
        never generated). Description becomes the executor goal; if no template is given it
        is inferred from the description."""
        from ..scaffold import pick_template, scaffold_project
        name = (payload.get("project") or "").strip()
        desc = (payload.get("description") or "").strip()
        tid = (payload.get("template_id") or "").strip() or None
        if not name:
            raise HTTPException(400, "project name required")
        if not desc:
            raise HTTPException(400, "description required")
        if tid is None:
            tid = pick_template(desc)
            if tid is None:
                raise HTTPException(422, "could not infer a template from the description; "
                                         "pick one explicitly")
        try:
            return scaffold_project(name, desc, tid)
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(422, str(e))

    # ---- configurator agent: description → draft config ----
    @app.post("/api/configure")
    def api_configure(payload: dict = Body(...)):
        desc = (payload.get("description") or "").strip()
        if not desc:
            raise HTTPException(400, "description required")
        from ..configurator import generate_config
        try:
            cfg = generate_config(desc, payload.get("engine", "claude"), payload.get("model"))
        except Exception as e:
            raise HTTPException(502, f"configurator failed: {e}")
        valid, err = True, None
        try:
            Config(**cfg)
        except Exception as e:
            valid, err = False, str(e)
        return {"config": cfg, "valid": valid, "error": err}

    # ---- project create / configure ----
    @app.post("/api/projects/create")
    def api_create(payload: dict = Body(...)):
        name = payload.get("project")
        if not name:
            raise HTTPException(400, "project name required")
        base = project_dir(name)
        if (base / "config.yaml").exists():
            raise HTTPException(409, f"project {name!r} already exists")
        try:
            cfg = Config(**payload)               # validate before creating anything
        except Exception as e:
            raise HTTPException(422, f"invalid config: {e}")
        state = StateStore(base)
        if cfg.seed.mode == "copy" and cfg.seed.path:
            src = Path(cfg.seed.path).expanduser()
            if src.exists():
                from ..scaffold import USER_CODE_IGNORE
                shutil.copytree(src, state.artifact_dir, dirs_exist_ok=True, ignore=USER_CODE_IGNORE)
        state.git_init()
        (base / "config.yaml").write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
        state.close()
        try:
            _assert_scorer_exists(base, cfg)      # a project MUST be runnable from the start
        except HTTPException:
            delete_project(name)                  # roll back the half-created, non-runnable project
            raise
        return {"created": name}

    # ---- onboarding an existing bot: expose the P4.5/P4.6 backend (adapter + onboard) ----
    @app.post("/api/gen-adapter")
    def api_gen_adapter(payload: dict = Body(...)):
        """Scaffold adapter.py (vetted protocol plumbing + a decide() stub) into the bot dir."""
        from ..adapter_gen import render_adapter_stub
        from ..profile_schema import BotProfile
        bot_dir = Path((payload.get("bot_dir") or "").strip()).expanduser()
        if not bot_dir.is_dir():
            raise HTTPException(422, f"bot-dir not found: {bot_dir}")
        profile = None
        prof = payload.get("profile")
        if prof:
            try:
                profile = (BotProfile.model_validate(prof) if isinstance(prof, dict)
                           else BotProfile.model_validate_json(Path(prof).read_text(encoding="utf-8")))
            except Exception as e:
                raise HTTPException(422, f"invalid profile: {e}")
        out = Path(payload["out"]).expanduser() if payload.get("out") else bot_dir / "adapter.py"
        if out.exists() and not payload.get("force"):
            raise HTTPException(409, f"{out} already exists; pass force=true to overwrite")
        out.write_text(render_adapter_stub(profile=profile), encoding="utf-8")
        return {"wrote": str(out)}

    @app.post("/api/check-adapter")
    def api_check_adapter(payload: dict = Body(...)):
        """Trust gate: drive the adapter over the protocol on synthetic bars → verdict PASS/FLAG.

        Proves protocol soundness + determinism + non-degeneracy — NOT semantics (sign/scale);
        run `validate` for that. Returns the check_adapter_orders verdict dict (200 even on FLAG;
        the verdict's ``ok`` carries PASS/FLAG so the UI can render either).
        """
        import shlex
        from ..bot_runner import drive_bot, BotProtocolError
        from ..validation import synth_bars, check_adapter_orders
        bot_dir = Path((payload.get("bot_dir") or "").strip()).expanduser()
        if not bot_dir.is_dir():
            raise HTTPException(422, f"bot-dir not found: {bot_dir}")
        bot_cmd = (payload.get("bot_cmd") or "").strip()
        if not bot_cmd:
            raise HTTPException(422, "bot_cmd required")
        bars = synth_bars(int(payload.get("n") or 40))
        from ..bot_runner import split_command
        cmd = split_command(bot_cmd)            # Windows paths keep their backslashes
        prt = float(payload.get("per_read_timeout") or 10.0)
        tt = float(payload.get("total_timeout") or 120.0)
        try:
            run1 = drive_bot(cmd, bars, params={}, per_read_timeout=prt, total_timeout=tt,
                             cwd=str(bot_dir))       # relative `python adapter.py` resolves
            run2 = drive_bot(cmd, bars, params={}, per_read_timeout=prt, total_timeout=tt,
                             cwd=str(bot_dir))
        except BotProtocolError as e:
            return {"ok": False, "well_formed": False, "deterministic": False, "degenerate": False,
                    "reasons": [f"adapter failed the protocol: {e}"]}
        return check_adapter_orders(run1, run2, len(bars))

    @app.post("/api/onboard")
    def api_onboard(payload: dict = Body(...)):
        """Create a runnable optimization project from a bot + its data + a P2 proposal."""
        from ..scaffold import scaffold_onboarding
        from ..proposal_schema import MetricProposal
        name = (payload.get("project") or payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "project name required")
        bot_dir = Path((payload.get("bot_dir") or "").strip()).expanduser()
        data = Path((payload.get("data") or "").strip()).expanduser()
        bot_cmd = (payload.get("bot_cmd") or "").strip()
        if not bot_dir.is_dir():
            raise HTTPException(422, f"bot-dir not found: {bot_dir}")
        if not data.is_file():
            raise HTTPException(422, f"data not found: {data}")
        if not bot_cmd:
            raise HTTPException(422, "bot_cmd required")
        prop = payload.get("proposal")
        try:
            proposal = (MetricProposal.model_validate(prop) if isinstance(prop, dict)
                        else MetricProposal.model_validate_json(Path(prop).read_text(encoding="utf-8")))
        except Exception as e:
            raise HTTPException(422, f"invalid proposal: {e}")
        try:
            return scaffold_onboarding(name, bot_dir=bot_dir, data_path=data, proposal=proposal,
                                       bot_cmd=bot_cmd, seed_token=payload.get("seed"),
                                       goal=payload.get("goal"))
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/profile")
    def api_profile(payload: dict = Body(...)):
        """Analyze an existing bot (read-only, LLM) → BotProfile (json + markdown). Synchronous,
        like /api/configure; the UI shows a spinner while the analyzer runs."""
        from ..profiler import analyze_bot, render_markdown, ProfileError
        src = Path((payload.get("bot_dir") or payload.get("path") or "").strip()).expanduser()
        if not src.exists():
            raise HTTPException(422, f"bot path not found: {src}")
        try:
            profile = analyze_bot(src, engine=payload.get("engine", "claude"),
                                  model=payload.get("model"), timeout=int(payload.get("timeout") or 180))
        except ProfileError as e:
            raise HTTPException(502, f"analysis failed: {e}")
        return {"profile": profile.model_dump(), "markdown": render_markdown(profile)}

    @app.post("/api/propose")
    def api_propose(payload: dict = Body(...)):
        """Propose evaluation metrics + tunable ranges from a profile + goal (LLM). Synchronous.

        The human reviews/edits the proposal before it becomes the onboarding config (ADR gate)."""
        from ..proposer import propose_evaluation, render_markdown as render_proposal_md, ProposalError
        from ..profile_schema import BotProfile
        prof = payload.get("profile")
        if not prof:
            raise HTTPException(400, "profile required")
        goal = (payload.get("goal") or "").strip()
        if not goal:
            raise HTTPException(400, "goal required")
        try:
            profile = (BotProfile.model_validate(prof) if isinstance(prof, dict)
                       else BotProfile.model_validate_json(Path(prof).read_text(encoding="utf-8")))
        except Exception as e:
            raise HTTPException(422, f"invalid profile: {e}")
        try:
            proposal = propose_evaluation(profile, goal, engine=payload.get("engine", "claude"),
                                          model=payload.get("model"), timeout=int(payload.get("timeout") or 180))
        except ProposalError as e:
            raise HTTPException(502, f"proposal failed: {e}")
        return {"proposal": proposal.model_dump(), "markdown": render_proposal_md(proposal)}

    @app.post("/api/validate")
    def api_validate(payload: dict = Body(...)):
        """P4 secondary validation of a bot → evidence report (PASS/FLAG). Synchronous.

        Untrusted bots need Docker (isolation = trust); trusted=true uses a process-separation
        subprocess (usable without Docker, for a reference/own bot)."""
        import shlex
        from .. import bot_engine
        from ..bot_io import load_bars_csv
        from ..bot_runner import score_bot, BotProtocolError
        from ..bot_sandbox import score_bot_sandboxed, SandboxUnavailable, docker_available
        from ..validation import run_secondary_validation
        bot_dir = Path((payload.get("bot_dir") or "").strip()).expanduser()
        data = Path((payload.get("data") or "").strip()).expanduser()
        bot_cmd_s = (payload.get("bot_cmd") or "").strip()
        if not bot_dir.is_dir():
            raise HTTPException(422, f"bot-dir not found: {bot_dir}")
        if not data.is_file():
            raise HTTPException(422, f"data not found: {data}")
        if not bot_cmd_s:
            raise HTTPException(422, "bot_cmd required")
        params = payload.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params) if params else {}
            except json.JSONDecodeError as e:
                raise HTTPException(422, f"invalid params JSON: {e}")
        seed = payload.get("seed") or bot_dir.name
        bars = load_bars_csv(data)
        from ..bot_runner import split_command
        bot_cmd = split_command(bot_cmd_s)
        prt = float(payload.get("per_read_timeout") or 10.0)
        tt = float(payload.get("total_timeout") or 120.0)
        if payload.get("trusted"):
            isolation = "subprocess (process-separation only; trusted asserted by operator)"
            def score_bars(b):
                return score_bot(bot_cmd, b, params=params, seed=seed, per_read_timeout=prt,
                                 total_timeout=tt, cwd=str(bot_dir))
        elif docker_available():
            isolation = "docker-sandbox"
            def score_bars(b):
                return score_bot_sandboxed(bot_cmd, b, bot_dir=str(bot_dir), seed=seed, params=params,
                                           per_read_timeout=prt, total_timeout=tt)
        else:
            raise HTTPException(409, "validating an untrusted bot requires Docker; start Docker or "
                                     "pass trusted=true only if you fully trust this bot")
        try:
            result = run_secondary_validation(
                bars=bars, score_bars=score_bars, isolation=isolation, seed=seed, params=params,
                name=(payload.get("name") or bot_dir.name), data_path=data,
                engine_path=bot_engine.__file__, config_extra={"bot_cmd": bot_cmd})
        except (SandboxUnavailable, BotProtocolError) as e:
            raise HTTPException(502, f"scoring failed: {e}")
        return {"report": result["report"], "verdict": result["verdict"], "reasons": result["reasons"],
                "beats": result["beats"], "determinism_ok": result["determinism_ok"],
                "gap": result["gap"], "anti_lookahead": result["anti_lookahead"],
                "isolation": result["isolation"]}

    # ---- research kits (P8): an idea → the wizard → a kit → a runnable project ----
    def _research_payload(payload: dict):
        from ..research import kits_root
        from ..research_agent import write_kit
        name = valid_name((payload.get("name") or "").strip())
        try:
            kit = write_kit(kits_root() / name, payload.get("research_yaml") or "",
                            payload.get("files") or {})
        except (ValueError, yaml.YAMLError) as e:
            raise HTTPException(422, f"invalid kit: {e}")
        return name, kit

    @app.post("/api/research/clarify")
    def api_research_clarify(payload: dict = Body(...)):
        """Idea → clarifying questions + a draft of the criteria (helper agent, read-only)."""
        from ..research_agent import clarify
        try:
            return clarify(payload.get("idea") or "", engine=payload.get("engine") or "claude",
                           model=payload.get("model"))
        except ValueError as e:
            raise HTTPException(422, f"the helper agent gave no usable answer: {e}")
        except Exception as e:                            # agent missing / crashed / timed out
            raise HTTPException(502, f"helper agent failed: {e}")

    @app.post("/api/research/draft")
    def api_research_draft(payload: dict = Body(...)):
        """Idea + criteria + answers → a complete kit draft (spec + scorer + seed) to review."""
        from ..research_agent import draft_kit
        try:
            return draft_kit((payload.get("name") or "").strip(), payload.get("idea") or "",
                             criteria=payload.get("criteria"), answers=payload.get("answers") or "",
                             engine=payload.get("engine") or "claude", model=payload.get("model"))
        except ValueError as e:
            raise HTTPException(422, f"the draft is not a usable kit: {e}")
        except Exception as e:
            raise HTTPException(502, f"helper agent failed: {e}")

    @app.post("/api/research/check")
    def api_research_check(payload: dict = Body(...)):
        """Save the (edited) kit and run the pre-flight: the judge is run on the seed twice."""
        from ..research import check_kit
        _, kit = _research_payload(payload)
        return check_kit(kit)

    @app.post("/api/research/create")
    def api_research_create(payload: dict = Body(...)):
        """Save the kit (reusable later) and create the project from it — pre-flight must pass."""
        from ..research import create_from_kit
        name, kit = _research_payload(payload)
        try:
            return create_from_kit(kit, name=name)
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/research/kits")
    def api_research_kits():
        """Saved kits (made by the wizard or copied in) — your own reusable templates."""
        from ..research import SPEC_FILE, kits_root
        out = []
        root = kits_root()
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            f = d / SPEC_FILE
            if d.is_dir() and f.is_file():
                try:
                    spec = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError:
                    continue
                out.append({"name": d.name, "goal": str(spec.get("goal") or "")[:200]})
        return {"kits": out}

    @app.get("/api/research/kits/{name}")
    def api_research_kit(name: str):
        from ..research import kits_root
        from ..research_agent import read_kit
        kit = kits_root() / valid_name(name)
        if not kit.is_dir():
            raise HTTPException(404, f"no such kit: {name}")
        return read_kit(kit)

    @app.get("/api/projects/{name}/config")
    def api_get_config(name: str):
        p = project_dir(name) / "config.yaml"
        if not p.exists():
            raise HTTPException(404, "no config for this project")
        return yaml.safe_load(p.read_text(encoding="utf-8"))

    @app.put("/api/projects/{name}/config")
    def api_put_config(name: str, payload: dict = Body(...)):
        _not_while_running(name)
        base = project_dir(name)
        if not (base / "config.yaml").exists():
            raise HTTPException(404, "no such project")
        try:
            Config(**payload)
        except Exception as e:
            raise HTTPException(422, f"invalid config: {e}")
        (base / "config.yaml").write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return {"updated": name}

    # ---- run control & lifecycle ----
    @app.post("/api/projects/{name}/test-eval")
    def api_test_eval(name: str):
        """Run the evaluation ONCE on the current artifact — verify the harness works and
        see the metrics or the raw stdout/stderr, without a full run (beats cold-start)."""
        # during a run the executor is editing this very working tree: scoring it now would grade a
        # half-written candidate, and any file the scorer drops would be committed as the agent's
        _not_while_running(name)
        base = project_dir(name)
        if not (base / "config.yaml").exists():
            raise HTTPException(404, "no such project")
        cfg = load_config(base / "config.yaml")
        state = StateStore(base)
        was_clean = False
        try:
            was_clean = (state.artifact_dir / ".git").exists() and not state.has_changes()
        except Exception:
            was_clean = False
        try:
            # apply any already-resolved zero-points (worst) from the latest run
            run = state.conn.execute("SELECT baseline_json FROM run "
                                     "WHERE baseline_json IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            saved = {}
            if run and run["baseline_json"]:
                try:
                    saved = json.loads(run["baseline_json"])
                except ValueError:
                    saved = {}
            for m in cfg.evaluation.metrics:
                if m.worst is None and m.name in saved:
                    m.worst = saved[m.name]

            adapter = get_metric_adapter(cfg.evaluation.adapter)
            sandbox = get_backend(cfg.sandbox.backend, cfg.sandbox)
            res = adapter.run(state.artifact_dir, sandbox, cfg.evaluation, cfg.limits.step_seconds)
            out = {"ok": bool(res.ok), "logs": (res.logs or "")[:4000],
                   "adapter": cfg.evaluation.adapter, "command": cfg.evaluation.command,
                   "metrics": [{"name": m["name"], "value": m["value"]} for m in res.metrics]}
            preview = False
            if res.ok:
                from ..scorer import resolve_worst, score
                values = {m["name"]: m["value"] for m in res.metrics}
                # preview any unpinned zero-point the same way the orchestrator does (with the
                # already-at-goal guard) so test-eval scores identically to a real run.
                for m in cfg.evaluation.metrics:
                    if m.worst is None and m.name in values:
                        m.worst = resolve_worst(m.dir, m.target, values[m.name])
                        preview = True
                out["preview_baseline"] = preview
                out["metric_specs"] = [{"name": m.name, "dir": m.dir, "weight": m.weight,
                                        "worst": m.worst, "target": m.target}
                                       for m in cfg.evaluation.metrics]
                # report-only fields the harness printed beyond the scored metrics (e.g. win
                # rate, profit factor, trade count, tested period) — shown but not scored. Read
                # the adapter's parsed `data` (robust to stderr noise in the logs).
                scored = {m.name for m in cfg.evaluation.metrics}
                out["extras"] = {k: v for k, v in (getattr(res, "data", None) or {}).items()
                                 if k not in scored}
                from ..scorer import constraint_violations
                out["violations"] = constraint_violations(
                    {**(getattr(res, "data", None) or {}), **values}, cfg.evaluation.constraints)
                try:
                    out["score"] = score(values, cfg.evaluation.metrics)
                except Exception as e:
                    out["score"] = None
                    out["logs"] = f"metrics ran but scoring failed: {e}\n" + out["logs"]
            return out
        except Exception as e:
            return {"ok": False, "logs": f"could not run evaluation: {e}", "metrics": []}
        finally:
            if was_clean:
                try:                                  # drop scoring side-effects, like the loop does,
                    state.revert_uncommitted()        # so they can't leak into the next candidate
                except Exception:
                    log.warning("test-eval: could not revert scorer side-effects: project=%s", name)
            state.close()

    @app.post("/api/projects/{name}/stop")
    def api_stop(name: str):
        base = project_dir(name)
        if not app.state.runs.is_running(name) and cli_run_alive(base):
            from ..projects import STOP_REQUEST
            (base / STOP_REQUEST).write_text("stop", encoding="utf-8")   # the CLI loop polls it
            return {"stopping": name, "cli": True}
        app.state.runs.stop(name)
        return {"stopping": name}

    @app.post("/api/projects/{name}/force-stop")
    def api_force_stop(name: str):
        if not app.state.runs.is_running(name) and cli_run_alive(project_dir(name)):
            raise HTTPException(409, "this run was started from the command line: use Stop (it ends "
                                     "after the current iteration) or Ctrl-C in its terminal")
        killed = app.state.runs.force_stop(name)   # SIGKILL the live agent immediately
        return {"force_stopped": name, "killed": killed}

    @app.post("/api/projects/{name}/delete")
    def api_delete(name: str):
        _not_while_running(name)
        delete_project(name)
        return {"deleted": name}

    @app.post("/api/projects/{name}/rename")
    def api_rename(name: str, to: str = Query(...)):
        _not_while_running(name)
        try:
            rename_project(name, to)
        except FileNotFoundError:
            raise HTTPException(404, f"no such project: {name}")
        except FileExistsError:
            raise HTTPException(409, f"target name already exists: {to}")
        return {"renamed": to}

    @app.post("/api/projects/{name}/reset")
    def api_reset(name: str):
        _not_while_running(name)
        if not project_dir(name).exists():
            raise HTTPException(404, f"no such project: {name}")
        reset_project(name)
        return {"reset": name}

    @app.get("/api/projects/{name}/export")
    def api_export(name: str, n: int | None = Query(None, alias="iter")):
        if not project_dir(name).exists():                   # don't let StateStore mkdir an orphan
            raise HTTPException(404, f"no such project: {name}")
        at_hash = None
        if n is not None:                                    # snapshot a specific kept iteration
            state = StateStore(project_dir(name))
            try:
                run_id = state.latest_run_id()
                at_hash = state.iteration_hash(run_id, n) if run_id is not None else None
            finally:
                state.close()
            if not at_hash:
                raise HTTPException(404, f"iteration {n} is not a restorable (kept) iteration")
        tmp = Path(tempfile.mkdtemp())                       # unique dir per request
        dest = tmp / f"{name}.zip"
        try:
            export_project(name, dest, at_hash=at_hash, at_label=n)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        fn = f"{name}.zip" if n is None else f"{name}-iter{n}.zip"
        return FileResponse(dest, filename=fn, media_type="application/zip",   # removed once sent
                            background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))

    @app.post("/api/projects/{name}/rewind")
    def api_rewind(name: str, n: int = Query(..., alias="iter")):
        _not_while_running(name)
        if not project_dir(name).exists():
            raise HTTPException(404, f"no such project: {name}")
        state = StateStore(project_dir(name))
        try:
            run_id = state.latest_run_id()
            if run_id is None:
                raise HTTPException(404, "no run history to rewind")
            try:
                state.rewind_to(run_id, n)
            except ValueError as e:
                raise HTTPException(422, str(e))
        finally:
            state.close()
        return {"rewound": n}

    @app.post("/api/projects/{name}/fork")
    def api_fork(name: str, n: int = Query(..., alias="iter"), to: str = Query(...)):
        _not_while_running(name)
        try:
            valid_name(to)
        except ValueError:
            raise HTTPException(400, f"invalid project name: {to!r}")
        try:
            fork_project(name, to, n)
        except FileNotFoundError:
            raise HTTPException(404, f"no such project: {name}")
        except FileExistsError:
            raise HTTPException(409, f"target name already exists: {to}")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"forked": to}

    return app
