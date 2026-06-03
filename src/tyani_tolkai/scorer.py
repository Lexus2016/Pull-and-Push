"""Deterministic scorer (spec §3, §6).

The scalar score and the keep/discard decision come from *code here*, never from
an LLM. Normalization is pinned to per-metric target-range: each metric maps its
``worst → target`` range onto ``0 → 100`` (clamped), so the zero-point is stable
across iterations. The weighted aggregate is the run's single score.
"""

from __future__ import annotations

from typing import Iterable, Literal, Mapping

from .config import MetricCfg

Verdict = Literal["keep", "discard"]


def normalize(value: float, worst: float, target: float, dir: str = "higher") -> float:
    """Map ``value`` onto 0–100 via the worst→target range, clamped.

    ``worst`` and ``target`` already encode direction (for ``dir='lower'`` the
    target is below the worst), so a single linear map works for both; ``dir`` is
    accepted for clarity/validation only.
    """
    if target == worst:
        raise ValueError("worst and target must differ")
    progress = (value - worst) / (target - worst)
    return max(0.0, min(100.0, progress * 100.0))


def score(values: Mapping[str, float], metrics: Iterable[MetricCfg]) -> float:
    """Weighted aggregate of normalized metrics → 0–100."""
    metrics = list(metrics)
    total_w = sum(m.weight for m in metrics)
    if total_w <= 0:
        raise ValueError("total metric weight must be > 0")
    acc = 0.0
    for m in metrics:
        if m.name not in values:
            raise KeyError(f"metric {m.name!r} missing from values")
        acc += normalize(values[m.name], m.worst, m.target, m.dir) * m.weight
    return acc / total_w


def decide(new_score: float, best_score: float | None, min_delta: float = 0.0) -> Verdict:
    """Keep iff the improvement clears the noise band.

    First iteration (no baseline yet) is always kept.
    """
    if best_score is None:
        return "keep"
    # strict: a candidate must IMPROVE beyond the noise band, not merely tie it —
    # otherwise a changed-but-not-better candidate gets committed and resets plateau.
    return "keep" if (new_score - best_score) > min_delta else "discard"
