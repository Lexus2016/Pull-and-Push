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
from dataclasses import dataclass, field

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
    feedback: str = ""    # validator's "why / what next"
    change: str = ""      # the diff of what the executor changed
    metrics: list = field(default_factory=list)  # raw objective metrics this iteration


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
        run = state.get_run(run_id)
        existing = state.last_iterations(run_id, 1)
        self.n = existing[-1].n if existing else (run["iter_count"] if run else 0)
        # restore counters on resume so stop conditions survive a restart
        self.plateau_count = run["plateau_count"] if run else 0
        self.no_op_count = run["no_op_count"] if run else 0
        self.last_feedback = ""
        self.halt = None              # None | "rate_limited" | "agent_error"
        self.consecutive_fail = 0     # consecutive hard agent failures (for escalation)

    def _run_executor(self, brief: str):
        """Run the executor, restarting it on a crash/timeout up to agent_retries.
        Does NOT retry rate-limits (pointless and abusive)."""
        cfg = self.cfg
        result = None
        for _ in range(max(0, cfg.limits.agent_retries) + 1):
            self.state.revert_uncommitted()          # clean slate before each attempt
            result = self.executor.run(brief, self.state.artifact_dir, "writeable",
                                       cfg.agents["executor"].timeout)
            if result.status in ("success", "rate_limited"):
                return result
            # crashed / timeout → restart the process (next loop iteration)
        return result

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

    def _consult_validator(self, values: dict, new_score, verdict: str, candidate_diff: str) -> None:
        """Run the read-only Validator for feedback; guarantee read-only by reverting
        any edits it makes (not every CLI honors a read-only flag)."""
        if self.validator is None:
            return
        cfg, state = self.cfg, self.state
        vprompt = build_validator_prompt(cfg, candidate_diff, values, new_score, verdict,
                                         self.last_feedback)
        vtimeout = cfg.agents["validator"].timeout if "validator" in cfg.agents else 300
        vres = self.validator.run(vprompt, state.artifact_dir, "read-only", vtimeout)
        if state.has_changes():               # enforce read-only regardless of engine
            state.revert_uncommitted()
        self.last_feedback = (vres.stdout or "").strip()[:1000]

    def run_iteration(self, context_text: str = "", on_phase=None) -> IterationOutcome:
        self.n += 1
        n = self.n
        cfg, state = self.cfg, self.state
        ph = on_phase or (lambda *_: None)
        brief = build_brief(state, self.run_id, cfg, context_text=context_text,
                            validator_feedback=self.last_feedback)

        ph("executor")                       # Executor is editing the artifact
        result = self._run_executor(brief)   # runs with restart-on-crash/timeout

        # provider rate limit / quota → pause the whole run and inform (no churn)
        if result.status == "rate_limited":
            state.revert_uncommitted()
            self.halt = "rate_limited"
            msg = "RATE LIMIT / quota from the agent — paused:\n" + (result.stdout or "")[:600]
            state.record_iteration(self.run_id, n=n, git_hash=None, score=state.best_score(self.run_id),
                                   verdict="fail", metrics=[], feedback=msg, agent_exit="rate_limited")
            state.update_run(self.run_id, iter_count=n)
            return IterationOutcome(n, "fail", state.best_score(self.run_id), msg)

        # hard failure that survived the retries → record + escalate if it keeps happening
        if result.status in ("crashed", "timeout"):
            state.revert_uncommitted()
            self.consecutive_fail += 1
            msg = (f"AGENT {result.status.upper()} (after {cfg.limits.agent_retries} retries):\n"
                   + (result.stdout or "")[:600])
            state.record_iteration(self.run_id, n=n, git_hash=None, score=state.best_score(self.run_id),
                                   verdict="fail", metrics=[], feedback=msg, agent_exit=result.status)
            state.update_run(self.run_id, iter_count=n)
            if self.consecutive_fail >= cfg.limits.max_agent_failures:
                self.halt = "agent_error"     # too many in a row → stop & inform
            return IterationOutcome(n, "fail", state.best_score(self.run_id), msg)

        self.consecutive_fail = 0   # the agent ran fine this iteration

        # did the agent actually change anything? (git decides, not the agent)
        if not state.has_changes():
            self.no_op_count += 1
            state.record_iteration(self.run_id, n=n, git_hash=None,
                                   score=state.best_score(self.run_id), verdict="no_op",
                                   metrics=[], agent_exit=result.status)
            state.update_run(self.run_id, no_op_count=self.no_op_count, iter_count=n)
            return IterationOutcome(n, "no_op", state.best_score(self.run_id))

        # Commit the candidate NOW so a kept commit contains ONLY the agent's changes.
        # Metric side-effects produced afterwards stay uncommitted and are cleaned,
        # never polluting the artifact's history.
        parent = state.head()
        cand_hash = state.commit(f"candidate {n}")
        candidate_diff = state.diff(parent, cand_hash)[:_DIFF_KEEP_CHARS]

        ph("scoring")                        # Metric Runner scores the candidate
        mres = self._run_metrics()
        state.revert_uncommitted()            # drop metric side-effects (tree → candidate)

        if not mres.ok:
            self._consult_validator({}, None, "fail", candidate_diff)  # advise before reset/record
            fb = self.last_feedback
            state.reset_hard(parent)          # discard the candidate commit
            state.record_iteration(self.run_id, n=n, git_hash=None, score=None, verdict="fail",
                                   metrics=[], change_summary=candidate_diff, feedback=fb,
                                   agent_exit=result.status)
            self.plateau_count += 1
            state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)
            return IterationOutcome(n, "fail", None, fb, candidate_diff)

        values = {m["name"]: m["value"] for m in mres.metrics}
        new_score = score(values, cfg.evaluation.metrics)
        best = state.best_score(self.run_id)
        verdict = decide(new_score, best, cfg.evaluation.min_delta)

        # Validator advises first (read-only) so its "why / what next" persists with the row.
        if self.validator is not None:
            ph("validator")                  # Validator analyzes & advises
        self._consult_validator(values, new_score, verdict, candidate_diff)
        fb = self.last_feedback
        if verdict == "keep":
            state.record_iteration(self.run_id, n=n, git_hash=cand_hash, score=new_score,
                                   verdict="keep", metrics=mres.metrics, change_summary=candidate_diff,
                                   feedback=fb, agent_exit=result.status)
            self.plateau_count = 0
            state.update_run(self.run_id, best_score=new_score, plateau_count=0, iter_count=n)
        else:
            state.reset_hard(parent)          # discard the candidate commit
            state.record_iteration(self.run_id, n=n, git_hash=None, score=new_score,
                                   verdict="discard", metrics=mres.metrics,
                                   change_summary=candidate_diff, feedback=fb, agent_exit=result.status)
            self.plateau_count += 1
            state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)
        return IterationOutcome(n, verdict, new_score, fb, candidate_diff, mres.metrics)

    # ---- loop ----

    def run_loop(self, on_iteration=None, should_stop=None, on_phase=None) -> LoopSummary:
        cfg = self.cfg
        while True:
            if should_stop and should_stop():
                best = self.state.best_score(self.run_id)
                self.state.set_status(self.run_id, "stopped")
                return LoopSummary("stopped", best, self.n)
            outcome = self.run_iteration(on_phase=on_phase)
            if on_iteration:
                on_iteration(outcome)
            if self.halt:                      # agent rate-limited or repeatedly failing
                best = self.state.best_score(self.run_id)
                self.state.set_status(self.run_id, "paused" if self.halt == "rate_limited" else "error")
                return LoopSummary(self.halt, best, self.n)
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
