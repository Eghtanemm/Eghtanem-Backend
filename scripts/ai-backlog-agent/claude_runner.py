"""Runs Claude Code non-interactively for one stage.

Permission model (verified against the installed CLI):

* ``--print --output-format json`` - non-interactive, machine-readable result
* ``--restricted`` - file tools confined to the worktree (cwd); settings files
  that could widen permissions are ignored
* ``--permission-mode dontAsk --permission-prompts none`` - anything not
  explicitly allow-listed is denied, nobody is prompted
* ``--tools`` / ``--allowedTools`` - per-stage minimal tool set; Bash is only
  allowed for read-only git commands and the PHP sandbox wrapper
* ``--max-turns`` + a hard wall-clock timeout

Authentication is the user's existing Claude Code login (subscription); no API
key is read or required.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from models import AuthError, ClaudeResult, IssueFailure, UsageLimitError
from process import CommandRunner
from safety import redact

AGENT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = AGENT_DIR / "prompts"

AUTH_FAILURE = re.compile(r"(?i)(not logged in|please run /login|invalid api key|authentication_error|oauth token (has )?expired|401 unauthorized)")
USAGE_LIMIT = re.compile(r"(?i)(usage limit reached|limit will reset|rate_limit_error|credit balance is too low|quota exceeded)")

READ_ONLY_GIT = ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)",
                 "Bash(git grep:*)", "Bash(git ls-files:*)"]

GUARDRAILS = """
You are running inside an automated backlog agent, in an isolated git worktree for ONE GitHub issue.
Hard rules (violating them fails the run):
- Only touch files inside the current working directory (the worktree). Never read or write .env files or secrets.
- Do NOT run git commit, git push, git checkout, git reset, git stash or any branch operation; the orchestrator owns git.
- Do NOT edit or delete existing database migrations; create new migrations instead.
- Never write destructive data operations, never rotate/revoke credentials, never touch production systems.
- Do not disable, skip or delete existing tests; do not weaken validation or security controls.
- PHP is not available directly. To run PHP tools use ONLY these exact command forms, each as a single
  standalone Bash command (no shell variables, no `cd`, no `&&`, `;`, pipes or redirections - those are denied):
    python3 {sandbox} phpunit --filter SomeTest
    python3 {sandbox} pint path/to/File.php
    python3 {sandbox} route-list
- If a command is denied or a tool cannot run, carry on with your task: the orchestrator independently runs
  Pint, PHPUnit and the other quality gates after you. A tooling or permission problem is NEVER a reason to set
  requires_human.
- Set requires_human only when the correct change itself needs a human: credential/APP_KEY rotation, production
  access or data changes, destructive/irreversible migrations, infrastructure/deployment, removing a public API
  without a migration plan, changes that could lock users out, or an unresolved product decision.
