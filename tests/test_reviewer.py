"""The Validator-as-Reviewer behaviour (the score is deterministic, so the agent's job is
judgement, not scoring): correct per-role system prompt, a prompt that demands a whole-system
critique, and an artifact snapshot that never leaks the hidden scoring harness."""

import pytest

from tyani_tolkai.agents.cli_agent import build_cli_prefix
from tyani_tolkai.brief import build_validator_prompt
from tyani_tolkai.config import Config
from tyani_tolkai.orchestrator import Orchestrator
from tyani_tolkai.state import StateStore


# ---------------- per-role system prompt (executor vs reviewer) ----------------

def test_claude_executor_vs_reviewer_get_different_focus():
    ex = " ".join(build_cli_prefix("claude", None, "writeable"))
    rv = " ".join(build_cli_prefix("claude", None, "read-only"))
    # executor is told to write and stay silent; reviewer is told NOT to write and to comment
    assert "code executor" in ex and "REVIEWER" not in ex
    assert "REVIEWER" in rv and "code executor" not in rv
    # the reviewer must be told not to assign the number (the harness scores) and not to peek
    assert "do NOT" in rv and "hidden on purpose" in rv


# ---------------- the reviewer prompt asks for a whole-system critique ----------------

def _cfg():
    return Config(
        project="p", mode="asymmetric",
        agents={"executor": {"engine": "mock"}, "validator": {"engine": "mock"}},
        roles={"executor": {"goal": "g"}, "validator": {"goal": "review it"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "r", "dir": "higher", "weight": 1, "target": 100}]},
    )


def test_validator_prompt_demands_whole_system_review():
    p = build_validator_prompt(_cfg(), "diff --git a/s.py b/s.py\n+x=1", {"r": 42.0}, 55.1, "keep",
                               artifact_text="--- s.py ---\nPARAMS = {'risk': 0.9}\n",
                               report_stats={"win_rate_pct": 40.0, "profit_factor": 4.9, "num_trades": 7})
    # never a scorer
    assert "do NOT assign or guess a number" in p
    # sees the whole system, not just the diff
    assert "FULL CURRENT SYSTEM" in p and "PARAMS = {'risk': 0.9}" in p
    # gets report-only diagnostics (win rate, PF, trades) to reason about, not just the scored metrics
    assert "Report stats" in p and "win_rate_pct=40.0" in p and "num_trades=7" in p
    # asked to flag fundamental design flaws
    assert "SYSTEM AS A WHOLE" in p
    assert "look-ahead" in p
    # three labelled parts
    for part in ("ASSESSMENT", "WHY", "IDEAS"):
        assert part in p


def test_validator_prompt_rejected_changes_direction():
    p = build_validator_prompt(_cfg(), "diff", {"r": 1.0}, 10.0, "discard",
                               prev_feedback="raise leverage", artifact_text="--- s.py ---\nx=1\n")
    assert "REJECTED" in p
    assert "raise leverage" in p and "change direction" in p


# ---------------- artifact snapshot must NOT leak the hidden harness ----------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    return tmp_path


def _orch_with_harness_inside_artifact(tmp_path):
    """Build a project in the OLD layout (harness committed INSIDE the artifact) — the hard case
    for the snapshot: tracked_files() includes the scorer, which must still be filtered out."""
    s = StateStore(tmp_path / "proj")
    s.artifact_dir.mkdir(parents=True, exist_ok=True)
    (s.artifact_dir / "strategy.py").write_text("PARAMS = {'fast': 10}\n# the real system\n")
    (s.artifact_dir / "metrics").mkdir()
    (s.artifact_dir / "metrics" / "run_backtest.py").write_text(
        "SECRET_SCORING_FORMULA = 'reward = total_return - 3*drawdown'\n")
    (s.artifact_dir / "metrics" / "data.csv").write_text("time,close\n1,2\n")
    s.git_init()                                   # commits all of the above
    cfg = _cfg()                                   # harness_dir is None → defaults to "metrics"
    run_id = s.create_run("asymmetric")
    return Orchestrator(cfg, s, run_id, None, None, None, validator=None), s


def test_artifact_snapshot_includes_system_excludes_harness(home, tmp_path):
    orch, s = _orch_with_harness_inside_artifact(tmp_path)
    try:
        # sanity: the harness really is a tracked file in this layout
        assert "metrics/run_backtest.py" in s.tracked_files()
        snap = orch._artifact_snapshot()
        # the system under review is shown
        assert "strategy.py" in snap and "PARAMS = {'fast': 10}" in snap
        # the scorer is NOT — neither its file header nor its formula leaks to the reviewer
        assert "run_backtest.py ---" not in snap
        assert "SECRET_SCORING_FORMULA" not in snap
        assert "data.csv ---" not in snap
    finally:
        s.close()


def test_format_harness_stats_reads_data_not_logs(home, tmp_path):
    """The per-iteration 📊 line comes from the adapter's parsed `data`, so stderr noise in the
    logs can't break it; scored metrics and null fields are excluded."""
    from tyani_tolkai.metrics.base import MetricResult
    s = StateStore(tmp_path / "proj")
    s.git_init()
    rid = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, rid, None, None, None)   # _cfg scores metric "r"
    try:
        mres = MetricResult(metrics=[{"name": "r", "value": 80.0}], logs="junk\nDeprecationWarning",
                            ok=True, data={"r": 80.0, "win_rate_pct": 57.1, "num_trades": 7,
                                           "profit_factor": None})
        line = orch._format_harness_stats(mres)
        assert line.startswith("📊")
        assert "win_rate_pct=57.1" in line and "num_trades=7" in line
        assert "r=" not in line              # scored metric excluded
        assert "profit_factor" not in line   # None excluded
        # no data → no line
        assert orch._format_harness_stats(MetricResult(ok=True)) == ""
    finally:
        s.close()
