"""Read an agent CLI's machine-readable output stream (one JSON event per line).

Every engine runs in its streaming JSON mode (claude stream-json, codex --json, grok
streaming-json, opencode --format json, agy stream-json). This turns that stream into:
  * a readable live log — one line per action ("▸ Write solution.py"), plus the agent's text;
  * the final answer (what a reviewer or helper said), without the CLI's framing;
  * the real usage — tokens, and the dollar cost where the CLI prices the call itself.
Lines that are not JSON (stderr, banners) pass through to the log unchanged, and an unknown
engine passes everything through, so a plain-text agent still logs exactly as before.
"""

from __future__ import annotations

import json
import os
import re

from .base import Usage

_DETAIL_KEYS = ("file_path", "filePath", "path", "TargetFile", "AbsolutePath", "command",
                "CommandLine", "pattern", "query", "Query", "url")


def _short(text, limit: int = 160) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def _detail(args) -> str:
    """The one argument that says what a tool call touched (a path, a command, a query)."""
    if not isinstance(args, dict):
        return ""
    for k in _DETAIL_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            if len(v) > 120 and " " not in v:      # a long path: its end names the file
                return "…" + v[-119:]
            return _short(v, 120)
    return ""


def _int(v) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


# ---- actions outside the artifact folder ----
# An executor is told to work only in its folder, but a CLI with a shell and file tools can read
# anything: in a live arena check grok ran sqlite3 on the run DB, searched the disk with rg and
# read the referee's source for the answer. That cannot be blocked without an OS sandbox, so every
# action that reaches outside the folder is flagged — in the log, the iteration and the next brief.
_PATHISH = re.compile(r"(?<![\w.:/-])(?:~|\.\.(?=[/\\])|/(?!/)|[A-Za-z]:[/\\])[^\s'\"`;|&<>()]*")
# what an agent WRITES into a file is not where it goes — only targets, commands and queries count
_CONTENT_KEYS = {"content", "new_string", "old_string", "newText", "oldText", "text", "edits",
                 "patch", "CodeContent", "ReplacementContent", "ReplacementChunks", "Instruction",
                 # a search pattern or a query is data ("/api/users"), not a place
                 "pattern", "query", "Query", "regex", "description", "prompt", "title"}
# normalised like every path we compare (realpath + normcase): on Windows "/bin" is C:\\bin
_SYSTEM = tuple(dict.fromkeys(os.path.normcase(os.path.realpath(p)) for p in (
    "/bin", "/sbin", "/usr", "/opt", "/etc", "/dev", "/System", "/Library", "/Applications",
    "/private/etc", "/var/folders", "/private/var/folders", "/nix",
    *(os.environ.get(v) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
      if os.environ.get(v)))))


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _escapes(token: str, root: str, must_exist: bool = False) -> bool:
    """Does a path (absolute, ~, or relative with ..) point outside ``root``? System locations
    (interpreters, shells) don't count — unless the artifact itself lives there (a temp dir).
    ``must_exist``: for words inside a command, only a path that is really there counts
    (``grep "/api/users"`` mentions a route, not a place)."""
    p = os.path.expanduser(token.strip("'\""))
    if not p or p in ("/", "~"):
        return p == "/"
    full = os.path.normcase(os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p)))
    if _under(full, root) or (must_exist and not os.path.exists(full)):
        return False
    return not any(_under(full, s) and not _under(root, s) for s in _SYSTEM)