""".strip()


def render(template_name: str, values: dict[str, Any]) -> str:
    text = (PROMPTS_DIR / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        if not isinstance(value, str):
            value = json.dumps(value, indent=2, default=str)
        text = text.replace("{{" + key + "}}", value)
    leftover = re.findall(r"\{\{(\w+)\}\}", text)
    if leftover:
        raise ValueError(f"Unfilled prompt placeholders in {template_name}: {leftover}")
    return text


class ClaudeRunner:
    def __init__(self, config, runner: CommandRunner, logger, repo_root: Path, php_env, log_dir: Path):
        self.cfg = config.claude
        self.config = config
        self.runner = runner
        self.logger = logger
        self.repo_root = repo_root
        self.php_env = php_env
        self.log_dir = log_dir
        self.sandbox = str(AGENT_DIR / "php_sandbox.py")

    # ---------------------------------------------------------------- checks

    def check_installation(self) -> dict[str, Any]:
        version = self.runner.run([self.cfg.binary, "--version"], timeout=60)
        if not version.ok:
            raise AuthError(f"Claude Code CLI not found or not working ('{self.cfg.binary} --version' failed)")
        status = self.runner.run([self.cfg.binary, "auth", "status"], timeout=60)
        try:
            data = json.loads(status.stdout)
        except json.JSONDecodeError:
            data = {}
        if not status.ok or not data.get("loggedIn"):
            raise AuthError("Claude Code is not authenticated. Run: claude auth login (or start 'claude' and use /login)")
        return {"version": version.stdout.strip(), "auth_method": data.get("authMethod"),
                "subscription": data.get("subscriptionType")}

    # ------------------------------------------------------------------- run

    def tools_for(self, stage: str) -> tuple[list[str], list[str]]:
        sandbox_rule = f"Bash(python3 {self.sandbox}:*)"
        read = ["Read", "Glob", "Grep"]
        if stage in ("analyze", "plan", "review", "investigate"):
            return read + ["Bash"], read + READ_ONLY_GIT + [sandbox_rule]
        if stage in ("implement", "repair"):
            return read + ["Edit", "Write", "Bash"], read + ["Edit", "Write"] + READ_ONLY_GIT + [sandbox_rule]
        raise ValueError(f"unknown stage {stage}")

    def run(self, stage: str, prompt: str, worktree: Path, *, schema: dict[str, Any] | None = None,
            max_turns: int | None = None, resume_session: str | None = None, execution_id: str = "") -> ClaudeResult:
        tools, allowed = self.tools_for(stage)
        session_id = None if resume_session else str(uuid.uuid4())
        args = [self.cfg.binary, "-p", "--output-format", "json",
                "--max-turns", str(max_turns or self.cfg.max_turns),
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--tools", ",".join(tools), "--allowedTools", ",".join(allowed),
                "--append-system-prompt", GUARDRAILS.format(sandbox=self.sandbox)]
        if self.cfg.restricted:
            args.append("--restricted")
        if self.cfg.model:
            args += ["--model", self.cfg.model]
        if self.cfg.effort:
            args += ["--effort", self.cfg.effort]
        if resume_session:
            args += ["--resume", resume_session]
        else:
            args += ["--session-id", session_id]
        if schema:
            args += ["--json-schema", json.dumps(schema)]
        env = {
            "AI_AGENT_WORKTREE": str(worktree),
            "AI_AGENT_REPO_ROOT": str(self.repo_root),
            "AI_AGENT_PHP_RUNNER": self.php_env.mode if self.php_env.available else "auto",
            "AI_AGENT_DOCKER_IMAGE": self.php_env.image or "auto",
        }
        log_base = self.log_dir / (execution_id or "adhoc")
        log_base.mkdir(parents=True, exist_ok=True)
        stamp = len(list(log_base.glob(f"{stage}*.prompt.md")))
        prompt_file = log_base / f"{stage}-{stamp}.prompt.md"
        prompt_file.write_text(redact(prompt), encoding="utf-8")

        self.logger.debug(f"claude {stage}: max_turns={max_turns or self.cfg.max_turns} timeout={self.cfg.timeout}s")
        result = self.runner.run(args, cwd=str(worktree), env=env, input_text=prompt, timeout=self.cfg.timeout)
        out_file = log_base / f"{stage}-{stamp}.output.json"
        out_file.write_text(redact(result.stdout or "") + ("\n--- stderr ---\n" + redact(result.stderr) if result.stderr else ""),
                            encoding="utf-8")

        if result.timed_out:
            raise IssueFailure(stage, f"Claude timed out after {self.cfg.timeout}s",
                               "Increase --timeout or split the issue into smaller pieces.")
        data: dict[str, Any] = {}
        try:
            data = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        text_blob = f"{result.stderr}\n{data.get('result', '') if isinstance(data, dict) else ''}"
        if AUTH_FAILURE.search(text_blob) or (data.get("api_error_status") == 401):
            raise AuthError("Claude Code authentication failed. Run: claude auth login")
        if USAGE_LIMIT.search(text_blob) or data.get("api_error_status") == 429:
            raise UsageLimitError("Claude usage/rate limit reached; stopping the batch until the limit resets")
        if not data:
            raise IssueFailure(stage, f"Claude returned no parsable output (exit {result.returncode}): "
                                      f"{redact(result.combined())[-400:]}")

        subtype = data.get("subtype", "")
        claude_result = ClaudeResult(
            stage=stage,
            session_id=data.get("session_id") or session_id,
            text=data.get("result") or "",
            structured=data.get("structured_output"),
            is_error=bool(data.get("is_error")),
            subtype=subtype,
            num_turns=int(data.get("num_turns") or 0),
            cost_usd=float(data.get("total_cost_usd") or 0.0),
            permission_denials=data.get("permission_denials") or [],
            duration=result.duration,
            log_path=str(out_file),
        )
        if claude_result.permission_denials:
            denied = "; ".join(
                f"{d.get('tool_name')}: {str(d.get('tool_input', {}).get('command') or d.get('tool_input', {}).get('file_path') or '')[:120]}"
                for d in claude_result.permission_denials[:5]
            )
            self.logger.warning(f"Claude {stage}: {len(claude_result.permission_denials)} operation(s) denied by policy ({denied})")
        if subtype == "error_max_turns":
            raise IssueFailure(stage, f"Claude hit the max-turns limit ({max_turns or self.cfg.max_turns})",
                               "Increase --max-turns or narrow the issue scope.")
        if claude_result.is_error or result.returncode != 0:
            raise IssueFailure(stage, f"Claude {stage} failed ({subtype or 'error'}, exit {result.returncode}): "
                                      f"{redact(claude_result.text)[:300]}")
        if schema and not isinstance(claude_result.structured, dict):
            raise IssueFailure(stage, "Claude did not return the required structured output")
        self.logger.debug(f"claude {stage} done: turns={claude_result.num_turns} {result.duration:.0f}s")
        return claude_result
