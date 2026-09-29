#!/usr/bin/env python3
"""Judge for the fast-primes research kit. Runs with cwd = the artifact (solution.py).

Prints ONE JSON object:
  speedup      scored: reference time / candidate time, summed over 5 growing n around 200 000,
               each call on a FRESHLY loaded module (a cache kept between calls gains nothing);
               the ratio cancels the machine's speed
  correct_pct  constraint (= 100): exact output on hidden n values, edge cases included
  forbidden    constraint (= 0): imports outside a small stdlib set + open/exec/eval/compile/
               __import__ (no disk caches, no smuggled modules)
  source_kb    constraint (<= 8): no precomputed prime tables pasted into the source
Never crashes on a broken candidate: it prints the worst values instead.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import time

ALLOWED = {"math", "itertools", "array", "bisect", "functools"}
BANNED_CALLS = {"open", "exec", "eval", "compile", "__import__"}
HIDDEN_N = [0, 1, 2, 3, 4, 10, 97, 100, 1000, 7919, 65537, 99991, 123457]
BENCH_N = [200_000 + 997 * k for k in range(5)]     # growing: a cache for a smaller n can't answer


def reference(n: int) -> list[int]:          # plain sieve = ground truth AND the speed baseline
    if n < 2:
        return []
    sieve = bytearray([1]) * (n + 1)
    sieve[0:2] = b"\x00\x00"
    for p in range(2, int(n ** 0.5) + 1):
        if sieve[p]:
            sieve[p * p::p] = bytearray(len(range(p * p, n + 1, p)))
    return [i for i, v in enumerate(sieve) if v]


def load(src_path: pathlib.Path):
    """A fresh module object every time — nothing survives from one timed call to the next."""
    spec = importlib.util.spec_from_file_location("solution", src_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.primes_upto


def total_time(get_fn) -> float:
    total = 0.0
    for n in BENCH_N:
        fn = get_fn()
        t0 = time.perf_counter()
        fn(n)
        total += time.perf_counter() - t0
    return max(total, 1e-9)


def main() -> None:
    worst = {"speedup": 0.0, "correct_pct": 0.0, "forbidden": 99, "source_kb": 999.0}
    src_path = pathlib.Path("solution.py")
    if not src_path.is_file():
        print(json.dumps({**worst, "error": "solution.py missing"}))
        return
    src = src_path.read_text(encoding="utf-8")
    source_kb = round(len(src.encode("utf-8")) / 1024, 2)
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(json.dumps({**worst, "source_kb": source_kb, "error": f"syntax: {e}"}))
        return
    mods, calls = set(), 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            calls += 1
    forbidden = len(mods - ALLOWED) + calls
    if forbidden:                                        # never execute a candidate that breaks the rules
        print(json.dumps({**worst, "forbidden": forbidden, "source_kb": source_kb}))
        return
    try:
        fn = load(src_path)
        ok = sum(1 for n in HIDDEN_N if list(fn(n)) == reference(n))
        correct_pct = round(100.0 * ok / len(HIDDEN_N), 2)
        speedup = (total_time(lambda: reference) / total_time(lambda: load(src_path))
                   if correct_pct == 100 else 0.0)
    except Exception as e:                               # noqa: BLE001 — a broken candidate scores worst
        print(json.dumps({**worst, "forbidden": 0, "source_kb": source_kb,
                          "error": f"{type(e).__name__}: {e}"}))
        return
    print(json.dumps({"speedup": round(speedup, 2), "correct_pct": correct_pct,
                      "forbidden": 0, "source_kb": source_kb}))


if __name__ == "__main__":
    main()
