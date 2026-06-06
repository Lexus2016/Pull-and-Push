"""The BotProfile contract emitted by the P1 analyzer (spec: docs/design/p1-bot-profile-analyzer.md).

Language-agnostic. Every analytical fact carries a confidence (0..1) and evidence
(``path:line`` strings) so a human reviewer can verify rather than trust. Provenance
and identity fields (analyzer_engine, source_root, bot_name) are stamped by the
analyzer, not produced by the LLM.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class EntryPoint(BaseModel):
    kind: str                      # "function" | "class-method" | "cli" | "script" | "unknown"
    location: str                  # "strategy.py:signals" | "main.py"
    inputs: str                    # prose: what it consumes
    outputs: str                   # prose: what it emits
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class Tunable(BaseModel):
    name: str
    location: str                  # path:line
    current_value: str | None = None
    inferred_type: str             # "int" | "float" | "bool" | "enum" | "unknown"
    semantic_role: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class DataSource(BaseModel):
    kind: str                      # "bundled-file" | "api" | "live-feed" | "none-found" | "unknown"
    location: str | None = None    # path or URL (URL = info only, never fetched)
    format: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class ExtractableFact(BaseModel):
    name: str                      # "return" | "sharpe" | "drawdown" | ...
    how: str                       # "prints to stdout" | "not extractable — engine must measure"
    trustworthy: bool              # false if self-reported
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class Risk(BaseModel):
    kind: str                      # "look-ahead" | "no-stop-loss" | "self-reported-pnl" | "prompt-injection" | ...
    severity: str                  # "high" | "medium" | "low" | "info"
    detail: str
    evidence: list[str] = Field(default_factory=list)


class BotProfile(BaseModel):
    schema_version: str = "1"
    analyzer_engine: str           # which LLM/agent produced this (provenance for non-deterministic B)
    bot_name: str
    source_root: str
    language: str                  # "python" | "javascript" | ... | "unknown"
    runtime: str | None = None
    framework: str                 # "custom" | "freqtrade" | ... | "unknown"
    entry_point: EntryPoint | None = None
    tunable_surface: list[Tunable] = Field(default_factory=list)
    data_source: DataSource | None = None
    extractable_metrics: list[ExtractableFact] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
