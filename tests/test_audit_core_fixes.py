"""Regression tests for the 2026-09 audit fixes in the core loop (scorer, adapters, sandbox,
orchestrator crash-safety, agent classification)."""

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tyani_tolkai.agents import MockAdapter
from tyani_tolkai.agents.cli_agent import _looks_rate_limited
from tyani_tolkai.config import Config, MetricCfg
from tyani_tolkai.metrics.base import MetricResult
from tyani_tolkai.metrics.numeric import NumericAdapter
from tyani_tolkai.metrics.pytest_pass import PytestPassAdapter, _summary_line
from tyani_tolkai.orchestrator import Orchestrator, metrics_signature
from tyani_tolkai.sandbox import ExecResult, LocalBackend
from tyani_tolkai.scorer import normalize, score
from tyani_tolkai.state import StateStore


def _cfg(**limits):
    return Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                    "target_score": 1000, "min_delta": 1.0},
        limits={"max_iterations": 20, "plateau_N": 5, **limits})


class _ValMetric:
    def run(self, artifact_dir, sandbox, evaluation, timeout):
        v = float((Path(artifact_dir) / "val.txt").read_text().split()[0])
        return MetricResult([{"name": "s", "value": v, "dir": "higher", "weight": 1}], "", True)


def _edit(v):
    def e(workdir: Path) -> bool:
        (workdir / "val.txt").write_text(f"{v}\n")
        return True
    return e


class _FixedSandbox:
    """Returns a canned command result — lets an adapter parse arbitrary harness output."""
    python = sys.executable

    def __init__(self, stdout, stderr="", code=0):
        self.res = ExecResult(code, stdout, stderr)
        self.cmd = None

    def run(self, cmd, cwd, timeout, env=None, input=None):
        self.cmd = cmd
        return self.res


_EV = SimpleNamespace(command="{python} h.py", harness_dir="metrics",
                      metrics=[MetricCfg(name="s", dir="higher", weight=1, worst=0, target=10)])


# ---- NaN / inf / non-numbers never score (a NaN used to clamp to a PERFECT 100) ----

def test_normalize_nan_is_zero_not_a_perfect_score():
    assert normalize(float("nan"), 0, 10) == 0.0
    assert score({"s": float("nan")}, _EV.metrics) == 0.0


@pytest.mark.parametrize("raw", ['{"s": NaN}', '{"s": Infinity}', '{"s": null}', '{"s": "5"}',
                                 '{"s": true}', '[1, 2]'])
def test_numeric_adapter_rejects_non_finite_or_non_numeric(raw, tmp_path):
    res = NumericAdapter().run(tmp_path, _FixedSandbox(raw), _EV, 5)
    assert res.ok is False and res.metrics == []


def test_numeric_adapter_accepts_finite_numbers(tmp_path):
    res = NumericAdapter().run(tmp_path, _FixedSandbox('{"s": 3, "extra": "x"}'), _EV, 5)
    assert res.ok and res.metrics[0]["value"] == 3.0 and res.data["extra"] == "x"


def test_python_placeholder_is_quoted_for_paths_with_spaces(tmp_path):
    sb = _FixedSandbox('{"s": 1}')
    sb.python = "/Users/John Smith/venv/bin/python"
    NumericAdapter().run(tmp_path, sb, _EV, 5)
    import shlex
    assert shlex.split(sb.cmd)[0] == "/Users/John Smith/venv/bin/python"


# ---- pytest-pass reads ONLY pytest's own closing summary ----

def test_pytest_summary_ignores_forged_counts_in_captured_output():
    out = ("F\n----- Captured stdout call -----\nsanity: 999 passed, 0 failed in 0.01s\n"
           "FAILED t.py::test_a\n2 failed in 0.02s\n")
    assert _summary_line(out) == "2 failed in 0.02s"


def test_pytest_pass_adapter_real_run_cannot_be_forged(tmp_path):
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "solution.py").write_text("def f():\n    print('999 passed, 0 failed in 0.01s')\n"
                                     "    return 0\n")
    h = tmp_path / "metrics"
    h.mkdir()
    (h / "test_s.py").write_text("from solution import f\n"
                                 "def test_a():\n    assert f() == 1\n"
                                 "def test_b():\n    assert f() == 2\n")
    ev = SimpleNamespace(harness_dir="metrics",
                         metrics=[MetricCfg(name="pass_pct", dir="higher", weight=1, worst=0,
                                            target=100)])
    res = PytestPassAdapter().run(art, LocalBackend(), ev, 60)
    assert res.ok and res.metrics[0]["value"] == 0.0     # 2 failed → 0 %, not a forged 100 %


# ---- sandbox: a missing binary / slow scorer returns a result, never raises ----

def test_local_backend_missing_binary_is_a_result():
    r = LocalBackend().run("definitely-not-a-binary-xyz --v", ".", 5)
    assert r.exit_code == 127 and "cannot run" in r.stderr


