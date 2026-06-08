"""FastAPI backend for the Pull-and-Push dashboard.

Live updates use polling (GET /live), not WebSocket — simpler and robust across the
run thread boundary while giving the same live evolution chart. A background thread
runs the orchestrator; the UI polls its outcomes. Optional token auth (from the
TYANI_TOLKAI_WEB_PASSWORD env) protects the API when set.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("pull_and_push")   # operational events; configured by the CLI's basicConfig

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from ..config import Config, load_config
from ..metrics import get_metric_adapter
from ..orchestrator import Orchestrator
from ..projects import (
    delete_project, export_project, fork_project, list_projects, project_dir,
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

    def snapshot(self, name: str) -> dict | None:
        with self._lock:
            r = self._runs.get(name)
            if r is None:
                return None          # no in-memory run → caller falls back to idle
            return {"status": r["status"], "summary": r["summary"],
                    "outcomes": list(r["outcomes"]), "phase": r.get("phase"),
                    "baseline": dict(r.get("baseline") or {}), "cost": r.get("cost", 0.0),
                    "best": r.get("best"),   # scale-correct bar from the DB (not max over mixed scales)
                    "checkpoint": r.get("checkpoint")}

    def is_running(self, name: str) -> bool:
        with self._lock:
            r = self._runs.get(name)
            return bool(r and r["status"] == "running")

    def start_run(self, name: str) -> None:
        with self._lock:
            if self._runs.get(name, {}).get("status") == "running":
                raise HTTPException(409, "run already in progress")
            self._stop.discard(name)   # clear inside the lock so a racing /stop isn't lost
            self._runs[name] = {"status": "running", "summary": None, "outcomes": [],
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
            _fire_webhook(cfg, name, {"project": name, "status": st, "reason": summary.reason,
                                      "best_score": summary.best_score,
                                      "iterations": summary.iterations})
        except Exception as e:  # surface failures to the UI rather than dying silently
            log.exception("run crashed: project=%s", name)
            with self._lock:
                self._runs[name]["status"] = "error"
                self._runs[name]["summary"] = {"error": str(e)}
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
            result = orch.run()
            with self._lock:
                self._runs[name]["status"] = "finished"
                self._runs[name]["summary"] = {
                    "mode": "symmetric", "reason": result.stop_reason,
                    "generations": result.generations, "best_a_id": result.best_a_id,
                    "best_b_id": result.best_b_id, "stable_a": result.stable_a,
                    "stable_b": result.stable_b}
            _fire_webhook(cfg, name, {"project": name, "status": "finished",
                                      "reason": result.stop_reason})
        except Exception as e:
            log.exception("symmetric run crashed: project=%s", name)
            with self._lock:
                self._runs[name]["status"] = "error"
                self._runs[name]["summary"] = {"error": str(e)}
            _fire_webhook(cfg, name, {"project": name, "status": "error", "error": str(e)})


def _symmetric_rivals(cfg):
    """Build the two rival executor adapters (claude/codex in prod). Module-level seam so tests
    inject deterministic scripted rivals."""
    a = cfg.agents["rival_a"]
    b = cfg.agents["rival_b"]
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
        return {
            "status": run["status"], "best_score": run["best_score"], "baseline": baseline,
            "cost": cost or 0.0,
            "checkpoint": ({"reason": cp["reason"], "iter": cp["iter"]} if cp else None),
            "iterations": [{"n": it.n, "score": it.score, "verdict": it.verdict,
                            "change": it.change_summary, "feedback": it.feedback, "ts": it.ts,
                            "metrics": [{"name": m["name"], "value": m["value"]} for m in it.metrics]}
                           for it in iters],
        }
    finally:
        state.close()


def create_app(token: str | None = None) -> FastAPI:
    app = FastAPI(title="Pull-and-Push")
    app.state.token = token if token is not None else os.environ.get("TYANI_TOLKAI_WEB_PASSWORD")
    app.state.runs = RunManager()

    # On startup, no background run can be alive yet — any DB run still marked 'running'
    # is an orphan from a previous process (e.g. the server was restarted mid-run). Heal it
    # so the UI doesn't show a zombie 'running' with no history.
    for _name in list_projects():
        _b = project_dir(_name)
        if (_b / "state.db").exists():
            _st = StateStore(_b)
            try:
                _st.conn.execute("UPDATE run SET status='stopped' WHERE status='running'")
                _st.conn.commit()
            finally:
                _st.close()

    @app.exception_handler(ValueError)
    async def _value_error(request, exc):       # invalid project name etc → 400, not 500
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def auth(t: str | None) -> None:
        if app.state.token and t != app.state.token:
            raise HTTPException(401, "bad or missing token")

    def _not_while_running(name: str) -> None:
        if app.state.runs.is_running(name):
            raise HTTPException(409, "a run is in progress; stop it first")

    @app.get("/")
    def index():
        # never cache the SPA shell, so UI updates show up without a hard refresh
        return FileResponse(STATIC / "index.html",
                            headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/projects")
    def api_projects(token: str | None = Query(None)):
        auth(token)
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

    @app.get("/api/projects/{name}")
    def api_project(name: str, token: str | None = Query(None)):
        auth(token)
        return _persisted_state(name)

    @app.get("/api/projects/{name}/arena")
    def api_arena(name: str, token: str | None = Query(None)):
        """Symmetric (Co-Evolution Arena) state from the arena.json manifest: status, the two
        stable curves, champions, deliverables, stop reason. The orchestrator rewrites it after
        every generation, so polling this gives live generation progress + the final result.
        Returns {mode, manifest}; manifest is null for an asymmetric or not-yet-run project."""
        auth(token)
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
    def api_files(name: str, token: str | None = Query(None)):
        auth(token)
        art = project_dir(name) / "artifact"
        if not (art / ".git").exists():
            return {"files": []}
        import subprocess
        tracked = subprocess.run(["git", "-C", str(art), "ls-files"],
                                 capture_output=True, text=True).stdout.split()
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
    def api_live(name: str, token: str | None = Query(None)):
        auth(token)
        snap = app.state.runs.snapshot(name)
        return snap or {"status": "idle", "outcomes": [], "summary": None}

    @app.get("/api/projects/{name}/agent-log")
    def api_agent_log(name: str, token: str | None = Query(None)):
        auth(token)
        p = project_dir(name) / "agent.log"   # the executor's real output (tee'd live)
        if not p.exists():
            return {"log": ""}
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            txt = ""
        return {"log": txt[-40000:]}          # tail — enough to follow without flooding

    @app.post("/api/projects/{name}/run")
    def api_run(name: str, token: str | None = Query(None)):
        auth(token)
        if not (project_dir(name) / "config.yaml").exists():
            raise HTTPException(404, "project has no config.yaml; create it via `tyani-tolkai run`")
        app.state.runs.start_run(name)
        return {"started": name}

    @app.post("/api/projects/{name}/checkpoint/continue")
    def api_cp_continue(name: str, token: str | None = Query(None)):
        """Operator chose to keep going at a human checkpoint: resolve it, give a fresh plateau
        budget, and resume the run. (For a 'target' checkpoint, raise target_score first via the
        config, otherwise it will pause again next boundary.)"""
        auth(token)
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
    def api_cp_accept(name: str, token: str | None = Query(None)):
        """Operator accepted the result at a checkpoint: resolve it and finish the run."""
        auth(token)
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
    def api_command(name: str, text: str = Query(...), role: str = Query("executor"),
                    token: str | None = Query(None)):
        auth(token)
        if role not in ("executor", "validator"):
            raise HTTPException(400, "role must be executor or validator")
        ctx = project_dir(name) / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        with (ctx / f"{role}.md").open("a", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        return {"ok": True}

    # ---- server-side directory browser (for the seed-path picker) ----
    @app.get("/api/fs")
    def api_fs(path: str | None = Query(None), token: str | None = Query(None)):
        """List a directory on the server so the UI can offer a file-manager-style
        picker (the browser sandbox can't hand us a real server path otherwise).
        Read-only: lists names, never file contents. Localhost tool — the user owns
        the machine; we just hide dotfiles and fail soft on unreadable dirs."""
        auth(token)
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
    def api_meta(token: str | None = Query(None)):
        auth(token)
        import tyani_tolkai.arena.cegis  # noqa: F401  (registers the cegis referee)
        from ..arena.referee import list_referees
        return {
            "engines": ["claude", "codex", "opencode", "agy"],
            "adapters": ["numeric", "command-exit", "pytest-pass"],
            "seeds": ["empty", "copy"],
            "modes": ["asymmetric", "symmetric"],
            "referees": list_referees(),
            "dirs": ["higher", "lower"],
        }

    # ---- research + scaffold phase: vetted templates → runnable project ----
    @app.get("/api/templates")
    def api_templates(token: str | None = Query(None)):
        auth(token)
        from ..scaffold import list_templates
        return {"templates": list_templates()}

    @app.post("/api/scaffold")
    def api_scaffold(payload: dict = Body(...), token: str | None = Query(None)):
        """Build a ready-to-run project from a template (the scoring harness ships vetted,
        never generated). Description becomes the executor goal; if no template is given it
        is inferred from the description."""
        auth(token)
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
    def api_configure(payload: dict = Body(...), token: str | None = Query(None)):
        auth(token)
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
    def api_create(payload: dict = Body(...), token: str | None = Query(None)):
        auth(token)
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
                shutil.copytree(src, state.artifact_dir, dirs_exist_ok=True)
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
    def api_gen_adapter(payload: dict = Body(...), token: str | None = Query(None)):
        """Scaffold adapter.py (vetted protocol plumbing + a decide() stub) into the bot dir."""
        auth(token)
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
    def api_check_adapter(payload: dict = Body(...), token: str | None = Query(None)):
        """Trust gate: drive the adapter over the protocol on synthetic bars → verdict PASS/FLAG.

        Proves protocol soundness + determinism + non-degeneracy — NOT semantics (sign/scale);
        run `validate` for that. Returns the check_adapter_orders verdict dict (200 even on FLAG;
        the verdict's ``ok`` carries PASS/FLAG so the UI can render either).
        """
        auth(token)
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
        cmd = shlex.split(bot_cmd)
        prt = float(payload.get("per_read_timeout") or 10.0)
        tt = float(payload.get("total_timeout") or 120.0)
        try:
            run1 = drive_bot(cmd, bars, params={}, per_read_timeout=prt, total_timeout=tt)
            run2 = drive_bot(cmd, bars, params={}, per_read_timeout=prt, total_timeout=tt)
        except BotProtocolError as e:
            return {"ok": False, "well_formed": False, "deterministic": False, "degenerate": False,
                    "reasons": [f"adapter failed the protocol: {e}"]}
        return check_adapter_orders(run1, run2, len(bars))

    @app.post("/api/onboard")
    def api_onboard(payload: dict = Body(...), token: str | None = Query(None)):
        """Create a runnable optimization project from a bot + its data + a P2 proposal."""
        auth(token)
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
    def api_profile(payload: dict = Body(...), token: str | None = Query(None)):
        """Analyze an existing bot (read-only, LLM) → BotProfile (json + markdown). Synchronous,
        like /api/configure; the UI shows a spinner while the analyzer runs."""
        auth(token)
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
    def api_propose(payload: dict = Body(...), token: str | None = Query(None)):
        """Propose evaluation metrics + tunable ranges from a profile + goal (LLM). Synchronous.

        The human reviews/edits the proposal before it becomes the onboarding config (ADR gate)."""
        auth(token)
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
    def api_validate(payload: dict = Body(...), token: str | None = Query(None)):
        """P4 secondary validation of a bot → evidence report (PASS/FLAG). Synchronous.

        Untrusted bots need Docker (isolation = trust); trusted=true uses a process-separation
        subprocess (usable without Docker, for a reference/own bot)."""
        auth(token)
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
        bot_cmd = shlex.split(bot_cmd_s)
        prt = float(payload.get("per_read_timeout") or 10.0)
        tt = float(payload.get("total_timeout") or 120.0)
        if payload.get("trusted"):
            isolation = "subprocess (process-separation only; trusted asserted by operator)"
            def score_bars(b):
                return score_bot(bot_cmd, b, params=params, seed=seed, per_read_timeout=prt, total_timeout=tt)
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

    @app.get("/api/projects/{name}/config")
    def api_get_config(name: str, token: str | None = Query(None)):
        auth(token)
        p = project_dir(name) / "config.yaml"
        if not p.exists():
            raise HTTPException(404, "no config for this project")
        return yaml.safe_load(p.read_text(encoding="utf-8"))

    @app.put("/api/projects/{name}/config")
    def api_put_config(name: str, payload: dict = Body(...), token: str | None = Query(None)):
        auth(token)
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
    def api_test_eval(name: str, token: str | None = Query(None)):
        """Run the evaluation ONCE on the current artifact — verify the harness works and
        see the metrics or the raw stdout/stderr, without a full run (beats cold-start)."""
        auth(token)
        base = project_dir(name)
        if not (base / "config.yaml").exists():
            raise HTTPException(404, "no such project")
        cfg = load_config(base / "config.yaml")
        state = StateStore(base)
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
                try:
                    out["score"] = score(values, cfg.evaluation.metrics)
                except Exception as e:
                    out["score"] = None
                    out["logs"] = f"metrics ran but scoring failed: {e}\n" + out["logs"]
            return out
        except Exception as e:
            return {"ok": False, "logs": f"could not run evaluation: {e}", "metrics": []}
        finally:
            state.close()

    @app.post("/api/projects/{name}/stop")
    def api_stop(name: str, token: str | None = Query(None)):
        auth(token)
        app.state.runs.stop(name)
        return {"stopping": name}

    @app.post("/api/projects/{name}/force-stop")
    def api_force_stop(name: str, token: str | None = Query(None)):
        auth(token)
        killed = app.state.runs.force_stop(name)   # SIGKILL the live agent immediately
        return {"force_stopped": name, "killed": killed}

    @app.post("/api/projects/{name}/delete")
    def api_delete(name: str, token: str | None = Query(None)):
        auth(token)
        _not_while_running(name)
        delete_project(name)
        return {"deleted": name}

    @app.post("/api/projects/{name}/rename")
    def api_rename(name: str, to: str = Query(...), token: str | None = Query(None)):
        auth(token)
        _not_while_running(name)
        try:
            rename_project(name, to)
        except FileNotFoundError:
            raise HTTPException(404, f"no such project: {name}")
        except FileExistsError:
            raise HTTPException(409, f"target name already exists: {to}")
        return {"renamed": to}

    @app.post("/api/projects/{name}/reset")
    def api_reset(name: str, token: str | None = Query(None)):
        auth(token)
        _not_while_running(name)
        if not project_dir(name).exists():
            raise HTTPException(404, f"no such project: {name}")
        reset_project(name)
        return {"reset": name}

    @app.get("/api/projects/{name}/export")
    def api_export(name: str, n: int | None = Query(None, alias="iter"),
                   token: str | None = Query(None)):
        auth(token)
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
        dest = Path(tempfile.mkdtemp()) / f"{name}.zip"      # unique dir per request
        export_project(name, dest, at_hash=at_hash, at_label=n)
        fn = f"{name}.zip" if n is None else f"{name}-iter{n}.zip"
        return FileResponse(dest, filename=fn, media_type="application/zip")

    @app.post("/api/projects/{name}/rewind")
    def api_rewind(name: str, n: int = Query(..., alias="iter"),
                   token: str | None = Query(None)):
        auth(token)
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
    def api_fork(name: str, n: int = Query(..., alias="iter"), to: str = Query(...),
                 token: str | None = Query(None)):
        auth(token)
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
