"""Agents' streaming output → readable log, final answer, real usage; isolation; cost accounting.

The event shapes below are cut down from real output of claude 2.x stream-json, codex exec --json,
grok --output-format streaming-json, opencode run --format json and agy --output-format stream-json
(captured 2026-09-29).
"""

import json
import sys
from pathlib import Path

import pytest

from tyani_tolkai.agents.base import RunResult, Usage
from tyani_tolkai.agents.cli_agent import (_ENV, _EXECUTOR_FOCUS, CLIAgentAdapter, _codex_mcp_off,
                                           build_cli_prefix)
from tyani_tolkai.agents.streams import StreamReader
from tyani_tolkai.config import Config
from tyani_tolkai.orchestrator import Orchestrator
from tyani_tolkai.sandbox import LocalBackend
from tyani_tolkai.state import StateStore


def _read(engine, events):
    r = StreamReader(engine)
    log = "".join(r.feed(e if isinstance(e, str) else json.dumps(e)) for e in events) + r.finish()
    return r, log


CLAUDE = [
    {"type": "system", "subtype": "init", "tools": ["Write"]},
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/solution.py", "content": "x"}}],
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 146, "cache_read_input_tokens": 24674,
                  "output_tokens": 38}}},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "File created"}]}},
    {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "DONE"}],
                                      "usage": {"input_tokens": 2, "output_tokens": 107}}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "DONE",
     "total_cost_usd": 0.2070508,
     "usage": {"input_tokens": 4, "cache_creation_input_tokens": 24820, "cache_read_input_tokens": 24674,
               "output_tokens": 177}},
]


def test_claude_stream_gives_actions_answer_and_its_own_cost():
    r, log = _read("claude", CLAUDE)
    assert "▸ Write /w/solution.py" in log and "DONE" in log
    assert "rate" not in log                     # an "allowed" rate-limit report is not news
    assert r.answer == "DONE" and r.actions == 1
    assert r.usage == Usage(input=24824, cached=24674, output=177, cost_usd=pytest.approx(0.2070508))


def test_claude_cut_short_still_counts_what_it_reported():
    # a timeout kills the agent before the final "result": usage comes from the messages seen
    r, _ = _read("claude", CLAUDE[:2])
    assert r.usage == Usage(input=148, cached=24674, output=38) and r.usage.cost_usd is None


def test_claude_rejected_rate_limit_is_rendered_as_one():
    r, log = _read("claude", [{"type": "rate_limit_event",
                               "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}])
    assert "rate limit reached" in log


def test_codex_stream():
    warn = {"type": "item.completed", "item": {"type": "error", "message": "Codex is ignoring 3 settings"}}
    r, log = _read("codex", [
        {"type": "thread.started"}, warn, warn,
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [
            {"path": "/w/ok.txt", "kind": "add"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}},
        {"type": "turn.completed", "usage": {"input_tokens": 47990, "cached_input_tokens": 23808,
                                             "output_tokens": 61, "reasoning_output_tokens": 16}},
    ])
    assert log.count("Codex is ignoring") == 1           # a repeated warning is logged once
    assert "▸ $ ls  → exit 0" in log and "▸ add /w/ok.txt" in log
    assert r.answer == "DONE" and r.actions == 2
    assert r.usage == Usage(input=24182, cached=23808, output=61)     # tokens only, no $


def test_grok_stream_joins_text_deltas_and_takes_the_end_totals():
    r, log = _read("grok", [
        {"type": "available_commands", "tools": ["write"]},
        {"type": "text", "data": "I'll"}, {"type": "text", "data": " create it."},
        {"type": "usage", "usage": {"input_tokens": 19951, "output_tokens": 202}},
        {"type": "tool_call", "title": "write", "rawInput": {"file_path": "/w/ok.txt"}},
        {"type": "text", "data": "DO"}, {"type": "text", "data": "NE"},
        {"type": "end", "total_cost_usd": 0.018, "usage": {
            "input_tokens": 20332, "cache_read_input_tokens": 22144, "output_tokens": 213}},
    ])
    assert "I'll create it.\n▸ write /w/ok.txt\nDONE\n" == log
    assert r.answer == "DONE"                            # the text after the last tool call
    assert r.usage == Usage(input=20332, cached=22144, output=213, cost_usd=0.018)


def test_opencode_stream_sums_steps_and_treats_zero_cost_as_unpriced():
    step = {"type": "step_finish", "part": {"cost": 0, "tokens": {
        "input": 100, "output": 5, "reasoning": 20, "cache": {"read": 1000, "write": 3}}}}
    r, log = _read("opencode", [
        {"type": "tool_use", "part": {"tool": "write", "state": {"status": "completed",
                                                                 "input": {"filePath": "/w/ok.txt"}}}},
        step, {"type": "text", "part": {"text": "DONE"}}, step])
    assert "▸ write /w/ok.txt" in log and r.answer == "DONE"
    assert r.usage == Usage(input=206, cached=2000, output=50) and r.usage.cost_usd is None


def test_agy_stream_names_mcp_calls_and_reads_the_result():
    r, log = _read("agy", [
        {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE",
            "tool_name": "call_mcp_tool", "tool_info": {"parameters": {"ServerName": "tqmemory",
                                                                       "ToolName": "health"}}}},
        {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE",
            "text_delta": "All done.\n", "usage": {"input_tokens": 20772, "output_tokens": 1342,
                                                    "cache_read_tokens": 16300}}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "All done."}},
    ])
    assert "▸ tqmemory.health" in log and "All done." in log
    assert r.answer == "All done." and r.usage == Usage(input=20772, cached=16300, output=1342)


