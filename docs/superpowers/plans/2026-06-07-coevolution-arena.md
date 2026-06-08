# Co-Evolution Arena (symmetric Rival↔Rival) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a fully working, tested symmetric Rival↔Rival ("Co-Evolution Arena") mode where
two agents co-evolve artifacts A and B judged by a deterministic referee, proven to converge on
a CEGIS toy domain before any real domain.

**Architecture:** The referee is wrapped as a Phase-1 metric adapter so the existing
`Orchestrator.run_loop` runs unchanged as a local-best generator against a *frozen* opponent
pool. A thin `SymmetricOrchestrator` alternates generations, maintains a champion archive + a
match matrix (the arena's true evaluator), applies a promotion gate, computes live/stable
signals, and stops on dominance/equilibrium/budget. Deterministic scripted rivals drive the toy
in tests; real claude/codex CLI agents are a config swap.

**Tech Stack:** Python 3.12, pydantic, SQLite + git (existing state), pytest. Design:
`docs/design/p6-coevolution-arena.md`.

**Test command (ALWAYS):** `.venv/bin/python -m pytest` (pyenv PyPy gives false cffi-sqlite
failures). Run single tests with `.venv/bin/python -m pytest tests/test_x.py::test_y -v`.

---

## File Structure

- Create `src/tyani_tolkai/arena/__init__.py` — package marker, re-exports.
- Create `src/tyani_tolkai/arena/referee.py` — `MatchOutcome`, `Referee` protocol,
  `OpponentStrategy`, `get_referee()` registry.
- Create `src/tyani_tolkai/arena/cegis.py` — CEGIS toy: ground-truth `L`, `CegisReferee`
  (`play`, labeled `counterexamples`, `materialize_context`), `min_consistent_substring`.
- Create `src/tyani_tolkai/metrics/arena.py` — `ArenaMetricAdapter` (referee + frozen pool →
  single `arena_fitness` metric + report data).
- Create `src/tyani_tolkai/agents/scripted_rival.py` — `ScriptedRecognizerRival`,
  `ScriptedAdversaryRival` (deterministic toy agents for tests).
- Create `src/tyani_tolkai/symmetric.py` — `SymmetricOrchestrator`, pool building, promotion
  gate, match matrix, stopping rule, per-generation reset.
- Modify `src/tyani_tolkai/config.py` — `ArenaCfg`, unlock symmetric in `load_config`, rival
  role/engine validation.
- Modify `src/tyani_tolkai/state.py` — `champion` + `match` tables + helpers.
- Modify `src/tyani_tolkai/brief.py` — rival-aware standing line pointing at `.arena/`.
- Modify `src/tyani_tolkai/cli.py`, `src/tyani_tolkai/web/server.py` — symmetric run + dual-curve.
- Tests: `tests/test_referee.py`, `tests/test_cegis.py`, `tests/test_arena_adapter.py`,
  `tests/test_scripted_rival.py`, `tests/test_symmetric.py`, `tests/test_cegis_convergence.py`,
  `tests/test_config.py` (extend), `tests/test_state.py` (extend), `tests/test_cli_symmetric.py`.

---

## Phase P6.1 — Referee interface + CEGIS toy + oracle

### Task 1: `MatchOutcome` + `Referee` protocol + `OpponentStrategy`

**Files:**
- Create: `src/tyani_tolkai/arena/__init__.py`
- Create: `src/tyani_tolkai/arena/referee.py`
- Test: `tests/test_referee.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_referee.py
from tyani_tolkai.arena.referee import MatchOutcome, OpponentStrategy, get_referee

def test_match_outcome_zero_sum_helper():
    o = MatchOutcome.zero_sum(a_score=0.7)
    assert o.a_score == 0.7
    assert o.b_score == 0.3
    assert o.detail == {}

def test_get_referee_unknown_raises():
    import pytest
    with pytest.raises(KeyError):
        get_referee("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_referee.py -v`
Expected: FAIL (module `tyani_tolkai.arena.referee` not found).

- [ ] **Step 3: Write minimal implementation**

```python
# src/tyani_tolkai/arena/__init__.py
from .referee import MatchOutcome, Referee, OpponentStrategy, get_referee  # noqa: F401
```

```python
# src/tyani_tolkai/arena/referee.py
"""Referee — the deterministic, vetted trust anchor for symmetric mode (design §1).

A referee runs side A's artifact against side B's artifact in a sandbox and emits an
OBJECTIVE outcome. It is NEVER an LLM and neither side scores itself. Determinism and
mirror-symmetry are enforced by tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

OpponentStrategy = Literal["sample", "accumulate"]


@dataclass
class MatchOutcome:
    a_score: float                      # objective scalar in [0,1] from A's perspective
    b_score: float                      # B's perspective; zero-sum ⇒ a_score + b_score == 1
    detail: dict = field(default_factory=dict)

    @classmethod
    def zero_sum(cls, a_score: float, detail: dict | None = None) -> "MatchOutcome":
        return cls(a_score=a_score, b_score=1.0 - a_score, detail=detail or {})


class Referee(Protocol):
    name: str
    version: str
    opponent_strategy: OpponentStrategy
    def play(self, a_dir: Path, b_dir: Path, sandbox, *, seed: int) -> MatchOutcome: ...


_REGISTRY: dict[str, type] = {}


def register_referee(cls: type) -> type:
    _REGISTRY[cls.name] = cls
    return cls


def get_referee(name: str) -> Referee:
    if name not in _REGISTRY:
        raise KeyError(f"unknown referee: {name!r}")
    return _REGISTRY[name]()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_referee.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/arena/ tests/test_referee.py
git commit -m "feat(arena): Referee protocol + MatchOutcome + referee registry (P6.1)"
```

### Task 2: CEGIS ground-truth language + minimal-consistent recognizer learner

**Files:**
- Create: `src/tyani_tolkai/arena/cegis.py`
- Test: `tests/test_cegis.py`

The toy: target language **L = "s contains the substring 'ab'"** over alphabet {a,b}. Hypothesis
class for side A = "contains substring t". The learner `min_consistent_substring` finds the
shortest `t` consistent with all labeled examples (converges to `t='ab'`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cegis.py
from tyani_tolkai.arena.cegis import in_L, min_consistent_substring

def test_in_L_is_contains_ab():
    assert in_L("ab") and in_L("aab") and in_L("xaby" .replace("x","a").replace("y","b"))
    assert not in_L("a") and not in_L("ba") and not in_L("")

def test_min_consistent_substring_converges_to_ab():
    # labeled examples (string, label) where label = in_L(string)
    ex = [("ab", True), ("ba", False), ("a", False), ("b", False), ("aab", True)]
    t = min_consistent_substring(ex, alphabet="ab", max_len=3)
    assert t == "ab"

def test_min_consistent_substring_none_when_unpinned():
    # too few examples → shortest consistent substring may be ambiguous; returns a substring
    # consistent with all given examples (not necessarily 'ab')
    t = min_consistent_substring([("ab", True), ("", False)], alphabet="ab", max_len=3)
    assert t is not None and ("ab".find(t) != -1 or len(t) >= 1)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_cegis.py -v` → FAIL (no module).

- [ ] **Step 3: Write minimal implementation**

```python
# src/tyani_tolkai/arena/cegis.py  (part 1 — language + learner)
"""CEGIS toy domain (design §7): A recognizes hidden L; B probes for misclassifications;
the referee is the oracle that labels counterexamples. Deterministic — no LLM, no API."""
from __future__ import annotations

import itertools
from typing import Iterable

TARGET_SUBSTRING = "ab"


def in_L(s: str) -> bool:
    """Ground-truth L: s contains the substring 'ab'. Known only to the referee."""
    return TARGET_SUBSTRING in s


def _candidates(alphabet: str, max_len: int) -> Iterable[str]:
    for length in range(1, max_len + 1):
        for tup in itertools.product(alphabet, repeat=length):
            yield "".join(tup)


def min_consistent_substring(examples, alphabet: str = "ab", max_len: int = 4) -> str | None:
    """Shortest substring t such that ('t in s') agrees with every (s, label) example.
    Deterministic tie-break: shortest first, then lexicographic. None if nothing consistent."""
    for t in _candidates(alphabet, max_len):           # length-ascending, lexicographic
        if all((t in s) == label for s, label in examples):
            return t
    return None
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_cegis.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/arena/cegis.py tests/test_cegis.py
git commit -m "feat(arena): CEGIS toy language L='contains ab' + min-consistent learner (P6.1)"
```

### Task 3: `CegisReferee.play` — deterministic, oracle-graded match

**Files:**
- Modify: `src/tyani_tolkai/arena/cegis.py`
- Test: `tests/test_cegis.py`

The referee reads side A's `recognizer.py` (exposes `accepts(s)->bool`) and side B's
`strings.txt` (one probe per line). It runs A's recognizer on B's probes **inside the sandbox**,
then grades each result against `in_L` (the oracle). `a_score` = fraction A classified correctly;
zero-sum. `detail` carries the labeled counterexamples A got wrong (for A's next brief).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cegis.py  (append)
from pathlib import Path
from tyani_tolkai.arena.cegis import CegisReferee
from tyani_tolkai.sandbox import LocalSandbox  # confirm class name in sandbox.py during Step 3

def _write(p: Path, name: str, body: str):
    (p / name).write_text(body, encoding="utf-8")

def test_cegis_play_perfect_recognizer_scores_one(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    _write(a, "recognizer.py", "def accepts(s):\n    return 'ab' in s\n")
    _write(b, "strings.txt", "ab\nba\naab\nb\n")
    ref = CegisReferee()
    out = ref.play(a, b, LocalSandbox(), seed=0)
    assert out.a_score == 1.0 and out.b_score == 0.0
    assert out.detail["counterexamples"] == []      # A is perfect → none

def test_cegis_play_accept_all_gives_counterexamples(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    _write(a, "recognizer.py", "def accepts(s):\n    return True\n")
    _write(b, "strings.txt", "ab\nba\nb\n")          # 'ba','b' ∉ L but accept-all says True
    ref = CegisReferee()
    out = ref.play(a, b, LocalSandbox(), seed=0)
    assert out.a_score == 1/3                          # only 'ab' correct
    labels = dict((c["s"], c["label"]) for c in out.detail["counterexamples"])
    assert labels == {"ba": False, "b": False}

def test_cegis_play_deterministic(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    _write(a, "recognizer.py", "def accepts(s):\n    return s.endswith('b')\n")
    _write(b, "strings.txt", "ab\nba\nb\naab\n")
    ref = CegisReferee()
    o1 = ref.play(a, b, LocalSandbox(), seed=0)
    o2 = ref.play(a, b, LocalSandbox(), seed=0)
    assert o1 == o2
```

- [ ] **Step 2: Run** the three tests → FAIL (no `CegisReferee`). Also confirm the sandbox class
  name/constructor by reading `src/tyani_tolkai/sandbox.py` and fix the import if needed.

- [ ] **Step 3: Write minimal implementation**

```python
# src/tyani_tolkai/arena/cegis.py  (append — referee)
import json
from pathlib import Path

from .referee import MatchOutcome, register_referee


@register_referee
class CegisReferee:
    name = "cegis-recognizer"
    version = "1"
    opponent_strategy = "accumulate"     # A scored vs the GROWING union of B's probes (design §3)

    def _probe_strings(self, b_dir: Path) -> list[str]:
        raw = (b_dir / "strings.txt").read_text(encoding="utf-8") if (b_dir / "strings.txt").exists() else ""
        # dedupe, keep order, drop blanks; bound to keep the match cheap and deterministic
        seen, out = set(), []
        for line in raw.splitlines():
            s = line.rstrip("\n")
            if s not in seen:
                seen.add(s); out.append(s)
        return out[:512]

    def play(self, a_dir: Path, b_dir: Path, sandbox, *, seed: int) -> MatchOutcome:
        probes = self._probe_strings(b_dir)
        if not probes:
            return MatchOutcome.zero_sum(1.0, {"counterexamples": [], "n": 0})
        # run A's recognizer on the probes INSIDE the sandbox (A is untrusted code)
        runner = (
            "import json,sys\n"
            "sys.path.insert(0, '.')\n"
            "import recognizer\n"
            "probes = json.loads(sys.argv[1])\n"
            "print(json.dumps([bool(recognizer.accepts(s)) for s in probes]))\n"
        )
        (a_dir / "_arena_runner.py").write_text(runner, encoding="utf-8")
        try:
            res = sandbox.run(
                f'{{python}} _arena_runner.py {json.dumps(json.dumps(probes))}',
                cwd=a_dir, timeout=60,
            )
            verdicts = json.loads(res.stdout)
        finally:
            (a_dir / "_arena_runner.py").unlink(missing_ok=True)
        correct, counter = 0, []
        for s, v in zip(probes, verdicts):
            truth = in_L(s)
            if bool(v) == truth:
                correct += 1
            else:
                counter.append({"s": s, "label": truth})   # ORACLE-labeled counterexample for A
        a_score = correct / len(probes)
        return MatchOutcome.zero_sum(a_score, {"counterexamples": counter, "n": len(probes)})
```

Note: `{python}` is substituted by the sandbox the same way `metrics/numeric.py` does it — but
the adapter (Task 11) resolves it before calling the referee, OR the referee resolves via
`getattr(sandbox, "python", sys.executable)`. Confirm during Step 3 and mirror
`metrics/numeric._resolve_python`. Keep the referee's command portable.

- [ ] **Step 4: Run** the CEGIS referee tests → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/arena/cegis.py tests/test_cegis.py
git commit -m "feat(arena): CegisReferee.play — sandboxed, oracle-graded, deterministic (P6.1)"
```

### Task 4: Referee determinism + mirror-symmetry contract test

**Files:**
- Test: `tests/test_referee.py` (append)

- [ ] **Step 1: Write the failing test** (drives nothing new if Task 3 is correct — it locks the
  invariant so future referees can't break it).

```python
# tests/test_referee.py  (append)
from pathlib import Path
from tyani_tolkai.arena.cegis import CegisReferee
from tyani_tolkai.sandbox import LocalSandbox

def test_referee_zero_sum_invariant(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    (a / "recognizer.py").write_text("def accepts(s):\n    return 'a' in s\n", encoding="utf-8")
    (b / "strings.txt").write_text("ab\nba\nb\n", encoding="utf-8")
    out = CegisReferee().play(a, b, LocalSandbox(), seed=1)
    assert abs((out.a_score + out.b_score) - 1.0) < 1e-9
```

- [ ] **Step 2: Run** → PASS (invariant already holds). If it fails, fix `MatchOutcome.zero_sum`.
- [ ] **Step 3: (no impl needed)**
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_referee.py tests/test_cegis.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add tests/test_referee.py
git commit -m "test(arena): lock referee zero-sum/determinism invariants (P6.1)"
```

---

## Phase P6.2 — Config: unlock symmetric + ArenaCfg + validation

### Task 5: `ArenaCfg` model + `Config.arena` + symmetric role validation

**Files:**
- Modify: `src/tyani_tolkai/config.py`
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (append)
import pytest
from tyani_tolkai.config import Config

def _base_symmetric(**over):
    d = {
        "project": "p", "mode": "symmetric",
        "agents": {"rival_a": {"engine": "mock"}, "rival_b": {"engine": "mock"}},
        "roles": {"rival_a": {"goal": "recognize"}, "rival_b": {"goal": "fool"}},
        "evaluation": {"adapter": "numeric", "command": "x",
                       "metrics": [{"name": "arena_fitness", "dir": "higher", "target": 1.0, "worst": 0.0}]},
        "arena": {"referee": "cegis-recognizer"},
    }
    d.update(over); return d

def test_symmetric_config_parses_arena_defaults():
    cfg = Config(**_base_symmetric())
    assert cfg.mode == "symmetric"
    assert cfg.arena.referee == "cegis-recognizer"
    assert cfg.arena.aggregate == "mean_gated"
    assert cfg.arena.generations == 20
    assert cfg.arena.opponent_pool.k_past == 3

def test_symmetric_requires_both_rivals():
    bad = _base_symmetric(agents={"rival_a": {"engine": "mock"}})
    with pytest.raises(ValueError):
        Config(**bad)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_config.py -k symmetric -v` → FAIL
  (no `arena` field / no validation).

- [ ] **Step 3: Write minimal implementation** — add models before `class Config` and extend the
  validator.

```python
# config.py — add near the other *Cfg models
class OpponentPoolCfg(BaseModel):
    latest: bool = True
    k_past: int = 3

class ArenaCfg(BaseModel):
    referee: str
    aggregate: Literal["mean", "min", "mean_gated"] = "mean_gated"
    min_floor: float = 0.5            # mean_gated: worst-case floor below which mean is penalized
    penalty: float = 1.0
    opponent_pool: OpponentPoolCfg = OpponentPoolCfg()
    generations: int = 20
    per_generation_iterations: int = 8
    dominance_tau: float = 0.95
    dominance_rounds: int = 2
    plateau_generations: int = 4
    promote_regression_max: float = 0.1
```

```python
# config.py — Config: add field + extend validation
class Config(BaseModel):
    # ... existing fields ...
    arena: "ArenaCfg | None" = None

    @model_validator(mode="after")
    def _check_roles_and_engines(self) -> "Config":
        if self.mode == "symmetric":
            for r in ("rival_a", "rival_b"):
                if r not in self.agents:
                    raise ValueError(f"symmetric mode requires agents.{r}")
            if self.arena is None:
                raise ValueError("symmetric mode requires an 'arena' block")
            for r in ("rival_a", "rival_b"):
                eng = self.agents[r].engine
                if eng in ("opencode", "agy"):
                    raise ValueError(
                        f"rival {r} engine {eng!r} does not write to the subprocess cwd; "
                        "rival executors must be claude, codex, or mock (tests)")
            return self
        # asymmetric (unchanged Phase-1 logic) ...
        if "executor" not in self.agents:
            raise ValueError("agents.executor is required")
        if "validator" in self.agents:
            ex = self.agents["executor"].engine
            va = self.agents["validator"].engine
            if ex == va:
                warnings.warn(
                    f"executor and validator use the same engine ({ex!r}); "
                    "anti-collusion lock #3 recommends different providers",
                    UserWarning, stacklevel=2)
        return self
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_config.py -v` → PASS (existing
  asymmetric tests still green — verify the whole file).
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/config.py tests/test_config.py
git commit -m "feat(config): ArenaCfg + symmetric role/engine validation (P6.2)"
```

### Task 6: Unlock `load_config` for symmetric

**Files:**
- Modify: `src/tyani_tolkai/config.py:178-187`
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (append)
def test_load_config_symmetric_no_longer_raises(tmp_path):
    import yaml
    from tyani_tolkai.config import load_config
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(_base_symmetric()), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.mode == "symmetric"
```

- [ ] **Step 2: Run** → FAIL (`NotImplementedError`).
- [ ] **Step 3: Write minimal implementation** — replace the guard at `config.py:185`:

```python
def load_config(path: str | Path) -> Config:
    """Load and validate a run config from a YAML file (asymmetric or symmetric)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Config(**data)
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_config.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/config.py tests/test_config.py
git commit -m "feat(config): unlock symmetric mode in load_config — drop NotImplementedError (P6.2)"
```

---

## Phase P6.3 — State (champion + match) + ArenaMetricAdapter + promotion gate

### Task 7: `champion` + `match` tables + helpers

**Files:**
- Modify: `src/tyani_tolkai/state.py` (SCHEMA + methods)
- Test: `tests/test_state.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py  (append)
def test_champion_and_match_roundtrip(tmp_path):
    from tyani_tolkai.state import StateStore
    st = StateStore(tmp_path / "proj")
    cid_a = st.add_champion(run_id=1, side="A", generation=0, git_hash="aaa",
                            stable_score=0.4, repro={"pool_hash": "h", "referee_version": "1"})
    cid_b = st.add_champion(run_id=1, side="B", generation=0, git_hash="bbb", stable_score=0.3)
    st.record_match(run_id=1, a_champion_id=cid_a, b_champion_id=cid_b, a_score=0.7, seed=0)
    champs_a = st.champions(run_id=1, side="A")
    assert champs_a[0]["git_hash"] == "aaa" and champs_a[0]["generation"] == 0
    matrix = st.match_matrix(run_id=1)
    assert matrix[(cid_a, cid_b)] == 0.7
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_state.py -k "champion or match" -v` → FAIL.

- [ ] **Step 3: Write minimal implementation** — append to `SCHEMA` (after the `checkpoint`
  table) and add methods to `StateStore`:

```python
# state.py SCHEMA append:
CREATE TABLE IF NOT EXISTS champion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    side TEXT NOT NULL,
    generation INTEGER NOT NULL,
    git_hash TEXT NOT NULL,
    stable_score REAL,
    repro_json TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    a_champion_id INTEGER NOT NULL,
    b_champion_id INTEGER NOT NULL,
    a_score REAL NOT NULL,
    seed INTEGER NOT NULL,
    ts TEXT NOT NULL,
    UNIQUE (a_champion_id, b_champion_id, seed)
);
```

```python
# state.py StateStore methods:
def add_champion(self, run_id, side, generation, git_hash, stable_score=None, repro=None) -> int:
    import json
    cur = self.conn.execute(
        "INSERT INTO champion (run_id, side, generation, git_hash, stable_score, repro_json, ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, side, generation, git_hash, stable_score,
         json.dumps(repro) if repro else None, _now()))
    self.conn.commit()
    return cur.lastrowid

def champions(self, run_id, side=None) -> list[dict]:
    q = "SELECT * FROM champion WHERE run_id=?"
    args = [run_id]
    if side:
        q += " AND side=?"; args.append(side)
    q += " ORDER BY generation, id"
    return [dict(r) for r in self.conn.execute(q, args).fetchall()]

def record_match(self, run_id, a_champion_id, b_champion_id, a_score, seed) -> None:
    self.conn.execute(
        "INSERT OR REPLACE INTO match (run_id, a_champion_id, b_champion_id, a_score, seed, ts) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, a_champion_id, b_champion_id, a_score, seed, _now()))
    self.conn.commit()

def match_matrix(self, run_id) -> dict:
    rows = self.conn.execute(
        "SELECT a_champion_id, b_champion_id, a_score FROM match WHERE run_id=?", (run_id,)).fetchall()
    return {(r["a_champion_id"], r["b_champion_id"]): r["a_score"] for r in rows}
```

- [ ] **Step 4: Run** the state tests → PASS. Also run full `tests/test_state.py` (existing tables
  unaffected — the two new `CREATE TABLE IF NOT EXISTS` are additive).
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/state.py tests/test_state.py
git commit -m "feat(state): champion + match tables + helpers for the arena (P6.3)"
```

### Task 8: `new_run` per side/generation (confirm/extend)

**Files:**
- Modify: `src/tyani_tolkai/state.py` (only if `new_run`/run-creation does not already accept what
  we need — read it first)
- Test: `tests/test_state.py` (append)

- [ ] **Step 1: Read** `state.py` for the existing run-creation method (e.g. `new_run`/`create_run`)
  and its signature. The symmetric orchestrator needs to create an independent run row (fresh
  `best_score=None`, `plateau_count=0`, mode='symmetric') per side per generation.

- [ ] **Step 2: Write the failing test** (adapt the method name to what exists):

```python
# tests/test_state.py  (append)
def test_new_run_starts_fresh(tmp_path):
    from tyani_tolkai.state import StateStore
    st = StateStore(tmp_path / "proj")
    rid = st.new_run(mode="symmetric")
    assert st.best_score(rid) is None
```

- [ ] **Step 3: Implement** only if missing. If `new_run` already exists and returns a fresh row,
  this task is a no-op confirmation — skip impl, keep the test.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** (only if code changed; otherwise fold the test into Task 7's commit).

### Task 9: `ArenaMetricAdapter` — referee + frozen pool → single `arena_fitness`

**Files:**
- Create: `src/tyani_tolkai/metrics/arena.py`
- Test: `tests/test_arena_adapter.py`

The adapter satisfies `MetricAdapter.run(artifact_dir, sandbox, evaluation, timeout)`. It ignores
`evaluation.command`; it plays the referee for the LIVE side (`artifact_dir`) against each frozen
opponent dir in its pool, aggregates per design §2 into one `arena_fitness`, and exposes
`mean/min/per_opponent/counterexamples` in `MetricResult.data`. Constructed with the live side's
identity ('A' or 'B') because A is `a_dir` and B is `b_dir` in `referee.play`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arena_adapter.py
from pathlib import Path
from tyani_tolkai.metrics.arena import ArenaMetricAdapter
from tyani_tolkai.arena.cegis import CegisReferee
from tyani_tolkai.sandbox import LocalSandbox

class _EvalStub:
    metrics = ()  # adapter emits its own metric name; scorer config supplies the MetricCfg

def _mkA(p, body): (p).mkdir(parents=True, exist_ok=True); (p/"recognizer.py").write_text(body)
def _mkB(p, lines): (p).mkdir(parents=True, exist_ok=True); (p/"strings.txt").write_text(lines)

def test_adapter_scores_A_against_two_B_opponents(tmp_path):
    a = tmp_path/"a"; _mkA(a, "def accepts(s):\n    return 'ab' in s\n")
    b1 = tmp_path/"b1"; _mkB(b1, "ab\nba\n")        # A perfect → 1.0
    b2 = tmp_path/"b2"; _mkB(b2, "b\naab\n")        # A perfect → 1.0
    adapter = ArenaMetricAdapter(CegisReferee(), side="A", opponents=[b1, b2],
                                 aggregate="mean_gated", min_floor=0.5, penalty=1.0, seed=0)
    res = adapter.run(a, LocalSandbox(), _EvalStub(), 60)
    assert res.ok
    fit = next(m for m in res.metrics if m["name"] == "arena_fitness")
    assert fit["value"] == 1.0
    assert res.data["mean"] == 1.0 and res.data["min"] == 1.0

def test_adapter_mean_gated_penalizes_low_min(tmp_path):
    a = tmp_path/"a"; _mkA(a, "def accepts(s):\n    return True\n")   # accept-all
    b1 = tmp_path/"b1"; _mkB(b1, "ab\n")            # 'ab'∈L → correct → 1.0
    b2 = tmp_path/"b2"; _mkB(b2, "ba\nb\n")         # both ∉L, accept-all wrong → 0.0
    adapter = ArenaMetricAdapter(CegisReferee(), side="A", opponents=[b1, b2],
                                 aggregate="mean_gated", min_floor=0.5, penalty=1.0, seed=0)
    res = adapter.run(a, LocalSandbox(), _EvalStub(), 60)
    fit = next(m for m in res.metrics if m["name"] == "arena_fitness")
    # mean = (1.0 + 0.0)/2 = 0.5 ; min = 0.0 < floor 0.5 → 0.5 - 1.0*(0.5-0.0) = 0.0
    assert res.data["mean"] == 0.5 and res.data["min"] == 0.0
    assert fit["value"] == 0.0
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_arena_adapter.py -v` → FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/tyani_tolkai/metrics/arena.py
"""ArenaMetricAdapter — bridges a referee + frozen opponent pool into the Phase-1 scorer.

Emits ONE scored metric `arena_fitness` (the §2 gate, computed here) plus report-only
mean/min/per-opponent/counterexamples in MetricResult.data. The unchanged scorer + decide
consume `arena_fitness` exactly like any numeric metric.
"""
from __future__ import annotations

from pathlib import Path

from .base import MetricResult


class ArenaMetricAdapter:
    def __init__(self, referee, side, opponents, *, aggregate="mean_gated",
                 min_floor=0.5, penalty=1.0, seed=0):
        self.referee = referee
        self.side = side                      # 'A' or 'B' — which dir the live artifact is
        self.opponents = list(opponents)
        self.aggregate = aggregate
        self.min_floor = min_floor
        self.penalty = penalty
        self.seed = seed

    def _live_score(self, outcome) -> float:
        return outcome.a_score if self.side == "A" else outcome.b_score

    def _gate(self, scores: list[float]) -> tuple[float, float, float]:
        mean = sum(scores) / len(scores)
        mn = min(scores)
        if self.aggregate == "mean":
            fit = mean
        elif self.aggregate == "min":
            fit = mn
        else:  # mean_gated
            fit = mean if mn >= self.min_floor else mean - self.penalty * (self.min_floor - mn)
        return max(0.0, min(1.0, fit)), mean, mn

    def run(self, artifact_dir, sandbox, evaluation, timeout) -> MetricResult:
        artifact_dir = Path(artifact_dir)
        if not self.opponents:
            return MetricResult(metrics=[], logs="no opponents in pool", ok=False)
        per, scores, counters = [], [], []
        for opp in self.opponents:
            opp = Path(opp)
            a_dir, b_dir = (artifact_dir, opp) if self.side == "A" else (opp, artifact_dir)
            out = self.referee.play(a_dir, b_dir, sandbox, seed=self.seed)
            sc = self._live_score(out)
            scores.append(sc)
            per.append({"opponent": str(opp), "score": sc})
            counters.extend(out.detail.get("counterexamples", []))
        fit, mean, mn = self._gate(scores)
        metrics = [{"name": "arena_fitness", "value": fit, "dir": "higher", "weight": 1.0}]
        data = {"mean": mean, "min": mn, "per_opponent": per, "counterexamples": counters}
        return MetricResult(metrics=metrics, logs=f"arena_fitness={fit:.4f}", ok=True, data=data)
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_arena_adapter.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/metrics/arena.py tests/test_arena_adapter.py
git commit -m "feat(metrics): ArenaMetricAdapter → single gated arena_fitness from a frozen pool (P6.3)"
```

### Task 10: Promotion gate

**Files:**
- Create function in `src/tyani_tolkai/symmetric.py` (start the module)
- Test: `tests/test_symmetric.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_symmetric.py
from tyani_tolkai.symmetric import promotion_gate_ok

def test_promotion_gate_blocks_regression():
    # candidate's worst-case score across the whole opposing archive vs the prior champion's
    assert promotion_gate_ok(candidate_archive_min=0.8, prev_champion_archive_min=0.85,
                             promote_regression_max=0.1) is True   # 0.05 drop ≤ 0.1 → ok
    assert promotion_gate_ok(candidate_archive_min=0.6, prev_champion_archive_min=0.85,
                             promote_regression_max=0.1) is False  # 0.25 drop > 0.1 → blocked

def test_promotion_gate_first_champion_always_ok():
    assert promotion_gate_ok(candidate_archive_min=0.3, prev_champion_archive_min=None,
                             promote_regression_max=0.1) is True
```

- [ ] **Step 2: Run** → FAIL (no module).
- [ ] **Step 3: Write minimal implementation**

```python
# src/tyani_tolkai/symmetric.py  (begin module)
"""SymmetricOrchestrator — alternating self-play over the Phase-1 loop (design §4)."""
from __future__ import annotations


def promotion_gate_ok(candidate_archive_min, prev_champion_archive_min, promote_regression_max) -> bool:
    """Crown the candidate only if it does not regress against the WHOLE opposing archive by
    more than `promote_regression_max` vs the prior champion's archive-wide worst case (§3)."""
    if prev_champion_archive_min is None:
        return True
    return (prev_champion_archive_min - candidate_archive_min) <= promote_regression_max
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/symmetric.py tests/test_symmetric.py
git commit -m "feat(symmetric): champion promotion gate vs whole archive (P6.3)"
```

### Task 11: Stopping rule from the match matrix

**Files:**
- Modify: `src/tyani_tolkai/symmetric.py`
- Test: `tests/test_symmetric.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_symmetric.py  (append)
from tyani_tolkai.symmetric import dominance_reached, best_vs_all

def test_dominance_reached_when_A_beats_whole_B_archive():
    # champ ids: A=[1,3], B=[2,4]; matrix a_score keyed (a_id,b_id)
    matrix = {(1,2):0.6,(1,4):0.5,(3,2):0.97,(3,4):0.96}
    # latest A champ = 3 beats every B (>=0.95) → dominance for A
    assert dominance_reached(matrix, a_ids=[1,3], b_ids=[2,4], tau=0.95, side="A") is True
    assert dominance_reached(matrix, a_ids=[1,3], b_ids=[2,4], tau=0.95, side="B") is False

def test_best_vs_all_picks_robust_champion():
    matrix = {(1,2):0.6,(1,4):0.9,(3,2):0.8,(3,4):0.85}
    # A=1 worst=0.6 ; A=3 worst=0.8 → best-A-vs-all-B = champ 3
    assert best_vs_all(matrix, a_ids=[1,3], b_ids=[2,4], side="A") == 3
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Write minimal implementation**

```python
# symmetric.py  (append)
def _a_view(matrix, a_id, b_id, side):
    s = matrix.get((a_id, b_id))
    if s is None:
        return None
    return s if side == "A" else 1.0 - s

def dominance_reached(matrix, a_ids, b_ids, tau, side) -> bool:
    """Latest champion of `side` beats the ENTIRE opposing archive at >= tau."""
    if side == "A":
        latest = a_ids[-1]
        scores = [_a_view(matrix, latest, b, "A") for b in b_ids]
    else:
        latest = b_ids[-1]
        scores = [_a_view(matrix, a, latest, "B") for a in a_ids]
    scores = [s for s in scores if s is not None]
    return bool(scores) and all(s >= tau for s in scores)

def best_vs_all(matrix, a_ids, b_ids, side):
    """The champion of `side` with the best WORST-CASE score across the whole opposing archive."""
    ids = a_ids if side == "A" else b_ids
    best_id, best_worst = None, -1.0
    for cid in ids:
        if side == "A":
            worsts = [_a_view(matrix, cid, b, "A") for b in b_ids]
        else:
            worsts = [_a_view(matrix, a, cid, "B") for a in a_ids]
        worsts = [w for w in worsts if w is not None]
        if not worsts:
            continue
        w = min(worsts)
        if w > best_worst:
            best_worst, best_id = w, cid
    return best_id
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/symmetric.py tests/test_symmetric.py
git commit -m "feat(symmetric): matrix-based dominance + best-vs-all robustness picks (P6.4)"
```

---

## Phase P6.4 — Scripted rivals + SymmetricOrchestrator + convergence proof

### Task 12: Scripted rivals (deterministic toy agents)

**Files:**
- Create: `src/tyani_tolkai/agents/scripted_rival.py`
- Test: `tests/test_scripted_rival.py`

Both satisfy `AgentAdapter.run(brief, workdir, profile, timeout) -> RunResult`. They read the
opponent context the orchestrator materialized under `workdir/.arena/` (design §7), edit the
artifact, and report `changed`.

- `ScriptedRecognizerRival` (A): reads `workdir/.arena/counterexamples.json` (accumulated
  oracle-labeled), computes `min_consistent_substring`, writes `recognizer.py` returning `t in s`
  (default `t=""` → accept-all when no examples yet, i.e. `'' in s` is always True — a deliberately
  wrong seed so the arms race has somewhere to go).
- `ScriptedAdversaryRival` (B): reads `workdir/.arena/opponent_recognizer.py` (A frozen), and
  deterministically enumerates short {a,b} strings, writing those A currently classifies in a way
  worth probing (it cannot know L; it emits a deterministic enumeration batch that necessarily
  contains boundary cases). Writes `strings.txt`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scripted_rival.py
import json
from pathlib import Path
from tyani_tolkai.agents.scripted_rival import ScriptedRecognizerRival, ScriptedAdversaryRival

def test_recognizer_rival_fits_counterexamples(tmp_path):
    arena = tmp_path/".arena"; arena.mkdir()
    (arena/"counterexamples.json").write_text(json.dumps(
        [{"s":"ab","label":True},{"s":"ba","label":False},{"s":"b","label":False},{"s":"aab","label":True}]))
    r = ScriptedRecognizerRival()
    res = r.run("brief", tmp_path, "writeable", 60)
    assert res.changed
    src = (tmp_path/"recognizer.py").read_text()
    ns = {}; exec(src, ns)
    assert ns["accepts"]("ab") and ns["accepts"]("aab")
    assert not ns["accepts"]("ba") and not ns["accepts"]("b")

def test_adversary_rival_emits_probes(tmp_path):
    arena = tmp_path/".arena"; arena.mkdir()
    (arena/"opponent_recognizer.py").write_text("def accepts(s):\n    return True\n")
    b = ScriptedAdversaryRival()
    res = b.run("brief", tmp_path, "writeable", 60)
    assert res.changed
    probes = [l for l in (tmp_path/"strings.txt").read_text().splitlines() if l]
    assert len(probes) >= 4 and all(set(p) <= set("ab") for p in probes)

def test_recognizer_rival_no_examples_is_accept_all(tmp_path):
    (tmp_path/".arena").mkdir()
    (tmp_path/".arena"/"counterexamples.json").write_text("[]")
    ScriptedRecognizerRival().run("brief", tmp_path, "writeable", 60)
    ns = {}; exec((tmp_path/"recognizer.py").read_text(), ns)
    assert ns["accepts"]("anything") is True   # seed is deliberately wrong (accept-all)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_scripted_rival.py -v` → FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/tyani_tolkai/agents/scripted_rival.py
"""Deterministic scripted rivals for the CEGIS toy — stand-ins for LLM agents in tests and the
convergence proof. They read opponent context from workdir/.arena/ (materialized by the
SymmetricOrchestrator) and edit the artifact. No LLM, no API, fully reproducible."""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from .base import RunResult
from ..arena.cegis import min_consistent_substring


class ScriptedRecognizerRival:
    def run(self, brief, workdir, profile, timeout) -> RunResult:
        workdir = Path(workdir)
        ce_path = workdir / ".arena" / "counterexamples.json"
        examples = json.loads(ce_path.read_text()) if ce_path.exists() else []
        pairs = [(e["s"], bool(e["label"])) for e in examples]
        t = min_consistent_substring(pairs, alphabet="ab", max_len=4) if pairs else ""
        body = f"def accepts(s):\n    return {t!r} in s\n"
        target = workdir / "recognizer.py"
        old = target.read_text() if target.exists() else None
        target.write_text(body, encoding="utf-8")
        return RunResult(status="success" if body != old else "no_op",
                         stdout=f"fit t={t!r}", changed=body != old)


class ScriptedAdversaryRival:
    def __init__(self, max_len: int = 4):
        self.max_len = max_len

    def run(self, brief, workdir, profile, timeout) -> RunResult:
        workdir = Path(workdir)
        # deterministic enumeration of all {a,b} strings up to max_len (incl. "") — necessarily
        # contains L's boundary cases; bounded + reproducible.
        probes = [""]
        for length in range(1, self.max_len + 1):
            probes += ["".join(p) for p in itertools.product("ab", repeat=length)]
        body = "\n".join(probes) + "\n"
        target = workdir / "strings.txt"
        old = target.read_text() if target.exists() else None
        target.write_text(body, encoding="utf-8")
        return RunResult(status="success" if body != old else "no_op",
                         stdout=f"{len(probes)} probes", changed=body != old)
```

Note: this adversary is a fixed full-enumeration, so it converges in one generation — fine for the
*machinery* proof. Task 14's e2e also includes a STAGED adversary variant (reveals probes
gradually) to exercise multi-generation dynamics, archive, and the gate.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_scripted_rival.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/agents/scripted_rival.py tests/test_scripted_rival.py
git commit -m "feat(agents): deterministic scripted CEGIS rivals for tests + proof (P6.4)"
```

### Task 13: `SymmetricOrchestrator.run` — alternation, reset recipe, archive, matrix, stop

**Files:**
- Modify: `src/tyani_tolkai/symmetric.py`
- Test: `tests/test_symmetric.py` (append — orchestration with stub referee + scripted rivals)

This is the integrating task. Read before writing: `state.py` run-creation + git helpers
(`git_init`, `commit`, `head`, `reset_hard`, `tracked_files`), and how `cli.py`/`web` build an
`Orchestrator` (executor, metric_adapter, sandbox) so the sub-runs are wired the same way.

Responsibilities of `SymmetricOrchestrator.run()`:
1. Set up two `StateStore` tracks (A, B) under the parent project dir; `git_init` each artifact.
2. For each generation, for each side: build opponent pool (per `referee.opponent_strategy`),
   materialize `.arena/` context into the live workdir, seed from the previous champion (copy
   artifact), create a fresh sub-run, construct `ArenaMetricAdapter(referee, side, pool, ...)`,
   run `Orchestrator(...).run_loop(should_stop=bound to per_generation_iterations + remaining
   budget)`, take the sub-run best as candidate.
3. Apply the promotion gate (candidate vs whole opposing archive via the referee); if ok,
   `add_champion` + record its stable_score; always `record_match` of the new champion vs all
   opposing champions to fill the matrix.
4. Update stable signals; evaluate the stopping rule from the matrix.
5. Return an `ArenaResult` (generations run, stop reason, best_A_id, best_B_id, the two stable
   curves).

- [ ] **Step 1: Write the failing test** (full-enumeration adversary ⇒ converge fast):

```python
# tests/test_symmetric.py  (append)
from pathlib import Path
from tyani_tolkai.symmetric import SymmetricOrchestrator, ArenaResult
from tyani_tolkai.arena.cegis import CegisReferee
from tyani_tolkai.agents.scripted_rival import ScriptedRecognizerRival, ScriptedAdversaryRival
from tyani_tolkai.sandbox import LocalSandbox
from tyani_tolkai.config import Config

def _cfg():
    return Config(project="toy", mode="symmetric",
        agents={"rival_a": {"engine": "mock"}, "rival_b": {"engine": "mock"}},
        roles={"rival_a": {"goal": "recognize"}, "rival_b": {"goal": "fool"}},
        evaluation={"adapter":"numeric","command":"x",
                    "metrics":[{"name":"arena_fitness","dir":"higher","target":1.0,"worst":0.0}]},
        arena={"referee":"cegis-recognizer","generations":6,"per_generation_iterations":3,
               "dominance_tau":0.99,"dominance_rounds":1,"plateau_generations":3})

def test_symmetric_run_converges_and_stops(tmp_path):
    orch = SymmetricOrchestrator(
        cfg=_cfg(), root=tmp_path, referee=CegisReferee(),
        executor_a=ScriptedRecognizerRival(), executor_b=ScriptedAdversaryRival(),
        sandbox=LocalSandbox())
    result = orch.run()
    assert isinstance(result, ArenaResult)
    assert result.stop_reason in ("dominance", "plateau")
    # the delivered best-A recognizer must equal L on a held-out set
    best_a_dir = orch.champion_dir("A", result.best_a_id)
    ns = {}; exec((Path(best_a_dir)/"recognizer.py").read_text(), ns)
    for s in ["ab","aab","xabx".replace("x","a"),"b","ba","",  "aaa","bbb","aba"]:
        assert ns["accepts"](s) == ("ab" in s)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_symmetric.py::test_symmetric_run_converges_and_stops -v`
  → FAIL (no `SymmetricOrchestrator`).

- [ ] **Step 3: Write minimal implementation.** Implement `SymmetricOrchestrator` with the
  responsibilities above. Key details to honor while implementing:
  - Two tracks: `root/"side_A"`, `root/"side_B"` as `StateStore`s; `git_init` each.
  - `.arena/` materialization: for side A write `counterexamples.json` (accumulated, deduped union
    when `opponent_strategy=="accumulate"`); for side B write `opponent_recognizer.py` (the frozen
    latest A champion's recognizer). Keep `.arena/` out of the referee's artifact view (referee
    only reads `recognizer.py` / `strings.txt`).
  - Seeding: copy the previous champion's tracked artifact files into the fresh workdir before the
    sub-run (carry artifact + lineage, NOT best_score). First generation: empty seed → scripted-A
    writes accept-all, scripted-B enumerates.
  - Pool building: `accumulate` → the single accumulated opponent dir (one materialized opponent
    representing the union); `sample` → latest + k random past champion dirs (deterministic RNG
    seeded by generation, since `random` is unavailable in this env use
    `(generation * 1103515245 + 12345) % len`-style index selection — no `Math.random`/`Date`).
  - Sub-run bound: pass `should_stop` to `run_loop` that returns True once
    `orch.n >= per_generation_iterations`, and also stop when the parent's remaining budget ≤ 0.
  - Promotion gate: materialize the candidate, play it against EVERY opposing champion via the
    referee, take the worst-case (min) as `candidate_archive_min`; compare to the prior champion's
    archive-min; `promotion_gate_ok(...)`.
  - Matrix: after crowning (or even if gated-out, record the candidate-as-provisional only when
    crowned), `record_match` of the new champion vs all opposing champions.
  - Stable signal: A = accuracy of the champion vs a FIXED held-out labeled set (generate once at
    construction with a fixed enumeration, NOT shown to agents); B = misclassification rate B's
    champion induces on a FIXED reference recognizer (the gen-0 seed A). Persist on the champion.
  - Stopping: `dominance_reached(...)` for `dominance_rounds` consecutive generations, OR no stable
    improvement for `plateau_generations` (reuse `validation.detect_oscillation` if applicable), OR
    generations exhausted, OR budget.
  - `ArenaResult` dataclass: `generations, stop_reason, best_a_id, best_b_id, stable_a, stable_b`.
  - `champion_dir(side, champion_id)`: checkout/copy that champion's `git_hash` into a temp dir for
    delivery/inspection (use `state.checkout`/`git worktree`/copy — confirm available git helpers).

  Implement incrementally, running the test after each sub-part. The full `run()` is ~120-160
  lines; keep helper methods small (`_materialize_context`, `_seed_from_champion`, `_play_generation`,
  `_promote`, `_update_matrix`, `_stable_signal`, `_stop`).

- [ ] **Step 4: Run** the convergence orchestration test → PASS. Then full
  `.venv/bin/python -m pytest tests/test_symmetric.py -v`.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/symmetric.py tests/test_symmetric.py
git commit -m "feat(symmetric): SymmetricOrchestrator — alternation, archive, matrix, gate, stop (P6.4)"
```

### Task 14: Convergence proof e2e — staged adversary exercises the dynamics

**Files:**
- Create: `tests/test_cegis_convergence.py`

A `StagedAdversaryRival` reveals probes gradually (length-by-length per generation) so A must
fix errors across multiple generations — exercising forgetting, the accumulate pool, the archive,
the gate, and the stable curve. The proof: A converges to L AND the run halts with a real reason
(not generations-exhausted), AND the stable-A curve is monotonic non-decreasing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cegis_convergence.py
from pathlib import Path
import itertools, json
from tyani_tolkai.agents.base import RunResult
from tyani_tolkai.agents.scripted_rival import ScriptedRecognizerRival
from tyani_tolkai.symmetric import SymmetricOrchestrator, ArenaResult
from tyani_tolkai.arena.cegis import CegisReferee, in_L
from tyani_tolkai.sandbox import LocalSandbox
from tyani_tolkai.config import Config

class StagedAdversaryRival:
    """Reveals all {a,b} strings of length == current call index (1,2,3,...), forcing the arms
    race to span several generations."""
    def __init__(self): self.k = 0
    def run(self, brief, workdir, profile, timeout):
        self.k += 1
        probes = ["".join(p) for p in itertools.product("ab", repeat=self.k)]
        body = "\n".join([""] + probes) + "\n"
        (Path(workdir)/"strings.txt").write_text(body, encoding="utf-8")
        return RunResult(status="success", stdout=f"len {self.k}", changed=True)

def _cfg():
    return Config(project="toy", mode="symmetric",
        agents={"rival_a":{"engine":"mock"},"rival_b":{"engine":"mock"}},
        roles={"rival_a":{"goal":"recognize"},"rival_b":{"goal":"fool"}},
        evaluation={"adapter":"numeric","command":"x",
                    "metrics":[{"name":"arena_fitness","dir":"higher","target":1.0,"worst":0.0}]},
        arena={"referee":"cegis-recognizer","generations":8,"per_generation_iterations":2,
               "dominance_tau":0.99,"dominance_rounds":1,"plateau_generations":3})

def test_coevolution_converges_to_L(tmp_path):
    orch = SymmetricOrchestrator(cfg=_cfg(), root=tmp_path, referee=CegisReferee(),
        executor_a=ScriptedRecognizerRival(), executor_b=StagedAdversaryRival(),
        sandbox=LocalSandbox())
    result = orch.run()
    assert result.stop_reason in ("dominance","plateau")
    # delivered best-A == L on a fresh held-out set
    ns = {}; exec((Path(orch.champion_dir("A", result.best_a_id))/"recognizer.py").read_text(), ns)
    held_out = ["".join(p) for n in range(0,5) for p in itertools.product("ab", repeat=n)]
    assert all(ns["accepts"](s) == in_L(s) for s in held_out)
    # stable-A curve is monotonic non-decreasing (no catastrophic forgetting leaked to delivery)
    assert all(b >= a - 1e-9 for a, b in zip(result.stable_a, result.stable_a[1:]))
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_cegis_convergence.py -v` → FAIL until
  the orchestrator handles the multi-generation accumulate/seed/gate correctly.

- [ ] **Step 3: Fix the orchestrator** (not new features — make the dynamics correct): ensure the
  `accumulate` strategy unions ALL of B's probes across generations into A's `.arena/`
  counterexamples; ensure seeding carries the prior A champion so A doesn't relearn from scratch;
  ensure the stable signal is computed against the FIXED held-out set. Iterate until green.

- [ ] **Step 4: Run** → PASS. Then full suite quick check:
  `.venv/bin/python -m pytest tests/test_cegis_convergence.py tests/test_symmetric.py -v`.
- [ ] **Step 5: Commit**

```bash
git add tests/test_cegis_convergence.py src/tyani_tolkai/symmetric.py
git commit -m "test(arena): P6.6 convergence proof — staged co-evolution reaches L and halts (P6.4/P6.6)"
```

### Task 15: Rival-aware brief

**Files:**
- Modify: `src/tyani_tolkai/brief.py` (`build_brief`)
- Test: `tests/test_brief.py` (append)

- [ ] **Step 1: Read** `brief.py:build_brief` signature and structure.
- [ ] **Step 2: Write the failing test**

```python
# tests/test_brief.py  (append)
def test_build_brief_symmetric_points_at_arena(tmp_path, ...):
    # construct a minimal symmetric Config + StateStore as the other build_brief tests do,
    # then assert the returned brief text contains ".arena/" guidance for a rival role.
    brief = build_brief(state, run_id, cfg, ...)
    assert ".arena/" in brief
```

- [ ] **Step 3: Implement** — in `build_brief`, when `cfg.mode == "symmetric"`, append a standing
  block: "Your opponent context is in `.arena/` (read-only): for the recognizer, the accumulated
  labeled counterexamples in `.arena/counterexamples.json`; for the adversary, the opponent's
  frozen recognizer in `.arena/opponent_recognizer.py`. Edit only your artifact
  (`recognizer.py` or `strings.txt`)." Keep it short; do not disturb asymmetric briefs.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_brief.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/brief.py tests/test_brief.py
git commit -m "feat(brief): rival-aware standing block pointing at .arena/ context (P6.4)"
```

---

## Phase P6.5 — CLI + web (mode selection + dual-curve)

### Task 16: CLI — run a symmetric config

**Files:**
- Modify: `src/tyani_tolkai/cli.py`
- Test: `tests/test_cli_symmetric.py`

- [ ] **Step 1: Read** `cli.py` for how the asymmetric run command builds + runs an `Orchestrator`
  (the `_run`/`cmd_run` path), to mirror it for symmetric (dispatch on `cfg.mode`).
- [ ] **Step 2: Write the failing test** — invoke the CLI run command on a symmetric toy config
  with scripted/mock engines and assert it converges + prints the deliverables. Use the same
  CLI-invocation harness as `tests/test_cli_*.py`.

```python
# tests/test_cli_symmetric.py  (sketch — match existing CLI test harness)
def test_cli_run_symmetric_converges(tmp_path, monkeypatch, capsys):
    # write a symmetric config.yaml; map rival engines to scripted rivals via the registry/injection
    # the asymmetric tests already use; run `cmd_run`; assert exit 0 and a "best-A" line in output.
    ...
```

- [ ] **Step 3: Implement** — in the run path, `if cfg.mode == "symmetric":` construct a
  `SymmetricOrchestrator` (referee from `get_referee(cfg.arena.referee)`, executors from the
  registry — `mock`/scripted in tests, claude/codex in prod), call `.run()`, print stop reason +
  `best-A-vs-all-B` / `best-B-vs-all-A`. Keep the asymmetric path unchanged.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_cli_symmetric.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/cli.py tests/test_cli_symmetric.py
git commit -m "feat(cli): run symmetric Co-Evolution Arena configs (P6.5)"
```

### Task 17: Web — mode awareness + dual stable-curve data

**Files:**
- Modify: `src/tyani_tolkai/web/server.py` (+ template/JS as the existing dashboard does)
- Test: `tests/test_web.py` (append)

- [ ] **Step 1: Read** `web/server.py` for the run-launch + progress-polling endpoints and how the
  single-curve dashboard is fed.
- [ ] **Step 2: Write the failing test** — a symmetric run started via the web path reaches a
  terminal state and the progress endpoint returns BOTH stable curves (A and B). Mirror the
  existing `test_web.py` style (TestClient/route asserts).
- [ ] **Step 3: Implement** — dispatch symmetric runs to `SymmetricOrchestrator` in the run
  thread; expose `stable_a` / `stable_b` (the §5 curves) and the deliverables on the run-status
  endpoint; render two curves. Keep asymmetric single-curve untouched. Do NOT plot the live
  adversarial signal as progress.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_web.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add src/tyani_tolkai/web/ tests/test_web.py
git commit -m "feat(web): symmetric mode — dual stable-curve dashboard + deliverables (P6.5)"
```

---

## Final integration

### Task 18: Full suite green + isolated bot-sandbox check

- [ ] **Step 1:** `.venv/bin/python -m pytest -q` — full suite. Expect all green except the known
  `tests/test_bot_sandbox.py` Docker-race flakiness in the FULL suite.
- [ ] **Step 2:** If `test_bot_sandbox.py` fails in the full run, re-run it isolated:
  `.venv/bin/python -m pytest tests/test_bot_sandbox.py -v` → expect 7/7 green (not a regression).
- [ ] **Step 3:** `git add -A && git commit -m "chore(arena): P6 complete — symmetric Co-Evolution Arena, full suite green"`
  (only if uncommitted glue remains).

---

## Self-Review (run after the plan is written; fix inline)

**Spec coverage** — every design section maps to a task:
- §1 Referee → Task 1, 3, 4. §2 ArenaMetricAdapter/arena_fitness → Task 9. §3 archive + strategy +
  promotion gate → Tasks 7, 10, 13. §4 SymmetricOrchestrator + reset recipe → Task 13.
  §4a match matrix → Tasks 7, 11, 13. §5 two signals → Task 13 (`_stable_signal`). §6 stopping →
  Tasks 11, 13. §7 CEGIS toy + oracle/info-flow → Tasks 2, 3, 12, 14. §8 config → Tasks 5, 6.
  §9 state tables → Task 7. §10 sandbox isolation → Tasks 3, 9 (referee runs via sandbox).
  CLI/web → Tasks 16, 17.
- **Convergence proof (P6.6)** → Task 14 (the gate before any real domain).

**Type consistency** — names used across tasks: `MatchOutcome(a_score,b_score,detail)`,
`Referee.play(a_dir,b_dir,sandbox,*,seed)`, `opponent_strategy`, `ArenaMetricAdapter(referee,side,
opponents,*,aggregate,min_floor,penalty,seed).run(...)→MetricResult` with metric name
`arena_fitness` + `data{mean,min,per_opponent,counterexamples}`, `promotion_gate_ok(candidate_
archive_min,prev_champion_archive_min,promote_regression_max)`, `dominance_reached(matrix,a_ids,
b_ids,tau,side)`, `best_vs_all(matrix,a_ids,b_ids,side)`, state `add_champion/champions/record_
match/match_matrix/new_run`, `SymmetricOrchestrator(cfg,root,referee,executor_a,executor_b,
sandbox).run()→ArenaResult(generations,stop_reason,best_a_id,best_b_id,stable_a,stable_b)` +
`champion_dir(side,id)`. Consistent across Tasks 1–17.

**Placeholder scan** — Tasks 8, 16, 17 carry "read first / confirm the existing helper" notes
rather than final code, because they depend on exact existing signatures in `state.py`/`cli.py`/
`web/server.py` not yet read. These are READ-THEN-MIRROR tasks, not vague placeholders: each names
the exact file, the exact behavior to add, and a concrete test. Resolve the signature at Step 1 of
each. Task 13's `run()` is described by responsibilities + helper decomposition rather than full
code because it is the one genuinely large integrator; implement incrementally against its test.

**Env discipline** — every run command uses `.venv/bin/python -m pytest`. No `random`/`Date.now`
in library code (deterministic index selection noted in Task 13).
