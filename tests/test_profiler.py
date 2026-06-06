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
    prompt2 = build_profiler_prompt("payload", truncated=["large.py"])
    assert "large.py" in prompt2
    assert "INCLUDED ONLY IN PART" in prompt2


def test_assemble_payload_labels_files_and_skips_binary(tmp_path):
    from tyani_tolkai.profiler import assemble_payload
    (tmp_path / "strategy.py").write_text("PARAMS = {}\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# my bot\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01binary")  # non-text ext → ignored
    (tmp_path / "data.csv").write_bytes(b"\xff\xfe\x00\x01\x02\x03bad")       # text ext, undecodable → dropped
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")

    res = assemble_payload(tmp_path)

    assert "FILE: strategy.py" in res.text
    assert "FILE: README.md" in res.text
    assert "PARAMS = {}" in res.text
    assert "logo.png" not in res.text        # non-text extension: not source, not embedded, not dropped
    assert "data.csv" in res.dropped         # text ext but undecodable → recorded in dropped
    assert "HEAD" not in res.text            # .git skipped entirely
    assert res.text.index("strategy.py") < res.text.index("README.md")  # code before README


def test_assemble_payload_single_file(tmp_path):
    from tyani_tolkai.profiler import assemble_payload
    f = tmp_path / "bot.py"
    f.write_text("x = 1\n", encoding="utf-8")
    res = assemble_payload(f)
    assert "FILE: bot.py" in res.text
    assert res.dropped == []
    assert res.truncated == []


def test_assemble_payload_budget_drops_overflow(tmp_path):
    from tyani_tolkai.profiler import assemble_payload
    (tmp_path / "a.py").write_text("a" * 50, encoding="utf-8")
    (tmp_path / "b.py").write_text("b" * 5000, encoding="utf-8")
    res = assemble_payload(tmp_path, budget_chars=200)
    # at least one file dropped for budget; payload stays under a sane bound
    assert res.dropped != []
    assert len(res.text) <= 200   # must stay within the stated budget_chars


def test_assemble_payload_truncates_large_file(tmp_path):
    from tyani_tolkai.profiler import assemble_payload
    (tmp_path / "big.py").write_text("z" * 500, encoding="utf-8")
    res = assemble_payload(tmp_path, max_file_chars=10)
    assert "big.py" in res.truncated         # head-only inclusion is disclosed
    assert "[truncated]" in res.text
    assert res.dropped == []                 # truncated ≠ dropped


def test_assemble_payload_empty_dir(tmp_path):
    from tyani_tolkai.profiler import assemble_payload
    res = assemble_payload(tmp_path)
    assert res.text == ""
    assert res.dropped == []
    assert res.truncated == []


def test_assemble_payload_truncated_then_over_budget_is_dropped_only(tmp_path):
    from tyani_tolkai.profiler import assemble_payload
    (tmp_path / "big.py").write_text("z" * 500, encoding="utf-8")
    # head-only would be ~100 chars, but the budget is smaller than even that block
    res = assemble_payload(tmp_path, budget_chars=20, max_file_chars=100)
    assert "big.py" in res.dropped
    assert "big.py" not in res.truncated   # not included → must NOT claim head-only
    assert res.text == ""


def _canned(extra=None):
    import json as _json
    d = dict(_VALID)
    if extra:
        d.update(extra)
    return "```json\n" + _json.dumps(d) + "\n```"


def test_analyze_bot_happy_path(tmp_path):
    from tyani_tolkai.profiler import analyze_bot
    (tmp_path / "bot.py").write_text("PARAMS = {'leverage': 3}\n", encoding="utf-8")
    seen = {}

    def runner(prompt):
        seen["prompt"] = prompt
        return _canned()

    p = analyze_bot(tmp_path, engine="claude", runner=runner)
    assert p.analyzer_engine == "claude"
    assert p.source_root == str(tmp_path)
    assert "FILE: bot.py" in seen["prompt"]          # bot embedded in prompt, not path


def test_analyze_bot_empty_dir_no_llm_call(tmp_path):
    from tyani_tolkai.profiler import analyze_bot

    def runner(prompt):
        raise AssertionError("runner must not be called for an empty bot")

    p = analyze_bot(tmp_path, engine="claude", runner=runner)
    assert p.language == "unknown"
    assert p.entry_point is None
    assert any("could not read source" in u for u in p.unknowns)


def test_analyze_bot_records_dropped_in_unknowns(tmp_path):
    from tyani_tolkai.profiler import analyze_bot
    (tmp_path / "bot.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "data.csv").write_bytes(b"\xff\xfe\x00\x01\x02bad")   # text ext, undecodable → dropped

    p = analyze_bot(tmp_path, engine="claude", runner=lambda _p: _canned())
    assert any("data.csv" in u for u in p.unknowns)


def test_analyze_bot_discloses_truncation_in_unknowns(tmp_path):
    from tyani_tolkai.profiler import analyze_bot, MAX_FILE_CHARS
    (tmp_path / "big.py").write_text("z" * (MAX_FILE_CHARS + 1000), encoding="utf-8")

    p = analyze_bot(tmp_path, engine="claude", runner=lambda _p: _canned())
    assert any("head-only" in u and "big.py" in u for u in p.unknowns)


def test_analyze_bot_repairs_once_then_succeeds(tmp_path):
    from tyani_tolkai.profiler import analyze_bot
    (tmp_path / "bot.py").write_text("x = 1\n", encoding="utf-8")
    calls = {"n": 0}

    def runner(prompt):
        calls["n"] += 1
        return "not json" if calls["n"] == 1 else _canned()

    p = analyze_bot(tmp_path, engine="claude", runner=runner)
    assert calls["n"] == 2                            # one repair retry
    assert p.language == "python"


def test_analyze_bot_raises_after_failed_repair(tmp_path):
    from tyani_tolkai.profiler import analyze_bot, ProfileError
    (tmp_path / "bot.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ProfileError):
        analyze_bot(tmp_path, engine="claude", runner=lambda _p: "still not json")
