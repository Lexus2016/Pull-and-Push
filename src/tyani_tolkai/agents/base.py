"""Agent adapter protocol + result type."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

RunStatus = Literal["success", "timeout", "crashed", "no_op", "rate_limited"]


@dataclass
class Usage:
    """What one agent call really consumed, as the CLI reported it."""
    input: int = 0                   # fresh input tokens (incl. cache writes)
    cached: int = 0                  # input read from the provider's cache (billed at ~10%)
    output: int = 0                  # output tokens, reasoning included
    cost_usd: float | None = None    # set when the CLI prices the call itself (claude, grok)

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.cached += other.cached
        self.output += other.output
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd


@dataclass
class RunResult:
    status: RunStatus
    stdout: str = ""
    changed: bool = False     # did the agent actually modify files?
    usage: Usage | None = None  # real consumption; None = the CLI reported nothing
    actions: int = 0          # tool calls seen in the stream (0 on a timeout = it only thought)


class AgentAdapter(Protocol):
    def run(self, brief: str, workdir: str | Path, profile: str, timeout: int) -> RunResult: ...
