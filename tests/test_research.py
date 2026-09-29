"""P8 research kits: spec → pre-flight → project → run → status/report → next kit; hard constraints;
the wizard's helper agent; the web + CLI surfaces."""

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tyani_tolkai import research as rs
from tyani_tolkai import research_agent as ra
from tyani_tolkai.agents import MockAdapter
from tyani_tolkai.cli import main as cli_main
from tyani_tolkai.config import Config, ConstraintCfg, load_config
from tyani_tolkai.metrics.numeric import NumericAdapter
from tyani_tolkai.orchestrator import Orchestrator
from tyani_tolkai.projects import project_dir
from tyani_tolkai.sandbox import LocalBackend
from tyani_tolkai.state import StateStore
from tyani_tolkai.web.server import create_app


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TYANI_TOLKAI_HOME", str(tmp_path / "home"))


_VAL_SCORER = ("import json, pathlib\n"
               "d = json.loads(pathlib.Path('val.json').read_text())\n"
               "print(json.dumps({'speed': d['speed'], 'correct': d['correct'], 'note': 'x'}))\n")


def _kit(tmp_path, name="speedy", code=_VAL_SCORER, seed=None, **spec_over):
    kit = tmp_path / f"kit-{name}"
    (kit / "seed").mkdir(parents=True)
    (kit / "scorer").mkdir()
    (kit / "seed" / "val.json").write_text(json.dumps(seed or {"speed": 1, "correct": 1}))
    (kit / "scorer" / "evaluate.py").write_text(code)
    spec = {"name": name, "goal": "go fast", "task": "edit val.json",
            "metrics": [{"name": "speed", "dir": "higher", "target": 10, "weight": 1}],
            "constraints": [{"name": "correct", "min": 1}],
            "agents": {"executor": "claude", "validator": "codex"},
            "limits": {"max_iterations": 5, "plateau_N": 3, "budget_usd": 1, "usd_per_mtok": 3}}
    spec.update(spec_over)
    (kit / "research.yaml").write_text(yaml.safe_dump(spec))
    return kit


def _codes(rep, level=None):
    return [i["code"] for i in rep["items"] if level is None or i["level"] == level]


# ---- spec ----

def test_spec_shorthand_and_config_roundtrip(tmp_path):
    spec = rs.load_kit(_kit(tmp_path))
    cfg = rs.spec_to_config(spec)
    assert cfg["agents"]["executor"]["engine"] == "claude"
    assert cfg["evaluation"]["constraints"] == [{"name": "correct", "min": 1.0}]
    assert cfg["roles"]["validator"]["goal"]                          # default reviewer brief
    Config(**cfg)
    back = rs.config_to_spec(cfg)
    assert back["name"] == "speedy" and back["constraints"][0]["name"] == "correct"
    assert rs.spec_to_config(rs.ResearchSpec.model_validate(back)) == cfg


def test_constraint_needs_a_bound():
    with pytest.raises(ValueError):
        ConstraintCfg(name="x")


# ---- pre-flight ----

def test_new_kit_skeleton_passes_the_preflight(tmp_path):
    kit = rs.new_kit(tmp_path / "k", name="tagline")
    rep = rs.check_kit(kit)
    assert rep["ok"], rs.format_check(rep)
    assert rep["values"]["score"] >= 0 and rep["eval_seconds"] is not None


def test_preflight_flags_a_metric_the_scorer_does_not_print(tmp_path):
    kit = _kit(tmp_path, metrics=[{"name": "latency", "dir": "lower", "target": 1}])
    rep = rs.check_kit(kit)
    assert not rep["ok"] and "metric-missing" in _codes(rep, "error")


def test_preflight_flags_a_crashing_scorer(tmp_path):
    rep = rs.check_kit(_kit(tmp_path, code="raise SystemExit(1)\n"))
    assert not rep["ok"] and "scorer-failed" in _codes(rep, "error")


