"""Project lifecycle: list / delete / rename / reset / export / import (spec §9).

A project is a self-contained directory under the home root. Export bundles it into
a single tar (the artifact's git history travels as a `git bundle`, all paths
relative, secrets excluded) so a run can continue on another machine.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

import yaml

from .state import StateStore

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def valid_name(name: str) -> str:
    """Reject anything that could escape the projects root (path traversal)."""
    if not name or ".." in name or "/" in name or "\\" in name or not _NAME_RE.match(name):
        raise ValueError(f"invalid project name: {name!r}")
    return name


def home_root() -> Path:
    return Path(os.environ.get("TYANI_TOLKAI_HOME", str(Path.home() / ".tyani-tolkai")))


def projects_root() -> Path:
    r = home_root() / "projects"
    r.mkdir(parents=True, exist_ok=True)
    return r


def project_dir(name: str) -> Path:
    valid_name(name)                       # centralized path-traversal guard
    return projects_root() / name


def list_projects() -> list[str]:
    return sorted(p.name for p in projects_root().iterdir() if p.is_dir())


def delete_project(name: str) -> None:
    d = project_dir(name)
    if d.exists():
        shutil.rmtree(d)


def rename_project(old: str, new: str) -> None:
    src, dst = project_dir(old), project_dir(new)
    if not src.exists():
        raise FileNotFoundError(f"no such project: {old}")
    if dst.exists():
        raise FileExistsError(f"project already exists: {new}")
    src.rename(dst)


def reset_project(name: str) -> None:
    """Git-reset the artifact to its initial commit and clear all run history; keep config."""
    d = project_dir(name)
    state = StateStore(d)
    try:
        seed = state._git("rev-list", "--max-parents=0", "HEAD").splitlines()[0]
        state._git("reset", "-q", "--hard", seed)
        state._git("clean", "-fdq")
        cur = state.conn.cursor()       # explicit cursor (PyPy-safe: no dangling statements)
        try:
            for tbl in ("metric", "iteration", "command", "checkpoint", "run"):
                cur.execute(f"DELETE FROM {tbl}")
            state.conn.commit()
        finally:
            cur.close()
    finally:
        state.close()


def _build_docs(d: Path, name: str) -> tuple[str, str]:
    """Generate README.md (how to run/use) + RESULTS.md (achieved metrics) for the bundle."""
    cfg = {}
    if (d / "config.yaml").exists():
        try:
            cfg = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    ev = cfg.get("evaluation", {}) or {}
    cmd, adapter, target = ev.get("command"), ev.get("adapter"), ev.get("target_score")
    goal = ((cfg.get("roles", {}) or {}).get("executor", {}) or {}).get("goal", "")
    desc = cfg.get("description") or goal or "(no description)"

    best, iters, achieved = None, 0, []
    if (d / "state.db").exists():
        con = sqlite3.connect(str(d / "state.db")); con.row_factory = sqlite3.Row
        try:
            run = (con.execute("SELECT * FROM run WHERE id IN (SELECT DISTINCT run_id FROM iteration) "
                               "ORDER BY id DESC LIMIT 1").fetchone()
                   or con.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone())
            if run:
                best, iters = run["best_score"], run["iter_count"]
                it = con.execute("SELECT id FROM iteration WHERE run_id=? AND verdict='keep' "
                                 "ORDER BY n DESC LIMIT 1", (run["id"],)).fetchone()
                if it:
                    achieved = [dict(m) for m in con.execute(
                        "SELECT name,value,dir,weight FROM metric WHERE iteration_id=?", (it["id"],)).fetchall()]
        finally:
            con.close()

    tgt = {m["name"]: m for m in (ev.get("metrics") or [])}
    rows = "".join(f"| {m['name']} | {m['value']} | {tgt.get(m['name'],{}).get('target','?')} "
                   f"| {m.get('dir','')} | {tgt.get(m['name'],{}).get('weight','')} |\n" for m in achieved) \
           or "| (no metrics recorded yet) | | | | |\n"

    readme = f"""# {cfg.get('project', name)} — result bundle

