# Тяни-Толкай (Push-Pull) — Adversarial Co-Evolution Agent Orchestrator

**Design Spec** · v1 · 2026-06-03

## 1. Purpose

A CLI-launched orchestrator with a WebUI that runs **two agents in opposition** to
drive a project toward maximum quality. One agent improves an artifact; the other
evaluates it. They iterate until quality plateaus or a target is reached. The
quality-improving artifact is the deliverable; the loop is the engine that hardens it.

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
- Hill-climbing via git: keep iteration if score improved, else revert.
- **Deterministic Scorer** (scalar + keep/discard decided by code, not an LLM).
- Three-layer verdict: metrics vector + scalar + text feedback.
- Persistence + pause/stop/resume; crash-only state.
- Projects: create, switch, save, export, import, delete, rename, reset.
- WebUI (mandatory quality — see §9).
- **Docker sandbox backend** for executing artifact code (default), `local` fallback.

### Deferred — Phase 2 (after MVP is implemented and tested)
- **Symmetric adversarial mode** + **Arena** (shared test ground) +
  **champion archive / Hall of Fame** (each side scored against a *pool* of the
  opponent's past versions, to defeat co-evolution forgetting/cycling).
- Hold-out (hidden) metrics; multi-judge averaging.
- Optional containerization of agents themselves.

## 3. Core concepts

**Roles**
- **Executor** — writes/edits the artifact. Runs with a *writeable* permission profile,
  working directory scoped to `artifact/`.
- **Validator (Verifier)** — read-only. Sees the artifact + the objectively computed
  metrics, returns text feedback only. Never assigns the deciding number.

**Iteration loop (asymmetric):**
```
Orchestrator
  1. prompt = goal + last feedback + live context  → Executor (writeable)  edits artifact/
  2. snapshot candidate (git)
  3. Metric Runner (independent, sandboxed run)     → objective metrics
  4. artifact + metrics → Validator (read-only)     → text feedback
  5. Scorer(metrics)                                → scalar score + keep/discard
  6. keep → git commit ; discard → git revert
  7. write to State Store (state.db + git)
  8. stop? (target_score / plateau_N / budget / max_iter) — else feedback → next prompt
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
- **score** — scalar 0–100, computed by deterministic Scorer (normalize → weight → aggregate).
  Weights come from config (a business decision). Drives stop conditions and the chart.
- **feedback** — from the Validator LLM; influences only the *next* iteration's direction.

## 4. Components

| Component | Single responsibility |
|---|---|
| **CLI** `tyani-tolkai` | Entrypoint. Reads config, starts Orchestrator + WebUI, prints URL + password (from `.env`). |
| **Config** | Run definition: mode, agent pair, role prompts/goals, evaluation (metrics + weights + target), limits (max_iter, plateau_N, per-step budget), artifact path. |
| **Adapter Registry** | `consilium`-style: agent name → CLI command + permission profile (writeable / read-only) + timeout + `--continue`. |
| **Orchestrator** (core) | Async mediator loop. All communication flows through it. |
| **Metric Runner** | Independent, reproducible run computing objective metrics. Executes inside the **sandbox backend**. Never trusts agent self-report. |
| **Scorer** | Deterministic code: normalize + weighted-aggregate → scalar + keep/discard. |
| **Sandbox backend** | Abstraction over artifact-code execution: `local` or `docker`. |
| **State Store** | SQLite (`state.db`) + git (artifact versions). Source of truth for resume. |
| **WebUI + WebSocket** | Dashboard: config, live log, evolution chart, steering channel. |

(Phase 2 adds **Arena** and **Champion Archive**.)

## 5. Evaluation mechanics & anti-collusion

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
4. **Editable-scope**: Executor may write only inside `artifact/`; Metric Runner,
   tests, and Scorer are out of reach → cannot tamper with its own metric.
5. **Hold-out** (Phase 2): some metrics on hidden data → no overfitting to what's visible.

For tasks **without** objective metrics (e.g. an article): Metric Runner = rubric scorer
over fixed criteria (structure / argument / language); scalar = their weighted sum;
mitigate LLM-judge bias via different provider + fixed rubric (+ multi-judge in Phase 2).

**Edge cases:** artifact won't run → `score = fail` → discard + feedback with `stderr`;
Executor changed nothing → no-op, counts toward plateau; metrics unchanged → plateau++.

## 6. Agent Adapter Registry

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
keeps an agent's session across iterations; transcripts logged per run.

## 7. State model & projects

**Project = unit of isolation.** The artifact lives *inside* the project (Orchestrator
owns its git repo), never as a link to an external folder — this gives isolation,
safe keep/discard, and a self-contained export.

```
~/.tyani-tolkai/
  registry.db                 # project list + which is active
  projects/<project_id>/
    config.yaml               # mode, agents, roles, metrics, weights, limits
    state.db                  # iterations, scores, verdicts, commands, status
    artifact/                 # orchestrator-owned git repo of the artifact
    context/<role>.md         # live context files (program.md-style) for steering
    logs/                     # per-run agent transcripts
```

**`state.db` (sketch):**
- `run` (id, status, mode, best_score, plateau_count, iter_count, cost_total, …)
- `iteration` (id, run_id, n, git_hash, score, verdict, cost, duration, agent_exit, ts)
- `metric` (iteration_id, name, value, dir, weight)
- `command` (id, run_id, ts, text, applied_at_iter) — steering log

**Run state machine:** `idle → running → paused → running → finished`
(+ `stopped`, `error`). Transitions are safe **at iteration boundaries** (state is
consistent after step 7). Pausing mid-agent-run kills the subprocess and discards the
**uncommitted** candidate (nothing lost — we only "keep" after evaluation + commit).

**Pause / stop / resume:** pause freezes status and frees processes; resume lifts from
the last checkpoint (`state.db` + `git HEAD`). Orphaned `running` status on startup →
safely recovered from the last completed iteration.

**Portability (other machine):** export = `tar` of `config.yaml` + `state.db` +
`git bundle` of the artifact + `context/`. All **relative paths**, zero absolute.
Import = unpack into `projects/`, continue. **Secrets (WebUI password, agent keys)
are NOT in the bundle** — target machine has its own `.env`.

**Projects CRUD (UI + CLI):** create, switch active, save (snapshot tag),
export/import (bundle above), rename (label in `registry.db`; `project_id` immutable),
delete (remove dir + record), **reset** (`git reset` artifact to initial + clear
iterations, keep `config.yaml`).

**Live steering (no stop):** a command from the UI is appended to `context/<role>.md`
and injected into the next iteration's prompt. A stop command applies at the next
iteration boundary, never tearing a running step.

## 8. Reliability & safety

**Agent run classification:** `success / timeout / rate-limited / crashed / no-op`.
- **Rate-limit / quota** → expected pause, not error: `paused(reason=limit)`, state saved,
  processes freed. Resume manually or with backoff (config). (Directly satisfies the
  "limits exhausted → stop → resume later" requirement.)
- **Timeout** (per run) → kill subprocess; candidate uncommitted → discard; retry or plateau++.
- **Crash** → log `stderr`, discard, retry N times, then pause.
- Invariant: any failure leaves state consistent (we keep only *after* evaluation + commit;
  uncommitted candidate is always safely discarded via `git checkout -- .`).

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

**Determinism of Metric Runner:** fixed seed, fixed inputs (same data for a backtest);
for noisy metrics, several runs + median (count in config). Without this, "improvement"
may be noise and keep/discard becomes unstable.

**Crash-only state:** SQLite is ACID. Order: `git commit` (get hash) → write iteration
row with that hash in one DB transaction. Crash between them → on resume reconcile
`git HEAD` ↔ last iteration row; mismatch → roll back the uncertain step. Always recover
from the last consistent checkpoint.

## 9. WebUI / UX — mandatory non-functional requirement

First-class, **not** "bolt on later":
- Built using the **`frontend-design` skill**; deliberate UX/UI from the start.
- **Not overloaded:** clear information architecture
  (Projects → active run → evolution chart → details on demand). Essentials visible,
  detail on disclosure.
- **Large fonts and large elements:** comfortable for long observation sessions;
  controls easy to click; sufficient contrast.
- **Evolution-of-quality chart is the central element** of the run screen.
- A dedicated UI-design pass (frontend-design / ui-design) during the UI phase.

## 10. Tech stack

- **Python 3.10+**, `asyncio` (subprocess orchestration + cancellation + timeouts).
- **FastAPI** + **WebSocket** for the WebUI and live updates.
- **SQLite** for state (single-file, portable, ACID).
- **git** for artifact versioning (commit/revert, `git bundle` for export).
- **Docker** for the default sandbox backend.
- Adapter pattern reused from `consilium`.

Rationale: matches the workload (long loop, subprocess orchestration, websocket
steering, durable resume, charts), fewest moving parts, aligned with `consilium`'s
Python/bash style, fastest to a working PoC.

## 11. Testing & validation

- **Unit:** Scorer (deterministic normalize/weight), Adapter Registry (command build),
  State Store (write/resume), config validation.
- **Integration with a mock agent:** fake CLI returning preset edits → run the full
  loop without expensive real agents; assert keep/discard, persist, plateau, all stop
  conditions.
- **Crash/resume:** kill mid-iteration → restart → consistency (`git HEAD` ↔ db).
- **Export/import:** export → import into a clean dir (simulated other machine) →
  continue → identical state.
- **Golden run (the honest proof the loop works):** a task with a known, measurable
  goal where quality *must* rise — e.g. "drive code from 3/10 to 10/10 unit tests" or
  numeric optimization with a known optimum. Run with a real agent; success = score
  rises monotonically (with hill-climbing) to target within a sane iteration count.
- **Anti-collusion test:** deliberately set Validator = Executor (same provider) and
  assert the score does **not** inflate without real metric movement (scalar is code).
- **No-op / regression test:** an agent doing nothing useful → score flat → plateau → clean stop.
- **Observability:** structured per-iteration log (prompt, exit code, metrics, score,
  verdict, cost) for post-mortem. The UI chart is the primary "working/not" signal.

## 12. Risks & open questions

- **Docker dependency** for the default sandbox backend (mitigated by `local` fallback).
- **Cost of real CLI runs** — 20 iterations × 2 roles × large prompts; budgets are
  first-class to bound this.
- **`local` backend runs untrusted generated code on the host** — acceptable only as an
  explicit, warned fallback.
- Antigravity headless is provided via the `agy` adapter (already integrated in `consilium`).

## 13. Out of scope (MVP)

Symmetric adversarial mode, Arena, champion archive, hold-out metrics, multi-judge,
containerized agents — all Phase 2.
