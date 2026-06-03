"""Orchestrator — the asymmetric loop (spec §3 steps 1–9).

Mediator pattern: all communication flows through here. Each iteration builds a
brief, runs the Executor in the writeable artifact, runs the Metric Runner in the
sandbox, scores deterministically, and keeps (git commit) or discards (git revert)
by hill-climbing. No-ops are tracked separately from plateau so a lazy agent can't
trigger a false "finished".

Phase 1 is synchronous (mock agent). Phase 2 makes agent runs async (real CLI
subprocesses with live stdout streaming) behind the same method shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from .brief import build_brief
from .config import Config
from .scorer import decide, score
from .state import StateStore

_DIFF_KEEP_CHARS = 2000  # cap stored reverted-candidate diff


@dataclass
class IterationOutcome:
    n: int
    verdict: str          # keep | discard | no_op | fail
    score: float | None


@dataclass
class LoopSummary:
    reason: str           # target | plateau | max_iter
    best_score: float | None
    iterations: int


class Orchestrator:
    def __init__(self, cfg: Config, state: StateStore, run_id: int,
                 executor, metric_adapter, sandbox):
        self.cfg = cfg
        self.state = state
        self.run_id = run_id
        self.executor = executor
        self.metric_adapter = metric_adapter
        self.sandbox = sandbox
        existing = state.last_iterations(run_id, 1)
        self.n = existing[-1].n if existing else 0
        self.plateau_count = 0
        self.no_op_count = 0

    def run_iteration(self, context_text: str = "") -> IterationOutcome:
        self.n += 1
        n = self.n
        brief = build_brief(self.state, self.run_id, self.cfg, context_text=context_text)

        result = self.executor.run(
            brief, self.state.artifact_dir, "writeable",
            self.cfg.agents["executor"].timeout,
        )

        # No-op: nothing meaningful changed → not a plateau, just nudge next time.
        if result.status == "no_op" or not result.changed:
            self.state.revert_uncommitted()
            self.no_op_count += 1
            self.state.record_iteration(
                self.run_id, n=n, git_hash=None, score=self.state.best_score(self.run_id),
                verdict="no_op", metrics=[], agent_exit=result.status,
            )
            self.state.update_run(self.run_id, no_op_count=self.no_op_count, iter_count=n)
            return IterationOutcome(n, "no_op", self.state.best_score(self.run_id))

        candidate_diff = self.state.diff_uncommitted()[:_DIFF_KEEP_CHARS]

        mres = self.metric_adapter.run(
            self.state.artifact_dir, self.sandbox, self.cfg.evaluation,
            self.cfg.limits.step_seconds,
        )
        if not mres.ok:
            self.state.revert_uncommitted()
            self.state.record_iteration(
                self.run_id, n=n, git_hash=None, score=None, verdict="fail",
                metrics=[], change_summary=candidate_diff, agent_exit=result.status,
            )
            self.plateau_count += 1
            self.state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)
            return IterationOutcome(n, "fail", None)

        values = {m["name"]: m["value"] for m in mres.metrics}
        new_score = score(values, self.cfg.evaluation.metrics)
        best = self.state.best_score(self.run_id)
        verdict = decide(new_score, best, self.cfg.evaluation.min_delta)

        if verdict == "keep":
            h = self.state.commit(f"iter {n}: score {new_score:.2f}")
            self.state.record_iteration(
                self.run_id, n=n, git_hash=h, score=new_score, verdict="keep",
                metrics=mres.metrics, agent_exit=result.status,
            )
            self.plateau_count = 0
            self.state.update_run(
                self.run_id, best_score=new_score, plateau_count=0, iter_count=n,
            )
        else:
            # discarded: keep the candidate diff in change_summary so the brief can
            # show the rejected attempt (Phase 1), then revert.
            self.state.record_iteration(
                self.run_id, n=n, git_hash=None, score=new_score, verdict="discard",
                metrics=mres.metrics, change_summary=candidate_diff, agent_exit=result.status,
            )
            self.state.revert_uncommitted()
            self.plateau_count += 1
            self.state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)

        return IterationOutcome(n, verdict, new_score)

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
