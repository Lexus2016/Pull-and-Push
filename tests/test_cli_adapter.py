"""P4.6 CLI: `gen-adapter` scaffolds a starter adapter; `check-adapter` is the protocol trust gate
(PASS for a varied protocol-speaking bot, FLAG for a degenerate one, error for a protocol violator)."""

import sys
import pytest
from tyani_tolkai import cli

_MOMENTUM = '''import sys, json
hist = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); t = m.get("type")
    if t == "init": print(json.dumps({"type": "ready"}), flush=True)
    elif t == "bar":
        hist.append(m["c"]); w = 1 if len(hist) >= 2 and hist[-1] > hist[-2] else 0
        print(json.dumps({"type": "order", "n": m["n"], "want": w}), flush=True)
    elif t == "end": break
'''

_FLAT = '''import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); t = m.get("type")
    if t == "init": print(json.dumps({"type": "ready"}), flush=True)
    elif t == "bar": print(json.dumps({"type": "order", "n": m["n"], "want": 0}), flush=True)
    elif t == "end": break
'''

_BROKEN = '''import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); t = m.get("type")
    if t == "init": print(json.dumps({"type": "ready"}), flush=True)
    elif t == "bar": print(json.dumps({"type": "order", "n": m["n"], "want": 5}), flush=True)  # 5 ∉ {-1,0,1}
    elif t == "end": break
'''


def _write_bot(tmp_path, src, name="bot.py"):
    d = tmp_path / "bot"; d.mkdir(exist_ok=True)
    p = d / name; p.write_text(src, encoding="utf-8")
    return d, p


# ---------------- check-adapter ----------------

def test_check_adapter_pass_on_varied_protocol_bot(tmp_path, capsys):
    d, p = _write_bot(tmp_path, _MOMENTUM)
    rc = cli.main(["check-adapter", "--bot-dir", str(d), "--bot-cmd", f"{sys.executable} {p}",
                   "--n", "16", "--per-read-timeout", "30", "--total-timeout", "60"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "adapter check: PASS" in out


def test_check_adapter_flags_degenerate_flat(tmp_path, capsys):
    d, p = _write_bot(tmp_path, _FLAT)
    rc = cli.main(["check-adapter", "--bot-dir", str(d), "--bot-cmd", f"{sys.executable} {p}",
                   "--n", "16", "--per-read-timeout", "30", "--total-timeout", "60"])
    out = capsys.readouterr().out
    assert rc == 3
    assert "FLAG" in out and "degenerate" in out.lower()


def test_check_adapter_protocol_violation_returns_1(tmp_path, capsys):
    d, p = _write_bot(tmp_path, _BROKEN)
    rc = cli.main(["check-adapter", "--bot-dir", str(d), "--bot-cmd", f"{sys.executable} {p}",
                   "--n", "16", "--per-read-timeout", "30", "--total-timeout", "60"])
    assert rc == 1
    assert capsys.readouterr().err.strip() != ""


def test_check_adapter_missing_botdir_returns_2(tmp_path):
    rc = cli.main(["check-adapter", "--bot-dir", str(tmp_path / "nope"),
                   "--bot-cmd", "python x.py"])
    assert rc == 2


# ---------------- gen-adapter ----------------

def test_gen_adapter_writes_stub(tmp_path, capsys):
    d = tmp_path / "bot"; d.mkdir()
    rc = cli.main(["gen-adapter", "--bot-dir", str(d)])
    assert rc == 0
    adapter = d / "adapter.py"
    assert adapter.exists()
    assert "def decide(" in adapter.read_text(encoding="utf-8")


def test_gen_adapter_refuses_overwrite_without_force(tmp_path):
    d = tmp_path / "bot"; d.mkdir()
    assert cli.main(["gen-adapter", "--bot-dir", str(d)]) == 0
    assert cli.main(["gen-adapter", "--bot-dir", str(d)]) == 2          # exists → refuse
    assert cli.main(["gen-adapter", "--bot-dir", str(d), "--force"]) == 0  # --force overwrites


def test_gen_adapter_missing_botdir_returns_2(tmp_path):
    assert cli.main(["gen-adapter", "--bot-dir", str(tmp_path / "nope")]) == 2


# ---------------- gen-adapter → check-adapter (the forcing function) ----------------

def test_scaffolded_stub_flunks_check_until_wired(tmp_path, capsys):
    d = tmp_path / "bot"; d.mkdir()
    assert cli.main(["gen-adapter", "--bot-dir", str(d)]) == 0
    adapter = d / "adapter.py"
    rc = cli.main(["check-adapter", "--bot-dir", str(d), "--bot-cmd", f"{sys.executable} {adapter}",
                   "--n", "16", "--per-read-timeout", "30", "--total-timeout", "60"])
    out = capsys.readouterr().out
    assert rc == 3 and "FLAG" in out and "degenerate" in out.lower()   # unwired stub is caught
