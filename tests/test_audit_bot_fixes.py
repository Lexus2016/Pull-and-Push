"""Regression tests for the 2026-09 audit fixes in onboarding, the backtest engine + template
harness, project import/export and the symmetric arena."""

import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from tyani_tolkai.bot_engine import SCORED_METRICS, simulate
from tyani_tolkai.bot_io import load_bars_csv
from tyani_tolkai.bot_runner import BotProtocolError, drive_bot

HARNESS = (Path(__file__).resolve().parents[1] / "src" / "tyani_tolkai" / "templates"
           / "btcusdt-futures" / "harness" / "run_backtest.py")


# ---- template harness: only -1 / 0 / 1 positions, sane data ----

def _harness_dir(tmp_path, rows):
    h = tmp_path / "metrics"
    h.mkdir()
    shutil.copy2(HARNESS, h / "run_backtest.py")
    lines = ["time,open,high,low,close,volume"] + [",".join(map(str, r)) for r in rows]
    (h / "data.csv").write_text("\n".join(lines) + "\n")
    art = tmp_path / "artifact"
    art.mkdir()
    return h, art


def _bars(n=40):
    return [(1_700_000_000_000 + i * 300_000, 100 + i % 5, 101 + i % 5, 99 + i % 5, 100 + i % 5, 1)
            for i in range(n)]


def _run_harness(h, art, signals_expr):
    (art / "strategy.py").write_text(f"PARAMS = {{}}\ndef signals(bars):\n    return {signals_expr}\n")
    return subprocess.run([sys.executable, str(h / "run_backtest.py")], cwd=art,
                          capture_output=True, text=True)


@pytest.mark.parametrize("expr", ["[5] * len(bars)", "[float('nan')] * len(bars)",
                                  "[0.5] * len(bars)", "['long'] * len(bars)"])
def test_harness_rejects_positions_outside_minus1_0_1(tmp_path, expr):
    h, art = _harness_dir(tmp_path, _bars())
    r = _run_harness(h, art, expr)
    assert r.returncode != 0 and "-1, 0 or 1" in (r.stderr + r.stdout)


def test_harness_accepts_valid_positions(tmp_path):
    h, art = _harness_dir(tmp_path, _bars())
    r = _run_harness(h, art, "[1 if i % 7 < 3 else 0 for i in range(len(bars))]")
    assert r.returncode == 0, r.stderr
    assert set(SCORED_METRICS) - {"max_drawdown_bars"} <= set(json.loads(r.stdout))


def test_harness_rejects_newest_first_data(tmp_path):
    h, art = _harness_dir(tmp_path, list(reversed(_bars())))
    r = _run_harness(h, art, "[0] * len(bars)")
    assert r.returncode != 0 and "strictly increase" in (r.stderr + r.stdout)


# ---- engine: drawdown on marked equity, honest stop fills, wipe-out, flat tail ----

P1 = {"leverage": 1, "risk_frac": 1.0, "stop_pct": 1000}      # no effective stop, no liquidation


def test_open_loss_counts_as_drawdown_even_if_never_closed():
    bars = [(100, 100, 100, 100, 0), (100, 100, 60, 60, 0), (60, 70, 60, 70, 0)]
    r = simulate(bars, [1, 1, 1], oos_start=0, params=P1)
    assert r["max_drawdown_pct"] > 30 and r["full_max_drawdown_pct"] > 30


def test_stop_gapped_through_fills_at_the_open():
    bars = [(100, 100, 100, 100, 0), (80, 80, 79, 80, 0), (80, 80, 80, 80, 0)]
    r = simulate(bars, [1, 0, 0], oos_start=0, params={"leverage": 1, "risk_frac": 1.0, "stop_pct": 2})
    assert r["return_oos_pct"] < -15                       # ~-20 %, not the -2 % of the stop price


def test_wipeout_inside_the_tail_is_a_total_drawdown():
    bars = [(100, 100, 100, 100, 0), (100, 100, 90, 90, 0)] + [(90, 90, 90, 90, 0)] * 3
    r = simulate(bars, [1] + [0] * 4, oos_start=0,
                 params={"leverage": 50, "risk_frac": 1.0, "stop_pct": 50})
    assert r["max_drawdown_pct"] == 100.0 and r["full_max_drawdown_pct"] == 100.0


def test_wipeout_before_the_tail_is_a_total_insample_loss():
    bars = [(100, 100, 100, 100, 0), (100, 100, 90, 90, 0)] + [(90, 90, 90, 90, 0)] * 8
    r = simulate(bars, [1] + [0] * 9, oos_start=6,
                 params={"leverage": 50, "risk_frac": 1.0, "stop_pct": 50})
    assert r["in_sample_return_pct"] == -100.0


def test_flat_no_trade_tail_is_not_underwater():
    bars = [(100, 101, 99, 100, 0)] * 20
    r = simulate(bars, [0] * 20, oos_start=10, params={})
    assert r["max_drawdown_bars"] == 0 and r["max_drawdown_pct"] == 0.0


