import pytest

from tyani_tolkai.configurator import build_configurator_prompt, extract_json, generate_config


def test_extract_plain():
    assert extract_json('{"a": 1}')["a"] == 1


def test_extract_fenced():
    assert extract_json('here:\n```json\n{"a": 2}\n```\nok')["a"] == 2


def test_extract_prose_surrounded():
    assert extract_json('blah blah {"x": {"y": 3}} trailing')["x"]["y"] == 3


def test_extract_nested_fenced_config():
    # the realistic case: a deeply nested config inside a ```json fence
    out = ('Here is your config:\n```json\n'
           '{"project":"p","agents":{"executor":{"engine":"claude"},'
           '"validator":{"engine":"codex"}},"evaluation":{"metrics":[{"name":"s"}]}}\n'
           '```\nDone.')
    cfg = extract_json(out)
    assert cfg["agents"]["validator"]["engine"] == "codex"
    assert cfg["evaluation"]["metrics"][0]["name"] == "s"


def test_extract_empty_raises():
    with pytest.raises(ValueError):
        extract_json("   ")


def test_extract_no_json_raises():
    with pytest.raises(ValueError):
        extract_json("no braces here")


def test_build_prompt_includes_description_and_schema():
    p = build_configurator_prompt("optimize sharpe ratio")
    assert "optimize sharpe ratio" in p
    assert "JSON" in p and "adapter" in p


def test_extract_braces_inside_strings():
    # braces inside JSON string values must not fool the parser
    assert extract_json('{"task": "use { and } literally", "n": 5}')["n"] == 5


def test_extract_skips_non_json_fence():
    out = '```bash\necho hi\n```\nthen the config:\n```json\n{"ok": true}\n```'
    assert extract_json(out)["ok"] is True


def test_generate_with_injected_runner():
    out = '```json\n{"project": "p", "mode": "asymmetric"}\n```'
    cfg = generate_config("desc", runner=lambda prompt: out)
    assert cfg["project"] == "p" and cfg["mode"] == "asymmetric"
