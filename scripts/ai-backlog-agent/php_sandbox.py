#!/usr/bin/env python3
"""Restricted PHP command wrapper exposed to Claude during a run.

Claude's only permitted way to execute PHP is:

    python3 <agent-dir>/php_sandbox.py phpunit [phpunit args]
    python3 <agent-dir>/php_sandbox.py pint [--test] [paths]
    python3 <agent-dir>/php_sandbox.py route-list

It must be invoked from inside the issue worktree assigned by the orchestrator
(``AI_AGENT_WORKTREE``). In docker mode commands run in a container without
network access; only the worktree and a read-only vendor/ are mounted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from php_env import PhpEnvironment  # noqa: E402
from process import CommandRunner  # noqa: E402

USAGE = "usage: php_sandbox.py {phpunit [args] | pint [--test] [paths] | route-list}"


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    worktree_env = os.environ.get("AI_AGENT_WORKTREE")
    repo_root = os.environ.get("AI_AGENT_REPO_ROOT")
    if not worktree_env or not repo_root:
        print("php_sandbox: must be run by the ai-backlog-agent (AI_AGENT_WORKTREE not set)", file=sys.stderr)
        return 2
    worktree = Path(worktree_env).resolve()
    cwd = Path.cwd().resolve()
    if cwd != worktree and worktree not in cwd.parents:
        print(f"php_sandbox: refusing to run outside the assigned worktree {worktree}", file=sys.stderr)
        return 2

    sub, args = argv[0], argv[1:]
    if sub == "phpunit":
        command = ["php", "vendor/bin/phpunit", "--colors=never", "--do-not-cache-result", *args]
    elif sub == "pint":
        if any(a.startswith("-") and a not in ("--test", "-v") for a in args):
            print("php_sandbox: only --test is allowed for pint", file=sys.stderr)
            return 2
        command = ["php", "vendor/bin/pint", *args]
    elif sub == "route-list":
        command = ["php", "artisan", "route:list", "--no-ansi"]
    else:
        print(USAGE, file=sys.stderr)
        return 2

    env = PhpEnvironment.detect(
        CommandRunner(), Path(repo_root),
        os.environ.get("AI_AGENT_PHP_RUNNER", "auto"), os.environ.get("AI_AGENT_DOCKER_IMAGE", "auto"),
    )
    if not env.available:
        print(f"php_sandbox: PHP environment unavailable: {env.reason}", file=sys.stderr)
        return 3
    result = env.run(worktree, command, timeout=int(os.environ.get("AI_AGENT_PHP_TIMEOUT", "900")))
    sys.stdout.write(result.stdout[-20000:])
    sys.stderr.write(result.stderr[-5000:])
    return result.returncode if result.returncode >= 0 else 124


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
