# Тяни-Толкай — Implementation Plan (MVP, asymmetric)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline) —
> this plan is executed task-by-task in-session. Steps use `- [ ]` checkboxes.
> Spec: `docs/superpowers/specs/2026-06-03-tyani-tolkai-design.md` (v3).

**Goal:** A CLI that runs an asymmetric Executor↔Validator loop with a deterministic
Scorer and git hill-climbing, proven by a golden run where score rises to target.

**Architecture:** Python `asyncio` orchestrator drives off-the-shelf CLI agents as
subprocesses (mediator pattern). State = SQLite + git. Scorer/Metric Runner are plain
code (never agents). Pluggable adapters for agents and metrics.

**Tech Stack:** Python 3.10+, asyncio, SQLite (`sqlite3`), git (subprocess), pydantic
(config), pytest. (FastAPI/Docker arrive in later phases.)

---

## Phasing (each phase = working, testable software)

- **Phase 1 — Core loop (THIS = first working solution).** Package, config, state
  (SQLite+git), adapter registry + mock agent, brief builder, scorer, metric runner
  (local backend; `numeric`/`command-exit`/`pytest-pass`), orchestrator (hill-climb,
  no-op/plateau/stop), CLI `run`. **Golden run on mock agent passes.**
- **Phase 2 — Real agents & hardening.** CLI adapters (`claude`/`codex`/`opencode`/`agy`),
  Docker sandbox, Validator role + "sees rejected attempt", noise-aware keep,
  oscillation detector, human checkpoints, pause/resume, rate-limit handling.
- **Phase 3 — Product surface.** FastAPI + WebSocket WebUI (evolution chart, live
  stdout, checkpoint panel), projects CRUD, export/import.

This document details **Phase 1** fully; Phases 2–3 are outlined at the end and will be
expanded into their own plans once Phase 1 is green.

---

## File structure (Phase 1)

```
pyproject.toml
src/tyani_tolkai/
  __init__.py
  config.py          # Config models + load/validate (pydantic)
  state.py           # StateStore: SQLite schema + git init/snapshot/commit/revert/diff
  scorer.py          # Scorer: per-metric target-range norm -> scalar + keep/discard
  registry.py        # AdapterRegistry: name -> AgentAdapter
  agents/
    __init__.py
    base.py          # AgentAdapter protocol (run(brief, workdir, profile) -> RunResult)
    mock.py          # MockAdapter: scripted edits (tests + golden run)
  metrics/
    __init__.py
    base.py          # MetricAdapter protocol (run(artifact, sandbox) -> MetricResult)
    numeric.py       # parse named numbers from JSON stdout
    command_exit.py  # pass/fail from exit code
    pytest_pass.py   # % tests passing
  brief.py           # BriefBuilder: assemble iteration brief from state (diff + history)
  sandbox.py         # SandboxBackend: LocalBackend (Phase 1); DockerBackend (Phase 2)
  orchestrator.py    # Orchestrator.run_iteration / run_loop (async)
tests/
  test_config.py  test_state.py  test_scorer.py  test_metrics.py
  test_registry_mock.py  test_brief.py  test_orchestrator.py
  test_golden_run.py
  fixtures/golden/   # broken code + hidden tests for the golden run
```

Boundaries: each module one responsibility; Scorer is pure (easy to test);
Orchestrator wires the others and owns the loop/stop logic.

---

## Phase 1 tasks

### Task 1 — Project scaffold
**Files:** Create `pyproject.toml`, `src/tyani_tolkai/__init__.py`, `tests/__init__.py`.
- [ ] `pyproject.toml`: project name `tyani-tolkai`, py>=3.10, deps `pydantic`,
  dev-deps `pytest`; console_script `tyani-tolkai = tyani_tolkai.cli:main`; configure
  `pytest` testpaths.
- [ ] `pip install -e ".[dev]"`; run `pytest -q` → 0 tests, exit 0.
- [ ] Commit: `chore: project scaffold`.

### Task 2 — Config models (`config.py`)
**Responsibility:** load+validate `config.yaml` into typed models.
- [ ] Models: `AgentCfg(engine,model,timeout)`, `RoleCfg(goal,task)`,
  `MetricCfg(name,dir:Literal['higher','lower'],weight,worst,target)`,
  `EvaluationCfg(adapter,command,metrics,target_score,runs,min_delta,harness_dir)`,
  `LimitsCfg(max_iterations,plateau_N,budget_usd,step_seconds)`,
  `Config(project,mode,agents,roles,seed,evaluation,limits,history,checkpoints,sandbox)`.
