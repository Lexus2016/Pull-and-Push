"""`pytest-pass` adapter: fraction of tests passing.

Runs pytest against the harness tests (``evaluation.harness_dir``, kept outside the
artifact and invisible to the Executor — spec lock #4) with the artifact importable.
Produces ``pass_pct`` (0–100), ``passed``, ``total``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .base import MetricResult

_PASSED = re.compile(r"(\d+) passed")
_FAILED = re.compile(r"(\d+) failed")
_ERROR = re.compile(r"(\d+) error")


def _count(pattern: re.Pattern, text: str) -> int:
    m = pattern.search(text)
    return int(m.group(1)) if m else 0


class PytestPassAdapter:
    def run(self, artifact_dir: str | Path, sandbox, evaluation, timeout: int) -> MetricResult:
        artifact_dir = Path(artifact_dir)
        harness = evaluation.harness_dir or "tests"
        harness_path = Path(harness)
        if not harness_path.is_absolute():
            # harness lives beside the artifact (project_dir/metrics), not inside it
            harness_path = (artifact_dir.parent / harness).resolve()
        cmd = f"{sys.executable} -m pytest {harness_path} -q -p no:cacheprovider"
        res = sandbox.run(
            cmd, cwd=artifact_dir, timeout=timeout,
            env={"PYTHONPATH": str(Path(artifact_dir).resolve())},
        )
        out = (res.stdout or "") + (res.stderr or "")
        passed = _count(_PASSED, out)
        failed = _count(_FAILED, out)
        errors = _count(_ERROR, out)
        total = passed + failed + errors
        ok = total > 0
        pass_pct = (passed / total * 100.0) if total else 0.0
        data = {"pass_pct": pass_pct, "passed": float(passed), "total": float(total)}
        metrics = [
            {"name": m.name, "value": data.get(m.name, 0.0), "dir": m.dir, "weight": m.weight}
            for m in evaluation.metrics
        ]
        return MetricResult(metrics=metrics, logs=out, ok=ok)
