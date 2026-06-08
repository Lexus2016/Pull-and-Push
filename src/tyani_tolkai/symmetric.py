"""SymmetricOrchestrator — alternating self-play over the Phase-1 loop (design §4).

The inner Phase-1 loop is only a LOCAL-best generator against a frozen pool; the arena's true
evaluation lives in the champion archive, the match matrix (§4a), and the stable signals. The
pure functions below implement the matrix-derived decisions; the orchestrator class wires them
to the Phase-1 engine.
"""
from __future__ import annotations


def promotion_gate_ok(candidate_archive_min, prev_champion_archive_min,
                      promote_regression_max) -> bool:
    """Crown the candidate only if it does not regress against the WHOLE opposing archive by
    more than ``promote_regression_max`` vs the prior champion's archive-wide worst case (§3).
    The first champion (no prior) is always accepted."""
    if prev_champion_archive_min is None:
        return True
    return (prev_champion_archive_min - candidate_archive_min) <= promote_regression_max


def _a_view(matrix, a_id, b_id, side):
    """The score of champion `side` in the (a_id, b_id) pairing, from that side's perspective."""
    s = matrix.get((a_id, b_id))
    if s is None:
        return None
    return s if side == "A" else 1.0 - s


def dominance_reached(matrix, a_ids, b_ids, tau, side) -> bool:
    """Latest champion of `side` beats the ENTIRE opposing archive at >= tau."""
    if side == "A":
        if not a_ids or not b_ids:
            return False
        latest = a_ids[-1]
        scores = [_a_view(matrix, latest, b, "A") for b in b_ids]
    else:
        if not a_ids or not b_ids:
            return False
        latest = b_ids[-1]
        scores = [_a_view(matrix, a, latest, "B") for a in a_ids]
    scores = [s for s in scores if s is not None]
    return bool(scores) and len(scores) == len(b_ids if side == "A" else a_ids) and all(s >= tau for s in scores)


def best_vs_all(matrix, a_ids, b_ids, side):
    """The champion of `side` with the best WORST-CASE score across the whole opposing archive
    (robustness — the deliverable best-A-vs-all-B / best-B-vs-all-A)."""
    ids = a_ids if side == "A" else b_ids
    best_id, best_worst = None, -1.0
    for cid in ids:
        if side == "A":
            worsts = [_a_view(matrix, cid, b, "A") for b in b_ids]
        else:
            worsts = [_a_view(matrix, a, cid, "B") for a in a_ids]
        worsts = [w for w in worsts if w is not None]
        if not worsts:
            continue
        w = min(worsts)
        if w > best_worst:
            best_worst, best_id = w, cid
    return best_id
