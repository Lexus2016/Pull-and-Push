"""P4.5 `pull-and-push onboard` — bot + data + proposal.json → runnable project (via the CLI)."""

import json
import pytest
from tyani_tolkai import cli
from tyani_tolkai.config import load_config
from tyani_tolkai.projects import project_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    return tmp_path


def _botdir(tmp_path):
    d = tmp_path / "thebot"; d.mkdir()
    (d / "bot.py").write_text("PARAMS = {'window': 14}\n", encoding="utf-8")
    return d


def _csv(tmp_path):
    p = tmp_path / "ohlcv.csv"
    rows = "\n".join(f"{i*1000},{1+i},{2+i},{0.5+i},{1.5+i},10" for i in range(20))
    p.write_text("time,open,high,low,close,volume\n" + rows + "\n", encoding="utf-8")
    return p


def _proposal_json(tmp_path, metrics=None):
    metrics = metrics if metrics is not None else [
        {"name": "return_oos_pct", "dir": "higher", "weight": 0.7, "target": 100.0,
         "rationale": "more profit", "confidence": 0.9},
        {"name": "max_drawdown_pct", "dir": "lower", "weight": 0.3, "target": 10.0,
         "rationale": "safer", "confidence": 0.8},
    ]
    p = tmp_path / "proposal.json"
    p.write_text(json.dumps({"proposer_engine": "claude", "bot_name": "mybot",
                             "goal": "maximize risk-adjusted return",
                             "proposed_metrics": metrics}), encoding="utf-8")
    return p


def test_onboard_creates_runnable_project(home, tmp_path, capsys):
    rc = cli.main(["onboard", "--bot-dir", str(_botdir(tmp_path)), "--data", str(_csv(tmp_path)),
                   "--proposal", str(_proposal_json(tmp_path)), "--name", "onb",
                   "--bot-cmd", "python bot.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "created project 'onb'" in out
    assert "run" in out                                   # prints the next step
    base = project_dir("onb")
    assert (base / "artifact" / "bot.py").exists()
    assert (base / "metrics" / "data.csv").exists()
    cfg = load_config(base / "config.yaml")
    assert cfg.evaluation.adapter == "numeric"
    assert "score-bot" in cfg.evaluation.command
    assert "--seed onb" in cfg.evaluation.command         # seed defaults to the project name
    assert {m.name for m in cfg.evaluation.metrics} == {"return_oos_pct", "max_drawdown_pct"}


def test_onboard_missing_botdir_returns_2(home, tmp_path):
    rc = cli.main(["onboard", "--bot-dir", str(tmp_path / "nope"), "--data", str(_csv(tmp_path)),
                   "--proposal", str(_proposal_json(tmp_path)), "--name", "x",
                   "--bot-cmd", "python bot.py"])
    assert rc == 2


def test_onboard_invalid_proposal_returns_2(home, tmp_path):
    bad = tmp_path / "bad.json"; bad.write_text("{not valid json", encoding="utf-8")
    rc = cli.main(["onboard", "--bot-dir", str(_botdir(tmp_path)), "--data", str(_csv(tmp_path)),
                   "--proposal", str(bad), "--name", "x", "--bot-cmd", "python bot.py"])
    assert rc == 2


def test_onboard_duplicate_name_returns_2(home, tmp_path, capsys):
    args = ["onboard", "--bot-dir", str(_botdir(tmp_path)), "--data", str(_csv(tmp_path)),
            "--proposal", str(_proposal_json(tmp_path)), "--name", "dup", "--bot-cmd", "python bot.py"]
    assert cli.main(args) == 0
    assert cli.main(args) == 2                             # second time: project exists
