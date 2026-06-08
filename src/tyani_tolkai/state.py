"""Durable state: SQLite for run/iteration history + git for artifact versions.

This is the source of truth for resume (spec §9, §11). The orchestrator only ever
"keeps" a candidate *after* it has been scored, so a crash leaves an uncommitted
working tree that is safely discarded — state stays consistent at iteration
boundaries. ``reconcile`` repairs a row that has no matching git commit.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    best_score REAL,
    plateau_count INTEGER NOT NULL DEFAULT 0,
    no_op_count INTEGER NOT NULL DEFAULT 0,
    iter_count INTEGER NOT NULL DEFAULT 0,
    cost_total REAL NOT NULL DEFAULT 0,
    baseline_json TEXT,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iteration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    n INTEGER NOT NULL,
    git_hash TEXT,
    score REAL,
    verdict TEXT,
    change_summary TEXT,
    feedback TEXT,
    cost REAL DEFAULT 0,
    duration REAL DEFAULT 0,
    agent_exit TEXT,
    ts TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES run(id)
);
CREATE TABLE IF NOT EXISTS metric (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    dir TEXT NOT NULL,
    weight REAL NOT NULL,
    FOREIGN KEY (iteration_id) REFERENCES iteration(id)
);
CREATE TABLE IF NOT EXISTS command (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    text TEXT NOT NULL,
    applied_at_iter INTEGER
);
CREATE TABLE IF NOT EXISTS checkpoint (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    iter INTEGER,
    reason TEXT,
    decision TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS champion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    side TEXT NOT NULL,             -- 'A' | 'B'
    generation INTEGER NOT NULL,
    git_hash TEXT NOT NULL,         -- champion commit in that side's artifact repo
    stable_score REAL,             -- §5 stable signal at snapshot time
    repro_json TEXT,               -- pool_hash, referee_version, aggregate, seeds (reproducible scoring)
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    a_champion_id INTEGER NOT NULL,
    b_champion_id INTEGER NOT NULL,
    a_score REAL NOT NULL,         -- referee a_score for this exact pairing
    seed INTEGER NOT NULL,
    ts TEXT NOT NULL,
    UNIQUE (a_champion_id, b_champion_id, seed)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IterationRow:
    n: int
    git_hash: str | None
    score: float | None
    verdict: str | None
    change_summary: str | None
    metrics: list[dict]
    feedback: str | None = None
    ts: str | None = None


class StateStore:
    """SQLite + git state for one project directory."""

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.artifact_dir = self.project_dir / "artifact"
        self.db_path = self.project_dir / "state.db"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")   # concurrent web read + run write
        self.conn.executescript(SCHEMA)
        for _mig in ("ALTER TABLE iteration ADD COLUMN feedback TEXT",
                     "ALTER TABLE run ADD COLUMN baseline_json TEXT",
                     "ALTER TABLE run ADD COLUMN metrics_sig TEXT"):
            try:                                        # migrate older DBs in place
                self.conn.execute(_mig)
            except sqlite3.OperationalError:
                pass                                    # column already exists
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- git helpers (operate inside artifact_dir) ----

    def _git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.artifact_dir), *args],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout.strip()

    def git_init(self) -> str:
        """Init the artifact repo and make an initial commit (allow empty)."""
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if not (self.artifact_dir / ".git").exists():
            self._git("init", "-q")
            # local identity so commits work even without global git config
            self._git("config", "user.email", "orchestrator@tyani-tolkai.local")
            self._git("config", "user.name", "tyani-tolkai")
            # byte-exact artifacts across platforms: never let git rewrite line endings
            # (Windows global autocrlf=true would otherwise turn LF into CRLF on commit)
            self._git("config", "core.autocrlf", "false")
            # keep transient bytecode out of artifact versions
            gi = self.artifact_dir / ".gitignore"
            if not gi.exists():
                gi.write_text("__pycache__/\n*.pyc\n")
        self._git("add", "-A")
        # allow empty so an empty seed still yields a baseline commit
        self._git("commit", "-q", "--allow-empty", "-m", "seed")
        return self.head()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def has_changes(self) -> bool:
        """True if the working tree has uncommitted changes (the agent edited files)."""
        return bool(self._git("status", "--porcelain").strip())

    def tracked_files(self) -> list[str]:
        """Committed files, excluding the housekeeping .gitignore."""
        return [f for f in self._git("ls-files").splitlines() if f and f != ".gitignore"]

    def is_artifact_empty(self) -> bool:
        """True when the artifact has no real content yet (only seed/.gitignore)."""
        return not self.tracked_files()

    def commit(self, msg: str) -> str:
        """Stage everything and commit the candidate; return new HEAD hash."""
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", msg)
        return self.head()

    def revert_uncommitted(self) -> None:
        """Discard the uncommitted candidate (restore last committed state)."""
        self._git("reset", "-q", "--hard", "HEAD")
        self._git("clean", "-fdq")

    def reset_hard(self, ref: str) -> None:
        """Hard-reset the working tree and HEAD to a ref (drops later commits)."""
        self._git("reset", "-q", "--hard", ref)
        self._git("clean", "-fdq")

    def diff(self, ref_a: str, ref_b: str = "HEAD") -> str:
        return self._git("diff", ref_a, ref_b)

    def diff_uncommitted(self) -> str:
        """Diff of the working tree vs HEAD (the current candidate)."""
        return self._git("add", "-A") or self._git("diff", "--cached", "HEAD")

    def commit_exists(self, git_hash: str) -> bool:
        try:
            self._git("cat-file", "-e", f"{git_hash}^{{commit}}")
            return True
        except RuntimeError:
            return False

    # ---- run / iteration records ----

    def create_run(self, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO run (status, mode, created_ts) VALUES (?, ?, ?)",
            ("running", mode, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ---- champion archive + match matrix (symmetric arena, design §3/§4a) ----

    def add_champion(self, run_id: int, side: str, generation: int, git_hash: str,
                     stable_score: float | None = None, repro: dict | None = None) -> int:
        import json
        cur = self.conn.execute(
            "INSERT INTO champion (run_id, side, generation, git_hash, stable_score, repro_json, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, side, generation, git_hash, stable_score,
             json.dumps(repro) if repro else None, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def champions(self, run_id: int, side: str | None = None) -> list[dict]:
        q = "SELECT * FROM champion WHERE run_id=?"
        args: list = [run_id]
        if side:
            q += " AND side=?"
            args.append(side)
        q += " ORDER BY generation, id"
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    def champion(self, champion_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM champion WHERE id=?", (champion_id,)).fetchone()
        return dict(r) if r else None

    def record_match(self, run_id: int, a_champion_id: int, b_champion_id: int,
                     a_score: float, seed: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO match (run_id, a_champion_id, b_champion_id, a_score, seed, ts) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, a_champion_id, b_champion_id, a_score, seed, _now()),
        )
        self.conn.commit()

    def match_matrix(self, run_id: int) -> dict:
        """(a_champion_id, b_champion_id) → a_score, the persisted §4a matrix."""
        rows = self.conn.execute(
            "SELECT a_champion_id, b_champion_id, a_score FROM match WHERE run_id=?",
            (run_id,)).fetchall()
        return {(r["a_champion_id"], r["b_champion_id"]): r["a_score"] for r in rows}

    def drop_generations_after(self, run_id: int, generation: int) -> int:
        """Delete champions (and their matches) with generation > N. Used on RESUME to discard a
        partially-played generation (e.g. crash after A was crowned but before B) so re-running it
        cannot create duplicate champions for the same generation. Returns how many were removed."""
        cur = self.conn.cursor()
        try:
            ids = [r["id"] for r in cur.execute(
                "SELECT id FROM champion WHERE run_id=? AND generation>?", (run_id, generation)).fetchall()]
            if ids:
                qs = ",".join("?" * len(ids))
                cur.execute(
                    f"DELETE FROM match WHERE run_id=? AND (a_champion_id IN ({qs}) OR b_champion_id IN ({qs}))",
                    (run_id, *ids, *ids))
                cur.execute("DELETE FROM champion WHERE run_id=? AND generation>?", (run_id, generation))
            self.conn.commit()
            return len(ids)
        finally:
            cur.close()

    def export_tree(self, git_hash: str, dest_dir: str | Path) -> Path:
        """Materialize a champion commit's file tree into ``dest_dir`` (created if missing).

        Uses ``git archive`` piped to ``tar`` so the live artifact repo is never touched.
        """
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "-C", str(self.artifact_dir), "archive", git_hash],
            capture_output=True,
        )
        if archive.returncode != 0:
            raise RuntimeError(f"git archive {git_hash} failed: {archive.stderr.decode(errors='replace')}")
        extract = subprocess.run(
            ["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True,
        )
        if extract.returncode != 0:
            raise RuntimeError(f"tar extract failed: {extract.stderr.decode(errors='replace')}")
        return dest

    def record_iteration(
        self,
        run_id: int,
        n: int,
        git_hash: str | None,
        score: float | None,
        verdict: str | None,
        metrics: list[dict],
        change_summary: str | None = None,
        cost: float = 0.0,
        duration: float = 0.0,
        agent_exit: str | None = None,
        feedback: str | None = None,
    ) -> int:
        """Write one iteration + its metric rows in a single transaction."""
        with self.conn:  # transaction
            cur = self.conn.execute(
                "INSERT INTO iteration "
                "(run_id, n, git_hash, score, verdict, change_summary, feedback, cost, duration, agent_exit, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, n, git_hash, score, verdict, change_summary, feedback, cost, duration, agent_exit, _now()),
            )
            iid = int(cur.lastrowid)
            for m in metrics:
                self.conn.execute(
                    "INSERT INTO metric (iteration_id, name, value, dir, weight) VALUES (?,?,?,?,?)",
                    (iid, m["name"], m["value"], m.get("dir", "higher"), m.get("weight", 1.0)),
                )
        return iid

    def update_run(self, run_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE run SET {cols} WHERE id = ?", (*fields.values(), run_id))
        self.conn.commit()

    def update_iteration_score(self, run_id: int, n: int, score: float | None) -> None:
        """Overwrite a stored iteration's composite score — used when the objective changes and
        the whole history is re-scored onto the new metric scale (score=None = not comparable)."""
        self.conn.execute("UPDATE iteration SET score = ? WHERE run_id = ? AND n = ?",
                          (score, run_id, n))
        self.conn.commit()

    def get_run(self, run_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()

    def latest_run_id(self) -> int | None:
        """The most recent run that has iteration history (fallback: the most recent run)."""
        r = self.conn.execute(
            "SELECT id FROM run WHERE id IN (SELECT DISTINCT run_id FROM iteration) "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if r is None:
            r = self.conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()
        return int(r["id"]) if r else None

    def iteration_hash(self, run_id: int, n: int) -> str | None:
        """The git commit of keep-iteration n (None if n isn't a restorable kept iteration)."""
        r = self.conn.execute(
            "SELECT git_hash FROM iteration WHERE run_id=? AND n=? AND verdict='keep' "
            "ORDER BY id DESC LIMIT 1", (run_id, n)).fetchone()
        return r["git_hash"] if r and r["git_hash"] else None

    def rewind_to(self, run_id: int, n: int) -> str:
        """Roll the artifact AND run state back to keep-iteration n, so the next Run continues
        from there. Recoverable: the current HEAD is tagged before the reset, so the dropped
        tail stays reachable in git. Returns the commit hash rewound to."""
        h = self.iteration_hash(run_id, n)
        if not h:
            raise ValueError(f"iteration {n} is not a restorable (kept) iteration")
        row = self.conn.execute(
            "SELECT score FROM iteration WHERE run_id=? AND n=? ORDER BY id DESC LIMIT 1",
            (run_id, n)).fetchone()
        score_n = row["score"] if row else None
        try:
            self._git("tag", "-f", f"backup/pre-rewind-{n}", "HEAD")   # keep the tail reachable
        except RuntimeError:
            pass
        self.reset_hard(h)
        cur = self.conn.cursor()                # explicit cursor (PyPy-safe)
        try:
            cur.execute("DELETE FROM metric WHERE iteration_id IN "
                        "(SELECT id FROM iteration WHERE run_id=? AND n>?)", (run_id, n))
            cur.execute("DELETE FROM iteration WHERE run_id=? AND n>?", (run_id, n))
            self.conn.commit()
        finally:
            cur.close()
        self.update_run(run_id, best_score=score_n, iter_count=n,
                        plateau_count=0, no_op_count=0, status="idle")
        return h

    # ---- human checkpoints ----
    def add_checkpoint(self, run_id: int, iteration: int, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO checkpoint (run_id, iter, reason, ts) VALUES (?, ?, ?, ?)",
            (run_id, iteration, reason, _now()))
        self.conn.commit()

    def open_checkpoint(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM checkpoint WHERE run_id = ? AND decision IS NULL ORDER BY id DESC LIMIT 1",
            (run_id,)).fetchone()

    def resolve_checkpoint(self, run_id: int, decision: str) -> None:
        self.conn.execute(
            "UPDATE checkpoint SET decision = ? WHERE run_id = ? AND decision IS NULL",
            (decision, run_id))
        self.conn.commit()

    def set_status(self, run_id: int, status: str) -> None:
        self.update_run(run_id, status=status)

    def best_score(self, run_id: int) -> float | None:
        row = self.conn.execute("SELECT best_score FROM run WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else row["best_score"]

    def last_iterations(self, run_id: int, k: int) -> list[IterationRow]:
        """Return the newest ``k`` iterations (oldest-first), each with its metrics."""
        rows = self.conn.execute(
            "SELECT * FROM iteration WHERE run_id = ? ORDER BY n DESC LIMIT ?",
            (run_id, k),
        ).fetchall()
        result: list[IterationRow] = []
        for r in reversed(rows):
            metrics = self.conn.execute(
                "SELECT name, value, dir, weight FROM metric WHERE iteration_id = ?",
                (r["id"],),
            ).fetchall()
            result.append(
                IterationRow(
                    n=r["n"],
                    git_hash=r["git_hash"],
                    score=r["score"],
                    verdict=r["verdict"],
                    change_summary=r["change_summary"],
                    metrics=[dict(m) for m in metrics],
                    feedback=r["feedback"] if "feedback" in r.keys() else None,
                    ts=r["ts"] if "ts" in r.keys() else None,
                )
            )
        return result

    def reconcile(self, run_id: int) -> int:
        """Crash-only repair: drop kept-iteration rows whose git commit is missing.

        Returns the number of rows removed.
        """
        removed = 0
        rows = self.conn.execute(
            "SELECT id, git_hash, verdict FROM iteration WHERE run_id = ? ORDER BY n DESC",
            (run_id,),
        ).fetchall()
        for r in rows:
            if r["verdict"] == "keep" and r["git_hash"] and not self.commit_exists(r["git_hash"]):
                self.conn.execute("DELETE FROM metric WHERE iteration_id = ?", (r["id"],))
                self.conn.execute("DELETE FROM iteration WHERE id = ?", (r["id"],))
                removed += 1
        if removed:
            # recompute run aggregates so best_score/counters reflect surviving rows
            best = self.conn.execute(
                "SELECT MAX(score) AS b FROM iteration WHERE run_id=? AND verdict='keep'",
                (run_id,)).fetchone()["b"]
            maxn = self.conn.execute(
                "SELECT MAX(n) AS m FROM iteration WHERE run_id=?", (run_id,)).fetchone()["m"] or 0
            noop = self.conn.execute(
                "SELECT COUNT(*) AS c FROM iteration WHERE run_id=? AND verdict='no_op'",
                (run_id,)).fetchone()["c"]
            self.conn.execute(
                "UPDATE run SET best_score=?, iter_count=?, plateau_count=0, no_op_count=? WHERE id=?",
                (best, maxn, noop, run_id))
        self.conn.commit()
        return removed
