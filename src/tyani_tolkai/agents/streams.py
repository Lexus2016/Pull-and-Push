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


class StreamReader:
    def __init__(self, engine: str | None):
        self.engine = engine
        self.usage: Usage | None = None
        self.answer: str | None = None
        self.actions = 0
        self._log: list[str] = []
        self._delta = ""                  # grok / agy stream text in pieces; flushed per line
        self._since_tool: list[str] = []  # text after the last tool call = the final answer
        self._msg_usage: dict[str, Usage] = {}   # claude: per-message usage until the result
        self._warned: set[str] = set()

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
            return self._emit(self._flush_delta() + (line + "\n" if line else ""))
        handler = getattr(self, "_" + self.engine, None)
        return self._emit(handler(obj) if handler else "")

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
        return self._flush_delta() + f"▸ {name}{' ' + detail if detail else ''}\n"

    def _warn(self, message) -> str:
        """An error / warning line, once: codex repeats its config warnings on every start."""
        line = f"! {_short(message, 300)}\n"
        if line in self._warned:
            return ""
        self._warned.add(line)
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
            u = e.get("usage") or {}
            cost = e.get("total_cost_usd")
            self.usage = Usage(
                input=_int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")),
                cached=_int(u.get("cache_read_input_tokens")), output=_int(u.get("output_tokens")),
                cost_usd=float(cost) if isinstance(cost, (int, float)) else None)
            if isinstance(e.get("result"), str):
                self.answer = e["result"].strip()
            if e.get("is_error"):
                return f"! {e.get('subtype', 'error')}: {_short(e.get('result', ''), 300)}\n"
            return ""
        if t == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            if info.get("status") == "rejected":
                return f"! rate limit reached ({info.get('rateLimitType', 'limit')})\n"
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
            u = e.get("usage") or {}
            cached = _int(u.get("cached_input_tokens"))
            self._add_usage(Usage(input=max(0, _int(u.get("input_tokens")) - cached), cached=cached,
                                  output=_int(u.get("output_tokens"))))
            return ""
        if t in ("turn.failed", "error"):
            err = e.get("error") if isinstance(e.get("error"), dict) else e
            return f"! {_short(err.get('message', t), 300)}\n"
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
            u = e.get("usage") or {}
            cost = e.get("total_cost_usd")
            self.usage = Usage(
                input=_int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")),
                cached=_int(u.get("cache_read_input_tokens")), output=_int(u.get("output_tokens")),
                cost_usd=float(cost) if isinstance(cost, (int, float)) else None)
            return ""
        if t == "error":
            return self._flush_delta() + f"! {_short(e.get('message') or e.get('data') or e, 300)}\n"
        return ""

    def _opencode(self, e: dict) -> str:
        t = e.get("type")
        part = e.get("part") or {}
        if t == "tool_use":
            state = part.get("state") or {}
            out = self._tool(part.get("tool", "tool"), state.get("input"))
            if state.get("status") == "error":
                out += f"! {_short(state.get('error', 'tool failed'), 300)}\n"
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
            return f"! {_short(msg or err, 300)}\n"
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
                return self._flush_delta() + f"! {r.get('status')}: {_short(r.get('error', ''), 300)}\n"
        return ""
