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


def metrics_signature(metrics) -> str:
    """Stable signature of the scoring OBJECTIVE — changes whenever a metric is added/removed or
    its direction/target/weight changes. Used to detect a mid-run objective change → re-baseline."""
    return json.dumps(sorted((m.name, m.dir, float(m.target), float(m.weight)) for m in metrics))


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
        self._reviewed_streak = False  # did the reviewer already give a rethink in this discard streak?
        self.aborted = False          # set by force_kill() — stop NOW, kill the live agent
        # estimated cumulative cost (USD) across the run; restored on resume so the budget cap holds
        self.cost_total = float(run["cost_total"]) if (run and "cost_total" in run.keys()
                                                       and run["cost_total"] is not None) else 0.0
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

    def _rebaseline_if_metrics_changed(self, ph) -> None:
        """If the scoring OBJECTIVE changed since this run last ran (metric added/removed, or a
        target/weight/direction edited), the stored best_score is on the OLD scale — so a genuinely
        better artifact can read lower and be wrongly discarded. Re-measure the current best under
        the NEW objective and make THAT the bar: pin any new metric's zero-point (keep existing ones
        for continuity), set best_score to the fresh measurement, and log it. Runs once per run."""
        cfg, state = self.cfg, self.state
        run = state.get_run(self.run_id)
        stored = run["metrics_sig"] if (run and "metrics_sig" in run.keys()) else None
        cur = metrics_signature(cfg.evaluation.metrics)
        if stored == cur:
            return
        best = state.best_score(self.run_id)
        if stored is None or best is None or state.is_artifact_empty():
            state.update_run(self.run_id, metrics_sig=cur)     # nothing to re-baseline yet — just record
            return
        ph("scoring")
        mres = self._run_metrics()                             # re-score the current best (HEAD)
        state.revert_uncommitted()
        if not mres.ok:
            return                                             # can't re-measure now → retry next run
        values = {m["name"]: m["value"] for m in mres.metrics}
        if any(m.name not in values for m in cfg.evaluation.metrics):
            return
        self._resolve_baseline(values)                         # pin zero-points for any NEW metric
        fresh = score(values, cfg.evaluation.metrics)
        state.update_run(self.run_id, best_score=fresh, metrics_sig=cur)
        try:
            sep = "─" * 60
            with (state.project_dir / "agent.log").open("a", encoding="utf-8") as f:
                f.write(f"\n{sep}\n♻ metrics changed → re-baselined: current best re-measured under "
                        f"the new objective = {fresh:.2f} (was {best:.2f} on the old scale). The loop "
                        f"now keeps improvements measured the same way.\n{sep}\n")
        except OSError:
            pass

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

    def _artifact_snapshot(self, max_chars: int = 9000) -> str:
        """The whole current artifact (the system under review) as text, so the Reviewer can judge
        the overall design — not just the diff. Excludes the hidden harness dir (anti-collusion #4)
        and big/binary data files; caps total size to keep the prompt bounded."""
        harness = (self.cfg.evaluation.harness_dir or "metrics").strip("/")
        parts, used = [], 0
        for rel in self.state.tracked_files():
            if rel.split("/", 1)[0] == harness:          # never show the scorer to the reviewer
                continue
            p = self.state.artifact_dir / rel
            try:
                if p.stat().st_size > 50_000:            # skip data blobs / huge files
                    continue
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue                                 # binary or unreadable → skip
            chunk = f"--- {rel} ---\n{text}\n"
            if used + len(chunk) > max_chars:
                chunk = chunk[: max(0, max_chars - used)] + "\n…(truncated)\n"
            parts.append(chunk)
            used += len(chunk)
            if used >= max_chars:
                break
        return "".join(parts).strip()

    def _report_extras(self, mres) -> dict:
        """Report-only fields the harness emitted beyond the scored metrics (win rate, profit
        factor, trade count, tested period, …) — useful diagnostics for the reviewer's analysis."""
        data = getattr(mres, "data", None) or {}
        if not isinstance(data, dict):
            return {}
        scored = {m.name for m in self.cfg.evaluation.metrics}
        return {k: v for k, v in data.items() if k not in scored and v is not None}

    def _should_review(self, verdict: str) -> bool:
        """Event-triggered reviewer: spend an LLM review only where it adds value, not on every
        step (the reviewer is the second expensive call per iteration). General across task types:
          - keep    → always (a new best: understand why it worked and propose the next move),
          - discard → ONCE per stuck streak, fired at ~half the plateau budget so the executor
            still has iterations left to act on the rethink (NOT on the last step before the loop
            gives up, where the advice would be wasted),
          - fail / no_op → never (the harness error or 'no change' is the signal; prose won't help).
        Between reviews the executor keeps the last real review (advice for the current best) plus
        the attempt history + scores, so it still has guidance without paying for a call each step."""
        if self.validator is None:
            return False
        if verdict == "keep":
            return True
        if verdict == "discard" and not self._reviewed_streak:
            threshold = max(2, (self.cfg.limits.plateau_N + 1) // 2)
            return self.plateau_count + 1 >= threshold
        return False

    def _consult_validator(self, values: dict, new_score, verdict: str, candidate_diff: str,
                           report_stats: dict | None = None) -> None:
        """Run the read-only Validator for feedback; guarantee read-only by reverting
        any edits it makes (not every CLI honors a read-only flag)."""
        if self.validator is None:
            return
        cfg, state = self.cfg, self.state
        vprompt = build_validator_prompt(cfg, candidate_diff, values, new_score, verdict,
                                         self.last_feedback, artifact_text=self._artifact_snapshot(),
                                         report_stats=report_stats, iteration=self.n)
        vtimeout = cfg.agents["validator"].timeout if "validator" in cfg.agents else 300
        vres = self.validator.run(vprompt, state.artifact_dir, "read-only", vtimeout)
        self._charge(vprompt, vres.stdout if vres else "")   # estimate reviewer cost
        if state.has_changes():               # enforce read-only regardless of engine
            state.revert_uncommitted()
        # keep the reviewer's full assessment/why/ideas (3 short parts) — 1000 chars clipped it
        self.last_feedback = (vres.stdout or "").strip()[:2000]

    def _format_harness_stats(self, mres) -> str:
        """One-line summary of the harness's report-only fields (anything it prints beyond the
        scored metrics — e.g. trade count, win rate, profit factor, tested period). Folded into
        the iteration feedback so the operator reads it per iteration without opening raw output.
        Reads the adapter's already-parsed `data` (robust to stderr noise in the logs)."""
        raw = getattr(mres, "data", None) or {}
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

    def _charge(self, *texts: str) -> None:
        """Add the estimated cost of an agent call to the run total. Tokens are approximated as
        chars/4 (CLI agents don't report exact usage), priced at limits.usd_per_mtok. Rough on
        purpose — it powers a SAFETY CAP (budget_usd), not an invoice. No-op when price is 0."""
        price = self.cfg.limits.usd_per_mtok or 0.0
        if price <= 0:
            return
        tokens = sum(len(t or "") for t in texts) // 4
        self.cost_total += tokens / 1_000_000.0 * price

    def _revalidate_best(self, n: int, ph) -> None:
        """Periodically re-score the current best (the working tree at iteration start IS the best,
        HEAD). If it no longer holds its recorded score (dropped beyond min_delta), it was a
        noise/flaky win — demote the recorded best to the fresh measurement so the loop re-improves
        from the truth, not from a fluke. Off unless evaluation.revalidate_every > 0."""
        cfg, state = self.cfg, self.state
        every = cfg.evaluation.revalidate_every or 0
        best = state.best_score(self.run_id)
        if every <= 0 or best is None or n <= 1 or (n - 1) % every != 0:
            return
        ph("scoring")
        mres = self._run_metrics()
        state.revert_uncommitted()                 # drop scoring side-effects → tree stays = best
        if not mres.ok:
            return
        values = {m["name"]: m["value"] for m in mres.metrics}
        if any(m.name not in values for m in cfg.evaluation.metrics):
            return
        fresh = score(values, cfg.evaluation.metrics)
        if fresh < best - cfg.evaluation.min_delta:
            state.update_run(self.run_id, best_score=fresh)
            try:
                sep = "─" * 60
                with (state.project_dir / "agent.log").open("a", encoding="utf-8") as f:
                    f.write(f"\n{sep}\n⚠ re-validation @ iter {n}: best {best:.2f} → {fresh:.2f} "
                            f"(previous best did not hold — demoted as noise/flaky)\n{sep}\n")
            except OSError:
                pass

    def run_iteration(self, context_text: str = "", on_phase=None) -> IterationOutcome:
        self.n += 1
        n = self.n
        cfg, state = self.cfg, self.state
        ph = on_phase or (lambda *_: None)
        self._revalidate_best(n, ph)         # demote a noise/flaky best before building the brief
        brief = build_brief(state, self.run_id, cfg, context_text=context_text,
                            validator_feedback=self.last_feedback)

        ph("executor")                       # Executor is editing the artifact
        self._log_iteration_header(n)        # accumulate agent.log across iterations
        result = self._run_executor(brief)   # runs with restart-on-crash/timeout
        self._charge(brief, result.stdout if result else "")   # estimate executor cost

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
            # No LLM review on a broken harness — the ACTUAL error is the feedback the executor
            # needs to fix it (prose can't), so we persist stdout/stderr and skip the call.
            err = (mres.logs or "").strip()
            fb = ("evaluation error:\n" + err[:1500]) if err else ""
            if self.last_feedback:
                fb = (fb + "\n\nlast review: " + self.last_feedback).strip()
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
            fb = self.last_feedback              # standing review for context (no new call)
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

        # Reviewer runs only on events that need judgement (keep / about-to-plateau), not every
        # step — see _should_review. Between reviews self.last_feedback holds the standing advice
        # for the current best, which still flows into the executor's next brief.
        stats = self._format_harness_stats(mres)         # report-only harness fields (cheap)
        review = ""
        if self._should_review(verdict):
            ph("validator")                  # Reviewer analyzes & advises (read-only)
            self._consult_validator(values, new_score, verdict, candidate_diff,
                                    report_stats=self._report_extras(mres))
            review = self.last_feedback
            if verdict == "discard":
                self._reviewed_streak = True   # one rethink per stuck streak (until a keep resets)
        parts = []
        if stats:
            parts.append(stats)
        if review:
            parts.append(review)
        elif verdict == "discard":           # discarded without a fresh review — log a compact line
            best_txt = f"{best:.2f}" if best is not None else "n/a"
            parts.append(f"score {new_score:.2f} did not beat best {best_txt} — auto-discard, "
                         f"no review (plateau {self.plateau_count + 1}/{cfg.limits.plateau_N})")
        fb = "\n\n".join(parts)
        if verdict == "keep":
            state.record_iteration(self.run_id, n=n, git_hash=cand_hash, score=new_score,
                                   verdict="keep", metrics=mres.metrics, change_summary=candidate_diff,
                                   feedback=fb, agent_exit=result.status)
            self.plateau_count = 0
            self._reviewed_streak = False      # a keep breaks the streak → allow a fresh rethink
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

    def _checkpoint_reason(self, best) -> str | None:
        """Which (enabled) human-checkpoint condition is due, if any — to PAUSE for the operator
        instead of silently finishing. Disabled conditions fall through to the normal stop logic."""
        cp = self.cfg.checkpoints
        if cp.on_target and best is not None and best >= self.cfg.evaluation.target_score:
            return "target"
        if cp.on_plateau and self.plateau_count >= self.cfg.limits.plateau_N:
            return "plateau"
        if cp.every_n and self.n > 0 and self.n % cp.every_n == 0:
            return "periodic"
        return None

    def run_loop(self, on_iteration=None, should_stop=None, on_phase=None) -> LoopSummary:
        cfg = self.cfg
        self._rebaseline_if_metrics_changed(on_phase or (lambda *_: None))   # objective changed? rescore the bar
        while True:
            if self.aborted or (should_stop and should_stop()):
                best = self.state.best_score(self.run_id)
                self.state.set_status(self.run_id, "stopped")
                return LoopSummary("stopped", best, self.n)
            ctx = self._drain_operator_context()       # live 'Steer' command, applied once
            outcome = self.run_iteration(context_text=ctx, on_phase=on_phase)
            if on_iteration:
                on_iteration(outcome)
            self.state.update_run(self.run_id, cost_total=self.cost_total)   # persist running cost
            if self.aborted:                   # Force-Stop landed mid-iteration
                best = self.state.best_score(self.run_id)
                self.state.set_status(self.run_id, "stopped")
                return LoopSummary("stopped", best, self.n)
            if self.halt:                      # agent rate-limited or repeatedly failing
                best = self.state.best_score(self.run_id)
                self.state.set_status(self.run_id, "paused" if self.halt == "rate_limited" else "error")
                return LoopSummary(self.halt, best, self.n)
            if cfg.limits.budget_usd is not None and self.cost_total >= cfg.limits.budget_usd:
                best = self.state.best_score(self.run_id)   # estimated spend hit the cap → stop
                self.state.set_status(self.run_id, "finished")
                return LoopSummary("budget", best, self.n)
            best = self.state.best_score(self.run_id)
            cp = self._checkpoint_reason(best)
            if cp:                              # PAUSE for the operator instead of finishing
                self.state.add_checkpoint(self.run_id, self.n, cp)
                self.state.set_status(self.run_id, "awaiting_review")
                return LoopSummary("checkpoint", best, self.n)
            if best is not None and best >= cfg.evaluation.target_score:
                self.state.set_status(self.run_id, "finished")
                return LoopSummary("target", best, self.n)
            if self.plateau_count >= cfg.limits.plateau_N:
                self.state.set_status(self.run_id, "finished")
                return LoopSummary("plateau", best, self.n)
            if self.n >= cfg.limits.max_iterations:
                self.state.set_status(self.run_id, "finished")
                return LoopSummary("max_iter", best, self.n)
