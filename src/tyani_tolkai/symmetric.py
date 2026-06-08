"""SymmetricOrchestrator — alternating self-play over the Phase-1 loop (design §4).

The inner Phase-1 loop is only a LOCAL-best generator against a frozen pool; the arena's true
evaluation lives in the champion archive, the match matrix (§4a), and the stable signals. The
pure functions below implement the matrix-derived decisions; the orchestrator class wires them
to the Phase-1 engine.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .metrics.arena import ArenaMetricAdapter
from .orchestrator import Orchestrator
from .state import StateStore

_R_FIXED_SRC = "def accepts(s):\n    return 'a' in s\n"   # fixed reference recognizer for B's stable signal


def promotion_gate_ok(candidate_archive_min, prev_champion_archive_min,
                      promote_regression_max) -> bool:
    """Crown the candidate only if it does not regress against the WHOLE opposing archive by
    more than ``promote_regression_max`` vs the prior champion's archive-wide worst case (§3).
    The first champion (no prior) is always accepted."""
    if prev_champion_archive_min is None:
        return True
    return (prev_champion_archive_min - candidate_archive_min) <= promote_regression_max


def _a_view(matrix, a_id, b_id, side):
    """The score of champion `side` in the (a_id, b_id) pairing, from that side's perspective."""
    s = matrix.get((a_id, b_id))
    if s is None:
        return None
    return s if side == "A" else 1.0 - s


def dominance_reached(matrix, a_ids, b_ids, tau, side) -> bool:
    """Latest champion of `side` beats the ENTIRE opposing archive at >= tau."""
    if side == "A":
        if not a_ids or not b_ids:
            return False
        latest = a_ids[-1]
        scores = [_a_view(matrix, latest, b, "A") for b in b_ids]
    else:
        if not a_ids or not b_ids:
            return False
        latest = b_ids[-1]
        scores = [_a_view(matrix, a, latest, "B") for a in a_ids]
    scores = [s for s in scores if s is not None]
    return bool(scores) and len(scores) == len(b_ids if side == "A" else a_ids) and all(s >= tau for s in scores)


def best_vs_all(matrix, a_ids, b_ids, side):
    """The champion of `side` with the best WORST-CASE score across the whole opposing archive
    (robustness — the deliverable best-A-vs-all-B / best-B-vs-all-A)."""
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


@dataclass
class ArenaResult:
    generations: int
    stop_reason: str
    best_a_id: int | None
    best_b_id: int | None
    stable_a: list
    stable_b: list


