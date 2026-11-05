"""Shared fakes for the unit tests. Nothing here talks to GitHub or Claude."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

from config import Config  # noqa: E402
from logger import AgentLogger  # noqa: E402
from models import Issue, IssueRef, PullRef  # noqa: E402
from process import CommandResult  # noqa: E402


def make_config(tmp: Path | None = None) -> Config:
    config = Config()
    if tmp:
        config.execution.state_dir = str(tmp / "state")
        config.execution.worktree_root = str(tmp / "worktrees")
    return config


def quiet_logger() -> AgentLogger:
    import io
    return AgentLogger(None, verbose=False, stream=io.StringIO())


BODY = """**Verification status:** Confirmed

## Problem
Something is wrong in a controller and needs fixing with care.

## Acceptance criteria
- [ ] The thing works.
- [ ] A regression test exists.
"""


def make_issue(number: int, priority: str | None = "P2", *, status: str = "Backlog", labels: list[str] | None = None,
               state: str = "OPEN", body: str = BODY, blocked: list[tuple[int, str]] | None = None,
               prs: list[PullRef] | None = None, subs: list[tuple[int, str]] | None = None, title: str | None = None,
               size: str | None = None, repo: str = "acme/app") -> Issue:
    labels = list(labels or [])
    if priority:
        labels.append(f"priority:{priority}")
    return Issue(
        number=number, title=title or f"Fix problem {number}", body=body, url=f"https://github.com/{repo}/issues/{number}",
        state=state, repository=repo, item_id=f"ITEM_{number}", status=status, labels=labels, priority=priority,
        size=size, blocked_by=[IssueRef(n, s, repo, "native") for n, s in (blocked or [])],
        sub_issues=[IssueRef(n, s, repo, "native") for n, s in (subs or [])], linked_prs=list(prs or []),
    )


class FakeRunner:
    """Scripted CommandRunner: first matching handler wins; unmatched commands succeed with empty output."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.handlers: list[tuple[Callable[[list[str]], bool], Callable[..., CommandResult]]] = []

    def on(self, predicate: Callable[[list[str]], bool], handler: Callable[..., CommandResult] | CommandResult):
        fn = handler if callable(handler) else (lambda *a, **k: handler)
        self.handlers.append((predicate, fn))
        return self

    def run(self, args, *, cwd=None, env=None, input_text=None, timeout=None) -> CommandResult:
        args = list(args)
        self.calls.append({"args": args, "cwd": cwd, "env": env, "input": input_text, "timeout": timeout})
        for predicate, handler in self.handlers:
            if predicate(args):
                return handler(args, cwd=cwd, env=env, input_text=input_text)
        return CommandResult(args, 0, "", "", 0.0)


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult([], 0, stdout, stderr, 0.01)


def fail(stderr: str = "boom", code: int = 1, stdout: str = "") -> CommandResult:
    return CommandResult([], code, stdout, stderr, 0.01)


class FakeGraphQL:
    """Stands in for GitHubClient in project-client tests."""

    def __init__(self, responder: Callable[[str, dict[str, Any]], dict[str, Any]]):
        self.responder = responder
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        self.queries.append((query, variables or {}))
        return self.responder(query, variables or {})


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class TempRepo:
    """A real git repository with a bare 'origin' remote, for git/worktree tests."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-test-"))
        self.remote = self.tmp / "remote.git"
        self.root = self.tmp / "repo"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        git("config", "user.email", "test@example.com", cwd=self.root)
        git("config", "user.name", "Test", cwd=self.root)
        git("config", "commit.gpgsign", "false", cwd=self.root)
        (self.root / "app").mkdir()
        (self.root / "app" / "Thing.php").write_text("<?php\n\nclass Thing {}\n")
        (self.root / "database" / "migrations").mkdir(parents=True)
        (self.root / "database" / "migrations" / "2024_01_01_000000_create_things.php").write_text(
            "<?php\nreturn new class { public function up() { Schema::create('things', fn ($t) => $t->id()); }"
            " public function down() { Schema::dropIfExists('things'); } };\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "ThingTest.php").write_text("<?php\n// test\n")
        (self.root / ".gitignore").write_text("/vendor\n.env\n")
        git("add", "-A", cwd=self.root)
        git("commit", "-q", "-m", "initial", cwd=self.root)
        git("remote", "add", "origin", str(self.remote), cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        git("fetch", "-q", "origin", cwd=self.root)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


def claude_json(structured: dict[str, Any] | None, *, subtype: str = "success", is_error: bool = False,
                session: str = "sess-1", denials: list | None = None, result: str = "") -> str:
    return json.dumps({
        "type": "result", "subtype": subtype, "is_error": is_error, "session_id": session, "num_turns": 3,
        "total_cost_usd": 0.0, "result": result or json.dumps(structured or {}), "structured_output": structured,
        "permission_denials": denials or [],
    })
