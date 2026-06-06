import sys

from tyani_tolkai.config import EvaluationCfg, MetricCfg
from tyani_tolkai.metrics import get_metric_adapter
from tyani_tolkai.sandbox import LocalBackend

PY = sys.executable.replace("\\", "/")   # forward-slashed so it survives shlex(posix) on Windows


def _eval(adapter, command, metrics, harness_dir=None):
    return EvaluationCfg(adapter=adapter, command=command, metrics=metrics, harness_dir=harness_dir)


def test_numeric_parses_json(tmp_path):
    cmd = f'{PY} -c "import json; print(json.dumps({{\'sharpe\': 1.8, \'max_dd\': 0.12}}))"'
    ev = _eval("numeric", cmd, [
        MetricCfg(name="sharpe", dir="higher", weight=0.6, worst=0.0, target=2.5),
        MetricCfg(name="max_dd", dir="lower", weight=0.4, worst=0.5, target=0.05),
    ])
    res = get_metric_adapter("numeric").run(tmp_path, LocalBackend(), ev, timeout=10)
    assert res.ok
    by = {m["name"]: m["value"] for m in res.metrics}
    assert by["sharpe"] == 1.8 and by["max_dd"] == 0.12


def test_numeric_resolves_python_placeholder(tmp_path):
    # {python} must resolve to the sandbox interpreter (sys.executable) so templates run on systems
    # where a bare `python` is not on PATH (only python3) — the portability fix.
    cmd = '{python} -c "import json; print(json.dumps({\'s\': 1.0}))"'
    ev = _eval("numeric", cmd, [MetricCfg(name="s", dir="higher", weight=1, worst=0, target=1)])
    res = get_metric_adapter("numeric").run(tmp_path, LocalBackend(), ev, timeout=10)
    assert res.ok and res.metrics[0]["value"] == 1.0


def test_numeric_broken_artifact(tmp_path):
    cmd = f'{PY} -c "raise SystemExit(2)"'
    ev = _eval("numeric", cmd, [MetricCfg(name="x", dir="higher", weight=1, worst=0, target=1)])
    res = get_metric_adapter("numeric").run(tmp_path, LocalBackend(), ev, timeout=10)
    assert not res.ok


def test_numeric_data_carries_report_fields_and_survives_stderr(tmp_path):
    # Harness prints its JSON to stdout AND a warning to stderr. Both the scored metric and the
    # report-only fields must come through: `data` is parsed from stdout, never stdout+stderr.
    h = tmp_path / "h.py"
    h.write_text("import sys, json\n"
                 "sys.stderr.write('DeprecationWarning: noise\\n')\n"
                 "print(json.dumps({'r': 80.0, 'win_rate_pct': 57.1, 'num_trades': 7}))\n")
    ev = _eval("numeric", f'{PY} {h.as_posix()}',
               [MetricCfg(name="r", dir="higher", weight=1, worst=0, target=100)])
    res = get_metric_adapter("numeric").run(tmp_path, LocalBackend(), ev, timeout=10)
    assert res.ok
    assert {m["name"]: m["value"] for m in res.metrics} == {"r": 80.0}
    assert res.data.get("win_rate_pct") == 57.1 and res.data.get("num_trades") == 7
    assert "DeprecationWarning" in res.logs        # stderr still captured for display


def test_command_exit(tmp_path):
    ev0 = _eval("command-exit", f'{PY} -c "raise SystemExit(0)"',
                [MetricCfg(name="passed", dir="higher", weight=1, worst=0, target=1)])
    r0 = get_metric_adapter("command-exit").run(tmp_path, LocalBackend(), ev0, timeout=10)
    assert r0.metrics[0]["value"] == 1.0

    ev1 = _eval("command-exit", f'{PY} -c "raise SystemExit(1)"',
                [MetricCfg(name="passed", dir="higher", weight=1, worst=0, target=1)])
    r1 = get_metric_adapter("command-exit").run(tmp_path, LocalBackend(), ev1, timeout=10)
    assert r1.metrics[0]["value"] == 0.0


def test_pytest_pass_counts(tmp_path):
    project = tmp_path / "proj"
    (project / "artifact").mkdir(parents=True)
    (project / "metrics").mkdir(parents=True)
    (project / "metrics" / "test_sample.py").write_text(
        "def test_a():\n    assert True\n"
        "def test_b():\n    assert True\n"
        "def test_c():\n    assert False\n"
    )
    ev = _eval(
        "pytest-pass", None,
        [MetricCfg(name="pass_pct", dir="higher", weight=1, worst=0, target=100)],
        harness_dir="metrics",
    )
    res = get_metric_adapter("pytest-pass").run(project / "artifact", LocalBackend(), ev, timeout=60)
    assert res.ok
    assert res.metrics[0]["value"] == round(2 / 3 * 100, 6) or abs(res.metrics[0]["value"] - 66.667) < 0.01
