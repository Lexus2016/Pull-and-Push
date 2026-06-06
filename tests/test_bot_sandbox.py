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


import subprocess as _sp
import sys
import pytest
from tyani_tolkai.bot_sandbox import docker_available, score_bot_sandboxed, SANDBOX_IMAGE

_HAVE_DOCKER = docker_available()
docker_required = pytest.mark.skipif(not _HAVE_DOCKER, reason="docker not available")

_REFBOT = '''\
import sys, json
hist = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    t = m.get("type")
    if t == "init":
        sys.stdout.write(json.dumps({"type": "ready"}) + "\\n"); sys.stdout.flush()
    elif t == "bar":
        hist.append(m["c"])
        want = 1 if len(hist) >= 2 and hist[-1] > hist[-2] else 0
        sys.stdout.write(json.dumps({"type": "order", "n": m["n"], "want": want}) + "\\n"); sys.stdout.flush()
    elif t == "end":
        break
'''


def _bars(closes):
    return [(c, c, c, c, 1.0) for c in closes]


def _write_refbot(tmp_path):
    d = tmp_path / "bot"
    d.mkdir()
    f = d / "refbot.py"
    f.write_text(_REFBOT, encoding="utf-8")
    f.chmod(0o644)
    d.chmod(0o755)
    return d


@docker_required
def test_sandboxed_matches_plain_subprocess(tmp_path):
    from tyani_tolkai.bot_runner import drive_bot
    from tyani_tolkai.bot_engine import simulate
    from tyani_tolkai import bot_protocol as bp
    bot_dir = _write_refbot(tmp_path)
    bars = _bars([1.0, 2.0, 3.0, 2.0, 4.0, 5.0, 1.0, 2.0, 3.0, 4.0])

    m_sandboxed = score_bot_sandboxed(["python", "refbot.py"], bars, bot_dir=str(bot_dir),
                                      seed="proj-x", params={},
                                      per_read_timeout=30.0, total_timeout=120.0)

    oos = bp.seeded_oos_start(len(bars), seed="proj-x")
    orders = drive_bot([sys.executable, str(bot_dir / "refbot.py")], bars, params={},
                       per_read_timeout=30.0, total_timeout=60.0)
    m_plain = simulate(bars, orders, oos_start=oos, params={})

    assert m_sandboxed == m_plain
    assert "return_oos_pct" in m_sandboxed


@docker_required
def test_network_is_actually_denied():
    r = _sp.run(["docker", "run", "--rm", "--network=none", "--pull=never", SANDBOX_IMAGE,
                 "python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 2)"],
                capture_output=True, timeout=60)
    assert r.returncode != 0


@docker_required
def test_fs_is_actually_readonly():
    r = _sp.run(["docker", "run", "--rm", "--read-only", "--pull=never", SANDBOX_IMAGE,
                 "python", "-c", "open('/nope.txt', 'w').write('x')"],
                capture_output=True, timeout=60)
    assert r.returncode != 0


@docker_required
def test_container_removed_after_run(tmp_path):
    bot_dir = _write_refbot(tmp_path)
    bars = _bars([1.0, 2.0, 3.0])
    score_bot_sandboxed(["python", "refbot.py"], bars, bot_dir=str(bot_dir),
                        seed="s", params={}, per_read_timeout=30.0, total_timeout=120.0)
    out = _sp.run(["docker", "ps", "-a", "--filter", "name=tt-bot-", "--format", "{{.Names}}"],
                  capture_output=True, text=True, timeout=30).stdout
    assert "tt-bot-" not in out
