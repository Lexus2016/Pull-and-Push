# P3 — Vetted scoring core (design)

Status: **design**. Implements phase **P3** of [`onboarding-existing-bots.md`](./onboarding-existing-bots.md).
Cross-checked with two independent models (agy/Gemini + opencode) — both converged on the
same critical findings, which are folded in below.

## Why P3 is decomposed

P3 is the integrity boundary: the first phase that **actually runs an untrusted bot**.
Shipping it as a monolith is irresponsible. Three sub-phases, increasing risk:

- **P3.1 — Bot protocol + reference adapter + vetted engine** (subprocess; pure Python;
  cross-platform; tested with a TRUSTED reference bot only). The functional core.
- **P3.2 — OS sandbox hardening** (Linux namespaces + seccomp + rlimits, or a vetted jailer
  like NsJail). **Cannot be validated on macOS** (no namespaces/seccomp) — must be built and
  tested on Linux. This is the real jail for ARBITRARY untrusted bots.
- **P3.3 — Orchestrator integration** (a new `EvaluationCfg` adapter `isolated-bot`).

Anti-look-ahead and anti-exfiltration live in the **protocol** (P3.1, works everywhere);
the OS sandbox (P3.2) stops escape/exfiltration at the kernel level on prod.

## The reference being generalized

The existing `templates/btcusdt-futures/harness/run_backtest.py` imports `strategy.py`
**in-process** and calls `signals(bars) -> [-1/0/1]`. That is fine for the trusted seed but
fatal for an arbitrary bot (RCE, state-poisoning, it could read the held-out data or rewrite
the score). P3 keeps the same *simulation* (leverage, stop, liquidation, commission, OOS tail)
but moves the bot behind a process boundary and a streaming protocol.

## Protocol (P3.1) — streaming, hardened against exfiltration

JSON-lines over the bot subprocess's stdin/stdout. **Strict request→response**: the engine
sends one message and waits for exactly one reply before the next. Both sides flush after every
line.

1. **init** — engine→bot: `{"type":"init","schema_version":"1","params":{...tunables...}}`.
   bot→engine: `{"type":"ready"}`.
2. **bar** — engine→bot, per bar in chronological order:
   `{"type":"bar","n":<int>,"o":..,"h":..,"l":..,"c":..,"v":..}`.
   bot→engine: `{"type":"order","n":<int>,"want":-1|0|1}` (desired position at bar n,
   decided from bars 0..n only — look-ahead is physically impossible).
3. **end** — engine→bot: `{"type":"end"}`. The engine then computes ALL PnL/metrics itself.

### Hardening folded in from the cross-AI review (critical)

- **NO absolute timestamp in `bar`.** Real timestamps let the bot fingerprint the date range
  and recall it from training memory — exfiltration without look-ahead. Bars carry only OHLCV
  + a bar counter `n`. (Calendar features are a documented limitation; revisit only if needed,
  via day-of-week/hour-of-day derived in the engine, never an absolute date.)
- **Randomized-but-seeded OOS split.** The scored OOS segment does NOT start at a fixed ratio.
  Its start is drawn from a band (e.g. [60%, 80%] of bars) using a per-project seed fixed at
  project creation. The bot never sees the seed or the boundary, so it cannot behave
  differently on the scored segment; yet re-runs are reproducible (the seed is fixed), so the
  determinism check still works.
- **Determinism verified across ≥2 runs.** A bot can be deterministic then diverge once it
  infers it's "in production". The engine runs the bot at least twice and rejects if the order
  sequences differ. (Implemented in P3.1's engine option; enforced by P3.2/P3.3 config.)
- **Masked failures.** The bot NEVER receives error detail. Protocol violations, bad JSON,
  timeouts → the parent logs internally and the evaluation fails with a generic message; no
  traceback or parser detail flows back to the bot/optimizer.
- **Bounded I/O.** Per-message read deadline AND a hard cap on total bytes read from the bot
  (e.g. 1 MB); non-protocol stdout lines are ignored. The bot cannot stall or flood the engine.
- **close_fds on spawn.** The child inherits no engine file descriptors (no leak of the data
  file or OOS split via /proc/self/fd).

## Engine (P3.1)

Generalize the `run_backtest.py` simulation into a reusable function that takes `bars` + an
**order sequence obtained via the protocol** (not in-process `signals()`), applies the same
leverage/stop/liquidation/commission model, and computes return/drawdown/liquidations/etc. on
the seeded OOS tail. The engine is the trusted judge; it never trusts any number the bot prints.

P3.1 spawns the bot as a plain `subprocess` (process separation, `close_fds`, timeouts, bounded
I/O) — but **no OS sandbox yet**. It is therefore used ONLY with the trusted reference adapter
in tests; running an arbitrary untrusted bot is gated until P3.2. The code carries a loud guard.

## Reference adapter (P3.1)

A small `bot_adapter.py` that an onboarded bot is wrapped with: it reads the protocol on stdin,
maintains the growing bar history, calls a `signals`-style decision per bar (using only bars so
far), and writes `order` lines. This is the standard interface a bot must speak; it also lets us
test the engine end-to-end with a known-good bot.

## Sandbox (P3.2 — Linux only, deferred)

`unshare(CLONE_NEWNET|CLONE_NEWNS|CLONE_NEWPID)` (no network, isolated FS/PID, before execve) +
tmpfs cwd + read-only minimal bind mounts (no engine code/data/OOS reachable) +
`PR_SET_NO_NEW_PRIVS` + seccomp whitelist + rlimits (CPU/AS/NOFILE/NPROC=0/FSIZE=0) + dedicated
uid + bounded pipe. Prefer a vetted jailer (NsJail) over hand-rolled. **Build and validate on
Linux** — macOS has no equivalent; on macOS dev the bot runs protocol-isolated only.

## Out of scope here

P4 (secondary validation + evidence report), P5 (UI). Calendar/time features for bots that need
absolute dates (documented limitation).
