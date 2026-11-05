"""Persistent execution registry (SQLite).

* Every state change is a single committed transaction (atomic).
* ``issue_locks`` prevents two executions of the same issue; stale locks of
  dead processes on this host are reclaimed.
* ``RunLock`` (fcntl) prevents two agent processes from running at once.
* Nothing credential-related is ever stored here.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from models import ExecutionStatus, Stage, UnsafeEnvironmentError
from safety import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    command TEXT,
    host TEXT,
    pid INTEGER,
    status TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    run_id TEXT,
    issue_number INTEGER NOT NULL,
    issue_url TEXT,
    issue_title TEXT,
    mode TEXT,
    stage TEXT,
    status TEXT NOT NULL,
    branch TEXT,
    worktree_path TEXT,
    base_branch TEXT,
    base_sha TEXT,
    claude_sessions TEXT DEFAULT '{}',
    analysis TEXT,
    plan TEXT,
    implementation TEXT,
    quality TEXT,
    review TEXT,
    commit_sha TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    closes_issue INTEGER,
    failure_stage TEXT,
    failure_reason TEXT,
    recommended_action TEXT,
    host TEXT,
    pid INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_issue ON executions(issue_number);
CREATE INDEX IF NOT EXISTS idx_exec_status ON executions(status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT,
    ts TEXT NOT NULL,
    stage TEXT,
    message TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS issue_locks (
    issue_number INTEGER PRIMARY KEY,
    execution_id TEXT NOT NULL,
    host TEXT NOT NULL,
    pid INTEGER NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
    base_sha TEXT NOT NULL,
    gate TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (base_sha, gate)
);
"""

