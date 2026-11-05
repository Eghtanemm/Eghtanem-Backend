#!/usr/bin/env python3
"""AI backlog agent: GitHub Project backlog -> Claude Code -> quality gates ->
independent review -> PR, one issue at a time, until the eligible backlog
is exhausted.

    python3 scripts/ai-backlog-agent/main.py               # whole eligible backlog
    python3 scripts/ai-backlog-agent/main.py --dry-run     # preview only
    python3 scripts/ai-backlog-agent/main.py --once        # one issue
    python3 scripts/ai-backlog-agent/main.py --limit 5     # at most five attempts
    python3 scripts/ai-backlog-agent/main.py --issue 42    # a specific issue
    python3 scripts/ai-backlog-agent/main.py --resume      # continue interrupted work
    python3 scripts/ai-backlog-agent/main.py --retry-failed
    python3 scripts/ai-backlog-agent/main.py --check       # preflight only (safe)
"""

from __future__ import annotations

import argparse
import shlex
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_runner import ClaudeRunner  # noqa: E402
from code_reviewer import CodeReviewer  # noqa: E402
from config import Config, ConfigError, load_config  # noqa: E402
from engine import Engine, RunOptions  # noqa: E402
from execution_state import ExecutionState, RunLock  # noqa: E402
from git_manager import GitManager  # noqa: E402
from github_client import GitHubClient  # noqa: E402
from issue_processor import IssueProcessor  # noqa: E402
from issue_selector import IssueSelector  # noqa: E402
from logger import AgentLogger  # noqa: E402
from models import AuthError, RepositoryIntegrityError, SystemicError, UnsafeEnvironmentError  # noqa: E402
from php_env import PhpEnvironment  # noqa: E402
from pr_manager import PullRequestManager  # noqa: E402
from process import CommandRunner  # noqa: E402
from project_client import ProjectClient  # noqa: E402
from quality_gate import QualityGate  # noqa: E402
from report import build_summary, dry_run_report, render_summary, save_report  # noqa: E402
from safety import environment_problems  # noqa: E402
from worktree_manager import WorktreeManager  # noqa: E402

EXIT_OK, EXIT_SYSTEMIC, EXIT_CONFIG, EXIT_INTERRUPTED = 0, 2, 3, 130


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Process the GitHub Project backlog sequentially with Claude Code and open PRs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Default (no flags): process every eligible issue, one by one, until none remain.",
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--once", action="store_true", help="process exactly one eligible issue and exit")
    scope.add_argument("--limit", type=int, metavar="N", help="attempt at most N issues (attempts, not successes)")
    scope.add_argument("--issue", type=int, metavar="NUMBER", help="process this issue only (all eligibility rules still apply)")
    p.add_argument("--dry-run", action="store_true", help="show eligible issues, order and skip reasons; change nothing")
    p.add_argument("--check", action="store_true", help="run preflight checks only (auth, project schema, git, PHP, Claude)")
    p.add_argument("--resume", action="store_true", help="resume interrupted executions first, then continue the backlog")
    p.add_argument("--retry-failed", action="store_true", help="retry previously failed issues that are still eligible")
    p.add_argument("--no-pr", action="store_true",
                   help="local only: commit on the issue branch but do not push, open PRs, comment or change project fields")
    p.add_argument("--max-turns", type=int, metavar="N", help="Claude max turns for implementation/repair stages")
    p.add_argument("--timeout", type=int, metavar="SECONDS", help="wall-clock timeout per Claude invocation")
    p.add_argument("--max-repair-attempts", type=int, metavar="N", help="repair attempts per failing gate/review")
    p.add_argument("--model", help="Claude model alias or name (overrides config)")
    p.add_argument("--config", metavar="PATH", help="config file (default: scripts/ai-backlog-agent/config.yaml if present)")
    p.add_argument("--verbose", "-v", action="store_true", help="show debug output")
    args = p.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        p.error("--limit must be >= 1")
    if args.dry_run and (args.resume or args.retry_failed):
        p.error("--dry-run cannot be combined with --resume/--retry-failed")
    return args


def apply_overrides(config: Config, args: argparse.Namespace) -> None:
    if args.max_turns is not None:
        config.claude.max_turns = args.max_turns
    if args.timeout is not None:
        config.claude.timeout = args.timeout
    if args.max_repair_attempts is not None:
        config.claude.max_repair_attempts = args.max_repair_attempts
    if args.model:
        config.claude.model = args.model


@dataclass
class Environment:
    runner: CommandRunner
    gh: GitHubClient
    git: GitManager
    repository: str
    base_branch: str
    project: ProjectClient
    php: PhpEnvironment
    claude_info: dict
    warnings: list[str]


