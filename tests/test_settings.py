"""Machine-wide settings: the scorer Python, the update check, diagnostics, run-end events."""

import json
import sys

import pytest
from fastapi.testclient import TestClient

from tyani_tolkai import settings as st
from tyani_tolkai.sandbox import LocalBackend
from tyani_tolkai.web.server import create_app


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(st.PYTHON_ENV, raising=False)
    monkeypatch.delenv("PULL_AND_PUSH_APP", raising=False)


def test_scorer_python_precedence(monkeypatch):
    assert LocalBackend().python == sys.executable.replace("\\", "/")
    st.save({"scorer_python": sys.executable})
    assert st.load()["scorer_python"] == sys.executable
    monkeypatch.setenv(st.PYTHON_ENV, "/env/python")
    assert LocalBackend().python == "/env/python"                    # env beats the setting
    st.save({"scorer_python": ""})
    monkeypatch.delenv(st.PYTHON_ENV)
    assert st.load()["scorer_python"] is None and LocalBackend().python == sys.executable.replace("\\", "/")


def test_settings_reject_a_broken_python_and_unknown_keys(tmp_path):
    with pytest.raises(ValueError):
        st.save({"scorer_python": str(tmp_path / "no-such-python")})
    with pytest.raises(ValueError, match="unknown"):
        st.save({"colour": "red"})
    assert st.python_info(sys.executable)["ok"] and st.python_info(sys.executable)["version"]


def test_numeric_scorer_runs_on_the_chosen_python(tmp_path):
    # {python} in the eval command must follow the setting — the whole point of choosing one
    from tyani_tolkai.metrics.numeric import NumericAdapter
    from tyani_tolkai.config import EvaluationCfg, MetricCfg
    (tmp_path / "ev.py").write_text("import json, sys; print(json.dumps({'m': len(sys.executable)}))")
    ev = EvaluationCfg(adapter="numeric", command="{python} ev.py",
                       metrics=[MetricCfg(name="m", dir="higher", target=1000, weight=1)])
    res = NumericAdapter().run(tmp_path, LocalBackend(), ev, 30)
    assert res.ok and res.metrics[0]["value"] == len(sys.executable)


def test_update_check_is_cached_and_compares_versions():
    calls = []

    def fetch():
        calls.append(1)
        return {"tag_name": "v9.9.9", "html_url": "https://example/rel"}
    r = st.latest_release(fetch=fetch)
    assert r["tag"] == "v9.9.9" and st.latest_release(fetch=fetch)["tag"] == "v9.9.9"
    assert len(calls) == 1                                           # a day's cache
    assert st.newer("v0.10.0", "0.9.9") and not st.newer("v0.3.0", "0.3.0") and not st.newer(None, "1")
    meta = TestClient(create_app(token=None)).get("/api/meta").json()
    assert meta["update"] == {"latest": "v9.9.9", "url": "https://example/rel"}
    st.save({"update_check": False})
    assert st.latest_release(fetch=fetch) is None and st.cached_release() is None


def test_no_update_check_inside_the_app(monkeypatch):
    monkeypatch.setenv("PULL_AND_PUSH_APP", "0.4.0")                 # Sparkle updates the app
    assert st.latest_release(fetch=lambda: 1 / 0) is None
    st.latest_release(fetch=lambda: {"tag_name": "v9"})              # no fetch, no cache


def test_offline_update_check_is_quiet():
    def boom():
        raise OSError("offline")
    assert st.latest_release(fetch=boom) is None


def test_settings_and_diagnostics_endpoints(monkeypatch):
    c = TestClient(create_app(token=None))
    s = c.get("/api/settings").json()
    assert s["settings"] == st.DEFAULTS and s["scorer_python"]["ok"]
    assert any(x["path"] for x in s["candidates"])
    assert c.put("/api/settings", json={"scorer_python": "/nope/python"}).status_code == 422
    assert c.put("/api/settings", json={"update_check": False}).json()["settings"]["update_check"] is False
    monkeypatch.setattr("tyani_tolkai.web.server._tool_versions",
                        lambda names, refresh=False: {n: {"path": None, "version": None} for n in names})
    d = c.get("/api/diagnostics").json()
    assert set(d["agents"]) == {"claude", "codex", "opencode", "agy"}
    assert d["scorer_python"]["source"] == "default" and d["update"] is None
    assert d["security"] == {"token": False, "loopback_only": False}


def test_run_end_events():
    app = create_app(token=None)
    rm = app.state.runs
    rm.ended("a", "finished", reason="target", best_score=91.0, target=90)
    rm.ended("b", "error", reason="crash", error="boom")
    c = TestClient(app)
    ev = c.get("/api/runs/events").json()
    assert ev["seq"] == 2 and [e["project"] for e in ev["events"]] == ["a", "b"]
    assert c.get("/api/runs/events?since=1").json()["events"][0]["reason"] == "crash"
    assert c.get("/api/runs/events?since=2").json()["events"] == []
