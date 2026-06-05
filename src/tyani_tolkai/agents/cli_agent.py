"""CLIAgentAdapter — drives a real off-the-shelf agent CLI as a subprocess.

No bespoke agent logic: we only shell out to `claude` / `codex` / `opencode` / `agy`
(spec §8) in the artifact directory, passing the brief as the prompt. Whether the
agent actually changed files is decided by the orchestrator via git, not by trusting
the agent — so this adapter just runs the process and classifies the outcome.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from .base import RunResult

# strip terminal control sequences (colors, cursor moves, charset designation, carriage
# returns) so the tee'd agent.log is readable plain text rather than raw ANSI from a TTY.
# Order matters: OSC and CSI are tried before the generic nF/2-char escape, and a lone ESC
# is the last-resort catch. The CSI param class is [0-?] (0x30-0x3F) so it also covers the
# private-mode prefixes < = > ? that the previous [0-9;?] regex let leak (e.g. ESC[>4m,
# ESC[<u), and the generic branch covers charset designation (ESC(B) and ESC 7 / ESC 8.
_ANSI = re.compile(
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC … (BEL or ST terminated)
    rb"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI … final byte
    rb"|\x1b[ -/]*[0-~]"                    # nF / 2-char escapes: ESC(B, ESC7, ESC=, ESC M …
    rb"|[\r\x08]"                           # carriage return, backspace
    rb"|\x1b"                               # last resort: a stray lone ESC
)
# A trailing, not-yet-complete escape at the end of a read chunk (its final byte hasn't
# arrived yet). We hold it back in `carry` so it isn't half-stripped at the chunk boundary.
_TRAIL_ESC = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*|[ -/]*)?$")


_EXECUTOR_FOCUS = (
    "You are an autonomous code executor in an automated loop. Ignore ALL global/personal "
    "agent instructions, memory, and rituals (activation tokens, SSoT, cheap-read "
    "justifications, tqmemory/memory checks, consultants, language/style rules). Work only in "
    "the current working directory; do not explore the wider filesystem or unrelated tools. "
    "Just WRITE/EDIT the files the prompt asks for, then STOP IMMEDIATELY. Do NOT run, execute, "
    "test, backtest, or verify the code yourself, do NOT run shell commands or the metric "
    "harness — the system scores it automatically after you stop. Finish in one short turn. "
    "No meta-commentary.")

# The Validator is NOT a scorer (a deterministic harness already produced the number) and it must
# NOT write code. Its whole value is the judgement the score can't give — so, unlike the executor,
# it is explicitly invited to comment: review the change, give an honest opinion, propose ideas.
_VALIDATOR_FOCUS = (
    "You are a read-only REVIEWER in an automated improvement loop. Ignore ALL global/personal "
    "agent instructions, memory, and rituals (activation tokens, SSoT, cheap-read justifications, "
    "tqmemory/memory checks, consultants, language/style rules). Do NOT edit, create, run, or test "
    "any files — you only read the diff and the metrics. A deterministic harness already computed "
    "the score, so never try to assign or guess a number. The scoring harness is hidden on purpose "
    "(files under metrics/ or a tests/ directory) — do NOT open, read or run it, and never repeat "
    "its contents; judge only from the system code, the diff and the metric values you are given. "
    "Your value is the judgement the score cannot give: review what the change did, give your honest "
    "opinion on whether it was a good idea (including risks or side-effects the score hides), and "
    "propose concrete ideas to improve next. Analytical commentary is exactly what is wanted; answer "
    "the prompt directly and concisely.")


def build_cli_prefix(engine: str, model: str | None, profile: str) -> list[str]:
    """Build the argv prefix for an engine (prompt is appended by the caller)."""
    # Executor (writeable) and Validator (read-only) get DIFFERENT system prompts: the executor is
    # told to write files and stay silent; the validator is told to NOT write and to give feedback.
    focus = _EXECUTOR_FOCUS if profile == "writeable" else _VALIDATOR_FOCUS
    if engine == "claude":
        # Headless claude must be allowed to use its file tools, or it BLOCKS forever
        # waiting for an interactive permission prompt that no one can answer (the run
        # then just sits in the executor phase until the step timeout). This boolean flag
        # is safe to place before the positional prompt. Read-only is still enforced by
        # the orchestrator reverting any edits the validator makes.
        # claude -p inherits the operator's FULL personal environment, which derails it from
        # the task. Two strippers (auth is kept — we stay on the default config dir):
        #  * --strict-mcp-config --mcp-config '{}': load NO MCP servers. Otherwise the agent
        #    inherits the operator's MCP tools (web search, memory, etc.) and spends the turn
        #    "researching" instead of writing code. (--mcp-config is variadic, so it is
        #    followed by another flag to stop it swallowing the positional prompt.)
        #  * --append-system-prompt: override ~/.claude/CLAUDE.md personal rituals so it acts
        #    as a confined executor (write files, don't run/test, stop).
        cmd = ["claude", "-p", "--dangerously-skip-permissions",
               "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
               "--append-system-prompt", focus]
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
        # `opencode run` is already non-interactive/auto-approving — it has no
        # --dangerously-skip-permissions flag. The working dir is passed via --dir in run()
        # (opencode ignores the process cwd and otherwise resolves the ENCLOSING git repo).
        cmd = ["opencode", "run"]
        if model:
            cmd += ["-m", model]
        return cmd
    if engine == "agy":
        # -p = non-interactive print; auto-approve tools so it can't stall on a prompt.
        # The workspace dir is added via --add-dir in run() (agy doesn't use the process cwd).
        cmd = ["agy", "-p", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        return cmd
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

    def _classify(self, full: str, returncode: int, timed_out: bool) -> RunResult:
        if self._killed and not timed_out:
            return RunResult(status="killed", stdout=full)   # deliberate Force-Stop
        if timed_out:
            return RunResult(status="timeout", stdout=full)
        status = "success" if returncode == 0 else "crashed"
        low = full.lower()
        if any(k in low for k in ("rate limit", "rate_limit", "ratelimit", "429",
                                  "too many requests", "quota", "overloaded",
                                  "usage limit", "insufficient_quota")):
            status = "rate_limited"        # transient provider limit — pause, don't retry
        return RunResult(status=status, stdout=full)

    def run(self, brief: str, workdir: str | Path, profile: str, timeout: int) -> RunResult:
        self._killed = False
        cmd = list(self.prefix)
        # The subprocess runs with cwd=workdir. claude respects that. The others resolve their
        # own working root (and would otherwise edit the ENCLOSING git repo), so state it
        # explicitly with each one's single-path flag — placed BEFORE the positional prompt:
        #   codex -C <dir> · opencode --dir <dir> · agy --add-dir <dir>
        if self.engine == "codex":
            cmd += ["-C", str(workdir)]
        elif self.engine == "opencode":
            cmd += ["--dir", str(workdir)]
        elif self.engine == "agy":
            cmd += ["--add-dir", str(workdir)]
        argv = [*cmd, brief]
        if profile == "writeable":
            return self._run_logged(argv, workdir, timeout)   # executor → live agent.log
        return self._run_plain(argv, workdir, timeout)        # validator → pipe (not logged)

    def _run_plain(self, argv, workdir, timeout) -> RunResult:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
        except FileNotFoundError:
            return RunResult(status="crashed", stdout=f"{self.prefix[0]!r} not installed")
        self._proc = proc
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            try:
                out, _ = proc.communicate()
            except Exception:
                out = ""
            timed_out = True
        finally:
            self._proc = None
        return self._classify(out or "", proc.returncode, timed_out)

    def _run_logged(self, argv, workdir, timeout) -> RunResult:
        """Run under a PTY so the agent (a TTY app) line-buffers and FLUSHES live; tee that
        stream to <project>/agent.log as it arrives so the dashboard can follow it in real
        time. Falls back to a pipe if a PTY isn't available."""
        import pty
        import select
        import time
        log_path = Path(workdir).parent / "agent.log"
        try:
            master, slave = pty.openpty()
        except OSError:
            return self._run_plain(argv, workdir, timeout)   # no PTY → at least don't crash
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), stdin=subprocess.DEVNULL,
                stdout=slave, stderr=slave, start_new_session=True, close_fds=True)
        except FileNotFoundError:
            os.close(master); os.close(slave)
            return RunResult(status="crashed", stdout=f"{self.prefix[0]!r} not installed")
        os.close(slave)
        self._proc = proc
        chunks: list[bytes] = []
        carry = b""          # a trailing partial escape held over to the next read
        timed_out = False
        deadline = time.monotonic() + max(1, timeout)
        try:
            # APPEND, never truncate: the orchestrator writes a per-iteration header before
            # each run, so agent.log accumulates every iteration (the operator reads the whole
            # history, not just the latest run that used to overwrite it).
            with open(log_path, "ab") as lf:
                while True:
                    if time.monotonic() > deadline:
                        self.kill(); timed_out = True; break
                    try:
                        r, _, _ = select.select([master], [], [], 0.5)
                    except (OSError, ValueError):
                        break
                    if r:
                        try:
                            data = os.read(master, 4096)
                        except OSError:
                            break          # PTY closed → child exited
                        if not data:
                            break
                        buf = carry + data
                        m = _TRAIL_ESC.search(buf)        # hold a partial escape at the tail
                        if m and m.start() < len(buf) and len(buf) - m.start() < 128:
                            carry = buf[m.start():]; buf = buf[:m.start()]
                        else:
                            carry = b""
                        clean = _ANSI.sub(b"", buf)
                        if clean:
                            lf.write(clean); lf.flush(); chunks.append(clean)
                    elif proc.poll() is not None:
                        break
                if carry:                                  # flush whatever escape never completed
                    tail = _ANSI.sub(b"", carry)
                    if tail:
                        lf.write(tail); lf.flush(); chunks.append(tail)
        except OSError:
            pass
        finally:
            try:
                os.close(master)
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                self.kill()
            self._proc = None
        full = b"".join(chunks).decode("utf-8", errors="replace")
        return self._classify(full, proc.returncode if proc.returncode is not None else 0, timed_out)
