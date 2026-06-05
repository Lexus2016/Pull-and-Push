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
