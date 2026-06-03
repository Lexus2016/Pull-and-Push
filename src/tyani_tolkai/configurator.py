"""Configurator agent: turn a plain-language task description into a valid config.

Per the no-bespoke-agents rule, the configurator is just an off-the-shelf CLI agent
(claude/codex/…) driven by a fixed schema prompt. It returns a JSON config object,
which we parse and hand to the form for the user to fine-tune. It never writes files
(read-only profile); we only read its stdout.
"""

from __future__ import annotations

import json
import re
import tempfile

CONFIGURATOR_PROMPT = """You are the PROJECT CONFIGURATOR for an adversarial co-evolution
orchestrator (an Executor agent improves an artifact; a deterministic scorer + optional
Validator agent judge it; the loop keeps improvements). Given the user's task, output a
SINGLE JSON config object and NOTHING else — no prose, no markdown fences.

JSON schema (fill every relevant field, keep it minimal and valid):
{
  "project": "<kebab-case-name>",
  "mode": "asymmetric",
  "agents": {
    "executor":  {"engine": "claude|codex|opencode|agy", "model": null, "timeout": 600},
    "validator": {"engine": "<a DIFFERENT provider than executor>", "model": null, "timeout": 300}
  },
  "roles": {
    "executor":  {"goal": "<what to maximize/achieve>", "task": "<constraints, e.g. edit only X>"},
    "validator": {"goal": "<diagnose score movement, give one concrete next step>"}
  },
  "evaluation": {
    "adapter": "numeric | command-exit | pytest-pass",
    "command": "<shell command; REQUIRED for numeric and command-exit; omit for pytest-pass>",
    "metrics": [
      {"name": "<metric>", "dir": "higher|lower", "weight": <num>, "worst": <num>, "target": <num>}
    ],
    "target_score": <num 0-100>,
    "min_delta": <num>,
    "harness_dir": "metrics/"
  },
  "limits":  {"max_iterations": <int>, "plateau_N": <int>, "step_seconds": <int>},
  "sandbox": {"backend": "local | docker"},
  "seed":    {"mode": "empty|copy|generate", "path": null}
}

Guidance:
- Choose the adapter that fits: code-correctness → pytest-pass; numeric optimization
  (Sharpe, latency, accuracy) → numeric with a command printing JSON; pass/fail script → command-exit.
- worst→target must map the metric's bad→good range so progress reads as 0→100.
- Pick DIFFERENT providers for executor vs validator (anti-collusion).
- For numeric/command-exit include a concrete "command". For pytest-pass set "harness_dir".

USER TASK:
<<<DESCRIPTION>>>

Output ONLY the JSON object."""


def build_configurator_prompt(description: str) -> str:
    return CONFIGURATOR_PROMPT.replace("<<<DESCRIPTION>>>", description.strip())


def extract_json(text: str) -> dict:
    """Pull the JSON config object out of an agent's free-text output.

    Handles plain JSON, fenced ```json blocks, and JSON surrounded by prose — including
    NESTED objects (depth-tracked balanced braces, not a naive regex).
    """
    if not text or not text.strip():
        raise ValueError("empty configurator output")
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:                              # strip the code fence, keep its contents
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in configurator output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in configurator output")


def generate_config(description: str, engine: str = "claude", model: str | None = None,
                    runner=None, timeout: int = 180) -> dict:
    """Run the configurator agent on a description and return a parsed config dict.

    ``runner`` (a callable ``prompt -> stdout``) is injectable for tests; by default a
    real CLI agent is used in read-only mode.
    """
    prompt = build_configurator_prompt(description)
    if runner is not None:
        out = runner(prompt)
    else:
        from .registry import build_adapter
        adapter = build_adapter(engine, model, "read-only")
        with tempfile.TemporaryDirectory() as d:
            res = adapter.run(prompt, d, "read-only", timeout)
        out = res.stdout
    return extract_json(out)
