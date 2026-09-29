"""Regression tests for the 2026-09 audit fixes in the web layer."""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from tyani_tolkai.projects import project_dir
from tyani_tolkai.web.server import RunManager, create_app

_VALID = {
    "project": "webfix", "mode": "asymmetric",
    "agents": {"executor": {"engine": "claude", "timeout": 600}},
    "roles": {"executor": {"goal": "improve"}},
    "evaluation": {"adapter": "numeric", "command": "true",
                   "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                   "target_score": 100},
    "limits": {"max_iterations": 2, "plateau_N": 6, "step_seconds": 600},
    "sandbox": {"backend": "local"}, "seed": {"mode": "empty"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    return TestClient(create_app(token=None))


def _live_run(mgr: RunManager, name: str, ns):
    with mgr._lock:
        mgr._epoch += 1
        mgr._runs[name] = {"status": "running", "summary": None, "epoch": mgr._epoch,
                           "outcomes": [{"n": n, "verdict": "keep", "score": float(n)} for n in ns],
                           "phase": None, "baseline": {}, "cost": 0.0, "best": None, "checkpoint": None}


def test_live_sends_only_new_iterations_within_an_epoch(client):
    mgr = client.app.state.runs
    _live_run(mgr, "webfix", [1, 2, 3])
    full = client.get("/api/projects/webfix/live").json()
    assert [o["n"] for o in full["outcomes"]] == [1, 2, 3] and full["delta"] is False
    ep = full["epoch"]
    part = client.get(f"/api/projects/webfix/live?since=2&epoch={ep}").json()
    assert part["delta"] is True and [o["n"] for o in part["outcomes"]] == [3]
    # the history was replaced (new Run / re-baseline) → a stale epoch gets the full list again
    _live_run(mgr, "webfix", [1, 2, 3, 4])
    again = client.get(f"/api/projects/webfix/live?since=3&epoch={ep}").json()
    assert again["delta"] is False and [o["n"] for o in again["outcomes"]] == [1, 2, 3, 4]


def test_test_eval_is_refused_while_running(client, monkeypatch):
    client.post("/api/projects/create", json=_VALID)
    monkeypatch.setattr(client.app.state.runs, "is_running", lambda n: True)
    assert client.post("/api/projects/webfix/test-eval").status_code == 409


def test_test_eval_leaves_no_scorer_side_effects(client):
    cmd = "{python} -c \"open('scratch.out', 'w').write('1')\""      # portable (no `touch` on Windows)
    cfg = dict(_VALID, evaluation=dict(_VALID["evaluation"], command=cmd))
    client.post("/api/projects/create", json=cfg)
    client.post("/api/projects/webfix/test-eval")
    art = project_dir("webfix") / "artifact"
    assert not (art / "scratch.out").exists()
    st = subprocess.run(["git", "-C", str(art), "status", "--porcelain"], capture_output=True,
                        text=True).stdout
    assert st.strip() == ""


def test_startup_heals_an_orphaned_running_arena_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    d = project_dir("arena-x")
    d.mkdir(parents=True)
    (d / "arena.json").write_text(json.dumps({"parent_run": 1, "status": "running", "generation": 2}))
    TestClient(create_app(token=None))                      # a (re)started server
    assert json.loads((d / "arena.json").read_text())["status"] == "stopped"


def test_files_endpoint_handles_spaces_and_cyrillic_names(client):
    client.post("/api/projects/create", json=_VALID)
    art = project_dir("webfix") / "artifact"
    (art / "моя стратегія.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(art), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(art), "commit", "-q", "-m", "add"], check=True,
                   capture_output=True)
    files = client.get("/api/projects/webfix/files").json()["files"]
    f = next(x for x in files if x["path"] == "моя стратегія.py")
    assert f["content"] == "x = 1\n"


def test_token_with_special_characters_authenticates(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    c = TestClient(create_app(token="a&b#c+d"))
    assert c.get("/api/projects", params={"token": "a&b#c+d"}).status_code == 200
    assert c.get("/api/projects", params={"token": "a&b"}).status_code == 401
