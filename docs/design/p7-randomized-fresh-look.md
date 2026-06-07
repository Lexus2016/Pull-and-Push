# P7 — Randomized fresh-look (SSoT-style frame rotation)

## Problem

The Phase-1 loop already injects a "fresh-look" nudge every `FRESH_LOOK_EVERY` (=5)
iterations (`brief._fresh_look`) — an SSoT-style "step back, look from the other side"
poke meant to break inertia / a local optimum.

But it was a **single fixed text** per role. A model habituates to a repeated identical
prompt: it learns to skim past the same tokens, so the nudge stops shifting behaviour.
That is not enough to actually re-route the search. We want a real internal randomization
so each checkpoint genuinely re-conditions the agent onto a different region of the
solution space.

## Key insight — randomize the FRAME, not the value

"Make the model use different neural pathways" is a metaphor. Mechanically, an LLM's output
is a function of **(input context, sampling)**. The only lever we own inside the brief is the
**input**. And the input only shifts the output if it is *semantically meaningful*:

- Injecting a random string (`q7Km3Xp9`) into the brief does **nothing useful** — it is
  semantically empty noise the model ignores or misreads as data. (That trick works for the
  *operator's own* SSoT ritual, where the agent reloads *itself*; it does not transfer to a
  one-shot executor whose input we are conditioning.)
- Injecting a **different semantic lens** does shift it — a different frame is a different
  conditioning prefix, hence a different next-token distribution.

So we randomize *which perspective* the checkpoint asks for, not random characters.

## Design

### 1. Lens pools (A)
Per role, a pool of genuinely distinct lenses:

- **Executor**: invert the objective · red-team your own solution · radically simplify ·
  cross-domain transfer · challenge the most expensive assumption.
- **Validator**: challenge the premise · hunt the unmodelled real-world factor ·
  look for overfitting/look-ahead/survivorship · name a fundamentally different approach.

### 2. Deterministic rotation (A)
Selection index = `(salt + checkpoint_ordinal) % len(pool)` where
`checkpoint_ordinal = n // FRESH_LOOK_EVERY` (1, 2, 3, …) and `salt = run_id` for the
executor (per-run offset), `0` for the validator.

- **Rotates** each checkpoint (consecutive checkpoints draw consecutive lenses) — this is the
  "different axis of attack each time" behaviour.
- **Deterministic** → the brief stays *reproducible* (a core Phase-1 property): same
  `(run_id, n)` ⇒ same lens. No wall-clock / `random()` seed, or two identical runs would
  diverge.
- The rotation index is the checkpoint *ordinal*, not `n` — using `n` would collapse
  (`n % len(pool)` is constant when the pool size divides the checkpoint stride).

### 3. Grounded entropy (B) — the real lever
On top of the lens, when history holds a **rejected** attempt (`verdict ∈ {discard, fail}`),
the checkpoint re-opens that concrete abandoned direction:
> "reconsider abandoned attempt #k … was dropping that direction a mistake?"

This is the strongest variation because it changes the *material* on the input, not just the
phrasing — the agent re-processes real, different past data. The abandoned attempt is picked
deterministically (`rejected[checkpoint_ordinal % len(rejected)]`).

### 4. Stable header (compat + signal)
A fixed header carries the `FRESH-LOOK` marker and the "look from the OTHER SIDE" signal on
**every** checkpoint regardless of which lens was drawn; the lens supplies the varying angle.
This also preserves the existing test contract.

### 5. Proof metric — `direction_diversity(diffs) → 0..1`
Randomization on faith is cargo-cult. `direction_diversity` is a cheap, deterministic signal:
fraction of recent attempts that touch a *distinct* set of lines. 1.0 = every attempt explores
different lines; low = the loop keeps editing the same lines (inertia / circling). Surfaced in
the brief (`- Direction diversity (last K): …`) so the effect is observable, not assumed.

## Trade-offs / guardrails

- **Exploration vs exploitation.** The nudge stays **periodic** (every Nth iteration); between
  checkpoints the loop does stable incremental work. We randomize the *poke*, not every step —
  otherwise the executor would keep abandoning working directions (the existing
  `detect_oscillation` would flag that seesaw).
- **Reproducibility preserved.** All selection is seeded from `(run_id, n)`; no nondeterminism.
- **Not neuro-magic.** This shifts the output distribution via meaningful input variation. The
  effect is real and now *measurable* (`direction_diversity`), but it is prompt engineering,
  not a literal network rewiring.
- **CLI sampling untouched.** A temperature bump at the checkpoint (widen sampling only when
  searching) is a future option but needs an API-level agent; current agents are CLI
  subprocesses whose sampling we do not control.

## Files
- `src/tyani_tolkai/brief.py` — lens pools, rewritten `_fresh_look(role, n, *, salt, past)`,
  `direction_diversity`, wired into `build_brief` (grounded + salt + diversity line).
- `tests/test_brief.py` — rotation+determinism, grounded-in-abandoned-attempt, diversity unit;
  existing fresh-look contract tests retained (stable header).

## Status
Implemented, full suite green (CPython `.venv`).
