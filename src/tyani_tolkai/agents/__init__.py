"""Agent adapters: off-the-shelf CLI agents (Phase 2) + a scripted mock (Phase 1).

No bespoke agents are ever written here (spec §3 hard constraint) — only thin
adapters that shell out to existing CLIs, plus a deterministic mock for tests.
"""

from .base import AgentAdapter, RunResult
from .mock import MockAdapter

__all__ = ["AgentAdapter", "RunResult", "MockAdapter"]
