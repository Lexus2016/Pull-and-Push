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


class StateStore:
    """SQLite + git state for one project directory."""

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.artifact_dir = self.project_dir / "artifact"
        self.db_path = self.project_dir / "state.db"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
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
        self._git("add", "-A")
        # allow empty so an empty seed still yields a baseline commit
        self._git("commit", "-q", "--allow-empty", "-m", "seed")
        return self.head()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def commit(self, msg: str) -> str:
        """Stage everything and commit the candidate; return new HEAD hash."""
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", msg)
        return self.head()

    def revert_uncommitted(self) -> None:
        """Discard the uncommitted candidate (restore last committed state)."""
        self._git("reset", "-q", "--hard", "HEAD")
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
    ) -> int:
        """Write one iteration + its metric rows in a single transaction."""
        with self.conn:  # transaction
            cur = self.conn.execute(
                "INSERT INTO iteration "
                "(run_id, n, git_hash, score, verdict, change_summary, cost, duration, agent_exit, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, n, git_hash, score, verdict, change_summary, cost, duration, agent_exit, _now()),
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

    def get_run(self, run_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()

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
        self.conn.commit()
        return removed
