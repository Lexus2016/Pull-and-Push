"""P4 secondary validation (spec: docs/design/p4-secondary-validation.md).

Control strategies are trusted reference order-sequences scored by the SAME vetted engine; they
check the scorer discriminates and that the bot beats trivial baselines. NOT the trust mechanism
(the engine + sandbox are) — these are secondary checks for the human evidence report.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path

from .bot_engine import simulate


def flat_orders(n: int) -> list[int]:
    """Do nothing: position 0 on every bar."""
    return [0] * n


def buy_and_hold_orders(n: int) -> list[int]:
    """Go long at the first bar and hold."""
    return [1] + [0] * (n - 1) if n > 0 else []


def random_orders(n: int, *, seed: str) -> list[int]:
    """Deterministic pseudo-random positions in {-1,0,1} (seeded → reproducible)."""
    r = random.Random(seed)
    return [r.choice((-1, 0, 1)) for _ in range(n)]


def score_controls(bars, *, oos_start: int, params: dict) -> dict[str, dict]:
    """Score the three control strategies through the vetted engine (in-process, no sandbox)."""
    n = len(bars)
    return {
        "flat": simulate(bars, flat_orders(n), oos_start=oos_start, params=params),
        "buy_and_hold": simulate(bars, buy_and_hold_orders(n), oos_start=oos_start, params=params),
        "random": simulate(bars, random_orders(n, seed="control"), oos_start=oos_start, params=params),
    }


def beats_controls(bot_metrics: dict, control_metrics: dict) -> dict:
    """Verdict: does the bot's OOS return beat the flat (do-nothing) and random baselines?"""
    bot = bot_metrics["return_oos_pct"]
    return {
        "beats_flat": bot >= control_metrics["flat"]["return_oos_pct"],
        "beats_random": bot >= control_metrics["random"]["return_oos_pct"],
    }


def check_determinism(score_callable, *, runs: int = 2) -> tuple[bool, list[dict]]:
    """Call score_callable `runs` times; ok iff every result equals the first.

    Generic over any scorer (engine, subprocess, or sandbox) — catches a bot that is
    deterministic on some runs then diverges once it infers it is 'in production'.
    """
    scores = [score_callable() for _ in range(max(1, runs))]
    ok = all(s == scores[0] for s in scores)
    return ok, scores


def hash_artifacts(*, engine_path, data_path, config: dict) -> dict[str, str]:
    """sha256 provenance fingerprint of the engine source, the data bytes, and the config.

    The human approves an exact frozen (engine, data, config) triple before trusting a run.
    """
    eng = hashlib.sha256(Path(engine_path).read_bytes()).hexdigest()
    data = hashlib.sha256(Path(data_path).read_bytes()).hexdigest()
    cfg = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"engine": eng, "data": data, "config": cfg}


def insample_oos_gap(metrics: dict) -> float:
    """In-sample minus out-of-sample return (large positive = overfitting signal)."""
    return metrics["in_sample_return_pct"] - metrics["return_oos_pct"]


# A large in-sample-minus-OOS gap (percentage points) is treated as an overfit warning.
_OVERFIT_GAP_FLAG = 30.0


def reverse_oos(bars, *, oos_start: int) -> list:
    """Return a copy of ``bars`` with the OOS tail (``bars[oos_start:]``) reversed.

    The anti-look-ahead perturbation: it destroys the temporal order of the future price path
    while leaving the in-sample prefix intact. A leak-free, causal OOS score MUST change under
    this; an invariant score means the score does not depend on the real future (degenerate or
    look-ahead-leaking). Never mutates the input.
    """
    bars = list(bars)
    n = len(bars)
    start = max(0, oos_start)
    if start >= n:
        return bars  # no OOS tail to perturb
    return bars[:start] + list(reversed(bars[start:]))


def _oos_value(x) -> float:
    """Coerce a scorer result (metrics dict or scalar) to its OOS return number."""
    return float(x["return_oos_pct"]) if isinstance(x, dict) else float(x)


def anti_lookahead_probe(score_real, score_perturbed, *, epsilon: float = 1e-9) -> dict:
    """Causality check: the OOS score must REACT to a reversed-future timeline.

    ``score_real`` / ``score_perturbed`` are zero-arg callables returning a metrics dict (with
    ``return_oos_pct``) or a scalar — typically the same scorer over ``bars`` vs ``reverse_oos(bars)``.
    NECESSARY, not sufficient: reacting does not prove the absence of every leak, but NOT reacting
    is a hard red flag (the score ignores the actual future path). ``ok`` is True iff it reacted.
    """
    baseline = _oos_value(score_real())
    perturbed = _oos_value(score_perturbed())
    delta = baseline - perturbed
    reacted = abs(delta) > epsilon
    return {"baseline": baseline, "perturbed": perturbed, "delta": delta,
            "reacted": reacted, "ok": reacted}


def evidence_verdict(*, beats: dict, determinism_ok: bool, gap: float,
                     anti_lookahead: dict | None = None) -> tuple[str, list[str]]:
    """Shared PASS/FLAG decision + human-readable reasons, used by both the report and the
    ``validate`` CLI so the rendered evidence and the process exit code never disagree."""
    reasons: list[str] = []
    if not beats.get("beats_flat", False):
        reasons.append("does not beat the flat (do-nothing) baseline")
    if not beats.get("beats_random", False):
        reasons.append("does not beat the random baseline")
    if not determinism_ok:
        reasons.append("non-deterministic (same input produced different scores)")
    if gap > _OVERFIT_GAP_FLAG:
        reasons.append(f"large in-sample/OOS gap ({gap:.1f} pts) — possible overfitting")
    if anti_lookahead is not None and not anti_lookahead.get("ok", True):
        reasons.append("OOS score did not react to a perturbed timeline "
                       "(possible look-ahead leak or degenerate scorer)")
    return ("PASS" if not reasons else "FLAG"), reasons


