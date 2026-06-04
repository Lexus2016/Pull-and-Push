"""The research + scaffold phase: vetted templates → runnable projects.

These tests prove the contract the user asked for — a project created from a description
runs immediately: its scoring harness is present, lives OUTSIDE the artifact (so the
executor can't tamper with it), is committed as the reset baseline, and actually scores.
"""

import pytest

from tyani_tolkai.config import load_config
from tyani_tolkai.metrics import get_metric_adapter
from tyani_tolkai.projects import project_dir
from tyani_tolkai.sandbox import get_backend
from tyani_tolkai.scorer import resolve_worst, score
from tyani_tolkai.state import StateStore
from tyani_tolkai import scaffold


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))
    return tmp_path


# ---------------- registry & picker (no project creation) ----------------

def test_list_templates_has_both_with_required_keys():
    ids = {t["id"] for t in scaffold.list_templates()}
    assert {"btcusdt-futures", "pytest-pass"} <= ids
    for t in scaffold.list_templates():
        assert t["name"] and isinstance(t["keywords"], list) and t["summary"]


def test_pick_template_matches_by_keywords():
    assert scaffold.pick_template("a leveraged btcusdt futures trading bot") == "btcusdt-futures"
    assert scaffold.pick_template("implement a python function to pass the pytest tests") == "pytest-pass"


def test_pick_template_none_on_no_match():
    assert scaffold.pick_template("xyzzy foobar qux") is None
    assert scaffold.pick_template("") is None


def test_load_template_rejects_traversal_and_missing():
    with pytest.raises(ValueError):
        scaffold.load_template("../etc")
    with pytest.raises(FileNotFoundError):
        scaffold.load_template("does-not-exist")


# ---------------- scaffold_project: structure & validity ----------------

def test_scaffold_creates_valid_runnable_project(home):
    res = scaffold.scaffold_project("p-btc", "tune a btcusdt futures strategy", "btcusdt-futures")
    assert res == {"created": "p-btc", "template": "btcusdt-futures", "harness": "metrics"}
    base = project_dir("p-btc")

    # config.yaml is valid and carries the description as the executor goal
    cfg = load_config(base / "config.yaml")
    assert cfg.project == "p-btc"
    assert cfg.description == "tune a btcusdt futures strategy"
    assert cfg.roles["executor"].goal == "tune a btcusdt futures strategy"

    # seed (editable surface) is inside the artifact; harness is OUTSIDE it (anti-collusion)
    assert (base / "artifact" / "strategy.py").exists()
    assert not (base / "artifact" / "metrics").exists()
    assert (base / "metrics" / "run_backtest.py").exists()
    assert (base / "metrics" / "data.csv").exists()


def test_scaffold_commits_seed_as_reset_baseline(home):
    scaffold.scaffold_project("p-commit", "make the hidden pytest tests pass", "pytest-pass")
    base = project_dir("p-commit")
    state = StateStore(base)
    try:
        # the seed is the first (and only) commit — git_init committed it
        head = state._git("rev-parse", "HEAD").strip()
        assert head
        tracked = state._git("ls-files").split()
        assert "solution.py" in tracked
    finally:
        state.close()


def test_scaffold_infers_template_when_none(home):
    res = scaffold.scaffold_project("p-auto", "implement python functions to pass pytest", None)
    assert res["template"] == "pytest-pass"


def test_scaffold_rejects_duplicate(home):
    scaffold.scaffold_project("dup", "btcusdt futures strategy", "btcusdt-futures")
    with pytest.raises(FileExistsError):
        scaffold.scaffold_project("dup", "btcusdt futures strategy", "btcusdt-futures")


def test_scaffold_requires_description(home):
    with pytest.raises(ValueError):
        scaffold.scaffold_project("nodesc", "   ", "pytest-pass")


def test_scaffold_uninferrable_description_raises(home):
    with pytest.raises(ValueError):
        scaffold.scaffold_project("noinfer", "xyzzy foobar", None)


# ---------------- the created project actually scores ----------------

def _eval(name):
    base = project_dir(name)
    cfg = load_config(base / "config.yaml")
    state = StateStore(base)
    try:
        sb = get_backend(cfg.sandbox.backend, cfg.sandbox)
        res = get_metric_adapter(cfg.evaluation.adapter).run(
            state.artifact_dir, sb, cfg.evaluation, cfg.limits.step_seconds)
        assert res.ok, f"evaluation failed: {(res.logs or '')[:400]}"
        vals = {m["name"]: m["value"] for m in res.metrics}
        for m in cfg.evaluation.metrics:
            if m.worst is None and m.name in vals:
                m.worst = resolve_worst(m.dir, m.target, vals[m.name])
        return vals, score(vals, cfg.evaluation.metrics)
    finally:
        state.close()


def test_pytest_template_scores_zero_at_seed(home):
    # the seed stub raises NotImplementedError → every hidden test fails → pass_pct 0
    scaffold.scaffold_project("score-py", "make the hidden pytest tests pass", "pytest-pass")
    vals, sc = _eval("score-py")
    assert vals["pass_pct"] == 0.0
    assert sc == 0.0


def test_btc_template_scores_on_real_harness(home):
    # the committed backtest runs against real data and produces a finite composite
    scaffold.scaffold_project("score-btc", "btcusdt futures strategy", "btcusdt-futures")
    vals, sc = _eval("score-btc")
    assert set(vals) == {"total_return_pct", "liquidations", "max_drawdown_pct", "max_drawdown_days"}
    assert 0.0 <= sc <= 100.0


def test_btc_harness_reports_extra_stats(home):
    # the operator-requested report-only fields (win rate, profit factor, trades, tested period)
    # are emitted by the harness and surfaced via the adapter's parsed `data`, beyond the 4 scored
    scaffold.scaffold_project("rep-btc", "btcusdt futures strategy", "btcusdt-futures")
    base = project_dir("rep-btc")
    cfg = load_config(base / "config.yaml")
    state = StateStore(base)
    try:
        sb = get_backend(cfg.sandbox.backend, cfg.sandbox)
        res = get_metric_adapter(cfg.evaluation.adapter).run(
            state.artifact_dir, sb, cfg.evaluation, cfg.limits.step_seconds)
        assert res.ok
        for k in ("win_rate_pct", "profit_factor", "num_trades", "period_start", "period_end"):
            assert k in res.data, f"harness did not report {k!r}"
        assert isinstance(res.data["num_trades"], int)
        assert res.data["period_start"] and res.data["period_end"]
    finally:
        state.close()
