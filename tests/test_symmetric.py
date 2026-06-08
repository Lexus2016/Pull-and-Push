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


# ---- P7-A: persistence + resume ----

import json as _json  # noqa: E402


def test_symmetric_manifest_persisted_and_finished_is_idempotent(tmp_path):
    ref = CegisReferee()
    o1 = SymmetricOrchestrator(cfg=_toy_cfg(), root=tmp_path, referee=ref,
                               executor_a=ScriptedRecognizerRival(),
                               executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    r1 = o1.run()
    manifest = tmp_path / "arena.json"
    assert manifest.exists()
    m = _json.loads(manifest.read_text())
    assert m["status"] == "finished"
    assert m["stop_reason"] in ("dominance", "plateau", "max_generations")
    assert m["best_a_id"] == r1.best_a_id
    assert len(m["champions"]["A"]) >= 1 and len(m["champions"]["B"]) >= 1
    champ_count_a = len(m["champions"]["A"])

    # re-construct on the same root → finished run is an idempotent no-op (no re-bootstrap)
    o2 = SymmetricOrchestrator(cfg=_toy_cfg(), root=tmp_path, referee=CegisReferee(),
                               executor_a=ScriptedRecognizerRival(),
                               executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    r2 = o2.run()
    assert r2.best_a_id == r1.best_a_id and r2.stop_reason == r1.stop_reason
    m2 = _json.loads(manifest.read_text())
    assert len(m2["champions"]["A"]) == champ_count_a   # no duplicate champions added


def test_symmetric_resume_continues_unfinished(tmp_path):
    o1 = SymmetricOrchestrator(cfg=_toy_cfg(generations=4), root=tmp_path, referee=CegisReferee(),
                               executor_a=ScriptedRecognizerRival(),
                               executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    o1.run()
    # simulate a crash AFTER generations completed but BEFORE the finalize write
    manifest = tmp_path / "arena.json"
    m = _json.loads(manifest.read_text())
    m["status"] = "running"
    manifest.write_text(_json.dumps(m))

    o2 = SymmetricOrchestrator(cfg=_toy_cfg(generations=4), root=tmp_path, referee=CegisReferee(),
                               executor_a=ScriptedRecognizerRival(),
                               executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    assert o2._resuming is True
    r2 = o2.run()
    # resumed run still delivers L and finishes cleanly
    from pathlib import Path as _P
    ns: dict = {}
    exec((_P(o2.champion_dir("A", r2.best_a_id)) / "recognizer.py").read_text(), ns)
    for s in ["ab", "aab", "b", "ba", "", "aba"]:
        assert ns["accepts"](s) == in_L(s)


def test_symmetric_run_respects_external_stop(tmp_path):
    o = SymmetricOrchestrator(cfg=_toy_cfg(generations=8), root=tmp_path, referee=CegisReferee(),
                              executor_a=ScriptedRecognizerRival(),
                              executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    r = o.run(should_stop=lambda: True)            # UI Stop pressed immediately
    assert r.stop_reason == "stopped"
    m = _json.loads((tmp_path / "arena.json").read_text())
    assert m["status"] == "stopped"
    # a stopped run is resumable (not a finished no-op)
    o2 = SymmetricOrchestrator(cfg=_toy_cfg(generations=8), root=tmp_path, referee=CegisReferee(),
                               executor_a=ScriptedRecognizerRival(),
                               executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    assert o2._resuming is True
    r2 = o2.run()                                  # resume → now runs to a real terminal
    assert r2.stop_reason in ("dominance", "plateau", "max_generations")


def test_symmetric_budget_cap_stops_run(tmp_path):
    cfg = Config(project="toy", mode="symmetric",
                 agents={"rival_a": {"engine": "mock"}, "rival_b": {"engine": "mock"}},
                 roles={"rival_a": {"goal": "r"}, "rival_b": {"goal": "f"}},
                 evaluation={"adapter": "numeric", "command": "x",
                             "metrics": [{"name": "arena_fitness", "dir": "higher",
                                          "target": 1.0, "worst": 0.0}]},
                 # dominance impossible (>1.0) and plateau far off → the BUDGET must be what stops it
                 arena={"referee": "cegis-recognizer", "generations": 20, "dominance_tau": 1.01,
                        "plateau_generations": 50, "per_generation_iterations": 3},
                 limits={"budget_usd": 1e-9, "usd_per_mtok": 1e9})
    o = SymmetricOrchestrator(cfg=cfg, root=tmp_path, referee=CegisReferee(),
                              executor_a=ScriptedRecognizerRival(),
                              executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    r = o.run()
    assert r.stop_reason == "budget"


def test_resume_drops_partial_generation_no_duplicate_champions(tmp_path):
    o1 = SymmetricOrchestrator(cfg=_toy_cfg(generations=4), root=tmp_path, referee=CegisReferee(),
                               executor_a=ScriptedRecognizerRival(),
                               executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    o1.run()
    champs_before = o1.ledger.champions(o1.parent_run, side="A")
    last_gen = max(c["generation"] for c in champs_before)

    # simulate a crash that left a PARTIAL extra champion at the last generation + an unfinished
    # manifest pointing one generation back (so resume re-plays the partial generation)
    o1.ledger.add_champion(o1.parent_run, "A", last_gen, "deadbeef", stable_score=0.0)
    man = tmp_path / "arena.json"
    m = _json.loads(man.read_text())
    m["status"] = "running"
    m["generation"] = last_gen - 1
    man.write_text(_json.dumps(m))

    o2 = SymmetricOrchestrator(cfg=_toy_cfg(generations=4), root=tmp_path, referee=CegisReferee(),
                               executor_a=ScriptedRecognizerRival(),
                               executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    assert o2._resuming is True
    o2.run()

    # after resume: at most ONE champion per (side, generation) — the partial dup was dropped
    for side in ("A", "B"):
        seen = [c["generation"] for c in o2.ledger.champions(o2.parent_run, side=side)]
        assert len(seen) == len(set(seen)), f"duplicate champion generation on side {side}: {seen}"


def test_budget_is_scoped_to_this_run_not_whole_db(tmp_path):
    # prior unrelated cost sitting in the side DB must NOT count against this run's budget
    o = SymmetricOrchestrator(cfg=_toy_cfg(generations=4), root=tmp_path, referee=CegisReferee(),
                              executor_a=ScriptedRecognizerRival(),
                              executor_b=ScriptedAdversaryRival(), sandbox=LocalBackend())
    # inject a big "previous run" cost into a side DB (the old global SUM would trip on this)
    o.state_a.conn.execute(
        "INSERT INTO run (status, mode, cost_total, created_ts) VALUES ('finished','symmetric',999.0,'t')")
    o.state_a.conn.commit()
    # give it a budget that the injected 999 would blow, but this run's real cost (mock=0) won't
    o.cfg.limits.budget_usd = 10.0
    r = o.run()
    assert r.stop_reason != "budget"            # scoped budget ignores the unrelated 999
    assert r.stop_reason in ("dominance", "plateau", "max_generations")
