"""FastAPI backend for the Pull-and-Push dashboard.

Live updates use polling (GET /live), not WebSocket — simpler and robust across the
run thread boundary while giving the same live evolution chart. A background thread
runs the orchestrator; the UI polls its outcomes. Optional token auth (from the
TYANI_TOLKAI_WEB_PASSWORD env) protects the API when set.
"""

from __future__ import annotations

import json
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


class RunManager:
    """Tracks background runs and their streamed outcomes (in memory)."""

    def __init__(self):
        self._runs: dict[str, dict] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    def stop(self, name: str) -> None:
        self._stop.add(name)

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
                    "baseline": dict(r.get("baseline") or {}), "cost": r.get("cost", 0.0)}

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
                                "phase": None, "baseline": {}, "cost": 0.0}
        threading.Thread(target=self._run, args=(name,), daemon=True).start()

    def _run(self, name: str) -> None:
        state = None
        run_id = None
        cfg = None
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
                # Seed the live view with THIS run's persisted history. Without it, /live returns
                # only the outcomes appended this session, so the chart/log collapse to the new
                # iterations on resume — it looked like the run had reset to iteration 1.
                hist = state.last_iterations(run_id, 100000)
                with self._lock:
                    if name in self._runs:
                        self._runs[name]["outcomes"] = [
                            {"n": it.n, "verdict": it.verdict, "score": it.score,
                             "feedback": it.feedback, "change": it.change_summary,
                             "metrics": [{"name": m["name"], "value": m["value"]} for m in it.metrics]}
                            for it in hist]
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
            with self._lock:
                self._runs[name]["orch"] = orch   # so Force-Stop can reach the live agent

            def on_iter(o):
                with self._lock:
                    self._runs[name]["outcomes"].append(
                        {"n": o.n, "verdict": o.verdict, "score": o.score,
                         "feedback": o.feedback, "change": o.change,
                         "metrics": [{"name": m["name"], "value": m["value"]} for m in (o.metrics or [])]})
                    self._runs[name]["baseline"] = dict(orch._baseline)   # resolved zero-points (live cards)
                    self._runs[name]["cost"] = orch.cost_total            # estimated spend so far

            def on_ph(p):
                with self._lock:
                    if name in self._runs:
                        self._runs[name]["phase"] = p

            summary = orch.run_loop(on_iteration=on_iter, on_phase=on_ph,
                                    should_stop=lambda: name in self._stop)
            st = {"stopped": "stopped", "rate_limited": "error",
                  "agent_error": "error"}.get(summary.reason, "finished")
            with self._lock:
                self._runs[name]["status"] = st
                self._runs[name]["summary"] = {"reason": summary.reason,
                    "best_score": summary.best_score, "iterations": summary.iterations}
            _fire_webhook(cfg, name, {"project": name, "status": st, "reason": summary.reason,
                                      "best_score": summary.best_score,
                                      "iterations": summary.iterations})
        except Exception as e:  # surface failures to the UI rather than dying silently
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
        return {
            "status": run["status"], "best_score": run["best_score"], "baseline": baseline,
            "cost": cost or 0.0,
            "iterations": [{"n": it.n, "score": it.score, "verdict": it.verdict,
                            "change": it.change_summary, "feedback": it.feedback,
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
        return {
            "engines": ["claude", "codex", "opencode", "agy"],
            "adapters": ["numeric", "command-exit", "pytest-pass"],
            "seeds": ["empty", "copy"],
            "modes": ["asymmetric"],
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
    def api_export(name: str, token: str | None = Query(None)):
        auth(token)
        dest = Path(tempfile.mkdtemp()) / f"{name}.zip"      # unique dir per request
        export_project(name, dest)
        return FileResponse(dest, filename=f"{name}.zip", media_type="application/zip")

    return app
