from pathlib import Path

from tyani_tolkai.agents import MockAdapter
from tyani_tolkai.config import Config
from tyani_tolkai.metrics.base import MetricResult
from tyani_tolkai.orchestrator import Orchestrator
from tyani_tolkai.sandbox import LocalBackend
from tyani_tolkai.state import StateStore


class FakeMetric:
    """Reads the first token of artifact/val.txt as the metric 's'."""

    def run(self, artifact_dir, sandbox, evaluation, timeout) -> MetricResult:
        p = Path(artifact_dir) / "val.txt"
        if not p.exists():
            return MetricResult([], "no val.txt", False)
        v = float(p.read_text().split()[0])
        return MetricResult([{"name": "s", "value": v, "dir": "higher", "weight": 1}], "", True)


def _cfg(target=100, plateau_N=3, max_iterations=20, min_delta=1.0):
    return Config(
        project="p",
        agents={"executor": {"engine": "mock"}},
        roles={"executor": {"goal": "raise s"}},
        evaluation={
            "adapter": "numeric",
            "command": "true",   # label only; FakeMetric replaces the real runner
            "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
            "target_score": target,
            "min_delta": min_delta,
        },
        limits={"max_iterations": max_iterations, "plateau_N": plateau_N},
    )


def _edit_val(v, tag=""):
    def e(workdir: Path) -> bool:
        (workdir / "val.txt").write_text(f"{v}\n# {tag}")
        return True
    return e


def _orch(tmp_path, edits, cfg):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    return Orchestrator(cfg, s, run_id, MockAdapter(edits), FakeMetric(), LocalBackend()), s, run_id


class _ScriptedMetric:
    """Returns a pre-scripted score per call (to simulate a flaky scorer for the same artifact)."""

    def __init__(self, scores):
        self.scores = scores
        self.i = 0

    def run(self, artifact_dir, sandbox, evaluation, timeout):
        v = self.scores[min(self.i, len(self.scores) - 1)]
        self.i += 1
        return MetricResult([{"name": "s", "value": v, "dir": "higher", "weight": 1}], "", True)


def test_baseline_revalidation_demotes_a_flaky_best(tmp_path):
    # revalidate_every=1: at iter 2 the best (90) is re-scored, comes back 50 (a flaky/noise win),
    # so it is demoted to 50 — the loop no longer trusts a fluke.
    cfg = Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                    "target_score": 1000, "min_delta": 1.0, "revalidate_every": 1},
        limits={"max_iterations": 20, "plateau_N": 5})
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(cfg, s, run_id, MockAdapter([_edit_val(1)]), _ScriptedMetric([90.0, 50.0]),
                        LocalBackend())
    assert orch.run_iteration().verdict == "keep"
    assert s.best_score(run_id) == 90.0                # kept the (flaky) 90
    orch.run_iteration()                               # iter2: re-validate best -> 50 -> demote; then no_op
    assert s.best_score(run_id) == 50.0                # demoted from the fluke
    log_path = s.project_dir / "agent.log"
    assert log_path.exists() and "re-validation" in log_path.read_text(encoding="utf-8")


def test_budget_cap_stops_the_run(tmp_path):
    # with a price set and a tiny budget, the estimated spend trips the hard cap and ends the run
    cfg = Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                    "target_score": 1000, "min_delta": 1.0},
        limits={"max_iterations": 20, "plateau_N": 10, "budget_usd": 1.0, "usd_per_mtok": 1_000_000.0})
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(cfg, s, run_id, MockAdapter([_edit_val(v) for v in (70, 80, 90, 100, 100, 100)]),
                        FakeMetric(), LocalBackend())
    summary = orch.run_loop()
    assert summary.reason == "budget"                      # stopped by the cap, not target/plateau
    assert s.get_run(run_id)["cost_total"] >= 1.0          # cost was tracked and persisted


def test_cost_tracking_off_by_default(tmp_path):
    # default price 0 -> no cost, no budget enforcement -> behaviour unchanged
    edits = [_edit_val(v) for v in (70, 80, 90, 100)]
    orch, s, run_id = _orch(tmp_path, edits, _cfg())
    orch.run_loop()
    assert orch.cost_total == 0.0


