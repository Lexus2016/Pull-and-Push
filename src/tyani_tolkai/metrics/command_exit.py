"""`command-exit` adapter: pass/fail from a command's exit code.

The configured metric (conventionally named ``passed``) gets value 1.0 on exit 0,
else 0.0. A non-zero exit is a *measured outcome* (the artifact failed its check),
not a runner error — so ``ok`` stays True (it ran).
"""

from __future__ import annotations

from pathlib import Path

from .base import MetricResult
from .numeric import _resolve_python


class CommandExitAdapter:
    def run(self, artifact_dir: str | Path, sandbox, evaluation, timeout: int) -> MetricResult:
        res = sandbox.run(_resolve_python(evaluation.command, sandbox), cwd=artifact_dir, timeout=timeout)
        passed = 1.0 if res.exit_code == 0 else 0.0
        metrics = [
            {"name": m.name, "value": passed, "dir": m.dir, "weight": m.weight}
            for m in evaluation.metrics
        ]
        logs = (res.stdout or "") + (res.stderr or "")
        return MetricResult(metrics=metrics, logs=logs, ok=not res.timed_out)
