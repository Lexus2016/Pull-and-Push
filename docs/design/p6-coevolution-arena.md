# P6 — Co-Evolution Arena (symmetric Rival↔Rival mode)

> Status: **DESIGN — awaiting approval.** Phase 1 (asymmetric Executor↔Validator +
> deterministic hidden scorer) is shipped. This document specifies the symmetric mode.
> Entry point to unlock: `src/tyani_tolkai/config.py:185` (`NotImplementedError`).

## Problem

Phase 1 drives a *single* artifact toward a *fixed* objective: the executor edits
`artifact/`, a deterministic scorer (`scorer.score` over a metric adapter) judges it,
the loop keeps/discards. The objective never moves.

Symmetric mode wants two rival agents to **co-evolve two artifacts A and B**, where each
is judged by how it performs **against the other** — a GAN / red-team↔blue-team arms
race. Canonical real target: A = anti-detect browser, B = bot-detector. They compete
until one dominates or the budget runs out.

Three things make this hard and are the whole point of the design:

1. **Who judges?** If A or B computes its own win, every gaming problem from Phase 1
   returns — worse, because *both* sides are LLMs and can co-drift / collude. The judge
   must be a deterministic, vetted **referee**, never an LLM, and neither side may score
   itself.
2. **Will it converge or just cycle?** Co-evolution is famously unstable
   (rock-paper-scissors cycling, mode collapse, catastrophic forgetting). We must *prove*
   convergence on a toy with a known answer before trusting it on a real domain.
3. **What do we show the user?** "% beat the current opponent" is a non-stationary target
   and a lie as a progress curve. Progress must be measured against something fixed.

## Key insight — the referee is a *parameterized metric adapter*

In Phase 1 the judge is `scorer.score()` over a metric adapter that measures the artifact
against a fixed objective. "Optimize A against a frozen B" is **the same contract** — only
the measurement now depends on the opponent.

So the referee is wrapped in an `ArenaMetricAdapter(referee, opponent_pool)`, and
"optimize A against frozen B" becomes a **plain Phase-1 run** whose metric adapter happens
to measure "A's win-rate over the frozen opponent pool". `Orchestrator.run_iteration`,
`run_loop`, `scorer.score`, the keep/discard decision, the per-candidate git commit —
**none of them change**. The entire adversarial nature is encapsulated in the adapter and
the opponent pool it was constructed with.

This is the lever that makes symmetric mode a thin orchestration layer (low risk, small
surface) instead of a parallel engine. Everything below serves this one idea.

## Design

### 1. `Referee` — the new trust anchor (A)

A deterministic, vetted match-engine. Pure code. **Never an LLM. Sides never self-judge.**

```
class Referee(Protocol):
    name: str
    def play(self, a_dir: Path, b_dir: Path, *, seed: int) -> MatchOutcome: ...

@dataclass
class MatchOutcome:
    a_score: float        # objective scalar in [0, 1] from A's perspective
    b_score: float        # B's perspective; for a zero-sum game b_score == 1 - a_score
    detail: dict          # referee-specific evidence (counterexamples found, etc.)
```

Invariants (enforced by tests):

- **Determinism:** same `(a_dir, b_dir, seed)` → identical `MatchOutcome`.
- **Symmetry / mirror:** swapping the roles and reading the mirror field gives the same
  number — `play(a, b).a_score` corresponds to the A-side reading regardless of argument
  order. (For a zero-sum referee this is `a_score + b_score == 1`.)
- **Isolation:** `play` executes side A's untrusted code inside the existing sandbox
  (`SandboxCfg`, local/docker), exactly like a Phase-1 metric run. It is the integrity
  boundary — if it is gameable or wrong, the whole arms race is meaningless.

The referee is selected by name from a small vetted registry (toy referee ships first;
real-domain referees are added later, each independently reviewed).

### 2. `ArenaMetricAdapter` — bridge into the existing scorer (A)

Wraps a referee + a frozen opponent pool, and presents the Phase-1 metric-adapter
interface so the unchanged `Orchestrator` can drive a side's sub-loop.

