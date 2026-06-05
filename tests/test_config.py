import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from tyani_tolkai.config import Config, load_config

FIXTURE = Path(__file__).parent / "fixtures" / "example_config.yaml"


def test_loads_example():
    cfg = load_config(FIXTURE)
    assert cfg.project == "trading-bot-sharpe"
    assert cfg.mode == "asymmetric"
    assert cfg.agents["executor"].engine == "claude"
    assert len(cfg.evaluation.metrics) == 2
    assert cfg.evaluation.min_delta == 1.0
    assert cfg.seed.mode == "copy" and cfg.seed.path == "~/strategies/baseline"
    assert cfg.evaluation.harness_dir == "metrics/"


def test_rejects_bad_dir():
    with pytest.raises(ValidationError):
        Config(
            project="p",
            agents={"executor": {"engine": "mock"}},
            roles={"executor": {"goal": "g"}},
            evaluation={
                "adapter": "numeric",
                "metrics": [{"name": "x", "dir": "sideways", "weight": 1, "worst": 0, "target": 1}],
            },
        )


def test_rejects_empty_metrics():
    with pytest.raises(ValidationError):
        Config(
            project="p",
            agents={"executor": {"engine": "mock"}},
            roles={"executor": {"goal": "g"}},
            evaluation={"adapter": "numeric", "metrics": []},
        )


def test_seed_generate_migrates_to_empty():
    # 'generate' was an unimplemented alias; old configs must still load (→ empty)
    common = dict(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "target": 100}]},
    )
    assert Config(seed="generate", **common).seed.mode == "empty"
    assert Config(seed={"mode": "generate"}, **common).seed.mode == "empty"
    assert Config(seed={"copy": "/tmp/x"}, **common).seed.mode == "copy"   # copy shorthand still works


def test_notify_requires_url_when_enabled():
    common = dict(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "target": 100}]},
    )
    with pytest.raises(ValidationError):
        Config(notify={"enabled": True}, **common)            # enabled but no url → invalid
    c = Config(notify={"enabled": True, "url": "https://example.com/hook"}, **common)
    assert c.notify.enabled and c.notify.method == "POST"     # default method
    assert Config(**common).notify.enabled is False           # off by default, url not required


def test_metric_worst_optional():
    cfg = Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "target": 100}]},
    )
    assert cfg.evaluation.metrics[0].worst is None      # baseline-derived later
    assert cfg.evaluation.min_delta == 0.5              # system-managed default


def test_rejects_inverted_range():
    # dir=higher but target < worst
    with pytest.raises(ValidationError):
        Config(
            project="p",
            agents={"executor": {"engine": "mock"}},
            roles={"executor": {"goal": "g"}},
            evaluation={
                "adapter": "numeric",
                "metrics": [{"name": "x", "dir": "higher", "weight": 1, "worst": 5, "target": 1}],
            },
        )


def test_warns_identical_engines():
    with pytest.warns(UserWarning, match="same engine"):
        Config(
            project="p",
            agents={"executor": {"engine": "claude"}, "validator": {"engine": "claude"}},
            roles={"executor": {"goal": "g"}, "validator": {"goal": "g"}},
            evaluation={
                "adapter": "numeric",
                "command": "true",
                "metrics": [{"name": "x", "dir": "higher", "weight": 1, "worst": 0, "target": 1}],
            },
        )


def test_requires_command_for_numeric():
    with pytest.raises(ValidationError):
        Config(
            project="p",
            agents={"executor": {"engine": "mock"}},
            roles={"executor": {"goal": "g"}},
            evaluation={
                "adapter": "numeric",  # no command → must fail
                "metrics": [{"name": "x", "dir": "higher", "weight": 1, "worst": 0, "target": 1}],
            },
        )


def test_different_engines_no_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning becomes an error
        cfg = load_config(FIXTURE)  # claude vs codex → no warning
    assert cfg.agents["validator"].engine == "codex"


def test_budget_without_price_warns():
    # a budget_usd with no price (usd_per_mtok=0) silently wouldn't be enforced — warn the operator
    base = dict(project="p", agents={"executor": {"engine": "mock"}},
                roles={"executor": {"goal": "g"}},
                evaluation={"adapter": "numeric", "command": "true",
                            "metrics": [{"name": "s", "dir": "higher", "weight": 1,
                                         "worst": 0, "target": 100}]})
    with pytest.warns(UserWarning, match="usd_per_mtok"):
        Config(**base, limits={"budget_usd": 5.0})
