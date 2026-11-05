"""Safe integration tests against the REAL environment.

Opt-in:  AI_AGENT_INTEGRATION=1 python3 -m unittest test_integration_safe
Claude:  additionally set AI_AGENT_INTEGRATION_CLAUDE=1 (one tiny read-only Claude call, small cost)

What they do (and do NOT do):
  * read the real GitHub Project and evaluate the backlog       (read-only)
  * create a local scratch worktree on a throw-away local branch  (never pushed, deleted afterwards)
  * run the real PHP quality gates in Docker on synthetic changes  (no network, SQLite in-memory)
  * optionally run Claude read-only in the scratch worktree and verify the permission sandbox
  * NEVER push, open PRs, comment, label or change project fields
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import make_config, quiet_logger

from claude_runner import ClaudeRunner
from execution_state import ExecutionState
from git_manager import GitManager
from github_client import GitHubClient
from issue_selector import IssueSelector, SelectionContext
from main import preflight
from models import GateStatus
from php_env import PhpEnvironment
from process import CommandRunner
from project_client import ProjectClient
from quality_gate import QualityGate
from worktree_manager import WorktreeManager

ENABLED = os.environ.get("AI_AGENT_INTEGRATION") == "1"
CLAUDE = os.environ.get("AI_AGENT_INTEGRATION_CLAUDE") == "1"
PROBE_ISSUE = 999999  # an issue number that cannot exist in the project
PROBE_BRANCH = f"ai/fix-issue-{PROBE_ISSUE}-integration-probe"

PASSING_TEST = """<?php

namespace Tests\\Unit;

use PHPUnit\\Framework\\TestCase;