# ---- bot runner: the bot runs from its folder; an endless line can't exhaust memory ----

def _adapter_dir(tmp_path):
    from tyani_tolkai.adapter_gen import render_adapter_stub
    d = tmp_path / "bot"
    d.mkdir()
    src = render_adapter_stub().replace("    return 0\n\n\ndef run_protocol_io",
                                        "    return 1 if len(history) % 2 else -1\n\n\n"
                                        "def run_protocol_io")
    (d / "adapter.py").write_text(src)
    return d


def test_relative_bot_command_resolves_in_the_bot_folder(tmp_path):
    d = _adapter_dir(tmp_path)
    bars = [(1.0, 1.0, 1.0, 1.0, 0.0)] * 6
    orders = drive_bot([sys.executable, "adapter.py"], bars, params={}, per_read_timeout=10,
                       total_timeout=30, cwd=str(d))
    assert orders == [1, -1, 1, -1, 1, -1]


def test_adapter_stub_runs_without_the_package_installed(tmp_path):
    d = _adapter_dir(tmp_path)
    msgs = [{"type": "init"}, {"type": "bar", "n": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0},
            {"type": "end"}]
    r = subprocess.run([sys.executable, "-S", "-I", "adapter.py"], cwd=d, capture_output=True,
                       text=True, input="".join(json.dumps(m) + "\n" for m in msgs), timeout=30)
    assert r.returncode == 0 and '"type":"order"' in r.stdout


def test_endless_line_hits_the_byte_cap_fast(tmp_path):
    cmd = [sys.executable, "-c",
           "import sys,time; sys.stdout.write('x'*5_000_000); sys.stdout.flush(); time.sleep(20)"]
    t0 = time.monotonic()
    with pytest.raises(BotProtocolError):
        drive_bot(cmd, [(1.0, 1.0, 1.0, 1.0, 0.0)] * 3, params={}, per_read_timeout=15,
                  total_timeout=30, max_bytes=10_000)
    assert time.monotonic() - t0 < 12


def test_check_adapter_needs_at_least_two_bars():
    from tyani_tolkai.validation import check_adapter_orders
    assert check_adapter_orders([], [], 0)["ok"] is False


# ---- onboarding: engine-measurable metrics, robust JSON, quoting, safe copy, secrets ----

def test_proposer_prompt_offers_the_engine_metric_names():
    from tyani_tolkai.profile_schema import BotProfile
    from tyani_tolkai.proposer import build_proposer_prompt
    prof = BotProfile(analyzer_engine="c", bot_name="b", source_root="/x", language="python",
                      framework="custom")
    prompt = build_proposer_prompt(prof, "max return")
    assert all(m in prompt for m in SCORED_METRICS)


def test_broken_outer_json_is_not_replaced_by_a_nested_object():
    from tyani_tolkai.configurator import extract_json
    text = ('{"proposed_metrics": [{"name": "return_oos_pct", "dir": "higher"},],'
            ' "warnings": []}')                             # trailing comma → outer object invalid
    with pytest.raises(ValueError):
        extract_json(text, require=("proposed_metrics",))


def test_onboarding_command_keeps_a_bot_command_with_spaces_as_one_argument():
    from tyani_tolkai.proposal_schema import MetricProposal, ProposedMetric
    from tyani_tolkai.scaffold import build_onboarding_config
    prop = MetricProposal(proposer_engine="c", bot_name="b", goal="g", proposed_metrics=[
        ProposedMetric(name="return_oos_pct", dir="higher", weight=1.0, target=50.0,
                       rationale="r", confidence=0.5)])
    cfg = build_onboarding_config("p", proposal=prop, bot_cmd='python "my bot.py"',
                                  seed_token="my seed")
    argv = shlex.split(cfg["evaluation"]["command"])
    assert argv[argv.index("--bot-cmd") + 1] == 'python "my bot.py"'
    assert argv[argv.index("--seed") + 1] == "my seed"


