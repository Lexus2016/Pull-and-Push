from tyani_tolkai.brief import build_brief, detect_oscillation
from tyani_tolkai.config import Config
from tyani_tolkai.state import StateStore


def _cfg(depth_k=6):
    return Config(
        project="p",
        agents={"executor": {"engine": "mock"}},
        roles={"executor": {"goal": "Improve the score", "task": "edit code.py"}},
        evaluation={
            "adapter": "numeric",
            "command": "true",
            "metrics": [{"name": "s", "dir": "higher", "weight": 1, "worst": 0, "target": 100}],
            "target_score": 100,
        },
        history={"depth_k": depth_k},
    )


def test_detect_oscillation():
    osc = ["+x = 0.03\n-x = 0.05", "+x = 0.05\n-x = 0.03"]   # adds then removes back
    assert detect_oscillation(osc) is True
    progress = ["+a = 1\n-a = 0", "+b = 2\n-b = 1"]
    assert detect_oscillation(progress) is False


def test_build_brief_first_iteration(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    brief = build_brief(s, run_id, _cfg())
    assert "ITERATION BRIEF (iteration 1)" in brief
    assert "Improve the score" in brief
    assert "first iteration" in brief
    assert "target: 100" in brief


def test_build_brief_with_history(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    for n, val, sc in [(1, 1, 70.0), (2, 2, 75.0)]:
        (s.artifact_dir / "code.py").write_text(f"x = {val}\n")
        h = s.commit(f"iter {n}")
        s.record_iteration(run_id, n=n, git_hash=h, score=sc, verdict="keep",
                           metrics=[{"name": "s", "value": sc, "dir": "higher", "weight": 1}])
        s.update_run(run_id, best_score=sc)
    brief = build_brief(s, run_id, _cfg())
    assert "code.py" in brief          # real diff surfaced
    assert "score=75.0" in brief
    assert "Oscillation flag:" in brief


def test_brief_empty_artifact_says_create_else_focused_change(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    # empty artifact → must instruct to CREATE the initial implementation
    b_empty = build_brief(s, run_id, _cfg())
    assert "EMPTY" in b_empty and "CREATE" in b_empty
    # once there is real content → switch to "one focused change"
    (s.artifact_dir / "code.py").write_text("x = 1\n")
    s.commit("seed code")
    assert "ONE focused change" in build_brief(s, run_id, _cfg())


def test_history_depth_respected(tmp_path):
    s = StateStore(tmp_path / "proj")
    s.git_init()
    run_id = s.create_run("asymmetric")
    for n in range(1, 4):
        (s.artifact_dir / "code.py").write_text(f"x = {n}\n")
        h = s.commit(f"iter {n}")
        s.record_iteration(run_id, n=n, git_hash=h, score=float(70 + n), verdict="keep", metrics=[])
    brief = build_brief(s, run_id, _cfg(depth_k=1))
    assert "#3" in brief and "#2" not in brief   # only the newest attempt shown


def _store_with_iters(tmp_path, count, tag):
    s = StateStore(tmp_path / f"proj_{tag}")
    s.git_init()
    run_id = s.create_run("asymmetric")
    for n in range(1, count + 1):
        (s.artifact_dir / "code.py").write_text(f"x = {n}\n")
        h = s.commit(f"iter {n}")
        s.record_iteration(run_id, n=n, git_hash=h, score=float(n), verdict="keep", metrics=[])
    return s, run_id


def test_fresh_look_executor_every_fifth_iteration(tmp_path):
    # SSoT-style "look from the other side" nudge fires on every FRESH_LOOK_EVERY-th iteration
    from tyani_tolkai.brief import FRESH_LOOK_EVERY
    # the NEXT brief is iteration count+1; count = N-1 → next iteration is the checkpoint
    s, rid = _store_with_iters(tmp_path, FRESH_LOOK_EVERY - 1, "on")
    b = build_brief(s, rid, _cfg())
    assert "FRESH-LOOK" in b and "OTHER SIDE" in b
    # one before → no nudge
    s2, rid2 = _store_with_iters(tmp_path, FRESH_LOOK_EVERY - 2, "off")
    assert "FRESH-LOOK" not in build_brief(s2, rid2, _cfg())


def test_fresh_look_validator_every_fifth_iteration():
    from tyani_tolkai.brief import build_validator_prompt, FRESH_LOOK_EVERY
    on = build_validator_prompt(_cfg(), "+x = 1", {"s": 1}, 1.0, "keep", iteration=FRESH_LOOK_EVERY)
    off = build_validator_prompt(_cfg(), "+x = 1", {"s": 1}, 1.0, "keep", iteration=FRESH_LOOK_EVERY - 1)
    assert "FRESH-LOOK" in on and "OTHER SIDE" in on
    assert "FRESH-LOOK" not in off
