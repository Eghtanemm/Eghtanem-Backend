"""Thin, retrying wrapper around the GitHub CLI (``gh``).

Authentication is whatever ``gh`` is already logged in with; tokens are never
read, printed or stored by the agent.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from models import AuthError, GitHubError
from process import CommandResult, CommandRunner

TRANSIENT = re.compile(
    r"(?i)(timed? ?out|connection (reset|refused)|temporarily unavailable|502|503|504|bad gateway|"
    r"something went wrong|EOF|tls handshake|i/o timeout|network is unreachable|could not resolve host)"
)
RATE_LIMIT = re.compile(r"(?i)(rate limit|secondary rate|abuse detection|API rate limit exceeded)")
AUTH = re.compile(r"(?i)(not logged in|authentication required|bad credentials|401|gh auth login|token .* expired)")
SCOPE = re.compile(r"(?i)(INSUFFICIENT_SCOPES|required scopes|missing required scope|read:project|project scope)")


class GitHubClient:
    def __init__(self, runner: CommandRunner, max_retries: int = 5, sleep: Callable[[float], None] = time.sleep, logger=None):
        self.runner = runner
        self.max_retries = max_retries
        self.sleep = sleep
        self.logger = logger

    # ------------------------------------------------------------------ core

    def gh(self, args: list[str], *, cwd: str | None = None, input_text: str | None = None, check: bool = True,
           retry: bool = True) -> CommandResult:
        attempts = self.max_retries if retry else 1
        delay = 2.0
        last: CommandResult | None = None
        for attempt in range(1, attempts + 1):
            result = self.runner.run(["gh", *args], cwd=cwd, input_text=input_text, timeout=180)
            if result.ok:
                return result
            last = result
            text = result.combined()
            if SCOPE.search(text):
                raise AuthError(
                    "GitHub token lacks a required scope (projects need 'project'). Run: gh auth refresh -s project"
                )
            if AUTH.search(text) and not RATE_LIMIT.search(text):
                raise AuthError("GitHub CLI is not authenticated. Run: gh auth login")
            if RATE_LIMIT.search(text):
                wait = max(delay, 60.0)
                if self.logger:
                    self.logger.warning(f"GitHub rate limit hit; waiting {int(wait)}s (attempt {attempt}/{attempts})")
                self.sleep(wait)
                delay *= 2
                continue
            if TRANSIENT.search(text) or result.timed_out:
                if self.logger:
                    self.logger.debug(f"Transient GitHub error, retrying in {int(delay)}s: {text.strip()[:200]}")
                self.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            break
        if check and last is not None:
            raise GitHubError(f"gh {' '.join(args[:3])} failed: {last.combined().strip()[:800]}")
        return last  # type: ignore[return-value]

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}})
        result = self.gh(["api", "graphql", "--input", "-"], input_text=payload)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"Invalid GraphQL response: {result.stdout[:300]}") from exc
        errors = data.get("errors")
        if errors:
            message = "; ".join(e.get("message", str(e)) for e in errors)
            if any(e.get("type") == "INSUFFICIENT_SCOPES" for e in errors) or SCOPE.search(message):
                raise AuthError("GitHub token lacks the 'project' scope. Run: gh auth refresh -s project")
            if RATE_LIMIT.search(message):
                raise GitHubError(f"GraphQL rate limited: {message}")
            if not data.get("data"):
                raise GitHubError(f"GraphQL error: {message}")
            if self.logger:
                self.logger.debug(f"GraphQL partial errors: {message[:300]}")
        return data.get("data") or {}

    # ------------------------------------------------------------- helpers

    def auth_status(self) -> dict[str, Any]:
        result = self.runner.run(["gh", "auth", "status"], timeout=60)
        text = result.combined()
        if not result.ok or "Logged in" not in text:
            raise AuthError("GitHub CLI is not authenticated. Run: gh auth login")
        account = re.search(r"account (\S+)", text)
        scopes_line = re.search(r"Token scopes: (.*)", text)
        scopes = re.findall(r"'([^']+)'", scopes_line.group(1)) if scopes_line else []
        return {"account": account.group(1) if account else None, "scopes": scopes}

    def viewer_login(self) -> str:
        return self.graphql("query { viewer { login } }")["viewer"]["login"]

    def repository_info(self, owner: str, name: str) -> dict[str, Any]:
        data = self.graphql(
            """query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){
                 id nameWithOwner url isArchived viewerPermission defaultBranchRef{ name } } }""",
            {"owner": owner, "name": name},
        )
        repo = data.get("repository")
        if not repo:
            raise GitHubError(f"Repository {owner}/{name} not found or not accessible")
        return repo

    def comment_on_issue(self, repo: str, number: int, body: str) -> None:
        self.gh(["issue", "comment", str(number), "-R", repo, "--body-file", "-"], input_text=body)

    def ensure_label(self, repo: str, name: str, color: str = "d93f0b", description: str = "") -> None:
        result = self.gh(["label", "list", "-R", repo, "--search", name, "--json", "name", "-L", "100"])
        names = {l["name"].lower() for l in json.loads(result.stdout or "[]")}
        if name.lower() not in names:
            self.gh(["label", "create", name, "-R", repo, "--color", color, "--description", description])

    def add_label(self, repo: str, number: int, label: str) -> None:
        self.gh(["issue", "edit", str(number), "-R", repo, "--add-label", label])