def test_plain_text_and_unknown_engines_pass_through():
    r, log = _read(None, ['{"type":"result"}', "plain line"])
    assert log == '{"type":"result"}\nplain line\n' and r.usage is None
    r, log = _read("claude", ["Reading additional input from stdin...", "not json {"])
    assert log == "Reading additional input from stdin...\nnot json {\n"


# ---- the adapter end to end, with a fake CLI that speaks claude's stream-json ----

def _fake_cli(tmp_path, events, exit_code=0):
    script = tmp_path / "fake_agent.py"
    script.write_text("import sys, json\n"
                      f"for e in {events!r}:\n"
                      "    print(json.dumps(e), flush=True)\n"
                      f"sys.exit({exit_code})\n", encoding="utf-8")
    return [sys.executable, str(script)]


def test_adapter_logs_readable_lines_and_returns_answer_and_usage(tmp_path):
    work = tmp_path / "artifact"
    work.mkdir()
    res = CLIAgentAdapter(_fake_cli(tmp_path, CLAUDE), engine="claude").run("brief", work, "writeable", 30)
    log = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert res.status == "success" and res.stdout == "DONE" and res.actions == 1
    assert res.usage.cost_usd == pytest.approx(0.2070508)
    assert "▸ Write /w/solution.py" in log and '"type"' not in log      # no raw JSON in the log


def test_a_crash_is_not_mistaken_for_a_rate_limit(tmp_path):
    # claude reports its rate-limit state ("allowed") in every run; a crashed run used to match
    # "rate_limit" in the raw stream and paused the whole research
    work = tmp_path / "artifact"
    work.mkdir()
    res = CLIAgentAdapter(_fake_cli(tmp_path, CLAUDE[:3], exit_code=1), engine="claude").run(
        "brief", work, "read-only", 30)
    assert res.status == "crashed"


# ---- isolation from the operator's personal setup ----

def test_every_engine_streams_json_and_skips_personal_setup():
    claude = build_cli_prefix("claude", None, "writeable")
    assert claude[claude.index("--settings") + 1] == '{"disableAllHooks":true}'
    assert "--disable-slash-commands" in claude and claude[claude.index("--output-format") + 1] == "stream-json"
    codex = build_cli_prefix("codex", None, "read-only")
    assert "--json" in codex and any(a.startswith("developer_instructions=") for a in codex)
    assert "--pure" in build_cli_prefix("opencode", None, "writeable")
    agy = build_cli_prefix("agy", None, "writeable")
    assert "--disable-slash-commands" in agy and agy[agy.index("--output-format") + 1] == "stream-json"
    grok = build_cli_prefix("grok", None, "writeable")
    assert grok[grok.index("--output-format") + 1] == "streaming-json"
    assert _ENV["grok"]["GROK_CLAUDE_RULES_ENABLED"] == "0" and _ENV["grok"]["GROK_CLAUDE_MCPS_ENABLED"] == "0"
    assert _ENV["opencode"] == {"OPENCODE_DISABLE_CLAUDE_CODE": "1"}


def test_codex_brief_is_a_valid_toml_string():
    codex = build_cli_prefix("codex", None, "writeable")
    value = next(a for a in codex if a.startswith("developer_instructions=")).split("=", 1)[1]
    assert json.loads(value) == _EXECUTOR_FOCUS          # JSON string == TOML basic string


