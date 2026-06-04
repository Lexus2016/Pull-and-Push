"""FastAPI backend for the Тяни-Толкай dashboard.

Live updates use polling (GET /live), not WebSocket — simpler and robust across the
run thread boundary while giving the same live evolution chart. A background thread
runs the orchestrator; the UI polls its outcomes. Optional token auth (from the
TYANI_TOLKAI_WEB_PASSWORD env) protects the API when set.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from ..config import Config, load_config
from ..metrics import get_metric_adapter
from ..orchestrator import Orchestrator
from ..projects import (
    delete_project, export_project, list_projects, project_dir,
    rename_project, reset_project,
)
from ..registry import build_adapter
from ..sandbox import get_backend
from ..state import StateStore

STATIC = Path(__file__).parent / "static"


class RunManager:
    """Tracks background runs and their streamed outcomes (in memory)."""

    def __init__(self):
        self._runs: dict[str, dict] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    def stop(self, name: str) -> None:
        self._stop.add(name)

    def snapshot(self, name: str) -> dict:
        with self._lock:
            r = self._runs.get(name)
            return dict(r) if r is None else {
                "status": r["status"], "summary": r["summary"],
                "outcomes": list(r["outcomes"]),
            }

    def is_running(self, name: str) -> bool:
        with self._lock:
            r = self._runs.get(name)
            return bool(r and r["status"] == "running")

    def start_demo(self) -> dict:
        """Run the built-in demo synchronously (fast) and return its outcomes."""
        import tempfile
        from .._demo import build_demo
        outcomes: list[dict] = []
        with tempfile.TemporaryDirectory() as d:
            _cfg, state, _rid, orch = build_demo(d)
            summary = orch.run_loop(
                on_iteration=lambda o: outcomes.append(
                    {"n": o.n, "verdict": o.verdict, "score": o.score}))
            state.close()
        return {"outcomes": outcomes, "summary": {"reason": summary.reason,
                "best_score": summary.best_score, "iterations": summary.iterations}}

    def start_run(self, name: str) -> None:
        with self._lock:
            if self._runs.get(name, {}).get("status") == "running":
                raise HTTPException(409, "run already in progress")
            self._stop.discard(name)   # clear inside the lock so a racing /stop isn't lost
            self._runs[name] = {"status": "running", "summary": None, "outcomes": []}
        threading.Thread(target=self._run, args=(name,), daemon=True).start()

    def _run(self, name: str) -> None:
        state = None
        run_id = None
        try:
            base = project_dir(name)
            cfg = load_config(base / "config.yaml")
            state = StateStore(base)
            # resume the latest unfinished run (continue progress) instead of starting over
            last = state.conn.execute("SELECT id, status FROM run ORDER BY id DESC LIMIT 1").fetchone()
            has_hist = last and state.conn.execute(
                "SELECT 1 FROM iteration WHERE run_id=? LIMIT 1", (last["id"],)).fetchone()
            if last and last["status"] != "finished" and has_hist:
                run_id = last["id"]
                state.reconcile(run_id)
                if state.has_changes():
                    state.revert_uncommitted()
                state.set_status(run_id, "running")
            else:
                run_id = state.create_run(cfg.mode)
            ex = cfg.agents["executor"]
            executor = build_adapter(ex.engine, ex.model, "writeable")
            validator = None
            if "validator" in cfg.agents:
                va = cfg.agents["validator"]
                validator = build_adapter(va.engine, va.model, "read-only")
            orch = Orchestrator(cfg, state, run_id, executor,
                                get_metric_adapter(cfg.evaluation.adapter),
                                get_backend(cfg.sandbox.backend, cfg.sandbox), validator)

            def on_iter(o):
                with self._lock:
                    self._runs[name]["outcomes"].append(
                        {"n": o.n, "verdict": o.verdict, "score": o.score,
                         "feedback": o.feedback, "change": o.change,
                         "metrics": [{"name": m["name"], "value": m["value"]} for m in (o.metrics or [])]})

            summary = orch.run_loop(on_iteration=on_iter,
                                    should_stop=lambda: name in self._stop)
            st = {"stopped": "stopped", "rate_limited": "error",
                  "agent_error": "error"}.get(summary.reason, "finished")
            with self._lock:
                self._runs[name]["status"] = st
                self._runs[name]["summary"] = {"reason": summary.reason,
                    "best_score": summary.best_score, "iterations": summary.iterations}
        except Exception as e:  # surface failures to the UI rather than dying silently
            with self._lock:
                self._runs[name]["status"] = "error"
                self._runs[name]["summary"] = {"error": str(e)}
            if state is not None and run_id is not None:
                try:
                    state.set_status(run_id, "error")   # no zombie 'running' in the db
                except Exception:
                    pass
        finally:
            if state is not None:
                state.close()                            # never leak the connection


def _persisted_state(name: str) -> dict:
    base = project_dir(name)
    if not (base / "state.db").exists():
        return {"iterations": [], "best_score": None}
    state = StateStore(base)
    try:
        # prefer the latest run that actually has history (skip empty/zombie runs)
        run = state.conn.execute(
            "SELECT * FROM run WHERE id IN (SELECT DISTINCT run_id FROM iteration) "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            run = state.conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            return {"iterations": [], "best_score": None}
        iters = state.last_iterations(run["id"], 100000)   # oldest-first, with metrics
        return {
            "status": run["status"], "best_score": run["best_score"],
            "iterations": [{"n": it.n, "score": it.score, "verdict": it.verdict,
                            "change": it.change_summary, "feedback": it.feedback,
                            "metrics": [{"name": m["name"], "value": m["value"]} for m in it.metrics]}
                           for it in iters],
        }
    finally:
        state.close()


def create_app(token: str | None = None) -> FastAPI:
    app = FastAPI(title="Тяни-Толкай")
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
        return {"projects": list_projects()}

    @app.get("/api/projects/{name}")
    def api_project(name: str, token: str | None = Query(None)):
        auth(token)
        return _persisted_state(name)

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

    @app.post("/api/projects/{name}/run")
    def api_run(name: str, token: str | None = Query(None)):
        auth(token)
        if not (project_dir(name) / "config.yaml").exists():
            raise HTTPException(404, "project has no config.yaml; create it via `tyani-tolkai run`")
        app.state.runs.start_run(name)
        return {"started": name}

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

    @app.post("/api/demo")
    def api_demo(token: str | None = Query(None)):
        auth(token)
        return JSONResponse(app.state.runs.start_demo())

    # ---- meta for the config form ----
    @app.get("/api/meta")
    def api_meta(token: str | None = Query(None)):
        auth(token)
        return {
            "engines": ["claude", "codex", "opencode", "agy"],
            "adapters": ["numeric", "command-exit", "pytest-pass"],
            "seeds": ["empty", "copy", "generate"],
            "modes": ["asymmetric"],
            "dirs": ["higher", "lower"],
        }

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
        return {"created": name}

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
    @app.post("/api/projects/{name}/stop")
    def api_stop(name: str, token: str | None = Query(None)):
        auth(token)
        app.state.runs.stop(name)
        return {"stopping": name}

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
    def api_export(name: str, token: str | None = Query(None)):
        auth(token)
        dest = Path(tempfile.mkdtemp()) / f"{name}.tar.gz"   # unique dir per request
        export_project(name, dest)
        return FileResponse(dest, filename=f"{name}.tar.gz")

    return app
