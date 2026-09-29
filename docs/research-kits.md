# Research kits — any experiment you can measure

A research kit is a folder that fully describes one experiment. The dashboard wizard
("🧪 Research from an idea") produces one for you; you can also write it by hand or have an agent
write it (the Claude Code skill in `skills/pull-and-push-research/` does exactly that).

```
my-research/
  research.yaml   what we want, how it is judged, when to stop
  seed/           the starting version — the only thing the executor edits
  scorer/         the judge (+ any reference data / hidden tests), invisible to the executor
```

## research.yaml

```yaml
name: fast-primes                      # becomes the project name
goal: Make primes_upto(n) as fast as possible while staying exactly correct.
task: Edit only solution.py. Standard library only. No precomputed tables.
scorer: "{python} ../metrics/evaluate.py"   # runs with cwd = the artifact; the judge is ../metrics
metrics:                               # scored: the seed's value → target maps onto 0..100
  - {name: speedup, dir: higher, weight: 1.0, target: 3}
constraints:                           # hard gates: a violating version is never kept
  - {name: correct_pct, min: 100}
  - {name: source_kb, max: 8}
target_score: 90                       # composite score that counts as done
agents: {executor: claude, validator: codex}   # different providers; effort is medium unless {engine: claude, effort: high}
limits: {max_iterations: 12, plateau_N: 4, step_seconds: 300, budget_usd: 3, usd_per_mtok: 3}
evaluation: {runs: 1, min_delta: 2}    # runs > 1 = median of N (noisy judges)
```

Optional: `review` (the reviewer's brief), `adapter` (`numeric` default, `command-exit`,
`pytest-pass` with the tests in `scorer/`), `checkpoints`, `sandbox`, `notify` — as in `config.yaml`.

## The judge (scorer/)

It prints **one JSON object**: every metric and constraint as a finite number, plus any report-only
fields. Rules that decide whether the result means anything:

- **Deterministic and fast** — seed randomness; time with repeats against a reference measured in
  the same run; stay well under `step_seconds`.
- **Measure, never trust** — run or inspect the artifact; never read a number it reports about
  itself. Keep answers and held-out data inside `scorer/`.
- **Fail closed** — a broken artifact prints the worst values and exits 0.
- **Constraints for rules** — correctness, allowed imports, size limits are gates, not weights.
- **Try to cheat it yourself first** — hard-coded answers, caches between repeated calls, fake
  numbers, forbidden imports: each must score low or break a constraint.

## Commands

```bash
pull-and-push research new    my-research --name my-research   # runnable skeleton to edit
pull-and-push research check  my-research                      # pre-flight: runs the judge twice
pull-and-push research create my-research                      # → project (pre-flight must pass)
pull-and-push research start  my-research                      # in the running dashboard, else here
pull-and-push status  my-research                              # compact state (--json)
pull-and-push report  my-research                              # start → best → target, kept steps, diff
pull-and-push research save   my-research --to next --source best   # next round from the best
```

`research create` pins each metric's zero-point to the seed's value measured by the pre-flight, so
"score 50" means half-way from where you started to the target. A run started from the command
line shows up in the dashboard (followed through its database) and its **Stop** works there.

The pre-flight refuses a kit whose judge crashes on the seed, misses a metric or constraint, prints
NaN, or already meets every target; it warns about a noisy or slow judge, a metric with no headroom,
same-provider agents and a missing budget cap.