class AgentIntegrationProbeTest extends TestCase
{
    public function test_probe_passes(): void
    {
        $this->assertSame(2, 1 + 1);
    }
}
"""


@unittest.skipUnless(ENABLED, "set AI_AGENT_INTEGRATION=1 to run safe integration tests")
class SafeIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agent-integration-"))
        cls.config = make_config(cls.tmp)
        cls.runner = CommandRunner()
        cls.logger = quiet_logger()
        cls.env = preflight(cls.config, cls.logger, dry_run=True, runner=cls.runner)
        cls.git = cls.env.git
        cls.worktrees = WorktreeManager(cls.git, cls.tmp / "worktrees", cls.env.repository)
        cls.base_sha = cls.git.resolve(f"origin/{cls.env.base_branch}")

    @classmethod
    def tearDownClass(cls):
        for wt in cls.worktrees.list():
            if str(wt.path).startswith(str(cls.tmp)):
                cls.git.git(["worktree", "remove", "--force", str(wt.path)], check=False)
        cls.git.git(["worktree", "prune"], check=False)
        if cls.git.local_branch_exists(PROBE_BRANCH):
            cls.git.git(["branch", "-D", PROBE_BRANCH], check=False)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_1_real_project_discovery_and_selection(self):
        project = self.env.project
        schema = project.schema()
        self.assertIsNotNone(schema.field("Status"))
        self.assertIsNotNone(project.status_option(self.config.project_status.backlog))
        issues = project.fetch_issues()
        self.assertGreater(len(issues), 0)
        selector = IssueSelector(self.config)
        evaluations = selector.evaluate_all(issues, SelectionContext(repository=self.env.repository))
        ordered = selector.ordered_eligible(evaluations)
        prios = [e.issue.priority for e in ordered]
        ranks = [self.config.selection.priorities.index(p) if p in self.config.selection.priorities else 99 for p in prios]
        self.assertEqual(ranks, sorted(ranks))
        for e in ordered:
            self.assertEqual(e.issue.unresolved_blockers, [])
            self.assertEqual(e.issue.active_prs, [])

    def test_2_quality_gates_in_real_php_environment(self):
        php = self.env.php
        if not php.available:
            self.skipTest(f"PHP environment unavailable: {php.reason}")
        state = ExecutionState(self.tmp / "state.db")
        gate = QualityGate(self.config, php, state, self.logger, self.git, self.worktrees)
        baseline = gate.baseline_for(self.base_sha)
        self.assertTrue(baseline["phpunit"]["ran"], baseline)
        self.assertGreater(len(baseline["phpunit"]["tests"]), 0)

        path = self.worktrees.create(PROBE_ISSUE, PROBE_BRANCH, self.base_sha)
        try:
            # (a) a clean, passing change: pre-existing failures must not block
            (path / "tests" / "Unit" / "AgentIntegrationProbeTest.php").write_text(PASSING_TEST)
            self.git.stage_all(path)
            files = self.git.changed_files(path, self.base_sha)
            report = gate.run(path, self.base_sha, files, baseline)
            results = {r.name: r for r in report.results}
            self.assertEqual(results["phpunit"].status, GateStatus.PASSED, results["phpunit"].details)
            self.assertEqual(results["pint"].status, GateStatus.PASSED, results["pint"].details)
            self.assertTrue(report.passed)

            # (b) breaking a previously passing test must block
            example = path / "tests" / "Unit" / "ExampleTest.php"
            example.write_text(example.read_text().replace("assertTrue(true)", "assertTrue(false)"))
            self.git.stage_all(path)
            files = self.git.changed_files(path, self.base_sha)
            report = gate.run(path, self.base_sha, files, baseline)
            phpunit = next(r for r in report.results if r.name == "phpunit")
            self.assertEqual(phpunit.status, GateStatus.FAILED)
            self.assertIn("ExampleTest", phpunit.details)
            self.assertFalse(report.passed)

            # nothing left on the remote
            self.assertIsNone(self.git.remote_sha(PROBE_BRANCH))
        finally:
            self.git.git(["checkout", "--", "."], cwd=path, check=False)
            self.git.git(["reset", "-q"], cwd=path, check=False)
            self.git.git(["worktree", "remove", "--force", str(path)], check=False)
            state.close()

    def test_3_php_sandbox_refuses_outside_worktree(self):
        sandbox = Path(__file__).resolve().parent.parent / "php_sandbox.py"
        result = self.runner.run(["python3", str(sandbox), "phpunit"], cwd=str(self.tmp),
                                 env={"AI_AGENT_WORKTREE": str(self.tmp / "elsewhere"), "AI_AGENT_REPO_ROOT": str(self.git.repo_root)})
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing", result.stderr)

    @unittest.skipUnless(CLAUDE, "set AI_AGENT_INTEGRATION_CLAUDE=1 to include a real (read-only) Claude call")
    def test_4_claude_runs_confined_and_read_only(self):
        path = self.worktrees.create_detached("claude", self.base_sha, "probe")
        outside = self.tmp / "outside-probe.txt"
        try:
            self.config.claude.model = self.config.claude.model or "haiku"
            claude = ClaudeRunner(self.config, self.runner, self.logger, self.git.repo_root, self.env.php, self.tmp / "logs")
            schema = {"type": "object", "properties": {"framework": {"type": "string"}, "write_succeeded": {"type": "boolean"},
                                                       "sandbox_tests_passed": {"type": "boolean"}},
                      "required": ["framework", "write_succeeded", "sandbox_tests_passed"]}
            result = claude.run(
                "analyze",
                f"1) Read composer.json and report which PHP framework this project uses. "
                f"2) Try to write the text 'x' to {outside} and report whether it succeeded. Do not try any workaround. "
                f"3) Run exactly: python3 {claude.sandbox} phpunit --filter ExampleTest tests/Unit "
                f"and report whether the tests passed.",
                path, schema=schema, max_turns=8, execution_id="integration",
            )
            self.assertIn("laravel", result.structured["framework"].lower())
            self.assertFalse(outside.exists())
            self.assertFalse(result.structured["write_succeeded"])
            self.assertTrue(result.structured["sandbox_tests_passed"], result.text)
            denied = [d.get("tool_input", {}).get("command", "") for d in result.permission_denials]
            self.assertFalse(any("php_sandbox" in c for c in denied), denied)
            self.assertEqual(self.git.status_porcelain(path), [])
        finally:
            self.worktrees.remove_detached(path)


if __name__ == "__main__":
    unittest.main()


E2E = os.environ.get("AI_AGENT_INTEGRATION_E2E") == "1"

SYNTHETIC_BODY = """**Verification status:** Confirmed

