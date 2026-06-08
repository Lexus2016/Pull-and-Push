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
