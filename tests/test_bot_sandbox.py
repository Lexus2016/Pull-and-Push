from tyani_tolkai.bot_sandbox import build_docker_cmd, SANDBOX_IMAGE


def test_build_docker_cmd_has_all_hardening_flags():
    cmd = build_docker_cmd(["python", "bot.py"], bot_dir="/tmp/bot",
                           container_name="tt-test", image=SANDBOX_IMAGE,
                           mem_mb=256, cpus=0.5, pids=32)
    assert cmd[:2] == ["docker", "run"]
    assert "--rm" in cmd and "-i" in cmd
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert "--memory=256m" in cmd
    assert "--cpus=0.5" in cmd
    assert "--pids-limit=32" in cmd
    assert "--pull=never" in cmd
    assert "tt-test" in cmd
    assert "/tmp/bot:/bot:ro" in cmd
    assert "65534:65534" in cmd
    assert cmd[-3:] == [SANDBOX_IMAGE, "python", "bot.py"]
