import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import FakeRunner, claude_json, make_config, ok, quiet_logger

from claude_runner import ClaudeRunner, render
from models import AuthError, IssueFailure, UsageLimitError
from process import CommandResult


class FakePhpEnv:
    available = True
    mode = "docker"
    image = "app-image"


class ClaudeRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.runner = FakeRunner()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def claude(self):
        return ClaudeRunner(self.config, self.runner, quiet_logger(), self.tmp, FakePhpEnv(), self.tmp / "logs")

    def test_command_uses_safe_non_interactive_permissions(self):
        self.runner.on(lambda a: a[0] == "claude", ok(claude_json({"x": 1})))
        result = self.claude().run("analyze", "prompt", self.tmp, schema={"type": "object"}, execution_id="e1")
        args = self.runner.calls[0]["args"]
        self.assertEqual(result.structured, {"x": 1})
        for flag in ("-p", "--restricted", "--json-schema", "--session-id"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("bypassPermissions", args)
        allowed = args[args.index("--allowedTools") + 1]
        self.assertNotIn("Edit", allowed.split(","))  # analysis is read-only
        self.assertIn("Bash(git diff:*)", allowed)
        self.assertNotIn("Bash(git push", allowed)
        self.assertEqual(self.runner.calls[0]["cwd"], str(self.tmp))
        self.assertEqual(self.runner.calls[0]["input"], "prompt")
        self.assertEqual(self.runner.calls[0]["env"]["AI_AGENT_WORKTREE"], str(self.tmp))
        self.assertTrue((self.tmp / "logs" / "e1").exists())

    def test_implement_stage_can_edit(self):
        self.runner.on(lambda a: True, ok(claude_json({})))
        self.claude().run("implement", "p", self.tmp)
        allowed = self.runner.calls[0]["args"][self.runner.calls[0]["args"].index("--allowedTools") + 1].split(",")
        self.assertIn("Edit", allowed)
        self.assertIn("Write", allowed)
        self.assertFalse(any(a.startswith("Bash(git commit") or a == "Bash" for a in allowed))

    def test_model_turns_and_resume(self):
        self.config.claude.model = "sonnet"
        self.runner.on(lambda a: True, ok(claude_json({})))
        self.claude().run("plan", "p", self.tmp, max_turns=7, resume_session="abc")
        args = self.runner.calls[0]["args"]
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertEqual(args[args.index("--max-turns") + 1], "7")
        self.assertEqual(args[args.index("--resume") + 1], "abc")
        self.assertNotIn("--session-id", args)

    def test_timeout_is_isolated_failure(self):
        self.runner.on(lambda a: True, CommandResult([], -1, "", "", 5.0, timed_out=True))
        with self.assertRaises(IssueFailure):
            self.claude().run("implement", "p", self.tmp)

    def test_max_turns_is_isolated_failure(self):
        self.runner.on(lambda a: True, ok(claude_json(None, subtype="error_max_turns", is_error=True)))
        with self.assertRaises(IssueFailure) as ctx:
            self.claude().run("implement", "p", self.tmp)
        self.assertIn("max-turns", ctx.exception.reason)

    def test_auth_failure_is_systemic(self):
        self.runner.on(lambda a: True, CommandResult([], 1, "", "Not logged in · Please run /login", 1.0))
        with self.assertRaises(AuthError):
            self.claude().run("analyze", "p", self.tmp)

    def test_usage_limit_is_systemic(self):
        self.runner.on(lambda a: True, ok(json.dumps({"is_error": True, "result": "Claude usage limit reached", "subtype": "success"})))
        with self.assertRaises(UsageLimitError):
            self.claude().run("analyze", "p", self.tmp)

    def test_missing_structured_output_fails(self):
        self.runner.on(lambda a: True, ok(claude_json(None)))
        with self.assertRaises(IssueFailure):
            self.claude().run("analyze", "p", self.tmp, schema={"type": "object"})

    def test_logs_are_redacted(self):
        secret = "ghp_" + "Z" * 36
        self.runner.on(lambda a: True, ok(claude_json({"note": secret})))
        self.claude().run("analyze", f"prompt {secret}", self.tmp, execution_id="e2")
        for f in (self.tmp / "logs" / "e2").iterdir():
            self.assertNotIn(secret, f.read_text())

    def test_installation_check(self):
        self.runner.on(lambda a: a[1:] == ["--version"], ok("2.1.0 (Claude Code)"))
        self.runner.on(lambda a: a[1:] == ["auth", "status"], ok('{"loggedIn": true, "authMethod": "claude.ai"}'))
        self.assertEqual(self.claude().check_installation()["auth_method"], "claude.ai")
        runner = FakeRunner().on(lambda a: a[1:] == ["auth", "status"], ok('{"loggedIn": false}'))
        with self.assertRaises(AuthError):
            ClaudeRunner(self.config, runner, quiet_logger(), self.tmp, FakePhpEnv(), self.tmp).check_installation()

    def test_all_prompt_templates_render(self):
        values = {k: "v" for k in ("issue_number", "issue_title", "issue_url", "issue_labels", "issue_priority", "issue_body",
                                    "repository", "mode", "mode_instructions", "analysis", "plan", "sandbox",
                                    "baseline_failures", "implementation_report", "quality_summary", "base_sha", "diff",
                                    "repair_source", "attempt", "max_attempts", "problems", "human_reasons")}
        for name in ("analyze.md", "plan.md", "implement.md", "review.md", "repair.md", "investigate.md"):
            self.assertNotIn("{{", render(name, values))
        with self.assertRaises(ValueError):
            render("analyze.md", {})


if __name__ == "__main__":
    unittest.main()