def test_preflight_flags_a_wrong_scorer_path(tmp_path):
    # the judge file is evaluate.py, but the command runs nope.py
    rep = rs.check_kit(_kit(tmp_path, scorer="{python} ../metrics/nope.py"))
    assert not rep["ok"] and "scorer-path" in _codes(rep, "error")


def test_preflight_flags_a_noisy_scorer(tmp_path):
    noisy = ("import json, random\nprint(json.dumps({'speed': random.random(), 'correct': 1}))\n")
    rep = rs.check_kit(_kit(tmp_path, code=noisy))
    assert "noisy" in _codes(rep, "warn")
    # a noisy field that is not scored (a timing constraint, a report-only number) is only info
    timing = ("import json, random\nprint(json.dumps({'speed': 1, 'correct': 1, "
              "'secs': random.random()}))\n")
    rep = rs.check_kit(_kit(tmp_path / "t", code=timing))
    assert "noisy" not in _codes(rep, "warn") and "noisy-unscored" in _codes(rep, "info")


def test_preflight_refuses_a_seed_that_already_meets_every_target(tmp_path):
    rep = rs.check_kit(_kit(tmp_path, seed={"speed": 50, "correct": 1}))
    assert not rep["ok"] and "already-done" in _codes(rep, "error")


def test_preflight_notes_a_seed_breaking_a_constraint(tmp_path):
    rep = rs.check_kit(_kit(tmp_path, seed={"speed": 1, "correct": 0}))
    assert rep["ok"] and "seed-constraints" in _codes(rep, "info")


def test_preflight_warns_about_same_provider_and_no_budget(tmp_path):
    rep = rs.check_kit(_kit(tmp_path, agents={"executor": "claude", "validator": "claude"},
                            limits={"max_iterations": 3}))
    assert {"collusion", "budget"} <= set(_codes(rep, "warn"))


# ---- create / save ----

def test_create_lays_out_the_project_and_refuses_duplicates(tmp_path):
    out = rs.create_from_kit(_kit(tmp_path))
    base = project_dir(out["created"])
    assert (base / "artifact" / "val.json").exists() and (base / "metrics" / "evaluate.py").exists()
    assert (base / "research" / "research.yaml").exists()            # provenance
    cfg = load_config(base / "config.yaml")
    assert cfg.evaluation.constraints[0].name == "correct"
    with pytest.raises(FileExistsError):
        rs.create_from_kit(_kit(tmp_path, name="speedy2"), name="speedy")


def test_create_refuses_a_kit_that_fails_the_preflight(tmp_path):
    with pytest.raises(ValueError, match="pre-flight failed"):
        rs.create_from_kit(_kit(tmp_path, code="raise SystemExit(1)\n"))
    assert not (project_dir("speedy") / "config.yaml").exists()


def _run(name, edits, max_iter=5):
    base = project_dir(name)
    cfg = load_config(base / "config.yaml")
    cfg.limits.max_iterations = max_iter
    st = StateStore(base)
    rid = st.create_run("asymmetric")
    orch = Orchestrator(cfg, st, rid, MockAdapter(edits), NumericAdapter(), LocalBackend())
    summary = orch.run_loop()
    st.close()
    return summary


def _set(speed, correct):
    def e(workdir: Path) -> bool:
        (workdir / "val.json").write_text(json.dumps({"speed": speed, "correct": correct}))
        return True
    return e


def test_a_candidate_breaking_a_constraint_is_never_kept(tmp_path):
    rs.create_from_kit(_kit(tmp_path))
    _run("speedy", [_set(2, 1), _set(9, 0), _set(4, 1)], max_iter=3)
    st = rs.project_status("speedy", last=3)
    verdicts = [(it["n"], it["verdict"]) for it in st["last"]]
    assert verdicts == [(1, "keep"), (2, "fail"), (3, "keep")]
    assert "constraint violated" in st["last"][1]["note"]
    head = json.loads((project_dir("speedy") / "artifact" / "val.json").read_text())
    assert head == {"speed": 4, "correct": 1}                  # the fast-but-wrong version is gone


