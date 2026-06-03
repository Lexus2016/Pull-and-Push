"""Configuration models and loader for a Тяни-Толкай run.

A run is described by a single ``config.yaml`` (see spec §15). Everything the
orchestrator needs — which agents play which role, how the artifact is scored,
the limits and stop conditions — lives here as validated, typed models.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class AgentCfg(BaseModel):
    engine: str                      # adapter name: mock | claude | codex | opencode | agy
    model: str | None = None
    timeout: int = 600               # seconds per agent run


class RoleCfg(BaseModel):
    goal: str
    task: str | None = None


class MetricCfg(BaseModel):
    name: str
    dir: Literal["higher", "lower"]
    weight: float = Field(gt=0)
    worst: float                     # maps to 0
    target: float                    # maps to 100

    @model_validator(mode="after")
    def _check_range(self) -> "MetricCfg":
        if self.worst == self.target:
            raise ValueError(f"metric {self.name!r}: worst and target must differ")
        if self.dir == "higher" and self.target < self.worst:
            raise ValueError(f"metric {self.name!r}: dir=higher needs target > worst")
        if self.dir == "lower" and self.target > self.worst:
            raise ValueError(f"metric {self.name!r}: dir=lower needs target < worst")
        return self


class EvaluationCfg(BaseModel):
    adapter: Literal["numeric", "command-exit", "pytest-pass", "rubric-llm", "custom"]
    command: str | None = None
    metrics: list[MetricCfg] = Field(min_length=1)
    target_score: float = 100.0
    runs: int = 1
    min_delta: float = 0.0           # keep only if score gain >= this (noise band)
    harness_dir: str | None = None


class LimitsCfg(BaseModel):
    max_iterations: int = 40
    plateau_N: int = 8
    budget_usd: float | None = None
    step_seconds: int = 600


class HistoryCfg(BaseModel):
    depth_k: int = 6


class CheckpointsCfg(BaseModel):
    on_plateau: bool = False
    on_target: bool = False
    every_n: int | None = None
    manual: bool = True


class SandboxCfg(BaseModel):
    backend: Literal["local", "docker"] = "local"
    memory: str | None = None
    cpus: float | None = None
    network: str = "none"


class SeedCfg(BaseModel):
    mode: Literal["empty", "copy", "generate"] = "empty"
    path: str | None = None


class Config(BaseModel):
    project: str
    mode: Literal["asymmetric", "symmetric"] = "asymmetric"
    agents: dict[str, AgentCfg]
    roles: dict[str, RoleCfg]
    seed: SeedCfg = SeedCfg()
    evaluation: EvaluationCfg
    limits: LimitsCfg = LimitsCfg()
    history: HistoryCfg = HistoryCfg()
    checkpoints: CheckpointsCfg = CheckpointsCfg()
    sandbox: SandboxCfg = SandboxCfg()

    @model_validator(mode="before")
    @classmethod
    def _normalize_seed(cls, data):
        if isinstance(data, dict) and "seed" in data:
            raw = data["seed"]
            if isinstance(raw, str):
                data["seed"] = {"mode": raw}
            elif isinstance(raw, dict) and "copy" in raw:
                data["seed"] = {"mode": "copy", "path": raw["copy"]}
        return data

    @model_validator(mode="after")
    def _check_roles_and_engines(self) -> "Config":
        if "executor" not in self.agents:
            raise ValueError("agents.executor is required")
        if self.mode == "asymmetric" and "validator" in self.agents:
            ex = self.agents["executor"].engine
            va = self.agents["validator"].engine
            if ex == va:
                warnings.warn(
                    f"executor and validator use the same engine ({ex!r}); "
                    "anti-collusion lock #3 recommends different providers",
                    UserWarning,
                    stacklevel=2,
                )
        return self


def load_config(path: str | Path) -> Config:
    """Load and validate a run config from a YAML file.

    Phase 1 supports asymmetric mode only; symmetric raises NotImplementedError.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cfg = Config(**data)
    if cfg.mode != "asymmetric":
        raise NotImplementedError("symmetric mode arrives in Phase 2")
    return cfg