class StreamReader:
    def __init__(self, engine: str | None, workdir=None):
        self.engine = engine
        self._root = os.path.normcase(os.path.realpath(str(workdir))) if workdir else None
        self.usage: Usage | None = None
        self.answer: str | None = None
        self.actions = 0
        self.outside: list[str] = []      # actions that reached outside the artifact folder
        self._log: list[str] = []
        self._delta = ""                  # grok / agy stream text in pieces; flushed per line
        self._since_tool: list[str] = []  # text after the last tool call = the final answer
        self._msg_usage: dict[str, Usage] = {}   # claude: per-message usage until the result
        self._warned: set[str] = set()
        self._calls: set[str] = set()     # opencode: tool calls already shown
        self._signals: list[str] = []     # the CLI's own lines (errors, stderr) — not the agent's prose
        self.failed = False               # the CLI reported a fatal error (whatever its exit code)

    # ---- public ----

    def feed(self, line: str) -> str:
        """One line of output → the text to append to the live log ("" = nothing)."""
        line = line.rstrip("\r\n")
        obj = None
        if self.engine and line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
        if not isinstance(obj, dict):
            if line:
                self._signals.append(line + "\n")
            return self._emit(self._flush_delta() + (line + "\n" if line else ""))
        handler = getattr(self, "_" + self.engine, None)
        out = handler(obj) if handler else ""
        if not out and obj.get("error") and not obj.get("type") and not obj.get("event"):
            err = obj["error"]                   # a JSON error in no known shape: never drop it
            out = self._warn(err.get("message", err) if isinstance(err, dict) else err)
        return self._emit(out)

    def finish(self) -> str:
        """End of stream: flush pending text, settle usage from partial reports (a timeout)."""
        out = self._emit(self._flush_delta())
        if self.usage is None and self._msg_usage:
            total = Usage()
            for u in self._msg_usage.values():
                total.add(u)
            self.usage = total
        if self.answer is None and self._since_tool:
            text = "".join(self._since_tool).strip()
            self.answer = text or None
        return out

    @property
    def rendered(self) -> str:
        return "".join(self._log)

    @property
    def signals(self) -> str:
        """What the CLI itself said (errors, warnings, stderr) — where a rate limit is looked for;
        the agent's own prose may well discuss rate limits."""
        return "".join(self._signals)

    # ---- helpers ----

    def _emit(self, text: str) -> str:
        if text:
            self._log.append(text)
        return text

    def _flush_delta(self) -> str:
        text, self._delta = self._delta, ""
        return text if not text or text.endswith("\n") else text + "\n"

    def _text(self, text: str) -> str:
        text = (text or "").strip("\n")
        if not text.strip():
            return ""
        self._since_tool.append(text + "\n")
        return self._flush_delta() + text + "\n"

    def _stream_text(self, piece: str) -> str:
        """A text delta: buffer it, emit only complete lines (the rest waits for more)."""
        self._since_tool.append(piece)
        self._delta += piece
        if "\n" not in self._delta:
            return ""
        done, _, self._delta = self._delta.rpartition("\n")
        return done + "\n"

    def _tool(self, name: str, args=None) -> str:
        self.actions += 1
        self._since_tool = []
        detail = _detail(args)
        line = f"▸ {name}{' ' + detail if detail else ''}"
        if self._reaches_outside(args):
            self.outside.append(line[2:])
            line += "   ⚠ outside the artifact folder"
        return self._flush_delta() + line + "\n"

    def _reaches_outside(self, args) -> bool:
        if not self._root or not isinstance(args, dict):
            return False
        for k, v in args.items():
            if k in _CONTENT_KEYS or not isinstance(v, str) or not v.strip():
                continue
            if "\n" not in v and len(v) < 1024 and " " not in v.strip() and _escapes(v, self._root):
                return True                     # a path argument (file_path, TargetFile, …)
            if any(_escapes(t, self._root, must_exist=True) for t in _PATHISH.findall(v[:4096])):
                return True                     # an existing path inside a command
        return False

    def _warn(self, message, fatal: bool = False) -> str:
        """An error / warning line, once: codex repeats its config warnings on every start."""
        self.failed = self.failed or fatal
        line = f"! {_short(message, 300)}\n"
        if line in self._warned:
            return ""
        self._warned.add(line)
        self._signals.append(line)
        return self._flush_delta() + line

    def _add_usage(self, u: Usage) -> None:
        if self.usage is None:
            self.usage = Usage()
        self.usage.add(u)

    # ---- engines ----

    def _claude(self, e: dict) -> str:
        t = e.get("type")
        if t == "assistant":
            msg = e.get("message") or {}
            u = msg.get("usage")
            if isinstance(u, dict) and msg.get("id"):
                self._msg_usage[msg["id"]] = Usage(
                    input=_int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")),
                    cached=_int(u.get("cache_read_input_tokens")), output=_int(u.get("output_tokens")))
            out = ""
            for block in msg.get("content") or []:
                if block.get("type") == "text":
                    out += self._text(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    out += self._tool(block.get("name", "tool"), block.get("input"))
            return out
        if t == "result":
            u, cost = e.get("usage"), e.get("total_cost_usd")
            if isinstance(u, dict) or isinstance(cost, (int, float)):   # absent ≠ a measured zero
                u = u if isinstance(u, dict) else {}
                self.usage = Usage(
                    input=_int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")),
                    cached=_int(u.get("cache_read_input_tokens")), output=_int(u.get("output_tokens")),
                    cost_usd=float(cost) if isinstance(cost, (int, float)) else None)
            if isinstance(e.get("result"), str):
                self.answer = e["result"].strip()
            if e.get("is_error"):
                return self._warn(f"{e.get('subtype', 'error')}: {e.get('result', '')}", fatal=True)
            return ""
        if t == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            if info.get("status") == "rejected":
                return self._warn(f"rate limit reached ({info.get('rateLimitType', 'limit')})")
        return ""

    def _codex(self, e: dict) -> str:
        t = e.get("type")
        if t == "item.completed":
            item = e.get("item") or {}
            kind = item.get("type")
            if kind == "agent_message":
                self.answer = (item.get("text") or "").strip() or self.answer
                return self._text(item.get("text", ""))
            if kind == "command_execution":
                out = self._tool("$", {"command": item.get("command", "")})
                code = item.get("exit_code")
                return out.rstrip("\n") + (f"  → exit {code}" if code is not None else "") + "\n"
            if kind == "file_change":
                return "".join(self._tool(c.get("kind", "edit"), {"path": c.get("path", "")})
                               for c in item.get("changes") or [])
            if kind == "mcp_tool_call":
                return self._tool(f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}")
            if kind == "web_search":
                return self._tool("web search", {"query": item.get("query", "")})
            if kind == "error":
                return self._warn(item.get("message", ""))
            return ""
        if t == "turn.completed":
            u = e.get("usage")
            if isinstance(u, dict):
                cached = _int(u.get("cached_input_tokens"))
                self._add_usage(Usage(input=max(0, _int(u.get("input_tokens")) - cached),
                                      cached=cached, output=_int(u.get("output_tokens"))))
            return ""
        if t in ("turn.failed", "error"):
            err = e.get("error") if isinstance(e.get("error"), dict) else e
            return self._warn(err.get("message", t), fatal=True)
        return ""

    def _grok(self, e: dict) -> str:
        t = e.get("type")
        if t == "text":
            return self._stream_text(e.get("data") or "")
        if t == "tool_call":
            return self._tool(e.get("title") or e.get("toolName") or "tool", e.get("rawInput"))
        if t == "usage":                  # per model call — kept in case the stream is cut short
            u = e.get("usage") or {}
            self._add_usage(Usage(
                input=_int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")),
                cached=_int(u.get("cache_read_input_tokens")), output=_int(u.get("output_tokens"))))
            return ""
        if t == "end":
            u, cost = e.get("usage"), e.get("total_cost_usd")
            if isinstance(u, dict) or isinstance(cost, (int, float)):   # absent ≠ a measured zero
                u = u if isinstance(u, dict) else {}
                self.usage = Usage(
                    input=_int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")),
                    cached=_int(u.get("cache_read_input_tokens")), output=_int(u.get("output_tokens")),
                    cost_usd=float(cost) if isinstance(cost, (int, float)) else None)
            return ""
        if t == "error":       # not treated as fatal: grok's error events are not documented
            return self._warn(e.get("message") or e.get("data") or e)
        return ""

    def _opencode(self, e: dict) -> str:
        t = e.get("type")
        part = e.get("part") or {}
        if t == "tool_use":
            call = part.get("callID")
            if call and call in self._calls:     # the same call again (a state update)
                return ""
            if call:
                self._calls.add(call)
            state = part.get("state") or {}
            out = self._tool(part.get("tool", "tool"), state.get("input"))
            if state.get("status") == "error":
                out += self._warn(state.get("error", "tool failed"))
            return out
        if t == "text":
            return self._text(part.get("text", ""))
        if t == "step_finish":
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            cost = part.get("cost")
            self._add_usage(Usage(
                input=_int(tok.get("input")) + _int(cache.get("write")), cached=_int(cache.get("read")),
                output=_int(tok.get("output")) + _int(tok.get("reasoning")),
                # 0 = a model opencode has no price for, not a free call → leave it to usd_per_mtok
                cost_usd=float(cost) if isinstance(cost, (int, float)) and cost > 0 else None))
            return ""
        if t == "error":
            err = e.get("error") or {}
            msg = (err.get("data") or {}).get("message") if isinstance(err, dict) else err
            return self._warn(msg or err, fatal=True)
        return ""

    def _agy(self, e: dict) -> str:
        ev = e.get("event")
        if ev == "step_update":
            s = e.get("step_update") or {}
            if s.get("step_type") == "agent_response":
                out = self._stream_text(s.get("text_delta") or "")
                u = s.get("usage")
                if s.get("state") == "DONE" and isinstance(u, dict):
                    self._add_usage(Usage(input=_int(u.get("input_tokens")),
                                          cached=_int(u.get("cache_read_tokens")),
                                          output=_int(u.get("output_tokens"))))
                return out
            if s.get("step_type") == "tool" and s.get("state") == "ACTIVE":
                info = s.get("tool_info") or {}
                params = info.get("parameters") or {}
                name = s.get("tool_name") or info.get("name") or "tool"
                if isinstance(params, dict) and params.get("ServerName"):   # an MCP call
                    name = f"{params['ServerName']}.{params.get('ToolName', 'tool')}"
                return self._tool(name, params)
            return ""
        if ev == "result":
            r = e.get("result") or {}
            if isinstance(r.get("response"), str):
                self.answer = r["response"].strip()
            if r.get("status") not in (None, "SUCCESS"):
                return self._warn(f"{r.get('status')}: {r.get('error', '')}", fatal=True)
        return ""
