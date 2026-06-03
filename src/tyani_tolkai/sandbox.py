"""Sandbox backends for executing artifact code (spec §11).

Phase 1 ships ``LocalBackend`` (subprocess with a hard timeout). ``DockerBackend``
shares the same interface and is implemented in Phase 2 (read-only artifact mount,
``--network none``, mem/cpu limits, harness injected so it is invisible).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class SandboxBackend(Protocol):
    def run(self, cmd: str, cwd: str | Path, timeout: int,
            env: dict | None = None) -> ExecResult: ...


class LocalBackend:
    """Run the command on the host with a timeout. No isolation — Phase 1 only."""

    name = "local"

    def run(self, cmd: str, cwd: str | Path, timeout: int,
            env: dict | None = None) -> ExecResult:
        import os
        # never write .pyc — stale bytecode in the reused artifact dir would make
        # the metric read an old version of the code (silent, nasty bug)
        full_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
        try:
            proc = subprocess.run(
                shlex.split(cmd),
                cwd=str(cwd),
                timeout=timeout,
                capture_output=True,
                text=True,
                env=full_env,
            )
            return ExecResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            return ExecResult(124, e.stdout or "", (e.stderr or "") + "\n[timeout]", timed_out=True)


class DockerBackend:
    """Phase 2: isolated execution. Interface placeholder for now."""

    name = "docker"

    def run(self, cmd: str, cwd: str | Path, timeout: int,
            env: dict | None = None) -> ExecResult:
        raise NotImplementedError("DockerBackend arrives in Phase 2")


def get_backend(name: str) -> SandboxBackend:
    if name == "local":
        return LocalBackend()
    if name == "docker":
        return DockerBackend()
    raise ValueError(f"unknown sandbox backend: {name!r}")
