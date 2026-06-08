import yaml

from tyani_tolkai.agents.scripted_rival import ScriptedAdversaryRival, ScriptedRecognizerRival
from tyani_tolkai.web import server


def _seed_project(name):
    base = server.project_dir(name)
    base.mkdir(parents=True, exist_ok=True)
    cfg = {
        "project": name, "mode": "symmetric",
        "agents": {"rival_a": {"engine": "mock"}, "rival_b": {"engine": "mock"}},
        "roles": {"rival_a": {"goal": "recognize"}, "rival_b": {"goal": "fool"}},
        "evaluation": {"adapter": "numeric", "command": "x",
                       "metrics": [{"name": "arena_fitness", "dir": "higher",
                                    "target": 1.0, "worst": 0.0}]},
        "arena": {"referee": "cegis-recognizer", "generations": 6,
                  "per_generation_iterations": 2, "dominance_tau": 0.99, "dominance_rounds": 1},
    }
    (base / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return base


def test_web_symmetric_run_reaches_terminal_with_arena_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server, "_symmetric_rivals",
                        lambda cfg: (ScriptedRecognizerRival(), ScriptedAdversaryRival()))
    name = "toy-arena-web"
    _seed_project(name)

    rm = server.RunManager()
    rm._runs[name] = {"status": "running", "summary": None, "outcomes": [],
                      "phase": None, "baseline": {}, "cost": 0.0, "best": None, "checkpoint": None}
    rm._run(name)   # run synchronously (no thread) for a deterministic test

    snap = rm.snapshot(name)
    assert snap["status"] == "finished"
    summ = snap["summary"]
    assert summ["mode"] == "symmetric"
    assert summ["reason"] in ("dominance", "plateau")
    assert summ["best_a_id"] is not None
    assert isinstance(summ["stable_a"], list) and isinstance(summ["stable_b"], list)


def test_api_arena_returns_manifest_after_run(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server, "_symmetric_rivals",
                        lambda cfg: (ScriptedRecognizerRival(), ScriptedAdversaryRival()))
    name = "arena-api"
    _seed_project(name)
    app = server.create_app(token=None)
    c = TestClient(app)

    # before run: graceful, mode known from config, no manifest
    pre = c.get(f"/api/projects/{name}/arena").json()
    assert pre["mode"] == "symmetric" and pre["manifest"] is None

    rm = server.RunManager()
    rm._runs[name] = {"status": "running", "summary": None, "outcomes": [],
                      "phase": None, "baseline": {}, "cost": 0.0, "best": None, "checkpoint": None}
    rm._run(name)

    post = c.get(f"/api/projects/{name}/arena").json()
    assert post["mode"] == "symmetric"
    man = post["manifest"]
    assert man["status"] == "finished"
    assert man["stop_reason"] in ("dominance", "plateau", "max_generations")
    assert man["best_a_id"] is not None
    assert isinstance(man["stable_a"], list) and len(man["stable_a"]) >= 1
    assert len(man["champions"]["A"]) >= 1


def test_meta_lists_symmetric_and_referees(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    c = TestClient(server.create_app(token=None))
    meta = c.get("/api/meta").json()
    assert "symmetric" in meta["modes"]
    assert "cegis-recognizer" in meta["referees"]


def test_create_symmetric_project_via_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    c = TestClient(server.create_app(token=None))
    cfg = {
        "project": "made-via-api", "mode": "symmetric",
        "agents": {"rival_a": {"engine": "claude", "timeout": 600},
                   "rival_b": {"engine": "codex", "timeout": 600}},
        "roles": {"rival_a": {"goal": "recognize"}, "rival_b": {"goal": "fool"}},
        "evaluation": {"adapter": "numeric", "command": "x",
                       "metrics": [{"name": "arena_fitness", "dir": "higher",
                                    "target": 1.0, "worst": 0.0}]},
        "arena": {"referee": "cegis-recognizer", "generations": 12},
        "sandbox": {"backend": "local"},
    }
    r = c.post("/api/projects/create", json=cfg)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == "made-via-api"
    # the saved config round-trips as symmetric
    got = c.get("/api/projects/made-via-api/config").json()
    assert got["mode"] == "symmetric" and got["arena"]["referee"] == "cegis-recognizer"
