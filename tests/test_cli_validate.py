import json
import sys
import pytest
from tyani_tolkai import cli
from tyani_tolkai.bot_runner import BotProtocolError


def _csv(tmp_path, closes=None):
    closes = closes if closes is not None else [1.0 + i * 0.5 for i in range(12)]
    rows = "\n".join(f"{i*1000},{c},{c+0.5},{c-0.5},{c},10" for i, c in enumerate(closes))
    p = tmp_path / "data.csv"
    p.write_text("time,open,high,low,close,volume\n" + rows + "\n", encoding="utf-8")
    return p


def _botdir(tmp_path):
    d = tmp_path / "bot"; d.mkdir()
    (d / "b.py").write_text("x=1\n", encoding="utf-8")
    return d


def _force_sandbox(monkeypatch, fake):
    """Drive the docker-sandbox code path with a canned scorer (no real Docker)."""
    monkeypatch.setattr(cli, "docker_available", lambda: True, raising=True)
    monkeypatch.setattr(cli, "_score_bot_sandboxed", fake, raising=False)


def test_validate_pass_when_bot_beats_controls_and_reacts(tmp_path, monkeypatch, capsys):
    # metrics depend on the last close → reversing the OOS tail changes the score (probe reacts);
    # high positive return beats flat(0)+random; tiny in-sample/OOS gap (no overfit flag).
    def fake(bot_cmd, bars, *, bot_dir, seed, params, per_read_timeout, total_timeout):
        last = bars[-1][3]   # changes when the OOS tail is reversed → probe reacts
        return {"return_oos_pct": 10000.0 + last, "in_sample_return_pct": 10002.0 + last,
                "num_trades": 3, "liquidations": 0, "max_drawdown_pct": 1.0}
    _force_sandbox(monkeypatch, fake)
    rc = cli.main(["validate", "--data", str(_csv(tmp_path)), "--bot-dir", str(_botdir(tmp_path)),
                   "--bot-cmd", "python b.py", "--seed", "proj-x", "--name", "mybot"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("# Evidence Report")
    assert "## Overall: PASS" in out
    assert "Anti-look-ahead probe" in out
    assert "## Provenance" in out


def test_validate_flags_bot_that_loses_to_flat(tmp_path, monkeypatch, capsys):
    def fake(bot_cmd, bars, *, bot_dir, seed, params, per_read_timeout, total_timeout):
        return {"return_oos_pct": -5.0, "in_sample_return_pct": 60.0,
                "num_trades": 1, "liquidations": 0, "max_drawdown_pct": 9.0}
    _force_sandbox(monkeypatch, fake)
    rc = cli.main(["validate", "--data", str(_csv(tmp_path)), "--bot-dir", str(_botdir(tmp_path)),
                   "--bot-cmd", "python b.py", "--seed", "proj-x"])
    out = capsys.readouterr().out
    assert rc == 3                                   # FLAG → non-zero, distinct from error codes
    assert "## Overall: FLAG" in out
    assert "does not beat" in out.lower()


def test_validate_writes_report_to_out(tmp_path, monkeypatch, capsys):
    def fake(bot_cmd, bars, *, bot_dir, seed, params, per_read_timeout, total_timeout):
        last = bars[-1][3]
        return {"return_oos_pct": 9000.0 + last, "in_sample_return_pct": 9001.0 + last,
                "num_trades": 2, "liquidations": 0, "max_drawdown_pct": 2.0}
    _force_sandbox(monkeypatch, fake)
    out_path = tmp_path / "evidence.md"
    rc = cli.main(["validate", "--data", str(_csv(tmp_path)), "--bot-dir", str(_botdir(tmp_path)),
                   "--bot-cmd", "python b.py", "--seed", "s", "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").startswith("# Evidence Report")


def test_validate_missing_data_returns_2(tmp_path):
    rc = cli.main(["validate", "--data", str(tmp_path / "nope.csv"),
                   "--bot-dir", str(_botdir(tmp_path)), "--bot-cmd", "python b.py", "--seed", "s"])
    assert rc == 2


def test_validate_scoring_error_returns_1(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise BotProtocolError("evaluation failed")
    _force_sandbox(monkeypatch, boom)
    rc = cli.main(["validate", "--data", str(_csv(tmp_path)), "--bot-dir", str(_botdir(tmp_path)),
                   "--bot-cmd", "python b.py", "--seed", "s"])
    assert rc == 1
    assert capsys.readouterr().err.strip() != ""


def test_validate_requires_docker_without_trusted(tmp_path, monkeypatch, capsys):
    # an untrusted bot may NOT be scored without the sandbox: no Docker + no --trusted → refuse.
    monkeypatch.setattr(cli, "docker_available", lambda: False, raising=True)
    rc = cli.main(["validate", "--data", str(_csv(tmp_path)), "--bot-dir", str(_botdir(tmp_path)),
                   "--bot-cmd", "python b.py", "--seed", "s"])
    assert rc == 1
    assert "docker" in capsys.readouterr().err.lower()


def test_validate_trusted_subprocess_end_to_end(tmp_path, capsys):
    # REAL pipeline (no mock, no Docker): a trusted reference bot scored via subprocess.
    d = tmp_path / "bot"; d.mkdir()
    refbot = d / "refbot.py"
    refbot.write_text(_REFBOT, encoding="utf-8")
    closes = [1.0 + (i % 5) * 0.4 - (i % 3) * 0.2 for i in range(24)]
    data = _csv(tmp_path, closes)
    rc = cli.main(["validate", "--data", str(data), "--bot-dir", str(d),
                   "--bot-cmd", f"{sys.executable} {refbot}", "--seed", "proj-real",
                   "--trusted", "--name", "refbot",
                   "--per-read-timeout", "30", "--total-timeout", "60"])
    out = capsys.readouterr().out
    assert rc in (0, 3)                                # PASS or FLAG, but it ran end-to-end
    assert out.startswith("# Evidence Report")
    assert "## Overall" in out
    assert "process-separation only" in out           # isolation note reflects --trusted
    assert "Anti-look-ahead probe" in out


# docker-gated real end-to-end (same reference bot as the score-bot e2e)
from tyani_tolkai.bot_sandbox import docker_available as _docker_available
_REFBOT = '''\
import sys, json
hist=[]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    m=json.loads(line); t=m.get("type")
    if t=="init": sys.stdout.write(json.dumps({"type":"ready"})+"\\n"); sys.stdout.flush()
    elif t=="bar":
        hist.append(m["c"]); want=1 if len(hist)>=2 and hist[-1]>hist[-2] else 0
        sys.stdout.write(json.dumps({"type":"order","n":m["n"],"want":want})+"\\n"); sys.stdout.flush()
    elif t=="end": break
'''


@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_validate_end_to_end_real_docker(tmp_path, capsys):
    d = tmp_path / "bot"; d.mkdir(); d.chmod(0o755)
    (d / "refbot.py").write_text(_REFBOT, encoding="utf-8"); (d / "refbot.py").chmod(0o644)
    data = tmp_path / "data.csv"
    rows = "\n".join(f"{i*1000},{1+i%3},{2+i%3},{0.5+i%3},{1.0+i%4},10" for i in range(16))
    data.write_text("time,open,high,low,close,volume\n" + rows + "\n", encoding="utf-8")
    rc = cli.main(["validate", "--data", str(data), "--bot-dir", str(d),
                   "--bot-cmd", "python refbot.py", "--seed", "proj-x",
                   "--per-read-timeout", "30", "--total-timeout", "120"])
    out = capsys.readouterr().out
    assert rc in (0, 3)                               # PASS or FLAG, but it ran
    assert out.startswith("# Evidence Report") and "## Overall" in out