def preflight(config: Config, logger: AgentLogger, *, dry_run: bool, runner: CommandRunner | None = None) -> Environment:
    runner = runner or CommandRunner()
    warnings: list[str] = []

    problems = environment_problems()
    if problems:
        raise UnsafeEnvironmentError("; ".join(problems))

    repo_root = GitManager.find_repo_root(runner, Path(__file__).resolve().parent)
    git = GitManager(runner, repo_root, config.github.remote, config.safety.protected_branches)
    integrity = git.integrity_check()
    if integrity:
        raise RepositoryIntegrityError("; ".join(integrity))

    gh = GitHubClient(runner, config.github.max_retries, logger=logger)
    auth = gh.auth_status()
    scopes = set(auth["scopes"])
    if scopes and not scopes & {"project", "read:project"}:
        raise AuthError("GitHub token lacks the project scope. Run: gh auth refresh -s project")
    if scopes and not dry_run and "project" not in scopes:
        raise AuthError("Updating project fields needs the 'project' scope (not just read:project). Run: gh auth refresh -s project")
    logger.info(f"GitHub CLI authenticated as {auth['account']}")

    repository = git.detect_repository() if config.github.repository == "auto" else config.github.repository
    owner, name = repository.split("/", 1)
    repo_info = gh.repository_info(owner, name)
    if repo_info.get("isArchived"):
        raise UnsafeEnvironmentError(f"{repository} is archived")
    if not dry_run and repo_info.get("viewerPermission") not in ("WRITE", "MAINTAIN", "ADMIN"):
        raise AuthError(f"Your GitHub account has '{repo_info.get('viewerPermission')}' permission on {repository}; WRITE is required")

    base_branch = repo_info["defaultBranchRef"]["name"] if config.github.base_branch == "auto" else config.github.base_branch
    git.fetch(base_branch)
    base_sha = git.resolve(f"{config.github.remote}/{base_branch}")
    logger.info(f"Repository {repository}; base branch '{base_branch}' @ {base_sha[:10]}")

    project = ProjectClient(gh, config.github.organization, config.github.project_number, repository, config, logger)
    schema = project.schema()
    logger.info(f"Project: {schema.title} ({schema.url})")
    status = schema.field("Status")
    if not status:
        raise SystemicError("Project has no 'Status' field; cannot determine the Backlog")
    ps = config.project_status
    if not status.option(ps.backlog):
        raise SystemicError(f"Status option '{ps.backlog}' not found (options: {[o.name for o in status.options]})")
    for key in ("in_progress", "in_review", "blocked"):
        wanted = getattr(ps, key)
        found = status.option(wanted)
        if not found:
            msg = f"Status option '{wanted}' ({key}) does not exist in the project"
            if key == "in_review" and ps.in_review_fallback:
                msg += f"; fallback '{ps.in_review_fallback}' will be used"
            warnings.append(msg)
            logger.warning(msg)
        elif found.name != wanted:
            logger.debug(f"Status '{wanted}' matched project option '{found.name}'")
    if not schema.field(config.selection.priority_field):
        logger.debug(f"No '{config.selection.priority_field}' field; priority is read from labels")

    php = PhpEnvironment.detect(runner, repo_root, config.quality_gates.php_runner, config.quality_gates.docker_image)
    if php.available:
        logger.info(f"PHP quality gates run via {php.mode}" + (f" (image {php.image}, no network)" if php.image else ""))
        if not php.vendor_dir.joinpath("autoload.php").exists():
            msg = f"{php.vendor_dir} is missing; run 'composer install' in the main checkout (inside docker if needed)"
            if not dry_run and config.quality_gates.on_unavailable == "fail":
                raise SystemicError(msg)
            warnings.append(msg)
    else:
        msg = f"PHP environment unavailable: {php.reason}"
        if not dry_run and config.quality_gates.on_unavailable == "fail":
            raise SystemicError(msg + " (required quality gates cannot run)")
        warnings.append(msg)
        logger.warning(msg)

    claude_info: dict = {}
    claude = ClaudeRunner(config, runner, logger, repo_root, php, config.state_dir / "logs")
    try:
        claude_info = claude.check_installation()
        logger.info(f"Claude Code {claude_info['version']} authenticated ({claude_info.get('auth_method')}, "
                    f"{claude_info.get('subscription') or 'n/a'})")
    except AuthError as exc:
        if not dry_run:
            raise
        warnings.append(str(exc))
        logger.warning(str(exc))
    return Environment(runner, gh, git, repository, base_branch, project, php, claude_info, warnings)


