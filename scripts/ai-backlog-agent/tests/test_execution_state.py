import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from execution_state import ExecutionState, RunLock
from models import ExecutionStatus, Stage, UnsafeEnvironmentError


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "agent.db"
        self.state = ExecutionState(self.db)
        self.run_id = self.state.start_run("main.py")

    def tearDown(self):
        self.state.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_execution_lifecycle_is_persisted(self):
        eid = self.state.create_execution(self.run_id, 7, "https://x/7", "Fix OTP", "implement")
        self.state.set_stage(eid, Stage.WORKSPACE_READY, branch="ai/fix-issue-7-otp", worktree_path="/w/7", base_sha="abc")
        self.state.update(eid, analysis={"summary": "s"}, claude_sessions={"analyze": "sess"})
        self.state.finish(eid, ExecutionStatus.SUCCEEDED, pr_url="https://x/pull/1", pr_number=1)
        reopened = ExecutionState(self.db)
        record = reopened.get(eid)
        self.assertEqual(record["stage"], "workspace_ready")
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["analysis"], {"summary": "s"})
        self.assertEqual(record["claude_sessions"], {"analyze": "sess"})
        self.assertEqual(record["pr_url"], "https://x/pull/1")
        events = reopened.conn.execute("SELECT stage FROM events WHERE execution_id=?", (eid,)).fetchall()
        self.assertIn("workspace_ready", [e[0] for e in events])
        reopened.close()

    def test_updates_are_atomic(self):
        eid = self.state.create_execution(self.run_id, 7, "u", "t", "implement")
        with self.assertRaises(sqlite3.Error):
            with self.state.tx() as c:
                c.execute("UPDATE executions SET stage='committed' WHERE execution_id=?", (eid,))
                c.execute("INSERT INTO nonexistent VALUES (1)")
        self.assertIsNone(self.state.get(eid)["stage"])

    def test_secrets_are_redacted_before_storage(self):
        eid = self.state.create_execution(self.run_id, 7, "u", "t", "implement")
        self.state.update(eid, failure_reason="token ghp_" + "a" * 36 + " leaked")
        self.assertNotIn("ghp_", self.state.get(eid)["failure_reason"])

    def test_unknown_columns_rejected(self):
        eid = self.state.create_execution(self.run_id, 7, "u", "t", "implement")
        with self.assertRaises(ValueError):
            self.state.update(eid, **{"status; DROP TABLE runs": "x"})

    def test_issue_lock_prevents_concurrent_execution(self):
        self.assertTrue(self.state.acquire_issue_lock(5, "e1"))
        self.assertTrue(self.state.acquire_issue_lock(5, "e1"))  # re-entrant for the same execution
        self.assertFalse(self.state.acquire_issue_lock(5, "e2"))
        self.assertIn(5, self.state.locked_issues())
        self.state.release_issue_lock(5, "e1")
        self.assertTrue(self.state.acquire_issue_lock(5, "e2"))

    def test_stale_lock_of_dead_process_is_reclaimed(self):
        with self.state.tx() as c:
            c.execute("INSERT INTO issue_locks VALUES (5, 'old', ?, 999999, 'x')", (self.state.host,))
        self.assertNotIn(5, self.state.locked_issues())
        self.assertTrue(self.state.acquire_issue_lock(5, "new"))

    def test_lock_held_on_other_host_is_respected(self):
        with self.state.tx() as c:
            c.execute("INSERT INTO issue_locks VALUES (5, 'remote', 'other-host', 1, 'x')")
        self.assertFalse(self.state.acquire_issue_lock(5, "mine"))

    def test_interrupted_detection(self):
        eid = self.state.create_execution(self.run_id, 3, "u", "t", "implement")
        self.state.update(eid, pid=999999)  # dead process
        live = self.state.create_execution(self.run_id, 4, "u", "t", "implement")  # our own live pid
        found = [r["execution_id"] for r in self.state.interrupted()]
        self.assertIn(eid, found)
        self.assertNotIn(live, found)
        self.state.mark_interrupted(live, "ctrl-c")
        self.assertIn(live, [r["execution_id"] for r in self.state.interrupted()])

    def test_attempt_counting_and_summary(self):
        for status in (ExecutionStatus.FAILED, ExecutionStatus.ABANDONED, ExecutionStatus.FAILED):
            eid = self.state.create_execution(self.run_id, 9, "u", "t", "implement")
            self.state.finish(eid, status)
        self.assertEqual(self.state.attempts(9), 2)  # abandoned claims don't count
        self.assertEqual(self.state.issue_summary()[9]["status"], "failed")

    def test_abandoned_is_ignored_by_summary(self):
        eid = self.state.create_execution(self.run_id, 10, "u", "t", "implement")
        self.state.finish(eid, ExecutionStatus.ABANDONED)
        self.assertNotIn(10, self.state.issue_summary())

    def test_baselines_cached_per_sha(self):
        self.assertIsNone(self.state.get_baseline("abc", "all"))
        self.state.save_baseline("abc", "all", {"phpunit": {"ran": True}})
        self.assertEqual(self.state.get_baseline("abc", "all"), {"phpunit": {"ran": True}})

    def test_old_database_is_migrated(self):
        old = self.tmp / "old.db"
        import sqlite3 as sq
        conn = sq.connect(old)
        conn.executescript("CREATE TABLE executions (execution_id TEXT PRIMARY KEY, run_id TEXT, issue_number INTEGER NOT NULL,"
                           " status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);")
        conn.close()
        migrated = ExecutionState(old)
        cols = {r[1] for r in migrated.conn.execute("PRAGMA table_info(executions)")}
        self.assertIn("human_labeled", cols)
        migrated.close()

    def test_db_file_permissions_are_private(self):
        self.assertEqual(os.stat(self.db).st_mode & 0o777, 0o600)

    def test_read_only_mode_never_creates_files(self):
        missing = self.tmp / "nope" / "agent.db"
        ro = ExecutionState(missing, read_only=True)
        self.assertEqual(ro.issue_summary(), {})
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(RuntimeError):
            ro.start_run("x")


class RunLockTest(unittest.TestCase):
    def test_second_process_lock_fails(self):
        tmp = Path(tempfile.mkdtemp())
        first, second = RunLock(tmp / "lock"), RunLock(tmp / "lock")
        first.acquire()
        with self.assertRaises(UnsafeEnvironmentError):
            second.acquire()
        first.release()
        second.acquire()
        second.release()
        shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
