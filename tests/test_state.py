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
