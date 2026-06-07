"""P5 backend: web endpoints exposing the onboarding backend (gen-adapter, check-adapter, onboard)."""

import sys
import pytest
from fastapi.testclient import TestClient

from tyani_tolkai.web.server import create_app
from tyani_tolkai.config import load_config
from tyani_tolkai.projects import project_dir
from tyani_tolkai.profile_schema import BotProfile, EntryPoint
from tyani_tolkai.proposal_schema import MetricProposal, ProposedMetric


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    return TestClient(create_app(token=None))


_MOMENTUM = '''import sys, json
hist = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); t = m.get("type")
    if t == "init": print(json.dumps({"type": "ready"}), flush=True)
    elif t == "bar":
        hist.append(m["c"]); w = 1 if len(hist) >= 2 and hist[-1] > hist[-2] else 0
        print(json.dumps({"type": "order", "n": m["n"], "want": w}), flush=True)
    elif t == "end": break
'''
_FLAT = _MOMENTUM.replace('w = 1 if len(hist) >= 2 and hist[-1] > hist[-2] else 0', 'w = 0')
_BROKEN = _MOMENTUM.replace('"want": w', '"want": 5')


def _botdir(tmp_path, bot_src=None, name="bot.py"):
    d = tmp_path / "thebot"; d.mkdir(exist_ok=True)
    (d / name).write_text(bot_src if bot_src is not None else "PARAMS = {'w': 1}\n", encoding="utf-8")
    return d


def _csv(tmp_path):
    p = tmp_path / "ohlcv.csv"
    rows = "\n".join(f"{i*1000},{1+i},{2+i},{0.5+i},{1.5+i},10" for i in range(20))
    p.write_text("time,open,high,low,close,volume\n" + rows + "\n", encoding="utf-8")
    return p


def _proposal():
    return {"proposer_engine": "claude", "bot_name": "mybot", "goal": "max risk-adjusted return",
            "proposed_metrics": [
                {"name": "return_oos_pct", "dir": "higher", "weight": 0.7, "target": 100.0,
                 "rationale": "profit", "confidence": 0.9},
                {"name": "max_drawdown_pct", "dir": "lower", "weight": 0.3, "target": 10.0,
                 "rationale": "risk", "confidence": 0.8}]}


# ---------------- /api/gen-adapter ----------------

def test_gen_adapter_writes_stub(client, tmp_path):
    d = _botdir(tmp_path)
    r = client.post("/api/gen-adapter", json={"bot_dir": str(d)})
    assert r.status_code == 200
    assert (d / "adapter.py").exists()
    assert "def decide(" in (d / "adapter.py").read_text(encoding="utf-8")


def test_gen_adapter_missing_dir_422(client, tmp_path):
    assert client.post("/api/gen-adapter", json={"bot_dir": str(tmp_path / "nope")}).status_code == 422


def test_gen_adapter_refuses_overwrite(client, tmp_path):
    d = _botdir(tmp_path)
    assert client.post("/api/gen-adapter", json={"bot_dir": str(d)}).status_code == 200
    assert client.post("/api/gen-adapter", json={"bot_dir": str(d)}).status_code == 409
    assert client.post("/api/gen-adapter", json={"bot_dir": str(d), "force": True}).status_code == 200


# ---------------- /api/check-adapter ----------------

def test_check_adapter_pass_on_momentum(client, tmp_path):
    d = _botdir(tmp_path, _MOMENTUM)
    r = client.post("/api/check-adapter", json={
        "bot_dir": str(d), "bot_cmd": f"{sys.executable} {d / 'bot.py'}", "n": 16,
        "per_read_timeout": 30, "total_timeout": 60})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_check_adapter_flags_flat(client, tmp_path):
    d = _botdir(tmp_path, _FLAT)
    j = client.post("/api/check-adapter", json={
        "bot_dir": str(d), "bot_cmd": f"{sys.executable} {d / 'bot.py'}", "n": 16,
        "per_read_timeout": 30, "total_timeout": 60}).json()
    assert j["ok"] is False and j["degenerate"] is True


def test_check_adapter_protocol_violation(client, tmp_path):
    d = _botdir(tmp_path, _BROKEN)
    j = client.post("/api/check-adapter", json={
        "bot_dir": str(d), "bot_cmd": f"{sys.executable} {d / 'bot.py'}", "n": 16,
        "per_read_timeout": 30, "total_timeout": 60}).json()
    assert j["ok"] is False and any("protocol" in r.lower() for r in j["reasons"])


# ---------------- /api/onboard ----------------

