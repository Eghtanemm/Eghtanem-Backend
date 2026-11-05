import unittest

from helpers import make_config, make_issue

from issue_selector import IssueSelector, SelectionContext, acceptance_criteria_count
from models import ExecutionStatus, ProcessingMode, PullRef, SkipCategory


def ctx(**kw):
    return SelectionContext(repository="acme/app", **kw)


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.selector = IssueSelector(self.config)

    def evaluate(self, issue, **kw):
        return self.selector.evaluate(issue, ctx(**kw))

    def test_open_backlog_issue_is_eligible_for_implementation(self):
        e = self.evaluate(make_issue(1))
        self.assertTrue(e.eligible)
        self.assertEqual(e.mode, ProcessingMode.IMPLEMENT)

    def test_closed_issue_is_skipped(self):
        e = self.evaluate(make_issue(1, state="CLOSED"))
        self.assertEqual(e.category, SkipCategory.CLOSED)

    def test_status_must_be_backlog_case_insensitive(self):
        self.assertTrue(self.evaluate(make_issue(1, status="backlog")).eligible)
        e = self.evaluate(make_issue(1, status="In progress"))
        self.assertEqual(e.category, SkipCategory.WRONG_STATUS)

    def test_active_pr_is_skipped_but_closed_pr_is_not(self):
        open_pr = PullRef(9, "u", "OPEN", True, "x")
        closed_pr = PullRef(8, "u", "CLOSED", False, "y")
        self.assertEqual(self.evaluate(make_issue(1, prs=[open_pr])).category, SkipCategory.EXISTING_PR)
        self.assertTrue(self.evaluate(make_issue(1, prs=[closed_pr])).eligible)

    def test_unresolved_dependency_blocks_and_resolved_does_not(self):
        self.assertEqual(self.evaluate(make_issue(1, blocked=[(5, "OPEN")])).category, SkipCategory.BLOCKED)
        self.assertEqual(self.evaluate(make_issue(1, blocked=[(5, "UNKNOWN")])).category, SkipCategory.BLOCKED)
        self.assertTrue(self.evaluate(make_issue(1, blocked=[(5, "CLOSED")])).eligible)

    def test_epic_with_open_sub_issues_is_skipped(self):
        self.assertEqual(self.evaluate(make_issue(1, subs=[(2, "OPEN")])).category, SkipCategory.EPIC)
        self.assertTrue(self.evaluate(make_issue(1, subs=[(2, "CLOSED")])).eligible)

    def test_empty_body_is_insufficient(self):
        self.assertEqual(self.evaluate(make_issue(1, body="")).category, SkipCategory.INSUFFICIENT_INFO)

    def test_other_repository_and_draft_items(self):
        self.assertEqual(self.evaluate(make_issue(1, repo="other/repo")).category, SkipCategory.OTHER_REPO)
        draft = make_issue(1)
        draft.content_type = "DraftIssue"
        self.assertEqual(self.evaluate(draft).category, SkipCategory.NOT_ISSUE)

    def test_needs_verification_label_routes_to_verify(self):
        e = self.evaluate(make_issue(1, labels=["needs-verification"]))
        self.assertEqual(e.mode, ProcessingMode.VERIFY)

    def test_credential_rotation_requires_human_investigation(self):
        issue = make_issue(2, "P0", title="[SECURITY][CRITICAL] Rotate exposed credentials and APP_KEY; enforce secret scan")
        e = self.evaluate(issue)
        self.assertTrue(e.eligible)
        self.assertEqual(e.mode, ProcessingMode.INVESTIGATE)
        self.config.selection.investigate_human_required = False
        self.assertEqual(self.evaluate(issue).category, SkipCategory.NEEDS_HUMAN)

    def test_revoke_sessions_is_not_a_credential_operation(self):
        issue = make_issue(12, "P1", title="[SECURITY][HIGH] Revoke sessions and make reset tokens single-use on password reset")
        self.assertEqual(self.evaluate(issue).mode, ProcessingMode.IMPLEMENT)

    def test_production_readiness_label_is_investigate_only(self):
        e = self.evaluate(make_issue(52, "P3", labels=["production-readiness"]))
        self.assertEqual(e.mode, ProcessingMode.INVESTIGATE)

    def test_human_label_is_skipped(self):
        self.assertEqual(self.evaluate(make_issue(1, labels=["ai:needs-human"])).category, SkipCategory.NEEDS_HUMAN)

    def test_locked_issue_is_in_progress(self):
        self.assertEqual(self.evaluate(make_issue(1), locked={1}).category, SkipCategory.IN_PROGRESS)

    def test_attempted_this_run_is_excluded(self):
        self.assertEqual(self.evaluate(make_issue(1), attempted_this_run={1}).category, SkipCategory.ATTEMPTED)


