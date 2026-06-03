import sys

from tyani_tolkai.config import EvaluationCfg, MetricCfg
from tyani_tolkai.metrics import get_metric_adapter
from tyani_tolkai.sandbox import LocalBackend


def _eval(adapter, command, metrics, harness_dir=None):
    return EvaluationCfg(adapter=adapter, command=command, metrics=metrics, harness_dir=harness_dir)


def test_numeric_parses_json(tmp_path):
    cmd = f'{sys.executable} -c "import json; print(json.dumps({{\'sharpe\': 1.8, \'max_dd\': 0.12}}))"'
    ev = _eval("numeric", cmd, [
        MetricCfg(name="sharpe", dir="higher", weight=0.6, worst=0.0, target=2.5),
        MetricCfg(name="max_dd", dir="lower", weight=0.4, worst=0.5, target=0.05),
    ])
    res = get_metric_adapter("numeric").run(tmp_path, LocalBackend(), ev, timeout=10)
    assert res.ok
    by = {m["name"]: m["value"] for m in res.metrics}
    assert by["sharpe"] == 1.8 and by["max_dd"] == 0.12


def test_numeric_broken_artifact(tmp_path):
    cmd = f'{sys.executable} -c "raise SystemExit(2)"'
    ev = _eval("numeric", cmd, [MetricCfg(name="x", dir="higher", weight=1, worst=0, target=1)])
    res = get_metric_adapter("numeric").run(tmp_path, LocalBackend(), ev, timeout=10)
    assert not res.ok


def test_command_exit(tmp_path):
    ev0 = _eval("command-exit", f'{sys.executable} -c "raise SystemExit(0)"',
                [MetricCfg(name="passed", dir="higher", weight=1, worst=0, target=1)])
    r0 = get_metric_adapter("command-exit").run(tmp_path, LocalBackend(), ev0, timeout=10)
    assert r0.metrics[0]["value"] == 1.0

    ev1 = _eval("command-exit", f'{sys.executable} -c "raise SystemExit(1)"',
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
