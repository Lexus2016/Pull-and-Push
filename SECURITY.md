# Security model — Pull-and-Push (Тяни-Толкай)

Pull-and-Push runs an adversarial co-evolution loop in which **off-the-shelf AI agents write
code, and that code is executed and scored**. Executing model-written code is the product's
core function, not an accident — so the security model is about *containing* that execution and
keeping the *scoring* trustworthy. Read this before exposing the system beyond your own machine.

## Threat model at a glance

| Asset | Threat | Mitigation |
|---|---|---|
| The host machine | LLM-written artifact runs arbitrary code | Sandbox the execution (see below) |
| The score / outcome | An agent games the judge instead of solving the task | Scoring is **deterministic code**, never the agent |
| Symmetric referee | A gameable/buggy referee silently corrupts the arms race | The referee is a vetted, deterministic trust anchor — never an LLM |
| API spend | A runaway or hostile run burns tokens ("Denial of Wallet") | `budget_usd` hard cap + `usd_per_mtok`; UI Stop |
| The web UI | Anyone who reaches it can launch code-executing, money-spending runs | Localhost bind by default + optional password; add auth + TLS to expose |
| Credentials | Agent API keys leak | Keys live in the agent CLIs' own auth, never in project configs |

## 1. Executing untrusted code — the sandbox is the boundary

Every artifact (asymmetric) and **both rival artifacts** (symmetric) are code the agent wrote.
In symmetric mode the **referee additionally executes one side's artifact against the other's
input**. All of this runs through the configured sandbox backend (`sandbox.backend`):

- **`local` (default)** — `LocalBackend`: a host subprocess with a hard timeout and
  `PYTHONDONTWRITEBYTECODE=1`. **No isolation.** Fast and convenient for trusted/dev use, but the
  code can read/write your filesystem, reach the network, and consume resources. **Do not run
  untrusted or unattended workloads on `local`.**
- **`docker`** — `DockerBackend`: runs in a container with `--network none` (default), a
  read-only mount of the project dir at its identical absolute path, and optional `--memory` /
  `--cpus` limits. This is the isolation you want for any code you don't fully trust.

**Production guidance:** use `docker` (or a stronger boundary — gVisor, Firejail, or a
disposable microVM) for any run where the agent's output is not fully trusted. The Docker backend
is implemented and unit-tested for configuration, but its live container run is **not yet
validated in CI** (no daemon in the dev box) — **validate it on a real Docker host before
relying on it in production.**

## 2. The judge is code, never the agent (anti-collusion)

The scalar score and the keep/discard decision come from `scorer.py` (asymmetric) or the
**referee** (symmetric) — deterministic Python, not the LLM. Neither side scores itself. In
symmetric mode the referee is the **trust anchor**: it must be deterministic, vetted, and never
an LLM. A referee that is gameable or wrong corrupts the entire arms race silently. Its
determinism and mirror-symmetry are enforced by tests (`tests/test_referee.py`); hold any new
referee to the same bar before registering it.

Mix agent providers for non-trivial runs (e.g. `claude` executor + `codex` validator/rival): same
model on both sides gives correlated blind spots. This is a quality measure, not a trust measure —
the deterministic judge is what prevents collusion.

## 3. Denial of Wallet

A co-evolution run makes many paid agent calls, and **symmetric mode doubles the cost** (two
sides, each running a bounded sub-loop per generation). Controls:

- `limits.budget_usd` — a hard cap; the run stops (`reason=budget`) when estimated spend reaches
  it. Requires `limits.usd_per_mtok` (price per 1M tokens) to be set, since cost is estimated
  from token counts. The estimate is rough — treat it as a **safety cap, not an invoice**.
- `arena.generations` and `arena.per_generation_iterations` bound the work.
- **UI Stop** halts a run between generations; the run persists as resumable.

## 4. The web UI

`tyani-tolkai web` binds to **`127.0.0.1` by default** (localhost only). The UI can create
projects, **launch runs that execute code and spend money**, and delete projects. Therefore:

- Set `TYANI_TOLKAI_WEB_PASSWORD` to require a token on every API call. The CLI prints a warning
  when it is unset.
- **Never bind to `0.0.0.0` or expose the port without** an authenticating, TLS-terminating
  reverse proxy in front. There is no multi-user model, no per-user isolation, and no CSRF
  protection — it is a single-operator tool.

## 5. Data & credentials

- Project data lives in `~/.tyani-tolkai/` (override with `TYANI_TOLKAI_HOME`), **outside the
  repo** — it is never committed.
- Agent API keys are managed by the agent CLIs' own auth (`claude`, `codex`, …). The orchestrator
  never reads or stores them. **Do not put credentials in `config.yaml`.**
- The hidden scorer/harness is kept out of the agent's reach (the executor cannot edit the code
  that grades it); the reviewer never sees it either (anti-collusion).

## Reporting

This is research/automation tooling, not a hardened multi-tenant service. If you find a security
issue, open an issue describing the impact and a minimal reproduction. Do not include credentials
or `.env` contents.
