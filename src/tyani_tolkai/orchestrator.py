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

import json
import statistics
from dataclasses import dataclass, field

from .brief import build_brief, build_validator_prompt
from .config import Config
from .scorer import decide, resolve_worst, score
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
        self.aborted = False          # set by force_kill() — stop NOW, kill the live agent
        # Baseline zero-points: metric.worst the user never has to invent. Pinned to the
        # first measured value, persisted so a resumed run keeps the same 0–100 scale.
        raw = run["baseline_json"] if (run and "baseline_json" in run.keys()) else None
        self._baseline: dict = json.loads(raw) if raw else {}
        for m in cfg.evaluation.metrics:
            if m.worst is None and m.name in self._baseline:
                m.worst = self._baseline[m.name]

    def force_kill(self) -> None:
        """Force-Stop: abort the loop and SIGKILL the live agent immediately (any thread)."""
        self.aborted = True
        for a in (self.executor, self.validator):
            if a is not None and hasattr(a, "kill"):
                try:
                    a.kill()
                except Exception:
                    pass

    def _resolve_baseline(self, values: dict) -> None:
        """Pin each unset metric.worst to its first measured value: the natural
        zero-point, so a normal seed reads ~0 (where you started) and target=100.
        Persisted for resume. Note: if the seed ALREADY meets/beats the target, we
        give the scale a hair of range and the seed reads ~100 — i.e. 'already done',
        which is the honest reading, not a forced 0."""
        changed = False
        for m in self.cfg.evaluation.metrics:
            if m.worst is not None or m.name not in values:
                continue
            m.worst = resolve_worst(m.dir, m.target, float(values[m.name]))
            self._baseline[m.name] = m.worst
            changed = True
        if changed:
            self.state.update_run(self.run_id, baseline_json=json.dumps(self._baseline))

    def _run_executor(self, brief: str):
        """Run the executor, restarting it on a crash/timeout up to agent_retries.
        Does NOT retry rate-limits (pointless and abusive)."""
        cfg = self.cfg
        result = None
        for _ in range(max(0, cfg.limits.agent_retries) + 1):
            if self.aborted:                         # Force-Stop: don't (re)spawn the agent
                return result
            self.state.revert_uncommitted()          # clean slate before each attempt
            result = self.executor.run(brief, self.state.artifact_dir, "writeable",
                                       cfg.agents["executor"].timeout)
            if result.status in ("success", "rate_limited") or self.aborted:
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

    def _format_harness_stats(self, mres) -> str:
        """One-line summary of the harness's report-only fields (anything it prints beyond the
        scored metrics — e.g. trade count, win rate, profit factor, tested period). Folded into
        the iteration feedback so the operator reads it per iteration without opening raw output."""
        try:
            raw = json.loads((mres.logs or "").strip())
        except (ValueError, TypeError):
            return ""
        if not isinstance(raw, dict):
            return ""
        scored = {m.name for m in self.cfg.evaluation.metrics}
        bits = [f"{k}={v}" for k, v in raw.items()
                if k not in scored and v is not None and isinstance(v, (int, float, str))]
        return ("📊 " + " · ".join(bits)) if bits else ""

    def _drain_operator_context(self, role: str = "executor") -> str:
        """Read and CLEAR the queued live operator instructions. The web 'Live agent command'
        appends them to <project>/context/<role>.md; nothing consumed that file before, so every
        steer was silently dropped (the brief always showed '(none)'). Draining here injects the
        text into the next brief exactly once, then empties the file."""
        path = self.state.project_dir / "context" / f"{role}.md"
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if text:
            try:
                path.write_text("", encoding="utf-8")     # consume once
            except OSError:
                pass
        return text

    def _log_iteration_header(self, n: int) -> None:
        """Append a per-iteration banner to <project>/agent.log. The agent runner appends
        (never truncates), so the log accumulates one clearly-delimited section per iteration
        — the operator can read the full history instead of just the latest, overwritten run."""
        try:
            sep = "─" * 60
            with (self.state.project_dir / "agent.log").open("a", encoding="utf-8") as f:
                f.write(f"\n{sep}\n▼ ITERATION {n} · executor\n{sep}\n")
        except OSError:
            pass

    def run_iteration(self, context_text: str = "", on_phase=None) -> IterationOutcome:
        self.n += 1
        n = self.n
        cfg, state = self.cfg, self.state
        ph = on_phase or (lambda *_: None)
        brief = build_brief(state, self.run_id, cfg, context_text=context_text,
                            validator_feedback=self.last_feedback)

        ph("executor")                       # Executor is editing the artifact
        self._log_iteration_header(n)        # accumulate agent.log across iterations
        result = self._run_executor(brief)   # runs with restart-on-crash/timeout

        # Force-Stop landed while the agent was running → drop any partial edit and bail NOW,
        # but LEAVE A RECORD so the iteration list shows what happened (not an empty list).
        if self.aborted:
            state.revert_uncommitted()
            state.record_iteration(self.run_id, n=n, git_hash=None, score=state.best_score(self.run_id),
                                   verdict="stopped", metrics=[],
                                   feedback="Force-stopped by the user while the agent was running.",
                                   agent_exit="killed")
            state.update_run(self.run_id, iter_count=n)
            return IterationOutcome(n, "stopped", state.best_score(self.run_id),
                                    "Force-stopped by the user.")

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
            # persist the ACTUAL evaluation error (stdout/stderr of the metric command), not
            # just the validator's guess — otherwise a broken/missing harness is invisible.
            err = (mres.logs or "").strip()
            fb = ("evaluation error:\n" + err[:1500]) if err else ""
            if self.last_feedback:
                fb = (fb + "\n\nvalidator: " + self.last_feedback).strip()
            state.reset_hard(parent)          # discard the candidate commit
            state.record_iteration(self.run_id, n=n, git_hash=None, score=None, verdict="fail",
                                   metrics=[], change_summary=candidate_diff, feedback=fb,
                                   agent_exit=result.status)
            self.plateau_count += 1
            state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)
            return IterationOutcome(n, "fail", None, fb, candidate_diff)

        values = {m["name"]: m["value"] for m in mres.metrics}
        # the command ran but didn't report every configured metric → fail THIS iteration
        # (revert + count toward plateau) instead of crashing the whole run on a KeyError.
        missing = [m.name for m in cfg.evaluation.metrics if m.name not in values]
        if missing:
            self._consult_validator(values, None, "fail", candidate_diff)
            fb = self.last_feedback
            state.reset_hard(parent)
            state.record_iteration(self.run_id, n=n, git_hash=None, score=None, verdict="fail",
                                   metrics=mres.metrics, change_summary=candidate_diff,
                                   feedback=("metrics not reported: " + ", ".join(missing)
                                             + (f"\n{fb}" if fb else "")),
                                   agent_exit=result.status)
            self.plateau_count += 1
            state.update_run(self.run_id, plateau_count=self.plateau_count, iter_count=n)
            return IterationOutcome(n, "fail", None, fb, candidate_diff)

        self._resolve_baseline(values)        # pin metric zero-points on first measurement
        new_score = score(values, cfg.evaluation.metrics)
        best = state.best_score(self.run_id)
        verdict = decide(new_score, best, cfg.evaluation.min_delta)

        # Validator advises first (read-only) so its "why / what next" persists with the row.
        if self.validator is not None:
            ph("validator")                  # Validator analyzes & advises
        self._consult_validator(values, new_score, verdict, candidate_diff)
        fb = self.last_feedback
        stats = self._format_harness_stats(mres)         # surface report-only harness fields
        if stats:
            fb = (stats + "\n\n" + fb).strip() if fb else stats
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
            if self.aborted or (should_stop and should_stop()):
                best = self.state.best_score(self.run_id)
                self.state.set_status(self.run_id, "stopped")
                return LoopSummary("stopped", best, self.n)
            ctx = self._drain_operator_context()       # live 'Steer' command, applied once
            outcome = self.run_iteration(context_text=ctx, on_phase=on_phase)
            if on_iteration:
                on_iteration(outcome)
            if self.aborted:                   # Force-Stop landed mid-iteration
                best = self.state.best_score(self.run_id)
                self.state.set_status(self.run_id, "stopped")
                return LoopSummary("stopped", best, self.n)
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
