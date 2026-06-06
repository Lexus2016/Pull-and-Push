# Onboarding existing trading bots — architecture decision

Status: **accepted** (design; build pending). Cross-checked independently with two outside
models (codex / OpenAI and agy / Gemini) — they converged on the same recommendation.

## Context

The engine optimizes an artifact against a **deterministic, vetted, hidden scorer** (the
"harness"). That invariant — the optimizer cannot see or game the judge — is what makes the
loop trustworthy.

A user with an **existing** trading bot pointed our fixed trading template at it. The fixed
harness didn't actually measure their bot, the zero-point pinned to the first measurement,
and the seed already met the target → **score 100 on iteration 1, a silent false "success".**
(We shipped a baseline reality-check that now flags "objective mis-specified" instead of
declaring victory — but that only catches the symptom.)

Every real bot differs: framework, entry point, data source, what it emits, what's tunable.
A single fixed harness cannot fit them. We need a **bespoke evaluation per bot**.

## The trap (rejected)

The obvious idea — *let an agent write a bespoke backtester/scorer for each bot* — **breaks
the core invariant**. An agent-written judge is buggy/gameable/possibly-wrong, and the
optimizer will actively maximize whatever number it emits. A "validation gate" on top
(baseline-not-0/100, broken-variant-scores-worse, determinism, OOS, cross-check vs the bot's
own PnL) is **necessary but NOT sufficient**: both reviewers showed it is gameable —

- **Look-ahead baked into the scorer logic**: the optimizer finds & exploits leaks the gate
  (even on OOS) won't see, because the leak is in the judge's own data handling.
- **Goodhart / fake reports**: if the gate cross-checks the bot's *self-reported* PnL, the
  bot can synthesize a matching fake report and "pass" without trading well.
- **State poisoning / RCE / side-channels**: a scorer that runs the bot in-process (or that
  uses `eval`/unsafe imports) lets the bot rewrite the score, the clock, or hidden data; and
  error tracebacks / timing leak the judge's logic for reverse-engineering.

## Decision: agent writes the ADAPTER, not the scorer

**The agent never writes the judge.** It produces only *declarative* + *adapter* artifacts;
the scoring engine is static, vetted code shipped with the system.

1. **Bot Profile (read-only analysis)** — the agent reads the bot and emits a structured
   profile: framework, entry point, **tunable surface** (params the executor may change),
   data source, and which performance facts are extractable. Human reviews.
2. **Adapter (thin, generated) + Manifest (declarative)** — the agent writes a small adapter
   that exposes the bot through ONE standard interface and a manifest (tunable ranges, data
   format, signal/order schema). The adapter's only job: turn market data into the bot's
   decisions and emit **orders/signals** in our standard format.
3. **Vetted scoring engine (static, never agent-generated)** — a trusted backtest engine
   feeds market data to the bot, receives its **orders**, and **computes PnL and all metrics
   itself** (return, drawdown, liquidations, fees, slippage, stability, complexity penalty).
   The bot never computes or reports its own score; its self-reports are never trusted.
4. **Isolation** — the bot runs as a separate sandboxed process: no network, no access to the
   engine code or the hidden/held-out data, communicating only via structured messages
   (e.g. stdin/stdout JSON-lines: data in → orders out). This kills state-poisoning and RCE.

This is "safe by construction": there is no agent-written judge to game. The existing
`btcusdt-futures` harness is already a small instance of this pattern (a vetted engine that
scores a strategy's signals) — we generalize it to *any* bot via the adapter + the bot's own
data, instead of a fixed `strategy.py` interface and bundled data.

## Guardrails (retained as SECONDARY checks, not the trust mechanism)

Run before optimization, surfaced to the human as an evidence report:
- **Control strategies with an expected ordering** (not just "broken < real"): e.g.
  flat < random < buy-and-hold < the user's bot — the engine must reproduce that order.
- **Anti-look-ahead probe**: shift the data timeline; an anomalous score jump reveals a leak.
- **Determinism**: same artifact → same score (do not paper over non-determinism with a
  median; require it or fail the gate).
- **Blind final hold-out**: a slice never used during optimization; report in-sample vs OOS.
- **Masked failures**: the executor sees only `Evaluation failed`, never a traceback/timing
  detail that leaks the engine's internals.
- **Immutable + traceable**: hash the engine, data, and config; human approves that exact
  frozen version; run under tight FS/permission limits.
- **Bounded budget**: cap iterations; the score is a leaky channel under enough tries.

## Phased plan (build incrementally; each phase ships value)

- **P1** — Bot Profile analyzer (read-only). Safe, standalone (helps users understand what
  the system sees), foundation for the rest.
- **P2** — Propose metrics + manifest from the profile + the user's goal; human approves.
- **P3** — The vetted scoring engine + the standard bot protocol + sandboxed subprocess comms
  (orders in/out; engine computes PnL). The core.
- **P4** — Secondary validation checks + the human evidence report.
- **P5** — Intent-first "new project" UI ("improve what I have" vs "start from an example"),
  enabled only once P1–P4 make "improve what I have" actually work.

## Why this was cross-checked first

Letting an agent write the judge is a hard-to-reverse **integrity boundary**. Two independent
reviews changed the design from "agent writes scorer + heavy gate" to "agent writes adapter;
vetted engine is the judge; bot isolated" — a materially safer architecture. Building the
original would have been a mistake; that is the point of reviewing before building.
