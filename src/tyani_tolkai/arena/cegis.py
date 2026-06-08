"""CEGIS toy domain (design §7): A recognizes a hidden language L; B probes for
misclassifications; the referee is the ORACLE that labels counterexamples. Fully
deterministic — no LLM, no API — so it can prove the co-evolution machinery converges.

Target language L = "s contains the substring 'ab'" over the alphabet {a, b}.
Side A's hypothesis class is "contains substring t"; the learner finds the shortest t
consistent with all oracle-labeled examples, which converges to t = 'ab'.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

from .referee import MatchOutcome, register_referee

TARGET_SUBSTRING = "ab"
_MAX_PROBES = 512


def in_L(s: str) -> bool:
    """Ground-truth L: s contains the substring 'ab'. Known ONLY to the referee/oracle."""
    return TARGET_SUBSTRING in s


def _candidates(alphabet: str, max_len: int):
    for length in range(1, max_len + 1):
        for tup in itertools.product(alphabet, repeat=length):
            yield "".join(tup)


def min_consistent_substring(examples, alphabet: str = "ab", max_len: int = 4) -> str | None:
    """Shortest substring t such that ('t in s') agrees with every (s, label) example.

    Deterministic tie-break: shortest first, then lexicographic (the order ``_candidates``
    yields). Returns None if no substring up to ``max_len`` is consistent.
    """
    examples = list(examples)
    for t in _candidates(alphabet, max_len):
        if all((t in s) == bool(label) for s, label in examples):
            return t
    return None


@register_referee
class CegisReferee:
    """Deterministic, sandboxed, oracle-graded match engine for the CEGIS toy."""

    name = "cegis-recognizer"
    version = "1"
    opponent_strategy = "accumulate"     # A scored vs the GROWING union of B's probes (design §3)

    def label(self, strings) -> list[dict]:
        """Oracle/teacher channel: label each string by the ground-truth L. Used by the
        SymmetricOrchestrator to build side A's accumulated counterexample context (design §7).
        Returns ``[{"s": str, "label": bool}, ...]`` deduped, in first-seen order."""
        seen: set[str] = set()
        out: list[dict] = []
        for s in strings:
            if s not in seen:
                seen.add(s)
                out.append({"s": s, "label": in_L(s)})
        return out

    def _probe_strings(self, b_dir: Path) -> list[str]:
        path = b_dir / "strings.txt"
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        seen: set[str] = set()
        out: list[str] = []
        for line in raw.splitlines():
            s = line.rstrip("\n")
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out[:_MAX_PROBES]

    def play(self, a_dir, b_dir, sandbox, *, seed: int) -> MatchOutcome:
        a_dir = Path(a_dir)
        b_dir = Path(b_dir)
        probes = self._probe_strings(b_dir)
        if not probes:
            # no probes ⇒ B exerts no pressure; A trivially "wins" the empty match
            return MatchOutcome.zero_sum(1.0, {"counterexamples": [], "n": 0})

        # INTEGRITY: feed the probes via STDIN and consume them BEFORE importing the recognizer,
        # so side A's (untrusted) code can never read the opponent's test set — nothing is written
        # into A's artifact dir, and the runner drains stdin before A's module-level code runs.
        # A's `accepts` only ever sees one string at a time. (Referee = the trust anchor.)
        runner = (
            "import json, sys\n"
            "probes = json.load(sys.stdin)\n"          # drain stdin first — A can't grab it later
            "sys.path.insert(0, '.')\n"
            "import recognizer\n"
            "print(json.dumps([bool(recognizer.accepts(s)) for s in probes]))\n"
        )
        (a_dir / "_arena_runner.py").write_text(runner, encoding="utf-8")
        py = str(getattr(sandbox, "python", sys.executable)).replace("\\", "/")
        res = None
        try:
            res = sandbox.run(f"{py} _arena_runner.py", cwd=a_dir, timeout=60,
                              input=json.dumps(probes))
            verdicts = json.loads(res.stdout) if res.exit_code == 0 else None
        except (json.JSONDecodeError, TypeError, ValueError):
            verdicts = None
        finally:
            (a_dir / "_arena_runner.py").unlink(missing_ok=True)

        if verdicts is None or len(verdicts) != len(probes):
            # A's recognizer crashed / produced garbage → B wins this match, but still hand A the
            # full oracle labels so it can repair next generation.
            counter = [{"s": s, "label": in_L(s)} for s in probes]
            err = (res.stderr or "")[:500] if res is not None else "no result"
            return MatchOutcome.zero_sum(0.0, {"counterexamples": counter, "n": len(probes),
                                               "error": err})

        correct, counter = 0, []
        for s, v in zip(probes, verdicts):
            truth = in_L(s)
            if bool(v) == truth:
                correct += 1
            else:
                counter.append({"s": s, "label": truth})   # ORACLE-labeled counterexample for A
        a_score = correct / len(probes)
        return MatchOutcome.zero_sum(a_score, {"counterexamples": counter, "n": len(probes)})
