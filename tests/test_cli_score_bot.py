import json
import sys
import pytest
from tyani_tolkai import cli
from tyani_tolkai.bot_runner import BotProtocolError


def _csv(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("time,open,high,low,close,volume\n"
                 "1000,1,2,0.5,1.5,10\n2000,1.5,2.5,1,2,20\n", encoding="utf-8")
    return p


def _botdir(tmp_path):
    d = tmp_path / "bot"; d.mkdir()
    (d / "b.py").write_text("x=1\n", encoding="utf-8")
    return d


def test_score_bot_prints_only_json_on_stdout(tmp_path, monkeypatch, capsys):
    canned = {"return_oos_pct": 12.5, "liquidations": 0, "num_trades": 3}
    seen = {}

    def fake(bot_cmd, bars, *, bot_dir, seed, params, per_read_timeout, total_timeout):
        seen["bot_cmd"] = bot_cmd
        seen["seed"] = seed
        return canned

    monkeypatch.setattr(cli, "_score_bot_sandboxed", fake, raising=False)
    rc = cli.main(["score-bot", "--data", str(_csv(tmp_path)), "--bot-dir", str(_botdir(tmp_path)),
                   "--bot-cmd", "python b.py", "--seed", "proj-x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out) == canned            # stdout is EXACTLY the JSON dict
    assert seen["bot_cmd"] == ["python", "b.py"]   # shlex-split
    assert seen["seed"] == "proj-x"


def test_score_bot_missing_data_returns_2(tmp_path):
    rc = cli.main(["score-bot", "--data", str(tmp_path / "nope.csv"),
                   "--bot-dir", str(_botdir(tmp_path)), "--bot-cmd", "python b.py", "--seed", "s"])
    assert rc == 2


def test_score_bot_missing_botdir_returns_2(tmp_path):
    rc = cli.main(["score-bot", "--data", str(_csv(tmp_path)),
                   "--bot-dir", str(tmp_path / "nobot"), "--bot-cmd", "python b.py", "--seed", "s"])
    assert rc == 2


def test_score_bot_error_path_returns_1_stdout_empty(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise BotProtocolError("evaluation failed")
    monkeypatch.setattr(cli, "_score_bot_sandboxed", boom, raising=False)
    rc = cli.main(["score-bot", "--data", str(_csv(tmp_path)), "--bot-dir", str(_botdir(tmp_path)),
                   "--bot-cmd", "python b.py", "--seed", "s"])
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out.strip() == ""                # nothing on stdout
    assert cap.err.strip() != ""                # diagnostic on stderr


# docker-gated real end-to-end
from tyani_tolkai.bot_sandbox import docker_available
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


@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_score_bot_end_to_end_real_docker(tmp_path, capsys):
    d = tmp_path / "bot"; d.mkdir(); d.chmod(0o755)
    (d / "refbot.py").write_text(_REFBOT, encoding="utf-8"); (d / "refbot.py").chmod(0o644)
    data = tmp_path / "data.csv"
    rows = "\n".join(f"{i*1000},{1+i%3},{2+i%3},{0.5+i%3},{1.0+i%4},{10}" for i in range(12))
    data.write_text("time,open,high,low,close,volume\n" + rows + "\n", encoding="utf-8")
    rc = cli.main(["score-bot", "--data", str(data), "--bot-dir", str(d),
                   "--bot-cmd", "python refbot.py", "--seed", "proj-x",
                   "--per-read-timeout", "30", "--total-timeout", "120"])
    assert rc == 0
    metrics = json.loads(capsys.readouterr().out)
    assert "return_oos_pct" in metrics and "num_trades" in metrics
