"""P4.5 onboarding integration: an existing bot + data + P2 proposal → a runnable project that
optimizes the bot through the vetted score-bot numeric adapter (docs/design/p4.5-...md)."""

import pytest

from tyani_tolkai import scaffold
from tyani_tolkai.config import load_config
from tyani_tolkai.proposal_schema import MetricProposal, ProposedMetric


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    return tmp_path


def _proposal(metrics=None, goal="maximize risk-adjusted return"):
    metrics = metrics if metrics is not None else [
        ProposedMetric(name="return_oos_pct", dir="higher", weight=0.7, target=100.0,
                       rationale="more profit is better", confidence=0.9),
        ProposedMetric(name="max_drawdown_pct", dir="lower", weight=0.3, target=10.0,
                       rationale="shallower drawdowns are safer", confidence=0.8),
    ]
    return MetricProposal(proposer_engine="claude", bot_name="mybot", goal=goal,
                          proposed_metrics=metrics)


def _botdir(tmp_path):
    d = tmp_path / "thebot"; d.mkdir()
    (d / "bot.py").write_text("PARAMS = {'window': 14}\n", encoding="utf-8")
    return d


def _csv(tmp_path):
    p = tmp_path / "ohlcv.csv"
    rows = "\n".join(f"{i*1000},{1+i},{2+i},{0.5+i},{1.5+i},10" for i in range(20))
    p.write_text("time,open,high,low,close,volume\n" + rows + "\n", encoding="utf-8")
    return p


# ---------------- build_onboarding_config (pure) ----------------

def test_build_onboarding_config_maps_proposal_metrics_and_wires_score_bot():
    cfg = scaffold.build_onboarding_config("proj", proposal=_proposal(), bot_cmd="python bot.py",
                                           seed_token="proj")
    ev = cfg["evaluation"]
    assert ev["adapter"] == "numeric"
    # metrics carried over verbatim (name, dir, weight, target)
    names = {m["name"]: m for m in ev["metrics"]}
    assert names["return_oos_pct"]["dir"] == "higher" and names["return_oos_pct"]["weight"] == 0.7
    assert names["max_drawdown_pct"]["dir"] == "lower" and names["max_drawdown_pct"]["target"] == 10.0
    # score-bot wiring: live bot (.), read-only data sibling, hidden seed, portable {python} -m
    cmd = ev["command"]
    assert "{python} -m tyani_tolkai.cli score-bot" in cmd
    assert "--bot-dir ." in cmd
    assert "../metrics/data.csv" in cmd
    assert '--bot-cmd "python bot.py"' in cmd
    assert "--seed proj" in cmd
    # local backend avoids docker-in-docker (score-bot does the bot's docker isolation itself)
    assert cfg["sandbox"]["backend"] == "local"
    assert cfg["evaluation"]["target_score"] == 100


def test_build_onboarding_config_validates_as_a_real_config():
    cfg = scaffold.build_onboarding_config("p", proposal=_proposal(), bot_cmd="python bot.py",
                                           seed_token="p")
    from tyani_tolkai.config import Config
    Config(**cfg)                                       # must not raise


def test_build_onboarding_config_rejects_empty_metrics():
    with pytest.raises(ValueError):
        scaffold.build_onboarding_config("p", proposal=_proposal(metrics=[]),
                                         bot_cmd="python bot.py", seed_token="p")


def test_build_onboarding_config_seed_token_defaults_to_name_via_scaffold(home, tmp_path):
    # seed_token flows into the command; when omitted at the scaffold layer it defaults to the name
    res = scaffold.scaffold_onboarding("seedproj", bot_dir=_botdir(tmp_path), data_path=_csv(tmp_path),
                                       proposal=_proposal(), bot_cmd="python bot.py")
    cfg = load_config(scaffold.project_dir("seedproj") / "config.yaml")
    assert "--seed seedproj" in cfg.evaluation.command


# ---------------- scaffold_onboarding (project on disk) ----------------

def test_scaffold_onboarding_creates_runnable_project(home, tmp_path):
    res = scaffold.scaffold_onboarding("onb", bot_dir=_botdir(tmp_path), data_path=_csv(tmp_path),
                                       proposal=_proposal(), bot_cmd="python bot.py", seed_token="tok")
    base = scaffold.project_dir("onb")
    # bot landed in the artifact (editable surface)
    assert (base / "artifact" / "bot.py").exists()
    # data landed OUTSIDE the artifact (executor/bot cannot peek the held-out tail)
    assert (base / "metrics" / "data.csv").exists()
    assert not (base / "artifact" / "data.csv").exists()
    # config is present and valid
    cfg = load_config(base / "config.yaml")
    assert cfg.evaluation.adapter == "numeric"
    assert "score-bot" in cfg.evaluation.command and "--seed tok" in cfg.evaluation.command
    assert {m.name for m in cfg.evaluation.metrics} == {"return_oos_pct", "max_drawdown_pct"}
    assert res["metrics"] == "metrics"


def test_scaffold_onboarding_duplicate_name_raises(home, tmp_path):
    scaffold.scaffold_onboarding("dup", bot_dir=_botdir(tmp_path), data_path=_csv(tmp_path),
                                 proposal=_proposal(), bot_cmd="python bot.py")
    with pytest.raises(FileExistsError):
        scaffold.scaffold_onboarding("dup", bot_dir=_botdir(tmp_path), data_path=_csv(tmp_path),
                                     proposal=_proposal(), bot_cmd="python bot.py")


def test_scaffold_onboarding_missing_inputs_raise(home, tmp_path):
    with pytest.raises(FileNotFoundError):
        scaffold.scaffold_onboarding("m1", bot_dir=tmp_path / "nope", data_path=_csv(tmp_path),
                                     proposal=_proposal(), bot_cmd="python bot.py")
    with pytest.raises(FileNotFoundError):
        scaffold.scaffold_onboarding("m2", bot_dir=_botdir(tmp_path), data_path=tmp_path / "no.csv",
                                     proposal=_proposal(), bot_cmd="python bot.py")
