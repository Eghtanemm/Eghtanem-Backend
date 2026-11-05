"""End-to-end pipeline tests: real git (temp repo + bare remote), fake GitHub/Claude/gates."""

import copy
import shutil
import unittest
from pathlib import Path

from helpers import TempRepo, git, make_config, make_issue, quiet_logger

from execution_state import ExecutionState
from git_manager import GitManager
from issue_processor import IssueProcessor
from models import (
    ClaudeResult, Eligibility, ExecutionStatus, FieldOption, GateResult, GateStatus, ProcessingMode, ProjectField,
    QualityReport, ReviewFinding, ReviewResult, Stage, UsageLimitError,
)
from pr_manager import PullRequestInfo
from process import CommandRunner
from worktree_manager import WorktreeManager

ANALYSIS = {"summary": "OTP never expires", "problem": "p", "root_cause": "rc", "acceptance_criteria": ["a"],
            "verification": {"status": "verified", "evidence": ["app/Thing.php:3 secret detail"], "reproduction_steps": []},
            "fix_necessary": True, "requires_human": False, "human_reasons": [], "sufficient_information": True,
            "fully_resolvable": True, "audit_ref": "SEC-06"}
PLAN = {"root_cause": "rc", "affected_files": ["app/Thing.php"], "proposed_solution": "s", "database_changes": "none",
        "api_compatibility": "same", "security_implications": "none", "required_tests": ["t"], "risks": [], "rollback": "revert",
        "fully_resolves_issue": True, "requires_human": False, "human_reasons": [], "commit_summary": "Expire OTP codes"}
IMPL = {"status": "completed", "root_cause": "rc", "files_modified": ["app/Thing.php"], "changes_made": ["c"],
        "tests_added": ["tests/ThingExpiryTest.php"], "known_limitations": [], "potential_risks": [],
        "fully_resolves_issue": True, "requires_human": False, "human_reasons": [], "commit_summary": "Expire OTP codes"}


class FakeProject:
    def __init__(self, issue, options=("Backlog", "In progress", "In review", "Done")):
        self.issue = issue
        self.field = ProjectField("F", "Status", "SINGLE_SELECT", [FieldOption(o, o) for o in options])
        self.status_history = []

    def refresh_issue(self, number):
        return copy.deepcopy(self.issue)

    def status_option(self, name):
        return self.field.option(name)

    def set_status(self, issue, name):
        option = self.field.option(name)
        assert option, f"missing option {name}"
        self.status_history.append(option.name)
        self.issue.status = option.name
        issue.status = option.name
        return option.name


class FakeGH:
    def __init__(self):
        self.comments, self.labels = [], []

    def comment_on_issue(self, repo, number, body):
        self.comments.append(body)

    def ensure_label(self, *a, **k):
        pass

    def add_label(self, repo, number, label):
        self.labels.append(label)


class FakeClaude:
    sandbox = "/agent/php_sandbox.py"

    def __init__(self, overrides=None, edits=None, repair_edits=None):
        self.overrides = overrides or {}
        self.edits = edits if edits is not None else {"app/Thing.php": "<?php\n\nclass Thing { public int $ttl = 10; }\n",
                                                      "tests/ThingExpiryTest.php": "<?php\n// expiry test\n"}
        self.repair_edits = repair_edits or {}
        self.calls = []

    def run(self, stage, prompt, worktree, *, schema=None, max_turns=None, resume_session=None, execution_id=""):
        self.calls.append((stage, resume_session))
        override = self.overrides.get(stage)
        if isinstance(override, BaseException):
            raise override
        structured = override if isinstance(override, dict) else {
            "analyze": ANALYSIS, "plan": PLAN, "implement": IMPL, "repair": IMPL,
            "investigate": {"summary": "rotate", "current_state": "x", "code_changes_recommended": ["placeholders"],
                            "human_actions_required": ["rotate key at provider"], "risks": [], "verification_steps": []},
        }[stage]
        edits = self.edits if stage == "implement" else (self.repair_edits if stage == "repair" else {})
        for path, content in edits.items():
            target = Path(worktree) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return ClaudeResult(stage, f"sess-{stage}", "", structured, False, "success", 3, 0.0, [], 1.0)


