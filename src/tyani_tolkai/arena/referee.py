"""Referee — the deterministic, vetted trust anchor for symmetric mode (design §1).

A referee runs side A's artifact against side B's artifact in a sandbox and emits an
OBJECTIVE outcome. It is NEVER an LLM and neither side scores itself. Determinism and
mirror-symmetry are enforced by tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

OpponentStrategy = Literal["sample", "accumulate"]


@dataclass
class MatchOutcome:
    a_score: float                      # objective scalar in [0,1] from A's perspective
    b_score: float                      # B's perspective; zero-sum ⇒ a_score + b_score == 1
    detail: dict = field(default_factory=dict)

    @classmethod
    def zero_sum(cls, a_score: float, detail: dict | None = None) -> "MatchOutcome":
        return cls(a_score=a_score, b_score=1.0 - a_score, detail=detail or {})


class Referee(Protocol):
    name: str
    version: str
    opponent_strategy: OpponentStrategy

    def play(self, a_dir: Path, b_dir: Path, sandbox, *, seed: int) -> MatchOutcome: ...


_REGISTRY: dict[str, type] = {}


def register_referee(cls: type) -> type:
    """Class decorator: register a referee under its ``name`` in the vetted registry."""
    _REGISTRY[cls.name] = cls
    return cls


def get_referee(name: str) -> Referee:
    if name not in _REGISTRY:
        raise KeyError(f"unknown referee: {name!r}")
    return _REGISTRY[name]()
