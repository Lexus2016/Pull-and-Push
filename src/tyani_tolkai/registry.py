"""Adapter registry: engine name → AgentAdapter (consilium-style, spec §8).

Phase 1 registers the ``mock`` engine. Real CLI adapters (``claude``, ``codex``,
``opencode``, ``agy``) are registered in Phase 2; asking for one now raises a clear
NotImplementedError rather than silently failing.
"""

from __future__ import annotations

from .agents.base import AgentAdapter

_CLI_ENGINES = {"claude", "codex", "opencode", "agy"}


class AdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, name: str, adapter: AgentAdapter) -> None:
        self._adapters[name] = adapter

    def get(self, name: str) -> AgentAdapter:
        if name in self._adapters:
            return self._adapters[name]
        raise KeyError(f"unknown engine: {name!r}")


def build_adapter(engine: str, model: str | None, profile: str) -> AgentAdapter:
    """Construct a CLI agent adapter for a real engine (spec §8)."""
    if engine in _CLI_ENGINES:
        from .agents.cli_agent import CLIAgentAdapter, build_cli_prefix
        return CLIAgentAdapter(build_cli_prefix(engine, model, profile))
    raise KeyError(f"unknown engine: {engine!r} (use mock for tests, or {_CLI_ENGINES})")