def test_status_report_and_save_for_the_next_round(tmp_path):
    rs.create_from_kit(_kit(tmp_path))
    _run("speedy", [_set(3, 1), _set(6, 1)], max_iter=2)
    s = rs.project_status("speedy")
    assert s["status"] == "finished" and s["best_iteration"] == 2 and s["iterations"] == 2
    r = rs.project_report("speedy")
    speed = next(m for m in r["metrics"] if m["name"] == "speed")
    assert (speed["start"], speed["best"]) == (3, 6) and [k["n"] for k in r["kept"]] == [1, 2]
    assert "val.json" in r["diff"]
    best_kit = rs.save_kit("speedy", tmp_path / "next", source="best", name="speedy-2")
    assert json.loads((best_kit / "seed" / "val.json").read_text())["speed"] == 6
    assert rs.load_kit(best_kit).name == "speedy-2" and (best_kit / "scorer" / "evaluate.py").exists()
    seed_kit = rs.save_kit("speedy", tmp_path / "again", source="seed")
    assert json.loads((seed_kit / "seed" / "val.json").read_text())["speed"] == 1


# ---- the wizard's helper agent ----

_CLARIFY_OUT = json.dumps({"summary": "s", "artifact": "val.json",
                           "metrics": [{"name": "speed", "dir": "higher", "target": 10,
                                        "weight": 1, "how": "read it"}],
                           "questions": ["How fast is fast?"]})


def _draft_out(files=None):
    return "Here you go:\n```json\n" + json.dumps({
        "research": {"name": "ignored", "goal": "go fast", "task": "edit val.json",
                     "metrics": [{"name": "speed", "dir": "higher", "weight": 1, "target": 10}],
                     "constraints": [{"name": "correct", "min": 1}], "target_score": 90},
        "files": files or {"seed/val.json": '{"speed": 1, "correct": 1}',
                           "scorer/evaluate.py": _VAL_SCORER}}) + "\n```"


def test_clarify_and_draft_produce_a_checkable_kit(tmp_path):
    c = ra.clarify("make it fast", runner=lambda p: _CLARIFY_OUT)
    assert c["questions"] == ["How fast is fast?"] and c["constraints"] == []
    d = ra.draft_kit("wiz", "make it fast", criteria=c, answers="Q: x\nA: y",
                     runner=lambda p: _draft_out())
    assert d["research"]["name"] == "wiz" and "scorer/evaluate.py" in d["files"]
    kit = ra.write_kit(tmp_path / "wiz", d["research_yaml"], d["files"])
    assert rs.check_kit(kit)["ok"]
    back = ra.read_kit(kit)
    assert back["files"]["seed/val.json"] and "go fast" in back["research_yaml"]


@pytest.mark.parametrize("files", [{"../evil.py": "x", "scorer/evaluate.py": "x"},
                                   {"/etc/x": "x", "scorer/evaluate.py": "x"},
                                   {"seed/a.py": "x"},                       # no judge
                                   {"notes.txt": "x", "scorer/evaluate.py": "x"}])
def test_draft_rejects_unsafe_or_judgeless_files(files):
    with pytest.raises(ValueError):
        ra.draft_kit("wiz", "idea", runner=lambda p: _draft_out(files))


def test_write_kit_drops_stale_text_files_but_keeps_binary_data(tmp_path):
    # a second draft under the same name must not inherit the first draft's files (they would
    # land in the project's seed/ or next to the judge); binary data the editor never shows stays
    d1 = ra.draft_kit("wiz", "idea", runner=lambda p: _draft_out(
        {"seed/old.py": "x = 1", "seed/val.json": '{"speed": 1, "correct": 1}',
         "scorer/evaluate.py": _VAL_SCORER, "scorer/old_ref.txt": "stale"}))
    kit = ra.write_kit(tmp_path / "wiz", d1["research_yaml"], d1["files"])
    (kit / "scorer" / "ref.bin").write_bytes(b"\xff\xfe\x00binary")
    d2 = ra.draft_kit("wiz", "idea", runner=lambda p: _draft_out())
    ra.write_kit(kit, d2["research_yaml"], d2["files"])
    assert not (kit / "seed" / "old.py").exists() and not (kit / "scorer" / "old_ref.txt").exists()
    assert (kit / "scorer" / "ref.bin").exists() and (kit / "seed" / "val.json").exists()
    assert sorted(ra.read_kit(kit)["files"]) == ["scorer/evaluate.py", "seed/val.json"]


