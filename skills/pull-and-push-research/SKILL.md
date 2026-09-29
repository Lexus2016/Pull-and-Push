---
name: pull-and-push-research
description: "Run a measurable research / optimization loop with the Pull-and-Push project (repo /Users/admin/_Projects/Тяни-Толкай): turn a goal into a research kit (research.yaml + seed/ + scorer/), try to break the judge, pre-flight it, run the executor↔reviewer loop, watch it, verify and report the result, and start the next round. Use whenever the user wants the BEST version of something that can be scored by a script — a trading strategy, faster or smaller code, a prompt, a config, SQL, text against a rubric — or says: проведи дослідження, знайди оптимальний / найкращий варіант, оптимізуй до цілі, пінг-понг, запусти Pull-and-Push / Тяни-Толкай, research loop, optimize until."
allowed-tools: Read, Write, Edit, Bash
---

# Pull-and-Push research

The engine optimizes ANY artifact against ANY deterministic scorer: an executor agent edits
`seed/`, the scorer grades each version with numbers, a version is kept only if its weighted score
beats the best AND it breaks no hard constraint, a reviewer agent explains and suggests the next
step. **It optimizes exactly what the scorer measures, loopholes included.** Most of your work is
the scorer; the loop is cheap to start and expensive to run on a bad judge.

## Where things are

- Repo: `/Users/admin/_Projects/Тяни-Толкай` (if moved: `readlink ~/.claude/skills/pull-and-push-research`).
- CLI: `PP="/Users/admin/_Projects/Тяни-Толкай/.venv/bin/pull-and-push"` (install: `./start.sh --check`).
- Projects: `~/.tyani-tolkai/projects/<name>/` (`artifact/` = the git repo being optimized,
  `metrics/` = the judge, `config.yaml`, `state.db`, `agent.log`). Saved kits: `~/.tyani-tolkai/research/`.
- Dashboard: the macOS app (Pull-and-Push.app) or `./start.sh` — humans watch here; it also has a
  wizard "🧪 Research from an idea" that produces the same kit format. Its address and token are
  in `~/.tyani-tolkai/dashboard.json` (0600) while it runs: the app picks the port and a random
  token, `$PP research start` reads the file by itself; `$PP url` prints the sign-in link.
- Machine check: `GET <url>/api/diagnostics?token=<token>` (agent CLIs + versions, scorer Python,
  git, update status) and `GET <url>/api/runs/events?since=<seq>&token=<token>` (runs that ended:
  status, reason, best vs target, cost) — cheaper than polling every project.
- A scorer that needs numpy/pandas: `{python}` follows Settings ▸ Python for scorers (or
  `PULL_AND_PUSH_PYTHON`); the macOS app's bundled Python has only the standard library.
- Worked example: `examples/research/fast-primes/` (speed with correctness/rules as constraints).
- Design: `docs/design/p8-research-kits.md`. Kit format: `docs/research-kits.md`.

## The kit

```
<kit>/research.yaml   name, goal, task, scorer, metrics, constraints, target_score, agents, limits, evaluation
<kit>/seed/           starting artifact — the only thing the executor may edit
<kit>/scorer/         the judge → <project>/metrics/ ; cwd = artifact, so it reads ../metrics/<data>
```
Scorer contract: prints ONE JSON object with every metric and constraint as a finite number (plus
report-only fields). Metrics map "the seed's value → target" onto 0..100: `research create`
pins each zero-point to the seed as measured by the pre-flight (never invent one; a hand-made
`worst` still wins). So "score 50" means half-way from where you started to the target. `constraints: [{name, min?, max?}]` are hard gates: a violating
candidate is never kept, whatever its score.

## Workflow

1. **Frame.** Goal in one sentence; the artifact (which files); 1–3 metrics with direction and a
   target that is ambitious but reachable; every "must never / must always" as a constraint; budget.
   Ask the human only what you cannot infer (where the data is, what "correct" means, spend
   ceiling). Otherwise decide and state the assumption.
2. **Scaffold.** `$PP research new <dir> --name <name>` (runnable generic skeleton), or copy the
   closest example. Write `seed/` (a minimal honest working version, not the optimum),
   `scorer/evaluate.py`, and `research.yaml`.
3. **Write the judge** (the part that decides everything):
   - stdlib only, deterministic (seed randomness; time with repeats and a ratio to a reference
     measured in the same run), fast (≪ step_seconds; aim < 30 s);
   - measure, never trust: execute / inspect the artifact, never read a number it reports about
     itself; keep reference answers and held-out data in `scorer/` only;
   - fail closed: a broken or missing artifact → exit 0 with the WORST values (+ an `error` field);
     don't execute a candidate that already breaks a rule (e.g. forbidden imports);
   - put correctness / rules / size limits in constraints, not in the weighted sum.
