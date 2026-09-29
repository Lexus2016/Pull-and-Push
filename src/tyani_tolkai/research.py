"""P8 research kits — any experiment, from a spec to a running loop (docs/design/p8-research-kits.md).

A research kit is a folder:

    research.yaml   the spec: goal, what may be edited, criteria, hard constraints, target, budget
    seed/           the starting artifact — the only thing the executor edits
    scorer/         the judge, laid down as <project>/metrics/ (outside the artifact, invisible to it)

The same kit is produced by a human (dashboard wizard) or an agent (CLI), checked by `check_kit`
before any agent is paid, turned into a project by `create_from_kit`, and turned back into a kit by
`save_kit` for the next research round. `project_status` / `project_report` give a compact,
machine-readable view of a run.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .config import AgentCfg, Config, ConstraintCfg, MetricCfg
from .metrics import get_metric_adapter
from .projects import home_root, project_dir, valid_name
from .sandbox import get_backend
from .scaffold import USER_CODE_IGNORE, _JUNK, templates_root
from .scorer import constraint_violations, resolve_worst, score
from .state import StateStore

SPEC_FILE = "research.yaml"
DEFAULT_SCORER = "{python} ../metrics/evaluate.py"
DEFAULT_REVIEW = ("Review the artifact and the change: is the approach sound, did it improve the real "
                  "objective or only the number (look for ways the change games the measurement), why "
                  "the score moved, and one or two concrete ideas to try next.")


def kits_root() -> Path:
    """Where kits made by the dashboard wizard are kept (reusable as your own templates)."""
    return home_root() / "research"


class ResearchSpec(BaseModel):
    """research.yaml. Agents may be given as an engine name ("claude") or a full {engine, model,
    timeout}; limits / evaluation / checkpoints / sandbox / notify pass through to config.yaml."""
    name: str
    goal: str                                   # what we want — the executor's objective
    task: str | None = None                     # what the executor may edit / must not do
    review: str | None = None                   # the validator's brief (default: DEFAULT_REVIEW)
    scorer: str = DEFAULT_SCORER                # eval command; cwd = the artifact, judge in ../metrics
    adapter: Literal["numeric", "command-exit", "pytest-pass"] = "numeric"
    metrics: list[MetricCfg] = Field(min_length=1)
    constraints: list[ConstraintCfg] = Field(default_factory=list)
    target_score: float = 90.0
    agents: dict[str, AgentCfg] = Field(
        default_factory=lambda: {"executor": AgentCfg(engine="claude"),
                                 "validator": AgentCfg(engine="codex", timeout=300)})
    limits: dict = Field(default_factory=dict)
    evaluation: dict = Field(default_factory=dict)   # runs, min_delta, revalidate_every
    checkpoints: dict | None = None
    sandbox: dict = Field(default_factory=lambda: {"backend": "local"})
    notify: dict | None = None

    @field_validator("agents", mode="before")
    @classmethod
    def _engine_shorthand(cls, v):
        if isinstance(v, dict):
            return {role: ({"engine": a} if isinstance(a, str) else a) for role, a in v.items()}
        return v

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        return valid_name(v)


def load_kit(kit_dir: str | Path) -> ResearchSpec:
    f = Path(kit_dir) / SPEC_FILE
    if not f.is_file():
        raise FileNotFoundError(f"no {SPEC_FILE} in {kit_dir}")
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{SPEC_FILE} must be a mapping")
    return ResearchSpec.model_validate(data)


def spec_to_config(spec: ResearchSpec, name: str | None = None) -> dict:
    """The project config.yaml a kit produces (validated)."""
    agents = {role: a.model_dump(exclude_none=True) for role, a in spec.agents.items()}
    roles: dict = {"executor": {"goal": spec.goal.strip()}}
    if spec.task and spec.task.strip():
        roles["executor"]["task"] = spec.task.strip()
    if "validator" in agents:
        roles["validator"] = {"goal": (spec.review or DEFAULT_REVIEW).strip()}
    evaluation = {"adapter": spec.adapter,
                  "metrics": [m.model_dump(exclude_none=True) for m in spec.metrics],
                  "target_score": spec.target_score, "harness_dir": "metrics", **spec.evaluation}
    if spec.constraints:
        evaluation["constraints"] = [c.model_dump(exclude_none=True) for c in spec.constraints]
    if spec.adapter != "pytest-pass":
        evaluation["command"] = spec.scorer
    cfg = {"project": name or spec.name, "description": spec.goal.strip(), "mode": "asymmetric",
           "agents": agents, "roles": roles, "seed": {"mode": "empty"}, "evaluation": evaluation,
           "limits": {"max_iterations": 30, "plateau_N": 6, "step_seconds": 600, **spec.limits},
           "sandbox": dict(spec.sandbox)}
    if spec.checkpoints:
        cfg["checkpoints"] = dict(spec.checkpoints)
    if spec.notify:
        cfg["notify"] = dict(spec.notify)
    Config(**cfg)                                   # raises on anything invalid
    return cfg


def config_to_spec(cfg: dict) -> dict:
    """Reverse of spec_to_config (for `save_kit`): a project config → a research.yaml mapping."""
    ev = dict(cfg.get("evaluation") or {})
    roles = cfg.get("roles") or {}
    spec: dict = {"name": cfg.get("project"),
                  "goal": (roles.get("executor") or {}).get("goal") or cfg.get("description") or "",
                  "task": (roles.get("executor") or {}).get("task"),
                  "review": (roles.get("validator") or {}).get("goal"),
                  "adapter": ev.pop("adapter", "numeric"),
                  "scorer": ev.pop("command", None),
                  "metrics": ev.pop("metrics", []),
                  "constraints": ev.pop("constraints", []),
                  "target_score": ev.pop("target_score", 90.0),
                  "agents": cfg.get("agents") or {},
                  "limits": cfg.get("limits") or {}}
    ev.pop("harness_dir", None)
    if ev:
        spec["evaluation"] = ev
    for k in ("checkpoints", "sandbox", "notify"):
        if cfg.get(k):
            spec[k] = cfg[k]
    return {k: v for k, v in spec.items() if v not in (None, [], {})}


# ---------------------------------------------------------------------------------- kit skeleton

_SKELETON = """\
# research.yaml — one experiment for Pull-and-Push. The loop: an executor agent edits seed/, the
# scorer in scorer/ grades it, a candidate is kept only if it beats the best (and breaks no
# constraint), a reviewer agent explains and suggests the next step. Repeat.
name: {name}
goal: >-
  Describe WHAT you want improved and what "better" means, in plain words.
