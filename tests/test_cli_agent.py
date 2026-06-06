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
    # claude/agy must auto-approve tool permissions, or headless runs block forever on an
    # interactive prompt. `opencode run` is non-interactive by default (no such flag) and
    # `codex exec` is non-interactive via its sandbox.
    assert "--dangerously-skip-permissions" in build_cli_prefix("claude", None, "writeable")
    assert "--dangerously-skip-permissions" in build_cli_prefix("agy", None, "writeable")
    assert "--dangerously-skip-permissions" not in build_cli_prefix("opencode", None, "writeable")
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
        CLIAgentAdapter(["true"], engine="claude").run("brief", tmp_path, "read-only", 5)
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


def test_writeable_tees_output_to_agent_log(tmp_path):
    # executor (writeable) output is captured to <project>/agent.log for live monitoring
    wd = tmp_path / "artifact"; wd.mkdir()
    adapter = CLIAgentAdapter(["/bin/sh", "-c", "echo HELLO-AGENT", "sh"])
    res = adapter.run("brief", wd, "writeable", 10)
    assert res.status == "success" and "HELLO-AGENT" in res.stdout
    assert "HELLO-AGENT" in (tmp_path / "agent.log").read_text()   # file outside artifact tree


def test_cli_adapter_missing_binary(tmp_path):
    adapter = CLIAgentAdapter(["definitely-not-a-real-binary-xyz-123"])
    res = adapter.run("brief", tmp_path, "writeable", 10)
    assert res.status == "crashed"
    assert "not installed" in res.stdout


def test_cli_adapter_dir_handling(tmp_path):
    """Prompt must be the final positional arg, after each engine's single-path working-dir flag.
    claude relies on cwd (no flag); codex gets -C, opencode --dir, agy --add-dir."""
    import subprocess
    from unittest.mock import patch

    def argv_for(engine, prefix):
        a = CLIAgentAdapter(prefix, engine=engine)
        with patch("subprocess.Popen") as m:
            fake = m.return_value
            fake.communicate.return_value = ("ok", "")
            fake.returncode = 0
            fake.poll.return_value = 0
            a.run("PROMPT", tmp_path, "read-only", 10)
            return m.call_args[0][0], m.call_args[1]["cwd"]

    av, cwd = argv_for("claude", ["claude", "-p"])
    assert av == ["claude", "-p", "PROMPT"] and cwd == str(tmp_path)   # claude: cwd only, no flag
    av, _ = argv_for("agy", ["agy", "-p"])
    assert av == ["agy", "-p", "--add-dir", str(tmp_path), "PROMPT"]   # agy: --add-dir workspace
    av, _ = argv_for("codex", ["codex", "exec"])
    assert av == ["codex", "exec", "-C", str(tmp_path), "PROMPT"]      # codex: -C single-path
    av, _ = argv_for("opencode", ["opencode", "run"])
    assert av == ["opencode", "run", "--dir", str(tmp_path), "PROMPT"] # opencode: --dir (else edits enclosing repo)


def test_ansi_stripper_removes_tty_control_noise():
    # Regression: the agent.log tee leaked terminal control bursts like "(B[>4m[<u" because the
    # old regex only matched [0-9;?] CSI params. The stripper must drop charset designation
    # (ESC(B), private-mode CSI (ESC[>4m, ESC[<u), 2-char escapes (ESC7/ESC8) and SGR colour,
    # while leaving human text (incl. UTF-8) and newlines/tabs intact.
    from tyani_tolkai.agents.cli_agent import _ANSI
    strip = lambda b: _ANSI.sub(b"", b)
    assert strip(b"\x1b(B\x1b[>4m\x1b[<u\x1b7\x1b8 hello") == b" hello"
    assert strip(b"\x1b[32mGotovo.\x1b[0m next\n") == b"Gotovo. next\n"
    plain = "Створив strategy.py\n\tок".encode()
    assert strip(plain) == plain                       # cyrillic + newline/tab untouched


def test_ansi_trailing_partial_escape_is_detected_for_carry():
    # an escape split across two PTY reads must be held back, not half-stripped at the boundary
    from tyani_tolkai.agents.cli_agent import _ANSI, _TRAIL_ESC
    buf = b"value=\x1b[3"                               # read 1 ends mid-CSI
    m = _TRAIL_ESC.search(buf)
    assert m and m.start() == len(b"value=")            # the partial escape is located
    carry, head = buf[m.start():], buf[:m.start()]
    rest = carry + b"1mRED\x1b[0m done"                 # read 2 completes it
    assert _ANSI.sub(b"", head) + _ANSI.sub(b"", rest) == b"value=RED done"


def test_popen_group_matches_platform():
    # detach kwargs differ by OS: new session on POSIX, new process group on Windows
    import os
    from tyani_tolkai.agents.cli_agent import _POPEN_GROUP
    if os.name == "nt":
        assert "creationflags" in _POPEN_GROUP and "start_new_session" not in _POPEN_GROUP
    else:
        assert _POPEN_GROUP == {"start_new_session": True}


def test_kill_on_windows_uses_taskkill(monkeypatch):
    # Windows has no killpg → kill() must shell out to taskkill /F /T /PID <pid>
    import tyani_tolkai.agents.cli_agent as cli
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        class _R:
            pass
        return _R()

    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    a = cli.CLIAgentAdapter(["x"])

    class FakeProc:
        pid = 4321
        def poll(self):
            return None       # still running

    a._proc = FakeProc()
    a.kill()
    assert seen["argv"][:4] == ["taskkill", "/F", "/T", "/PID"]
    assert seen["argv"][4] == "4321"


def test_pipe_logged_path_tees_output(tmp_path):
    # the no-PTY logged runner (the Windows path) also works on POSIX: capture + tee to agent.log
    from tyani_tolkai.agents.cli_agent import CLIAgentAdapter
    wd = tmp_path / "artifact"; wd.mkdir()
    a = CLIAgentAdapter(["/bin/sh", "-c", "echo HELLO-PIPE", "sh"])
    res = a._run_pipe_logged(["/bin/sh", "-c", "echo HELLO-PIPE", "sh"], wd, 10)
    assert res.status == "success" and "HELLO-PIPE" in res.stdout
    assert "HELLO-PIPE" in (tmp_path / "agent.log").read_text()
