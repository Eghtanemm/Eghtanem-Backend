import json
import unittest

from helpers import FakeRunner, make_issue, ok

from github_client import GitHubClient
from models import GateResult, GateStatus, GitHubError, QualityReport, ReviewFinding, ReviewResult, UnsafeEnvironmentError
from pr_manager import PullRequestManager, commit_message, is_fully_resolved, pr_title, render_body
from safety import ChangedFile

PLAN = {"fully_resolves_issue": True, "proposed_solution": "Do X", "risks": ["r1"], "rollback": "revert",
        "database_changes": "None", "api_compatibility": "unchanged", "root_cause": "rc"}
IMPL = {"status": "completed", "fully_resolves_issue": True, "changes_made": ["changed A"], "tests_added": ["ATest"],
        "known_limitations": [], "potential_risks": [], "root_cause": "rc", "commit_summary": "Expire OTP codes after ten minutes"}


def review(passed=True, fully=True):
    findings = [] if passed else [ReviewFinding("high", "bug", "desc")]
    return ReviewResult("approve" if passed else "request_changes", findings + [ReviewFinding("low", "nit", "n")], "ok", fully)


def quality(status=GateStatus.PASSED):
    return QualityReport([GateResult("phpunit", status, "10 tests, 0 failing", "vendor/bin/phpunit"),
                          GateResult("phpstan", GateStatus.SKIPPED, "not configured", required=False)])


class FormattingTest(unittest.TestCase):
    def test_commit_message_and_title(self):
        self.assertEqual(commit_message(7, "Expire OTP codes after ten minutes."), "fix(#7): expire OTP codes after ten minutes")
        self.assertEqual(pr_title(7, "Expire OTP codes", True), "[AI] Fix #7 - Expire OTP codes")
        self.assertTrue(pr_title(7, "Expire OTP codes", False).startswith("[AI] Partially address #7"))
        self.assertLessEqual(len(pr_title(7, "word " * 40, True)), 72)

    def test_fully_resolved_requires_all_agreement(self):
        self.assertTrue(is_fully_resolved(PLAN, IMPL, review()))
        self.assertFalse(is_fully_resolved({**PLAN, "fully_resolves_issue": False}, IMPL, review()))
        self.assertFalse(is_fully_resolved(PLAN, {**IMPL, "status": "partial"}, review()))
        self.assertFalse(is_fully_resolved(PLAN, IMPL, review(fully=False)))

    def test_body_sections_and_closing_keyword(self):
        body = render_body(make_issue(7), {"summary": "OTP never expired", "audit_ref": "SEC-06"}, PLAN, IMPL,
                           [ChangedFile("app/A.php", "M", 3, 1)], quality(), review(), [], True, False, True)
        for section in ("## Summary", "## Root Cause", "## Implementation", "## Files Changed", "## Testing",
                        "## Quality Gates", "## Risks and Limitations", "## Database Changes", "## AI Disclosure"):
            self.assertIn(section, body)
        self.assertTrue(body.startswith("Closes #7"))
        self.assertIn("`vendor/bin/phpunit` → **passed**", body)
        self.assertIn("phpstan | ⏭️ skipped", body)
        self.assertIn("requires careful human review", body)

    def test_partial_fix_does_not_close(self):
        body = render_body(make_issue(7), {}, {**PLAN, "fully_resolves_issue": False, "unaddressed_criteria": ["rotate key"]},
                           IMPL, [], quality(), review(), [], False, False, True)
        self.assertNotIn("Closes #7", body)
        self.assertTrue(body.startswith("Refs #7"))
        self.assertIn("rotate key", body)

    def test_unverified_flag_is_visible(self):
        body = render_body(make_issue(7), {}, PLAN, IMPL, [], quality(GateStatus.UNAVAILABLE), review(), [], True, True, True)
        self.assertIn("UNVERIFIED", body)


class CreateTest(unittest.TestCase):
    def manager(self, runner):
        return PullRequestManager(GitHubClient(runner, sleep=lambda s: None), "acme/app", "main", ["main"])

    def view(self, draft=True, head="ai/fix-issue-7-x", base="main"):
        return ok(json.dumps({"number": 12, "url": "https://x/pull/12", "isDraft": draft, "state": "OPEN",
                              "headRefName": head, "baseRefName": base}))

    def test_creates_draft_pr_and_verifies(self):
        runner = FakeRunner()
        runner.on(lambda a: a[1:3] == ["pr", "list"], ok("[]"))
        runner.on(lambda a: a[1:3] == ["pr", "create"], ok("https://x/pull/12\n"))
        runner.on(lambda a: a[1:3] == ["pr", "view"], self.view())
        info = self.manager(runner).create(7, "ai/fix-issue-7-x", "[AI] Fix #7 - x", "Closes #7")
        self.assertEqual((info.number, info.url, info.reused), (12, "https://x/pull/12", False))
        create = next(c for c in runner.calls if c["args"][1:3] == ["pr", "create"])
        self.assertIn("--draft", create["args"])
        self.assertEqual(create["args"][create["args"].index("--base") + 1], "main")
        self.assertEqual(create["input"], "Closes #7")
        self.assertFalse(any("merge" in c["args"] for c in runner.calls))

    def test_existing_pr_is_reused_never_duplicated(self):
        runner = FakeRunner()
        runner.on(lambda a: a[1:3] == ["pr", "list"], ok('[{"number": 3, "url": "https://x/pull/3", "isDraft": true}]'))
        info = self.manager(runner).create(7, "ai/fix-issue-7-x", "t", "b")
        self.assertTrue(info.reused)
        self.assertFalse(any(c["args"][1:3] == ["pr", "create"] for c in runner.calls))

    def test_verification_rejects_wrong_head(self):
        runner = FakeRunner()
        runner.on(lambda a: a[1:3] == ["pr", "list"], ok("[]"))
        runner.on(lambda a: a[1:3] == ["pr", "create"], ok("https://x/pull/12"))
        runner.on(lambda a: a[1:3] == ["pr", "view"], self.view(head="other"))
        with self.assertRaises(GitHubError):
            self.manager(runner).create(7, "ai/fix-issue-7-x", "t", "b")

    def test_non_draft_is_converted_back_to_draft(self):
        runner = FakeRunner()
        runner.on(lambda a: a[1:3] == ["pr", "list"], ok("[]"))
        runner.on(lambda a: a[1:3] == ["pr", "create"], ok("https://x/pull/12"))
        runner.on(lambda a: a[1:3] == ["pr", "view"], self.view(draft=False))
        self.manager(runner).create(7, "ai/fix-issue-7-x", "t", "b")
        self.assertTrue(any(c["args"][1:3] == ["pr", "ready"] and "--undo" in c["args"] for c in runner.calls))

    def test_refuses_protected_branch(self):
        with self.assertRaises(UnsafeEnvironmentError):
            self.manager(FakeRunner()).create(7, "main", "t", "b")


if __name__ == "__main__":
    unittest.main()