def test_onboarding_copy_leaves_out_git_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    from tyani_tolkai.proposal_schema import MetricProposal, ProposedMetric
    from tyani_tolkai.scaffold import scaffold_onboarding
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "bot.py").write_text("print(1)\n")
    (bot / ".env").write_text("API_KEY=live\n")
    (bot / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")   # a worktree pointer
    data = tmp_path / "d.csv"
    data.write_text("time,open,high,low,close,volume\n1,1,1,1,1,0\n")
    prop = MetricProposal(proposer_engine="c", bot_name="b", goal="g", proposed_metrics=[
        ProposedMetric(name="return_oos_pct", dir="higher", weight=1.0, target=50.0,
                       rationale="r", confidence=0.5)])
    scaffold_onboarding("ob", bot_dir=bot, data_path=data, proposal=prop, bot_cmd="python bot.py")
    art = tmp_path / "home" / "projects" / "ob" / "artifact"
    assert (art / "bot.py").exists() and not (art / ".env").exists()
    assert (art / ".git").is_dir()                          # a fresh repo of our own


def test_git_init_refuses_a_gitdir_pointer_file(tmp_path):
    from tyani_tolkai.state import StateStore
    s = StateStore(tmp_path / "p")
    s.artifact_dir.mkdir(parents=True)
    (s.artifact_dir / ".git").write_text("gitdir: /elsewhere\n")
    with pytest.raises(RuntimeError):
        s.git_init()
    s.close()


def test_profiler_masks_secret_values_but_keeps_ordinary_fields():
    from tyani_tolkai.profiler import redact_secrets
    out = redact_secrets('{"key": "K-LIVE", "secret": "S-LIVE", "leverage": 5}\nmax_tokens = 100\n'
                         'bot_token=123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PAL')
    assert "K-LIVE" not in out and "S-LIVE" not in out and "AAHdq" not in out
    assert '"leverage": 5' in out and "max_tokens = 100" in out


@pytest.mark.parametrize("rows", [["1,1,1,1,nan,0"], ["1,1,1,1,,0"], ["2,1,1,1,1,0", "1,1,1,1,1,0"]])
def test_bars_csv_rejects_bad_values_and_reversed_time(tmp_path, rows):
    p = tmp_path / "d.csv"
    p.write_text("time,open,high,low,close,volume\n" + "\n".join(rows) + "\n")
    with pytest.raises(ValueError):
        load_bars_csv(p)


# ---- projects: export sees WAL-resident rows; snapshots import as a fresh start ----

def test_export_includes_rows_still_in_the_wal(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    from tyani_tolkai.projects import export_project, project_dir
    from tyani_tolkai.state import StateStore
    s = StateStore(project_dir("w"))
    s.git_init()
    (project_dir("w") / "config.yaml").write_text("project: w\n")
    rid = s.create_run("asymmetric")
    s.record_iteration(rid, n=1, git_hash=None, score=1.0, verdict="discard", metrics=[])
    dest = tmp_path / "w.zip"
    export_project("w", dest)                                # the live connection is still open
    s.close()
    with zipfile.ZipFile(dest) as z:
        z.extract("w/state.db", tmp_path / "x")
    con = sqlite3.connect(str(tmp_path / "x" / "w" / "state.db"))
    assert con.execute("SELECT COUNT(*) FROM iteration").fetchone()[0] == 1
    con.close()


def test_snapshot_export_imports_as_a_runnable_fresh_project(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    from tyani_tolkai.projects import export_project, import_project, project_dir
    from tyani_tolkai.state import StateStore
    s = StateStore(project_dir("src"))
    s.git_init()
    (project_dir("src") / "config.yaml").write_text("project: src\n")
    (s.artifact_dir / "a.py").write_text("x = 1\n")
    h = s.commit("candidate 1")
    rid = s.create_run("asymmetric")
    s.record_iteration(rid, n=1, git_hash=h, score=5.0, verdict="keep", metrics=[])
    s.close()
    export_project("src", tmp_path / "snap.zip", at_hash=h, at_label=1)
    import_project(tmp_path / "snap.zip", "dst")
    art = project_dir("dst") / "artifact"
    assert (art / "a.py").read_text() == "x = 1\n" and (art / ".git").is_dir()


# ---- symmetric: a Stop during A's turn doesn't count the generation as played ----

def test_stop_mid_generation_is_replayed_on_resume(tmp_path):
    from tyani_tolkai.agents.scripted_rival import ScriptedAdversaryRival, ScriptedRecognizerRival
    from tyani_tolkai.arena.cegis import CegisReferee
    from tyani_tolkai.config import Config
    from tyani_tolkai.sandbox import LocalBackend
    from tyani_tolkai.symmetric import SymmetricOrchestrator

    cfg = Config(project="toy", mode="symmetric",
                 agents={"rival_a": {"engine": "mock"}, "rival_b": {"engine": "mock"}},
                 roles={"rival_a": {"goal": "a"}, "rival_b": {"goal": "b"}},
                 evaluation={"adapter": "numeric", "command": "x",
                             "metrics": [{"name": "arena_fitness", "dir": "higher", "target": 1.0,
                                          "worst": 0.0}]},
                 arena={"referee": "cegis-recognizer", "generations": 4,
                        "per_generation_iterations": 2})
    flag = {"stop": False}

    class _A(ScriptedRecognizerRival):
        calls = 0

        def run(self, *a, **k):
            _A.calls += 1
            if _A.calls >= 2:                               # 1st call = bootstrap; then gen 1
                flag["stop"] = True
            return super().run(*a, **k)

    class _B(ScriptedAdversaryRival):
        calls = 0

        def run(self, *a, **k):
            _B.calls += 1
            return super().run(*a, **k)

    o = SymmetricOrchestrator(cfg, tmp_path, CegisReferee(), _A(), _B(), LocalBackend())
    try:
        r = o.run(should_stop=lambda: flag["stop"])
    finally:
        o.close()
    assert r.stop_reason == "stopped"
    m = json.loads((tmp_path / "arena.json").read_text())
    assert m["generation"] == 0                             # gen 1 not counted: B never played it
    assert _B.calls == 1                                    # only the bootstrap seed