task: >-
  What the executor may edit (e.g. "Edit only solution.txt") and what it must not do.
# the judge: runs with cwd = the artifact; the scorer lives in ../metrics (outside the artifact)
scorer: "{{python}} ../metrics/evaluate.py"
metrics:                     # scored — each maps (the seed's value → target) onto 0..100
  - {{name: score, dir: higher, weight: 1.0, target: 100}}
constraints: []              # hard gates, e.g. - {{name: correct_pct, min: 100}}
target_score: 90             # composite score that counts as done
agents:
  executor: claude           # claude | codex | opencode
  validator: codex           # a DIFFERENT provider — uncorrelated blind spots
limits:
  max_iterations: 20
  plateau_N: 6               # stop after N changes in a row that did not improve
  step_seconds: 600          # timeout per agent step AND per evaluation
  budget_usd: 5              # hard cap on the (rough) estimated spend
  usd_per_mtok: 3            # price used for that estimate
evaluation:
  runs: 1                    # >1 = median of N measurements (noisy scorers)
  min_delta: 0.5             # keep only if the score improves by MORE than this
"""


def new_kit(kit_dir: str | Path, name: str | None = None) -> Path:
    """Write a runnable kit skeleton (the generic 'custom' example: seed + scorer + commented spec)
    to replace with your experiment."""
    kit = Path(kit_dir)
    if kit.exists() and any(kit.iterdir()):
        raise FileExistsError(f"{kit} is not empty")
    name = valid_name(name or kit.name)
    tpl = templates_root() / "custom"
    kit.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tpl / "seed", kit / "seed", ignore=_JUNK)
    shutil.copytree(tpl / "harness", kit / "scorer", ignore=_JUNK)
    (kit / SPEC_FILE).write_text(_SKELETON.format(name=name), encoding="utf-8")
    return kit


# ---------------------------------------------------------------------------------- pre-flight

def _numbers(res) -> dict:
    """Every value the scorer printed (report-only fields + scored metrics)."""
    vals = dict(getattr(res, "data", None) or {})
    vals.update({m["name"]: m["value"] for m in res.metrics})
    return vals


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}


def check_kit(kit_dir: str | Path, *, repeats: int = 2) -> dict:
    """Pre-flight: validate the spec and run the scorer on the seed `repeats` times, in the same
    layout a project uses. Errors block `create`; warnings are advice. Returns
    {ok, name, items: [{level, code, msg}], values, eval_seconds, baseline_score}."""
    items: list[dict] = []

    def add(level: str, code: str, msg: str) -> None:
        items.append({"level": level, "code": code, "msg": msg})

    def result(**extra) -> dict:
        return {"ok": not any(i["level"] == "error" for i in items), "items": items, **extra}

    kit = Path(kit_dir)
    try:
        spec = load_kit(kit)
        cfg = Config(**spec_to_config(spec))
    except Exception as e:                                     # noqa: BLE001 — report, don't raise
        add("error", "spec", f"invalid {SPEC_FILE}: {e}")
        return result(name=None, values={}, eval_seconds=None, baseline_score=None)

    ev, lim = cfg.evaluation, cfg.limits
    scorer_dir = kit / "scorer"
    if not scorer_dir.is_dir() or not any(scorer_dir.iterdir()):
        add("error", "scorer", "no scorer/ folder — the judge that prints the metrics is required")
        return result(name=spec.name, values={}, eval_seconds=None, baseline_score=None)

    ex = cfg.agents.get("executor")
    va = cfg.agents.get("validator")
    if ex and va and ex.engine == va.engine:
        add("warn", "collusion", f"executor and validator are both {ex.engine!r} — use different "
                                 "providers so their blind spots don't coincide")
    if va is None:
        add("info", "no-validator", "no validator: the executor only sees scores, no review")
    if lim.budget_usd is None or (lim.usd_per_mtok or 0) <= 0:
        add("warn", "budget", "no spend cap: set limits.budget_usd and limits.usd_per_mtok")

    values: dict = {}
    durations: list[float] = []
    baseline = None
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "project"
        art = proj / "artifact"
        art.mkdir(parents=True)
        if (kit / "seed").is_dir():
            shutil.copytree(kit / "seed", art, dirs_exist_ok=True, ignore=USER_CODE_IGNORE)
        else:
            add("info", "empty-seed", "no seed/ — the executor starts from an empty folder")
        shutil.copytree(scorer_dir, proj / "metrics", ignore=_JUNK)

        if ev.adapter in ("numeric", "command-exit"):
            script = next((t for t in (ev.command or "").split() if t.endswith(".py")), None)
            if script and not (art / script).resolve().exists():
                add("error", "scorer-path", f"the scorer command runs {script!r} (relative to the "
                                            "artifact), which is not in scorer/")
                return result(name=spec.name, values={}, eval_seconds=None, baseline_score=None)

        adapter = get_metric_adapter(ev.adapter)
        try:
            sandbox = get_backend(cfg.sandbox.backend, cfg.sandbox)
        except Exception as e:                                 # noqa: BLE001
            add("error", "sandbox", f"sandbox unavailable: {e}")
            return result(name=spec.name, values={}, eval_seconds=None, baseline_score=None)

        before = _files(art)
        runs: list[dict] = []
        for _ in range(max(1, repeats)):
            t0 = time.monotonic()
            res = adapter.run(art, sandbox, ev, lim.step_seconds)
            durations.append(time.monotonic() - t0)
            if not res.ok and not getattr(res, "data", None):   # printed JSON = judged below
                tail = (res.logs or "").strip()[-1200:]
                add("error", "scorer-failed", "the scorer failed on the seed — it must run on the "
                    "starting artifact (give the seed a minimal working version, or make the scorer "
                    f"print low values for a missing artifact):\n{tail}")
                break
            runs.append(_numbers(res))
        stray = sorted(_files(art) - before)
        if stray:
            add("info", "side-effects", "the scorer wrote into the artifact (discarded after every "
                                        f"evaluation, but it should write elsewhere): {stray[:5]}")

    eval_s = max(durations) if durations else None
    if eval_s is not None and eval_s > lim.step_seconds * 0.5:
        add("warn", "slow", f"one evaluation took {eval_s:.1f}s — over half of step_seconds "
                            f"({lim.step_seconds}); raise limits.step_seconds")
    if not runs or len(runs) < max(1, repeats):
        return result(name=spec.name, values=runs[0] if runs else {}, eval_seconds=eval_s,
                      baseline_score=None)
    values = runs[0]

    def finite(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    missing = [m.name for m in ev.metrics if not finite(values.get(m.name))]
    if missing:
        add("error", "metric-missing", "the scorer does not print these scored metrics as finite "
                                       f"numbers: {', '.join(missing)} (printed: {sorted(values)})")
    for c in ev.constraints:
        if not finite(values.get(c.name)):
            add("error", "constraint-missing", f"constraint {c.name!r} is not printed by the scorer")
    noisy = sorted(k for k in set(runs[0]) | set(runs[-1])
                   if finite(runs[0].get(k)) and runs[0].get(k) != runs[-1].get(k))
    scored = {m.name for m in ev.metrics}
    if noisy_scored := [k for k in noisy if k in scored]:
        add("warn", "noisy", f"the scorer is not deterministic ({', '.join(noisy_scored)} changed "
                             "between two runs on the same seed): set evaluation.runs: 3 and a "
                             "larger min_delta, or fix the randomness (seed it)")
    elif noisy:                          # a timing constraint / report field: normal, not a signal
        add("info", "noisy-unscored", f"{', '.join(noisy)} vary between runs (not scored — only "
                                      "matters if a constraint sits close to its limit)")
    if missing:
        return result(name=spec.name, values=values, eval_seconds=eval_s, baseline_score=None)

    specs = [m.model_copy() for m in ev.metrics]
    for m in specs:
        v = float(values[m.name])
        if (m.dir == "higher" and v >= m.target) or (m.dir == "lower" and v <= m.target):
            add("warn", "no-headroom", f"{m.name}={v:g} already meets its target {m.target:g} on the "
                                       "seed — it adds a constant, not a signal; raise the target")
        if m.worst is None:
            m.worst = resolve_worst(m.dir, m.target, v)
    try:
        baseline = score({m.name: float(values[m.name]) for m in specs}, specs)
    except (KeyError, ValueError) as e:
        add("error", "score", f"cannot score the seed: {e}")
    if baseline is not None and all(
            (m.dir == "higher" and float(values[m.name]) >= m.target)
            or (m.dir == "lower" and float(values[m.name]) <= m.target) for m in specs):
        add("error", "already-done", "the seed already meets every target — nothing to optimize; "
                                     "the targets are too low or the scorer isn't measuring the seed")
    broken = constraint_violations(values, ev.constraints)
    if broken:
        add("info", "seed-constraints", "the seed breaks constraint(s) — the first kept candidate "
                                        "must satisfy them: " + "; ".join(broken))
    return result(name=spec.name, values=values, eval_seconds=eval_s, baseline_score=baseline)


def format_check(report: dict) -> str:
    """The pre-flight report as terminal text."""
    icon = {"error": "✖", "warn": "⚠", "info": "·"}
    lines = [f"pre-flight {'PASS' if report['ok'] else 'FAIL'}: {report.get('name') or '?'}"]
    if report.get("values"):
        shown = {k: v for k, v in report["values"].items() if isinstance(v, (int, float))}
        lines.append("  seed values: " + ", ".join(f"{k}={v:g}" for k, v in shown.items()))
    if report.get("eval_seconds") is not None:
        lines.append(f"  one evaluation: {report['eval_seconds']:.2f}s")
    for it in report["items"]:
        lines.append(f"  {icon.get(it['level'], '-')} [{it['code']}] {it['msg']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- create / save

def create_from_kit(kit_dir: str | Path, name: str | None = None, *, check: bool = True) -> dict:
    """Kit → runnable project (pre-flight first; errors refuse). The kit is copied into the
    project as research/ for provenance. Returns {created, check}."""
    kit = Path(kit_dir)
    spec = load_kit(kit)
    name = valid_name(name or spec.name)
    report = check_kit(kit) if check else None
    if report is not None and not report["ok"]:
        raise ValueError("pre-flight failed — fix the kit first:\n" + format_check(report))
    base = project_dir(name)
    if (base / "config.yaml").exists():
        raise FileExistsError(f"project {name!r} already exists")
    cfg = spec_to_config(spec, name)
    # 0 = the SEED. Left unpinned, the loop pins each metric's zero-point to its first measured
    # CANDIDATE — if the executor's first edit already lands near the target, the whole 0..100
    # scale collapses into a sliver where measurement noise swings the score by tens of points.
    # The pre-flight has just measured the seed, so pin to that (an explicit `worst` still wins).
    seed_vals = (report or {}).get("values") or {}
    for m in cfg["evaluation"]["metrics"]:
        v = seed_vals.get(m["name"])
        if m.get("worst") is None and isinstance(v, (int, float)) and math.isfinite(v):
            m["worst"] = resolve_worst(m["dir"], m["target"], float(v))
    Config(**cfg)
    state = StateStore(base)
    try:
        state.artifact_dir.mkdir(parents=True, exist_ok=True)
        if (kit / "seed").is_dir():
            shutil.copytree(kit / "seed", state.artifact_dir, dirs_exist_ok=True,
                            ignore=USER_CODE_IGNORE)
        shutil.copytree(kit / "scorer", base / "metrics", dirs_exist_ok=True, ignore=_JUNK)
        shutil.copytree(kit, base / "research", dirs_exist_ok=True, ignore=USER_CODE_IGNORE)
        if report is not None:                             # the seed's measured values → `report`
            (base / "research" / "preflight.json").write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8")
        state.git_init()                                   # the seed = the reset baseline
        (base / "config.yaml").write_text(
            yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    finally:
        state.close()
    return {"created": name, "check": report}


def save_kit(project: str, kit_dir: str | Path, *, source: Literal["best", "seed"] = "best",
             name: str | None = None) -> Path:
    """Project → kit, for the next research round: the same scorer and spec, and as the seed either
    the project's current best (continue from where it got to) or its original seed."""
    base = project_dir(project)
    if not (base / "config.yaml").is_file():
        raise FileNotFoundError(f"no such project: {project}")
    kit = Path(kit_dir)
    if kit.exists() and any(kit.iterdir()):
        raise FileExistsError(f"{kit} is not empty")
    cfg = yaml.safe_load((base / "config.yaml").read_text(encoding="utf-8")) or {}
    spec = config_to_spec(cfg)
    if name:
        spec["name"] = valid_name(name)
    kit.mkdir(parents=True, exist_ok=True)
    state = StateStore(base)
    try:
        ref = state.head() if source == "best" else \
            state._git("rev-list", "--max-parents=0", "HEAD").splitlines()[0]
        state.export_tree(ref, kit / "seed")
    finally:
        state.close()
    (kit / "seed" / ".gitignore").unlink(missing_ok=True)   # housekeeping file of the artifact repo
    if (base / "metrics").is_dir():
        shutil.copytree(base / "metrics", kit / "scorer", ignore=_JUNK)
    (kit / SPEC_FILE).write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
                                 encoding="utf-8")
    return kit