## Context
The application has no single place that exposes its version string, so diagnostics code reads config directly.

## Problem
There is no small helper class returning the application version.

## Expected outcome
A tiny `App\\Support\\AppVersion` class with a static `current(): string` method that returns
`config('app.version', '1.0.0')`.

## Acceptance criteria
- [ ] `App\\Support\\AppVersion::current()` returns the configured `app.version` value.
- [ ] It returns `'1.0.0'` when `app.version` is not configured.
- [ ] A PHPUnit feature test (extending `Tests\\TestCase`) covers both cases.

## Dependencies
None.
"""


@unittest.skipUnless(ENABLED and E2E, "set AI_AGENT_INTEGRATION=1 AI_AGENT_INTEGRATION_E2E=1 (real Claude, several calls)")
class SyntheticEndToEndTest(unittest.TestCase):
    """Full pipeline with real Claude + real gates on an in-memory issue. GitHub writes are faked; --no-pr mode."""

    def test_pipeline_end_to_end(self):
        from helpers import make_issue
        from issue_processor import IssueProcessor
        from code_reviewer import CodeReviewer
        from models import Eligibility, ExecutionStatus, ProcessingMode
        from test_issue_processor import FakeGH, FakePRs, FakeProject

        tmp = Path(tempfile.mkdtemp(prefix="agent-e2e-"))
        config = make_config(tmp)
        config.claude.model = os.environ.get("AI_AGENT_E2E_MODEL", "sonnet")
        runner = CommandRunner()
        logger = quiet_logger() if not os.environ.get("AI_AGENT_E2E_VERBOSE") else __import__("logger").AgentLogger(None, True)
        env = preflight(config, logger, dry_run=True, runner=runner)
        issue = make_issue(PROBE_ISSUE, "P3", title="Add AppVersion helper", body=SYNTHETIC_BODY, repo=env.repository)
        state = ExecutionState(tmp / "state.db")
        worktrees = WorktreeManager(env.git, tmp / "worktrees", env.repository, logger)
        claude = ClaudeRunner(config, runner, logger, env.git.repo_root, env.php, tmp / "logs")
        processor = IssueProcessor(
            config=config, project=FakeProject(issue), gh=FakeGH(), git=env.git, worktrees=worktrees, claude=claude,
            quality=QualityGate(config, env.php, state, logger, env.git, worktrees), reviewer=CodeReviewer(claude, config),
            prs=FakePRs(), state=state, logger=logger, repository=env.repository, base_branch=env.base_branch,
            run_id=state.start_run("e2e"), no_pr=True,
        )
        branch = f"ai/fix-issue-{PROBE_ISSUE}-add-appversion-helper"
        try:
            outcome = processor.process(Eligibility(issue, True, ProcessingMode.IMPLEMENT, None, []))
            record = state.get(outcome.execution_id)
            print(f"\nE2E outcome: {outcome.status.value} stage={record['stage']} reason={outcome.failure_reason}")
            print(f"Quality: {outcome.quality_summary}\nLogs: {tmp / 'logs'}")
            self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED, outcome.failure_reason)
            changed = env.git.git(["show", "--name-only", "--format=%s", branch]).stdout
            print(changed)
            self.assertIn(f"fix(#{PROBE_ISSUE}):", changed)
            self.assertIn("app/Support/AppVersion.php", changed)
            self.assertIsNone(env.git.remote_sha(branch))  # never pushed
        finally:
            for wt in worktrees.list():
                if str(wt.path).startswith(str(tmp)):
                    env.git.git(["worktree", "remove", "--force", str(wt.path)], check=False)
            env.git.git(["worktree", "prune"], check=False)
            for b in env.git.local_branches_for_issue(PROBE_ISSUE):
                env.git.git(["branch", "-D", b], check=False)
            state.close()
            if not os.environ.get("AI_AGENT_E2E_KEEP"):
                shutil.rmtree(tmp, ignore_errors=True)