def test_checkpoint_pauses_on_plateau(tmp_path):
    # with checkpoints.on_plateau, hitting the plateau PAUSES for the operator (awaiting_review)
    # and records a checkpoint, instead of silently finishing.
    cfg = Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                    "target_score": 1000, "min_delta": 1.0},
        limits={"max_iterations": 20, "plateau_N": 2},
        checkpoints={"on_plateau": True, "manual": False})
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(cfg, s, run_id, MockAdapter([_edit_val(v) for v in (70, 60, 60, 60)]),
                        FakeMetric(), LocalBackend())
    summary = orch.run_loop()
    assert summary.reason == "checkpoint"
    assert s.get_run(run_id)["status"] == "awaiting_review"
    cp = s.open_checkpoint(run_id)
    assert cp is not None and cp["reason"] == "plateau"


def test_resume_after_checkpoint_gets_a_fresh_budget(tmp_path):
    # the risky path: after a plateau checkpoint, the operator continues (resolve + reset
    # plateau_count). The resumed run must make progress and only re-pause after FRESH attempts,
    # not immediately re-fire at the old plateau count.
    cfg = Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                    "target_score": 1000, "min_delta": 1.0},
        limits={"max_iterations": 50, "plateau_N": 2},
        checkpoints={"on_plateau": True, "manual": False})
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    Orchestrator(cfg, s, run_id, MockAdapter([_edit_val(v) for v in (70, 60, 60)]),
                 FakeMetric(), LocalBackend()).run_loop()
    assert s.get_run(run_id)["status"] == "awaiting_review"
    # operator: Continue (what the endpoint does)
    s.resolve_checkpoint(run_id, "continue")
    s.update_run(run_id, plateau_count=0)
    # resume: improve (80 keep) then plateau again
    summary2 = Orchestrator(cfg, s, run_id, MockAdapter([_edit_val(v) for v in (80, 70, 70)]),
                            FakeMetric(), LocalBackend()).run_loop()
    assert summary2.reason == "checkpoint"        # paused again — but only after fresh attempts
    assert s.best_score(run_id) == 80.0           # progress WAS made on resume (fresh budget worked)


def test_cost_restored_on_resume(tmp_path):
    # the budget cap must survive a restart: cost_total is persisted and a resumed orchestrator
    # picks it up (otherwise the cap resets to 0 every restart).
    cfg = Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
                    "target_score": 1000, "min_delta": 1.0},
        limits={"max_iterations": 1, "plateau_N": 5, "usd_per_mtok": 1_000_000.0})
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    Orchestrator(cfg, s, run_id, MockAdapter([_edit_val(70)]), FakeMetric(), LocalBackend()).run_loop()
    persisted = s.get_run(run_id)["cost_total"]
    assert persisted > 0
    assert Orchestrator(cfg, s, run_id, MockAdapter([]), FakeMetric(), LocalBackend()).cost_total == persisted


def test_improving_run_reaches_target(tmp_path):
    edits = [_edit_val(v) for v in (70, 80, 90, 100)]
    orch, s, run_id = _orch(tmp_path, edits, _cfg())
    summary = orch.run_loop()
    assert summary.reason == "target"
    assert summary.best_score == 100.0
    scores = [r.score for r in s.last_iterations(run_id, 10)]
    assert scores == sorted(scores)            # monotonic non-decreasing
    assert all(r.verdict == "keep" for r in s.last_iterations(run_id, 10))


def test_regression_is_discarded_and_reverted(tmp_path):
    orch, s, run_id = _orch(tmp_path, [_edit_val(70), _edit_val(60)], _cfg())
    o1 = orch.run_iteration()
    assert o1.verdict == "keep"
    o2 = orch.run_iteration()
    assert o2.verdict == "discard"
    assert (s.artifact_dir / "val.txt").read_text().split()[0] == "70"   # reverted
    assert s.best_score(run_id) == 70.0


def test_no_op_does_not_count_as_plateau(tmp_path):
    orch, s, run_id = _orch(tmp_path, [], _cfg())   # no scripted edits → no_op
    o = orch.run_iteration()
    assert o.verdict == "no_op"
    assert orch.no_op_count == 1
    assert orch.plateau_count == 0


class _FakeValidator:
    def __init__(self, text):
        self.text = text
        self.seen = []

    def run(self, prompt, workdir, profile, timeout):
        from tyani_tolkai.agents.base import RunResult
        self.seen.append(prompt)
        assert profile == "read-only"
        return RunResult(status="success", stdout=self.text)


