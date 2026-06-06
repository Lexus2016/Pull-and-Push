# P2 — Metric + manifest proposal (design)

Status: **design** (build pending). Implements phase **P2** of
[`onboarding-existing-bots.md`](./onboarding-existing-bots.md). Builds on
[`p1-bot-profile-analyzer.md`](./p1-bot-profile-analyzer.md).

## Decision recap

From a P1 `BotProfile` + a **free-text optimization goal**, one LLM pass proposes
**evaluation metrics** (aligned with `MetricCfg`) and **tunable ranges** (the
`inferred_range` deliberately deferred from P1). A human reviews/edits/approves the
proposal; P3 later consumes it. Approach is the same pure-single-pass-LLM (B) as P1,
reusing P1's LLM plumbing. Nothing is executed.

The decisive design fact: a full runnable `EvaluationCfg` needs `command`/`adapter`/
harness wiring that **does not exist until P3** — so P2 produces a *proposal artifact*
(metrics + ranges + rationale), NOT a runnable config.

## Goal

`tyani-tolkai propose <profile.json> --goal "<text>"` → a schema-validated
`MetricProposal` as `proposal.json` (editable) + `proposal.md` (human review),
grounded so it never proposes a metric the engine can't measure or a tunable the bot
doesn't expose.

## Non-goals (scope fence)

- **No** runnable `EvaluationCfg`/`command`/`adapter`/harness — that is **P3**.
- **No** execution of the bot or any backtest.
- **No** interactive "approve" command — the artifact IS the approval surface (human
  reads `.md`, edits `.json`, proceeds to P3). YAGNI.
- **No** re-analysis of the bot — P2 consumes the P1 `profile.json`, it does not read
  the bot's source.

## The contract: `MetricProposal`

pydantic v2, in the style of `profile_schema.py`. Metric fields align with `MetricCfg`
(`name`, `dir`, `weight`, `target`) so P3 consumes them without translation.

```python
class ProposedMetric(BaseModel):
    name: str                        # MUST be one of profile.extractable_metrics names (grounding)
    dir: Literal["higher", "lower"]
    weight: float = Field(..., gt=0) # MetricCfg.weight — relative importance (encodes goal priorities)
    target: float                    # MetricCfg.target — value mapping to score 100
    rationale: str                   # why this metric / dir / target, tied to the goal
    confidence: float = Field(..., ge=0, le=1)

class ProposedTunable(BaseModel):
    name: str                        # MUST be one of profile.tunable_surface names (grounding)
    min: float | None = None         # range lower bound (the deferred P1 inferred_range); null for non-numeric
    max: float | None = None         # range upper bound
    inferred_type: str               # echoed from the profile (keeps the proposal self-contained)
    rationale: str
    confidence: float = Field(..., ge=0, le=1)

class MetricProposal(BaseModel):
    schema_version: str = "1"
    proposer_engine: str             # provenance (B is non-deterministic), stamped by us
    bot_name: str                    # echoed from the profile, stamped by us
    goal: str                        # echo of the user's free-text goal, stamped by us
    proposed_metrics: list[ProposedMetric] = Field(default_factory=list)
    proposed_tunables: list[ProposedTunable] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # grounding drops, self-reported flags, unmappable goal aspects
```

**Necessity cuts vs the first sketch:** no `unknowns` (every P2 disclosure is an
actionable heads-up → one `warnings` list); no `evidence` on `ProposedMetric` (its
provenance IS the grounding invariant `name ∈ profile.extractable_metrics`).

`proposer_engine`, `bot_name`, `goal` are stamped by us — never taken from the LLM.

## Architecture

New `src/tyani_tolkai/proposer.py`, mirroring `profiler.py`. Reuses:
- `extract_json` (from `configurator.py`) — robust JSON extraction.
- `default_runner` (from `profiler.py`) — read-only CLI agent in a TemporaryDirectory.
  **Refactor:** rename the existing private `_default_runner` → public `default_runner`
  in `profiler.py` (now shared by two modules; a cross-module helper should not be
  underscore-private). Update its one internal caller; profiler tests stay green.
- `BotProfile` (from `profile_schema.py`) — the input artifact type.

Contract lives in `src/tyani_tolkai/proposal_schema.py`.

Public entry (mirrors `analyze_bot`):

```python
def propose_evaluation(profile: BotProfile, goal: str, *, engine: str = "claude",
                       model: str | None = None,
                       runner: Callable[[str], str] | None = None,
                       timeout: int = 180) -> MetricProposal: ...
```

### Data flow