# ---------------------------------------------------------------------------------- status / report

def _latest_run(st: StateStore):
    return (st.conn.execute("SELECT * FROM run WHERE id IN (SELECT DISTINCT run_id FROM iteration) "
                            "ORDER BY id DESC LIMIT 1").fetchone()
            or st.conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone())


def _first_line(text: str | None, n: int = 160) -> str:
    t = (text or "").strip().splitlines()
    return (t[0][:n] if t else "")


def _diffstat(diff: str | None) -> str:
    """'solution.py +29/−4' — which files a kept step touched and how much (from its stored diff)."""
    files: dict[str, list[int]] = {}
    cur = None
    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            cur = line[4:].removeprefix("b/")
            files.setdefault(cur, [0, 0])
        elif cur and line.startswith("+") and not line.startswith("+++"):
            files[cur][0] += 1
        elif cur and line.startswith("-") and not line.startswith("---"):
            files[cur][1] += 1
    return ", ".join(f"{f} +{a}/−{d}" for f, (a, d) in files.items()) or _first_line(diff, 120)


def project_status(project: str, last: int = 5) -> dict:
    """Compact state of a project's latest run — for an agent polling a research loop."""
    base = project_dir(project)
    if not (base / "config.yaml").is_file():
        raise FileNotFoundError(f"no such project: {project}")
    cfg = yaml.safe_load((base / "config.yaml").read_text(encoding="utf-8")) or {}
    lim, ev = cfg.get("limits") or {}, cfg.get("evaluation") or {}
    out: dict = {"project": project, "status": "new", "best_score": None, "best_iteration": None,
                 "target_score": ev.get("target_score"), "iterations": 0,
                 "max_iterations": lim.get("max_iterations"), "plateau": 0,
                 "plateau_N": lim.get("plateau_N"), "cost_usd": 0.0, "verdicts": {}, "last": []}
    if (base / "arena.json").exists():                   # symmetric: the manifest is the state
        m = json.loads((base / "arena.json").read_text(encoding="utf-8"))
        out.update(mode="symmetric", status=m.get("status"), generation=m.get("generation"),
                   generations=m.get("generations"), stop_reason=m.get("stop_reason"))
        return out
    if not (base / "state.db").exists():
        return out
    st = StateStore(base)
    try:
        run = _latest_run(st)
        if run is None:
            return out
        rid = run["id"]
        keep = st.conn.execute("SELECT n FROM iteration WHERE run_id=? AND verdict='keep' "
                               "ORDER BY n DESC LIMIT 1", (rid,)).fetchone()
        counts = {r["verdict"]: r["c"] for r in st.conn.execute(
            "SELECT verdict, COUNT(*) AS c FROM iteration WHERE run_id=? GROUP BY verdict", (rid,))}
        out.update(status=run["status"], best_score=run["best_score"],
                   best_iteration=keep["n"] if keep else None, iterations=run["iter_count"] or 0,
                   plateau=run["plateau_count"] or 0, cost_usd=round(run["cost_total"] or 0.0, 4),
                   verdicts=counts,
                   last=[{"n": it.n, "verdict": it.verdict, "score": it.score,
                          "metrics": {m["name"]: m["value"] for m in it.metrics},
                          "note": _first_line(it.feedback)}
                         for it in st.last_iterations(rid, max(0, last))])
    finally:
        st.close()
    return out


