"""Sequential engine behaviour: unlimited / limit / once / issue modes, failure continuation,
systemic stop, no infinite loops, fresh project data before every selection."""

import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import make_config, make_issue, quiet_logger

from engine import Engine, RunOptions
from execution_state import ExecutionState
from issue_selector import IssueSelector
from models import AuthError, ExecutionStatus, IssueOutcome, PullRef, UsageLimitError
from report import build_summary, render_summary


class FakeProject:
    def __init__(self, issues):
        self.issues = {i.number: i for i in issues}
        self.fetches = 0

    def fetch_issues(self):
        self.fetches += 1
        return list(self.issues.values())


class FakeProcessor:
    """Simulates real side effects: success -> issue moves to In review with an open PR."""

    def __init__(self, project, state, run_id, fail=(), systemic=None, human=()):
        self.project = project
        self.state = state
        self.run_id = run_id
        self.fail = set(fail)
        self.human = set(human)
        self.systemic = systemic
        self.order = []
        self.resumed = []

    def process(self, elig):
        n = elig.issue.number
        self.order.append(n)
        if self.systemic == n:
            raise AuthError("token expired")
        eid = self.state.create_execution(self.run_id, n, "u", elig.issue.title, elig.mode.value)
        if n in self.fail:
            self.state.finish(eid, ExecutionStatus.FAILED, failure_stage="quality_gates", failure_reason="tests")
            return IssueOutcome(n, elig.issue.title, ExecutionStatus.FAILED, eid, failure_stage="quality_gates",
                                failure_reason="tests", recommended_action="fix")
        if n in self.human:
            self.state.finish(eid, ExecutionStatus.NEEDS_HUMAN, failure_reason="rotation")
            return IssueOutcome(n, elig.issue.title, ExecutionStatus.NEEDS_HUMAN, eid)
        url = f"https://x/pull/{100 + n}"
        self.state.finish(eid, ExecutionStatus.SUCCEEDED, pr_url=url, pr_number=100 + n)
        issue = self.project.issues[n]
        issue.status = "In review"
        issue.linked_prs.append(PullRef(100 + n, url, "OPEN", True, f"ai/fix-issue-{n}-x"))
        return IssueOutcome(n, elig.issue.title, ExecutionStatus.SUCCEEDED, eid, pr_url=url, pr_number=100 + n,
                            quality_summary="all passed")

    def resume(self, record):
        self.resumed.append(record["issue_number"])
        self.state.finish(record["execution_id"], ExecutionStatus.SUCCEEDED, pr_url="https://x/pull/999")
        return IssueOutcome(record["issue_number"], "", ExecutionStatus.SUCCEEDED, record["execution_id"], pr_url="https://x/pull/999")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.state = ExecutionState(self.tmp / "agent.db")
        self.run_id = self.state.start_run("test")

    def tearDown(self):
        self.state.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, issues, **proc_kw):
        project = FakeProject(issues)
        processor = FakeProcessor(project, self.state, self.run_id, **proc_kw)
        engine = Engine(config=self.config, project=project, selector=IssueSelector(self.config), processor=processor,
                        state=self.state, logger=quiet_logger(), repository="acme/app")
        return engine, processor, project

    def backlog(self, n=6):
        prios = ["P3", "P0", "P2", "P1", "P2", "P0"]
        return [make_issue(i + 1, prios[i % len(prios)]) for i in range(n)]

    def test_default_processes_entire_backlog_in_priority_order(self):
        engine, proc, project = self.engine(self.backlog(6))
        result = engine.run(RunOptions())
        self.assertEqual(proc.order, [2, 6, 4, 3, 5, 1])
        self.assertEqual(result.stopped_reason, "no eligible issues remain")
        self.assertEqual(sum(1 for e in result.final_evaluations if e.eligible), 0)
        # fresh project data before every selection (+1 final verification)
        self.assertEqual(project.fetches, 6 + 1 + 1)

    def test_no_arbitrary_limit_in_default_mode(self):
        issues = [make_issue(i, "P2") for i in range(1, 41)]
        engine, proc, _ = self.engine(issues)
        engine.run(RunOptions())
        self.assertEqual(len(proc.order), 40)

    def test_limit_counts_attempts_including_failures(self):
        engine, proc, _ = self.engine(self.backlog(6), fail={2})
        result = engine.run(RunOptions(limit=3))
        self.assertEqual(proc.order, [2, 6, 4])
        self.assertIn("limit of 3", result.stopped_reason)

    def test_once_processes_exactly_one(self):
        engine, proc, _ = self.engine(self.backlog(6))
        engine.run(RunOptions(limit=1))
        self.assertEqual(proc.order, [2])

    def test_isolated_failure_continues_and_is_not_retried_in_same_run(self):
        engine, proc, _ = self.engine(self.backlog(4), fail={2})
        result = engine.run(RunOptions())
        self.assertEqual(proc.order, [2, 4, 3, 1])
        self.assertEqual(proc.order.count(2), 1)
        summary = build_summary(result, self.run_id)
        self.assertEqual(summary["totals"]["failed"], 1)
        self.assertEqual(summary["failures"][0]["issue"], 2)

    def test_stop_after_failure_when_configured(self):
        self.config.execution.continue_after_isolated_failure = False
        engine, proc, _ = self.engine(self.backlog(4), fail={2})
        engine.run(RunOptions())
        self.assertEqual(proc.order, [2])

    def test_systemic_failure_stops_batch(self):
        engine, proc, _ = self.engine(self.backlog(4), systemic=4)
        result = engine.run(RunOptions())
        self.assertEqual(proc.order, [2, 4])
        self.assertIn("AuthError", result.systemic_error)

    def test_usage_limit_is_systemic(self):
        engine, proc, _ = self.engine(self.backlog(2))
        proc.process = lambda elig: (_ for _ in ()).throw(UsageLimitError("limit"))
        result = engine.run(RunOptions())
        self.assertIn("UsageLimitError", result.systemic_error)

    def test_failed_issue_from_previous_run_excluded_and_retry_flag(self):
        eid = self.state.create_execution(self.run_id, 2, "u", "t", "implement")
        self.state.finish(eid, ExecutionStatus.FAILED, failure_stage="review", failure_reason="x")
        engine, proc, _ = self.engine(self.backlog(3))
        engine.run(RunOptions())
        self.assertNotIn(2, proc.order)
        engine, proc, _ = self.engine(self.backlog(3))
        engine.run(RunOptions(retry_failed=True))
        self.assertEqual(proc.order, [2])  # only previously failed issues

    def test_retry_limit_respected(self):
        for _ in range(2):
            eid = self.state.create_execution(self.run_id, 2, "u", "t", "implement")
            self.state.finish(eid, ExecutionStatus.FAILED)
        engine, proc, _ = self.engine(self.backlog(3))
        engine.run(RunOptions(retry_failed=True))
        self.assertEqual(proc.order, [])

    def test_specific_issue_respects_eligibility(self):
        issues = self.backlog(3) + [make_issue(9, "P1", blocked=[(1, "OPEN")])]
        engine, proc, _ = self.engine(issues)
        result = engine.run(RunOptions(issue=9))
        self.assertEqual(proc.order, [])
        self.assertIn("blocked", result.stopped_reason)
        engine, proc, _ = self.engine(issues)
        engine.run(RunOptions(issue=3))
        self.assertEqual(proc.order, [3])

    def test_issues_with_existing_pr_or_wrong_status_are_skipped(self):
        issues = [make_issue(1, "P0", prs=[PullRef(5, "u", "OPEN")]), make_issue(2, "P0", status="In progress"), make_issue(3, "P3")]
        engine, proc, _ = self.engine(issues)
        engine.run(RunOptions())
        self.assertEqual(proc.order, [3])

    def test_resume_handles_interrupted_first(self):
        eid = self.state.create_execution(self.run_id, 5, "u", "t", "implement")
        self.state.mark_interrupted(eid, "ctrl-c")
        engine, proc, _ = self.engine(self.backlog(2))
        engine.run(RunOptions(resume=True))
        self.assertEqual(proc.resumed, [5])
        self.assertEqual(proc.order, [2, 1])

    def test_keyboard_interrupt_stops_gracefully(self):
        engine, proc, _ = self.engine(self.backlog(3))

        def interrupt(elig):
            raise KeyboardInterrupt

        proc.process = interrupt
        result = engine.run(RunOptions())
        self.assertTrue(result.interrupted)

    def test_report_contents(self):
        engine, _, _ = self.engine(self.backlog(3) + [make_issue(8, "P1", blocked=[(1, "OPEN")])])
        result = engine.run(RunOptions())
        summary = build_summary(result, self.run_id)
        self.assertEqual(summary["totals"]["issues_discovered"], 4)
        self.assertEqual(summary["totals"]["draft_prs_created"], 3)
        self.assertEqual(summary["totals"]["with_existing_prs"], 3)
        # PR creation never closes issues, so #8 stays blocked by the still-open #1
        self.assertEqual(summary["totals"]["blocked_by_dependencies"], 1)
        self.assertEqual(summary["totals"]["remaining_eligible"], 0)
        text = render_summary(summary)
        for heading in ("## Execution Summary", "## Pull Requests", "## Failed Issues", "## Backlog Status"):
            self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()
