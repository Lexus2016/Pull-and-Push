"""Deterministic scripted rivals for the CEGIS toy — stand-ins for LLM agents in tests and the
convergence proof. They read opponent context from ``workdir/.arena/`` (materialized by the
SymmetricOrchestrator) and edit the artifact. No LLM, no API, fully reproducible.

A real claude/codex rival is a drop-in replacement: it reads the same ``.arena/`` facts via the
brief prose and edits the same artifact files.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from ..arena.cegis import min_consistent_substring
from .base import RunResult


class ScriptedRecognizerRival:
    """Side A: fit the shortest substring rule consistent with all oracle-labeled counterexamples
    seen so far. With no examples yet it writes accept-all (``'' in s`` is always True) — a
    deliberately wrong seed so the arms race has somewhere to go."""

    def run(self, brief, workdir, profile, timeout) -> RunResult:
        workdir = Path(workdir)
        ce_path = workdir / ".arena" / "counterexamples.json"
        examples = json.loads(ce_path.read_text(encoding="utf-8")) if ce_path.exists() else []
        pairs = [(e["s"], bool(e["label"])) for e in examples]
        t = min_consistent_substring(pairs, alphabet="ab", max_len=4) if pairs else ""
        if t is None:                      # inconsistent examples (shouldn't happen) → widen search
            t = min_consistent_substring(pairs, alphabet="ab", max_len=6) or ""
        body = f"def accepts(s):\n    return {t!r} in s\n"
        target = workdir / "recognizer.py"
        old = target.read_text(encoding="utf-8") if target.exists() else None
        target.write_text(body, encoding="utf-8")
        return RunResult(status="success" if body != old else "no_op",
                         stdout=f"fit t={t!r}", changed=body != old)


class ScriptedAdversaryRival:
    """Side B: deterministically enumerate all {a,b} strings up to ``max_len`` (incl. "") as
    probes. The full enumeration necessarily contains L's boundary cases; bounded + reproducible."""

    def __init__(self, max_len: int = 4):
        self.max_len = max_len

    def run(self, brief, workdir, profile, timeout) -> RunResult:
        workdir = Path(workdir)
        probes = [""]
        for length in range(1, self.max_len + 1):
            probes += ["".join(p) for p in itertools.product("ab", repeat=length)]
        body = "\n".join(probes) + "\n"
        target = workdir / "strings.txt"
        old = target.read_text(encoding="utf-8") if target.exists() else None
        target.write_text(body, encoding="utf-8")
        return RunResult(status="success" if body != old else "no_op",
                         stdout=f"{len(probes)} probes", changed=body != old)