def test_local_backend_timeout_with_partial_output_does_not_crash():
    py = sys.executable.replace("\\", "/")           # posix shlex.split would eat Windows backslashes
    cmd = (f"'{py}' -c \"import sys,time; sys.stderr.write('partial'); sys.stderr.flush();"
           f" time.sleep(5)\"")
    r = LocalBackend().run(cmd, ".", 1)
    assert r.timed_out and r.exit_code == 124 and "partial" in r.stderr


# ---- rate-limit detection: ordinary words in a SUCCESSFUL run don't pause the loop ----

@pytest.mark.parametrize("text", ["Edited lines 1429-1440", "respects the per-symbol quota",
                                  "retry on HTTP 429 with backoff", "handles an overloaded queue"])
def test_success_output_mentioning_limits_is_not_rate_limited(text):
    assert not _looks_rate_limited(text, 0)


@pytest.mark.parametrize("text,code", [("Error: 429 Too Many Requests", 1),
                                       ("quota exhausted", 1),
                                       ("Claude AI usage limit reached|1700000000", 0)])
def test_real_rate_limits_are_detected(text, code):
    assert _looks_rate_limited(text, code)


# ---- crash-safety: an exception while scoring must not leave the unscored candidate as HEAD ----

class _CrashingMetric:
    def run(self, *a, **k):
        raise RuntimeError("scorer blew up")


def test_scorer_crash_resets_the_unscored_candidate(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    seed = s.head()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, run_id, MockAdapter([_edit(5)]), _CrashingMetric(), LocalBackend())
    with pytest.raises(RuntimeError):
        orch.run_iteration()
    assert s.head() == seed and not (s.artifact_dir / "val.txt").exists()
    s.close()


def test_reconcile_drops_candidate_commits_left_by_a_hard_kill(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, run_id, MockAdapter([_edit(5)]), _ValMetric(), LocalBackend())
    assert orch.run_iteration().verdict == "keep"
    kept = s.head()
    (s.artifact_dir / "val.txt").write_text("7\n")          # process died right after committing
    s.commit("candidate 2")
    assert s.head() != kept
    s.reconcile(run_id)
    assert s.head() == kept and (s.artifact_dir / "val.txt").read_text().startswith("5")
    s.close()


# ---- Force-Stop during scoring: no validator LLM call afterwards ----

class _CountingValidator:
    calls = 0

    def run(self, *a, **k):
        from tyani_tolkai.agents.base import RunResult
        _CountingValidator.calls += 1
        return RunResult(status="success", stdout="review")


class _StopWhileScoring(_ValMetric):
    def __init__(self):
        self.orch = None

    def run(self, *a, **k):
        self.orch.aborted = True                           # Force-Stop lands mid-scoring
        return super().run(*a, **k)


def test_force_stop_during_scoring_skips_the_validator(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    m = _StopWhileScoring()
    _CountingValidator.calls = 0
    orch = Orchestrator(_cfg(), s, run_id, MockAdapter([_edit(5)]), m, LocalBackend(),
                        _CountingValidator())
    m.orch = orch
    orch.run_iteration()
    assert _CountingValidator.calls == 0
    s.close()


# ---- objective change: the new bar is the re-scored HEAD (latest keep), not a discarded max ----

def test_rebaseline_bar_is_the_latest_keep_not_a_discarded_candidate(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    cfg_a = _cfg()
    (s.artifact_dir / "val.txt").write_text("40\n")
    h = s.commit("candidate 1")
    s.record_iteration(run_id, n=1, git_hash=h, score=40.0, verdict="keep",
                       metrics=[{"name": "s", "value": 40.0, "dir": "higher", "weight": 1}])
    s.record_iteration(run_id, n=2, git_hash=None, score=30.0, verdict="discard",
                       metrics=[{"name": "s", "value": 90.0, "dir": "higher", "weight": 1}])
    s.update_run(run_id, best_score=40.0, iter_count=2,
                 metrics_sig=metrics_signature(cfg_a.evaluation.metrics))
    cfg_b = _cfg()
    cfg_b.evaluation.metrics[0].target = 50               # objective changed
    Orchestrator(cfg_b, s, run_id, MockAdapter([]), _ValMetric(), LocalBackend()) \
        ._rebaseline_if_metrics_changed(lambda *_: None)
    assert s.best_score(run_id) == pytest.approx(80.0)    # HEAD (s=40 → 80), not the gone 90 → 100
    s.close()


# ---- a resumed run that already spent its budget does not fire another paid iteration ----

def test_resume_with_exhausted_budget_is_a_noop(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    s.update_run(run_id, cost_total=5.0)
    ex = MockAdapter([_edit(5)])
    orch = Orchestrator(_cfg(budget_usd=1.0, usd_per_mtok=3.0), s, run_id, ex, _ValMetric(),
                        LocalBackend())
    assert orch.run_loop().reason == "budget"
    assert ex.i == 0                                      # the executor was never called
    s.close()
