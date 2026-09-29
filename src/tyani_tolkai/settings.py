"""Machine-wide settings (~/.tyani-tolkai/settings.json), shared by the terminal and the macOS app:
which Python runs the scorers (`{python}`), and whether a source install checks GitHub for a newer
release. Plus the small probes the dashboard's Settings panel shows (Python versions, the update
check)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .projects import home_root

REPO = "Lexus2016/Pull-and-Push"
DEFAULTS = {"scorer_python": None, "update_check": True}
PYTHON_ENV = "PULL_AND_PUSH_PYTHON"          # overrides the setting (CI, one-off runs)


def _file() -> Path:
    return home_root() / "settings.json"


def load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in (data if isinstance(data, dict) else {}).items()
                           if k in DEFAULTS}}


def save(patch: dict) -> dict:
    """Validate and merge ``patch`` into the settings; returns them. Raises ValueError."""
    unknown = set(patch) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    cur = load()
    if "scorer_python" in patch:
        py = (patch["scorer_python"] or "").strip() or None
        if py is not None:
            info = python_info(py)
            if not info["ok"]:
                raise ValueError(f"{py}: {info['error']}")
        cur["scorer_python"] = py
    if "update_check" in patch:
        cur["update_check"] = bool(patch["update_check"])
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def scorer_python() -> str:
    """The interpreter `{python}` means for local scorers: $PULL_AND_PUSH_PYTHON, else the setting,
    else the one running Pull-and-Push (inside the macOS app: the bundled, stdlib-only one)."""
    return os.environ.get(PYTHON_ENV) or load()["scorer_python"] or sys.executable


def python_info(path: str) -> dict:
    """{path, ok, version, error} — runs the interpreter once."""
    exe = shutil.which(path) or path
    try:
        r = subprocess.run([exe, "-c", "import sys; print(sys.version.split()[0])"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"path": exe, "ok": False, "version": None, "error": str(e)}
    if r.returncode != 0:
        return {"path": exe, "ok": False, "version": None,
                "error": (r.stderr or r.stdout).strip()[-300:] or f"exit code {r.returncode}"}
    return {"path": exe, "ok": True, "version": r.stdout.strip(), "error": None}


def python_candidates() -> list[str]:
    """Interpreters worth offering: the current one, python3 on PATH and the usual install places."""
    home = Path.home()
    found = [sys.executable, shutil.which("python3"), shutil.which("python"),
             "/opt/homebrew/bin/python3", "/usr/local/bin/python3", str(home / ".pyenv/shims/python3")]
    out: list[str] = []
    for p in found:
        if p and os.path.isfile(p) and os.access(p, os.X_OK) and p not in out:
            out.append(p)
    return out


# ------------------------------------------------------------------------------ update check

def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for x in v.lstrip("vV").split("."):
        num = "".join(ch for ch in x if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def newer(latest: str | None, current: str) -> bool:
    return bool(latest) and _version_tuple(latest) > _version_tuple(current)


def latest_release(force: bool = False, fetch=None) -> dict | None:
    """GitHub's latest release {tag, url, checked}, cached a day in the data dir; None when the
    check is off, runs inside the macOS app (Sparkle updates it), or GitHub is unreachable."""
    if os.environ.get("PULL_AND_PUSH_APP") or not load()["update_check"]:
        return None
    cache = home_root() / "update-check.json"
    try:
        c = json.loads(cache.read_text(encoding="utf-8"))
        if not force and time.time() - float(c.get("checked", 0)) < 86400:
            return c
    except (OSError, ValueError, TypeError):
        pass
    try:
        data = (fetch or _fetch_latest)()
    except Exception:                                   # noqa: BLE001 — offline is normal
        return None
    c = {"tag": data.get("tag_name"), "url": data.get("html_url"), "checked": time.time()}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(c), encoding="utf-8")
    except OSError:
        pass
    return c


def cached_release() -> dict | None:
    """The last update check's result, without touching the network."""
    if os.environ.get("PULL_AND_PUSH_APP") or not load()["update_check"]:
        return None
    try:
        return json.loads((home_root() / "update-check.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fetch_latest() -> dict:
    import urllib.request
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "pull-and-push"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))