def project_report(project: str, diff_chars: int = 20000) -> dict:
    """What the run achieved: each metric at the start (first measurement = the zero point), at the
    current best, and its target; every kept step; the seed → best diff."""
    base = project_dir(project)
    status = project_status(project, last=0)
    cfg = yaml.safe_load((base / "config.yaml").read_text(encoding="utf-8")) or {}
    ev = cfg.get("evaluation") or {}
    rep = {**status, "constraints": ev.get("constraints") or [], "metrics": [], "kept": [],
           "diff": ""}
    if not (base / "state.db").exists() or status.get("mode") == "symmetric":
        return rep
    st = StateStore(base)
    try:
        run = _latest_run(st)
        if run is None:
            return rep
        iters = st.last_iterations(run["id"], 10**9)
        measured = [it for it in iters if it.metrics and it.verdict != "fail"]
        kept = [it for it in iters if it.verdict == "keep"]
        first = {m["name"]: m["value"] for m in measured[0].metrics} if measured else {}
        best = {m["name"]: m["value"] for m in kept[-1].metrics} if kept else {}
        seed: dict = {}
        pf = base / "research" / "preflight.json"
        if pf.is_file():
            try:
                seed = json.loads(pf.read_text(encoding="utf-8")).get("values") or {}
            except ValueError:
                seed = {}
        rep["metrics"] = [{"name": m["name"], "dir": m["dir"], "target": m["target"],
                           "weight": m.get("weight", 1.0), "seed": seed.get(m["name"]),
                           "start": first.get(m["name"]), "best": best.get(m["name"])}
                          for m in ev.get("metrics") or []]
        rep["kept"] = [{"n": it.n, "score": it.score,
                        "metrics": {m["name"]: m["value"] for m in it.metrics},
                        "change": _diffstat(it.change_summary)} for it in kept]
        try:
            seed = st._git("rev-list", "--max-parents=0", "HEAD").splitlines()[0]
            rep["diff"] = st._git("diff", seed, "HEAD", "--", ".", ":(exclude).gitignore")[:diff_chars]
        except RuntimeError:
            rep["diff"] = ""
    finally:
        st.close()
    return rep