4. **Try to break your own judge before paying for a run.** Write 2–4 cheating seeds and run the
   scorer on each (`cd <copy>/seed && python ../scorer/evaluate.py`): hard-coded answers or a
   pasted lookup table, caching between repeated calls, wrong-but-fast output, deleting the work,
   forbidden imports / disk writes, printing fake numbers. Every cheat must score low or break a
   constraint. (fast-primes needed: fresh module per timed call, growing n, `source_kb` limit,
   banned `open/exec/eval`.) Fix and repeat.
5. **Pre-flight:** `$PP research check <dir>` until PASS; read every ⚠ (noisy → `evaluation.runs: 3`
   and a larger `min_delta`; no-headroom → raise the target; slow → raise `limits.step_seconds`).
6. **Create:** `$PP research create <dir> [--name <name>]`. First run small: `max_iterations` 3–5,
   a `budget_usd` (claude / grok report their own $; codex / agy / opencode only tokens, so give
   `usd_per_mtok` when one of those runs), executor and validator from different providers
   (claude / codex is the proven pair; opencode, agy and grok work too — agy since the v0.4.2 launch
   fix). Every agent reasons at `medium` by default — short steps, not one long think; raise
   one only for a reason (`{engine: claude, effort: high}`), and prefer more iterations to it.
7. **Start:** `$PP research start <name>` — it runs inside the dashboard when one is up (found via
   `dashboard.json`); it prints "no dashboard … running in the foreground" otherwise, so launch it
   with Bash `run_in_background: true`, or use `--foreground` explicitly. Either way the
   human can open the dashboard and watch (a command-line run is followed through its DB) and press
   **Stop** (it ends after the current iteration); Force Stop only works for dashboard runs.
8. **Watch, don't hover:** one iteration takes minutes. Use a Monitor poll loop (20–30 s) that
   emits one line per new iteration and exits when `status` ≠ running — run the JSON parsing with
   `/Users/admin/_Projects/Тяни-Толкай/.venv/bin/python` (a bare `python3` may be missing in the
   monitor's shell, which silently ends the watch). Stop early if every iteration fails with the
   same evaluation error (fix the kit): `touch ~/.tyani-tolkai/projects/<name>/stop.request` (a
   command-line run), or POST `<url>/api/projects/<name>/stop?token=<token>` with the url/token from
   `dashboard.json` (a dashboard run). `agent.log` shows what the executor did, one `▸ tool
   path` line per action; an `AGENT TIMEOUT … only reasoned` iteration means the step was too big or
   the effort too high — shrink the task before raising the timeout. `status --json` carries the
   real `cost_usd`, `tokens` and `cost_measured` (false = part of it was guessed or unpriced), and
   `outside` — iterations whose executor reached outside its folder (`⚠` in the log). Any
   `outside` > 0: read those iterations and treat a kept candidate after them as suspect.
9. **Verify before you believe:** `$PP report <name>` (seed → first kept → best → target per metric, kept steps,
   seed → best diff). Read the diff looking for gaming; re-run the scorer on the best artifact
   (`cd ~/.tyani-tolkai/projects/<name>/artifact && python ../metrics/evaluate.py`); if the
   metric allows, check it on data the loop never saw.
10. **Next round (research-level ping-pong):** `$PP research save <name> --to <dir> --source best`
    (continue from the best, e.g. with a higher target or an extra metric) or `--source seed` (the
    judge was gamed — fix it and start over). Then `research create <dir> --name <name>-v2`.
    To simply keep going with the same kit: raise `max_iterations` / target in `config.yaml` and
    start again — Run continues the same history.
11. **Report to the human** in their language: the goal; each metric start → best (target); the
    constraints held; iterations, stop reason and cost (say "≈" when `cost_measured` is false); the change that mattered (from
    the diff); caveats (noise, overfitting, what the judge does not measure); the next round you
    recommend.

## Pre-flight codes

| code | fix |
|---|---|
| `spec` | research.yaml invalid — the message names the field |
| `scorer` / `scorer-path` | no `scorer/`, or the command runs a file that isn't in it |
| `scorer-failed` | the judge crashed on the seed — make the seed runnable or the scorer fail-closed |
| `metric-missing` / `constraint-missing` | the JSON lacks the name, or the value is NaN/inf/non-number |
| `already-done` | the seed meets every target — raise targets or the scorer isn't measuring the seed |
| `noisy` ⚠ | values differ between two runs — seed the randomness or set `evaluation.runs` |
| `no-headroom` ⚠ | one metric is already at target — it adds a constant, not a signal |
| `slow` ⚠ | evaluation > half of `step_seconds` |
| `collusion` / `budget` ⚠ | same provider for both agents / no spend cap |

## Don'ts

- Don't let an LLM grade the artifact inside the loop; the helper agent may only DRAFT a scorer.
- Don't put secrets in `seed/` (the executor LLM reads it) or rely on network in the scorer.
- Don't edit `metrics/` of a project mid-run; change the kit and start a new round.
- Don't report a result you haven't verified in step 9.