For the live side's current candidate it runs `referee.play(live, opponent, seed=...)`
against **every** opponent in the frozen pool and aggregates the per-match scores into a
**single scored metric** `arena_fitness` (dir=higher, target=1.0) that the existing
`scorer.score` + `decide` consume unchanged. The gate is computed **inside the adapter**, not
by handing two raw metrics to the weighted scorer (which would produce a muddled blend —
cross-AI review catch). `mean`, `min`, and the per-opponent breakdown are exposed as
report-only fields in `MetricResult.data` for the UI and the match matrix (§4a).

Aggregation into `arena_fitness`:

- **Default `mean_gated`**, defined as an explicit rule: `arena_fitness = mean` when
  `min >= min_floor`, else `mean - penalty * (min_floor - min)`. Pure `min` makes the
  gradient sparse (signal only from the single hardest opponent → stalls); pure `mean` lets a
  side farm weak archived opponents and ignore the strong one. `mean_gated` keeps a usable
  gradient from the mean while penalizing a collapsing worst case. Modes:
  `mean | min | mean_gated` (default `mean_gated`); `min_floor`, `penalty` configurable.
- The opponent pool is **frozen** for the whole of a side's generation (stationary within
  the sub-loop) — that is what lets the Phase-1 loop treat it as a fixed objective.

### 3. Champion archive + opponent sampling (B)

Each side keeps a **hall-of-fame** of its best versions — reusing the per-candidate git
commits that Phase 1 already produces. When a generation ends with a new best for a side,
that commit is recorded as a champion: `champion(side, generation, git_hash, stable_score)`.

The opponent pool an `ArenaMetricAdapter` is built with depends on a **per-referee opponent
strategy** (pluggable, declared by the referee):

- **`sample`** (default, for behavioral-artifact domains like the real anti-detect↔detector):
  `{latest champion} ∪ {k random past champions}` (k configurable, default 3). Scoring against
  a *pool of past bests* — not only the latest — prevents (a) rock-paper-scissors cycling and
  (b) catastrophic forgetting (re-losing to opponents already beaten).
- **`accumulate`** (for data-counterexample domains like CEGIS, §7): the *growing union of all
  unique opponent outputs ever produced* — a monotonically increasing set. This is what
  guarantees CEGIS convergence; a k-sample would let the side memorize the sampled subset
  instead of learning the underlying target. (Cross-AI review catch.)

**Champion validation gate (cross-AI review catch).** A generation's best from the inner
sub-loop is only optimized against the *frozen* pool it saw, so it can overfit to that pool
and silently regress against archived champions it never faced. Before crowning it champion
and adding it to the archive, the parent tests the candidate against the **whole** current
opposing archive; if its win-rate regresses beyond `promote_regression_max` (default 0.1
below the prior champion's archive-wide score), the champion is **not** updated this
generation (the inner loop simply tries again next generation against a refreshed pool). This
is the primary structural guard against cycling.

### 4. `SymmetricOrchestrator` — alternating self-play (A)

Wraps the existing `Orchestrator.run_loop`; does **not** replace or fork it. **Principle
(cross-AI review):** the inner Phase-1 loop is only a *local-best generator against a frozen
pool* — the arena's real evaluation lives in the archive, the **match matrix (§4a)**, and the
stable signals. The outer layer never trusts the inner `best_score` as global truth.

```
for generation in range(max_generations):
    play_generation(side="A", generation)   # optimize A vs frozen B-pool, then gate+archive
    play_generation(side="B", generation)   # optimize B vs frozen A-pool (incl. A's new champ)
    update_match_matrix(generation)          # §4a — every A-champ vs every B-champ
    update_stable_signals(generation)        # §5 — vs fixed validation panel
    if stopping_rule(generation):            # §6 — computed from the matrix, not best_score
        break

def play_generation(side, generation):
    pool   = build_opponent_pool(opposing_side)      # §3 strategy: sample | accumulate
    seed   = copy_previous_champion(side)            # carry ARTIFACT + lineage, not the score
    run    = state.new_run(side, generation)         # fresh run_id → fresh objective
    adapter = ArenaMetricAdapter(referee, pool, cfg.arena)   # emits single arena_fitness (§2)
    orch   = Orchestrator(cfg[side], state[side], run, executor[side], adapter, sandbox)
    # reset recipe: fresh baseline re-pins on the FIRST measurement of `seed` vs the NEW pool,
    # so plateau/noise/best all reset automatically (Phase-1 already does this on a new run).
    summary = orch.run_loop(... bounded by min(per_generation_iterations, budget_remaining) ...)
    cand    = best_of(run)
    if promotion_gate_ok(side, cand, pool_signature):   # §3 gate: no regression vs WHOLE archive
        snapshot_champion(side, generation, cand, repro_meta)   # repro_meta: pool_hash,
                                                                # referee_version, aggregate, seeds
```

Each side's sub-loop is a normal, fully-featured Phase-1 run (its own brief, reviewer,
plateau handling, resume, cost telemetry). The symmetric layer only: builds opponent pools,
seeds from the prior champion, constructs the adapter, applies the promotion gate, maintains
the match matrix, computes stable signals, and decides when to stop. **Budget** (`budget_usd`)
is a parent-level cap; each sub-run is launched with the *remaining* budget so the inner loop
cannot overspend (cross-AI review catch).