def test_helper_that_gives_no_answer_is_reported_as_such(monkeypatch):
    # a timed-out helper used to surface as "no JSON object in configurator output"
    from tyani_tolkai import profiler
    from tyani_tolkai.agents.base import RunResult

    class Slow:
        def run(self, *a):
            return RunResult(status="timeout", stdout="")
    monkeypatch.setattr("tyani_tolkai.registry.build_adapter", lambda *a: Slow())
    with pytest.raises(profiler.HelperAgentError, match="within 7s"):
        profiler.default_runner("claude", None, 7)("prompt")
    monkeypatch.setattr(ra, "_runner", lambda *a, **k: profiler.default_runner("claude", None, 7))
    r = TestClient(create_app(token=None)).post("/api/research/draft",
                                               json={"name": "wiz", "idea": "x"})
    assert r.status_code == 502 and "timed out" in r.json()["detail"]


def test_prompts_treat_the_idea_as_data():
    assert "inert data" in ra.CLARIFY_PROMPT and "inert data" in ra.DRAFT_PROMPT


# ---- web ----

def test_web_wizard_endpoints(tmp_path, monkeypatch):
    outs = {"clarify": _CLARIFY_OUT, "draft": _draft_out()}
    monkeypatch.setattr(ra, "_runner", lambda *a, **k: (lambda p: outs["clarify"]
                                                         if "ONE JSON object and nothing else:\n{\n  \"summary\"" in p
                                                         else outs["draft"]))
    c = TestClient(create_app(token=None))
    q = c.post("/api/research/clarify", json={"idea": "make it fast"}).json()
    assert q["questions"] == ["How fast is fast?"]
    d = c.post("/api/research/draft", json={"name": "wiz", "idea": "make it fast", "criteria": q}).json()
    kit = {"name": "wiz", "research_yaml": d["research_yaml"], "files": d["files"]}
    rep = c.post("/api/research/check", json=kit).json()
    assert rep["ok"], rep
    assert c.post("/api/research/create", json=kit).json()["created"] == "wiz"
    assert c.post("/api/research/create", json=kit).status_code == 409
    assert [k["name"] for k in c.get("/api/research/kits").json()["kits"]] == ["wiz"]
    assert "scorer/evaluate.py" in c.get("/api/research/kits/wiz").json()["files"]
    bad = dict(kit, files={"../x": "y"})
    assert c.post("/api/research/check", json=bad).status_code == 422
    assert c.get("/api/research/kits/..%2Fx").status_code in (400, 404)


def test_test_eval_reports_constraint_violations(tmp_path):
    rs.create_from_kit(_kit(tmp_path, seed={"speed": 1, "correct": 0}))
    c = TestClient(create_app(token=None))
    r = c.post("/api/projects/speedy/test-eval").json()
    assert r["ok"] and r["violations"] and "correct" in r["violations"][0]


# ---- CLI ----

