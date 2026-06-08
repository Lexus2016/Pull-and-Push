"""ArenaMetricAdapter — bridges a referee + frozen opponent pool into the Phase-1 scorer.

Emits ONE scored metric ``arena_fitness`` (the §2 gate, computed HERE — not by handing two raw
metrics to the weighted scorer) plus report-only mean/min/per-opponent/counterexamples in
``MetricResult.data``. The unchanged scorer + decide consume ``arena_fitness`` exactly like any
numeric metric, so the Phase-1 ``run_iteration`` loop runs unmodified against a frozen pool.
"""
from __future__ import annotations

from pathlib import Path

from .base import MetricResult


class ArenaMetricAdapter:
    def __init__(self, referee, side, opponents, *, aggregate="mean_gated",
                 min_floor=0.5, penalty=1.0, seed=0):
        self.referee = referee
        self.side = side                      # 'A' or 'B' — which dir holds the LIVE artifact
        self.opponents = [Path(o) for o in opponents]
        self.aggregate = aggregate
        self.min_floor = min_floor
        self.penalty = penalty
        self.seed = seed

    def _live_score(self, outcome) -> float:
        return outcome.a_score if self.side == "A" else outcome.b_score

    def _gate(self, scores: list[float]) -> tuple[float, float, float]:
        mean = sum(scores) / len(scores)
        mn = min(scores)
        if self.aggregate == "mean":
            fit = mean
        elif self.aggregate == "min":
            fit = mn
        else:  # mean_gated: usable gradient from the mean, penalized if the worst case collapses
            fit = mean if mn >= self.min_floor else mean - self.penalty * (self.min_floor - mn)
        return max(0.0, min(1.0, fit)), mean, mn

    def run(self, artifact_dir, sandbox, evaluation, timeout) -> MetricResult:
        artifact_dir = Path(artifact_dir)
        if not self.opponents:
            return MetricResult(metrics=[], logs="no opponents in pool", ok=False)
        per, scores, counters = [], [], []
        for opp in self.opponents:
            a_dir, b_dir = (artifact_dir, opp) if self.side == "A" else (opp, artifact_dir)
            out = self.referee.play(a_dir, b_dir, sandbox, seed=self.seed)
            sc = self._live_score(out)
            scores.append(sc)
            per.append({"opponent": str(opp), "score": sc})
            counters.extend(out.detail.get("counterexamples", []))
        fit, mean, mn = self._gate(scores)
        metrics = [{"name": "arena_fitness", "value": fit, "dir": "higher", "weight": 1.0}]
        data = {"mean": mean, "min": mn, "per_opponent": per, "counterexamples": counters}
        return MetricResult(metrics=metrics, logs=f"arena_fitness={fit:.4f}", ok=True, data=data)
