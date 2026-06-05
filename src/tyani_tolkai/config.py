"""Configuration models and loader for a Pull-and-Push run.

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
    weight: float = Field(default=1.0, gt=0)
    worst: float | None = None       # value mapped to score 0; None → auto-set from the
    #                                   first measured (baseline) value, so the user never
    #                                   has to invent a zero-point (orchestrator fills it).
    target: float                    # maps to 100

    @model_validator(mode="after")
    def _check_range(self) -> "MetricCfg":
        if self.worst is None:        # baseline-derived later; nothing to validate yet
            return self
        if self.worst == self.target:
            raise ValueError(f"metric {self.name!r}: worst and target must differ")
        if self.dir == "higher" and self.target < self.worst:
            raise ValueError(f"metric {self.name!r}: dir=higher needs target > worst")
        if self.dir == "lower" and self.target > self.worst:
            raise ValueError(f"metric {self.name!r}: dir=lower needs target < worst")
        return self


class EvaluationCfg(BaseModel):
    # only adapters that get_metric_adapter actually implements — don't advertise unbuilt ones
    adapter: Literal["numeric", "command-exit", "pytest-pass"]
    command: str | None = None
    metrics: list[MetricCfg] = Field(min_length=1)
    target_score: float = 100.0
    runs: int = 1
    # noise band on the 0–100 score: keep a candidate only if it improves by more than this.
    # System-managed default (0.5 ≈ ignore sub-half-point wiggle); not surfaced in the UI.
    min_delta: float = 0.5
    harness_dir: str | None = None
    # Re-score the current best every N iterations; if it no longer holds its score (a noise/flaky
    # win), demote it so the loop re-improves from the truth. 0 = off (default — costs a scorer run).
    revalidate_every: int = 0

    @model_validator(mode="after")
    def _require_command(self) -> "EvaluationCfg":
        if self.adapter in ("numeric", "command-exit") and not self.command:
            raise ValueError(f"adapter {self.adapter!r} requires a 'command'")
        return self


class LimitsCfg(BaseModel):
    max_iterations: int = 40
    plateau_N: int = 8
    budget_usd: float | None = None
    # price used to turn the (estimated) tokens into USD. 0 = cost tracking off (and budget_usd is
    # not enforced). The estimate is rough — CLI agents don't report exact tokens — so it is a
    # SAFETY CAP, not an invoice. Set both usd_per_mtok and budget_usd to enable a hard stop.
    usd_per_mtok: float = 0.0
    step_seconds: int = 600
    agent_retries: int = 2          # restart a crashed/timed-out agent this many times
    max_agent_failures: int = 3     # consecutive hard failures → halt the run (escalate)

    @model_validator(mode="after")
    def _budget_needs_price(self) -> "LimitsCfg":
        if self.budget_usd is not None and (self.usd_per_mtok or 0) <= 0:
            warnings.warn(
                "budget_usd is set but usd_per_mtok is 0 — the cost stays 0, so the budget cap will "
                "NOT be enforced. Set usd_per_mtok (price per 1M tokens) to enable the hard stop.",
                UserWarning, stacklevel=2)
        return self


class HistoryCfg(BaseModel):
    depth_k: int = 6


class CheckpointsCfg(BaseModel):
    on_plateau: bool = False
    on_target: bool = False
    every_n: int | None = None
    manual: bool = True


class SandboxCfg(BaseModel):
    backend: Literal["local", "docker"] = "local"
    image: str = "python:3.12-slim"
    memory: str | None = None
    cpus: float | None = None
    network: str = "none"


class SeedCfg(BaseModel):
    # 'generate' was an unimplemented alias for 'empty' — old configs are migrated in
    # Config._normalize_seed, so only these two modes remain valid.
    mode: Literal["empty", "copy"] = "empty"
    path: str | None = None


class NotifyCfg(BaseModel):
    """Optional webhook called once when a run reaches a terminal state."""
    enabled: bool = False
    url: str | None = None
    method: Literal["GET", "POST"] = "POST"

    @model_validator(mode="after")
    def _require_url(self) -> "NotifyCfg":
        if self.enabled and not (self.url and self.url.strip()):
            raise ValueError("notify.enabled requires a 'url'")
        return self


class Config(BaseModel):
    project: str
    description: str | None = None        # original plain-language task (from the generator)
    mode: Literal["asymmetric", "symmetric"] = "asymmetric"
    agents: dict[str, AgentCfg]
    roles: dict[str, RoleCfg]
    seed: SeedCfg = SeedCfg()
    evaluation: EvaluationCfg
    limits: LimitsCfg = LimitsCfg()
    history: HistoryCfg = HistoryCfg()
    checkpoints: CheckpointsCfg = CheckpointsCfg()
    sandbox: SandboxCfg = SandboxCfg()
    notify: NotifyCfg = NotifyCfg()

    @model_validator(mode="before")
    @classmethod
    def _normalize_seed(cls, data):
        if isinstance(data, dict) and "seed" in data:
            raw = data["seed"]
            if isinstance(raw, str):
                data["seed"] = raw = {"mode": raw}
            elif isinstance(raw, dict) and "copy" in raw:
                data["seed"] = raw = {"mode": "copy", "path": raw["copy"]}
            if isinstance(raw, dict) and raw.get("mode") == "generate":
                raw["mode"] = "empty"            # migrate the retired 'generate' alias
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
