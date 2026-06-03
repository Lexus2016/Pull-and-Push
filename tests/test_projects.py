import pytest

from tyani_tolkai.projects import (
    delete_project, export_project, import_project, list_projects,
    project_dir, rename_project, reset_project,
)
from tyani_tolkai.state import StateStore


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


def test_export_import_roundtrip(home, tmp_path):
    _make_project("p")
    tar = tmp_path / "p.tar.gz"
    export_project("p", tar)
    assert tar.exists()
    import_project(tar, "q")
    qd = project_dir("q")
    assert (qd / "state.db").exists()
    assert (qd / "artifact" / ".git").exists()
    assert (qd / "artifact" / "code.py").read_text() == "x=1\n"
