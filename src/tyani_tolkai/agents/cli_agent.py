"""CLIAgentAdapter — drives a real off-the-shelf agent CLI as a subprocess.

No bespoke agent logic: we only shell out to `claude` / `codex` / `opencode` / `agy`
(spec §8) in the artifact directory, passing the brief as the prompt. Whether the
agent actually changed files is decided by the orchestrator via git, not by trusting
the agent — so this adapter just runs the process and classifies the outcome.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import RunResult


def build_cli_prefix(engine: str, model: str | None, profile: str) -> list[str]:
    """Build the argv prefix for an engine (prompt is appended by the caller)."""
    if engine == "claude":
        cmd = ["claude", "-p"]
        if model:
            cmd += ["--model", model]
        return cmd
    if engine == "codex":
        sandbox = "workspace-write" if profile == "writeable" else "read-only"
        cmd = ["codex", "exec", "--sandbox", sandbox]
        if model:
            cmd += ["-m", model]
        return cmd
    if engine == "opencode":
        cmd = ["opencode", "run"]
        if model:
            cmd += ["-m", model]
        return cmd
    if engine == "agy":
        return ["agy", "-p"]
    raise ValueError(f"no CLI prefix for engine {engine!r}")


class CLIAgentAdapter:
    """Run a CLI agent: argv = prefix + [brief], executed in the working dir."""

    def __init__(self, prefix: list[str], engine: str | None = None):
        self.prefix = prefix
        self.engine = engine

    def run(self, brief: str, workdir: str | Path, profile: str, timeout: int) -> RunResult:
        cmd = list(self.prefix)
        # The subprocess runs with cwd=workdir, so the agent already has the working
        # directory. Only codex needs it stated explicitly via -C (a single-path flag).
        # claude/agy use --add-dir, which is GREEDY (variadic) and swallows the prompt
        # argument that follows it — breaking the call ("prompt not provided"). So we do
        # NOT pass --add-dir; cwd is sufficient.
        if self.engine == "codex":
            cmd += ["-C", str(workdir)]

        argv = [*cmd, brief]
        try:
            proc = subprocess.run(
                argv, cwd=str(workdir), capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return RunResult(status="crashed", stdout=f"{self.prefix[0]!r} not installed")
        except subprocess.TimeoutExpired as e:
            return RunResult(status="timeout", stdout=(e.stdout or "") if isinstance(e.stdout, str) else "")
        status = "success" if proc.returncode == 0 else "crashed"
        return RunResult(status=status, stdout=(proc.stdout or "") + (proc.stderr or ""))
