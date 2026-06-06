import pytest

from tyani_tolkai.projects import (
    delete_project, export_project, import_project, list_projects,
    project_dir, rename_project, reset_project, valid_name,
)
from tyani_tolkai.state import StateStore


def test_valid_name_rejects_traversal():
    for bad in ["../evil", "a/b", "..", "", ".hidden/y", "a\\b", "/abs"]:
        with pytest.raises(ValueError):
            valid_name(bad)
    assert valid_name("ok-name_1.2") == "ok-name_1.2"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    return tmp_path


def _make_project(name="p"):
    d = project_dir(name)
    st = StateStore(d)
    st.git_init()
    (d / "config.yaml").write_text(f"project: {name}\n")
    (st.artifact_dir / "code.py").write_text("x=1\n")
    h = st.commit("change")
    rid = st.create_run("asymmetric")
    st.record_iteration(rid, n=1, git_hash=h, score=70.0, verdict="keep", metrics=[])
    st.update_run(rid, best_score=70.0)
    st.close()
    return d


def test_list_and_delete(home):
    _make_project("p")
    assert "p" in list_projects()
    delete_project("p")
    assert "p" not in list_projects()


def test_rename(home):
    _make_project("p")
    rename_project("p", "q")
    assert "q" in list_projects() and "p" not in list_projects()


def test_reset_clears_history(home):
    _make_project("p")
    reset_project("p")
    st = StateStore(project_dir("p"))
    rows = st.conn.execute("SELECT COUNT(*) AS c FROM iteration").fetchone()["c"]
    st.close()
    assert rows == 0


def test_export_includes_docs_and_code(home, tmp_path):
    import zipfile
    _make_project("p")
    zp = tmp_path / "p.zip"
    export_project("p", zp)
    names = zipfile.ZipFile(zp).namelist()
    assert any(n.endswith("/README.md") for n in names)        # how-to-run doc
    assert any(n.endswith("/RESULTS.md") for n in names)        # metrics report
    assert any(n.endswith("/artifact/code.py") for n in names)  # the actual result code
    assert not any("__pycache__" in n for n in names)           # no junk


def test_export_import_roundtrip(home, tmp_path):
    _make_project("p")
    zp = tmp_path / "p.zip"
    export_project("p", zp)
    assert zp.exists()
    import_project(zp, "q")
    qd = project_dir("q")
    assert (qd / "state.db").exists()
    assert (qd / "artifact" / ".git").exists()
    assert (qd / "artifact" / "code.py").read_text() == "x=1\n"


def _make_multi(name="p", iters=3):
    d = project_dir(name)
    st = StateStore(d)
    st.git_init()
    (d / "config.yaml").write_text(f"project: {name}\n")
    rid = st.create_run("asymmetric")
    hashes = {}
    for n in range(1, iters + 1):
        # write_bytes (not write_text) → exact LF on every OS; Windows text mode would emit CRLF
        (st.artifact_dir / "code.py").write_bytes(f"v{n}\n".encode())
        h = st.commit(f"iter {n}")
        hashes[n] = h
        st.record_iteration(rid, n=n, git_hash=h, score=float(70 + n), verdict="keep", metrics=[])
        st.update_run(rid, best_score=float(70 + n), iter_count=n)
    st.close()
    return d, hashes


def test_export_at_hash_snapshots_that_iteration(home, tmp_path):
    import zipfile
    _, hashes = _make_multi("p", 3)
    dest = tmp_path / "snap.zip"
    export_project("p", dest, at_hash=hashes[1], at_label=1)
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
        code = next(n for n in names if n.endswith("artifact/code.py"))
        assert z.read(code).decode() == "v1\n"            # iteration 1's tree, not the latest v3
        snap = z.read(next(n for n in names if n.endswith("SNAPSHOT.txt"))).decode()
        assert "iteration 1" in snap and "71.0" in snap   # THIS iteration's own score (not best 73)
        assert "overall BEST" in snap                     # disambiguation vs README/RESULTS


def test_fork_project_positions_copy_and_leaves_original(home):
    from tyani_tolkai.projects import fork_project
    _, hashes = _make_multi("orig", 3)
    fork_project("orig", "fork1", 1)
    assert "fork1" in list_projects()
    fst = StateStore(project_dir("fork1"))
    try:
        assert fst.head() == hashes[1]
        assert (fst.artifact_dir / "code.py").read_text() == "v1\n"
    finally:
        fst.close()
    ost = StateStore(project_dir("orig"))                 # original untouched (still at iter 3)
    try:
        assert ost.head() == hashes[3]
        assert (ost.artifact_dir / "code.py").read_text() == "v3\n"
    finally:
        ost.close()


def test_fork_with_bad_iteration_leaves_no_orphan(home):
    import pytest
    from tyani_tolkai.projects import fork_project
    _make_multi("orig2", 2)
    with pytest.raises(ValueError):
        fork_project("orig2", "fork_bad", 99)      # 99 is not a kept iteration
    assert "fork_bad" not in list_projects()        # the half-copy must be cleaned up
