from pathlib import Path

from tyani_tolkai.state import StateStore


def _new_store(tmp_path) -> StateStore:
    s = StateStore(tmp_path / "proj")
    s.git_init()
    return s


def test_git_init_and_head(tmp_path):
    s = _new_store(tmp_path)
    head = s.head()
    assert len(head) == 40  # sha-1


def test_commit_and_revert(tmp_path):
    s = _new_store(tmp_path)
    f = s.artifact_dir / "code.py"
    f.write_text("x = 1\n")
    h1 = s.commit("add code")
    assert s.commit_exists(h1)

    # uncommitted change is discarded by revert
    f.write_text("x = 999\n")
    s.revert_uncommitted()
    assert f.read_text() == "x = 1\n"


def test_record_and_last_iterations(tmp_path):
    s = _new_store(tmp_path)
    run_id = s.create_run("asymmetric")
    for n in range(1, 4):
        (s.artifact_dir / "code.py").write_text(f"x = {n}\n")
        h = s.commit(f"iter {n}")
        s.record_iteration(
            run_id, n=n, git_hash=h, score=float(70 + n), verdict="keep",
            metrics=[{"name": "sharpe", "value": 1.0 + n, "dir": "higher", "weight": 1.0}],
        )
        s.update_run(run_id, best_score=float(70 + n), iter_count=n)

    last2 = s.last_iterations(run_id, 2)
    assert [r.n for r in last2] == [2, 3]          # oldest-first of the newest two
    assert last2[-1].score == 73.0
    assert last2[-1].metrics[0]["name"] == "sharpe"
    assert s.best_score(run_id) == 73.0


def test_reconcile_drops_phantom_kept_row(tmp_path):
    s = _new_store(tmp_path)
    run_id = s.create_run("asymmetric")
    # a legit kept iteration
    (s.artifact_dir / "code.py").write_text("ok\n")
    h = s.commit("real")
    s.record_iteration(run_id, n=1, git_hash=h, score=70.0, verdict="keep", metrics=[])
    # a phantom kept iteration whose commit never existed (simulated crash)
    s.record_iteration(run_id, n=2, git_hash="0" * 40, score=80.0, verdict="keep", metrics=[])

    removed = s.reconcile(run_id)
    assert removed == 1
    remaining = s.last_iterations(run_id, 10)
    assert [r.n for r in remaining] == [1]


def test_rewind_to_rolls_back_artifact_and_state(tmp_path):
    import pytest
    s = _new_store(tmp_path)
    run_id = s.create_run("asymmetric")
    hashes = {}
    for n in range(1, 5):                           # 4 kept iterations
        (s.artifact_dir / "code.py").write_text(f"x = {n}\n")
        h = s.commit(f"iter {n}")
        hashes[n] = h
        s.record_iteration(run_id, n=n, git_hash=h, score=float(70 + n), verdict="keep",
                           metrics=[{"name": "s", "value": float(n), "dir": "higher", "weight": 1.0}])
        s.update_run(run_id, best_score=float(70 + n), iter_count=n)
    # rewind to iteration 2
    assert s.rewind_to(run_id, 2) == hashes[2]
    assert s.head() == hashes[2]
    assert (s.artifact_dir / "code.py").read_text() == "x = 2\n"      # the tree at iter 2
    run = s.get_run(run_id)
    assert run["best_score"] == 72.0 and run["iter_count"] == 2
    assert [it.n for it in s.last_iterations(run_id, 100)] == [1, 2]  # 3 & 4 truncated
    assert s.commit_exists(hashes[4])               # dropped tail kept (tagged), recoverable
    with pytest.raises(ValueError):                 # a non-kept iteration is not restorable
        s.rewind_to(run_id, 99)
    s.close()


def test_create_run_starts_fresh(tmp_path):
    st = StateStore(tmp_path / "proj")
    rid = st.create_run(mode="symmetric")
    assert st.best_score(rid) is None


def test_champion_and_match_roundtrip(tmp_path):
    st = StateStore(tmp_path / "proj")
    cid_a = st.add_champion(run_id=1, side="A", generation=0, git_hash="aaa",
                            stable_score=0.4, repro={"pool_hash": "h", "referee_version": "1"})
    cid_b = st.add_champion(run_id=1, side="B", generation=0, git_hash="bbb", stable_score=0.3)
    st.record_match(run_id=1, a_champion_id=cid_a, b_champion_id=cid_b, a_score=0.7, seed=0)
    champs_a = st.champions(run_id=1, side="A")
    assert champs_a[0]["git_hash"] == "aaa" and champs_a[0]["generation"] == 0
    assert st.champion(cid_b)["side"] == "B"
    matrix = st.match_matrix(run_id=1)
    assert matrix[(cid_a, cid_b)] == 0.7


def test_record_match_idempotent_on_seed(tmp_path):
    st = StateStore(tmp_path / "proj")
    st.record_match(run_id=1, a_champion_id=1, b_champion_id=2, a_score=0.5, seed=0)
    st.record_match(run_id=1, a_champion_id=1, b_champion_id=2, a_score=0.9, seed=0)  # replace
    assert st.match_matrix(run_id=1)[(1, 2)] == 0.9


def test_export_tree_materializes_commit(tmp_path):
    st = _new_store(tmp_path)
    (st.artifact_dir / "recognizer.py").write_text("def accepts(s):\n    return 'ab' in s\n")
    h = st.commit("candidate 1")
    dest = tmp_path / "out"
    st.export_tree(h, dest)
    assert (dest / "recognizer.py").read_text() == "def accepts(s):\n    return 'ab' in s\n"


def test_max_gain_over_best(tmp_path):
    st = StateStore(tmp_path / "p")
    st.git_init()
    rid = st.create_run("asymmetric")
    st.record_iteration(rid, n=1, git_hash="a", score=63.18, verdict="keep", metrics=[])
    st.record_iteration(rid, n=2, git_hash=None, score=63.59, verdict="discard", metrics=[])  # near-miss
    st.record_iteration(rid, n=3, git_hash=None, score=40.0, verdict="discard", metrics=[])
    assert abs(st.max_gain_over_best(rid, 63.18) - 0.41) < 1e-6
    assert st.max_gain_over_best(rid, 99.0) == 0.0      # nothing beats 99
    assert st.max_gain_over_best(rid, None) == 0.0