def test_cli_research_flow(tmp_path, capsys):
    kit = tmp_path / "k"
    assert cli_main(["research", "new", str(kit), "--name", "cli-demo"]) == 0
    assert cli_main(["research", "check", str(kit)]) == 0
    capsys.readouterr()
    assert cli_main(["research", "check", str(kit), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli_main(["research", "create", str(kit)]) == 0
    capsys.readouterr()
    assert cli_main(["status", "cli-demo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "new"
    assert cli_main(["report", "cli-demo"]) == 0
    assert cli_main(["research", "check", str(tmp_path / "missing")]) == 3
    assert cli_main(["research", "create", str(kit)]) == 2               # already exists
    assert cli_main(["research", "save", "cli-demo"]) == 2               # --to is required
    assert cli_main(["research", "save", "cli-demo", "--to", str(tmp_path / "k2")]) == 0
    assert (tmp_path / "k2" / "research.yaml").exists()


def test_cli_start_prefers_the_dashboard_then_runs_in_the_foreground(tmp_path, monkeypatch):
    import tyani_tolkai.cli as cli
    rs.create_from_kit(_kit(tmp_path))
    calls = []
    monkeypatch.setattr(rs, "start_via_dashboard",
                        lambda name, url, token=None: calls.append(("web", name)) or {"started": name})
    assert cli_main(["research", "start", "speedy"]) == 0 and calls == [("web", "speedy")]

    def refused(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(rs, "start_via_dashboard", refused)
    monkeypatch.setattr(cli, "cmd_run", lambda args: calls.append(("fg", args.resume)) or 0)
    assert cli_main(["research", "start", "speedy"]) == 0
    assert calls[-1] == ("fg", True)                   # continue semantics, like the dashboard's Run


def test_the_shipped_example_kit_passes_the_preflight():
    kit = Path(__file__).resolve().parents[1] / "examples" / "research" / "fast-primes"
    rep = rs.check_kit(kit)
    assert rep["ok"], rs.format_check(rep)
    assert rep["values"]["correct_pct"] == 100 and rep["values"]["forbidden"] == 0


def test_create_pins_the_scale_zero_to_the_seed(tmp_path):
    rs.create_from_kit(_kit(tmp_path, seed={"speed": 2, "correct": 1}))
    speed = load_config(project_dir("speedy") / "config.yaml").evaluation.metrics[0]
    assert speed.worst == 2.0                       # the seed, not whatever the first candidate scores


# ---- a run started from the command line: the dashboard shows it, can Stop it, never "heals" it ----

def _dead_pid():
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_cli_run_marker_protects_the_run_from_the_dashboard(tmp_path):
    import os
    rs.create_from_kit(_kit(tmp_path))
    base = project_dir("speedy")
    st = StateStore(base)
    st.create_run("asymmetric")                                  # status 'running'
    st.close()
    (base / "run.pid").write_text(str(os.getpid()))             # a live CLI run
    c = TestClient(create_app(token=None))                       # startup must not heal it
    assert rs.project_status("speedy")["status"] == "running"
    assert c.post("/api/projects/speedy/run").status_code == 409
    assert c.post("/api/projects/speedy/delete").status_code == 409
    assert c.post("/api/projects/speedy/force-stop").status_code == 409
    assert c.post("/api/projects/speedy/stop").json().get("cli") is True
    assert (base / "stop.request").exists()
    (base / "run.pid").write_text(str(_dead_pid()))             # the process is gone → orphan
    TestClient(create_app(token=None))
    assert rs.project_status("speedy")["status"] == "stopped"


def test_cli_run_honours_a_stop_request_and_cleans_up(tmp_path, monkeypatch):
    import argparse
    import tyani_tolkai.cli as cli
    rs.create_from_kit(_kit(tmp_path))
    base = project_dir("speedy")

    def edit_then_request_stop(workdir: Path) -> bool:
        (workdir / "val.json").write_text(json.dumps({"speed": 5, "correct": 1}))
        (workdir.parent / "stop.request").write_text("stop")   # the dashboard's Stop
        return True
    execs = iter([MockAdapter([edit_then_request_stop, _set(9, 1)]), MockAdapter([])])
    monkeypatch.setattr(cli, "build_adapter", lambda *a, **k: next(execs))
    assert cli.cmd_run(argparse.Namespace(config=str(base / "config.yaml"), resume=False)) == 0
    s = rs.project_status("speedy")
    assert s["status"] == "stopped" and s["iterations"] == 1     # stopped at the boundary
    assert not (base / "run.pid").exists() and not (base / "stop.request").exists()


def test_report_summarises_each_kept_step_as_a_diffstat():
    diff = ("diff --git a/solution.py b/solution.py\n--- a/solution.py\n+++ b/solution.py\n"
            "@@ -1,2 +1,3 @@\n-old\n+new\n+more\n")
    assert rs._diffstat(diff) == "solution.py +2/−1"
