"""Metric adapter protocol + registry.

An adapter runs the artifact inside a sandbox and returns an objective metrics
vector. The Scorer (separate, pure code) turns that into the scalar. Adapters are
the only thing most new tasks need to supply (spec §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class MetricResult:
    metrics: list[dict] = field(default_factory=list)  # [{name, value, dir, weight}]
    logs: str = ""
    ok: bool = True                                     # False → artifact failed to run


class MetricAdapter(Protocol):
    def run(self, artifact_dir: str | Path, sandbox, evaluation, timeout: int) -> MetricResult: ...


def get_metric_adapter(name: str) -> MetricAdapter:
    from .command_exit import CommandExitAdapter
    from .numeric import NumericAdapter
    from .pytest_pass import PytestPassAdapter

    table = {
        "numeric": NumericAdapter,
        "command-exit": CommandExitAdapter,
        "pytest-pass": PytestPassAdapter,
    }
    if name not in table:
        raise NotImplementedError(f"metric adapter {name!r} not available in Phase 1")
    return table[name]()
