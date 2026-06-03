from tyani_tolkai.agents.cli_agent import CLIAgentAdapter, build_cli_prefix


def test_build_cli_prefix_engines():
    assert build_cli_prefix("claude", "opus", "writeable")[:2] == ["claude", "-p"]
    assert "--model" in build_cli_prefix("claude", "opus", "writeable")

    codex_w = build_cli_prefix("codex", None, "writeable")
    assert "workspace-write" in codex_w
    codex_r = build_cli_prefix("codex", None, "read-only")
    assert "read-only" in codex_r

    assert build_cli_prefix("opencode", None, "writeable")[:2] == ["opencode", "run"]
    assert build_cli_prefix("agy", None, "writeable") == ["agy", "-p"]


def test_cli_adapter_runs_subprocess_and_edits(tmp_path):
    # fake "agent": writes the brief ($1) into out.txt inside the workdir
    adapter = CLIAgentAdapter(["/bin/sh", "-c", 'printf "%s" "$1" > out.txt', "sh"])
    res = adapter.run("hello-brief", tmp_path, "writeable", 10)
    assert res.status == "success"
    assert (tmp_path / "out.txt").read_text() == "hello-brief"


def test_cli_adapter_missing_binary(tmp_path):
    adapter = CLIAgentAdapter(["definitely-not-a-real-binary-xyz-123"])
    res = adapter.run("brief", tmp_path, "writeable", 10)
    assert res.status == "crashed"
    assert "not installed" in res.stdout
