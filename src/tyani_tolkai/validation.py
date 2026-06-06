"""P4 secondary validation (spec: docs/design/p4-secondary-validation.md).

Control strategies are trusted reference order-sequences scored by the SAME vetted engine; they
check the scorer discriminates and that the bot beats trivial baselines. NOT the trust mechanism
(the engine + sandbox are) — these are secondary checks for the human evidence report.
"""
from __future__ import annotations

import hashlib
import json
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
