"""The MetricProposal contract emitted by the P2 proposer (spec: docs/design/p2-metric-proposal.md).

Proposed metric fields align with config.MetricCfg (name/dir/weight/target) so P3 can
consume them without translation. Provenance/identity fields (proposer_engine, bot_name,
goal) are stamped by the proposer, not produced by the LLM.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProposedMetric(BaseModel):
    name: str                              # MUST be one of profile.extractable_metrics names (grounding)
    dir: Literal["higher", "lower"]        # closed set — matches MetricCfg.dir
    weight: float = Field(..., gt=0)       # MetricCfg.weight — relative importance
    target: float                          # MetricCfg.target — value mapping to score 100
    rationale: str
    confidence: float = Field(..., ge=0, le=1)


class ProposedTunable(BaseModel):
    name: str                              # MUST be one of profile.tunable_surface names (grounding)
    min: float | None = None               # range lower bound (deferred P1 inferred_range); null for non-numeric
    max: float | None = None
    inferred_type: str                     # echoed from the profile
    rationale: str
    confidence: float = Field(..., ge=0, le=1)


class MetricProposal(BaseModel):
    schema_version: str = "1"
    proposer_engine: str                   # provenance (B is non-deterministic), stamped by us
    bot_name: str
    goal: str                              # echo of the user's free-text goal
    proposed_metrics: list[ProposedMetric] = Field(default_factory=list)
    proposed_tunables: list[ProposedTunable] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