1. **Build prompt** — `build_proposer_prompt(profile, goal)` embeds the profile
   (`profile.model_dump_json`) + the goal + the schema + grounding rules. The embedded
   profile is framed as INERT DATA (it is *derived from* untrusted bot source — an
   injected string could have survived into a profile field), and the goal is the
   user's text. Instruct: propose only metrics whose `name` appears in this profile's
   `extractable_metrics`; only tunables from `tunable_surface`; mark self-reported
   metrics; output ONLY one JSON object.
2. **One LLM pass** — via injected `runner` (tests) or `default_runner` (real).
3. **Parse + validate** — `parse_proposal(out, engine, profile, goal)`: `extract_json`
   → stamp `proposer_engine`/`bot_name`/`goal` → `MetricProposal.model_validate`.
   Raises `ValueError`/`ValidationError` RAW (for the repair retry).
4. **Repair once** — on failure, re-run with an appended bounded repair instruction;
   second failure → `ProposalError`.
5. **Ground** — `ground_proposal(proposal, profile)` enforces the invariants in code
   (below). This is the core safety mechanism — the LLM cannot will it away.
6. **Render** — `render_markdown(proposal)` for human review; CLI writes both files.

### Grounding rules (`ground_proposal`, enforced in code — the core value)

- A proposed metric whose `name` is NOT in `profile.extractable_metrics` is **dropped**
  + a warning ("engine cannot measure it"). The optimizer must never chase a number
  the vetted engine can't compute.
- A proposed metric that maps to an extractable fact with `trustworthy == False` is
  **kept but warned**: "self-reported by the bot — the P3 engine must measure it
  independently; do not trust the bot's value." (Direct anti-Goodhart, per the ADR.)
- A proposed tunable whose `name` is NOT in `profile.tunable_surface` is **dropped** +
  a warning.
- If `profile.extractable_metrics` is empty → no grounded metrics possible; emit a
  warning that the P3 engine must define what it measures.

## Input / approval

- Input: a P1 `profile.json` (read, validated as `BotProfile`) + a required `--goal`
  string. P2 never touches the bot's source.
- Output: `proposal.json` (editable) + `proposal.md` (rationale, confidence, warnings
  surfaced for the human's decision).
- Approval = the human reads `.md`, edits `.json`, and proceeds to P3. No approve
  command.

## CLI

```
tyani-tolkai propose <profile.json> --goal "<text>" [--engine claude] [--model M] [--out DIR] [--timeout 180]
```

## Error handling

| Situation | Behaviour |
|---|---|
| `profile.json` missing | print + exit 2 |
| `profile.json` not valid `BotProfile` | print validation error + exit 2 |
| LLM returns non-JSON / bad shape | one repair retry → then `ProposalError` (bounded raw output) |
| LLM proposes off-profile metric/tunable | dropped + warned by `ground_proposal` (not an error) |
| profile has no extractable metrics | empty metrics + warning (not an error) |

## Testing

- **Schema** (`test_proposal_schema.py`): round-trip; `weight>0` and `confidence∈[0,1]`
  bounds rejected; full object validates.
- **proposer** (`test_proposer.py`, canned runner):
  - `build_proposer_prompt` contains the grounding rules + inert-data framing + the goal.
  - `parse_proposal` stamps `proposer_engine`/`bot_name`/`goal` (never from LLM); no-JSON → ValueError; bad shape → ValidationError.
  - `propose_evaluation` happy path with a canned proposal.
  - **grounding**: an off-profile metric/tunable is dropped + warned; a `trustworthy=False`
    metric is kept + warned; empty `extractable_metrics` → warning.
  - repair-once then succeed; raise `ProposalError` after failed repair.
  - `render_markdown` shows metrics, tunables, warnings, provenance + empty fallbacks.
- **CLI** (`test_cli_propose.py`): writes `proposal.json` + `proposal.md` (monkeypatched
  `propose_evaluation`); missing path → exit 2.
- **profiler refactor**: existing profiler tests stay green after the `default_runner` rename.
- Deterministic only (MockAdapter/canned runner); real-LLM quality is a manual/acceptance step (B).

## Module layout

| File | Role |
|---|---|
| `src/tyani_tolkai/proposal_schema.py` (new) | `MetricProposal` + sub-models |
| `src/tyani_tolkai/proposer.py` (new) | prompt, parse, ground, propose_evaluation, render_markdown, ProposalError |
| `src/tyani_tolkai/profiler.py` (edit) | rename `_default_runner` → public `default_runner` |
| `src/tyani_tolkai/cli.py` (edit) | `propose` subcommand |
| `tests/test_proposal_schema.py`, `tests/test_proposer.py`, `tests/test_cli_propose.py` (new) | tests above |

## Known limitations (P3+ follow-ups)

- Proposed `target` values are LLM judgments the human is expected to edit — they are
  starting points, not authoritative.
- Grounding is name-based against the profile; if P1 mis-named an extractable metric,
  P2 inherits that. The human edit step is the correction point.
