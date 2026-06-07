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


def test_fresh_look_rotates_lens_and_is_deterministic(tmp_path):
    # The nudge must NOT repeat one fixed text: different checkpoints draw different semantic lenses,
    # yet the choice is deterministic (reproducible brief) — seeded by checkpoint ordinal + run salt.
    from tyani_tolkai.brief import _LENSES_EXECUTOR, FRESH_LOOK_EVERY
    s1, r1 = _store_with_iters(tmp_path, FRESH_LOOK_EVERY - 1, "cp1")        # next iter 5  → checkpoint 1
    s2, r2 = _store_with_iters(tmp_path, 2 * FRESH_LOOK_EVERY - 1, "cp2")    # next iter 10 → checkpoint 2
    b1, b2 = build_brief(s1, r1, _cfg()), build_brief(s2, r2, _cfg())
    assert _LENSES_EXECUTOR[(r1 + 1) % len(_LENSES_EXECUTOR)] in b1          # salt=run_id, cp=1
    assert _LENSES_EXECUTOR[(r2 + 2) % len(_LENSES_EXECUTOR)] in b2          # salt=run_id, cp=2
    assert b1 != b2                                                          # rotation actually varied it
    assert build_brief(s1, r1, _cfg()) == b1                                 # same state in → same brief out


def test_fresh_look_grounds_in_abandoned_attempt(tmp_path):
    # When history holds a REJECTED attempt, the checkpoint re-opens that concrete abandoned direction.
    from tyani_tolkai.brief import FRESH_LOOK_EVERY
    s = StateStore(tmp_path / "grounded")
    s.git_init()
    rid = s.create_run("asymmetric")
    for n in range(1, FRESH_LOOK_EVERY):                                     # iters 1..4; next is checkpoint 5
        (s.artifact_dir / "code.py").write_text(f"x = {n}\n")
        h = s.commit(f"iter {n}")
        s.record_iteration(rid, n=n, git_hash=h, score=float(n),
                           verdict=("discard" if n == 2 else "keep"), metrics=[])
    b = build_brief(s, rid, _cfg())
    assert "FRESH-LOOK" in b
    assert "abandoned attempt #2" in b


def test_direction_diversity():
    from tyani_tolkai.brief import direction_diversity
    assert direction_diversity([]) == 1.0
    assert direction_diversity(["+a = 1"]) == 1.0
    assert direction_diversity(["+a = 1", "+b = 2", "+c = 3"]) == 1.0       # all distinct line-sets
    assert direction_diversity(["+a = 1", "+a = 1", "+a = 1"]) == 1 / 3     # same set repeated → low
