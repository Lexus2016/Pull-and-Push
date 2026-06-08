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
