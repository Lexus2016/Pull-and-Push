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
