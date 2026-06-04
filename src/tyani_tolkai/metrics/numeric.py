"""`numeric` adapter: run a command that prints a JSON object of named numbers."""

from __future__ import annotations

import json
from pathlib import Path

from .base import MetricResult


class NumericAdapter:
    def run(self, artifact_dir: str | Path, sandbox, evaluation, timeout: int) -> MetricResult:
        res = sandbox.run(evaluation.command, cwd=artifact_dir, timeout=timeout)
        logs = (res.stdout or "") + (res.stderr or "")
        if res.exit_code != 0:
            return MetricResult(metrics=[], logs=logs, ok=False)
        try:
            data = json.loads(res.stdout)
        except (json.JSONDecodeError, TypeError):
            return MetricResult(metrics=[], logs="invalid JSON:\n" + logs, ok=False)
        metrics = []
        for m in evaluation.metrics:
            if m.name in data:
                metrics.append(
                    {"name": m.name, "value": float(data[m.name]), "dir": m.dir, "weight": m.weight}
                )
        ok = len(metrics) == len(evaluation.metrics)
        # expose the full parsed harness output (incl. report-only fields beyond the scored
        # metrics) so consumers don't re-parse logs (which include stderr and would break).
        report = data if isinstance(data, dict) else {}
        return MetricResult(metrics=metrics, logs=logs, ok=ok, data=report)