class FakeQuality:
    def __init__(self, results=None):
        self.results = list(results or [True])
        self.runs = 0

    def baseline_for(self, sha):
        return {"phpunit": {"ran": True, "tests": [], "failed": {"Old::broken": "x"}}}

    def run(self, worktree, base_sha, files, baseline):
        self.runs += 1
        passed = self.results.pop(0) if self.results else True
        status = GateStatus.PASSED if passed else GateStatus.FAILED
        return QualityReport([GateResult("phpunit", status, "1 new failure" if not passed else "ok", "vendor/bin/phpunit", "FAILED X")])


class FakeReviewer:
    def __init__(self, verdicts=None):
        self.verdicts = list(verdicts or [True])
        self.calls = 0

    def review(self, issue, worktree, base_sha, diff, implementation, quality_summary, execution_id):
        self.calls += 1
        assert "Thing" in diff  # reviewer sees the actual diff
        ok = self.verdicts.pop(0) if self.verdicts else True
        findings = [] if ok else [ReviewFinding("high", "Missing authz check", "desc", "app/Thing.php", 3)]
        return ReviewResult("approve" if ok else "request_changes", findings, "summary", True), "sess-review"


class FakePRs:
    def __init__(self, interrupt_once=False, github_error_once=False):
        self.created = []
        self.interrupt_once = interrupt_once
        self.github_error_once = github_error_once

    def create(self, issue_number, branch, title, body):
        if self.github_error_once:
            self.github_error_once = False
            from models import GitHubError
            raise GitHubError("HTTP 502 while creating PR")
        if self.interrupt_once:
            self.interrupt_once = False
            raise KeyboardInterrupt
        existing = next((p for p in self.created if p[1] == branch), None)
        if existing:
            return PullRequestInfo(1, "https://x/pull/1", True, reused=True)
        self.created.append((issue_number, branch, title, body))
        return PullRequestInfo(1, "https://x/pull/1", True)

    def unverified(self, quality):
        return False


class ProcessorTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.config = make_config(self.repo.tmp)
        self.state = ExecutionState(self.repo.tmp / "state" / "agent.db")
        self.run_id = self.state.start_run("test")
        self.git = GitManager(CommandRunner(), self.repo.root, "origin", self.config.safety.protected_branches)
        self.worktrees = WorktreeManager(self.git, self.repo.tmp / "wt", "acme/app")

    def tearDown(self):
        self.state.close()
        self.repo.cleanup()

    def build(self, issue=None, claude=None, quality=None, reviewer=None, prs=None, no_pr=False, options=None):
        self.issue = issue or make_issue(7, "P1", title="Fix OTP expiration")
        self.project = FakeProject(self.issue, **({"options": options} if options else {}))
        self.gh = FakeGH()
        self.claude = claude or FakeClaude()
        self.quality = quality or FakeQuality()
        self.reviewer = reviewer or FakeReviewer()
        self.prs = prs or FakePRs()
        return IssueProcessor(config=self.config, project=self.project, gh=self.gh, git=self.git, worktrees=self.worktrees,
                              claude=self.claude, quality=self.quality, reviewer=self.reviewer, prs=self.prs, state=self.state,
                              logger=quiet_logger(), repository="acme/app", base_branch="main", run_id=self.run_id, no_pr=no_pr)

    def elig(self, mode=ProcessingMode.IMPLEMENT, reasons=None):
        return Eligibility(self.issue, True, mode, None, reasons or [])

    # ------------------------------------------------------------------ happy path

    def test_full_success_path(self):
        proc = self.build()
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED, outcome.failure_reason)
        self.assertEqual(outcome.pr_url, "https://x/pull/1")
        # status: In progress at claim, In review after PR; never Done
        self.assertEqual(self.project.status_history, ["In progress", "In review"])
        # commit pushed to the dedicated branch only; main untouched
        branch = "ai/fix-issue-7-fix-otp-expiration"
        self.assertTrue(self.git.remote_sha(branch))
        self.assertEqual(self.git.remote_sha("main"), git("rev-parse", "origin/main", cwd=self.repo.root))
        log = git("log", "-1", "--format=%s", self.git.remote_sha(branch), cwd=self.repo.root)
        self.assertEqual(log, "fix(#7): expire OTP codes")
        # one PR, linked and fully described
        self.assertEqual(len(self.prs.created), 1)
        _, pr_branch, title, body = self.prs.created[0]
        self.assertEqual(pr_branch, branch)
        self.assertEqual(title, "[AI] Fix #7 - Expire OTP codes")
        self.assertTrue(body.startswith("Closes #7"))
        # stages persisted, worktree cleaned up, issue comment posted
        record = self.state.get(outcome.execution_id)
        self.assertEqual(record["stage"], Stage.COMPLETED.value)
        self.assertFalse(Path(record["worktree_path"]).exists())
        self.assertIn("Draft PR #1", self.gh.comments[-1])
        # analyze -> plan -> implement share one Claude session
        self.assertEqual(self.claude.calls[1], ("plan", "sess-analyze"))
        self.assertEqual(self.claude.calls[2], ("implement", "sess-plan"))
        self.assertEqual(self.reviewer.calls, 1)
        self.assertEqual(self.state.locked_issues(), {})

    def test_missing_in_review_option_is_reported_not_invented(self):
        proc = self.build(options=("Backlog", "In progress", "Done"))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(self.project.status_history, ["In progress"])

    def test_no_pr_mode_is_local_only(self):
        proc = self.build(no_pr=True)
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(self.project.status_history, [])
        self.assertEqual(self.prs.created, [])
        self.assertEqual(self.gh.comments, [])
        self.assertIsNone(self.git.remote_sha("ai/fix-issue-7-fix-otp-expiration"))
        self.assertTrue(self.git.local_branch_exists("ai/fix-issue-7-fix-otp-expiration"))

    # ------------------------------------------------------------- repair loops

    def test_quality_failure_repaired_then_passes(self):
        proc = self.build(quality=FakeQuality([False, True]))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertIn(("repair", "sess-implement"), self.claude.calls)
        self.assertEqual(self.quality.runs, 2)

    def test_quality_failure_beyond_repair_limit_fails_without_pr(self):
        proc = self.build(quality=FakeQuality([False, False, False, False]))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.FAILED)
        self.assertEqual(outcome.failure_stage, "quality_gates")
        self.assertEqual(sum(1 for s, _ in self.claude.calls if s == "repair"), self.config.claude.max_repair_attempts)
        self.assertEqual(self.prs.created, [])
        self.assertIsNone(self.git.remote_sha("ai/fix-issue-7-fix-otp-expiration"))
        self.assertEqual(self.project.status_history, ["In progress", "Backlog"])  # reverted
        self.assertIn("Work kept in", outcome.recommended_action)  # agent work is never discarded

    def test_review_blocking_findings_repaired(self):
        proc = self.build(reviewer=FakeReviewer([False, True]))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(self.reviewer.calls, 2)
        self.assertEqual(self.quality.runs, 2)  # gates re-run after the review repair

    def test_review_still_blocking_fails(self):
        proc = self.build(reviewer=FakeReviewer([False, False, False]))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.FAILED)
        self.assertEqual(outcome.failure_stage, "review")
        self.assertEqual(self.prs.created, [])

    def test_safety_violation_escalates_to_human(self):
        bad = {"database/migrations/2024_01_01_000000_create_things.php": "<?php // edited existing migration\n"}
        proc = self.build(claude=FakeClaude(edits=bad, repair_edits=bad))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.NEEDS_HUMAN)
        self.assertEqual(outcome.failure_stage, "safety")
        self.assertEqual(self.prs.created, [])

    def test_no_changes_is_not_committed(self):
        proc = self.build(claude=FakeClaude(edits={}, repair_edits={}))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.NEEDS_HUMAN)
        self.assertIn("no changes", outcome.failure_reason)
        self.assertFalse(self.git.local_branch_exists("ai/fix-issue-7-fix-otp-expiration"))  # pristine -> discarded

    # ---------------------------------------------------------- human required

    def test_analysis_requires_human(self):
        analysis = {**ANALYSIS, "requires_human": True, "human_reasons": ["needs production key rotation"]}
        proc = self.build(claude=FakeClaude({"analyze": analysis}))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.NEEDS_HUMAN)
        self.assertIn("ai:needs-human", self.gh.labels)
        self.assertIn("needs production key rotation", self.gh.comments[0])
        self.assertEqual(self.project.status_history, ["In progress", "Backlog"])
        self.assertNotIn("implement", [s for s, _ in self.claude.calls])

    def test_post_merge_human_actions_do_not_block_the_code_change(self):
        actions = ["Remove the file from deployed servers and confirm a 404"]
        analysis = {**ANALYSIS, "post_merge_human_actions": actions}
        plan = {**PLAN, "post_merge_human_actions": actions, "fully_resolves_issue": False}
        proc = self.build(claude=FakeClaude({"analyze": analysis, "plan": plan}))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED, outcome.failure_reason)
        _, _, title, body = self.prs.created[0]
        self.assertTrue(title.startswith("[AI] Partially address #7"))
        self.assertTrue(body.startswith("Refs #7"))
        self.assertIn(actions[0], body)
        self.assertIn("will not close it", self.gh.comments[-1])
        self.assertNotIn("ai:needs-human", self.gh.labels)

    def test_human_label_recorded(self):
        analysis = {**ANALYSIS, "requires_human": True, "human_reasons": ["product decision"]}
        proc = self.build(claude=FakeClaude({"analyze": analysis}))
        outcome = proc.process(self.elig())
        self.assertEqual(self.state.get(outcome.execution_id)["human_labeled"], 1)

    def test_unverified_finding_is_not_fixed(self):
        analysis = {**ANALYSIS, "verification": {"status": "uncertain", "evidence": ["could not find"], "reproduction_steps": []}}
        self.issue = make_issue(7, "P1", labels=["needs-verification"])
        proc = self.build(issue=self.issue, claude=FakeClaude({"analyze": analysis}))
        outcome = proc.process(self.elig(ProcessingMode.VERIFY))
        self.assertEqual(outcome.status, ExecutionStatus.NEEDS_HUMAN)
        self.assertEqual(outcome.failure_stage, "verification")
        self.assertIn("could not find", self.gh.comments[0])

    def test_security_issue_comment_hides_evidence(self):
        analysis = {**ANALYSIS, "requires_human": True, "human_reasons": ["product decision"]}
        issue = make_issue(7, "P1", labels=["security"])
        proc = self.build(issue=issue, claude=FakeClaude({"analyze": analysis}))
        proc.process(Eligibility(issue, True, ProcessingMode.IMPLEMENT, None, []))
        self.assertNotIn("secret detail", self.gh.comments[0])
        self.assertIn("security-sensitive", self.gh.comments[0])
        report = next((self.config.state_dir / "reports" / "human").iterdir())
        self.assertIn("secret detail", report.read_text())  # kept privately

    def test_investigate_mode_never_implements(self):
        issue = make_issue(2, "P0", title="Rotate exposed credentials and APP_KEY")
        proc = self.build(issue=issue)
        outcome = proc.process(Eligibility(issue, True, ProcessingMode.INVESTIGATE, None, ["credential rotation"]))
        self.assertEqual(outcome.status, ExecutionStatus.NEEDS_HUMAN)
        self.assertEqual([s for s, _ in self.claude.calls], ["investigate"])
        self.assertIn("rotate key at provider", self.gh.comments[0])
        self.assertEqual(self.project.status_history, [])  # never claimed In progress
        self.assertEqual(self.git.local_branches_for_issue(2), [])
        self.assertEqual([w for w in self.worktrees.list() if "investigate" in str(w.path)], [])

    # ------------------------------------------------------------------- claim

    def test_claim_fails_when_remote_branch_exists(self):
        git("push", "-q", "origin", "main:refs/heads/ai/fix-issue-7-other-machine", cwd=self.repo.root)
        proc = self.build()
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.ABANDONED)
        self.assertEqual(self.project.status_history, [])
        self.assertEqual(self.claude.calls, [])

    def test_claim_fails_when_status_changed_meanwhile(self):
        proc = self.build()
        self.project.issue.status = "In progress"
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.ABANDONED)
        self.assertEqual(self.claude.calls, [])

    def test_claim_fails_when_locked(self):
        proc = self.build()
        self.state.acquire_issue_lock(7, "someone-else")
        with self.state.tx() as c:
            c.execute("UPDATE issue_locks SET pid=1 WHERE issue_number=7")  # pid 1 is alive
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.ABANDONED)

    # ----------------------------------------------------------- systemic/resume

    def test_systemic_error_propagates_and_marks_interrupted(self):
        proc = self.build(claude=FakeClaude({"implement": UsageLimitError("limit")}))
        with self.assertRaises(UsageLimitError):
            proc.process(self.elig())
        record = self.state.latest(7)
        self.assertEqual(record["status"], ExecutionStatus.INTERRUPTED.value)
        self.assertEqual(record["stage"], Stage.PLANNED.value)
        self.assertEqual(self.state.locked_issues(), {})

    def test_resume_after_interruption_does_not_duplicate_work(self):
        proc = self.build(prs=FakePRs(interrupt_once=True))
        with self.assertRaises(KeyboardInterrupt):
            proc.process(self.elig())
        record = self.state.latest(7)
        self.assertEqual(record["stage"], Stage.PUSHED.value)
        commit_before = self.git.remote_sha("ai/fix-issue-7-fix-otp-expiration")
        calls_before = list(self.claude.calls)
        self.project.issue.status = "In progress"
        outcome = proc.resume(self.state.interrupted()[0])
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED, outcome.failure_reason)
        self.assertEqual(len(self.prs.created), 1)
        self.assertEqual(self.git.remote_sha("ai/fix-issue-7-fix-otp-expiration"), commit_before)  # no new commit
        self.assertEqual(self.claude.calls, calls_before)  # no Claude stage repeated

    def test_pr_failure_after_push_is_resumable_not_failed(self):
        proc = self.build(prs=FakePRs(github_error_once=True))
        outcome = proc.process(self.elig())
        self.assertEqual(outcome.status, ExecutionStatus.FAILED)
        self.assertIn("--resume", outcome.recommended_action)
        record = self.state.latest(7)
        self.assertEqual(record["status"], ExecutionStatus.INTERRUPTED.value)
        self.assertEqual(self.project.status_history, ["In progress"])  # not reverted: branch is pushed
        resumed = proc.resume(self.state.interrupted()[0])
        self.assertEqual(resumed.status, ExecutionStatus.SUCCEEDED, resumed.failure_reason)
        self.assertEqual(len(self.prs.created), 1)
        self.assertEqual(self.project.status_history, ["In progress", "In review"])

    def test_resume_mid_implementation_continues_from_plan(self):
        proc = self.build(claude=FakeClaude({"implement": KeyboardInterrupt()}))
        with self.assertRaises(KeyboardInterrupt):
            proc.process(self.elig())
        self.assertEqual(self.state.latest(7)["stage"], Stage.PLANNED.value)
        proc.claude = FakeClaude()
        self.project.issue.status = "In progress"
        outcome = proc.resume(self.state.interrupted()[0])
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED, outcome.failure_reason)
        self.assertEqual([s for s, _ in proc.claude.calls], ["implement"])  # analysis/plan not repeated

    def test_retry_archives_previous_failed_attempt(self):
        proc = self.build(quality=FakeQuality([False, False, False]))
        first = proc.process(self.elig())
        self.assertEqual(first.status, ExecutionStatus.FAILED)
        self.project.issue.status = "Backlog"
        proc.quality = FakeQuality([True])
        second = proc.process(self.elig())
        self.assertEqual(second.status, ExecutionStatus.SUCCEEDED, second.failure_reason)
        archived = git("branch", "--list", "ai-archive/issue-7-*", cwd=self.repo.root)
        self.assertTrue(archived.strip())


if __name__ == "__main__":
    unittest.main()