class _RecordingExecutor:
    """Executor that records every brief it is handed, then applies the next scripted edit —
    so a test can assert the reviewer's advice actually reaches the executor's NEXT brief."""

    def __init__(self, edits):
        self.edits = edits
        self.i = 0
        self.briefs = []

    def run(self, brief, workdir, profile, timeout):
        from tyani_tolkai.agents.base import RunResult
        self.briefs.append(brief)
        if self.i >= len(self.edits):
            return RunResult(status="no_op", stdout="", changed=False)
        edit = self.edits[self.i]
        self.i += 1
        changed = bool(edit(Path(workdir)))
        return RunResult(status="success" if changed else "no_op", stdout="ok", changed=changed)


def test_validator_feedback_flows_into_next_brief(tmp_path):
    # The reviewer's recommendation must be ANALYSED BY THE EXECUTOR next iteration — i.e. it must
    # appear in the executor's next brief (the whole point of the loop).
    val = _FakeValidator("reduce leverage")
    ex = _RecordingExecutor([_edit_val(70), _edit_val(80)])
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, run_id, ex, FakeMetric(), LocalBackend(), validator=val)

    o1 = orch.run_iteration()
    assert o1.verdict == "keep"
    assert orch.last_feedback == "reduce leverage"          # captured from the reviewer

    orch.run_iteration()
    assert len(ex.briefs) >= 2
    # iteration 1 had no prior feedback; iteration 2 carries the reviewer's exact recommendation
    assert "reduce leverage" not in ex.briefs[0]
    assert "Validator feedback: reduce leverage" in ex.briefs[1]


class _VerdictValidator:
    """Returns distinct advice for keep vs discard, so a test can tell WHICH review fired."""

    def __init__(self):
        self.seen = []

    def run(self, prompt, workdir, profile, timeout):
        from tyani_tolkai.agents.base import RunResult
        self.seen.append(prompt)
        txt = "DISCARD-RETHINK" if "verdict: DISCARD" in prompt else "KEEP-ADVICE"
        return RunResult(status="success", stdout=txt)


def test_reviewer_is_event_triggered_and_actionable(tmp_path):
    # Token efficiency: the reviewer (2nd expensive LLM call) must NOT run on every iteration —
    # only on a keep, plus ONE rethink per stuck streak, fired EARLY enough that the executor still
    # has iterations left to use it (not wasted on the last step before the loop gives up).
    val = _VerdictValidator()
    ex = _RecordingExecutor([_edit_val(v) for v in (70, 60, 60, 60, 60)])
    cfg = _cfg(plateau_N=4, min_delta=1.0, max_iterations=20)
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(cfg, s, run_id, ex, FakeMetric(), LocalBackend(), validator=val)
    verdicts = [orch.run_iteration().verdict for _ in range(5)]
    assert verdicts == ["keep", "discard", "discard", "discard", "discard"]
    # 2 reviews over 5 iterations: the keep (#1) and exactly ONE discard rethink — not every step
    assert len(val.seen) == 2
    # ACTIONABLE: the discard rethink reached a LATER executor brief (iters remained to use it),
    # and did NOT fire only on the final, wasted step
    assert any("DISCARD-RETHINK" in b for b in ex.briefs[3:])


def test_reviewed_streak_resets_after_a_keep(tmp_path):
    # ONE rethink per discard streak — a keep must RESET the streak so the next stuck run gets its
    # own rethink (else all rethinks after the first streak would be silently disabled).
    val = _FakeValidator("rethink")
    cfg = _cfg(plateau_N=4, min_delta=1.0, max_iterations=50)
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    edits = [_edit_val(v) for v in (70, 60, 60, 80, 70, 70)]   # keep, 2 discards, keep, 2 discards
    orch = Orchestrator(cfg, s, run_id, MockAdapter(edits), FakeMetric(), LocalBackend(), validator=val)
    verdicts = [orch.run_iteration().verdict for _ in range(6)]
    assert verdicts == ["keep", "discard", "discard", "keep", "discard", "discard"]
    # 4 reviews: keep #1, one rethink in streak A (#3), keep #4, one rethink in streak B (#6)
    assert len(val.seen) == 4


