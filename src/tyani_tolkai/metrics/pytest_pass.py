"""`pytest-pass` adapter: fraction of tests passing.

Runs pytest against the harness tests (``evaluation.harness_dir``, kept outside the
artifact and invisible to the Executor — spec lock #4) with the artifact importable.
Produces ``pass_pct`` (0–100), ``passed``, ``total``.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from .base import MetricResult

_PASSED = re.compile(r"(\d+) passed")
_FAILED = re.compile(r"(\d+) failed")
_ERROR = re.compile(r"(\d+) error")
# pytest's closing summary: "2 failed, 1 passed, 1 warning in 0.12s" (-q; '=' padded without -q)
_SUMMARY = re.compile(r"\b\d+ (?:passed|failed|errors?)\b.*\bin \d+(?:\.\d+)?s\b")


def _count(pattern: re.Pattern, text: str) -> int:
    m = pattern.search(text)
    return int(m.group(1)) if m else 0


def _summary_line(stdout: str) -> str:
    """pytest's own closing summary line — the LAST one in stdout. The counts must come only from
    it: captured output of a failing test is printed ABOVE the summary, so a first-match search over
    the whole output let code under test print '999 passed' and turn a red suite into a 100 score."""
    for line in reversed((stdout or "").splitlines()):
        if _SUMMARY.search(line):
            return line
    return ""


class PytestPassAdapter:
    def run(self, artifact_dir: str | Path, sandbox, evaluation, timeout: int) -> MetricResult:
        artifact_dir = Path(artifact_dir)
        harness = evaluation.harness_dir or "tests"
        harness_path = Path(harness)
        if not harness_path.is_absolute():
            # harness lives beside the artifact (project_dir/metrics), not inside it
            harness_path = (artifact_dir.parent / harness).resolve()
        # forward-slash both the interpreter and the harness path so they survive
        # shlex.split(posix=True) intact on Windows (backslashes would be eaten there)
        py = str(getattr(sandbox, "python", sys.executable)).replace("\\", "/")
        harness_str = str(harness_path).replace("\\", "/")
        cmd = f"{shlex.quote(py)} -m pytest {shlex.quote(harness_str)} -q -p no:cacheprovider"
        res = sandbox.run(
            cmd, cwd=artifact_dir, timeout=timeout,
            env={"PYTHONPATH": str(Path(artifact_dir).resolve())},
        )
        out = (res.stdout or "") + (res.stderr or "")
        summary = _summary_line(res.stdout)
        passed = _count(_PASSED, summary)
        failed = _count(_FAILED, summary)
        errors = _count(_ERROR, summary)
        total = passed + failed + errors
        ok = total > 0
        pass_pct = (passed / total * 100.0) if total else 0.0
        data = {"pass_pct": pass_pct, "passed": float(passed), "total": float(total)}
        metrics = [
            {"name": m.name, "value": data.get(m.name, 0.0), "dir": m.dir, "weight": m.weight}
            for m in evaluation.metrics
        ]
        return MetricResult(metrics=metrics, logs=out, ok=ok, data=data)   # data: for constraints
