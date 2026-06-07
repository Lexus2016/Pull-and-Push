# P5 — Intent-first onboarding UI

Status: **built** (P5.1–P5.3). The final phase of
[`onboarding-existing-bots.md`](./onboarding-existing-bots.md): a web path that lets a user OPTIMIZE
an existing bot, alongside the existing "start from a template" path. It surfaces the already-built,
tested P1–P4.6 backend (profile → propose → gen-adapter → check-adapter → onboard → validate → run).

## Decisions (made to minimize end-user questions)

- **Two intents** on the "new project" surface: *Start from an example* (existing scaffold flow,
  unchanged) and *Improve my existing bot* (new guided stepper).
- **`decide()` fill = CLI/editor handoff** (not LLM auto-fill, not an embedded editor — both deferred).
  The one inherently non-web step (wiring the bot's logic) stays in the user's editor; the UI
  generates the stub and shows the `check-adapter` verdict when they return. Honest MVP on the
  current backend; lowest new risk.
- **Defaults everywhere** so the user mostly clicks "Next": accept proposed metrics as-is, seed =
  project name, agents = claude+claude. Safety verdicts (profile glance, check-adapter, validate)
  are SHOWN, not interrogated.
- **Layered safety is surfaced, not bypassed**: check-adapter (protocol) → validate (semantics via
  control spectrum + anti-look-ahead + evidence report) → human. The UI never auto-runs the whole
  chain past these gates.

## Backend endpoints (thin wrappers over tested functions)

Fast/deterministic (sync) — **built in increment 1**:
- `POST /api/gen-adapter` {bot_dir, profile?, out?, force?} → writes adapter.py (`render_adapter_stub`).
- `POST /api/check-adapter` {bot_dir, bot_cmd, n?, timeouts} → `drive_bot` ×2 + `check_adapter_orders`
  verdict (200 even on FLAG; `ok` carries PASS/FLAG).
- `POST /api/onboard` {project, bot_dir, data, proposal, bot_cmd, seed?, goal?} → `scaffold_onboarding`.

LLM/Docker (async, run-thread pattern) — **increment 2**:
- `POST /api/profile` (LLM, `profiler.analyze_bot`) — async; poll for the BotProfile.
- `POST /api/propose` (LLM, `proposer.propose_evaluation`) — async; poll for the proposal.
- `POST /api/validate` (Docker, the P4.2 evidence battery) — async; poll for the evidence report.

## Frontend — **increment 3**

An "Improve my bot" stepper in `static/index.html`: point at bot+data+goal → Profile → Metrics →
Adapter (gen + "fill decide() then Check") → Onboard → Validate → Run. Verify visually
(browser/Playwright) since it is subjective UI.

## Incremental plan
1. **Deterministic backend endpoints** (gen-adapter, check-adapter, onboard) + tests. ✅ DONE.
2. **LLM/Docker endpoints** (profile, propose, validate) — built SYNCHRONOUS (like /api/configure),
   not async; `run_secondary_validation` extracted so the CLI + web share the P4 battery. + tests. ✅ DONE.
3. **The "Improve my bot" stepper UI** in static/index.html. ✅ DONE — verified live: node --check on
   the inline JS, the card renders in the running server's DOM (0 console errors), and the
   deterministic endpoints (gen/check/onboard) pass end-to-end via curl + Playwright. The LLM steps
   (profile/propose) and Docker validate need a real env to click through.

## Out of scope
LLM auto-fill of `decide()`; embedded web code editor; multi-language bots; one-click no-gate chain.
