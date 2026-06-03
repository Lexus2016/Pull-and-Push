import pytest
from fastapi.testclient import TestClient

from tyani_tolkai.web.server import create_app


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
