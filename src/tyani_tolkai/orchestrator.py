"""Orchestrator — the asymmetric loop (spec §3 steps 1–9).

Mediator pattern: all communication flows through here. Each iteration builds a
brief, runs the Executor in the writeable artifact, runs the Metric Runner in the
sandbox (median over `runs` for noise), scores deterministically, keeps (git commit)
or discards (git revert) by hill-climbing, then asks the optional Validator for
feedback to steer the next iteration. Whether the agent changed anything is decided
via git, not by trusting the agent. No-ops are tracked separately from plateau.

Phase 2 is synchronous; Phase 3 adds async streaming behind the same shape.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .brief import build_brief, build_validator_prompt
from .config import Config
from .scorer import decide, score
from .state import StateStore

_DIFF_KEEP_CHARS = 2000


@dataclass
class IterationOutcome:
    n: int
    verdict: str          # keep | discard | no_op | fail
    score: float | None
    feedback: str = ""


@dataclass
class LoopSummary:
    reason: str           # target | plateau | max_iter
    best_score: float | None
    iterations: int


class Orchestrator:
    def __init__(self, cfg: Config, state: StateStore, run_id: int,
                 executor, metric_adapter, sandbox, validator=None):
        self.cfg = cfg
        self.state = state
        self.run_id = run_id
        self.executor = executor
        self.metric_adapter = metric_adapter
        self.sandbox = sandbox
        self.validator = validator
        existing = state.last_iterations(run_id, 1)
        self.n = existing[-1].n if existing else 0
        self.plateau_count = 0
        self.no_op_count = 0
        self.last_feedback = ""

    # ---- metric runner with noise median ----

    def _run_metrics(self):
        from .metrics.base import MetricResult
        runs = max(1, self.cfg.evaluation.runs)
        results = [
            self.metric_adapter.run(
                self.state.artifact_dir, self.sandbox, self.cfg.evaluation,
                self.cfg.limits.step_seconds,
            )
            for _ in range(runs)
        ]
        if any(not r.ok for r in results):
            bad = next(r for r in results if not r.ok)
            return MetricResult(metrics=[], logs=bad.logs, ok=False)
        if runs == 1:
            return results[0]
        # median per metric name
        by_name: dict[str, list[float]] = {}
        meta: dict[str, dict] = {}
        for r in results:
            for m in r.metrics:
                by_name.setdefault(m["name"], []).append(m["value"])
                meta[m["name"]] = m
        merged = [
            {**meta[name], "value": statistics.median(vals)}
            for name, vals in by_name.items()
        ]
        return MetricResult(metrics=merged, logs="median over %d runs" % runs, ok=True)

    # ---- one iteration ----

    def run_iteration(self, context_text: str = "") -> IterationOutcome:
        self.n += 1
        n = self.n
        cfg, state = self.cfg, self.state
        brief = build_brief(state, self.run_id, cfg, context_text=context_text,
                            validator_feedback=self.last_feedback)

        result = self.executor.run(
            brief, state.artifact_dir, "writeable", cfg.agents["executor"].timeout,
        )

        # agent error → revert, record fail, do not blame the design (no plateau++)
        if result.status in ("crashed", "timeout"):
            state.revert_uncommitted()
            state.record_iteration(self.run_id, n=n, git_hash=None,
                                   score=state.best_score(self.run_id), verdict="fail",
                                   metrics=[], agent_exit=result.status)
            state.update_run(self.run_id, iter_count=n)
            return IterationOutcome(n, "fail", state.best_score(self.run_id))

        # did the agent actually change anything? (git decides, not the agent)
        if not state.has_changes():
            self.no_op_count += 1
            state.record_iteration(self.run_id, n=n, git_hash=None,
                                   score=state.best_score(self.run_id), verdict="no_op",
                                   metrics=[], agent_exit=result.status)
            state.update_run(self.run_id, no_op_count=self.no_op_count, iter_count=n)
            return IterationOutcome(n, "no_op", state.best_score(self.run_id))

        candidate_diff = state.diff_uncommitted()[:_DIFF_KEEP_CHARS]
        mres = self._run_metrics()

        if not mres.ok:
            state.revert_uncommitted()
            state.record_iteration(self.run_id, n=n, git_hash=None, score=None,
                                   verdict="fail", metrics=[], change_summary=candidate_diff,
                                   agent_exit=result.status)
            self.plateau_count += 1
            state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)
            return IterationOutcome(n, "fail", None)

        values = {m["name"]: m["value"] for m in mres.metrics}
        new_score = score(values, cfg.evaluation.metrics)
        best = state.best_score(self.run_id)
        verdict = decide(new_score, best, cfg.evaluation.min_delta)

        if verdict == "keep":
            h = state.commit(f"iter {n}: score {new_score:.2f}")
            state.record_iteration(self.run_id, n=n, git_hash=h, score=new_score,
                                   verdict="keep", metrics=mres.metrics, agent_exit=result.status)
            self.plateau_count = 0
            state.update_run(self.run_id, best_score=new_score, plateau_count=0, iter_count=n)
        else:
            state.record_iteration(self.run_id, n=n, git_hash=None, score=new_score,
                                   verdict="discard", metrics=mres.metrics,
                                   change_summary=candidate_diff, agent_exit=result.status)
            state.revert_uncommitted()
            self.plateau_count += 1
            state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)

        # Validator feedback for the NEXT iteration (read-only; never scores)
        if self.validator is not None:
            vprompt = build_validator_prompt(cfg, candidate_diff, values, new_score, verdict,
                                             self.last_feedback)
            vtimeout = cfg.agents.get("validator").timeout if "validator" in cfg.agents else 300
            vres = self.validator.run(vprompt, state.artifact_dir, "read-only", vtimeout)
            self.last_feedback = (vres.stdout or "").strip()[:1000]

        return IterationOutcome(n, verdict, new_score, self.last_feedback)

    # ---- loop ----

    def run_loop(self, on_iteration=None) -> LoopSummary:
        cfg = self.cfg
        while True:
            outcome = self.run_iteration()
            if on_iteration:
                on_iteration(outcome)
            best = self.state.best_score(self.run_id)
            if best is not None and best >= cfg.evaluation.target_score:
                self.state.set_status(self.run_id, "finished")
                return LoopSummary("target", best, self.n)
            if self.plateau_count >= cfg.limits.plateau_N:
                self.state.set_status(self.run_id, "finished")
                return LoopSummary("plateau", best, self.n)
            if self.n >= cfg.limits.max_iterations:
                self.state.set_status(self.run_id, "finished")
                return LoopSummary("max_iter", best, self.n)
