"""Brief Builder (spec §4): assemble the curated iteration brief from state.

The Orchestrator — not the agent — owns memory. Each iteration we hand the agent a
compact, deterministic brief: goal, best/target, last delta, and the attempt history
of the last K iterations *with their real diffs* (the gradient-like signal) plus an
oscillation flag. The brief is bounded and reproducible.
"""

from __future__ import annotations

from .config import Config
from .state import StateStore

_MAX_DIFF_LINES = 15


def _truncate(text: str, n: int = _MAX_DIFF_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text.strip()
    return "\n".join(lines[:n] + [f"… (+{len(lines) - n} more lines)"])


def _added_removed(diff: str) -> tuple[set[str], set[str]]:
    added, removed = set(), set()
    for ln in diff.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            added.add(ln[1:].strip())
        elif ln.startswith("-") and not ln.startswith("---"):
            removed.add(ln[1:].strip())
    added.discard("")
    removed.discard("")
    return added, removed


def detect_oscillation(diffs: list[str]) -> bool:
    """True if a later attempt removes a line an earlier attempt added (seesaw)."""
    for i in range(len(diffs)):
        added_i, _ = _added_removed(diffs[i])
        for j in range(i + 1, len(diffs)):
            _, removed_j = _added_removed(diffs[j])
            if added_i & removed_j:
                return True
    return False


def _attempt_diff(state: StateStore, verdict: str | None, git_hash: str | None,
                  change_summary: str | None) -> str:
    if verdict == "keep" and git_hash:
        try:
            return state.diff(f"{git_hash}~1", git_hash)
        except Exception:
            return "(initial commit)"
    # discarded candidates aren't committed; the orchestrator stores their diff
    # in change_summary for Phase 1
    return change_summary or "(reverted; diff not retained)"


def build_validator_prompt(cfg: Config, candidate_diff: str, metrics_values: dict,
                           new_score: float | None, verdict: str, prev_feedback: str = "") -> str:
    """Prompt for the read-only Validator (spec §4): it sees the candidate diff, the
    metrics, the verdict — and, when rejected, its own prior advice — then returns one
    concrete next step. It never assigns the number."""
    role = cfg.roles.get("validator")
    goal = role.goal if role else "Diagnose why the score moved; give one concrete next step."
    score_txt = "n/a (did not run)" if new_score is None else f"{new_score:.2f}"
    lines = [
        "You are the read-only VALIDATOR. Do NOT edit files. Return only short, concrete advice.",
        f"- Goal: {goal}",
        f"- This candidate scored {score_txt} → verdict: {verdict.upper()}",
        f"- Metrics: " + (", ".join(f"{k}={v}" for k, v in metrics_values.items()) or "(none)"),
    ]
    if verdict in ("discard", "fail"):
        lines.append("- This attempt was REJECTED (it did not clear the best score + noise band).")
        if prev_feedback:
            lines.append(f"- Your PREVIOUS advice was: {prev_feedback!r} — it did not work; change direction.")
    lines.append("- Candidate diff:")
    lines.append(_truncate(candidate_diff, 40))
    lines.append("Give ONE concrete next change to raise the weighted score (<=3 sentences).")
    return "\n".join(lines)


def build_brief(state: StateStore, run_id: int, cfg: Config,
                role: str = "executor", context_text: str = "",
                validator_feedback: str = "") -> str:
    role_cfg = cfg.roles[role]
    best = state.best_score(run_id)
    target = cfg.evaluation.target_score
    attempts = state.last_iterations(run_id, cfg.history.depth_k)

    lines: list[str] = []
    n = (attempts[-1].n + 1) if attempts else 1
    lines.append(f"ITERATION BRIEF (iteration {n})")
    lines.append(f"- Goal: {role_cfg.goal}")
    if role_cfg.task:
        lines.append(f"- Task constraint: {role_cfg.task}")
    lines.append(f"- Current best score: {best if best is not None else '(none yet)'} "
                 f"(target: {target})")

    if attempts:
        last = attempts[-1]
        prev = attempts[-2].score if len(attempts) > 1 else None
        verdict = (last.verdict or "?").upper()
        lines.append(f"- Last iteration: {prev}→{last.score} ({verdict})")
    lines.append(f"- Validator feedback: {validator_feedback or '(none)'}")

    diffs: list[str] = []
    lines.append("- Attempt history (last K), with the real diff of each attempt:")
    if not attempts:
        lines.append("    (none — this is the first iteration)")
    for a in attempts:
        d = _attempt_diff(state, a.verdict, a.git_hash, a.change_summary)
        diffs.append(d)
        mtxt = ", ".join(f"{m['name']}={m['value']}" for m in a.metrics) or "(no metrics)"
        lines.append(f"    #{a.n}  {(a.verdict or '?').upper()}  score={a.score}")
        lines.append(f"      metrics: {mtxt}")
        for dl in _truncate(d).splitlines():
            lines.append(f"      {dl}")

    lines.append(f"- Oscillation flag: {'YES' if detect_oscillation(diffs) else 'no'}")
    lines.append(f"- Live operator instructions: {context_text.strip() or '(none)'}")
    lines.append("- Your task: make ONE focused change to improve the weighted score. "
                 "Edit files in place.")
    return "\n".join(lines)
