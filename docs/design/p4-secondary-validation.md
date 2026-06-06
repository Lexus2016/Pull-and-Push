# P4 — Secondary validation + evidence report (design + P4.1 plan)

Status: **design**. Implements phase **P4** of [`onboarding-existing-bots.md`](./onboarding-existing-bots.md).
These are the guardrails the ADR keeps as **SECONDARY** checks (NOT the trust mechanism — the
vetted engine + sandbox are). They run before/around optimization and surface evidence for a
human to approve.

CLI brand is **`pull-and-push`** (package stays `tyani_tolkai`).

## Decomposition

- **P4.1 — Validation core + evidence report** (pure Python, fast, no Docker): control strategies
  + discrimination verdict, determinism check, artifact hashing (provenance), in-sample↔OOS gap
  (overfit signal), and the human-readable evidence report. Fully testable without Docker.
- **P4.2 — Sandboxed probes** (Docker): `pull-and-push validate` CLI + real-bot determinism (two
  sandboxed runs) + anti-look-ahead probe (re-score on a perturbed/shifted timeline; flag
  anomalous score jumps).

## Design decision: spectrum, not a brittle ordering assert

The ADR sketches "flat < random < buy-and-hold < bot". A hard ordering assert is fragile —
buy-and-hold LOSES to flat in a downtrend. So P4.1 computes the control **spectrum** (flat /
random / buy-and-hold scores via the SAME vetted engine) and the meaningful, non-fragile verdict
is: **the bot must beat `flat` and `random`** (a bot that can't beat doing-nothing or a coin-flip
is suspect). All control numbers go into the report for the human to eyeball. The controls also
prove the engine DISCRIMINATES (distinct scores for distinct strategies — a broken scorer would
flatten them).

Controls are OUR trusted code → they generate order sequences in-process and run through
`bot_engine.simulate` directly (no sandbox needed; the sandbox is only for the untrusted bot).

## P4.1 module: `src/tyani_tolkai/validation.py`

- `flat_orders(n) -> list[int]` = all 0.
- `buy_and_hold_orders(n) -> list[int]` = `[1] + [0]*(n-1)` (long from bar 0, hold).
- `random_orders(n, *, seed) -> list[int]` = deterministic `random.Random(seed)` choices of -1/0/1.
- `score_controls(bars, *, oos_start, params) -> dict[str, dict]` = {"flat":m, "buy_and_hold":m,
  "random":m} via `simulate`.
- `beats_controls(bot_metrics, control_metrics) -> dict` = {"beats_flat":bool, "beats_random":bool}
  comparing `return_oos_pct`.
- `check_determinism(score_callable, *, runs=2) -> tuple[bool, list[dict]]` = call N times, ok = all
  equal. (Generic — works with the engine, the subprocess scorer, or the sandboxed scorer.)
- `hash_artifacts(*, engine_path, data_path, config) -> dict[str,str]` = sha256 of the engine
  source, the data bytes, and canonical-JSON config — a provenance fingerprint the human approves.
- `insample_oos_gap(metrics) -> float` = `in_sample_return_pct - return_oos_pct` (large positive =
  overfitting).
- `build_evidence_report(*, bot_name, bot_metrics, control_metrics, beats, determinism_ok, hashes,
  gap) -> str` = Markdown: provenance hashes, control spectrum, beats verdict, determinism verdict,
  overfit gap, overall **PASS/FLAG** (FLAG if the bot fails to beat a control, or non-deterministic,
  or a large overfit gap).

## P4.1 plan (TDD)

### Task 1: control strategies + scoring
**Files:** `src/tyani_tolkai/validation.py` (create), `tests/test_validation.py` (create).
- `flat_orders`, `buy_and_hold_orders`, `random_orders(seed)` (deterministic), `score_controls`,
  `beats_controls`.
- Tests: flat→all 0; buy_and_hold→[1,0,..]; random deterministic for a seed (two calls equal) and
  values ⊂ {-1,0,1}; `score_controls` returns the 3 keys each with `return_oos_pct`; `beats_controls`
  true/false correctly vs given control metrics.

### Task 2: determinism + hashing + overfit gap
**Files:** modify `validation.py` + `tests/test_validation.py`.
- `check_determinism(callable, runs=2)`: deterministic fake → (True, ...); a counter fake → (False, ...).
- `hash_artifacts`: temp engine/data files + a config dict → stable sha256s; changing any input
  changes its hash; missing file → clear error.
- `insample_oos_gap`: from a metrics dict → correct difference.

### Task 3: evidence report
**Files:** modify `validation.py` + `tests/test_validation.py`.
- `build_evidence_report(...)` → Markdown starting `# Evidence Report`, with sections for provenance,
  control spectrum, beats verdict, determinism, overfit gap, and an overall verdict line. Test: a
  bot that beats controls + deterministic + small gap → "PASS"; a bot that loses to `flat` →
  "FLAG" and the reason surfaced.
- Full suite green; no new failures vs the 48 pre-existing PyPy/sqlite.

## Out of scope (P4.2 / later)
`pull-and-push validate` CLI; real-bot sandboxed determinism (2 runs) + anti-look-ahead perturbation
probe (Docker); immutable engine-version pinning enforcement; bounded-iteration budget (already in
the orchestrator's `limits`).