def format_status(s: dict) -> str:
    best = "—" if s.get("best_score") is None else f"{s['best_score']:.2f}"
    lines = [f"{s['project']}: {s.get('status')}  best={best} (iter {s.get('best_iteration') or '—'})"
             f"  target={s.get('target_score')}  iterations={s.get('iterations')}/"
             f"{s.get('max_iterations')}  plateau={s.get('plateau')}/{s.get('plateau_N')}"
             f"  ≈${s.get('cost_usd', 0):.2f}  {s.get('verdicts') or ''}"]
    for it in s.get("last") or []:
        sc = "—" if it["score"] is None else f"{it['score']:.2f}"
        mets = " ".join(f"{k}={v:g}" for k, v in it["metrics"].items())
        lines.append(f"  #{it['n']:<4} {it['verdict']:<8} {sc:>7}  {mets}  {it['note']}")
    return "\n".join(lines)


def format_report(r: dict) -> str:
    lines = [format_status({**r, "last": []}), "",
             "metric              seed   1st kept       best     target"]
    def f(v) -> str:
        return "—" if v is None else f"{v:g}"
    for m in r.get("metrics") or []:
        lines.append(f"  {m['name']:<14} {f(m.get('seed')):>8}  {f(m['start']):>9}  "
                     f"{f(m['best']):>9}  {('≥' if m['dir'] == 'higher' else '≤')}{m['target']:g}")
    if r.get("constraints"):
        lines.append("constraints: " + ", ".join(
            f"{c['name']}" + (f" ≥ {c['min']:g}" if c.get("min") is not None else "")
            + (f" ≤ {c['max']:g}" if c.get("max") is not None else "") for c in r["constraints"]))
    if r.get("kept"):
        lines.append("\nkept steps:")
        lines += [f"  #{k['n']:<4} {k['score']:.2f}  {k['change']}" for k in r["kept"]
                  if k["score"] is not None]
    if r.get("diff"):
        lines += ["", "seed → best diff:", r["diff"]]
    return "\n".join(lines)


def start_via_dashboard(project: str, url: str, token: str | None = None,
                        timeout: float = 5.0) -> dict:
    """Start (= continue) a project's run in a running dashboard, so a human sees it live and can
    Stop it there. Raises OSError (URLError) when no dashboard answers at ``url``."""
    import urllib.parse
    import urllib.request
    q = f"?token={urllib.parse.quote(token)}" if token else ""
    req = urllib.request.Request(f"{url.rstrip('/')}/api/projects/{project}/run{q}", method="POST",
                                 data=b"")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")