JSON_COLUMNS = {"claude_sessions", "analysis", "plan", "implementation", "quality", "review"}
UPDATABLE = {
    "mode", "stage", "status", "branch", "worktree_path", "base_branch", "base_sha", "claude_sessions",
    "analysis", "plan", "implementation", "quality", "review", "commit_sha", "pr_number", "pr_url",
    "closes_issue", "failure_stage", "failure_reason", "recommended_action", "finished_at", "issue_title",
    "pid", "host", "run_id",
}
TERMINAL = {ExecutionStatus.SUCCEEDED.value, ExecutionStatus.FAILED.value, ExecutionStatus.NEEDS_HUMAN.value,
            ExecutionStatus.ABANDONED.value}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """Exclusive, non-blocking process lock: only one agent process at a time."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise UnsafeEnvironmentError(
                f"Another ai-backlog-agent process holds {self.path}; parallel execution is not allowed"
            ) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{socket.gethostname()}:{os.getpid()}\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class ExecutionState:
    def __init__(self, db_path: Path, read_only: bool = False):
        self.db_path = db_path
        self.read_only = read_only
        self.host = socket.gethostname()
        self.pid = os.getpid()
        if read_only:
            if db_path.exists():
                self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None)
            else:
                self.conn = sqlite3.connect(":memory:", isolation_level=None)
                self.conn.executescript(SCHEMA)
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(SCHEMA)
            os.chmod(db_path, 0o600)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise RuntimeError("state opened read-only (dry run)")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ runs

    def start_run(self, command: str) -> str:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(run_id, started_at, command, host, pid, status) VALUES (?,?,?,?,?,?)",
                (run_id, now_iso(), command, self.host, self.pid, "running"),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, summary=? WHERE run_id=?",
                (now_iso(), status, json.dumps(summary, default=str), run_id),
            )

    # ------------------------------------------------------------ executions

    def create_execution(self, run_id: str, issue_number: int, issue_url: str, title: str, mode: str) -> str:
        execution_id = f"exec-{issue_number}-{uuid.uuid4().hex[:10]}"
        ts = now_iso()
        with self.tx() as c:
            c.execute(
                """INSERT INTO executions(execution_id, run_id, issue_number, issue_url, issue_title, mode, stage,
                   status, host, pid, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (execution_id, run_id, issue_number, issue_url, title, mode, None, ExecutionStatus.RUNNING.value,
                 self.host, self.pid, ts, ts),
            )
        return execution_id

    def update(self, execution_id: str, **fields: Any) -> None:
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"Not updatable: {bad}")
        sets, values = [], []
        for key, value in fields.items():
            if isinstance(value, (Stage, ExecutionStatus)):
                value = value.value
            if key in JSON_COLUMNS and value is not None and not isinstance(value, str):
                value = redact(json.dumps(value, default=str))
            elif isinstance(value, str):
                value = redact(value)
            sets.append(f"{key}=?")
            values.append(value)
        sets.append("updated_at=?")
        values.append(now_iso())
        values.append(execution_id)
        with self.tx() as c:
            c.execute(f"UPDATE executions SET {', '.join(sets)} WHERE execution_id=?", values)

    def set_stage(self, execution_id: str, stage: Stage, message: str = "", **fields: Any) -> None:
        self.update(execution_id, stage=stage, **fields)
        self.event(execution_id, stage.value, message or f"stage {stage.value} reached")

    def finish(self, execution_id: str, status: ExecutionStatus, **fields: Any) -> None:
        self.update(execution_id, status=status, finished_at=now_iso(), **fields)
        self.event(execution_id, "finish", f"execution finished: {status.value}", fields)

    def event(self, execution_id: str, stage: str, message: str, data: Any = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(execution_id, ts, stage, message, data) VALUES (?,?,?,?,?)",
                (execution_id, now_iso(), stage, redact(message),
                 redact(json.dumps(data, default=str)) if data is not None else None),
            )

    def get(self, execution_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        return self._row(row) if row else None

    def history(self, issue_number: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM executions WHERE issue_number=? ORDER BY created_at", (issue_number,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def all_executions(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM executions ORDER BY created_at").fetchall()]

    def latest(self, issue_number: int) -> dict[str, Any] | None:
        hist = self.history(issue_number)
        return hist[-1] if hist else None

    def attempts(self, issue_number: int) -> int:
        """Attempts that ended in failure/interruption (claim races, needs_human and successes don't count)."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM executions WHERE issue_number=? AND status IN (?,?)",
            (issue_number, ExecutionStatus.FAILED.value, ExecutionStatus.INTERRUPTED.value),
        ).fetchone()
        return int(row[0])

    def interrupted(self) -> list[dict[str, Any]]:
        """Executions still marked running whose process is gone (or explicitly interrupted)."""
        rows = self.conn.execute(
            "SELECT * FROM executions WHERE status IN (?,?) ORDER BY created_at",
            (ExecutionStatus.RUNNING.value, ExecutionStatus.INTERRUPTED.value),
        ).fetchall()
        result = []
        for row in rows:
            data = self._row(row)
            if data["status"] == ExecutionStatus.INTERRUPTED.value:
                result.append(data)
            elif data["host"] == self.host and not pid_alive(data["pid"]):
                result.append(data)  # process died (crash, kill -9, reboot)
            # RUNNING on another host: cannot tell, leave it alone
        return result

    def mark_interrupted(self, execution_id: str, reason: str) -> None:
        self.update(execution_id, status=ExecutionStatus.INTERRUPTED, failure_reason=reason)
        self.event(execution_id, "interrupt", reason)

    def issue_summary(self) -> dict[int, dict[str, Any]]:
        """Latest meaningful execution per issue (used by the selector). Abandoned claims are ignored."""
        result: dict[int, dict[str, Any]] = {}
        for row in self.all_executions():
            if row["status"] != ExecutionStatus.ABANDONED.value:
                result[row["issue_number"]] = row
        return result

    # ----------------------------------------------------------------- locks

    def acquire_issue_lock(self, issue_number: int, execution_id: str) -> bool:
        with self.tx() as c:
            row = c.execute("SELECT * FROM issue_locks WHERE issue_number=?", (issue_number,)).fetchone()
            if row:
                same_host = row["host"] == self.host
                if same_host and not pid_alive(row["pid"]):
                    c.execute("DELETE FROM issue_locks WHERE issue_number=?", (issue_number,))
                elif row["execution_id"] == execution_id:
                    return True
                else:
                    return False
            c.execute(
                "INSERT INTO issue_locks(issue_number, execution_id, host, pid, acquired_at) VALUES (?,?,?,?,?)",
                (issue_number, execution_id, self.host, self.pid, now_iso()),
            )
            return True

    def transfer_issue_lock(self, issue_number: int, execution_id: str) -> bool:
        """Take over the lock of a dead process during resume."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM issue_locks WHERE issue_number=?", (issue_number,)).fetchone()
            if row and row["host"] == self.host and pid_alive(row["pid"]) and row["pid"] != self.pid:
                return False
            c.execute("DELETE FROM issue_locks WHERE issue_number=?", (issue_number,))
            c.execute(
                "INSERT INTO issue_locks(issue_number, execution_id, host, pid, acquired_at) VALUES (?,?,?,?,?)",
                (issue_number, execution_id, self.host, self.pid, now_iso()),
            )
            return True

    def release_issue_lock(self, issue_number: int, execution_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM issue_locks WHERE issue_number=? AND execution_id=?", (issue_number, execution_id))

    def locked_issues(self) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM issue_locks").fetchall()
        live = {}
        for row in rows:
            if row["host"] != self.host or pid_alive(row["pid"]):
                live[row["issue_number"]] = dict(row)
        return live

    # ------------------------------------------------------------- baselines

    def get_baseline(self, base_sha: str, gate: str) -> Any:
        row = self.conn.execute("SELECT data FROM baselines WHERE base_sha=? AND gate=?", (base_sha, gate)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_baseline(self, base_sha: str, gate: str, data: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO baselines(base_sha, gate, data, created_at) VALUES (?,?,?,?)",
                (base_sha, gate, json.dumps(data), now_iso()),
            )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in JSON_COLUMNS:
            if key in data and data[key]:
                try:
                    data[key] = json.loads(data[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        return data
