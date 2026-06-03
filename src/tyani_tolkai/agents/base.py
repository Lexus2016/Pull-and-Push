"""Agent adapter protocol + result type."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

RunStatus = Literal["success", "timeout", "crashed", "no_op"]


@dataclass
class RunResult:
    status: RunStatus
    stdout: str = ""
    changed: bool = False     # did the agent actually modify files?


class AgentAdapter(Protocol):
    def run(self, brief: str, workdir: str | Path, profile: str, timeout: int) -> RunResult: ...