{desc}

_Produced by Тяни-Толкай — an adversarial co-evolution orchestrator: one agent improves the
artifact, a deterministic scorer (+ optional validator agent) judges it, and the best version
is kept._

## What's in this archive
- `artifact/` — **the result**: the code the agent produced (e.g. `strategy.py`).
- `metrics/` — the evaluation harness + data used to score it (lets you reproduce the numbers).
- `config.yaml` — the run setup (agents, metrics, targets).
- `RESULTS.md` — the score and metrics achieved.
- `artifact.bundle` — full git history of how the artifact evolved
  (optional: `git clone artifact.bundle history`).

## How to run / reproduce
1. Install Python 3.10+ and any libraries your evaluation needs.
2. From the `artifact/` directory, run the evaluation command:
   ```
   {cmd or '<your evaluation command>'}
   ```
   Adapter: `{adapter}`. The harness and data live in `metrics/` (the command references them).
3. It prints the objective metrics as JSON — exactly what was optimized.

## How to use the result
`artifact/` is your deliverable. For a trading strategy, `strategy.py` holds the tuned
result; `metrics/backtest.py` shows precisely how those parameters are consumed, so you can
port them into your own backtest or live pipeline.
"""
    results = f"""# Results — {cfg.get('project', name)}

- **Composite score:** {best if best is not None else '—'} / target {target if target is not None else '—'}
- **Iterations:** {iters}

## Metrics achieved (best kept version)
| metric | value | target | dir | weight |
|---|---|---|---|---|
{rows}
> Composite score normalizes each metric `worst → target` to 0–100 and takes the weighted sum.
"""
    return readme, results


def export_project(name: str, dest_tar: str | Path) -> Path:
    d = project_dir(name)
    if not d.exists():
        raise FileNotFoundError(f"no such project: {name}")
    dest = Path(dest_tar)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / name
        stage.mkdir()
        # copy everything except the live artifact git checkout (travels as a bundle)
        for item in ("config.yaml", "state.db"):
            if (d / item).exists():
                shutil.copy2(d / item, stage / item)
        _skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
        for sub in ("context", "metrics"):
            if (d / sub).exists():
                shutil.copytree(d / sub, stage / sub, ignore=_skip)
        # the actual result code, ready to read/run (working tree, without .git)
        if (d / "artifact").exists():
            shutil.copytree(d / "artifact", stage / "artifact",
                            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        if (d / "artifact" / ".git").exists():     # + full history as a portable bundle
            subprocess.run(["git", "-C", str(d / "artifact"), "bundle", "create",
                            str(stage / "artifact.bundle"), "--all"], check=True,
                           capture_output=True)
        readme, results = _build_docs(d, name)     # human-readable deliverable docs
        (stage / "README.md").write_text(readme, encoding="utf-8")
        (stage / "RESULTS.md").write_text(results, encoding="utf-8")
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(stage, arcname=name)
    return dest


def import_project(src_tar: str | Path, name: str | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(src_tar, "r:gz") as tar:
            tar.extractall(tmp, filter="data")   # safe extraction (no path escape)
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if not roots:
            raise ValueError("empty archive")
        staged = roots[0]
        proj_name = name or staged.name
        dest = project_dir(proj_name)
        if dest.exists():
            raise FileExistsError(f"project already exists: {proj_name}")
        dest.mkdir(parents=True)
        for item in ("config.yaml", "state.db"):
            if (staged / item).exists():
                shutil.copy2(staged / item, dest / item)
        for sub in ("context", "metrics"):
            if (staged / sub).exists():
                shutil.copytree(staged / sub, dest / sub)
        if (staged / "artifact.bundle").exists():
            subprocess.run(["git", "clone", "-q", str(staged / "artifact.bundle"),
                            str(dest / "artifact")], check=True, capture_output=True)
        return proj_name
