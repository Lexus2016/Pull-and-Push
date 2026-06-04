"""CLIAgentAdapter — drives a real off-the-shelf agent CLI as a subprocess.

No bespoke agent logic: we only shell out to `claude` / `codex` / `opencode` / `agy`
(spec §8) in the artifact directory, passing the brief as the prompt. Whether the
agent actually changed files is decided by the orchestrator via git, not by trusting
the agent — so this adapter just runs the process and classifies the outcome.
"""

from __future__ import annotations

import os
import signal
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
    """Run a CLI agent: argv = prefix + [brief], executed in the working dir.

    The child runs in its own process group (start_new_session) so a Force-Stop from
    another thread can kill the whole tree instantly via ``kill()``.
    """

    def __init__(self, prefix: list[str], engine: str | None = None):
        self.prefix = prefix
        self.engine = engine
        self._proc: subprocess.Popen | None = None
        self._killed = False

    def kill(self) -> None:
        """Force-terminate the running agent and its process group (any thread)."""
        self._killed = True
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # whole tree (node children etc.)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    p.kill()
                except Exception:
                    pass

    def run(self, brief: str, workdir: str | Path, profile: str, timeout: int) -> RunResult:
        self._killed = False
        cmd = list(self.prefix)
        # The subprocess runs with cwd=workdir, so the agent already has the working
        # directory. Only codex needs it stated explicitly via -C (a single-path flag).
        # claude/agy use --add-dir, which is GREEDY (variadic) and swallows the prompt
        # argument that follows it — breaking the call ("prompt not provided"). So we do
        # NOT pass --add-dir; cwd is sufficient.
        if self.engine == "codex":
            cmd += ["-C", str(workdir)]

        argv = [*cmd, brief]
        # For the executor (writeable) we tee the agent's real output to <project>/agent.log
        # (the project dir is workdir's parent — OUTSIDE the artifact git tree, so it never
        # pollutes change-detection). This gives ground-truth visibility into what the agent
        # is doing, with no cooperation from the agent. Read-only (validator) uses a pipe.
        log_file = None
        if profile == "writeable":
            try:
                log_file = open(Path(workdir).parent / "agent.log", "w", encoding="utf-8")
            except OSError:
                log_file = None
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), text=True,
                stdout=(log_file or subprocess.PIPE),
                stderr=subprocess.STDOUT if log_file else subprocess.PIPE,
                # Detach stdin so a CLI that reads it (codex appends a piped <stdin> block;
                # others may await interactive input) gets immediate EOF instead of blocking.
                stdin=subprocess.DEVNULL,
                start_new_session=True,   # own process group → killable as a unit on Force-Stop
            )
        except FileNotFoundError:
            if log_file:
                log_file.close()
            return RunResult(status="crashed", stdout=f"{self.prefix[0]!r} not installed")
        self._proc = proc
        timed_out = False
        out = err = ""
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            try:
                out, err = proc.communicate()
            except Exception:
                out, err = "", ""
            timed_out = True
        finally:
            self._proc = None
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass

        if log_file is not None:   # output went to the file → read it back as the result text
            try:
                full = (Path(workdir).parent / "agent.log").read_text(encoding="utf-8", errors="replace")
            except OSError:
                full = ""
        else:
            full = (out or "") + (err or "")
        if self._killed and not timed_out:
            return RunResult(status="killed", stdout=full)   # deliberate Force-Stop
        if timed_out:
            return RunResult(status="timeout", stdout=full)
        status = "success" if proc.returncode == 0 else "crashed"
        low = full.lower()
        if any(k in low for k in ("rate limit", "rate_limit", "ratelimit", "429",
                                  "too many requests", "quota", "overloaded",
                                  "usage limit", "insufficient_quota")):
            status = "rate_limited"        # transient provider limit — pause, don't retry
        return RunResult(status=status, stdout=full)
