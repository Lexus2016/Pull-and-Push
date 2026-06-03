import pytest
from fastapi.testclient import TestClient

from tyani_tolkai.web.server import create_app

_VALID = {
    "project": "webtest", "mode": "asymmetric",
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


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Тяни-Толкай" in r.text


def test_projects_empty(client):
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert r.json() == {"projects": []}


def test_demo_runs_and_converges(client):
    r = client.post("/api/demo")
    assert r.status_code == 200
    d = r.json()
    assert d["summary"]["reason"] == "target"
    assert d["summary"]["best_score"] == 100.0
    assert d["outcomes"][-1]["score"] == 100.0


def test_auth_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    c = TestClient(create_app(token="secret"))
    assert c.get("/api/projects").status_code == 401
    assert c.get("/api/projects?token=secret").status_code == 200


def test_meta(client):
    m = client.get("/api/meta").json()
    assert "claude" in m["engines"] and "numeric" in m["adapters"]


def test_create_get_update_config(client):
    r = client.post("/api/projects/create", json=_VALID)
    assert r.status_code == 200, r.text
    assert "webtest" in client.get("/api/projects").json()["projects"]

    cfg = client.get("/api/projects/webtest/config").json()
    assert cfg["project"] == "webtest"
    assert cfg["evaluation"]["target_score"] == 100

    cfg["evaluation"]["target_score"] = 80
    assert client.put("/api/projects/webtest/config", json=cfg).status_code == 200
    assert client.get("/api/projects/webtest/config").json()["evaluation"]["target_score"] == 80


def test_create_invalid_rejected(client):
    bad = dict(_VALID, project="badp")
    bad["evaluation"] = {"adapter": "numeric",  # no command → invalid
                         "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}]}
    assert client.post("/api/projects/create", json=bad).status_code == 422


def test_configure_endpoint(client, monkeypatch):
    import tyani_tolkai.configurator as conf
    monkeypatch.setattr(conf, "generate_config", lambda *a, **k: dict(_VALID))
    r = client.post("/api/configure", json={"description": "do something useful"})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["config"]["project"] == "webtest"


def test_configure_requires_description(client):
    assert client.post("/api/configure", json={}).status_code == 400


def test_rename_conflict_and_missing(client):
    client.post("/api/projects/create", json=dict(_VALID, project="a"))
    client.post("/api/projects/create", json=dict(_VALID, project="b"))
    assert client.post("/api/projects/a/rename?to=b").status_code == 409   # target exists
    assert client.post("/api/projects/zzz/rename?to=q").status_code == 404  # source missing
    assert client.post("/api/projects/zzz/reset").status_code == 404


def test_path_traversal_rejected(client):
    assert client.post("/api/projects/create", json=dict(_VALID, project="../evil")).status_code == 400
    client.post("/api/projects/create", json=_VALID)
    assert client.post("/api/projects/webtest/rename?to=../x").status_code == 400


def test_command_role_validated(client):
    client.post("/api/projects/create", json=_VALID)
    assert client.post("/api/projects/webtest/command?role=../etc&text=hi").status_code == 400


def test_lifecycle_blocked_while_running(client, monkeypatch):
    client.post("/api/projects/create", json=dict(_VALID, project="busy"))
    monkeypatch.setattr(client.app.state.runs, "is_running", lambda n: True)
    assert client.post("/api/projects/busy/delete").status_code == 409
    assert client.post("/api/projects/busy/reset").status_code == 409


def test_lifecycle_endpoints(client):
    client.post("/api/projects/create", json=dict(_VALID, project="lc"))
    assert client.post("/api/projects/lc/stop").status_code == 200
    assert client.post("/api/projects/lc/reset").status_code == 200
    assert client.post("/api/projects/lc/rename?to=lc2").status_code == 200
    assert "lc2" in client.get("/api/projects").json()["projects"]
    assert client.post("/api/projects/lc2/delete").status_code == 200
    assert "lc2" not in client.get("/api/projects").json()["projects"]
