"""Adapter registry: engine name → AgentAdapter (consilium-style, spec §8).

Phase 1 registers the ``mock`` engine. Real CLI adapters (``claude``, ``codex``,
``opencode``, ``agy``) are registered in Phase 2; asking for one now raises a clear
NotImplementedError rather than silently failing.
"""

from __future__ import annotations

from .agents.base import AgentAdapter

_PHASE2 = {"claude", "codex", "opencode", "agy"}


class AdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, name: str, adapter: AgentAdapter) -> None:
        self._adapters[name] = adapter

    def get(self, name: str) -> AgentAdapter:
        if name in self._adapters:
            return self._adapters[name]
        if name in _PHASE2:
            raise NotImplementedError(
                f"CLI adapter {name!r} arrives in Phase 2; register a mock for Phase 1 tests"
            )
        raise KeyError(f"unknown engine: {name!r}")
