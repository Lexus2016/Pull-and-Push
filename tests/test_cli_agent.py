from tyani_tolkai.agents.cli_agent import CLIAgentAdapter, build_cli_prefix


def test_build_cli_prefix_engines():
    assert build_cli_prefix("claude", "opus", "writeable")[:2] == ["claude", "-p"]
    assert "--model" in build_cli_prefix("claude", "opus", "writeable")

    codex_w = build_cli_prefix("codex", None, "writeable")
    assert "workspace-write" in codex_w
    codex_r = build_cli_prefix("codex", None, "read-only")
    assert "read-only" in codex_r

    assert build_cli_prefix("opencode", None, "writeable")[:2] == ["opencode", "run"]
    assert build_cli_prefix("agy", None, "writeable")[:2] == ["agy", "-p"]


def test_headless_agents_auto_approve_to_never_hang():
    # claude/opencode/agy must auto-approve tool permissions, or headless runs block
    # forever on an interactive prompt. codex exec is non-interactive via its sandbox.
    assert "--dangerously-skip-permissions" in build_cli_prefix("claude", None, "writeable")
    assert "--dangerously-skip-permissions" in build_cli_prefix("opencode", None, "writeable")
    assert "--dangerously-skip-permissions" in build_cli_prefix("agy", None, "writeable")
    assert "--sandbox" in build_cli_prefix("codex", None, "writeable")


def test_subprocess_detaches_stdin_and_new_session(tmp_path):
    # a CLI that reads stdin must not block on inherited stdin → DEVNULL; and it must run
    # in its own session so Force-Stop can kill the whole group.
    import subprocess as sp
    from unittest.mock import patch
    with patch("subprocess.Popen") as m:
        fake = m.return_value
        fake.communicate.return_value = ("", "")
        fake.returncode = 0
        fake.poll.return_value = 0
        CLIAgentAdapter(["true"], engine="claude").run("brief", tmp_path, "writeable", 5)
    k = m.call_args[1]
    assert k.get("stdin") == sp.DEVNULL
    assert k.get("start_new_session") is True


def test_kill_terminates_a_running_agent(tmp_path):
    # a real long-running child must die promptly when kill() is called from another thread
    import threading, time
    adapter = CLIAgentAdapter(["/bin/sh", "-c", "sleep 30", "sh"])
    result = {}
    t = threading.Thread(target=lambda: result.update(
        r=adapter.run("brief", tmp_path, "writeable", 30)))
    t.start()
    time.sleep(0.5)
    adapter.kill()
    t.join(timeout=5)
    assert not t.is_alive()                      # run() returned promptly, not after 30s
    assert result["r"].status == "killed"


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


def test_cli_adapter_dir_handling(tmp_path):
    """Prompt must be the final positional arg; cwd carries the working dir.
    claude/agy must NOT get a greedy --add-dir (it swallows the prompt). codex gets -C."""
    import subprocess
    from unittest.mock import patch

    def argv_for(engine, prefix):
        a = CLIAgentAdapter(prefix, engine=engine)
        with patch("subprocess.Popen") as m:
            fake = m.return_value
            fake.communicate.return_value = ("ok", "")
            fake.returncode = 0
            fake.poll.return_value = 0
            a.run("PROMPT", tmp_path, "writeable", 10)
            return m.call_args[0][0], m.call_args[1]["cwd"]

    av, cwd = argv_for("claude", ["claude", "-p"])
    assert av == ["claude", "-p", "PROMPT"] and cwd == str(tmp_path)   # no --add-dir
    av, _ = argv_for("agy", ["agy", "-p"])
    assert av == ["agy", "-p", "PROMPT"]
    av, _ = argv_for("codex", ["codex", "exec"])
    assert av == ["codex", "exec", "-C", str(tmp_path), "PROMPT"]      # single-path flag
    av, _ = argv_for("opencode", ["opencode", "run"])
    assert av == ["opencode", "run", "PROMPT"]
