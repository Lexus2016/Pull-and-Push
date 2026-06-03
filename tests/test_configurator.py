import pytest

from tyani_tolkai.configurator import build_configurator_prompt, extract_json, generate_config


def test_extract_plain():
    assert extract_json('{"a": 1}')["a"] == 1


def test_extract_fenced():
    assert extract_json('here:\n```json\n{"a": 2}\n```\nok')["a"] == 2


def test_extract_prose_surrounded():
    assert extract_json('blah blah {"x": {"y": 3}} trailing')["x"]["y"] == 3


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


def test_generate_with_injected_runner():
    out = '```json\n{"project": "p", "mode": "asymmetric"}\n```'
    cfg = generate_config("desc", runner=lambda prompt: out)
    assert cfg["project"] == "p" and cfg["mode"] == "asymmetric"
