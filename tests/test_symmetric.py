from tyani_tolkai.symmetric import best_vs_all, dominance_reached, promotion_gate_ok


def test_promotion_gate_blocks_regression():
    assert promotion_gate_ok(candidate_archive_min=0.8, prev_champion_archive_min=0.85,
                             promote_regression_max=0.1) is True    # 0.05 drop ≤ 0.1 → ok
    assert promotion_gate_ok(candidate_archive_min=0.6, prev_champion_archive_min=0.85,
                             promote_regression_max=0.1) is False   # 0.25 drop > 0.1 → blocked


def test_promotion_gate_first_champion_always_ok():
    assert promotion_gate_ok(candidate_archive_min=0.3, prev_champion_archive_min=None,
                             promote_regression_max=0.1) is True


def test_dominance_reached_when_A_beats_whole_B_archive():
    matrix = {(1, 2): 0.6, (1, 4): 0.5, (3, 2): 0.97, (3, 4): 0.96}
    assert dominance_reached(matrix, a_ids=[1, 3], b_ids=[2, 4], tau=0.95, side="A") is True
    assert dominance_reached(matrix, a_ids=[1, 3], b_ids=[2, 4], tau=0.95, side="B") is False


def test_dominance_false_when_matrix_incomplete():
    matrix = {(3, 2): 0.97}   # missing (3,4)
    assert dominance_reached(matrix, a_ids=[1, 3], b_ids=[2, 4], tau=0.95, side="A") is False


def test_best_vs_all_picks_robust_champion():
    matrix = {(1, 2): 0.6, (1, 4): 0.9, (3, 2): 0.8, (3, 4): 0.85}
    # A=1 worst=0.6 ; A=3 worst=0.8 → best-A-vs-all-B = champ 3
    assert best_vs_all(matrix, a_ids=[1, 3], b_ids=[2, 4], side="A") == 3
    # B perspective: 1-score. B=2 worsts {1-0.6=0.4, 1-0.8=0.2}→0.2 ; B=4 {0.1,0.15}→0.1 → B=2
    assert best_vs_all(matrix, a_ids=[1, 3], b_ids=[2, 4], side="B") == 2


# ---- integration: full SymmetricOrchestrator on the CEGIS toy ----

from pathlib import Path  # noqa: E402

from tyani_tolkai.arena.cegis import CegisReferee, in_L  # noqa: E402
from tyani_tolkai.agents.scripted_rival import (  # noqa: E402
    ScriptedAdversaryRival, ScriptedRecognizerRival)
from tyani_tolkai.config import Config  # noqa: E402
from tyani_tolkai.sandbox import LocalBackend  # noqa: E402
from tyani_tolkai.symmetric import ArenaResult, SymmetricOrchestrator  # noqa: E402


def _toy_cfg(**arena_over):
    arena = {"referee": "cegis-recognizer", "generations": 6, "per_generation_iterations": 3,
             "dominance_tau": 0.99, "dominance_rounds": 1, "plateau_generations": 4}
    arena.update(arena_over)
    return Config(project="toy", mode="symmetric",
                  agents={"rival_a": {"engine": "mock"}, "rival_b": {"engine": "mock"}},
                  roles={"rival_a": {"goal": "recognize the hidden language"},
                         "rival_b": {"goal": "find strings the recognizer misclassifies"}},
                  evaluation={"adapter": "numeric", "command": "x",
                              "metrics": [{"name": "arena_fitness", "dir": "higher",
                                           "target": 1.0, "worst": 0.0}]},
                  arena=arena)


def test_symmetric_run_converges_and_stops(tmp_path):
    orch = SymmetricOrchestrator(
        cfg=_toy_cfg(), root=tmp_path, referee=CegisReferee(),
        executor_a=ScriptedRecognizerRival(), executor_b=ScriptedAdversaryRival(),
        sandbox=LocalBackend())
    result = orch.run()
    assert isinstance(result, ArenaResult)
    assert result.stop_reason in ("dominance", "plateau")
    assert result.best_a_id is not None
    best_a_dir = orch.champion_dir("A", result.best_a_id)
    ns: dict = {}
    exec((Path(best_a_dir) / "recognizer.py").read_text(), ns)
    for s in ["ab", "aab", "aabb", "b", "ba", "", "aaa", "bbb", "aba"]:
        assert ns["accepts"](s) == in_L(s)