def test_iteration_records_change_and_feedback(tmp_path):
    val = _FakeValidator("reduce leverage")
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, run_id, MockAdapter([_edit_val(80)]),
                        FakeMetric(), LocalBackend(), validator=val)
    o = orch.run_iteration()
    assert o.verdict == "keep"
    assert "val.txt" in o.change            # the diff (what changed) is on the outcome
    assert o.feedback == "reduce leverage"  # the validator's why/next
    row = s.last_iterations(run_id, 1)[0]
    assert row.feedback == "reduce leverage"            # persisted for reload
    assert row.change_summary and "val.txt" in row.change_summary


class _CrashThenEdit:
    """Crashes `crashes` times, then succeeds with an edit — to test restart-on-crash."""
    def __init__(self, crashes, edit):
        self.left = crashes
        self.edit = edit
    def run(self, brief, workdir, profile, timeout):
        from tyani_tolkai.agents.base import RunResult
        if self.left > 0:
            self.left -= 1
            return RunResult("crashed", "boom")
        self.edit(Path(workdir))
        return RunResult("success", "ok", changed=True)


class _RateLimited:
    def run(self, brief, workdir, profile, timeout):
        from tyani_tolkai.agents.base import RunResult
        return RunResult("rate_limited", "Error: 429 too many requests / quota")


class _AlwaysCrash:
    def run(self, brief, workdir, profile, timeout):
        from tyani_tolkai.agents.base import RunResult
        return RunResult("crashed", "boom")


def test_executor_restarted_on_crash(tmp_path):
    s = StateStore(tmp_path / "p"); s.git_init(); rid = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, rid, _CrashThenEdit(1, _edit_val(80)),
                        FakeMetric(), LocalBackend())
    o = orch.run_iteration()
    assert o.verdict == "keep"          # the retry recovered and the real change scored


def test_phase_callbacks_fire_in_order(tmp_path):
    s = StateStore(tmp_path / "p"); s.git_init(); rid = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, rid, MockAdapter([_edit_val(80)]), FakeMetric(),
                        LocalBackend(), validator=_FakeValidator("ok"))
    phases = []
    orch.run_iteration(on_phase=lambda p: phases.append(p))
    assert phases == ["executor", "scoring", "validator"]


def test_rate_limit_pauses_run(tmp_path):
    s = StateStore(tmp_path / "p"); s.git_init(); rid = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, rid, _RateLimited(), FakeMetric(), LocalBackend())
    summ = orch.run_loop()
    assert summ.reason == "rate_limited"
    assert s.get_run(rid)["status"] == "paused"


def test_repeated_crash_escalates(tmp_path):
    s = StateStore(tmp_path / "p"); s.git_init(); rid = s.create_run("asymmetric")
    cfg = _cfg(); cfg.limits.agent_retries = 0; cfg.limits.max_agent_failures = 2
    orch = Orchestrator(cfg, s, rid, _AlwaysCrash(), FakeMetric(), LocalBackend())
    summ = orch.run_loop()
    assert summ.reason == "agent_error"
    assert s.get_run(rid)["status"] == "error"


def test_plateau_stops_the_loop(tmp_path):
    # first keeps 70, then meaningful-but-non-improving changes → plateau
    edits = [_edit_val(70, tag=i) for i in range(6)]
    orch, s, run_id = _orch(tmp_path, edits, _cfg(plateau_N=3))
    summary = orch.run_loop()
    assert summary.reason == "plateau"
    assert summary.best_score == 70.0


class _SideEffectMetric(FakeMetric):
    """Like FakeMetric but also writes a junk file, simulating a metric side-effect."""

    def run(self, artifact_dir, sandbox, evaluation, timeout):
        (Path(artifact_dir) / "sideeffect.txt").write_text("junk")
        return super().run(artifact_dir, sandbox, evaluation, timeout)


def test_metric_side_effects_not_committed(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, run_id, MockAdapter([_edit_val(80)]),
                        _SideEffectMetric(), LocalBackend())
    o = orch.run_iteration()
    assert o.verdict == "keep"
    tracked = s._git("ls-files")
    assert "val.txt" in tracked              # the agent's change is committed
    assert "sideeffect.txt" not in tracked   # the metric's side-effect is NOT
    assert not (s.artifact_dir / "sideeffect.txt").exists()   # and cleaned from disk