### 4a. Match matrix — the arena's source of truth (B)

The parent maintains a results matrix: for every (A-champion, B-champion) pair, the referee's
`a_score`. Cheap to keep incrementally (only the new champion's row/column is filled each
generation). **Everything that decides the run reads from the matrix, not the inner loop:**

- **Dominance** (stop): a side's latest champion beats the *entire* opposing archive ≥
  `dominance_tau` for `R` generations.
- **Deliverables:** `best-A-vs-all-B` / `best-B-vs-all-A` = the champion with the best
  worst-case row across the whole opposing archive (robustness, not a lucky single win).
- **Cycle detection:** non-transitive loops in the matrix (A1<B1, B1<A2, A2<B2, …) +
  `detect_oscillation` on the stable signal → flags rock-paper-scissors instead of progress.

**Rival executors must be `claude` or `codex`** (they write to the subprocess cwd;
`opencode`/`agy` do not — read-only roles only). Validated at config load (mirrors the
Phase-1 anti-collusion engine check), erroring on a non-writing executor.

### 5. Two signals (B)

- **Live adversarial signal** — the metric a side's sub-loop optimizes: win-rate vs the
  *current* frozen pool. Non-stationary (the opponent improves between generations).
  Internal only — **never plotted as the progress curve.**
- **Stable progress signal** — win-rate vs a **fixed held-out validation set** frozen at
  run start. Stationary, therefore an honest curve. This is what the UI plots, one curve
  per side. Computed by the referee against the fixed set after each generation.

  The validation set is a referee-specific fixed panel established at run start (for the
  toy: a fixed, held-out, ground-truth-labeled set of strings — see §7). It is *not* drawn
  from either side's evolving archive.

### 6. Stopping rule (A)

Winning is a **stopping rule, not the product.** Arms races rarely have a permanent winner;
the value is robust artifacts. Stop when **any** of:

- **Dominance:** one side beats the opponent's *entire* archive with win-rate ≥ `dominance_τ`
  for `R` consecutive generations.
- **Equilibrium / stalemate:** neither side's *stable* signal improves for `N` generations.
  Reuse the existing `detect_oscillation` to additionally flag cycling (a side's stable
  signal oscillating rather than rising) and stop on that too.
