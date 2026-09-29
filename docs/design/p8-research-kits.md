# P8 — Research kits: any experiment, from an idea to a running loop

**Status:** implemented (2026-09-29) · **Supersedes in the UI:** "🪄 Generate from a description"

## Problem

The engine is domain-agnostic, but only three vetted templates produce a *runnable* project. The
"Generate from a description" path drafts a `config.yaml` whose eval command names a scorer nobody
writes, so project creation is (correctly) refused — a dead end for every new research direction.
There was also no way to say "optimize X, but Y must never break", and no compact, scriptable way
for an agent (Claude Code) to create, run, watch and read a research loop.

## Decision

One unit of work — the **research kit** — used identically by a human (dashboard wizard) and by an
agent (CLI):

```
my-research/
  research.yaml   # the spec: goal, what may be edited, criteria, hard constraints, target, budget
  seed/           # the starting artifact — the ONLY thing the executor edits
  scorer/         # the judge: copied to <project>/metrics/, outside the artifact, invisible to it
    evaluate.py   # reads the artifact (cwd), prints ONE JSON object of named numbers
```

A kit is a user-owned template: `research create` turns it into a project; `research save` turns a
project (its best result or its seed) back into a kit to modify and run again. Kits made by the
wizard live in `~/.tyani-tolkai/research/<name>/`.

### Hard constraints (new, core)

`evaluation.constraints: [{name, min?, max?}]`. `name` is any number the scorer prints — a scored
metric or a report-only field. A candidate violating any constraint is **never kept** (verdict
`fail`, the violation is the executor's feedback), whatever its score. This is what makes
"faster, but correctness stays 100 %" expressible; a weighted sum alone would trade the one for the
other. Constraint-violating rows are not re-scored on a re-baseline.

### Pre-flight check (the judge is checked before any agent is paid)

`research check` stages the project layout in a temp dir and runs the scorer on the seed **twice**:
it must exit 0, print a JSON object with every scored metric and constraint as a finite number, and
give the same values both times (else: noisy → `evaluation.runs`). It reports the eval time against
`step_seconds`, metrics with no headroom (seed already at target), a seed that already meets
`target_score` (mis-specified objective), constraints the seed violates, same-provider
executor/validator, and a missing budget cap. Errors block `create`.

### Scale zero = the seed

Left alone, the loop pins each metric's zero-point to the first measured *candidate*. Dogfooding
showed the failure: the executor's first edit landed at 2.91× against a 3× target, so the whole
0..100 scale spanned 2.91–3.00 and timing noise alone moved the score by tens of points.
`research create` therefore pins `worst` to the seed's value from the pre-flight (an explicit
`worst` still wins) and keeps the pre-flight report in `research/preflight.json` for `report`.

### Command-line runs and the dashboard

A CLI run writes `run.pid` for its lifetime. The dashboard then does not "heal" the project to
stopped at startup, refuses a second run and delete/reset/rename, follows the run through the DB,
and its **Stop** drops `stop.request`, which the CLI loop polls between iterations (Force Stop is
refused — the process isn't the server's to kill).

### Agent-facing CLI

| command | purpose |
|---|---|
| `research new DIR` | kit skeleton (commented spec, placeholder seed + scorer) |
| `research check DIR [--json]` | pre-flight (above) |
| `research create DIR [--name N]` | check + create the project (kit copied to the project for provenance) |
| `research start NAME [--url]` | start via the running dashboard (live for the human), else run in the foreground |
| `research save NAME DIR [--from best\|seed]` | project → kit (the next research round) |
| `status NAME [--json] [--last N]` | compact state: status, best, iterations, plateau, cost, last N |
| `report NAME [--json]` | best vs seed vs target per metric, kept history, seed→best diff |

### Wizard (dashboard, for humans)

① idea → the helper agent returns clarifying questions + a draft of criteria; ② answers → it drafts
the whole kit (spec + scorer + seed), shown editable — the scorer is the judge, so it is displayed
for review; ③ pre-flight; ④ create → Run. Same kit format, same `check`/`create` as the CLI.

## Borrowed / not borrowed

- From Karpathy's *autoresearch*: a human-authored research program + a fixed evaluation + a fixed
  per-experiment time budget + keep-if-better in git. We keep the judge hidden and deterministic
  (the agent never sees or edits it) and gate it with a pre-flight instead of trusting it.
- From the existing templates: the `seed/` + harness layout and the scorer contract (one JSON
  object on stdout). A kit is simply a template that lives outside the package.
- Not borrowed: LLM-as-judge. The helper agent may *draft* a scorer, but it is reviewed, checked,
  and then fixed for the whole run — never consulted per iteration.
