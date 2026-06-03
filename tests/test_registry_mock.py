import pytest

from tyani_tolkai.agents import MockAdapter
from tyani_tolkai.agents.cli_agent import CLIAgentAdapter
from tyani_tolkai.registry import AdapterRegistry, build_adapter


def _write_x(val):
    def edit(workdir):
        (workdir / "code.py").write_text(f"x = {val}\n")
        return True
    return edit


def test_mock_applies_edits_in_order(tmp_path):
    m = MockAdapter([_write_x(1), _write_x(2)])
    r1 = m.run("brief", tmp_path, "writeable", 10)
    assert r1.status == "success" and r1.changed
    assert (tmp_path / "code.py").read_text() == "x = 1\n"
    r2 = m.run("brief", tmp_path, "writeable", 10)
    assert (tmp_path / "code.py").read_text() == "x = 2\n"


def test_mock_exhausted_is_noop(tmp_path):
    m = MockAdapter([_write_x(1)])
    m.run("brief", tmp_path, "writeable", 10)
    r = m.run("brief", tmp_path, "writeable", 10)
    assert r.status == "no_op" and not r.changed


def test_registry_get_and_errors():
    reg = AdapterRegistry()
    mock = MockAdapter([])
    reg.register("mock", mock)
    assert reg.get("mock") is mock
    with pytest.raises(KeyError):
        reg.get("nope")


def test_build_adapter_real_engines():
    assert isinstance(build_adapter("claude", "opus", "writeable"), CLIAgentAdapter)
    assert isinstance(build_adapter("codex", None, "read-only"), CLIAgentAdapter)
    with pytest.raises(KeyError):
        build_adapter("nope", None, "writeable")