class SymmetricOrchestrator:
    """Drives the alternating co-evolution of two sides over the Phase-1 engine (design §4).

    Each side is a normal Phase-1 track (its own artifact git repo + run history). Each
    generation: build the opposing pool, materialize ``.arena/`` context, run a bounded Phase-1
    sub-loop with the ``ArenaMetricAdapter`` as the judge, apply the promotion gate, refresh the
    match matrix, record the stable signals, and check the matrix-based stopping rule.
    """

    def __init__(self, cfg, root, referee, executor_a, executor_b, sandbox):
        self.cfg = cfg
        self.arena = cfg.arena
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.referee = referee
        self.executor_a = executor_a
        self.executor_b = executor_b
        self.sandbox = sandbox

        self.state_a = StateStore(self.root / "side_A")
        self.state_a.git_init()
        self.state_b = StateStore(self.root / "side_B")
        self.state_b.git_init()
        self._ignore_arena(self.state_a)
        self._ignore_arena(self.state_b)
        # the champion + match ledger + parent run live in the PROJECT-level DB (base/state.db),
        # so the CLI/web can read a symmetric run's status + result WITHOUT re-running it, and a
        # resumed run reuses the same parent_run (see _resume_or_create).
        self.ledger = StateStore(self.root)
        self.parent_run = self._resume_or_create()

        self.stable_a: list = []
        self.stable_b: list = []
        self.held_out = self._enumerate(0, 4)            # fixed validation set (NOT shown to agents)
        self.r_fixed = self.root / "_r_fixed"
        self.r_fixed.mkdir(exist_ok=True)
        (self.r_fixed / "recognizer.py").write_text(_R_FIXED_SRC, encoding="utf-8")
        self._cache = self.root / "_champions"
        self._cache.mkdir(exist_ok=True)
        self._dom_streak = 0
        self._best_stable_a = -1.0
        self._plateau = 0
        self._extern_stop = None                         # UI Stop hook (set in run())
        self._active_orch = None                         # current sub-loop (for Force-Stop)
        self._apply_resume()                             # restore curves/counters if resuming

    # ---- small helpers ----

    @staticmethod
    def _enumerate(lo, hi):
        out = []
        for n in range(lo, hi + 1):
            out += ["".join(p) for p in itertools.product("ab", repeat=n)]
        return out

    def _ignore_arena(self, state):
        gi = state.artifact_dir / ".gitignore"
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if ".arena/" not in existing:
            gi.write_text(existing + ".arena/\n_arena_*\n", encoding="utf-8")

    def _state(self, side):
        return self.state_a if side == "A" else self.state_b

    def _executor(self, side):
        return self.executor_a if side == "A" else self.executor_b

    def _side_cfg(self, side):
        rival = "rival_a" if side == "A" else "rival_b"
        return self.cfg.model_copy(update={
            "agents": {**self.cfg.agents, "executor": self.cfg.agents[rival]},
            "roles": {**self.cfg.roles, "executor": self.cfg.roles[rival]},
            "limits": self.cfg.limits.model_copy(update={
                "max_iterations": self.arena.per_generation_iterations,
                "plateau_N": max(2, self.arena.per_generation_iterations),
            }),
        })

    def _latest_champ(self, side):
        champs = self.ledger.champions(self.parent_run, side=side)
        return champs[-1] if champs else None

    def _materialize_champion(self, side, champ_row):
        dest = self._cache / f"{side}_{champ_row['id']}"
        if not dest.exists():
            self._state(side).export_tree(champ_row["git_hash"], dest)
        return dest

    def champion_dir(self, side, champion_id):
        ch = self.ledger.champion(champion_id)
        return self._materialize_champion(side, ch)

    # ---- opponent context + pools ----

    def _all_b_probes(self):
        """Union of every B champion's probe strings (the 'accumulate' growing set, design §3)."""
        probes: list[str] = []
        for ch in self.ledger.champions(self.parent_run, side="B"):
            sp = self._materialize_champion("B", ch) / "strings.txt"
            if sp.exists():
                probes += [ln for ln in sp.read_text(encoding="utf-8").splitlines()]
        return list(dict.fromkeys(probes))               # dedupe, keep order

    def _accumulated_counterexamples(self):
        probes = self._all_b_probes()
        if hasattr(self.referee, "label"):
            return self.referee.label(probes)            # oracle/teacher channel (design §7)
        return [{"s": s} for s in probes]

    def _materialize_context(self, side, workdir, opponent_dirs):
        arena = Path(workdir) / ".arena"
        arena.mkdir(parents=True, exist_ok=True)
        if side == "A":
            (arena / "counterexamples.json").write_text(
                json.dumps(self._accumulated_counterexamples()), encoding="utf-8")
        else:
            rec = ""
            if opponent_dirs:
                rp = Path(opponent_dirs[0]) / "recognizer.py"
                rec = rp.read_text(encoding="utf-8") if rp.exists() else ""
            (arena / "opponent_recognizer.py").write_text(rec, encoding="utf-8")

    def _opponent_pool_dirs(self, side):
        opp = "B" if side == "A" else "A"
        opp_champs = self.ledger.champions(self.parent_run, side=opp)
        if not opp_champs:
            return []
        if self.referee.opponent_strategy == "accumulate" and side == "A":
            union = self._cache / "_b_union"
            union.mkdir(parents=True, exist_ok=True)
            (union / "strings.txt").write_text("\n".join(self._all_b_probes()) + "\n", encoding="utf-8")
            return [union]
        dirs = [self._materialize_champion(opp, opp_champs[-1])]
        for ch in opp_champs[:-1][-self.arena.opponent_pool.k_past:]:
            dirs.append(self._materialize_champion(opp, ch))
        return dirs

    # ---- scoring helpers (all via the referee — never an LLM) ----

    def _live_score(self, side, art_dir, opponent_dir):
        a_dir, b_dir = (art_dir, opponent_dir) if side == "A" else (opponent_dir, art_dir)
        out = self.referee.play(a_dir, b_dir, self.sandbox, seed=0)
        return out.a_score if side == "A" else out.b_score

    def _archive_min(self, side, art_dir):
        """Worst-case live score of ``art_dir`` against the WHOLE opposing archive (gate input)."""
        opp = "B" if side == "A" else "A"
        champs = self.ledger.champions(self.parent_run, side=opp)
        scores = [self._live_score(side, art_dir, self._materialize_champion(opp, ch)) for ch in champs]
        return min(scores) if scores else None

    def _stable_signal(self, side, art_dir):
        if side == "A":
            tb = self.root / "_held_out_b"
            tb.mkdir(exist_ok=True)
            (tb / "strings.txt").write_text("\n".join(self.held_out) + "\n", encoding="utf-8")
            return self.referee.play(art_dir, tb, self.sandbox, seed=0).a_score
        return self.referee.play(self.r_fixed, art_dir, self.sandbox, seed=0).b_score

    # ---- crowning + matrix ----

    def _crown(self, side, generation, git_hash):
        art = self._cache / f"_pending_{side}"
        if art.exists():
            shutil.rmtree(art)
        self._state(side).export_tree(git_hash, art)
        stable = self._stable_signal(side, art)
        repro = {"referee_version": getattr(self.referee, "version", "?"),
                 "aggregate": self.arena.aggregate, "pool_strategy": self.referee.opponent_strategy}
        cid = self.ledger.add_champion(self.parent_run, side, generation, git_hash,
                                       stable_score=stable, repro=repro)
        return cid

    def _update_matrix(self, generation):
        a_champs = self.ledger.champions(self.parent_run, side="A")
        b_champs = self.ledger.champions(self.parent_run, side="B")
        existing = self.ledger.match_matrix(self.parent_run)
        for ac in a_champs:
            ad = self._materialize_champion("A", ac)
            for bc in b_champs:
                if (ac["id"], bc["id"]) in existing:
                    continue
                bd = self._materialize_champion("B", bc)
                out = self.referee.play(ad, bd, self.sandbox, seed=0)
                self.ledger.record_match(self.parent_run, ac["id"], bc["id"], out.a_score, 0)

    def _record_stable_curves(self):
        a = self.ledger.champions(self.parent_run, side="A")
        b = self.ledger.champions(self.parent_run, side="B")
        self.stable_a.append(a[-1]["stable_score"] if a else None)
        self.stable_b.append(b[-1]["stable_score"] if b else None)

    # ---- generation steps ----

    def _bootstrap(self):
        """Seed gen-0 champions for both sides (A first so B can see A's recognizer)."""
        self._materialize_context("A", self.state_a.artifact_dir, opponent_dirs=[])
        self.executor_a.run("", self.state_a.artifact_dir, "writeable", 60)
        self._crown("A", 0, self.state_a.commit("seed champion A gen 0"))

        self._materialize_context("B", self.state_b.artifact_dir, opponent_dirs=[self.state_a.artifact_dir])
        self.executor_b.run("", self.state_b.artifact_dir, "writeable", 60)
        self._crown("B", 0, self.state_b.commit("seed champion B gen 0"))

        self._update_matrix(0)
        self._record_stable_curves()

    def _run_side(self, side, generation):
        state = self._state(side)
        pool = self._opponent_pool_dirs(side)
        if not pool:
            return None
        self._materialize_context(side, state.artifact_dir, pool)
        adapter = ArenaMetricAdapter(self.referee, side, pool, aggregate=self.arena.aggregate,
                                     min_floor=self.arena.min_floor, penalty=self.arena.penalty, seed=0)
        run_id = state.create_run("symmetric")
        orch = Orchestrator(self._side_cfg(side), state, run_id, self._executor(side),
                            adapter, self.sandbox)
        self._active_orch = orch                         # so Force-Stop can reach the live agent
        orch.run_loop(should_stop=self._should_stop)
        return state.head()

    def _should_stop(self) -> bool:
        """Inner sub-loop stop: external (UI Stop) or the global budget cap."""
        return bool(self._extern_stop and self._extern_stop()) or self._budget_exhausted()

    def force_kill(self) -> None:
        """Kill the currently-running sub-loop's agent process group (UI Force-Stop)."""
        if getattr(self, "_active_orch", None) is not None:
            try:
                self._active_orch.force_kill()
            except Exception:
                pass

    def _play_and_crown(self, side, generation):
        head = self._run_side(side, generation)
        if head is None:
            return
        latest = self._latest_champ(side)
        if latest and latest["git_hash"] == head:
            return                                       # no change → keep the existing champion
        cand = self._cache / f"_cand_{side}"
        if cand.exists():
            shutil.rmtree(cand)
        self._state(side).export_tree(head, cand)
        cand_min = self._archive_min(side, cand)
        prev_min = None
        if latest:
            prev_min = self._archive_min(side, self._materialize_champion(side, latest))
        if promotion_gate_ok(cand_min, prev_min, self.arena.promote_regression_max):
            self._crown(side, generation, head)

    def _budget_exhausted(self):
        cap = self.cfg.limits.budget_usd
        if not cap:
            return False
        total = 0.0
        for st in (self.state_a, self.state_b):
            rows = st.conn.execute("SELECT COALESCE(SUM(cost_total),0) AS c FROM run").fetchone()
            total += rows["c"] or 0.0
        return total >= cap

    def _stop(self, generation):
        a = self.ledger.champions(self.parent_run, side="A")
        b = self.ledger.champions(self.parent_run, side="B")
        matrix = self.ledger.match_matrix(self.parent_run)
        a_ids = [c["id"] for c in a]
        b_ids = [c["id"] for c in b]
        dom = (dominance_reached(matrix, a_ids, b_ids, self.arena.dominance_tau, "A")
               or dominance_reached(matrix, a_ids, b_ids, self.arena.dominance_tau, "B"))
        if dom:
            self._dom_streak += 1
            if self._dom_streak >= self.arena.dominance_rounds:
                return "dominance"
        else:
            self._dom_streak = 0
        cur = max([s for s in self.stable_a if s is not None], default=-1.0)
        if cur > self._best_stable_a + 1e-9:
            self._best_stable_a = cur
            self._plateau = 0
        else:
            self._plateau += 1
            if self._plateau >= self.arena.plateau_generations:
                return "plateau"
        return None

    # ---- resume + manifest (project-level persistence) ----

    def _resume_or_create(self) -> int:
        """Reuse the parent run if a manifest exists (resume an unfinished run, or no-op a
        finished one); otherwise start a fresh parent run. Sets _resuming / _finished /
        _completed_gen / _restore so __init__ and run() can act on them."""
        self._resuming = False
        self._finished = False
        self._completed_gen = -1
        self._restore = None
        mpath = self.root / "arena.json"
        if mpath.exists():
            try:
                m = json.loads(mpath.read_text(encoding="utf-8"))
            except ValueError:
                m = None
            if m and m.get("parent_run") is not None:
                self._restore = m
                self._completed_gen = int(m.get("generation", -1))
                if m.get("status") == "finished":
                    self._finished = True
                else:
                    self._resuming = True
                return int(m["parent_run"])
        return self.ledger.create_run("symmetric")

    def _apply_resume(self) -> None:
        """Restore in-memory progress (curves, stop counters) from the manifest on resume."""
        if not self._restore:
            return
        m = self._restore
        self.stable_a = list(m.get("stable_a") or [])
        self.stable_b = list(m.get("stable_b") or [])
        self._dom_streak = int(m.get("dom_streak", 0))
        self._best_stable_a = float(m.get("best_stable_a", -1.0))
        self._plateau = int(m.get("plateau", 0))

    def _save_manifest(self, status: str, stop_reason=None) -> None:
        a = self.ledger.champions(self.parent_run, side="A")
        b = self.ledger.champions(self.parent_run, side="B")
        matrix = self.ledger.match_matrix(self.parent_run)
        a_ids = [c["id"] for c in a]
        b_ids = [c["id"] for c in b]
        payload = {
            "parent_run": self.parent_run, "status": status, "generation": self._completed_gen,
            "generations": self.arena.generations, "stop_reason": stop_reason,
            "referee": self.arena.referee, "stable_a": self.stable_a, "stable_b": self.stable_b,
            "dom_streak": self._dom_streak, "best_stable_a": self._best_stable_a,
            "plateau": self._plateau,
            "best_a_id": best_vs_all(matrix, a_ids, b_ids, "A") if a_ids and b_ids else None,
            "best_b_id": best_vs_all(matrix, a_ids, b_ids, "B") if a_ids and b_ids else None,
            "champions": {
                "A": [{"id": c["id"], "generation": c["generation"], "git_hash": c["git_hash"],
                       "stable_score": c["stable_score"]} for c in a],
                "B": [{"id": c["id"], "generation": c["generation"], "git_hash": c["git_hash"],
                       "stable_score": c["stable_score"]} for c in b],
            },
        }
        tmp = self.root / "arena.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.root / "arena.json")        # atomic

    def _result(self, generation: int, reason: str) -> ArenaResult:
        a_ids = [c["id"] for c in self.ledger.champions(self.parent_run, side="A")]
        b_ids = [c["id"] for c in self.ledger.champions(self.parent_run, side="B")]
        matrix = self.ledger.match_matrix(self.parent_run)
        return ArenaResult(generations=generation, stop_reason=reason,
                           best_a_id=best_vs_all(matrix, a_ids, b_ids, "A"),
                           best_b_id=best_vs_all(matrix, a_ids, b_ids, "B"),
                           stable_a=self.stable_a, stable_b=self.stable_b)

    # ---- public entry point ----

    def run(self, should_stop=None) -> ArenaResult:
        self._extern_stop = should_stop                   # UI Stop wires here
        if self._finished:                                # finished run → idempotent no-op
            m = self._restore or {}
            return self._result(int(m.get("generation", self._completed_gen)),
                                 m.get("stop_reason") or "max_generations")
        if not self._resuming:
            self._bootstrap()
            self._completed_gen = 0
            self._save_manifest(status="running")
        reason = "max_generations"
        last_gen = self._completed_gen
        for generation in range(self._completed_gen + 1, self.arena.generations + 1):
            if self._extern_stop and self._extern_stop():     # UI Stop between generations
                reason = "stopped"
                break
            self._play_and_crown("A", generation)
            self._play_and_crown("B", generation)
            self._update_matrix(generation)
            self._record_stable_curves()
            self._completed_gen = generation
            last_gen = generation
            stop = self._stop(generation)
            self._save_manifest(status="running")
            if stop:
                reason = stop
                break
            if self._extern_stop and self._extern_stop():
                reason = "stopped"
                break
            if self._budget_exhausted():
                reason = "budget"
                break
        status = "stopped" if reason == "stopped" else "finished"
        self.ledger.set_status(self.parent_run, status)
        self._save_manifest(status=status, stop_reason=reason)
        return self._result(last_gen, reason)

