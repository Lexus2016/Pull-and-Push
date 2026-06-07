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
    assert "Pull-and-Push" in r.text


def test_projects_empty(client):
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert r.json()["projects"] == []
    assert r.json()["items"] == []


def test_projects_list_has_status(client):
    client.post("/api/projects/create", json=_VALID)
    items = client.get("/api/projects").json()["items"]
    assert items and items[0]["name"] == "webtest" and "status" in items[0]


def test_create_rejects_project_without_a_scorer(client):
    # a config whose eval command names a *.py harness that doesn't exist must NOT create a
    # silently-broken project — reject and roll back (systemic: no project born non-runnable).
    bad = dict(_VALID)
    bad["project"] = "noscorer"
    bad["evaluation"] = {"adapter": "numeric",
                         "command": "python metrics/backtest.py --strategy strategy.py",
                         "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                         "target_score": 100}
    r = client.post("/api/projects/create", json=bad)
    assert r.status_code == 422
    assert "scorer" in r.json()["detail"].lower()
    # rolled back — the half-created project is gone
    assert "noscorer" not in [i["name"] for i in client.get("/api/projects").json().get("items", [])]


def test_create_allows_project_with_existing_scorer(client, tmp_path):
    # if the scorer file IS present (seed=copy brings it in), creation succeeds
    src = tmp_path / "seedsrc" / "metrics"
    src.mkdir(parents=True)
    (src / "backtest.py").write_text("print('{\"s\": 1.0}')\n")
    ok = dict(_VALID)
    ok["project"] = "withscorer"
    ok["seed"] = {"mode": "copy", "path": str(tmp_path / "seedsrc")}
    ok["evaluation"] = {"adapter": "numeric", "command": "python metrics/backtest.py",
                        "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                        "target_score": 100}
    assert client.post("/api/projects/create", json=ok).status_code == 200


def test_auth_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    c = TestClient(create_app(token="secret"))
    assert c.get("/api/projects").status_code == 401
    assert c.get("/api/projects?token=secret").status_code == 200


def test_live_no_active_run_is_idle(client):
    client.post("/api/projects/create", json=_VALID)
    r = client.get("/api/projects/webtest/live")   # no in-memory run → must not 500
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_test_eval_endpoint(client):
    client.post("/api/projects/create", json=_VALID)
    r = client.post("/api/projects/webtest/test-eval")
    assert r.status_code == 200
    assert "ok" in r.json() and "logs" in r.json()


def test_files_endpoint(client):
    client.post("/api/projects/create", json=_VALID)
    r = client.get("/api/projects/webtest/files")
    assert r.status_code == 200
    assert "files" in r.json()


def test_meta(client):
    m = client.get("/api/meta").json()
    assert "claude" in m["engines"] and "numeric" in m["adapters"]
    assert m["seeds"] == ["empty", "copy"]              # generate dropped


def test_fs_lists_directory(client, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x")
    d = client.get("/api/fs", params={"path": str(tmp_path)}).json()
    assert d["path"] == str(tmp_path)
    names = [e["name"] for e in d["entries"]]
    assert "sub" in names and "a.txt" in names
    assert d["entries"][0]["dir"] is True               # dirs sorted first


def test_create_without_worst_is_valid(client):
    cfg = dict(_VALID, project="nowrst")
    cfg["evaluation"] = {"adapter": "numeric", "command": "true", "target_score": 100,
                         "metrics": [{"name": "s", "dir": "higher", "weight": 1, "target": 100}]}
    assert client.post("/api/projects/create", json=cfg).status_code == 200
    # persisted-state payload exposes a baseline map (empty until the first run)
    st = client.get("/api/projects/nowrst").json()
    assert "baseline" in st


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


def test_agent_log_endpoint(client):
    client.post("/api/projects/create", json=_VALID)
    r = client.get("/api/projects/webtest/agent-log")
    assert r.status_code == 200 and "log" in r.json()


def test_force_stop_no_active_run(client):
    client.post("/api/projects/create", json=_VALID)
    r = client.post("/api/projects/webtest/force-stop")
    assert r.status_code == 200 and r.json()["killed"] is False   # nothing running → nothing killed


def test_webhook_fires_on_completion(monkeypatch):
    import urllib.request
    from tyani_tolkai.web import server
    from tyani_tolkai.config import Config
    captured = {}

    class _Resp:
        def close(self): pass

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cfg = Config(project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
                 evaluation={"adapter": "numeric", "command": "true",
                             "metrics": [{"name": "s", "dir": "higher", "target": 100}]},
                 notify={"enabled": True, "url": "https://hook.test/x", "method": "POST"})
    server._fire_webhook(cfg, "p", {"project": "p", "status": "finished"})
    assert captured["method"] == "POST" and b"finished" in captured["data"]


def test_webhook_skipped_when_disabled(monkeypatch):
    import urllib.request
    from tyani_tolkai.web import server
    from tyani_tolkai.config import Config
    hits = {"n": 0}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: hits.__setitem__("n", hits["n"] + 1))
    cfg = Config(project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
                 evaluation={"adapter": "numeric", "command": "true",
                             "metrics": [{"name": "s", "dir": "higher", "target": 100}]})
    server._fire_webhook(cfg, "p", {"status": "finished"})
    assert hits["n"] == 0       # disabled → never called


def test_lifecycle_endpoints(client):
    client.post("/api/projects/create", json=dict(_VALID, project="lc"))
    assert client.post("/api/projects/lc/stop").status_code == 200
    assert client.post("/api/projects/lc/reset").status_code == 200
    assert client.post("/api/projects/lc/rename?to=lc2").status_code == 200
    assert "lc2" in client.get("/api/projects").json()["projects"]
    assert client.post("/api/projects/lc2/delete").status_code == 200
    assert "lc2" not in client.get("/api/projects").json()["projects"]


def _seed_kept_iters(name, iters=2):
    """Give an existing project N kept git-committed iterations (so snapshots exist)."""
    from tyani_tolkai.state import StateStore
    from tyani_tolkai.projects import project_dir
    st = StateStore(project_dir(name))
    rid = st.create_run("asymmetric")
    hs = {}
    for n in range(1, iters + 1):
        (st.artifact_dir / "code.py").write_text(f"v{n}\n")
        h = st.commit(f"i{n}")
        hs[n] = h
        st.record_iteration(rid, n=n, git_hash=h, score=float(70 + n), verdict="keep", metrics=[])
        st.update_run(rid, best_score=float(70 + n), iter_count=n)
    st.close()
    return hs


def test_export_iter_rewind_fork(client):
    from tyani_tolkai.state import StateStore
    from tyani_tolkai.projects import project_dir
    client.post("/api/projects/create", json=dict(_VALID, project="snap"))
    hs = _seed_kept_iters("snap", 2)
    # download a specific iteration's artifact
    r = client.get("/api/projects/snap/export?iter=1")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    # a non-kept iteration cannot be snapshotted
    assert client.get("/api/projects/snap/export?iter=99").status_code == 404
    # rewind in place
    assert client.post("/api/projects/snap/rewind?iter=1").status_code == 200
    st = StateStore(project_dir("snap"))
    try:
        assert st.head() == hs[1]
    finally:
        st.close()
    # fork to a new project
    assert client.post("/api/projects/snap/fork?iter=1&to=snapfork").status_code == 200
    assert "snapfork" in client.get("/api/projects").json()["projects"]


def test_run_continues_after_finish_never_loses_iterations(client):
    """Pressing Run after a FINISHED run (plateau / max-iter / even a target win) RESUMES the same
    run — history and the iteration count carry on, previous iterations are never lost."""
    from tyani_tolkai.state import StateStore
    from tyani_tolkai.projects import project_dir
    from tyani_tolkai.web import server
    client.post("/api/projects/create", json=dict(_VALID, project="cont"))
    _seed_kept_iters("cont", 3)                       # run with 3 kept iterations
    st = StateStore(project_dir("cont"))
    try:
        rid = st.conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()["id"]
        st.set_status(rid, "finished")                # simulate a plateau / max-iter / target finish
        run_id, resuming = server._select_run_id(st)
        assert resuming is True and run_id == rid     # Run CONTINUES the finished run, not a new one
    finally:
        st.close()


def test_select_run_id_fresh_project_starts_first_run(client):
    """With no prior history at all, _select_run_id signals 'create the first run'."""
    from tyani_tolkai.state import StateStore
    from tyani_tolkai.projects import project_dir
    from tyani_tolkai.web import server
    client.post("/api/projects/create", json=dict(_VALID, project="fresh"))
    st = StateStore(project_dir("fresh"))
    try:
        run_id, resuming = server._select_run_id(st)
        assert run_id is None and resuming is False
    finally:
        st.close()


def test_select_run_id_skips_empty_zombie_run(client):
    """A newer EMPTY run (created but with no iterations) must not strand real history: resume the
    latest run that actually HAS iterations."""
    from tyani_tolkai.state import StateStore
    from tyani_tolkai.projects import project_dir
    from tyani_tolkai.web import server
    client.post("/api/projects/create", json=dict(_VALID, project="zombie"))
    _seed_kept_iters("zombie", 2)                     # run with history
    st = StateStore(project_dir("zombie"))
    try:
        real = st.conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()["id"]
        st.create_run("asymmetric")                   # newer empty zombie run
        run_id, resuming = server._select_run_id(st)
        assert resuming is True and run_id == real     # resume the run WITH history, not the zombie
    finally:
        st.close()


def test_rewind_fork_blocked_while_running(client, monkeypatch):
    client.post("/api/projects/create", json=dict(_VALID, project="busy2"))
    monkeypatch.setattr(client.app.state.runs, "is_running", lambda n: True)
    assert client.post("/api/projects/busy2/rewind?iter=1").status_code == 409
    assert client.post("/api/projects/busy2/fork?iter=1&to=x").status_code == 409


def test_export_missing_project_404_no_orphan(client):
    from tyani_tolkai.projects import projects_root
    assert client.get("/api/projects/ghost/export?iter=1").status_code == 404
    assert client.get("/api/projects/ghost/export").status_code == 404
    assert not (projects_root() / "ghost").exists()    # StateStore must not mkdir an orphan
