import yaml

from tyani_tolkai import cli
from tyani_tolkai.agents.scripted_rival import ScriptedAdversaryRival, ScriptedRecognizerRival


def _write_cfg(tmp_path):
    cfg = {
        "project": "toy-arena", "mode": "symmetric",
        "agents": {"rival_a": {"engine": "mock"}, "rival_b": {"engine": "mock"}},
        "roles": {"rival_a": {"goal": "recognize"}, "rival_b": {"goal": "fool"}},
        "evaluation": {"adapter": "numeric", "command": "x",
                       "metrics": [{"name": "arena_fitness", "dir": "higher",
                                    "target": 1.0, "worst": 0.0}]},
        "arena": {"referee": "cegis-recognizer", "generations": 6,
                  "per_generation_iterations": 2, "dominance_tau": 0.99, "dominance_rounds": 1},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_cli_run_symmetric_converges(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_build_rivals",
                        lambda cfg: (ScriptedRecognizerRival(), ScriptedAdversaryRival()))
    cfg_path = _write_cfg(tmp_path)
    rc = cli.main(["run", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "symmetric run" in out
    assert "best-A-vs-all-B" in out
    assert "finished: reason=" in out


def test_cli_run_symmetric_scripted_engine_offline(tmp_path, monkeypatch, capsys):
    """The 'scripted' engine (the offline Co-Evolution Arena demo) runs with NO monkeypatch and
    NO API — _build_rivals resolves it to the deterministic CEGIS rivals."""
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    cfg = {
        "project": "arena-demo", "mode": "symmetric",
        "agents": {"rival_a": {"engine": "scripted"}, "rival_b": {"engine": "scripted"}},
        "roles": {"rival_a": {"goal": "recognize"}, "rival_b": {"goal": "fool"}},
        "evaluation": {"adapter": "numeric", "command": "x",
                       "metrics": [{"name": "arena_fitness", "dir": "higher", "target": 1.0, "worst": 0.0}]},
        "arena": {"referee": "cegis-recognizer", "generations": 6,
                  "per_generation_iterations": 2, "dominance_tau": 0.99, "dominance_rounds": 1},
        "sandbox": {"backend": "local"},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    rc = cli.main(["run", str(p)])              # no monkeypatch — real scripted-engine path
    assert rc == 0
    out = capsys.readouterr().out
    assert "best-A-vs-all-B" in out and "finished: reason=" in out