def test_codex_mcp_servers_from_the_operators_config_are_switched_off(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        'model = "x"\n[mcp_servers.tqmemory]\ncommand = "t"\n[mcp_servers.tqmemory.tools.health]\n'
        'approval = "auto"\n[mcp_servers.serena]\ncommand = "uvx"\n[profiles.fast]\nmodel = "y"\n',
        encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert _codex_mcp_off() == ("-c", "mcp_servers.tqmemory.enabled=false",
                                "-c", "mcp_servers.serena.enabled=false")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert _codex_mcp_off() == ()


def test_isolation_env_reaches_the_agent(tmp_path, monkeypatch):
    script = tmp_path / "env_agent.py"
    script.write_text("import os; print(os.environ.get('OPENCODE_DISABLE_CLAUDE_CODE'))", encoding="utf-8")
    work = tmp_path / "artifact"
    work.mkdir()
    res = CLIAgentAdapter([sys.executable, str(script)], engine="opencode").run("b", work, "read-only", 30)
    assert res.stdout.strip() == "1"


# ---- cost accounting ----

def _cfg(**limits):
    return Config(project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
                  evaluation={"adapter": "numeric", "command": "true",
                              "metrics": [{"name": "s", "dir": "higher", "worst": 0, "target": 100}]},
                  limits=limits)


def _orch(tmp_path, cfg):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    return Orchestrator(cfg, s, s.create_run("asymmetric"), None, None, LocalBackend())


def test_charge_prefers_what_the_cli_reported(tmp_path):
    o = _orch(tmp_path, _cfg(usd_per_mtok=10))
    o._charge(RunResult("success", usage=Usage(input=1000, cached=0, output=0, cost_usd=0.25)))
    assert o.cost_total == pytest.approx(0.25)
    # tokens only → priced, cache reads at 10%
    o._charge(RunResult("success", usage=Usage(input=1_000_000, cached=1_000_000, output=0)))
    assert o.cost_total == pytest.approx(0.25 + 10 + 1)
    assert o.cost_measured and o.tokens_total == 2_001_000


def test_charge_marks_guesses_and_unpriced_tokens(tmp_path):
    o = _orch(tmp_path, _cfg())                                         # no usd_per_mtok
    o._charge(RunResult("success", usage=Usage(input=500, output=50)))
    assert o.cost_total == 0 and not o.cost_measured                     # tokens known, no price
    o = _orch(tmp_path / "b", _cfg(usd_per_mtok=4))
    o._charge(RunResult("success", stdout="x" * 400), prompt="y" * 3600)   # CLI reported nothing
    assert o.cost_total == pytest.approx(1000 / 1e6 * 4) and not o.cost_measured


def test_every_executor_attempt_is_charged(tmp_path):
    class Flaky:
        calls = 0

        def run(self, brief, workdir, profile, timeout):
            Flaky.calls += 1
            return RunResult("crashed", usage=Usage(cost_usd=0.1))

    o = _orch(tmp_path, _cfg(agent_retries=2))
    o.executor = Flaky()
    o._run_executor("brief")
    assert Flaky.calls == 3 and o.cost_total == pytest.approx(0.3)


def test_a_timeout_that_only_reasoned_says_so(tmp_path):
    class Thinker:
        def run(self, brief, workdir, profile, timeout):
            return RunResult("timeout", stdout="…", actions=0)

    o = _orch(tmp_path, _cfg(agent_retries=0))
    o.executor = Thinker()
    out = o.run_iteration()
    assert out.verdict == "fail" and "only reasoned — not a single action" in out.feedback


# ---- the budget warning knows which engines price themselves ----

def test_budget_warning_only_for_engines_that_report_tokens_alone(recwarn):
    base = dict(project="p", roles={"executor": {"goal": "g"}},
                evaluation={"adapter": "numeric", "command": "true",
                            "metrics": [{"name": "s", "dir": "higher", "worst": 0, "target": 100}]},
                limits={"budget_usd": 5})
    Config(**base, agents={"executor": {"engine": "claude"}, "validator": {"engine": "grok"}})
    assert not [w for w in recwarn if "usd_per_mtok" in str(w.message)]
    with pytest.warns(UserWarning, match="codex report only tokens"):
        Config(**base, agents={"executor": {"engine": "claude"}, "validator": {"engine": "codex"}})
