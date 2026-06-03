"""Project lifecycle: list / delete / rename / reset / export / import (spec §9).

A project is a self-contained directory under the home root. Export bundles it into
a single tar (the artifact's git history travels as a `git bundle`, all paths
relative, secrets excluded) so a run can continue on another machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .state import StateStore


def home_root() -> Path:
    return Path(os.environ.get("TYANI_TOLKAI_HOME", str(Path.home() / ".tyani-tolkai")))


def projects_root() -> Path:
    r = home_root() / "projects"
    r.mkdir(parents=True, exist_ok=True)
    return r


def project_dir(name: str) -> Path:
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
        with state.conn:
            for tbl in ("metric", "iteration", "command", "checkpoint", "run"):
                state.conn.execute(f"DELETE FROM {tbl}")
    finally:
        state.close()


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
        for sub in ("context", "metrics"):
            if (d / sub).exists():
                shutil.copytree(d / sub, stage / sub)
        # artifact history as a portable bundle
        if (d / "artifact" / ".git").exists():
            subprocess.run(["git", "-C", str(d / "artifact"), "bundle", "create",
                            str(stage / "artifact.bundle"), "--all"], check=True,
                           capture_output=True)
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
