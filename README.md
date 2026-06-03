# Тяни-Толкай (Push-Pull)

An **adversarial co-evolution orchestrator**: it runs two off-the-shelf agent CLIs in
opposition to drive a project to maximum quality. One agent improves an artifact; the
other evaluates it. They iterate — keep-if-better — until quality plateaus or hits a
target. The hardened artifact is the deliverable; the loop is the engine.

Inspired by GANs, Karpathy's `autoresearch`, and the `consilium` adapter pattern.

- **Design spec:** `docs/superpowers/specs/2026-06-03-tyani-tolkai-design.md` (v3)
- **Plan:** `docs/superpowers/plans/2026-06-03-tyani-tolkai-core-mvp.md`

## Core ideas

- **Two axes:** competition mode (*asymmetric* Executor↔Validator | *symmetric* Rival↔Rival —
  Phase 2 later) × pluggable agent engine per role (`claude` / `codex` / `opencode` / `agy`).
- **No bespoke agents.** Every role is an off-the-shelf CLI agent; we only build code we
  control (orchestrator, scorer, metric runner, state, web).
- **The number comes from code, the LLM only advises.** A deterministic Scorer computes
  the 0–100 score from objective metrics; the Validator LLM gives text feedback only.
  Five anti-collusion locks (incl. harness invisibility).
- **Hill-climbing via git** + curated *iteration brief* with the real diff of past attempts.

## Install

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # add web deps too (fastapi/uvicorn/httpx)
.venv/bin/pytest                       # 50 passed, 1 skipped (docker)
```

## Quick start — watch it work

```bash
# 1) terminal demo (mock executor, real numeric adapter + sandbox)
.venv/bin/tyani-tolkai demo
#   iter 1: keep 55 → 2: keep 70 → 3: keep 85 → 4: keep 100 → finished (target)

# 2) the dashboard (open the printed URL)
.venv/bin/tyani-tolkai web
#   • click "Demo" to see the evolution chart climb to 100
#   • ⚙ Config tab → 🪄 "Згенерувати з опису": describe the task in plain language;
#     the configurator agent drafts the whole project, you tweak it in the form, then Create.
#   • tabs split Config / Progress so the long form never mixes with results
```

### The configurator agent
Instead of filling the form by hand, describe the task ("optimize strategy.py for Sharpe,
metrics sharpe↑ and max_dd↓, target 90, edit only strategy.py"). A chosen CLI agent (with
the fixed schema prompt in `configurator.py`) emits a valid config, which lands in the form
for you to fine-tune. No bespoke agent — just an off-the-shelf CLI in read-only mode.

## Real run (your own task, real agents)

Write a `config.yaml` (see spec §15 / `tests/fixtures/example_config.yaml`):

```yaml
project: my-task
mode: asymmetric
agents:
  executor:  { engine: claude, model: opus,  timeout: 600 }
  validator: { engine: codex,  model: gpt-5, timeout: 300 }   # different provider
roles:
  executor:  { goal: "Make all tests pass.", task: "Edit src/ only." }
  validator: { goal: "Say why the score moved; one concrete next step." }
seed: { copy: ~/path/to/starting/code }       # empty | copy:<path> | generate
evaluation:
  adapter: pytest-pass                         # numeric | command-exit | pytest-pass
  metrics: [ { name: pass_pct, dir: higher, weight: 1, worst: 0, target: 100 } ]
  target_score: 100
  harness_dir: metrics/                        # hidden tests, invisible to the executor
limits: { max_iterations: 30, plateau_N: 6, step_seconds: 600 }
sandbox: { backend: local }                    # docker (default in spec) | local
```

```bash
.venv/bin/tyani-tolkai run config.yaml          # drives the real agents
.venv/bin/tyani-tolkai run config.yaml --resume # continue after a stop / limit
```

## Projects

```bash
tyani-tolkai projects list
tyani-tolkai projects export my-task --to my-task.tar.gz   # portable (git bundle inside)
tyani-tolkai projects import my-task.tar.gz --to copy-1     # continue on another machine
tyani-tolkai projects reset my-task                         # back to seed, keep config
tyani-tolkai projects rename my-task --to renamed
tyani-tolkai projects delete renamed
```

## Status

| Phase | State |
|---|---|
| **1 — core asymmetric loop** | ✅ done (config, state SQLite+git, scorer, metric adapters, brief, orchestrator, CLI, golden run) |
| **2 — real agents & hardening** | ✅ CLI adapters, git change-detection, Validator role, noise median, Docker backend, seeding, resume, projects, export/import |
| **3 — product surface** | ✅ WebUI (FastAPI dashboard, live evolution chart, demo+run, token auth) |

**Known boundaries (honest):**
- **Symmetric mode** (Rival↔Rival + Arena + champion archive) — designed, not yet built.
- **Docker backend** is implemented but validated only by design here (no Docker daemon in
  the dev box); the `local` backend is fully tested. The interpreter for `pytest-pass` is
  taken from the backend automatically (`python` inside Docker, the host venv locally).
- **Command semantics differ by backend:** `local` runs metric `command`s via argv (shlex),
  Docker via `sh -c`. Keep commands simple (avoid shell pipes) for cross-backend parity.
- **Checkpoints** currently surface as stop-points (target/plateau/max) visible in the UI;
  interactive mid-run continue/adjust is the next increment.
- **Live updates** use polling (1s), not WebSocket — same live chart, simpler/robust.
- **WebUI auth** is a token passed as a URL query param — fine for the intended
  localhost single-user use. For remote exposure, front it with a TLS reverse proxy
  (the token would otherwise appear in logs/history).
- **Path safety:** project names are validated against traversal (`../`, `/`); only
  `executor`/`validator` roles accepted for steering.

## Tests

`50 passed, 1 skipped` — unit (scorer, config, state, metrics, brief, registry, sandbox),
integration (orchestrator with mock + validator), CLI adapter, projects round-trip,
WebUI endpoints, and the **golden run** proving 1/10→10/10 convergence with the harness
left untouched (anti-collusion).
