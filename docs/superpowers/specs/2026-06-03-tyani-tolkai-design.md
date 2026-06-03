# Тяни-Толкай (Push-Pull) — Adversarial Co-Evolution Agent Orchestrator

**Design Spec** · v3 · 2026-06-03

> v2 changelog: added the Agent Interaction Protocol (curated iteration brief +
> attempt history), Task Onboarding & Metric Adapters (built-in adapters + artifact
> seeding), Human-in-the-loop Checkpoints, Feedback-Quality Monitor, diff-to-validator
> + live stdout streaming, and a concrete `config.yaml` example. These came from a
> review through the lens of "the operator is also one of the agents and uses this to
> solve real tasks."
>
> v3 changelog (cross-AI review, `agy` + `opencode`): closed the **evaluation-harness
> visibility hole** (harness must be invisible to the Executor, not merely unwritable —
> §6 lock #4 + §7); the brief now carries the **real diff** of past attempts, not a prose
> summary (§4); the **Validator now sees the last rejected attempt + its own prior
> advice** (§4); the Scorer normalization is pinned to **per-metric target-range** (§6);
> added **noise-aware keep** and **baseline re-validation** (§11); **no-op ≠ plateau**
> (§6/§11); a light **oscillation detector** (§4); and a de-confounded feedback monitor (§11).

## 1. Purpose

A CLI-launched orchestrator with a WebUI that runs **two agents in opposition** to
drive a project toward maximum quality. One agent improves an artifact; the other
evaluates it. They iterate until quality plateaus or a target is reached. The
quality-improving artifact is the deliverable; the loop is the engine that hardens it.

This is a tool the operator uses to solve their own tasks, and the operator may *be*
one (or both) of the agents. So the agent's working experience — what it sees each
iteration, how it remembers past attempts — is a first-class design concern, not an
afterthought (see §4).

Conceptual lineage: GAN (generator vs discriminator), Karpathy's `autoresearch`
(modify → run → keep-if-better loop), and `consilium` (agent-name → CLI subprocess
adapter pattern). This project = `autoresearch` + a formal verifier + persistence/resume
+ adversarial capability + a real WebUI, reusing `consilium`'s adapter idea.

Two independent configuration axes:

1. **Competition mode** — *asymmetric* (Executor ↔ Validator, one project) or
   *symmetric* (Rival ↔ Rival, two adjacent projects).
2. **Agent engine per role** — each role is bound to an external agent CLI
   (`claude`, `codex`, `opencode`, `agy`). Any pairing, including identical engines.

## 2. Scope

### MVP (this spec drives implementation of)
- **Asymmetric mode only**: Executor (writeable) ↔ Validator (read-only).
- One agent pair; pre-run config (prompt + goal + task per role).
- **Agent interaction protocol**: fresh process per iteration + curated brief + attempt history (§4).
- **Task onboarding**: built-in metric adapters + artifact seeding (§7).
- Hill-climbing via git: keep iteration if score improved, else revert.
- **Deterministic Scorer** (scalar + keep/discard decided by code, not an LLM).
- Three-layer verdict: metrics vector + scalar + text feedback.
- **Human-in-the-loop checkpoints** (§10) + **feedback-quality monitor** (§11).
- Persistence + pause/stop/resume; crash-only state.
- Projects: create, switch, save, export, import, delete, rename, reset.
- WebUI with live stdout streaming (mandatory quality — see §12).
- **Docker sandbox backend** for executing artifact code (default), `local` fallback.

### Deferred — Phase 2 (after MVP is implemented and tested)
- **Symmetric adversarial mode** + **Arena** (shared test ground) +
  **champion archive / Hall of Fame** (each side scored against a *pool* of the
  opponent's past versions, to defeat co-evolution forgetting/cycling).
- Hold-out (hidden) metrics; multi-judge averaging.
- **Beam search**: `candidates_per_iteration > 1` (generate several variants, keep best
  — escapes local optima). MVP uses a single candidate (greedy hill-climb).
- Optional containerization of agents themselves.

## 3. Core concepts

**Roles**
- **Executor** — writes/edits the artifact. Runs with a *writeable* permission profile,
  working directory scoped to `artifact/`.
- **Validator (Verifier)** — read-only. Sees the **diff** + the objectively computed
  metrics (full artifact on demand), returns text feedback only. Never assigns the
  deciding number.

**Hard constraint — no bespoke agents.** Every role (Executor, Validator) is an
*off-the-shelf* CLI agent selected in config (`claude` / `codex` / `opencode` / `agy`).
We never write, fine-tune, or maintain a custom "agent" of our own — that would be one
more thing to debug and babysit. We build only **code we fully control**: Orchestrator,
Brief Builder, Metric Runner, metric adapters, Scorer, State Store, WebUI. The Validator
is nothing more than another configured CLI agent given a read-only profile and an
evaluation prompt; the Scorer/Metric Runner that produce the actual number are plain
code, not agents.

**Iteration loop (asymmetric):**
```
Orchestrator
  1. Brief Builder assembles iteration brief (§4) → Executor (writeable)  edits artifact/
  2. snapshot candidate (git); compute diff vs last kept version
  3. Metric Runner (independent, sandboxed run)     → objective metrics
  4. diff + metrics → Validator (read-only)         → text feedback
  5. Scorer(metrics)                                → scalar score + keep/discard
  6. keep → git commit ; discard → git revert
  7. write to State Store (state.db + git); append attempt to history
  8. checkpoint? (§10) — pause for human if triggered
  9. stop? (target_score / plateau_N / budget / max_iter) — else loop with new brief
```

**Three-layer verdict** (the central design inversion: *the number comes from code,
the LLM only advises*):
```json
{
  "metrics":  [{"name": "sharpe",  "value": 1.8,  "dir": "higher", "weight": 0.6},
               {"name": "max_dd",  "value": 0.12, "dir": "lower",  "weight": 0.4}],
  "score":    73.4,
  "verdict":  "keep",
  "feedback": "drawdown rose from over-leverage on trend; reduce position size"
}
```
- **metrics** — objective, from Metric Runner. Give the Executor *direction*.
- **score** — scalar 0–100, computed by deterministic Scorer. Normalization is pinned to
  **per-metric target-range**: each metric maps its config `worst → target` range onto
  `0 → 100` (clamped), so the zero-point is stable across iterations (no drifting z-score,
  no scale-domination from naive min-max). Then weight → aggregate. Weights and worst/target
  come from config (a business decision). Drives stop conditions and the chart.
- **feedback** — from the Validator LLM; influences only the *next* iteration's direction.

## 4. Agent interaction protocol

This is what makes convergence actually work. The naive options both fail: a fresh
agent process each iteration *forgets* everything and repeats mistakes; a single
`--continue` session *bloats* context, costs more each step, and lets the agent drift
in its own narrative. We take a third path.

**Fresh process per iteration + a curated "iteration brief".** The Orchestrator —
not the agent — owns memory. Each iteration the **Brief Builder** assembles a compact,
deterministic brief and passes it as the prompt:

```
ITERATION BRIEF  (iteration N)
- Goal: <role goal from config>
- Current best score: <best> (target: <target>)
- Last iteration: score <prev> → <curr>  (KEPT | REVERTED)
- Metrics now vs best: sharpe 1.6→1.8 ▲ , max_dd 0.10→0.12 ▼
- Validator feedback: "<text feedback from step 4>"
- Attempt history (last K), with the ACTUAL diff of each attempt:
    #N-1  KEPT   score 72→73
      diff: strategy.py L45  leverage = 0.05 → 0.03
            strategy.py L62  position = equity*0.05 → equity*0.03
      metrics: sharpe 1.6→1.8 ▲  max_dd 0.10→0.08 ▼
    #N-2  REVERT score 73→69
      diff: strategy.py L80  added trend filter (ema_fast>ema_slow)
      metrics: sharpe 1.8→1.5 ▼  max_dd 0.08→0.14 ▼
    #N-3  KEPT   score 70→72  (diff …)
- Oscillation flag: <set if a line is being changed back and forth>
- Live operator instructions: <contents of context/<role>.md, if any>
- Your task: make ONE focused change to improve the weighted score. Edit files in place.
```

Properties:
- **Attempt history carries the real diff** (not a prose summary) of the last K attempts,
  each annotated with its per-metric deltas. This is the single highest-leverage signal
  (cross-AI review): the data already lives in git + `state.db`, we just surface it. It
  approximates a *gradient* — the Executor sees *which lines changed → which metrics
  moved* — instead of reconstructing causality from one lossy sentence. A focused diff is
  ~5–15 lines, so K≈6 attempts ≈ 60 lines: bounded and cheaper than transcripts.
- **Oscillation detector** (light): the Brief Builder compares attempt diffs; if the same
  line is changed back and forth across attempts, it raises an `Oscillation flag` so the
  Executor stops seesawing between inversely-correlated metrics.
- The brief is **bounded** (last K attempts) → predictable cost; and **reproducible**:
  the same state yields the same brief (good for resume/tests).
- The agent edits files **in place** in `artifact/`; we read changes from git, not from
  the agent's stdout. Stdout is for *observability only* (streamed to the UI, §12).

**What the Validator sees:** the **diff** of the candidate vs the last kept version,
plus the computed metrics, plus the goal/rubric — and, crucially, **the last REJECTED
attempt's diff, its own prior advice, and why it was rejected** (metric delta). This turns
the Validator from a passive critic of the current code into a search director: it knows
"I advised the trend filter, it raised drawdown 5%, it was reverted — that path is wrong,
try another." Without this, a fresh Validator process re-sees the same reverted code and
re-issues the same failed advice (the State-Blind Validation Loop). Full artifact is
available on request, but diff-first keeps feedback focused and cheap.

**Why not `--continue`:** memory in the brief is curated and capped; a raw session is
neither. We may still use `--continue` as an *opt-in* per-adapter flag for engines where
a warm session measurably helps, but the brief remains the source of truth.

## 5. Components

| Component | Single responsibility |
|---|---|
| **CLI** `tyani-tolkai` | Entrypoint. Reads config, starts Orchestrator + WebUI, prints URL + password (from `.env`). |
| **Config** | Run definition: mode, agent pair, role prompts/goals, evaluation (metric adapter + weights + target), seeding, limits, checkpoint policy, history depth K. |
| **Adapter Registry** | `consilium`-style: agent name → CLI command + permission profile (writeable / read-only) + timeout + optional `--continue`. |
| **Brief Builder** | Assembles the curated iteration brief (§4) from state. Deterministic. |
| **Orchestrator** (core) | Async mediator loop. All communication flows through it. Owns checkpoints + feedback monitor. |
| **Metric Runner** | Independent, reproducible run computing objective metrics via a **metric adapter**. Executes inside the **sandbox backend**. Never trusts agent self-report. |
| **Metric Adapter** | Pluggable: turns a run into a metrics vector (built-ins in §7). |
| **Scorer** | Deterministic code: normalize + weighted-aggregate → scalar + keep/discard. |
| **Sandbox backend** | Abstraction over artifact-code execution: `local` or `docker`. |
| **State Store** | SQLite (`state.db`) + git (artifact versions). Source of truth for resume. |
| **WebUI + WebSocket** | Dashboard: config, live log + streamed agent stdout, evolution chart, steering + checkpoint controls. |

(Phase 2 adds **Arena** and **Champion Archive**.)

## 6. Evaluation mechanics & anti-collusion

Who computes what:

| Who | Type | Produces |
|---|---|---|
| Metric Runner | code | objective numbers (backtest, tests, etc.), deterministic |
| Scorer | code | scalar 0–100 + keep/discard |
| Validator | LLM (different provider) | text feedback only |

**Five anti-collusion locks:**
1. Scalar and keep/discard are deterministic code → an LLM cannot be "talked into" a score.
2. Metrics come from an independent run, not agent self-report → cannot be faked.
3. Validator ≠ Executor: different CLI/provider (config enforces; warn if identical).
4. **Harness invisibility** (not just unwritable): the evaluation harness — tests,
   `metrics/` adapter, fixtures, scoring data — must be **invisible** to the Executor,
   not merely outside its write scope. An agent games code it can *read*: if `tests/`
   or `backtest.py` sit inside `artifact/`, the Executor can weaken asserts, `@pytest.skip`,
   or hardcode favorable values — the metric stays "objective" while measuring a corrupted
   harness, converging to 100 on a worthless artifact. Defense: the harness lives in
   `metrics/` (outside `artifact/`) and is **injected into the sandbox at run time**
   (mounted/baked into the Docker image), with `artifact/` mounted **read-only**, so it
   never exists on the filesystem the Executor sees. Where a task genuinely needs tests
   inside the repo (TDD), scoring runs against a **hidden held-out copy** of the tests,
   not the ones the Executor can touch.
5. **Hold-out** (Phase 2): some metrics on hidden data → no overfitting to what's visible.

For tasks **without** objective metrics (e.g. an article): metric adapter = `rubric-llm`
(the judge is one of the configured CLI agents in a read-only profile, not a custom
agent) over fixed criteria (structure / argument / language); scalar = their weighted sum;
mitigate LLM-judge bias via different provider + fixed rubric (+ multi-judge in Phase 2)
+ human checkpoints (§10) for final sign-off.

**Edge cases:** artifact won't run → `score = fail` → discard + feedback with `stderr`.
**No-op ≠ plateau:** a zero-diff or comment/whitespace-only change is a *no-op*, tracked
by a separate `no_op_count`, NOT counted toward `plateau_N` — otherwise a lazy agent
editing comments triggers a false "plateau → finished" and exits without real work. No-op →
nudge the Executor ("make a substantive change"); only a *meaningful* change that fails to
improve the score counts toward the plateau.

## 7. Task onboarding, metric adapters & seeding

Setting up a *new* task must take minutes, not an afternoon. Three pieces.

**Built-in metric adapters** (pick one in config, or `custom`):

| Adapter | Metric it produces | Use case |
|---|---|---|
| `pytest-pass` | % of tests passing (+ count) | "make the code correct/complete" |
| `command-exit` | pass/fail from exit code + parsed stdout | any script with a clear success signal |
| `numeric` | named numbers parsed from JSON/stdout | optimization (Sharpe, latency, accuracy…) |
| `rubric-llm` | per-criterion scores from a read-only judge | text/article/design quality |
| `custom` | whatever a user script emits as `metrics.json` | anything bespoke |

**Metric adapter interface** (so a new one is a small plugin):
```
run(artifact_dir, sandbox) -> { "metrics": [ {name, value, dir}, ... ], "logs": "..." }
```
The Metric Runner invokes the adapter inside the sandbox; the Scorer applies config
weights. An adapter is the *only* thing most new tasks need to supply. All harness files
(adapter, fixtures, hidden tests) live in `metrics/` — outside `artifact/` and injected
into the sandbox at run time, so the Executor never sees them (lock #4).

**Optional agent-assisted bootstrap:** for a bespoke task, an optional one-off meta-step
can ask **one of the configured CLI agents** (never a bespoke agent of ours) to draft a
`custom` adapter / `metrics.json` producer from the goal.
The operator reviews it (it lives outside `artifact/`, so the Executor can't later edit
it — lock #4). MVP ships the built-ins; the bootstrap is a convenience.

**Artifact seeding** (config `seed:`): how `artifact/` starts.
- `empty` — agent generates from scratch per the goal.
- `copy: <path>` — copy an existing codebase in (the original is never touched).
- `generate` — a one-off agent run produces an initial version before the loop.

## 8. Agent Adapter Registry

Reuses the `consilium` mapping pattern (`bin/consult`). Each adapter declares the
invocation template and a permission profile.

| Agent | Invocation (sketch) | Notes |
|---|---|---|
| `claude` | `claude -p [-c] [--model M] [--add-dir DIR] "<prompt>"` | writeable via working dir |
| `codex` | `codex exec [--sandbox workspace-write\|read-only] [-m M] [-C DIR] "<prompt>"` | native sandbox flag |
| `opencode` | `opencode run [-c] [-m M] "<prompt>"` | |
| `agy` | `agy -p [-c] [--add-dir DIR] "<prompt>"` | Antigravity CLI; headless confirmed |

Permission profiles: **writeable** (Executor — working dir = `artifact/`) and
**read-only** (Validator). Per-call timeout (cf. `CONSILIUM_TIMEOUT`); `--continue`
is opt-in per adapter (see §4); stdout is streamed live and transcripts logged per run.

## 9. State model & projects

**Project = unit of isolation.** The artifact lives *inside* the project (Orchestrator
owns its git repo), never as a link to an external folder — this gives isolation,
safe keep/discard, and a self-contained export.

```
~/.tyani-tolkai/
  registry.db                 # project list + which is active
  projects/<project_id>/
    config.yaml               # mode, agents, roles, metric adapter, weights, seeding, limits, checkpoints
    state.db                  # iterations, scores, verdicts, attempts, commands, status
    artifact/                 # orchestrator-owned git repo of the artifact
    context/<role>.md         # live context files (program.md-style) for steering
    metrics/                  # metric adapter + fixtures (out of Executor's reach)
    logs/                     # per-run agent transcripts
```

**`state.db` (sketch):**
- `run` (id, status, mode, best_score, plateau_count, iter_count, cost_total, …)
- `iteration` (id, run_id, n, git_hash, score, verdict, change_summary, cost, duration, agent_exit, ts)
- `metric` (iteration_id, name, value, dir, weight)
- `command` (id, run_id, ts, text, applied_at_iter) — steering log
- `checkpoint` (id, run_id, iter, reason, decision, ts) — human-in-the-loop log

The brief's attempt history (§4) is reconstructed from `iteration.git_hash` (the **real
diff** vs its parent commit) + the `metric` rows (per-metric deltas) — never from a prose
summary. `change_summary` is kept only as an optional short human-readable label for the
UI/logs, not as a signal fed to the agent.

**Run state machine:** `idle → running → paused → running → finished`
(+ `stopped`, `error`, `awaiting_review`). Transitions are safe **at iteration
boundaries** (state consistent after step 7). Pausing mid-agent-run kills the subprocess
and discards the **uncommitted** candidate (nothing lost — we only "keep" after
evaluation + commit).

**Pause / stop / resume:** pause freezes status and frees processes; resume lifts from
the last checkpoint (`state.db` + `git HEAD`). Orphaned `running` status on startup →
safely recovered from the last completed iteration.

**Portability (other machine):** export = `tar` of `config.yaml` + `state.db` +
`git bundle` of the artifact + `context/` + `metrics/`. All **relative paths**, zero
absolute. Import = unpack into `projects/`, continue. **Secrets (WebUI password, agent
keys) are NOT in the bundle** — target machine has its own `.env`.

**Projects CRUD (UI + CLI):** create, switch active, save (snapshot tag),
export/import (bundle above), rename (label in `registry.db`; `project_id` immutable),
delete (remove dir + record), **reset** (`git reset` artifact to initial + clear
iterations, keep `config.yaml`).

**Live steering (no stop):** a command from the UI is appended to `context/<role>.md`
and injected into the next iteration's brief (§4). A stop command applies at the next
iteration boundary, never tearing a running step.

## 10. Human-in-the-loop checkpoints

The operator stays in control without babysitting. Config `checkpoints:` defines when
the run pauses into `awaiting_review` and surfaces the current artifact + score + diff:
- `on_plateau` — when `plateau_N` is hit (before declaring "finished").
- `every_n: <k>` — periodic review.
- `on_target` — when `target_score` is reached (final human sign-off — this is the
  "recognized by a human" requirement from the original idea).
- `manual` — operator hits "Review now" in the UI; applies at the next boundary.

At a checkpoint the operator chooses: **continue**, **accept & finish**,
**adjust goal/target/weights**, **edit rubric**, or **inject instruction** (→ steering).
Decisions are logged in `checkpoint`. Checkpoints are essential where the metric is
imperfect (e.g. article quality): the human is the final arbiter of "this is a 100".

## 11. Reliability & safety

**Agent run classification:** `success / timeout / rate-limited / crashed / no-op`.
- **Rate-limit / quota** → expected pause, not error: `paused(reason=limit)`, state saved,
  processes freed. Resume manually or with backoff (config). (Directly satisfies the
  "limits exhausted → stop → resume later" requirement.)
- **Timeout** (per run) → kill subprocess; candidate uncommitted → discard; retry or plateau++.
- **Crash** → log `stderr`, discard, retry N times, then pause.
- Invariant: any failure leaves state consistent (we keep only *after* evaluation + commit;
  uncommitted candidate is always safely discarded via `git checkout -- .`).

**Feedback-quality monitor.** The Validator's advice can mislead — but the Executor
*interprets* it, so good advice poorly executed looks like bad advice (the confounder).
We do NOT pretend code can judge "did the Executor truly follow the advice" — that needs
judgement, not a regex. So the monitor stays deliberately simple and hands the judgement
to the human: after M consecutive `discard` iterations with no score movement, it (a)
re-prompts the Validator once ("your guidance isn't moving the score; change approach"),
and if the stall continues, (b) raises a **checkpoint** (§10) where the operator — seeing
the diffs, the advice, and the score history — decides whether the Validator, the
Executor, the metric, or the goal is at fault. The monitor flags the stall; the human
de-confounds it. (A light heuristic — was the diff non-empty and in the area the feedback
named — only annotates the checkpoint, it does not auto-blame.)

**Sandbox backend (security of execution).**
- **Agents (Executor + Validator) run on the host** — their CLIs hold credentials in
  `~/.claude`, `~/.config`, etc. Containerizing them would mean injecting secrets inside.
  Executor only *edits files* — a safe operation.
- **Artifact-code execution (Metric Runner) runs in Docker** — this is the real risk
  (running agent-generated code). Default backend:
  `docker run --rm --network none --memory <M> --cpus <C> -v artifact:/work:ro …`.
  Gives isolation, reproducible environment (image pins dependencies → strengthens
  portability), and resource limits (= per-step budget).
- `local` backend = fallback for quick starts / Docker-less machines, with an explicit
  UI warning that no sandbox is active.

**Budgets — two axes:** wall-clock per step (autoresearch-style) and tokens/cost
(per step + per run; accumulated in state, shown in UI). Stop = `max_iterations` OR
`plateau_N` OR `budget_exceeded` OR `target_score`, whichever first. Budget breach → pause.

**Determinism & noise-aware keep:** fixed seed, fixed inputs (same data for a backtest);
for noisy metrics, several runs + median (count in config). Crucially, **keep only if the
improvement exceeds the measured noise band** (`min_delta` from config, or an estimated
std across the runs) — otherwise a lucky random spike gets committed as the new baseline
and *poisons* it: every correct future step then looks like a regression against the
inflated bar, and progress stalls. Optional **baseline re-validation**: periodically
(or on a long plateau) re-run the current best; if it no longer clears the bar, the
baseline was a noise outlier → lower it. Without this, "improvement" may be noise and
keep/discard becomes unstable.

**Crash-only state:** SQLite is ACID. Order: `git commit` (get hash) → write iteration
row with that hash in one DB transaction. Crash between them → on resume reconcile
`git HEAD` ↔ last iteration row; mismatch → roll back the uncertain step. Always recover
from the last consistent checkpoint.

## 12. WebUI / UX — mandatory non-functional requirement

First-class, **not** "bolt on later":
- Built using the **`frontend-design` skill**; deliberate UX/UI from the start.
- **Not overloaded:** clear information architecture
  (Projects → active run → evolution chart → details on demand). Essentials visible,
  detail on disclosure.
- **Large fonts and large elements:** comfortable for long observation sessions;
  controls easy to click; sufficient contrast.
- **Evolution-of-quality chart is the central element** of the run screen.
- **Live agent stdout streaming**: watch what the agent is doing *now* (edits, reasoning),
  not just the final result, via WebSocket.
- **Checkpoint panel**: when `awaiting_review`, surface artifact + score + diff and the
  decision controls (continue / accept / adjust / inject).
- A dedicated UI-design pass (frontend-design / ui-design) during the UI phase.

## 13. Tech stack

- **Python 3.10+**, `asyncio` (subprocess orchestration + cancellation + timeouts + stream capture).
- **FastAPI** + **WebSocket** for the WebUI and live updates.
- **SQLite** for state (single-file, portable, ACID).
- **git** for artifact versioning (commit/revert, `git bundle` for export).
- **Docker** for the default sandbox backend.
- Adapter pattern reused from `consilium`.

Rationale: matches the workload (long loop, subprocess orchestration, websocket
steering, durable resume, charts), fewest moving parts, aligned with `consilium`'s
Python/bash style, fastest to a working PoC.

## 14. Testing & validation

- **Unit:** Scorer (deterministic normalize/weight), Brief Builder (same state → same
  brief), Adapter Registry (command build), each metric adapter, State Store
  (write/resume), config validation.
- **Integration with a mock agent:** fake CLI returning preset edits → run the full
  loop without expensive real agents; assert keep/discard, persist, plateau, attempt
  history, checkpoints, all stop conditions.
- **Crash/resume:** kill mid-iteration → restart → consistency (`git HEAD` ↔ db).
- **Export/import:** export → import into a clean dir (simulated other machine) →
  continue → identical state.
- **Golden run (the honest proof the loop works):** a task with a known, measurable
  goal where quality *must* rise — e.g. "drive code from 3/10 to 10/10 unit tests"
  (`pytest-pass`) or numeric optimization with a known optimum. Run with a real agent;
  success = score rises monotonically (with hill-climbing) to target within a sane
  iteration count.
- **Anti-collusion test:** deliberately set Validator = Executor (same provider) and
  assert the score does **not** inflate without real metric movement (scalar is code).
- **No-op / regression test:** an agent doing nothing useful → score flat → plateau → clean stop.
- **Feedback-monitor test:** mock a misleading Validator → assert escalation fires.
- **Observability:** structured per-iteration log (brief, exit code, metrics, score,
  verdict, cost) for post-mortem. The UI chart is the primary "working/not" signal.

## 15. Example `config.yaml`

```yaml
project: trading-bot-sharpe
mode: asymmetric

agents:
  executor:  { engine: claude, model: opus,   timeout: 600 }
  validator: { engine: codex,  model: gpt-5,  timeout: 300 }   # different provider (lock #3)

roles:
  executor:
    goal: "Maximize risk-adjusted return of strategy.py."
    task: "Edit strategy.py only. One focused change per iteration."
  validator:
    goal: "Diagnose why the weighted score moved; give one concrete next step."

seed: { copy: ~/strategies/baseline }     # empty | copy:<path> | generate

evaluation:
  adapter: numeric                         # pytest-pass | command-exit | numeric | rubric-llm | custom
  command: "python backtest.py --json"     # emits metrics JSON to stdout
  metrics:                                  # worst→target maps to 0→100 (per-metric norm)
    - { name: sharpe, dir: higher, weight: 0.6, worst: 0.0, target: 2.5 }
    - { name: max_dd, dir: lower,  weight: 0.4, worst: 0.5, target: 0.05 }
  target_score: 95
  runs: 3                                   # median over 3 (noise control)
  min_delta: 1.0                            # keep only if score gain exceeds noise band
  harness_dir: metrics/                     # injected into sandbox; invisible to Executor (lock #4)

limits:    { max_iterations: 40, plateau_N: 8, budget_usd: 25, step_seconds: 600 }
history:   { depth_k: 6 }                   # attempts shown in the brief
checkpoints: { on_plateau: true, on_target: true, every_n: 10 }
sandbox:   { backend: docker, memory: 2g, cpus: 2, network: none }
```

## 16. Risks & open questions

- **Docker dependency** for the default sandbox backend (mitigated by `local` fallback).
- **Cost of real CLI runs** — bounded by `budget_usd` and the capped brief (§4).
- **`local` backend runs untrusted generated code on the host** — acceptable only as an
  explicit, warned fallback.
- **Metric design is the operator's real work** — built-in adapters lower this, but a
  bad metric still yields a well-optimized wrong thing. Human checkpoints (§10) catch it.
- Antigravity headless is provided via the `agy` adapter (already integrated in `consilium`).

## 17. Out of scope (MVP)

Symmetric adversarial mode, Arena, champion archive, hold-out metrics, multi-judge,
beam search, containerized agents — all Phase 2.
