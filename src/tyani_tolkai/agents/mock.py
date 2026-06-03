"""MockAdapter — a scripted, deterministic stand-in for a real CLI agent.

Each call applies the next edit in ``edits`` to the working directory. An edit is
a callable ``(workdir: Path) -> bool`` returning True if it changed anything. When
the script is exhausted (or an edit changes nothing) the run is a ``no_op``. Used
by unit tests and the golden run, so the loop can be proven without paid agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .base import RunResult

Edit = Callable[[Path], bool]


class MockAdapter:
    def __init__(self, edits: list[Edit] | None = None):
        self.edits = edits or []
        self.i = 0

    def run(self, brief: str, workdir: str | Path, profile: str, timeout: int) -> RunResult:
        workdir = Path(workdir)
        if self.i >= len(self.edits):
            return RunResult(status="no_op", stdout="(no more scripted edits)", changed=False)
        edit = self.edits[self.i]
        self.i += 1
        changed = bool(edit(workdir))
        status = "success" if changed else "no_op"
        return RunResult(status=status, stdout=f"applied edit {self.i}", changed=changed)
