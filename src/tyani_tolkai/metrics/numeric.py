"""`numeric` adapter: run a command that prints a JSON object of named numbers."""

from __future__ import annotations

import json
import math
import shlex
import sys
from pathlib import Path

from .base import MetricResult


def _resolve_python(command: str, sandbox) -> str:
    """Substitute the portable ``{python}`` placeholder with the sandbox's interpreter
    (sys.executable locally, the image's ``python`` in docker). A bare ``python`` in a command
    is NOT portable — many systems only have ``python3`` — so templates use ``{python}``."""
    # forward slashes so the path survives shlex.split(posix=True) on Windows (C:/...\python.exe)
    py = str(getattr(sandbox, "python", sys.executable)).replace("\\", "/")
    # quoted: a venv under a path with spaces ("C:/Program Files/…", "/Users/John Smith/…") must
    # stay ONE argv item through shlex.split / sh -c
    return (command or "").replace("{python}", shlex.quote(py))


class NumericAdapter:
    def run(self, artifact_dir: str | Path, sandbox, evaluation, timeout: int) -> MetricResult:
        res = sandbox.run(_resolve_python(evaluation.command, sandbox), cwd=artifact_dir, timeout=timeout)
        logs = (res.stdout or "") + (res.stderr or "")
        if res.exit_code != 0:
            return MetricResult(metrics=[], logs=logs, ok=False)
        try:
            data = json.loads(res.stdout)
        except (json.JSONDecodeError, TypeError):
            return MetricResult(metrics=[], logs="invalid JSON:\n" + logs, ok=False)
        if not isinstance(data, dict):
            return MetricResult(metrics=[], logs="expected a JSON object of named numbers:\n" + logs,
                                ok=False)
        metrics, bad = [], []
        for m in evaluation.metrics:
            if m.name not in data:
                continue
            v = data[m.name]
            # json.loads accepts NaN / Infinity, and a harness may print null or a string. None of
            # them is a measurement: NaN would clamp to a PERFECT 100 in the scorer (and can't be
            # stored), ±inf would pin a useless zero-point. Treat as a failed evaluation instead.
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                bad.append(f"{m.name}={v!r}")
                continue
            metrics.append({"name": m.name, "value": float(v), "dir": m.dir, "weight": m.weight})
        if bad:
            return MetricResult(metrics=[], ok=False, data=data,
                                logs="non-numeric / non-finite metric value(s): " + ", ".join(bad)
                                     + "\n" + logs)
        ok = len(metrics) == len(evaluation.metrics)
        # expose the full parsed harness output (incl. report-only fields beyond the scored
        # metrics) so consumers don't re-parse logs (which include stderr and would break).
        report = data if isinstance(data, dict) else {}
        return MetricResult(metrics=metrics, logs=logs, ok=ok, data=report)
