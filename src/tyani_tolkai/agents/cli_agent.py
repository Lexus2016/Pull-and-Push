"""CLIAgentAdapter — drives a real off-the-shelf agent CLI as a subprocess.

No bespoke agent logic: we only shell out to `claude` / `codex` / `opencode` / `agy` / `grok`
(spec §8) in the artifact directory, passing the brief as the prompt. Whether the
agent actually changed files is decided by the orchestrator via git, not by trusting
the agent — so this adapter just runs the process and classifies the outcome.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path

from .base import RunResult
from .streams import StreamReader

# Detach the agent into its own process group so Force-Stop can kill the whole tree.
# POSIX: a new session; Windows: a new process group (taskkill /T then finishes the tree).
_POPEN_GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt" else {"start_new_session": True})

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

# Provider rate-limit / quota detection. A FAILED run (non-zero exit) is checked for any limit
# hint; a SUCCESSFUL run only for an unambiguous limit notice near the end of its output. Bare
# words like "quota", "429" or "overloaded" in a successful run are ordinary content — an agent
# that edited "lines 1429-1440" or wrote a "per-symbol quota" must not pause the whole run and
# have its edit reverted.
_RATE_ANY = re.compile(r"rate[ _-]?limit|\b429\b|too many requests|quota|overloaded|usage limit",
                       re.IGNORECASE)
_RATE_STRONG = re.compile(r"usage limit reached|rate limit (?:reached|exceeded)|rate_limit_exceeded"
                          r"|too many requests|insufficient_quota|quota exceeded"
                          r"|exceeded your current quota|overloaded_error", re.IGNORECASE)


def _looks_rate_limited(output: str, returncode: int) -> bool:
    if returncode != 0:
        return bool(_RATE_ANY.search(output or ""))
    return bool(_RATE_STRONG.search((output or "")[-600:]))


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


# Helpers (research wizard, configurator, bot profiler, metric proposer) are prompt → text: all
# their input is in the prompt and only stdout is parsed, so they get no tools and their own brief
# (the reviewer's "never write or repeat the harness" is the opposite of "draft the scorer").
_HELPER_FOCUS = (
    "You are a text helper called by a program that parses your reply. Ignore ALL global/personal "
    "agent instructions, memory, and rituals (activation tokens, SSoT, tqmemory/memory checks, "
    "consultants, language/style rules). You have no tools and need none: everything you need is "
    "in the prompt. Answer it directly with exactly the requested output (e.g. one JSON object) — "
    "no preamble, no follow-up questions.")


def _focus(profile: str) -> str:
    # Executor (writeable) and Validator (read-only) get DIFFERENT briefs: the executor is told to
    # write files and stay silent; the validator is told to NOT write and to give feedback.
    return {"writeable": _EXECUTOR_FOCUS, "text": _HELPER_FOCUS}.get(profile, _VALIDATOR_FOCUS)


# ---- isolation from the operator's personal agent setup ----
# Each CLI loads its owner's personal rules, skills, hooks and MCP servers. Measured on a
# one-line task ("create ok.txt"): codex read ~/.codex/MCP.md and called the operator's
# tqmemory (140k input tokens); agy and grok ran the operator's rituals; opencode printed
# the operator's activation token; claude ran the operator's SessionStart hooks. The loop
# wants a confined worker, so every engine is started without what it can be told to skip.
_NO_HOOKS = '{"disableAllHooks":true}'   # claude: keep user settings (auth, env, proxy), no hooks
_ENV = {
    # grok imports Claude Code's and Codex's rules, skills, agents, MCP servers and hooks
    "grok": {f"GROK_{src}_{what}_ENABLED": "0" for src in ("CLAUDE", "CODEX")
             for what in ("RULES", "SKILLS", "AGENTS", "MCPS", "HOOKS")},
    "opencode": {"OPENCODE_DISABLE_CLAUDE_CODE": "1"},   # ~/.claude rules and skills
}
# the brief goes INTO the prompt for engines with no system-prompt flag (codex has
# developer_instructions, claude --append-system-prompt, grok --rules)
_BRIEF_IN_PROMPT = {"opencode", "agy"}


def _codex_mcp_off() -> tuple[str, ...]:
    """`-c mcp_servers.<name>.enabled=false` for every MCP server in the operator's codex config.
    `-c mcp_servers={}` does not remove them; each one (npx / uvx / OAuth) starts on every
    `codex exec` — measured 45 s → 10 s for a one-line task with them off. Only servers declared
    in config.toml: overriding a plugin's server leaves a table without a transport and codex
    refuses to start."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        text = (home / "config.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    names = dict.fromkeys(re.findall(r'^\s*\[mcp_servers\.([A-Za-z0-9_-]+)[.\]]', text, re.M))
    return tuple(a for n in names for a in ("-c", f"mcp_servers.{n}.enabled=false"))