- **Budget:** `max_generations`, or the existing `budget_usd` cost cap (summed across both
  sides' sub-loops).

**Deliverable:** `best-A-vs-all-B` and `best-B-vs-all-A` (the champion of each side that is
most robust across the whole opposing archive), plus the two stable curves.

### 7. First domain — a TOY, not trading: Predicate-vs-Counterexample (CEGIS)

We must prove co-evolution **converges** (does not merely cycle) before any real domain.
Chosen toy: **counterexample-guided recognition**, the cleanest toy that exercises the
*whole* machine (an LLM editing code under an adversarial deterministic referee) **and**
has a checkable fixpoint.

- **A (recognizer):** writes `recognizer.py` exposing `accepts(s: str) -> bool`. Goal:
  recognize exactly a hidden target language **L**.
- **B (adversary):** writes strings (one per line, `strings.txt`) trying to be
  **misclassified** by A — a false positive (`s ∉ L` but `accepts(s)`) or false negative
  (`s ∈ L` but `not accepts(s)`).
- **Referee:** knows the ground-truth **L** as deterministic code (neither agent sees it).
  Runs A's `accepts` on B's strings inside the sandbox. `a_score` = fraction of B's strings
  A classifies correctly per ground truth; `b_score` = misclassification rate B induced.
  Zero-sum: `a_score + b_score == 1`.

**Why this is the right toy (the convergence proof):** the arms race converges when A
recognizes L correctly — at which point B can no longer find a counterexample within bounds
(B "starves"). Because *we* (the experimenters) know L, we can **verify** "A converged to L"
against a fixed held-out labeled set — not merely observe "it stopped". For a regular,
finite-alphabet L this is the classic CEGIS setting and is **provably convergent**. That is
the "known equilibrium" the design demands, while still running the full
agent→artifact→referee→score pipeline.

**Convergence precondition (stated explicitly):** L must lie within A's hypothesis class
(both A and the target are expressible as a regex / finite recognizer over the same
alphabet). Otherwise B wins forever and CEGIS cannot converge — that would test
non-convergence, not the machine. The toy fixes L inside A's class on purpose.

**Stable validation set:** a fixed, held-out set of strings labeled by ground-truth L,
drawn at run start and never shown to either agent. A's stable signal = accuracy on it.
B's stable signal must also be **stationary**, so it is measured against a **fixed
reference recognizer** `R_fixed` (the seed A, frozen at run start, plus a couple of fixed
simple recognizers) — *not* the moving current A: B's stable signal = the misclassification
rate B's current `strings.txt` induces on `R_fixed`. As B improves it exhausts `R_fixed`'s
blind spots, so the curve rises and then saturates — stationary and plottable.

*Alternative considered and rejected as the first toy:* a numeric min-max game with a known
saddle point. It proves the *math* converges but its "artifacts" are just numbers — it
barely tests that an LLM editing code co-evolves. Kept as a possible second, even simpler
referee if P6.1 needs the most minimal start.

### 8. Config (A)

Unlock and extend `config.py`. `mode: symmetric` already exists in the `Config` model
(`config.py:137`); remove the `NotImplementedError` at `config.py:185`.

```yaml
mode: symmetric
agents:
  rival_a: { engine: claude }     # must write to cwd → claude | codex
  rival_b: { engine: codex }      # mixing providers = uncorrelated blind spots
roles:
  rival_a: { goal: "recognize the hidden language" }
  rival_b: { goal: "find strings A misclassifies" }
arena:
  referee: cegis-recognizer       # name from the vetted referee registry
  aggregate: mean_gated           # mean | min | mean_gated
  opponent_pool: { latest: true, k_past: 3 }
  generations: 20
  per_generation_iterations: 8    # bound on each side's Phase-1 sub-loop
  dominance_tau: 0.95
  dominance_rounds: 2             # R consecutive generations
  plateau_generations: 4          # N for the equilibrium stop
```

`roles` keys become `rival_a`/`rival_b` (Phase-1 used `executor`/`validator`); the role +
engine validation is extended to require writing executors for both rivals.

### 9. State (B)

Reuse the existing schema. A symmetric run is a **parent run row** plus **two child
Phase-1 tracks** (A and B), each with its own `artifact/` git repo and its own normal
Phase-1 history — so resume, the iteration list, cost telemetry, and the existing UI all
work per side for free. Add one table:

```
CREATE TABLE IF NOT EXISTS champion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,      -- the parent symmetric run
    side TEXT NOT NULL,           -- 'A' | 'B'
    generation INTEGER NOT NULL,
    git_hash TEXT NOT NULL,       -- champion commit in that side's artifact repo
    stable_score REAL,            -- §5 stable signal at snapshot time
    repro_json TEXT,              -- pool_hash, referee_version, aggregate, seeds (reproducible scoring)
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,      -- the parent symmetric run
    a_champion_id INTEGER NOT NULL,   -- champion.id, side='A'
    b_champion_id INTEGER NOT NULL,   -- champion.id, side='B'
    a_score REAL NOT NULL,        -- referee a_score for this exact pairing
    seed INTEGER NOT NULL,
    ts TEXT NOT NULL,
    UNIQUE (a_champion_id, b_champion_id, seed)
);
```

`run.mode` already exists (`state.py:21`) and distinguishes symmetric runs. The two child
tracks are stored as two `StateStore` directories under the symmetric project dir. The
`match` table is the persisted §4a matrix; dominance / deliverables / cycle detection read
from it.

### 10. Sandbox (A)

The untrusted surface **doubles**: both rivals' executors run in the sandbox, and the
referee additionally executes side A's artifact against B's input. The referee runs each
match through the existing sandbox backend (identical-path read-only mount, `network: none`
by default). For the toy, B's output is just strings (data, low risk); A's recognizer is
the executed untrusted code. Real domains will carry heavier isolation requirements — noted
for the per-domain referee review, out of scope for the toy.

## Trade-offs / guardrails

- **Cost doubles** (two sides each running a bounded Phase-1 sub-loop per generation).
  Bounded by `per_generation_iterations` and the existing `budget_usd` cap summed across
  both sides.
- **Co-evolution instability** (cycling, mode collapse, co-drift) — mitigated by the
  champion archive (score vs a pool of past bests), the `mean_gated` aggregate, and
  `detect_oscillation` on the stable signal.
- **Non-stationary score** — mitigated by the live/stable split; only the stable signal is
  ever shown as progress.
- **The referee is the integrity boundary** — deterministic + vetted + symmetry/determinism
  tests; never an LLM.
- **YAGNI:** no live mid-run opponent reconfiguration, no multi-objective referees, no more
  than two sides — all out of scope until the toy proves convergence.

## What this is NOT (carried from the design decision note)

- Sides do **not** judge themselves; the referee does.
- The referee is **not** an LLM.
- We do **not** start with trading. Toy first, real domain only after P6.6 proves
  convergence.

## Phased plan

- **P6.1** `Referee` interface + `MatchOutcome` + deterministic CEGIS match-engine on the
  toy domain + tests for determinism and symmetry (A-vs-B == mirror).
- **P6.2** Config: `rival_a`/`rival_b` roles, drop `config.py:185` `NotImplementedError`,
  `arena` schema + writing-executor validation.
- **P6.3** Champion archive (git snapshots + `champion` table) + per-referee opponent
  strategies (`sample` / `accumulate`) + the `match` table and **promotion gate** (no
  champion crowned that regresses against the whole opposing archive).
- **P6.4** `SymmetricOrchestrator`: alternating generations over the Phase-1 `run_loop` +
  per-generation reset recipe (fresh run, seed=prev champion, repro metadata) + **match
  matrix maintenance** + two signals + matrix-based stopping rule.
- **P6.5** CLI/web: symmetric mode selection + visualize the two stable curves / the arms
  race.
- **P6.6** Toy e2e proving convergence (A recognizes L, B starves, verified against the
  held-out set) — **gate before any real domain.**

## Files

- `src/tyani_tolkai/arena/referee.py` — `Referee` protocol, `MatchOutcome`, referee registry.
- `src/tyani_tolkai/arena/cegis.py` — the toy CEGIS recognizer referee + ground-truth L.
- `src/tyani_tolkai/metrics/arena.py` — `ArenaMetricAdapter` (referee + frozen pool → metric).
- `src/tyani_tolkai/symmetric.py` — `SymmetricOrchestrator`, opponent strategies, promotion
  gate, match matrix, stopping rule.
- `src/tyani_tolkai/config.py` — unlock symmetric, `arena` schema, rival roles/validation.
- `src/tyani_tolkai/state.py` — `champion` table + paired-track helpers.
- `src/tyani_tolkai/cli.py`, `src/tyani_tolkai/web/server.py` — mode selection + dual-curve UI.
- `tests/` — `test_referee.py` (determinism/symmetry), `test_arena_adapter.py`,
  `test_symmetric.py` (alternation, archive, stopping), `test_cegis_convergence.py` (P6.6 e2e).

## Status

DESIGN — approved, hardened by cross-AI review (codex + agy), ready for the implementation
plan. The review confirmed the central lever (referee = parameterized metric adapter, inner
loop unchanged) and added three structural guards now folded in: (1) the **match matrix** as
the arena's source of truth — dominance/deliverables/cycle-detection read from it, never from
the inner `best_score`; (2) the **promotion gate** + per-referee **`accumulate`** strategy for
CEGIS (growing counterexample union) against pool-overfitting; (3) `arena_fitness` as a single
adapter-computed gated metric (not two raw metrics to the scorer) + per-generation reset recipe
(carry artifact+lineage, not the score) + remaining-budget propagation.

Lessons carried from Phase 1: rival executors must be claude/codex (cwd); commit the candidate
before running metrics (already how Phase 1 works); baseline re-validation guards a noise best;
mix providers for uncorrelated blind spots; `detect_oscillation` already exists and is reused
to flag cycling.
