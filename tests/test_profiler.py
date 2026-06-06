import json
import pytest
from pydantic import ValidationError
from tyani_tolkai.profiler import parse_profile, ProfileError

_VALID = {
    "language": "python", "framework": "custom", "entry_point": None,
    "tunable_surface": [], "data_source": None,
    "extractable_metrics": [], "risks": [], "unknowns": [],
}


def test_parse_profile_stamps_provenance():
    text = "here you go:\n```json\n" + json.dumps(_VALID) + "\n```"
    p = parse_profile(text, engine="codex", source_root="/tmp/foo")
    assert p.analyzer_engine == "codex"          # stamped by us, not the LLM
    assert p.source_root == "/tmp/foo"
    assert p.bot_name == "foo"                    # basename default
    assert p.language == "python"


def test_parse_profile_no_json_raises():
    with pytest.raises(ValueError):
        parse_profile("no json here at all", engine="claude", source_root="/tmp/x")


def test_parse_profile_invalid_schema_raises():
    bad = json.dumps({"language": "python"})     # missing required 'framework'
    with pytest.raises(ValidationError):
        parse_profile(bad, engine="claude", source_root="/tmp/x")


def test_analyzer_engine_overrides_llm_supplied():
    payload = {**_VALID, "analyzer_engine": "llm-supplied-value"}
    text = "```json\n" + json.dumps(payload) + "\n```"
    p = parse_profile(text, engine="codex", source_root="/tmp/foo")
    assert p.analyzer_engine == "codex"   # our stamped value wins


def test_bot_name_from_llm_preserved():
    payload = {**_VALID, "bot_name": "my-bot"}
    text = "```json\n" + json.dumps(payload) + "\n```"
    p = parse_profile(text, engine="codex", source_root="/tmp/foo")
    assert p.bot_name == "my-bot"   # LLM-supplied value survives setdefault


def test_build_profiler_prompt_contains_guardrails():
    from tyani_tolkai.profiler import build_profiler_prompt
    prompt = build_profiler_prompt("===== FILE: a.py =====\nx = 1\n", dropped=["big.csv"])
    # payload is embedded
    assert "===== FILE: a.py =====" in prompt
    # injection framing + output discipline are present
    assert "inert data" in prompt.lower()
    assert "unknowns" in prompt.lower()
    assert "path:line" in prompt.lower()
    assert "json" in prompt.lower()
    # dropped files are disclosed to the model
    assert "big.csv" in prompt
