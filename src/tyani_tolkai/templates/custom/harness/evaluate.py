#!/usr/bin/env python3
"""Scoring harness (the JUDGE) for the 'custom / any task' template.

THE GENERAL CONTRACT — this is all the engine needs for ANY task:
  read the artifact you are optimizing, then print ONE JSON object whose "score" is a number
  (higher = better). The loop maximizes it. Keep this file OUTSIDE the artifact so the executor
  can't edit the judge.

    {"score": <0..100>, ...optional report-only fields...}

>>> REPLACE the example rubric below with how YOU measure success for your task. <<<
The artifact can be anything a script can read — text, code, a config, a prompt, a SQL query, a
CSV, a model file. Read it, compute a number, print it. That is the whole integration.

The example here is deliberately tiny, deterministic and NON-trading/NON-test (to show the engine
is general): it scores a short marketing tagline in solution.txt against a keyword/length/banned
rubric, so a freshly scaffolded project runs end-to-end before you write your real scorer.

Stdlib only. Usage: evaluate.py   (run with cwd = the artifact dir)
"""
from __future__ import annotations

import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent   # the metrics/ dir — put any reference data here


def load_artifact() -> str:
    # cwd is the artifact dir; read whatever file(s) you optimize. (Here: a single text file.)
    return pathlib.Path("solution.txt").read_text(encoding="utf-8").strip()


def evaluate(artifact: str) -> dict:
    # ---------------- EXAMPLE RUBRIC — replace this whole function for your task ----------------
    keywords = ["reusable", "steel", "insulated", "leakproof", "eco"]   # reward these ideas
    banned = ["very", "really", "just", "amazing", "best"]              # penalise filler/hype
    low = artifact.lower()
    hits = sum(1 for k in keywords if k in low)
    bans = sum(1 for b in banned if re.search(rf"\b{re.escape(b)}\b", low))
    n = len(artifact)
    length_fit = 1.0 if 40 <= n <= 120 else max(0.0, 1.0 - abs(n - 80) / 80.0)
    # weights sum to 100 so a perfect artifact can actually reach the top score (and the target)
    raw = 70.0 * (hits / len(keywords)) + 30.0 * length_fit - 10.0 * bans
    score = max(0.0, min(100.0, raw))
    return {
        "score": round(score, 2),          # <- the one required key (higher is better)
        # optional report-only fields (shown in the log / reviewer, not scored):
        "keyword_hits": hits,
        "length": n,
        "banned_used": bans,
    }
    # -------------------------------------------------------------------------------------------


def main() -> int:
    print(json.dumps(evaluate(load_artifact())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
