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


def test_cli_adapter_adds_dir_flags(tmp_path):
    from unittest.mock import patch
    import subprocess

    # Test claude adds --add-dir
    adapter = CLIAgentAdapter(["claude", "-p"], engine="claude")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="done")
        adapter.run("hello-prompt", tmp_path, "writeable", 10)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        argv = args[0]
        assert argv == ["claude", "-p", "--add-dir", str(tmp_path), "hello-prompt"]
        assert kwargs["cwd"] == str(tmp_path)

    # Test agy adds --add-dir
    adapter_agy = CLIAgentAdapter(["agy", "-p"], engine="agy")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="done")
        adapter_agy.run("hello-prompt", tmp_path, "writeable", 10)
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert argv == ["agy", "-p", "--add-dir", str(tmp_path), "hello-prompt"]

    # Test codex adds -C
    adapter_codex = CLIAgentAdapter(["codex", "exec"], engine="codex")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="done")
        adapter_codex.run("hello-prompt", tmp_path, "writeable", 10)
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert argv == ["codex", "exec", "-C", str(tmp_path), "hello-prompt"]

    # Test opencode does not add dir flags
    adapter_opencode = CLIAgentAdapter(["opencode", "run"], engine="opencode")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="done")
        adapter_opencode.run("hello-prompt", tmp_path, "writeable", 10)
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert argv == ["opencode", "run", "hello-prompt"]
