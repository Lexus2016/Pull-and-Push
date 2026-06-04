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
        # Headless claude must be allowed to use its file tools, or it BLOCKS forever
        # waiting for an interactive permission prompt that no one can answer (the run
        # then just sits in the executor phase until the step timeout). This boolean flag
        # is safe to place before the positional prompt. Read-only is still enforced by
        # the orchestrator reverting any edits the validator makes.
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
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
        cmd = ["opencode", "run", "--dangerously-skip-permissions"]  # auto-approve, never block
        if model:
            cmd += ["-m", model]
        return cmd
    if engine == "agy":
        # -p = non-interactive print; auto-approve tools so it can't stall on a prompt.
        return ["agy", "-p", "--dangerously-skip-permissions"]
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
                # Detach stdin: a CLI that reads stdin (codex appends a piped <stdin> block;
                # others may wait for interactive input) would otherwise BLOCK forever on an
                # inherited stdin. DEVNULL gives an immediate EOF so the agent can't stall.
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return RunResult(status="crashed", stdout=f"{self.prefix[0]!r} not installed")
        except subprocess.TimeoutExpired as e:
            return RunResult(status="timeout", stdout=(e.stdout or "") if isinstance(e.stdout, str) else "")
        out = (proc.stdout or "") + (proc.stderr or "")
        status = "success" if proc.returncode == 0 else "crashed"
        low = out.lower()
        if any(k in low for k in ("rate limit", "rate_limit", "ratelimit", "429",
                                  "too many requests", "quota", "overloaded",
                                  "usage limit", "insufficient_quota")):
            status = "rate_limited"        # transient provider limit — pause, don't retry
        return RunResult(status=status, stdout=out)