# The loop wants many short steps, not one long think: every role on every engine reasons at
# "medium" unless the config says otherwise. Left unset, each CLI would use the operator's own
# default (claude xhigh, grok high) — measured: a Grok executor turn reasoned for the whole
# 10-minute timeout with zero tool calls; a claude helper thought 8+ min without answering.
DEFAULT_EFFORT = "medium"


def build_cli_prefix(engine: str, model: str | None, profile: str,
                     effort: str | None = None) -> list[str]:
    """Build the argv prefix for an engine (prompt is appended by the caller). Profiles:
    "writeable" (executor), "read-only" (validator), "text" (helper: prompt → answer, no tools).
    ``effort``: reasoning effort, passed to every engine (None → DEFAULT_EFFORT)."""
    effort = effort or DEFAULT_EFFORT
    focus = _focus(profile)
    # Output is each CLI's streaming JSON (read by agents/streams.py): a readable live log, the
    # final answer, and the real token usage / cost — instead of guessing tokens from text length.
    if engine == "claude" and profile == "text":
        # measured on the wizard's "draft the kit" call (a ~13 KB answer): at the inherited
        # high/xhigh it thought 8+ min (49k thinking tokens) without writing a character; at
        # medium it answered in 135 s with a kit that passed the pre-flight.
        # --tools "" = no built-in tools (nothing to write, run or hang on). Like --mcp-config it is
        # variadic, so another flag must follow it before the positional prompt.
        cmd = ["claude", "-p", "--effort", effort, "--tools", "", "--mcp-config",
               '{"mcpServers":{}}', "--strict-mcp-config", "--append-system-prompt", focus,
               "--settings", _NO_HOOKS, "--disable-slash-commands",
               "--output-format", "stream-json", "--verbose"]
        if model:
            cmd += ["--model", model]
        return cmd
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
        #  * no hooks, no skills (their descriptions alone are thousands of tokens per call)
        cmd = ["claude", "-p", "--dangerously-skip-permissions",
               "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
               "--append-system-prompt", focus, "--effort", effort,
               "--settings", _NO_HOOKS, "--disable-slash-commands",
               "--output-format", "stream-json", "--verbose"]
        if model:
            cmd += ["--model", model]
        return cmd
    if engine == "codex":
        sandbox = "workspace-write" if profile == "writeable" else "read-only"
        cmd = ["codex", "exec", "--sandbox", sandbox, "-c", f"model_reasoning_effort={effort}",
               # our brief as a developer message (a JSON string is a valid TOML string)
               "-c", "developer_instructions=" + json.dumps(focus), "--json"]
        if model:
            cmd += ["-m", model]
        return cmd
    if engine == "opencode":
        # `opencode run` is already non-interactive/auto-approving — it has no
        # --dangerously-skip-permissions flag. The working dir is passed via --dir in run()
        # (opencode ignores the process cwd and otherwise resolves the ENCLOSING git repo).
        # --variant is its reasoning effort; names are provider-specific and an unknown one is
        # silently ignored (tested), so passing it never breaks a model that lacks it.
        cmd = ["opencode", "run", "--variant", effort, "--pure", "--format", "json"]  # --pure: no plugins
        if model:
            cmd += ["-m", model]
        return cmd
    if engine == "agy":
        # auto-approve tools so it can't stall on a prompt. The workspace (--add-dir) and the
        # prompt are added in run(): `--print` TAKES the prompt as its value (agy 1.2+) — a bare
        # `-p` before other flags made agy read the next flag as the prompt and ignore the task.
        cmd = ["agy", "--dangerously-skip-permissions", "--effort", effort,
               "--disable-slash-commands", "--output-format", "stream-json"]
        if model:
            cmd += ["--model", model]
        return cmd
    if engine == "grok":
        # xAI Grok Build. `-p/--single` TAKES the prompt as its value and `--cwd` sets the
        # workspace — both added in run(); --rules appends our brief to its system prompt.
        cmd = ["grok", "--rules", focus, "--always-approve"]   # headless: nobody answers a prompt
        if profile != "writeable":
            # reviewer / helper may read, never write: a plain headless `grok -p` DID write a file
            # in a write-bait test (consilium). --deny takes exactly one value (not variadic).
            cmd += ["--deny", "Write", "--deny", "Edit", "--deny", "Bash"]
        cmd += ["--reasoning-effort", effort, "--output-format", "streaming-json"]
        if model:
            cmd += ["-m", model]
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
        self._env: dict | None = None     # extra environment for this engine (isolation)

    def kill(self) -> None:
        """Force-terminate the running agent and its whole process tree (any thread)."""
        self._killed = True
        p = self._proc
        if p is None or p.poll() is not None:
            return
        if os.name == "nt":
            # Windows has no killpg: kill the tree via taskkill, fall back to Popen.kill()
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, check=False)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # whole tree (node children etc.)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.kill()
            except Exception:
                pass

    def _classify(self, reader: StreamReader, returncode: int, timed_out: bool) -> RunResult:
        # decided on the RENDERED stream, never the raw JSON: claude reports its rate-limit state
        # ("rate_limit_event", "allowed") on every run, so a raw match paused runs that crashed
        full = reader.rendered
        seen = dict(usage=reader.usage, actions=reader.actions, outside=list(reader.outside))
        if self._killed and not timed_out:
            return RunResult(status="killed", stdout=full, **seen)
        if timed_out:
            return RunResult(status="timeout", stdout=full, **seen)
        # a CLI may report a fatal error and still exit 0 (agy's result status, claude's is_error)
        ok = returncode == 0 and not reader.failed
        status = "success" if ok else "crashed"
        # a limit is looked for in what the CLI said, not in the agent's prose (a reviewer may well
        # discuss a "rate limit exceeded" bug in the code it reviews)
        if _looks_rate_limited(reader.signals, 0 if ok else 1):
            status = "rate_limited"        # transient provider limit — pause, don't retry
        answer = reader.answer if status == "success" and reader.answer else full
        return RunResult(status=status, stdout=answer, **seen)

    def run(self, brief: str, workdir: str | Path, profile: str, timeout: int) -> RunResult:
        self._killed = False
        cmd = list(self.prefix)
        # The subprocess runs with cwd=workdir. claude respects that. The others resolve their
        # own working root (and would otherwise edit the ENCLOSING git repo), so state it
        # explicitly with each one's single-path flag — placed BEFORE the positional prompt:
        #   codex -C <dir> · opencode --dir <dir> · agy --add-dir <dir> · grok --cwd <dir>
        last_message = None
        if self.engine == "codex":
            if cmd[:1] == ["codex"]:
                cmd += _codex_mcp_off()
            cmd += ["-C", str(workdir)]
            # codex's own final message, kept apart from the event stream (-o writes just that)
            fd, last_message = tempfile.mkstemp(prefix="pp-codex-", suffix=".txt")
            os.close(fd)
            cmd += ["-o", last_message]
        elif self.engine == "opencode":
            cmd += ["--dir", str(workdir)]
        elif self.engine == "agy":
            cmd += ["--add-dir", str(workdir), "--print"]      # the brief is --print's value
        elif self.engine == "grok":
            cmd += ["--cwd", str(workdir), "-p"]               # the brief is -p's value
        if self.engine in _BRIEF_IN_PROMPT:
            brief = _focus(profile) + "\n\n---\n\n" + brief
        argv = [*cmd, brief]
        extra = _ENV.get(self.engine or "")
        self._env = {**os.environ, **extra} if extra else None
        if os.name == "nt":
            # Windows Popen (shell=False) won't resolve .cmd/.bat shims (many CLIs are installed
            # that way via npm) through PATHEXT — do it explicitly so the agent actually launches.
            import shutil
            resolved = shutil.which(argv[0])
            if resolved:
                argv[0] = resolved
        try:
            if profile == "writeable":
                res = self._run_logged(argv, workdir, timeout)   # executor → live agent.log
            else:
                res = self._run_plain(argv, workdir, timeout)    # validator → pipe (not logged)
            if last_message and res.status == "success":
                answer = Path(last_message).read_text(encoding="utf-8", errors="replace").strip()
                if answer:
                    res.stdout = answer
            return res
        finally:
            if last_message:
                Path(last_message).unlink(missing_ok=True)

    def _run_plain(self, argv, workdir, timeout) -> RunResult:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=self._env, **_POPEN_GROUP)
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
                out = b""
            timed_out = True
        finally:
            self._proc = None
        lines = _Lines(StreamReader(self.engine, workdir))
        lines.push(out or b"")
        lines.close()
        return self._classify(lines.reader, proc.returncode, timed_out)

    def _run_logged(self, argv, workdir, timeout) -> RunResult:
        """Run under a PTY so the agent (a TTY app) line-buffers and FLUSHES live; tee that
        stream to <project>/agent.log as it arrives so the dashboard can follow it in real
        time. Falls back to a pipe if a PTY isn't available."""
        try:
            import pty                       # POSIX only — Windows has no pty
            import select
        except ImportError:
            return self._run_pipe_logged(argv, workdir, timeout)   # Windows: pipe + background tee
        import time
        log_path = Path(workdir).parent / "agent.log"
        try:
            master, slave = pty.openpty()
        except OSError:
            return self._run_plain(argv, workdir, timeout)   # no PTY → at least don't crash
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), stdin=subprocess.DEVNULL,
                stdout=slave, stderr=slave, close_fds=True, env=self._env, **_POPEN_GROUP)
        except FileNotFoundError:
            os.close(master); os.close(slave)
            return RunResult(status="crashed", stdout=f"{self.prefix[0]!r} not installed")
        os.close(slave)
        self._proc = proc
        lines = _Lines(StreamReader(self.engine, workdir))
        carry = b""          # a trailing partial escape held over to the next read
        timed_out = False
        deadline = time.monotonic() + max(1, timeout)
        try:
            # APPEND, never truncate: the orchestrator writes a per-iteration header before
            # each run, so agent.log accumulates every iteration (the operator reads the whole
            # history, not just the latest run that used to overwrite it).
            with open(log_path, "a", encoding="utf-8") as lf:
                def put(text: str) -> None:
                    if text:
                        lf.write(text); lf.flush()
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
                        put(lines.push(_ANSI.sub(b"", buf)))
                    elif proc.poll() is not None:
                        break
                put(lines.push(_ANSI.sub(b"", carry)))    # an escape that never completed
                put(lines.close())
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
        return self._classify(lines.reader, proc.returncode if proc.returncode is not None else 0,
                              timed_out)

    def _run_pipe_logged(self, argv, workdir, timeout) -> RunResult:
        """No-PTY fallback (Windows): run the agent over a pipe and tee its output to
        <project>/agent.log from a background thread; the main thread enforces the timeout."""
        import threading
        log_path = Path(workdir).parent / "agent.log"
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self._env, **_POPEN_GROUP)
        except FileNotFoundError:
            return RunResult(status="crashed", stdout=f"{self.prefix[0]!r} not installed")
        self._proc = proc
        lines = _Lines(StreamReader(self.engine, workdir))

        def _pump():
            try:
                with open(log_path, "a", encoding="utf-8") as lf:   # APPEND, like the PTY path
                    for raw in iter(proc.stdout.readline, b""):
                        text = lines.push(_ANSI.sub(b"", raw))
                        if text:
                            lf.write(text); lf.flush()
                    text = lines.close()
                    if text:
                        lf.write(text); lf.flush()
            except Exception:
                pass

        th = threading.Thread(target=_pump, daemon=True)
        th.start()
        timed_out = False
        try:
            proc.wait(timeout=max(1, timeout))
        except subprocess.TimeoutExpired:
            self.kill(); timed_out = True
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        finally:
            self._proc = None
        th.join(timeout=15)          # let the pump finish parsing what is buffered (answer, usage)
        rc = proc.returncode if proc.returncode is not None else 0
        return self._classify(lines.reader, rc, timed_out)


class _Lines:
    """Output bytes in any chunking → whole text lines → StreamReader → text for the log."""

    def __init__(self, reader: StreamReader):
        self.reader = reader
        self._decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode
        self._buf = ""
        self._closed = False

    def push(self, data: bytes | str) -> str:
        if not data:
            return ""
        self._buf += data if isinstance(data, str) else self._decode(data)
        if "\n" not in self._buf:
            return ""
        *done, self._buf = self._buf.split("\n")
        return "".join(self.reader.feed(line) for line in done)

    def close(self) -> str:
        if self._closed:
            return ""
        self._closed = True
        rest, self._buf = self._buf + self._decode(b"", final=True), ""
        return (self.reader.feed(rest) if rest.strip() else "") + self.reader.finish()