def test_onboard_creates_project(client, tmp_path):
    d = _botdir(tmp_path)
    r = client.post("/api/onboard", json={
        "project": "webonb", "bot_dir": str(d), "data": str(_csv(tmp_path)),
        "proposal": _proposal(), "bot_cmd": "python bot.py"})
    assert r.status_code == 200
    base = project_dir("webonb")
    assert (base / "artifact" / "bot.py").exists() and (base / "metrics" / "data.csv").exists()
    cfg = load_config(base / "config.yaml")
    assert "score-bot" in cfg.evaluation.command
    assert {m.name for m in cfg.evaluation.metrics} == {"return_oos_pct", "max_drawdown_pct"}


def test_onboard_missing_data_422(client, tmp_path):
    d = _botdir(tmp_path)
    r = client.post("/api/onboard", json={
        "project": "x", "bot_dir": str(d), "data": str(tmp_path / "no.csv"),
        "proposal": _proposal(), "bot_cmd": "python bot.py"})
    assert r.status_code == 422


def test_onboard_duplicate_409(client, tmp_path):
    d = _botdir(tmp_path)
    body = {"project": "dupweb", "bot_dir": str(d), "data": str(_csv(tmp_path)),
            "proposal": _proposal(), "bot_cmd": "python bot.py"}
    assert client.post("/api/onboard", json=body).status_code == 200
    assert client.post("/api/onboard", json=body).status_code == 409


# ---------------- /api/profile + /api/propose (LLM, mocked at the module boundary) ----------------

def _canned_profile():
    return BotProfile(analyzer_engine="claude", bot_name="mybot", source_root=".", language="python",
                      framework="custom",
                      entry_point=EntryPoint(kind="function", location="bot.py:decide",
                                             inputs="OHLCV", outputs="+1/-1", confidence=0.8))


def _canned_proposal():
    return MetricProposal(proposer_engine="claude", bot_name="mybot", goal="max return",
                          proposed_metrics=[ProposedMetric(name="return_oos_pct", dir="higher",
                                                           weight=1.0, target=100.0,
                                                           rationale="profit", confidence=0.9)])


def test_profile_returns_profile_and_markdown(client, tmp_path, monkeypatch):
    monkeypatch.setattr("tyani_tolkai.profiler.analyze_bot", lambda *a, **k: _canned_profile())
    d = _botdir(tmp_path)
    r = client.post("/api/profile", json={"bot_dir": str(d)})
    assert r.status_code == 200
    j = r.json()
    assert j["profile"]["bot_name"] == "mybot" and j["profile"]["language"] == "python"
    assert "markdown" in j and j["markdown"]


def test_profile_missing_path_422(client, tmp_path):
    assert client.post("/api/profile", json={"bot_dir": str(tmp_path / "nope")}).status_code == 422


def test_propose_returns_proposal(client, monkeypatch):
    monkeypatch.setattr("tyani_tolkai.proposer.propose_evaluation", lambda *a, **k: _canned_proposal())
    r = client.post("/api/propose", json={"profile": _canned_profile().model_dump(),
                                          "goal": "maximize risk-adjusted return"})
    assert r.status_code == 200
    j = r.json()
    assert [m["name"] for m in j["proposal"]["proposed_metrics"]] == ["return_oos_pct"]
    assert "markdown" in j


def test_propose_requires_goal(client):
    assert client.post("/api/propose", json={"profile": _canned_profile().model_dump()}).status_code == 400


# ---------------- /api/validate (trusted subprocess path → no Docker needed) ----------------

def test_validate_trusted_runs_and_reports(client, tmp_path):
    d = _botdir(tmp_path, _MOMENTUM)
    r = client.post("/api/validate", json={
        "bot_dir": str(d), "data": str(_csv(tmp_path)), "bot_cmd": f"{sys.executable} {d / 'bot.py'}",
        "seed": "proj-x", "trusted": True, "per_read_timeout": 30, "total_timeout": 60})
    assert r.status_code == 200
    j = r.json()
    assert j["verdict"] in ("PASS", "FLAG")
    assert j["report"].startswith("# Evidence Report")
    assert "process-separation only" in j["isolation"]


def test_validate_missing_data_422(client, tmp_path):
    d = _botdir(tmp_path, _MOMENTUM)
    r = client.post("/api/validate", json={
        "bot_dir": str(d), "data": str(tmp_path / "no.csv"),
        "bot_cmd": f"{sys.executable} {d / 'bot.py'}", "trusted": True})
    assert r.status_code == 422


def test_validate_untrusted_without_docker_409(client, tmp_path, monkeypatch):
    monkeypatch.setattr("tyani_tolkai.bot_sandbox.docker_available", lambda: False)
    d = _botdir(tmp_path, _MOMENTUM)
    r = client.post("/api/validate", json={
        "bot_dir": str(d), "data": str(_csv(tmp_path)), "bot_cmd": f"{sys.executable} {d / 'bot.py'}"})
    assert r.status_code == 409