class DuplicatePreventionTest(unittest.TestCase):
    def setUp(self):
        self.selector = IssueSelector(make_config())

    def evaluate(self, status, **kw):
        record = {"execution_id": "e1", "status": status, "pr_url": kw.pop("pr_url", None), "branch": "ai/fix-issue-1-x",
                  "failure_stage": "quality_gates", "failure_reason": "tests failed"}
        return self.selector.evaluate(make_issue(1), ctx(executions={1: record}, **kw))

    def test_completed_issue_is_never_reprocessed(self):
        e = self.evaluate(ExecutionStatus.SUCCEEDED.value, pr_url="https://x/pull/3")
        self.assertEqual(e.category, SkipCategory.COMPLETED)

    def test_local_only_success_is_not_reprocessed(self):
        self.assertEqual(self.evaluate(ExecutionStatus.SUCCEEDED.value).category, SkipCategory.COMPLETED)

    def test_running_or_interrupted_requires_resume(self):
        self.assertEqual(self.evaluate(ExecutionStatus.RUNNING.value).category, SkipCategory.IN_PROGRESS)
        self.assertEqual(self.evaluate(ExecutionStatus.INTERRUPTED.value).category, SkipCategory.IN_PROGRESS)

    def test_needs_human_is_skipped(self):
        self.assertEqual(self.evaluate(ExecutionStatus.NEEDS_HUMAN.value).category, SkipCategory.NEEDS_HUMAN)

    def test_failed_is_excluded_unless_retry_requested(self):
        self.assertEqual(self.evaluate(ExecutionStatus.FAILED.value).category, SkipCategory.FAILED)
        self.assertTrue(self.evaluate(ExecutionStatus.FAILED.value, retry_failed=True, attempts={1: 1}).eligible)

    def test_retry_limit_is_enforced(self):
        e = self.evaluate(ExecutionStatus.FAILED.value, retry_failed=True, attempts={1: 2})
        self.assertEqual(e.category, SkipCategory.FAILED)
        self.assertIn("retry limit", e.reasons[0])


class OrderingTest(unittest.TestCase):
    def setUp(self):
        self.selector = IssueSelector(make_config())

    def order(self, issues):
        evaluations = self.selector.evaluate_all(issues, ctx())
        return [e.issue.number for e in self.selector.ordered_eligible(evaluations)]

    def test_priority_order_p0_to_p3_and_unknown_last(self):
        issues = [make_issue(1, "P3"), make_issue(2, None), make_issue(3, "P0"), make_issue(4, "P1"), make_issue(5, "P2")]
        self.assertEqual(self.order(issues), [3, 4, 5, 1, 2])

    def test_fewer_dependencies_first_within_priority(self):
        a = make_issue(1, "P1", blocked=[(9, "CLOSED"), (8, "CLOSED")])
        b = make_issue(2, "P1")
        self.assertEqual(self.order([a, b]), [2, 1])

    def test_smaller_size_first(self):
        self.assertEqual(self.order([make_issue(1, "P1", size="XL"), make_issue(2, "P1", size="XS")]), [2, 1])

    def test_acceptance_criteria_preferred(self):
        vague = make_issue(1, "P2", body="## Problem\nSomething vague that is long enough to pass the minimum length check.")
        clear = make_issue(2, "P2")
        self.assertEqual(self.order([vague, clear]), [2, 1])

    def test_blocked_issue_not_selected(self):
        chosen, _ = self.selector.select_next([make_issue(1, "P0", blocked=[(2, "OPEN")]), make_issue(2, "P3")], ctx())
        self.assertEqual(chosen.issue.number, 2)

    def test_select_next_none_when_nothing_eligible(self):
        chosen, evals = self.selector.select_next([make_issue(1, state="CLOSED")], ctx())
        self.assertIsNone(chosen)
        self.assertEqual(len(evals), 1)

    def test_acceptance_criteria_count(self):
        self.assertEqual(acceptance_criteria_count(make_issue(1).body), 2)
        self.assertEqual(acceptance_criteria_count("no criteria"), 0)


if __name__ == "__main__":
    unittest.main()