def test_validator_edits_are_reverted(tmp_path):
    class EditingValidator:
        def run(self, prompt, workdir, profile, timeout):
            from tyani_tolkai.agents.base import RunResult
            (Path(workdir) / "vedit.txt").write_text("validator wrote this")
            return RunResult(status="success", stdout="advice")

    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, run_id, MockAdapter([_edit_val(80)]),
                        FakeMetric(), LocalBackend(), validator=EditingValidator())
    orch.run_iteration()
    assert not (s.artifact_dir / "vedit.txt").exists()   # validator change discarded
    assert "vedit.txt" not in s._git("ls-files")


def test_resume_restores_counters(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    s.update_run(run_id, plateau_count=2, no_op_count=1, iter_count=5)
    orch = Orchestrator(_cfg(), s, run_id, MockAdapter([]), FakeMetric(), LocalBackend())
    assert orch.plateau_count == 2
    assert orch.no_op_count == 1
    assert orch.n == 5


def _cfg_no_worst(target=100):
    """Like _cfg but the metric has NO worst — the system must pin it from baseline."""
    return Config(
        project="p",
        agents={"executor": {"engine": "mock"}},
        roles={"executor": {"goal": "raise s"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "weight": 1, "target": target}],
                    "target_score": target, "min_delta": 1.0},
        limits={"max_iterations": 20, "plateau_N": 3},
    )


def test_baseline_worst_pinned_from_first_measurement(tmp_path):
    cfg = _cfg_no_worst()
    assert cfg.evaluation.metrics[0].worst is None     # user never supplied it
    orch, s, run_id = _orch(tmp_path, [_edit_val(20), _edit_val(60)], cfg)
    o1 = orch.run_iteration()
    assert cfg.evaluation.metrics[0].worst == 20.0     # pinned to the baseline measurement
    assert o1.score == 0.0 and o1.verdict == "keep"    # 0 = where you started
    o2 = orch.run_iteration()
    assert o2.score == 50.0                            # (60-20)/(100-20) → 50
    import json
    assert json.loads(s.get_run(run_id)["baseline_json"]) == {"s": 20.0}   # persisted


def test_missing_metric_fails_iteration_not_run(tmp_path):
    # config wants 's' AND 'extra', but FakeMetric only reports 's' → fail THIS iteration,
    # don't crash the whole run with a KeyError from the scorer.
    cfg = Config(
        project="p", agents={"executor": {"engine": "mock"}}, roles={"executor": {"goal": "g"}},
        evaluation={"adapter": "numeric", "command": "true",
                    "metrics": [{"name": "s", "dir": "higher", "target": 100},
                                {"name": "extra", "dir": "higher", "target": 100}],
                    "target_score": 100},
        limits={"max_iterations": 5, "plateau_N": 3},
    )
    orch, s, run_id = _orch(tmp_path, [_edit_val(70)], cfg)
    o = orch.run_iteration()
    assert o.verdict == "fail" and o.score is None
    assert "extra" in s.last_iterations(run_id, 1)[0].feedback   # names the missing metric
    assert orch.plateau_count == 1                               # counted as a non-improving step


def test_force_stop_records_a_stopped_iteration(tmp_path):
    # Force-Stop must leave a visible row in the iteration list, not an empty list.
    s = StateStore(tmp_path / "p"); s.git_init(); rid = s.create_run("asymmetric")
    orch = Orchestrator(_cfg(), s, rid, MockAdapter([_edit_val(70)]), FakeMetric(), LocalBackend())
    orch.force_kill()                         # abort lands before/while the agent runs
    o = orch.run_iteration()
    assert o.verdict == "stopped"
    rows = s.last_iterations(rid, 1)
    assert rows and rows[0].verdict == "stopped" and rows[0].feedback   # recorded with a reason


def test_baseline_worst_restored_on_resume(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    import json
    s.update_run(run_id, baseline_json=json.dumps({"s": 10.0}))
    cfg = _cfg_no_worst()
    orch = Orchestrator(cfg, s, run_id, MockAdapter([]), FakeMetric(), LocalBackend())
    assert cfg.evaluation.metrics[0].worst == 10.0     # restored from the persisted baseline
