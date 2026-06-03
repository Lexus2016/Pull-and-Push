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
