"""Research + scaffold phase: turn a plain-language description into a runnable project.

Pure-LLM generation of the *scoring harness* is what previously derailed runs (the
agent would invent a scorer that rewarded the wrong thing, or delete it). So the
harness is never generated — it ships in a **vetted template**. A template is a
directory under ``templates/<id>/`` holding:

    template.json   manifest: id, name, keywords, summary, and a config skeleton
    seed/           files copied INTO the artifact (the editable surface), committed
    harness/        files copied into <project>/metrics — OUTSIDE the artifact, so the
                    executor can neither see nor edit the scorer, and `git clean` of the
                    artifact can't wipe it (anti-collusion lock #4)

``scaffold_project`` assembles a validated ``config.yaml`` from the skeleton (with the
user's description as the task goal), lays down seed + harness, and commits the seed —
producing a project that runs immediately, with no hand-authored math.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from .config import Config
from .projects import project_dir, valid_name
from .proposal_schema import MetricProposal
from .state import StateStore

_JUNK = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def templates_root() -> Path:
    return Path(__file__).resolve().parent / "templates"


def load_template(template_id: str) -> dict:
    """Read and lightly validate a template manifest."""
    if "/" in template_id or ".." in template_id or "\\" in template_id:
        raise ValueError(f"invalid template id: {template_id!r}")
    manifest = templates_root() / template_id / "template.json"
    if not manifest.exists():
        raise FileNotFoundError(f"no such template: {template_id!r}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if "config" not in data:
        raise ValueError(f"template {template_id!r} has no 'config' skeleton")
    return data


def list_templates() -> list[dict]:
    """All templates, as light dicts (id, name, summary, keywords) for a picker UI."""
    root = templates_root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        man = d / "template.json"
        if not (d.is_dir() and man.exists()):
            continue
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        out.append({
            "id": m.get("id", d.name),
            "name": m.get("name", d.name),
            "summary": m.get("summary", ""),
            "keywords": m.get("keywords", []),
        })
    return out


def pick_template(description: str) -> str | None:
    """Best-matching template id for a description, falling back to the generic 'custom' skeleton.

    Deterministic, no model call: lowercase the description, count how many of each template's
    keywords appear, return the highest scorer (ties break by id, alphabetical, for reproducibility).
    If NO domain template matches, fall back to 'custom' (when present) so the auto-detect path never
    dead-ends — the user always gets a runnable project to refine, not an error. Returns None only
    for an empty description or when no template (not even 'custom') exists.
    """
    text = (description or "").lower()
    if not text.strip():
        return None
    templates = list_templates()
    best_id, best_score = None, 0
    for tpl in templates:
        score = sum(1 for kw in tpl.get("keywords", []) if str(kw).lower() in text)
        if score > best_score or (score == best_score and score > 0
                                  and (best_id is None or tpl["id"] < best_id)):
            best_id, best_score = tpl["id"], score
    if best_score > 0:
        return best_id
    return "custom" if any(t["id"] == "custom" for t in templates) else None


def _build_config(name: str, description: str, template: dict) -> dict:
    """Merge the template skeleton with the project name + description, then validate."""
    cfg = json.loads(json.dumps(template["config"]))   # deep copy
    cfg["project"] = name
    cfg["description"] = description.strip()
    # The harness is laid down directly by scaffold, so the seed mode is 'empty'
    # (the first git commit captures the seed as the reset baseline either way).
    cfg.setdefault("seed", {"mode": "empty"})
    # Fold the description into the executor goal so the agent knows the real task.
    roles = cfg.setdefault("roles", {})
    ex = roles.setdefault("executor", {})
    ex.setdefault("goal", description.strip())
    Config(**cfg)                                       # raises on any invalid skeleton
    return cfg


def scaffold_project(name: str, description: str, template_id: str | None = None) -> dict:
    """Create a runnable project from a template. Returns {created, template, harness}.

    If ``template_id`` is None it is inferred from the description; a description that
    matches nothing raises (the caller should offer an explicit template choice).
    """
    valid_name(name)
    if not (description or "").strip():
        raise ValueError("description is required")
    if template_id is None:
        template_id = pick_template(description)
        if template_id is None:
            raise ValueError("could not infer a template from the description; "
                             "pass an explicit template_id")
    template = load_template(template_id)
    tpl_dir = templates_root() / template_id

    base = project_dir(name)
    if (base / "config.yaml").exists():
        raise FileExistsError(f"project {name!r} already exists")

    cfg = _build_config(name, description, template)
    harness_dir = (cfg.get("evaluation") or {}).get("harness_dir") or "metrics"

    state = StateStore(base)
    try:
        state.artifact_dir.mkdir(parents=True, exist_ok=True)
        seed_src = tpl_dir / "seed"
        if seed_src.is_dir():
            shutil.copytree(seed_src, state.artifact_dir, dirs_exist_ok=True, ignore=_JUNK)
        harness_src = tpl_dir / "harness"
        laid_harness = False
        if harness_src.is_dir():
            shutil.copytree(harness_src, base / harness_dir, dirs_exist_ok=True, ignore=_JUNK)
            laid_harness = True
        state.git_init()                                # commits the seed as the baseline
        (base / "config.yaml").write_text(
            yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    finally:
        state.close()
    return {"created": name, "template": template_id,
            "harness": harness_dir if laid_harness else None}


# --------------------------------------------------------------------------- P4.5 onboarding
def build_onboarding_config(name: str, *, proposal: MetricProposal, bot_cmd: str, seed_token: str,
                            data_rel: str = "../metrics/data.csv", goal: str | None = None,
                            harness_dir: str = "metrics") -> dict:
    """Assemble + validate a runnable config that optimizes an existing bot via the `score-bot`
    numeric adapter (spec: docs/design/p4.5-onboarding-integration.md).

    The bot IS the artifact (cwd of the eval command); the OOS data sits in ``../metrics`` outside
    the artifact so the bot/executor cannot peek the held-out tail. Metrics come straight from the
    P2 proposal. Raises if the proposal carries no metrics (EvaluationCfg needs ≥1).
    """
    if not proposal.proposed_metrics:
        raise ValueError("proposal has no metrics; cannot build an evaluation config")
    goal = (goal or proposal.goal or "").strip()
    metrics = [{"name": m.name, "dir": m.dir, "weight": m.weight, "target": m.target}
               for m in proposal.proposed_metrics]
    # cwd is the artifact → `--bot-dir .` is the live edited bot, `../metrics/data.csv` the read-only
    # sibling. `{python}` is substituted by the numeric adapter; `-m tyani_tolkai.cli` avoids relying
    # on the console script being on PATH inside the run sandbox.
    command = (f'{{python}} -m tyani_tolkai.cli score-bot '
               f'--data {data_rel} --bot-dir . --bot-cmd "{bot_cmd}" --seed {seed_token}')
    cfg = {
        "project": name,
        "description": goal,
        "mode": "asymmetric",
        "agents": {"executor": {"engine": "claude", "timeout": 600},
                   "validator": {"engine": "claude", "timeout": 300}},
        "roles": {
            "executor": {"goal": goal,
                         "task": "Improve the bot's score by tuning its parameters and/or refining "
                                 "its logic. Do NOT read, modify, or run anything under ../metrics "
                                 "(the hidden scorer + held-out data). One focused change per step."},
            "validator": {"goal": "Review the bot and the change: whether the approach is sound, why "
                                  "the score moved, and one or two concrete ideas to try next."},
        },
        "seed": {"mode": "empty"},
        "evaluation": {"adapter": "numeric", "command": command, "metrics": metrics,
                       "target_score": 100},
        "limits": {"max_iterations": 40, "plateau_N": 8, "step_seconds": 600},
        # local: the orchestrator runs score-bot as a subprocess; score-bot spawns the Docker
        # sandbox for the bot itself, so a docker backend here would nest containers.
        "sandbox": {"backend": "local"},
    }
    Config(**cfg)                                       # raises on any invalid field
    return cfg


def scaffold_onboarding(name: str, *, bot_dir, data_path, proposal: MetricProposal, bot_cmd: str,
                        seed_token: str | None = None, goal: str | None = None) -> dict:
    """Create a runnable optimization project from an existing bot + its data + a P2 proposal.

    Lays the bot into the artifact (the editable surface), the OHLCV data into ``metrics/data.csv``
    (outside the artifact), writes the generated config, and commits the bot as the reset baseline.
    Mirrors `scaffold_project`. Returns ``{created, data, metrics}``. No Docker needed to scaffold;
    a real run needs Docker (score-bot sandboxes the bot).
    """
    valid_name(name)
    bot_dir = Path(bot_dir)
    data_path = Path(data_path)
    if not bot_dir.is_dir():
        raise FileNotFoundError(f"bot-dir not found: {bot_dir}")
    if not data_path.is_file():
        raise FileNotFoundError(f"data not found: {data_path}")
    if not proposal.proposed_metrics:
        raise ValueError("proposal has no metrics; cannot onboard")
    seed_token = seed_token or name
    harness_dir = "metrics"

    base = project_dir(name)
    if (base / "config.yaml").exists():
        raise FileExistsError(f"project {name!r} already exists")

    cfg = build_onboarding_config(name, proposal=proposal, bot_cmd=bot_cmd, seed_token=seed_token,
                                  data_rel=f"../{harness_dir}/data.csv", goal=goal,
                                  harness_dir=harness_dir)
    state = StateStore(base)
    try:
        state.artifact_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bot_dir, state.artifact_dir, dirs_exist_ok=True, ignore=_JUNK)
        (base / harness_dir).mkdir(parents=True, exist_ok=True)
        shutil.copy2(data_path, base / harness_dir / "data.csv")
        state.git_init()                                # commits the bot as the reset baseline
        (base / "config.yaml").write_text(
            yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    finally:
        state.close()
    return {"created": name, "data": str(base / harness_dir / "data.csv"), "metrics": harness_dir}
