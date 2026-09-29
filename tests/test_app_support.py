"""Server support for the macOS app: Host/Origin guard, the dashboard marker, port auto, shutdown."""

import json
import os
import socket
import stat

import pytest
from fastapi.testclient import TestClient

from tyani_tolkai import cli
from tyani_tolkai import research as rs
from tyani_tolkai.projects import (DASHBOARD_MARKER, clear_dashboard_marker, home_root,
                                   read_dashboard_marker, write_dashboard_marker)
from tyani_tolkai.web.server import LOOPBACK_HOSTS, RunManager, _hostname, create_app


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))


def _client(**kw):
    return TestClient(create_app(token=None, **kw), base_url="http://127.0.0.1:8765")


def test_hostname_parsing():
    assert _hostname("127.0.0.1:8765") == "127.0.0.1"
    assert _hostname("LOCALHOST") == "localhost"
    assert _hostname("[::1]:8765") == "::1"
    assert _hostname("evil.example:8765") == "evil.example"


def test_loopback_dashboard_refuses_foreign_host_headers():
    # DNS rebinding: a page on evil.example whose name now resolves to 127.0.0.1 is same-origin
    # with the API — only its Host header gives it away
    c = _client(allowed_hosts=LOOPBACK_HOSTS)
    assert c.get("/api/projects").status_code == 200
    assert c.get("/api/projects", headers={"host": "localhost:8765"}).status_code == 200
    assert c.get("/api/projects", headers={"host": "evil.example:8765"}).status_code == 403
    # a dashboard deliberately exposed on the network does not check hosts
    assert _client().get("/api/projects", headers={"host": "box.lan:8765"}).status_code == 200


def test_cross_origin_writes_are_refused():
    c = _client()
    evil = {"origin": "https://evil.example"}
    # a body-less POST is a "simple" request a browser sends cross-site without a preflight
    assert c.post("/api/projects/x/run", headers=evil).status_code == 403
    assert c.post("/api/projects/x/run", headers={"origin": "null"}).status_code == 403
    assert c.delete("/api/projects/x", headers=evil).status_code == 403
    same = {"origin": "http://127.0.0.1:8765"}
    assert c.post("/api/projects/x/run", headers=same).status_code != 403
    assert c.post("/api/projects/x/run").status_code != 403          # CLI clients send no Origin
    assert c.get("/api/projects", headers=evil).status_code == 200   # reads are not side effects


def test_meta_reports_which_agent_clis_are_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda e: "/bin/x" if e == "codex" else None)
    inst = _client().get("/api/meta").json()["installed"]
    assert inst == {"claude": False, "codex": True, "opencode": False, "agy": False}


def test_shutdown_kills_live_agents_and_runs_hooks():
    killed = []

    class Orch:
        def __init__(self, n):
            self.n = n

        def force_kill(self):
            killed.append(self.n)
    app = create_app(token=None)
    app.state.runs._runs = {"a": {"orch": Orch("a")}, "b": {"orch": Orch("b")}}
    hooks = []
    app.state.shutdown_hooks.append(lambda: hooks.append("done"))
    with TestClient(app):            # the context manager runs the lifespan: startup → shutdown
        pass
    assert sorted(killed) == ["a", "b"] and hooks == ["done"]
    assert RunManager().shutdown() is None                           # nothing running: no-op


def test_dashboard_marker_roundtrip_is_owner_only():
    f = write_dashboard_marker("http://127.0.0.1:9000", "tok")
    if os.name != "nt":                  # Windows has no POSIX modes (the profile dir's ACL applies)
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert read_dashboard_marker() == {"url": "http://127.0.0.1:9000", "token": "tok",
                                       "pid": os.getpid()}
    clear_dashboard_marker()
    assert not f.exists() and read_dashboard_marker() is None


def test_marker_of_a_dead_or_other_dashboard(monkeypatch):
    f = home_root() / DASHBOARD_MARKER
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"url": "http://127.0.0.1:9000", "token": None, "pid": 1}))
    monkeypatch.setattr("tyani_tolkai.projects.pid_alive", lambda pid: False)
    assert read_dashboard_marker() is None                           # its process is gone
    clear_dashboard_marker()
    assert f.exists()                                                # not ours: left alone
    f.write_text("{broken")
    assert read_dashboard_marker() is None


def test_research_start_uses_the_running_dashboard(monkeypatch, capsys):
    write_dashboard_marker("http://127.0.0.1:9123", "apptoken")
    seen = {}
    monkeypatch.setattr(rs, "start_via_dashboard",
                        lambda name, url, token: seen.update(name=name, url=url, token=token))
    monkeypatch.delenv("TYANI_TOLKAI_WEB_PASSWORD", raising=False)
    assert cli.main(["research", "start", "p1"]) == 0
    assert seen == {"name": "p1", "url": "http://127.0.0.1:9123", "token": "apptoken"}
    # an explicit other --url never receives the app's token
    assert cli.main(["research", "start", "p1", "--url", "http://127.0.0.1:1"]) == 0
    assert seen["url"] == "http://127.0.0.1:1" and seen["token"] is None


def test_web_socket_auto_prefers_8765_then_any_free_port():
    busy = socket.socket()
    try:
        busy.bind(("127.0.0.1", 8765))
        busy.listen(1)
    except OSError:
        busy.close()
        busy = None                                                  # already taken: same case
    s = cli._web_socket("127.0.0.1", "auto")
    try:
        assert s.getsockname()[1] != 8765 and s.getsockname()[1] > 0
    finally:
        s.close()
    with pytest.raises(OSError):                                     # an explicit busy port fails
        cli._web_socket("127.0.0.1", "8765")
    if busy:
        busy.close()


def test_dashboard_assets_are_vendored_and_served():
    # offline-capable, no third-party requests: every script/style/font comes from /static/vendor
    import re
    from tyani_tolkai.web.server import STATIC
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r'(?:src|href)="https?://', html)
    c = _client()
    refs = re.findall(r'(?:src|href)="(static/vendor/[^"]+)"', html)
    assert len(refs) == 6
    for ref in refs:
        assert c.get("/" + ref).status_code == 200, ref
    fonts = re.findall(r"url\(([^)]+)\)", c.get("/static/vendor/fonts/fonts.css").text)
    assert fonts and all(c.get("/static/vendor/fonts/" + f).status_code == 200 for f in set(fonts))


def test_active_runs_endpoint():
    app = create_app(token="t")
    app.state.runs._runs = {"a": {"status": "running"}, "b": {"status": "finished"},
                            "c": {"status": "running"}}
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/runs/active").status_code == 401
    assert c.get("/api/runs/active?token=t").json() == {"active": ["a", "c"]}


def test_second_dashboard_on_the_same_data_refuses(monkeypatch, capsys):
    import subprocess
    import sys
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        f = home_root() / DASHBOARD_MARKER
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "x", "pid": other.pid}))
        bound = []
        monkeypatch.setattr(cli, "_web_socket", lambda *a: bound.append(a))
        monkeypatch.setattr(cli, "_dashboard_answers", lambda live: True)
        assert cli.main(["web", "--port", "auto"]) == 3
        assert "already running" in capsys.readouterr().err and not bound
        # a marker whose process lives but serves no dashboard (a reused PID) does not block
        monkeypatch.setattr(cli, "_dashboard_answers", lambda live: False)
        monkeypatch.setattr(cli, "_web_socket", lambda *a: (_ for _ in ()).throw(OSError("bound")))
        with pytest.raises(OSError, match="bound"):
            cli.main(["web", "--port", "auto"])
    finally:
        other.kill()
