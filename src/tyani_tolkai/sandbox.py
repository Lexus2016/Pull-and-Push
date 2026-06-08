"""Sandbox backends for executing artifact code (spec §11).

Phase 1 ships ``LocalBackend`` (subprocess with a hard timeout). ``DockerBackend``
shares the same interface and is implemented in Phase 2 (read-only artifact mount,
``--network none``, mem/cpu limits, harness injected so it is invisible).
"""

from __future__ import annotations

import shlex
import subprocess
import sys
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
            env: dict | None = None, input: str | None = None) -> ExecResult: ...


class LocalBackend:
    """Run the command on the host with a timeout. No isolation — Phase 1 only."""

    name = "local"
    # Interpreter the metric adapters invoke (host venv). Forward slashes so the path stays
    # intact through shlex.split(posix=True) on Windows too — C:/...\python.exe works there.
    python = sys.executable.replace("\\", "/")

    def run(self, cmd: str, cwd: str | Path, timeout: int,
            env: dict | None = None, input: str | None = None) -> ExecResult:
        import os
        # never write .pyc — stale bytecode in the reused artifact dir would make
        # the metric read an old version of the code (silent, nasty bug)
        full_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
        try:
            proc = subprocess.run(
                # posix split correctly unquotes args; the interpreter path is forward-slashed
                # (LocalBackend.python) so it survives intact on Windows too.
                shlex.split(cmd),
                cwd=str(cwd),
                timeout=timeout,
                capture_output=True,
                text=True,
                env=full_env,
                input=input,          # stdin (e.g. the referee feeds probes here, off-disk)
            )
            return ExecResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            return ExecResult(124, e.stdout or "", (e.stderr or "") + "\n[timeout]", timed_out=True)


class DockerBackend:
    """Isolated execution (spec §11). Mounts the PROJECT dir (artifact's parent) at
    its identical absolute path, read-only, so absolute harness paths resolve inside
    the container exactly as on the host. Network off, mem/cpu limited.

    Caveat: metric commands must use the image's ``python`` (not a host interpreter
    path). Validate on a Docker host — not exercised by the local test suite.
    """

    name = "docker"
    python = "python"              # the image's interpreter (host paths don't exist inside)

    def __init__(self, image: str = "python:3.12-slim", network: str = "none",
                 memory: str | None = None, cpus: float | None = None):
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus

    def run(self, cmd: str, cwd: str | Path, timeout: int,
            env: dict | None = None, input: str | None = None) -> ExecResult:
        cwd = Path(cwd).resolve()
        project = cwd.parent  # artifact's parent (project dir) holds metrics/ harness too
        argv = ["docker", "run", "--rm", "--network", self.network,
                "-v", f"{project}:{project}:ro", "-w", str(cwd),
                "-e", "PYTHONDONTWRITEBYTECODE=1"]
        if input is not None:
            argv.append("-i")            # keep stdin open so the container can read it
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        if self.memory:
            argv += ["--memory", self.memory]
        if self.cpus:
            argv += ["--cpus", str(self.cpus)]
        argv += [self.image, "sh", "-c", cmd]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input)
            return ExecResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            return ExecResult(124, e.stdout or "", (e.stderr or "") + "\n[timeout]", timed_out=True)


def get_backend(name: str, sandbox=None) -> SandboxBackend:
    if name == "local":
        return LocalBackend()
    if name == "docker":
        return DockerBackend(
            image=getattr(sandbox, "image", "python:3.12-slim") if sandbox else "python:3.12-slim",
            network=getattr(sandbox, "network", "none") if sandbox else "none",
            memory=getattr(sandbox, "memory", None) if sandbox else None,
            cpus=getattr(sandbox, "cpus", None) if sandbox else None,
        )
    raise ValueError(f"unknown sandbox backend: {name!r}")
