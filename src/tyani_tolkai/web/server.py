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
            self._runs[name] = {"status": "running", "summary": None, "outcomes": []}
        self._stop.discard(name)
        threading.Thread(target=self._run, args=(name,), daemon=True).start()

    def _run(self, name: str) -> None:
        try:
            base = project_dir(name)
            cfg = load_config(base / "config.yaml")
            state = StateStore(base)
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
                        {"n": o.n, "verdict": o.verdict, "score": o.score})

            summary = orch.run_loop(on_iteration=on_iter,
                                    should_stop=lambda: name in self._stop)
            with self._lock:
                self._runs[name]["status"] = "stopped" if summary.reason == "stopped" else "finished"
                self._runs[name]["summary"] = {"reason": summary.reason,
                    "best_score": summary.best_score, "iterations": summary.iterations}
            state.close()
        except Exception as e:  # surface failures to the UI rather than dying silently
            with self._lock:
                self._runs[name]["status"] = "error"
                self._runs[name]["summary"] = {"error": str(e)}


def _persisted_state(name: str) -> dict:
    base = project_dir(name)
    if not (base / "state.db").exists():
        return {"iterations": [], "best_score": None}
    state = StateStore(base)
    try:
        run = state.conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            return {"iterations": [], "best_score": None}
        rows = state.conn.execute(
            "SELECT n, score, verdict FROM iteration WHERE run_id=? ORDER BY n", (run["id"],),
        ).fetchall()
        return {
            "status": run["status"], "best_score": run["best_score"],
            "iterations": [{"n": r["n"], "score": r["score"], "verdict": r["verdict"]} for r in rows],
        }
    finally:
        state.close()


def create_app(token: str | None = None) -> FastAPI:
    app = FastAPI(title="Тяни-Толкай")
    app.state.token = token if token is not None else os.environ.get("TYANI_TOLKAI_WEB_PASSWORD")
    app.state.runs = RunManager()

    def auth(t: str | None) -> None:
        if app.state.token and t != app.state.token:
            raise HTTPException(401, "bad or missing token")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/projects")
    def api_projects(token: str | None = Query(None)):
        auth(token)
        return {"projects": list_projects()}

    @app.get("/api/projects/{name}")
    def api_project(name: str, token: str | None = Query(None)):
        auth(token)
        return _persisted_state(name)

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
        delete_project(name)
        return {"deleted": name}

    @app.post("/api/projects/{name}/rename")
    def api_rename(name: str, to: str = Query(...), token: str | None = Query(None)):
        auth(token)
        rename_project(name, to)
        return {"renamed": to}

    @app.post("/api/projects/{name}/reset")
    def api_reset(name: str, token: str | None = Query(None)):
        auth(token)
        reset_project(name)
        return {"reset": name}

    @app.get("/api/projects/{name}/export")
    def api_export(name: str, token: str | None = Query(None)):
        auth(token)
        dest = Path(tempfile.gettempdir()) / f"{name}.tar.gz"
        export_project(name, dest)
        return FileResponse(dest, filename=f"{name}.tar.gz")

    return app
