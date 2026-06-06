import json
import pytest
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
    with pytest.raises(Exception):               # pydantic ValidationError
        parse_profile(bad, engine="claude", source_root="/tmp/x")