def build_processor(config: Config, env: Environment, state: ExecutionState, logger: AgentLogger, run_id: str,
                    no_pr: bool) -> IssueProcessor:
    worktrees = WorktreeManager(env.git, config.worktree_root, env.repository, logger)
    claude = ClaudeRunner(config, env.runner, logger, env.git.repo_root, env.php, config.state_dir / "logs")
    quality = QualityGate(config, env.php, state, logger, env.git, worktrees)
    return IssueProcessor(
        config=config, project=env.project, gh=env.gh, git=env.git, worktrees=worktrees, claude=claude, quality=quality,
        reviewer=CodeReviewer(claude, config),
        prs=PullRequestManager(env.gh, env.repository, env.base_branch, config.safety.protected_branches, logger,
                               draft=config.pull_requests.draft),
        state=state, logger=logger, repository=env.repository, base_branch=env.base_branch, run_id=run_id, no_pr=no_pr,
    )


def run_dry(config: Config, env: Environment, logger: AgentLogger) -> int:
    state = ExecutionState(config.state_dir / "agent.db", read_only=True)
    engine = Engine(config=config, project=env.project, selector=IssueSelector(config), processor=None, state=state,
                    logger=logger, repository=env.repository)
    issues = env.project.fetch_issues()
    evaluations = IssueSelector(config).evaluate_all(issues, engine.context(set(), False))
    ordered = IssueSelector(config).ordered_eligible(evaluations)
    schema = env.project.schema()
    status = schema.field("Status")
    mapping = {k: (status.option(getattr(config.project_status, k)).name if status.option(getattr(config.project_status, k)) else "MISSING")
               for k in ("backlog", "in_progress", "in_review", "blocked")}
    logger.raw(dry_run_report(evaluations, {
        "title": schema.title, "url": schema.url, "repository": env.repository, "base_branch": env.base_branch,
        "status_mapping": mapping, "warnings": env.warnings,
    }, ordered, state.interrupted()))
    state.close()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        apply_overrides(config, args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.resume and not config.execution.resume_enabled:
        print("Configuration error: --resume requested but execution.resume_enabled is false", file=sys.stderr)
        return EXIT_CONFIG

    read_only = args.dry_run or args.check
    log_file = None if read_only else config.state_dir / "logs" / "agent.jsonl"
    logger = AgentLogger(log_file, verbose=args.verbose)
    if config.source_path:
        logger.debug(f"Config: {config.source_path}")

    def _sigterm(signum, frame):  # treat SIGTERM like Ctrl+C: save state, stop cleanly
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)

    try:
        env = preflight(config, logger, dry_run=read_only)
    except SystemicError as exc:
        logger.error(f"Preflight failed ({type(exc).__name__}): {exc}")
        return EXIT_SYSTEMIC
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED

    if args.check:
        logger.info("Preflight checks passed." + (f" {len(env.warnings)} warning(s)." if env.warnings else ""))
        return EXIT_OK
    if args.dry_run:
        return run_dry(config, env, logger)

    lock = RunLock(config.state_dir / "agent.lock")
    try:
        lock.acquire()
    except UnsafeEnvironmentError as exc:
        logger.error(str(exc))
        return EXIT_SYSTEMIC

    state = ExecutionState(config.state_dir / "agent.db")
    run_id = state.start_run(" ".join(shlex.quote(a) for a in (argv if argv is not None else sys.argv[1:])))
    logger.bind(run_id=run_id)
    limit = 1 if args.once else (args.limit if args.limit is not None else config.execution.default_limit)
    logger.info(f"Run {run_id} started: " + (f"limit {limit} issue(s)" if limit else "processing the entire eligible backlog")
                + (" [--no-pr: local commits only]" if args.no_pr else ""))
    interrupted = state.interrupted()
    if interrupted and not args.resume:
        logger.warning(f"{len(interrupted)} interrupted execution(s) found: "
                       + ", ".join(f"#{r['issue_number']}" for r in interrupted) + " (run with --resume to continue them)")

    processor = build_processor(config, env, state, logger, run_id, args.no_pr)
    engine = Engine(config=config, project=env.project, selector=IssueSelector(config), processor=processor, state=state,
                    logger=logger, repository=env.repository)
    result = engine.run(RunOptions(limit=limit, issue=args.issue, resume=args.resume, retry_failed=args.retry_failed))

    summary = build_summary(result, run_id)
    text = render_summary(summary)
    logger.raw(text)
    path = save_report(config.state_dir, run_id, summary, text)
    logger.info(f"Report saved to {path}")
    status = "systemic_failure" if result.systemic_error else ("interrupted" if result.interrupted else "completed")
    state.finish_run(run_id, status, summary["totals"])
    state.close()
    lock.release()
    if result.systemic_error:
        return EXIT_SYSTEMIC
    if result.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