- [ ] `load_config(path) -> Config` (yaml + pydantic validation).
- [ ] Validation rules: weights sum > 0; `dir` in {higher,lower}; mode == 'asymmetric'
  (Phase 1); warn if executor.engine == validator.engine (lock #3).
- [ ] Tests (`test_config.py`): loads the spec §15 example; rejects bad `dir`; rejects
  empty metrics; emits warning on identical engines.
- [ ] Commit: `feat: config models + validation`.

### Task 3 — State store (`state.py`)
**Responsibility:** durable state (SQLite) + artifact versioning (git), crash-only.
- [ ] SQLite schema per spec §9: `run, iteration, metric, command, checkpoint`.
- [ ] `StateStore.open(project_dir)`; methods: `create_run`, `record_iteration`
  (one transaction: takes git_hash + metrics + verdict), `last_iterations(k)`,
  `best_score`, `set_status`, `reconcile()` (git HEAD ↔ last row).
- [ ] Git helpers: `git_init`, `git_snapshot()->hash`, `git_commit(msg)->hash`,
  `git_revert_uncommitted()`, `git_diff(hash_a,hash_b)->str`, `git_diff_head()`.
- [ ] Tests (`test_state.py`): create run → record 3 iterations → `last_iterations(2)`
  returns newest 2 with metrics; revert restores files; reconcile rolls back a row with
  no matching commit; resume reads best_score correctly.
- [ ] Commit: `feat: SQLite+git state store with crash-only resume`.

### Task 4 — Scorer (`scorer.py`)  *(pure, deterministic — TDD anchor)*
**Responsibility:** metrics vector → scalar 0–100 + keep/discard.
- [ ] `normalize(value, worst, target, dir) -> 0..100` (clamped target-range map).
- [ ] `score(metrics, cfg) -> float` (weighted aggregate).
- [ ] `decide(new_score, best_score, min_delta) -> 'keep'|'discard'` (keep iff
  `new_score - best_score >= min_delta`).
- [ ] Tests (`test_scorer.py`): higher-better and lower-better normalize correctly;
  clamping below worst → 0, above target → 100; weighted sum matches hand calc;
  improvement below `min_delta` → discard (noise band); equal → discard.
- [ ] Commit: `feat: deterministic target-range scorer with noise band`.

### Task 5 — Metric adapters (`metrics/`)
**Responsibility:** run artifact in sandbox → metrics vector.
- [ ] `base.py`: `MetricResult(metrics:list[dict], logs:str, ok:bool)`; protocol
  `MetricAdapter.run(artifact_dir, sandbox) -> MetricResult`.
- [ ] `numeric.py`: run `cfg.command` in sandbox, parse JSON stdout → named numbers.
- [ ] `command_exit.py`: exit 0 → `{passed:1}` else `{passed:0}`; capture stderr to logs.
- [ ] `pytest_pass.py`: run pytest against **harness_dir** tests over artifact; parse
  passed/total → `{pass_pct, passed, total}`.
- [ ] Non-runnable artifact → `ok=False` (Scorer maps to fail/discard).
- [ ] Tests (`test_metrics.py`): numeric parses JSON; command_exit maps codes;
  pytest_pass counts on a tiny fixture (2/3 passing → 66.7); broken code → ok=False.
- [ ] Commit: `feat: numeric/command-exit/pytest-pass metric adapters`.

### Task 6 — Adapter registry + mock agent (`registry.py`, `agents/`)
**Responsibility:** map engine name → agent; provide deterministic mock for tests.
- [ ] `base.py`: `RunResult(status:Literal['success','timeout','crashed','no_op'], stdout, changed:bool)`;
  protocol `AgentAdapter.run(brief:str, workdir:str, profile:str, timeout:int) -> RunResult`.
- [ ] `mock.py`: `MockAdapter(script)` — `script` is a list of callables/edits applied to
  `workdir` per iteration (e.g. fix one failing test each call); returns RunResult.
- [ ] `registry.py`: `AdapterRegistry.get(engine) -> AgentAdapter`; Phase 1 registers
  `mock`; CLI-agent classes added in Phase 2.
- [ ] Tests (`test_registry_mock.py`): registry returns mock; mock applies the i-th edit
  and reports `changed`; empty edit → `no_op`.
- [ ] Commit: `feat: adapter registry + scripted mock agent`.

### Task 7 — Brief builder (`brief.py`)
**Responsibility:** assemble the curated iteration brief (spec §4) from state.
- [ ] `build_brief(state, cfg, role) -> str`: goal, best/target, last delta,
  metrics-now-vs-best, validator feedback (Phase 1: empty/none), attempt history of
  last K with **real git diff** (from `git_hash`) + per-metric deltas, oscillation flag,
  live `context/<role>.md` contents, the task line.
- [ ] `oscillation_flag(attempts) -> bool`: same file:line changed back and forth.
- [ ] Tests (`test_brief.py`): brief is deterministic for fixed state; includes diff
  lines of last attempts; sets oscillation flag on a back-and-forth fixture; respects
  `history.depth_k`.
- [ ] Commit: `feat: brief builder with diff-based attempt history`.

### Task 8 — Sandbox local backend (`sandbox.py`)
**Responsibility:** execute a command for the metric runner; Phase 1 = local subprocess
with timeout (Docker in Phase 2 behind same interface).
- [ ] Protocol `SandboxBackend.run(cmd, cwd, timeout, env) -> (exit, stdout, stderr)`.
- [ ] `LocalBackend`: subprocess with timeout + kill; `DockerBackend` stub raises
  NotImplemented (Phase 2).
- [ ] Tests: runs echo; enforces timeout (kills a sleep); captures stderr.
- [ ] Commit: `feat: local sandbox backend (docker interface stub)`.

### Task 9 — Orchestrator (`orchestrator.py`)
**Responsibility:** the loop (spec §3 steps 1–9), hill-climbing, stop logic.
- [ ] `async run_iteration(...)`: build brief → run executor (mock) in writeable workdir →
  snapshot → metric runner (sandbox) → scorer → keep(commit)/discard(revert) →
  record_iteration → classify no_op vs plateau.
- [ ] `async run_loop(...)`: until `target_score` OR `plateau_N` (meaningful-change
  plateau, not no_op) OR `max_iterations`. Updates best_score; no-op → separate counter
  + nudge (re-run once), not plateau.
- [ ] Tests (`test_orchestrator.py`, mock agent): improving mock → score rises, keeps;
  regressing mock → discard + revert; no-op mock → no_op_count rises, not plateau;
  reaching target → stops with status finished; plateau of meaningful no-improve → stop.
- [ ] Commit: `feat: asymmetric orchestrator loop with hill-climbing + stop logic`.

### Task 10 — CLI (`cli.py`)
**Responsibility:** `tyani-tolkai run <config.yaml>` → runs the loop, prints per-iter
score + final verdict; `--resume` continues an existing project.
- [ ] `main()` argparse: `run` subcommand (config path, `--resume`).
- [ ] Wires Config → StateStore → Registry → Orchestrator; prints a simple table/line
  per iteration (score, verdict) and a final summary.
- [ ] Test: invoke `run` on the golden config with mock engine via subprocess; exit 0;
  stdout shows rising scores. (Overlaps Task 11.)
- [ ] Commit: `feat: CLI run/resume entrypoint`.

### Task 11 — Golden run (the proof) (`test_golden_run.py`, `fixtures/golden/`)
**Responsibility:** end-to-end proof the loop raises real quality.
- [ ] Fixture: `artifact/` with a function failing 7/10 hidden tests in `metrics/`
  (harness invisible to the "agent"); a MockAdapter scripted to fix one test per
  iteration.
- [ ] Config: `pytest-pass`, target_score=100, plateau_N=5, mock engine.
- [ ] Test asserts: score is monotonically non-decreasing, reaches 100 within ~10
  iterations, status `finished`, git history has one commit per *kept* iteration, no
  reverted state leaks into HEAD.
- [ ] **Anti-collusion micro-check:** the mock never edits files in `metrics/` (can't —
  injected at run time); assert the harness dir is unchanged after the run.
- [ ] Commit: `test: golden run proves convergence to target`.

**Phase 1 done-definition:** `pytest -q` all green, golden run reaches target, `tyani-tolkai
run` works on the golden config from a clean checkout. This is the first solution to test together.

---

## Phase 2 — Real agents & hardening (outline)
CLI adapters (`claude -p`, `codex exec`, `opencode run`, `agy -p`) with writeable/read-only
profiles + live stdout streaming; DockerBackend (read-only artifact mount, `--network none`,
mem/cpu limits, harness injected so it's invisible); Validator role producing feedback and
**seeing the last rejected attempt + its prior advice**; noise-aware keep (median over runs)
+ baseline re-validation; oscillation detector wired into the brief; human checkpoints
(`awaiting_review` + decisions); pause/stop/resume + rate-limit→pause; budget accounting.
Each becomes a task set with the same TDD shape (mock first, then real).

## Phase 3 — Product surface (outline)
FastAPI + WebSocket: dashboard (Projects → run → evolution chart central), live agent
stdout stream, checkpoint panel; projects CRUD; export/import (`tar` + `git bundle`,
relative paths, secrets excluded). Built via `frontend-design` skill (large fonts/elements,
not overloaded — spec §12).

---

## Self-review (plan vs spec v3)
- **Spec coverage (Phase 1 portion):** loop §3 → Task 9; deterministic scorer + target-range
  norm §3/§6 → Task 4; metric adapters + harness invisibility §6/§7 → Tasks 5+11; brief w/
  diff history + oscillation §4 → Task 7; state+git+resume §9/§11 → Task 3; no-op≠plateau
  §6/§11 → Tasks 9+11; config §15 → Task 2; CLI §5 → Task 10. Validator/feedback, Docker,
  checkpoints, noise-median, UI, projects CRUD → deferred to Phases 2–3 (intentional).
- **Placeholders:** none — every task names exact files, interfaces, and concrete test
  assertions. Full per-line code is produced at execution time (inline execution).
- **Type consistency:** `RunResult` (Task 6) consumed by Orchestrator (Task 9);
  `MetricResult` (Task 5) consumed by Scorer (Task 4) via Orchestrator; `git_hash` from
  State (Task 3) consumed by Brief (Task 7). Names aligned across tasks.