def build_evidence_report(*, bot_name: str, bot_metrics: dict, control_metrics: dict,
                          beats: dict, determinism_ok: bool, hashes: dict, gap: float,
                          anti_lookahead: dict | None = None, isolation: str | None = None) -> str:
    """Render the secondary-validation evidence report (Markdown) for human approval.

    Overall verdict is FLAG if the bot fails to beat a control, is non-deterministic, or shows a
    large in-sample/OOS overfit gap; otherwise PASS. These are SECONDARY checks — a PASS is
    supporting evidence, not a guarantee.
    """
    verdict, reasons = evidence_verdict(beats=beats, determinism_ok=determinism_ok, gap=gap,
                                        anti_lookahead=anti_lookahead)

    out: list[str] = []
    out.append(f"# Evidence Report — {bot_name}\n")
    out.append("> SECONDARY checks. A PASS is supporting evidence, not a guarantee; the vetted "
               "engine + sandbox remain the trust mechanism.\n")

    out.append("## Provenance (approve this exact frozen triple)")
    out.append(f"- engine: `{hashes.get('engine', '?')}`")
    out.append(f"- data:   `{hashes.get('data', '?')}`")
    out.append(f"- config: `{hashes.get('config', '?')}`")
    if isolation:
        out.append(f"- isolation: {isolation}")
    out.append("")

    out.append("## Control spectrum (scored by the same vetted engine)")
    out.append(f"- flat:         {control_metrics['flat']['return_oos_pct']}")
    out.append(f"- random:       {control_metrics['random']['return_oos_pct']}")
    out.append(f"- buy_and_hold: {control_metrics['buy_and_hold']['return_oos_pct']}")
    out.append(f"- **{bot_name}: {bot_metrics['return_oos_pct']}**  "
               f"(beats flat: {beats.get('beats_flat')}, beats random: {beats.get('beats_random')})\n")

    out.append("## Determinism")
    out.append(f"- {'OK — identical scores across runs' if determinism_ok else 'FAILED — scores diverged'}\n")

    out.append("## Overfit gap")
    out.append(f"- in-sample minus OOS return: {gap:.1f} pts"
               f"{' (FLAGGED)' if gap > _OVERFIT_GAP_FLAG else ''}\n")

    if anti_lookahead is not None:
        out.append("## Anti-look-ahead probe (reversed-future timeline)")
        out.append(f"- real OOS: {anti_lookahead.get('baseline')}  |  "
                   f"reversed-future OOS: {anti_lookahead.get('perturbed')}  |  "
                   f"delta: {anti_lookahead.get('delta')}")
        out.append("- " + ("OK — score reacted to the perturbed timeline"
                           if anti_lookahead.get("ok")
                           else "FLAGGED — score did not react (possible look-ahead / "
                                "degenerate scorer)") + "\n")

    out.append(f"## Overall: {verdict}")
    if reasons:
        for r in reasons:
            out.append(f"- {r}")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- P4.6 adapter gate
def synth_bars(n: int = 40) -> list:
    """Deterministic, non-monotonic OHLCV probe series for the adapter gate.

    Rises and falls (no RNG → reproducible) so a direction-sensitive ``decide()`` actually varies
    its orders; a constant-output adapter then stands out as degenerate.
    """
    bars = []
    price = 100.0
    for i in range(n):
        price *= 1.0 + 0.02 * math.sin(i / 2.0)
        c = price * (1.005 if i % 2 else 0.997)
        bars.append((price, price * 1.01, price * 0.99, c, 10.0))
    return bars


def check_adapter_orders(run1, run2, n_bars: int) -> dict:
    """Verdict on an adapter's order stream (protocol soundness, NOT semantic correctness).

    ``run1`` / ``run2`` are the order lists from two `drive_bot` passes over the SAME synthetic bars.
    Checks: well-formed (one order per bar, each in {-1,0,1}); deterministic (the two runs match);
    non-degenerate (not the same order on every bar of a varied series — catches an unwired/constant
    `decide()`). ``ok`` requires all three. Sign/scale/look-ahead defects are NOT caught here — they
    are caught downstream by `validate` (control spectrum + anti-look-ahead) and the human.
    """
    reasons: list[str] = []
    well_formed = (run1 is not None and len(run1) == n_bars
                   and all(o in (-1, 0, 1) for o in run1))
    if not well_formed:
        reasons.append("not well-formed: expected one order in {-1,0,1} per bar")
    deterministic = run1 is not None and run2 is not None and run1 == run2
    if not deterministic:
        reasons.append("non-deterministic: two runs over identical bars produced different orders")
    degenerate = bool(run1) and len(set(run1)) <= 1
    if degenerate:
        reasons.append("degenerate: the same order on every bar — decide() may be unwired or constant")
    ok = well_formed and deterministic and not degenerate
    return {"well_formed": well_formed, "deterministic": deterministic,
            "degenerate": degenerate, "ok": ok, "reasons": reasons}
