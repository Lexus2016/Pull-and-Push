import yaml

from tyani_tolkai.state import StateStore
from tyani_tolkai.web import server


def _seed(name, *, min_delta, plateau_n, best, near_miss, plateau_count, iters):
    base = server.project_dir(name)
    base.mkdir(parents=True, exist_ok=True)
    cfg = {
        "project": name, "mode": "asymmetric",
        "agents": {"executor": {"engine": "claude"}},
        "roles": {"executor": {"goal": "g"}},
        "evaluation": {"adapter": "numeric", "command": "x", "min_delta": min_delta,
                       "target_score": 100,
                       "metrics": [{"name": "s", "dir": "higher", "weight": 1, "target": 100, "worst": 0}]},
        "limits": {"max_iterations": 100, "plateau_N": plateau_n},
    }
    (base / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    st = StateStore(base)
    st.git_init()
    rid = st.create_run("asymmetric")
    st.record_iteration(rid, n=1, git_hash="a", score=best, verdict="keep", metrics=[])
    if near_miss is not None:
        st.record_iteration(rid, n=2, git_hash=None, score=near_miss, verdict="discard", metrics=[])
    for k in range(iters):
        st.record_iteration(rid, n=3 + k, git_hash=None, score=10.0, verdict="discard", metrics=[])
    st.update_run(rid, best_score=best, plateau_count=plateau_count, iter_count=iters + 2)
    st.set_status(rid, "finished")
    st.close()


def test_plateau_hint_surfaces_near_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    # plateaued (count >= N), best 63.18, a discard at 63.59 (+0.41 < min_delta 0.5)
    _seed("stuck", min_delta=0.5, plateau_n=5, best=63.18, near_miss=63.59, plateau_count=5, iters=5)
    d = server._persisted_state("stuck")
    h = d["plateau_hint"]
    assert h is not None
    assert abs(h["gain"] - 0.41) < 1e-6
    assert h["min_delta"] == 0.5
    assert 0 < h["suggest_min_delta"] < 0.41        # below the gain → the near-miss would be kept
    assert h["plateau_n"] == 5 and h["plateau_count"] == 5


def test_no_plateau_hint_when_gain_exceeds_min_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    # the near-miss beat the best by MORE than min_delta → it should have been kept; not a min_delta issue
    _seed("notmd", min_delta=0.5, plateau_n=5, best=60.0, near_miss=61.0, plateau_count=5, iters=5)
    assert server._persisted_state("notmd")["plateau_hint"] is None


def test_no_plateau_hint_when_not_plateaued(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    _seed("ok", min_delta=0.5, plateau_n=20, best=63.18, near_miss=63.59, plateau_count=3, iters=3)
    assert server._persisted_state("ok")["plateau_hint"] is None
