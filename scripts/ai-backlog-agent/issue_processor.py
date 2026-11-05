"""Processes ONE issue end-to-end as a resumable stage machine.

claim -> workspace -> baseline -> analyze -> plan -> implement
      -> [quality gates -> independent review -> repair]* -> commit
      -> push -> PR -> project status -> cleanup

Every stage transition is persisted before moving on, so an interrupted
execution can be resumed from the last completed stage (``--resume``) without
repeating work (no second PR, no second commit).
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import schemas
from claude_runner import ClaudeRunner, render, tests_policy
from code_reviewer import CodeReviewer, format_findings, parse_review
from git_manager import GitManager, branch_name
from github_client import GitHubClient
from issue_selector import _same
from models import (
    AgentError, ClaimFailed, Eligibility, ExecutionStatus, GateResult, GateStatus, GitHubError, Issue, IssueFailure,
    IssueOutcome, NeedsHuman, ProcessingMode, QualityReport, ReviewResult, Stage, SystemicError, stage_at_least,
)
from pr_manager import PullRequestManager, commit_message, is_fully_resolved, pr_title, render_body
from project_client import ProjectClient
from quality_gate import QualityGate, format_report
from safety import ChangedFile, human_required_reasons, scan_diff
from worktree_manager import WorktreeManager

MODE_INSTRUCTIONS = {
    ProcessingMode.IMPLEMENT: "The finding is reported as confirmed. Still verify it against the code before planning a fix.",
    ProcessingMode.VERIFY: (
        "This issue is labelled `needs-verification`. Your primary job is to establish whether the finding is real. "
        "Only mark verification.status=verified with concrete code evidence. If you cannot verify it safely, say "
        "`uncertain` - do not invent a problem or a fix."
    ),
}


@dataclass
class ExecutionContext:
    execution_id: str
    issue: Issue
    mode: ProcessingMode
    stage: str | None = None
    branch: str = ""
    worktree: Path | None = None
    base_sha: str = ""
    sessions: dict[str, str] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    implementation: dict[str, Any] = field(default_factory=dict)
    quality: QualityReport | None = None
    review: ReviewResult | None = None
    files: list[ChangedFile] = field(default_factory=list)
    safety_warnings: list[str] = field(default_factory=list)
    baseline: dict[str, Any] = field(default_factory=dict)
    commit_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    status_changed: bool = False
    resuming: bool = False
    human_reasons: list[str] = field(default_factory=list)


def quality_from_dict(data: dict[str, Any] | None) -> QualityReport | None:
    if not data:
        return None
    return QualityReport([
        GateResult(r["name"], GateStatus(r["status"]), r.get("summary", ""), r.get("command", ""), r.get("details", ""),
                   r.get("duration", 0.0), r.get("required", True))
        for r in data.get("results") or []
    ])


class IssueProcessor:
    def __init__(self, *, config, project: ProjectClient, gh: GitHubClient, git: GitManager, worktrees: WorktreeManager,
                 claude: ClaudeRunner, quality: QualityGate, reviewer: CodeReviewer, prs: PullRequestManager, state,
                 logger, repository: str, base_branch: str, run_id: str, no_pr: bool = False):
        self.config = config
        self.project = project
        self.gh = gh
        self.git = git
        self.worktrees = worktrees
        self.claude = claude
        self.quality = quality
        self.reviewer = reviewer
        self.prs = prs
        self.state = state
        self.log = logger
        self.repository = repository
        self.base_branch = base_branch
        self.run_id = run_id
        self.no_pr = no_pr

    # ================================================================ entry

    def process(self, elig: Eligibility) -> IssueOutcome:
        issue = elig.issue
        mode = elig.mode or ProcessingMode.IMPLEMENT
        execution_id = self.state.create_execution(self.run_id, issue.number, issue.url, issue.title, mode.value)
        ctx = ExecutionContext(execution_id, issue, mode, human_reasons=list(elig.reasons))
        self.log.bind(issue=issue.number, execution_id=execution_id)
        self.log.info(f"Selected #{issue.number} [{issue.priority or '-'}] {issue.title} (mode: {mode.value})")
        try:
            if not self.state.acquire_issue_lock(issue.number, execution_id):
                return self._abandon(ctx, "issue is locked by another execution")
            return self._guarded(ctx, fresh=True)
        finally:
            self.state.release_issue_lock(issue.number, execution_id)
            self.log.unbind("issue", "execution_id")

    def resume(self, record: dict[str, Any]) -> IssueOutcome:
        number = record["issue_number"]
        execution_id = record["execution_id"]
        self.log.bind(issue=number, execution_id=execution_id)
        self.log.info(f"Resuming {execution_id} for #{number} from stage '{record.get('stage') or 'start'}'")
        try:
            if not self.state.transfer_issue_lock(number, execution_id):
                self.state.event(execution_id, "resume", "lock held by a live process; not resuming")
                return self._outcome_from_record(execution_id, ExecutionStatus.ABANDONED)
            issue = self.project.refresh_issue(number)
            if issue is None or not issue.is_open:
                self.state.finish(execution_id, ExecutionStatus.ABANDONED, failure_reason="issue closed or removed from project")
                return self._outcome_from_record(execution_id, ExecutionStatus.ABANDONED)
            self.state.update(execution_id, status=ExecutionStatus.RUNNING, pid=self.state.pid, host=self.state.host,
                              run_id=self.run_id)
            ctx = ExecutionContext(
                execution_id=execution_id, issue=issue, mode=ProcessingMode(record.get("mode") or "implement"),
                stage=record.get("stage"), branch=record.get("branch") or "",
                worktree=Path(record["worktree_path"]) if record.get("worktree_path") else None,
                base_sha=record.get("base_sha") or "", sessions=record.get("claude_sessions") or {},
                analysis=record.get("analysis") or {}, plan=record.get("plan") or {},
                implementation=record.get("implementation") or {}, quality=quality_from_dict(record.get("quality")),
                review=parse_review(record["review"], self.config.quality_gates.blocking_review_severities)
                if isinstance(record.get("review"), dict) else None,
                commit_sha=record.get("commit_sha"), pr_number=record.get("pr_number"), pr_url=record.get("pr_url"),
                status_changed=True, resuming=True,
            )
            # Our own PR may legitimately exist; any *other* active PR means someone else took over.
            foreign = [p for p in issue.active_prs if p.head_ref != ctx.branch]
            if foreign:
                raise ClaimFailed(f"issue now has another active PR: {', '.join(p.url for p in foreign)}")
            return self._guarded(ctx, fresh=False)
        finally:
            self.state.release_issue_lock(number, execution_id)
            self.log.unbind("issue", "execution_id")

    # ============================================================== guarded

    def _guarded(self, ctx: ExecutionContext, fresh: bool) -> IssueOutcome:
        try:
            if fresh:
                self._claim(ctx)
            if ctx.mode == ProcessingMode.INVESTIGATE:
                return self._investigate(ctx)
            return self._pipeline(ctx)
        except ClaimFailed as exc:
            return self._abandon(ctx, str(exc))
        except NeedsHuman as exc:
            return self._needs_human(ctx, exc)
        except IssueFailure as exc:
            return self._failed(ctx, exc.stage, exc.reason, exc.recommended_action)
        except GitHubError as exc:
            return self._failed(ctx, ctx.stage or "github", str(exc), "Check GitHub connectivity/permissions and retry with --retry-failed.")
        except (SystemicError, KeyboardInterrupt) as exc:
            reason = "interrupted by user/signal" if isinstance(exc, KeyboardInterrupt) else f"systemic failure: {exc}"
            self.state.mark_interrupted(ctx.execution_id, reason)
            self.log.error(f"#{ctx.issue.number} stopped at stage '{ctx.stage}': {reason} (resumable with --resume)")
            raise
        except AgentError as exc:
            return self._failed(ctx, ctx.stage or "unknown", str(exc), "Inspect the execution log.")
        except Exception as exc:  # unexpected bug: isolate it, keep the trace
            self.state.event(ctx.execution_id, "internal_error", str(exc), {"traceback": traceback.format_exc()})
            return self._failed(ctx, ctx.stage or "internal", f"internal error: {exc!r}",
                                "This is likely a bug in the agent; see the JSON log for the traceback.")

    # ================================================================ claim

    def _claim(self, ctx: ExecutionContext) -> None:
        number = ctx.issue.number
        fresh = self.project.refresh_issue(number)
        if fresh is None:
            raise ClaimFailed("issue is no longer in the project")
        if not fresh.is_open:
            raise ClaimFailed("issue was closed")
        if not _same(fresh.status, self.config.project_status.backlog):
            raise ClaimFailed(f"status changed to '{fresh.status}' (being processed elsewhere?)")
        if fresh.active_prs:
            raise ClaimFailed(f"issue now has an active PR: {fresh.active_prs[0].url}")
        if fresh.unresolved_blockers and self.config.selection.skip_blocked_issues:
            raise ClaimFailed(f"blocked by #{fresh.unresolved_blockers[0].number}")
        remote = self.git.remote_branches_for_issue(number)
        if remote:
            raise ClaimFailed(f"remote branch already exists ({remote[0]}); another machine may be processing it")
        ctx.issue = fresh
        if not self.no_pr and ctx.mode != ProcessingMode.INVESTIGATE:
            target = self.config.project_status.in_progress
            if self.project.status_option(target):
                self.log.info(f"Claiming #{number}: status -> {self.project.status_option(target).name}")
                self.project.set_status(fresh, target)
                ctx.status_changed = True
                check = self.project.refresh_issue(number)
                if check is None or not _same(check.status, target):
                    raise ClaimFailed(f"status was changed concurrently (now '{check.status if check else '?'}')")
            else:
                self.log.warning(f"Status option '{target}' not found in project; claiming locally only")
            if self.config.execution.claim_comment:
                self.gh.comment_on_issue(self.repository, number,
                                         f"🤖 The AI backlog agent started working on this issue (execution `{ctx.execution_id}`).")
        self._stage(ctx, Stage.CLAIMED, "issue claimed")

    # ============================================================= pipeline

    def _pipeline(self, ctx: ExecutionContext) -> IssueOutcome:
        self._workspace(ctx)
        ctx.baseline = self.quality.baseline_for(ctx.base_sha)
        if not stage_at_least(ctx.stage, Stage.BASELINE_DONE):
            self._stage(ctx, Stage.BASELINE_DONE, "quality baseline measured")
        if not stage_at_least(ctx.stage, Stage.ANALYZED):
            self._analyze(ctx)
        if not stage_at_least(ctx.stage, Stage.PLANNED):
            self._plan(ctx)
        if not stage_at_least(ctx.stage, Stage.IMPLEMENTED):
            self._implement(ctx)
        if not stage_at_least(ctx.stage, Stage.REVIEWED):
            self._verify_loop(ctx)
        if not stage_at_least(ctx.stage, Stage.COMMITTED):
            self._commit(ctx)
        if self.no_pr:
            self.log.info(f"--no-pr: leaving commit {ctx.commit_sha[:10] if ctx.commit_sha else ''} on local branch {ctx.branch}")
            self._cleanup(ctx)
            self.state.finish(ctx.execution_id, ExecutionStatus.SUCCEEDED)
            return self._outcome(ctx, ExecutionStatus.SUCCEEDED)
        if not stage_at_least(ctx.stage, Stage.PUSHED):
            self._push(ctx)
        if not stage_at_least(ctx.stage, Stage.PR_CREATED):
            self._create_pr(ctx)
        if not stage_at_least(ctx.stage, Stage.PROJECT_UPDATED):
            self._update_project(ctx)
        self._cleanup(ctx)
        self._stage(ctx, Stage.COMPLETED, "execution completed")
        self.state.finish(ctx.execution_id, ExecutionStatus.SUCCEEDED)
        self.log.info(f"Issue #{ctx.issue.number} completed successfully: {ctx.pr_url}")
        return self._outcome(ctx, ExecutionStatus.SUCCEEDED)

    # ------------------------------------------------------------ workspace

    def _workspace(self, ctx: ExecutionContext) -> None:
        if stage_at_least(ctx.stage, Stage.WORKSPACE_READY) and ctx.branch and ctx.worktree:
            ctx.worktree = self.worktrees.attach_existing(ctx.issue.number, ctx.branch, ctx.worktree)
            return
        self.log.info("Creating isolated worktree...")
        self.git.fetch(self.base_branch)
        ctx.base_sha = self.git.resolve(f"{self.git.remote}/{self.base_branch}")
        ctx.branch = branch_name(ctx.issue.number, ctx.issue.title)
        self._archive_previous_attempts(ctx)
        ctx.worktree = self.worktrees.create(ctx.issue.number, ctx.branch, ctx.base_sha)
        self.state.update(ctx.execution_id, branch=ctx.branch, worktree_path=str(ctx.worktree), base_sha=ctx.base_sha,
                          base_branch=self.base_branch)
        self._stage(ctx, Stage.WORKSPACE_READY, f"worktree {ctx.worktree} on {ctx.branch} @ {ctx.base_sha[:10]}")

    def _archive_previous_attempts(self, ctx: ExecutionContext) -> None:
        """A previous *agent* attempt (known from state) may have left a branch/worktree: move it aside, never delete."""
        for record in self.state.history(ctx.issue.number):
            if record["execution_id"] == ctx.execution_id or not record.get("branch"):
                continue
            path = Path(record["worktree_path"]) if record.get("worktree_path") else None
            if self.git.local_branch_exists(record["branch"]) or (path and self.worktrees.find(path)):
                self.worktrees.archive(ctx.issue.number, record["branch"], path, record["execution_id"][-10:])

    # -------------------------------------------------------------- analyze

    def _issue_values(self, ctx: ExecutionContext) -> dict[str, str]:
        return {
            "issue_number": str(ctx.issue.number),
            "issue_title": ctx.issue.title,
            "issue_url": ctx.issue.url,
            "issue_labels": ", ".join(ctx.issue.labels) or "(none)",
            "issue_priority": ctx.issue.priority or "unknown",
            "issue_body": ctx.issue.body,
            "repository": self.repository,
        }

    def _analyze(self, ctx: ExecutionContext) -> None:
        self.log.info("Analyzing issue and repository...")
        prompt = render("analyze.md", {**self._issue_values(ctx), "mode": ctx.mode.value,
                                       "mode_instructions": MODE_INSTRUCTIONS[ctx.mode]})
        result = self.claude.run("analyze", prompt, ctx.worktree, schema=schemas.ANALYSIS,
                                 max_turns=self.config.claude.analysis_max_turns, execution_id=ctx.execution_id)
        ctx.analysis = result.structured or {}
        ctx.sessions["analyze"] = result.session_id or ""
        self.state.update(ctx.execution_id, analysis=ctx.analysis, claude_sessions=ctx.sessions)
        a = ctx.analysis
        verification = a.get("verification") or {}
        status = verification.get("status")
        evidence = "\n".join(f"- {e}" for e in verification.get("evidence") or [])
        if a.get("requires_human"):
            raise NeedsHuman("analysis", a.get("human_reasons") or ["analysis requires human decision"], a.get("summary", ""), evidence)
        if not a.get("sufficient_information", True):
            raise NeedsHuman("analysis", ["the issue lacks information needed for a safe implementation"], a.get("summary", ""), evidence)
        if status != "verified":
            raise NeedsHuman("verification", [f"finding could not be verified (status: {status})"], a.get("summary", ""), evidence)
        if not a.get("fix_necessary", True):
            raise NeedsHuman("verification", ["analysis concluded that no code fix is necessary"], a.get("summary", ""), evidence)
        details = ", ".join(str(x) for x in (a.get("audit_ref"), a.get("severity")) if x)
        self.log.info(f"Analysis complete: finding {status}" + (f" ({details})" if details else ""))
        self._stage(ctx, Stage.ANALYZED, "analysis completed")

    def _plan(self, ctx: ExecutionContext) -> None:
        self.log.info("Preparing implementation plan...")
        prompt = render("plan.md", {**self._issue_values(ctx), "analysis": ctx.analysis,
                                    "tests_policy": tests_policy(self.config.claude.write_tests, "plan")})
        resume = ctx.sessions.get("analyze") if self.config.claude.resume_sessions else None
        result = self._run_with_resume_fallback("plan", prompt, ctx, schemas.PLAN, resume, self.config.claude.analysis_max_turns)
        ctx.plan = result.structured or {}
        ctx.sessions["plan"] = result.session_id or ""
        self.state.update(ctx.execution_id, plan=ctx.plan, claude_sessions=ctx.sessions)
        if ctx.plan.get("requires_human"):
            raise NeedsHuman("plan", ctx.plan.get("human_reasons") or ["plan requires human approval"],
                             ctx.plan.get("proposed_solution", ""))
        self.log.info(f"Implementation plan completed ({len(ctx.plan.get('affected_files') or [])} files, "
                      f"fully resolves: {ctx.plan.get('fully_resolves_issue')})")
        self._stage(ctx, Stage.PLANNED, "plan completed")

    def _implement(self, ctx: ExecutionContext) -> None:
        self.log.info("Implementing...")
        failing = sorted(((ctx.baseline.get("phpunit") or {}).get("failed") or {}).keys())
        dirty_note = ""
        if ctx.resuming and self.git.status_porcelain(ctx.worktree):
            dirty_note = ("\n\nNOTE: this worktree contains partial changes from an interrupted earlier attempt. "
                          "Review them (git diff) and complete or correct them.")
        prompt = render("implement.md", {**self._issue_values(ctx), "analysis": ctx.analysis, "plan": ctx.plan,
                                         "sandbox": self.claude.sandbox,
                                         "tests_policy": tests_policy(self.config.claude.write_tests, "implement"),
                                         "baseline_failures": "\n".join(f"- {t}" for t in failing) or "- (none)"}) + dirty_note
        resume = ctx.sessions.get("plan") if self.config.claude.resume_sessions else None
        result = self._run_with_resume_fallback("implement", prompt, ctx, schemas.IMPLEMENTATION, resume, None)
        self._accept_implementation(ctx, result.structured or {}, result.session_id, "implement")
        self._stage(ctx, Stage.IMPLEMENTED, "implementation completed")
        self.log.info(f"Implementation completed ({len(ctx.implementation.get('files_modified') or [])} files modified)")

    def _accept_implementation(self, ctx: ExecutionContext, report: dict[str, Any], session: str | None, stage: str) -> None:
        ctx.implementation = report
        ctx.sessions["implement"] = session or ctx.sessions.get("implement", "")
        self.state.update(ctx.execution_id, implementation=report, claude_sessions=ctx.sessions)
        if report.get("requires_human"):
            raise NeedsHuman(stage, report.get("human_reasons") or ["implementation requires human approval"],
                             "; ".join(report.get("known_limitations") or []))
        if report.get("status") == "blocked":
            raise IssueFailure(stage, "implementation agent reported it is blocked: " + "; ".join(report.get("known_limitations") or []),
                               "Review the execution log and refine the issue description.")

    def _run_with_resume_fallback(self, stage, prompt, ctx, schema, resume, max_turns):
        if resume:
            try:
                return self.claude.run(stage, prompt, ctx.worktree, schema=schema, max_turns=max_turns,
                                       resume_session=resume, execution_id=ctx.execution_id)
            except IssueFailure as exc:
                if "no parsable output" not in exc.reason and "not found" not in exc.reason.lower():
                    raise
                self.log.debug(f"Could not resume session for {stage}; starting a fresh session")
        return self.claude.run(stage, prompt, ctx.worktree, schema=schema, max_turns=max_turns, execution_id=ctx.execution_id)

    # ---------------------------------------------- quality / review / repair

    def _diff_state(self, ctx: ExecutionContext) -> list[str]:
        self.git.stage_all(ctx.worktree)
        ctx.files = self.git.changed_files(ctx.worktree, ctx.base_sha)
        added = self.git.added_lines(ctx.worktree, ctx.base_sha)
        contents = {f.path: self.git.file_at(ctx.worktree, f.path) for f in ctx.files
                    if f.path.startswith("database/migrations/") and f.status.startswith("A")}
        s = self.config.safety
        scan = scan_diff(ctx.files, added, contents, s.forbidden_paths, s.max_changed_files, s.max_changed_lines)
        if not self.config.claude.write_tests:
            touched = [f.path for f in ctx.files if f.path.startswith("tests/")]
            if touched:
                scan.blocking.append("test files changed although writing tests is disabled (claude.write_tests=false): "
                                     + ", ".join(touched[:10]))
        mentioned = set(ctx.plan.get("affected_files") or []) | set(ctx.implementation.get("files_modified") or [])
        unexpected = [f.path for f in ctx.files if mentioned and f.path not in mentioned and not f.path.startswith("tests/")]
        ctx.safety_warnings = [f"File changed but not listed in the plan/report: `{p}`" for p in unexpected[:10]]
        return scan.blocking

    def _verify_loop(self, ctx: ExecutionContext) -> None:
        max_repairs = self.config.claude.max_repair_attempts
        gate_repairs = review_repairs = safety_repairs = 0
        while True:
            violations = self._diff_state(ctx)
            if violations:
                if safety_repairs >= max_repairs:
                    raise NeedsHuman("safety", violations, "The automated change violates safety rules; a human must decide.")
                safety_repairs += 1
                self.log.warning(f"Safety scan blocked the change: {violations}; repair {safety_repairs}/{max_repairs}")
                self._repair(ctx, "deterministic safety scan", "\n".join(f"- {v}" for v in violations), safety_repairs)
                continue

            self.log.info("Running quality gates (Pint, PHPUnit, PHPStan, migrations, audit)...")
            ctx.quality = self.quality.run(ctx.worktree, ctx.base_sha, ctx.files, ctx.baseline)
            self.git.stage_all(ctx.worktree)  # pint autofix may have changed files
            ctx.files = self.git.changed_files(ctx.worktree, ctx.base_sha)
            self.state.update(ctx.execution_id, quality=ctx.quality.to_dict())
            for line in format_report(ctx.quality).splitlines():
                self.log.info(line)
            if not ctx.quality.passed:
                blocking = ctx.quality.blocking
                if all(r.status == GateStatus.UNAVAILABLE for r in blocking):
                    raise IssueFailure("quality_gates", "required checks could not run: " + "; ".join(r.summary for r in blocking),
                                       "Fix the PHP test environment (see `main.py --check`) and retry with --retry-failed.")
                if gate_repairs >= max_repairs:
                    raise IssueFailure("quality_gates", "quality gates still failing after "
                                       f"{gate_repairs} repair attempt(s): " + "; ".join(f"{r.name}: {r.summary}" for r in blocking),
                                       "Inspect the kept worktree and the gate output; fix manually or refine the issue.")
                gate_repairs += 1
                problems = "\n\n".join(f"### {r.name}: {r.summary}\nCommand: {r.command}\n```\n{r.details[-5000:]}\n```" for r in blocking)
                self.log.warning(f"Quality gates failed; repair attempt {gate_repairs}/{max_repairs}")
                self._repair(ctx, "quality gates", problems, gate_repairs)
                continue
            self._stage(ctx, Stage.QUALITY_PASSED, "quality gates passed")

            if not self.config.quality_gates.independent_review:
                ctx.review = None
                break
            self.log.info("Running independent code review...")
            diff = self.git.diff_text(ctx.worktree, ctx.base_sha)
            ctx.review, session = self.reviewer.review(ctx.issue, ctx.worktree, ctx.base_sha, diff, ctx.implementation,
                                                       format_report(ctx.quality), ctx.execution_id)
            ctx.sessions["review"] = session or ""
            self.state.update(ctx.execution_id, review=ctx.review.raw, claude_sessions=ctx.sessions)
            if ctx.review.passed:
                self.log.info(f"Independent review passed ({ctx.review.verdict}, {len(ctx.review.findings)} non-blocking finding(s))")
                break
            findings = format_findings(ctx.review.blocking_findings) if ctx.review.blocking_findings else ctx.review.summary
            if review_repairs >= max_repairs:
                raise IssueFailure("review", f"review still blocking after {review_repairs} repair attempt(s) "
                                   f"(verdict {ctx.review.verdict}): {findings[:600]}",
                                   "Read the review findings in the execution log; fix manually or refine the issue.")
            review_repairs += 1
            self.log.warning(f"Review requested changes ({len(ctx.review.blocking_findings)} blocking); "
                             f"repair attempt {review_repairs}/{max_repairs}")
            self._repair(ctx, "independent code review", findings, review_repairs)
        self._stage(ctx, Stage.REVIEWED, "review completed")

    def _repair(self, ctx: ExecutionContext, source: str, problems: str, attempt: int) -> None:
        prompt = render("repair.md", {
            "issue_number": str(ctx.issue.number), "issue_title": ctx.issue.title, "repair_source": source,
            "attempt": str(attempt), "max_attempts": str(self.config.claude.max_repair_attempts),
            "problems": problems, "sandbox": self.claude.sandbox,
            "tests_policy": tests_policy(self.config.claude.write_tests, "repair"),
        })
        resume = ctx.sessions.get("implement") if self.config.claude.resume_sessions else None
        if not resume:
            prompt += "\n\n## Context\n\nPlan:\n```json\n" + json.dumps(ctx.plan, indent=2) + "\n```\n"
        result = self._run_with_resume_fallback("repair", prompt, ctx, schemas.IMPLEMENTATION, resume, None)
        self._accept_implementation(ctx, result.structured or {}, result.session_id, "repair")

    # --------------------------------------------------------------- commit

    def _commit(self, ctx: ExecutionContext) -> None:
        violations = self._diff_state(ctx)
        if violations:
            raise NeedsHuman("commit", violations, "Final safety scan failed before commit.")
        if self.git.current_branch(ctx.worktree) != ctx.branch:
            raise IssueFailure("commit", f"worktree is not on {ctx.branch}")
        if not (ctx.quality and ctx.quality.passed):
            raise IssueFailure("commit", "refusing to commit: quality gates have not passed")
        if self.config.quality_gates.independent_review and not (ctx.review and ctx.review.passed):
            raise IssueFailure("commit", "refusing to commit: independent review has not passed")
        summary = ctx.implementation.get("commit_summary") or ctx.plan.get("commit_summary") or ctx.issue.title
        message = commit_message(ctx.issue.number, summary)
        ctx.commit_sha = self.git.commit(ctx.worktree, message, ctx.issue.number)
        self.log.info(f"Committed {ctx.commit_sha[:10]}: {message}")
        self._stage(ctx, Stage.COMMITTED, message, commit_sha=ctx.commit_sha)

    # ----------------------------------------------------------- push & PR

    def _push(self, ctx: ExecutionContext) -> None:
        self.log.info(f"Pushing {ctx.branch}...")
        try:
            self.git.push(ctx.worktree, ctx.branch, ctx.issue.number)
        except AgentError as exc:
            if isinstance(exc, SystemicError):
                raise
            raise IssueFailure("push", str(exc), "Check push permissions/network; resume with --resume.") from exc
        self._stage(ctx, Stage.PUSHED, f"pushed {ctx.branch}")

    def _create_pr(self, ctx: ExecutionContext) -> None:
        self.log.info("Creating Draft PR..." if self.config.pull_requests.draft else "Creating PR...")
        if not ctx.files and ctx.worktree:
            self.git.stage_all(ctx.worktree)
            ctx.files = self.git.changed_files(ctx.worktree, ctx.base_sha)
        quality = ctx.quality or QualityReport([])
        fully = is_fully_resolved(ctx.plan, ctx.implementation, ctx.review, ctx.analysis)
        unverified = self.prs.unverified(quality)
        title = pr_title(ctx.issue.number, ctx.implementation.get("commit_summary") or ctx.issue.title, fully,
                         self.config.pull_requests.title_max_length)
        body = render_body(ctx.issue, ctx.analysis, ctx.plan, ctx.implementation, ctx.files, quality, ctx.review,
                           ctx.safety_warnings, fully, unverified, self.config.pull_requests.close_issue_only_when_fully_resolved,
                           short=self.config.pull_requests.description == "short")
        try:
            info = self.prs.create(ctx.issue.number, ctx.branch, title, body)
        except GitHubError as exc:
            raise IssueFailure("pull_request", f"PR creation failed: {exc}", "Branch is pushed; resume with --resume.") from exc
        ctx.pr_number, ctx.pr_url = info.number, info.url
        self._stage(ctx, Stage.PR_CREATED, f"PR {info.url}", pr_number=info.number, pr_url=info.url,
                    closes_issue=1 if fully else 0)
        self.log.info(f"PR created successfully: {info.url}")
        if self.config.pull_requests.comment_on_issue and not info.reused:
            link = "will close this issue when merged" if fully else "partially addresses this issue (it will not close it)"
            self._safe_comment(ctx.issue.number,
                               f"🤖 {'Draft PR' if info.is_draft else 'PR'} #{info.number} was opened by the AI backlog agent and {link}. "
                               "It requires human review before merging.")

    def _update_project(self, ctx: ExecutionContext) -> None:
        self.log.info("Updating project status...")
        cfg = self.config.project_status
        target = cfg.in_review if self.project.status_option(cfg.in_review) else cfg.in_review_fallback
        if target and self.project.status_option(target):
            self.project.set_status(ctx.issue, target)
            self.log.info(f"#{ctx.issue.number} status -> {self.project.status_option(target).name}")
        else:
            msg = f"Status option '{cfg.in_review}' missing and no fallback configured; status left as '{ctx.issue.status}'"
            self.log.warning(msg)
            self.state.event(ctx.execution_id, "project", msg)
        self._stage(ctx, Stage.PROJECT_UPDATED, "project status updated")

    # ---------------------------------------------------------- investigate

    def _investigate(self, ctx: ExecutionContext) -> IssueOutcome:
        reasons = ctx.human_reasons or human_required_reasons(
            ctx.issue.title, ctx.issue.body, ctx.issue.labels, self.config.safety.human_required_patterns, []
        ) or ["requires human intervention"]
        self.log.info(f"#{ctx.issue.number} requires human intervention ({'; '.join(reasons)}); investigating only")
        self.git.fetch(self.base_branch)
        base_sha = self.git.resolve(f"{self.git.remote}/{self.base_branch}")
        path = self.worktrees.create_detached(ctx.issue.number, base_sha, "investigate")
        try:
            prompt = render("investigate.md", {**self._issue_values(ctx), "human_reasons": "\n".join(f"- {r}" for r in reasons)})
            result = self.claude.run("investigate", prompt, path, schema=schemas.INVESTIGATION,
                                     max_turns=self.config.claude.analysis_max_turns, execution_id=ctx.execution_id)
        finally:
            self.worktrees.remove_detached(path)
        data = result.structured or {}
        self.state.update(ctx.execution_id, analysis=data)
        recommendation = "\n".join([
            data.get("summary", ""),
            "",
            "**Code changes a developer can make:**",
            *[f"- {c}" for c in data.get("code_changes_recommended") or ["(none)"]],
            "",
            "**Actions that require a human with the right access:**",
            *[f"- {c}" for c in data.get("human_actions_required") or []],
            "",
            "**Risks:**",
            *[f"- {c}" for c in data.get("risks") or []],
            "",
            "**How to verify:**",
            *[f"- {c}" for c in data.get("verification_steps") or []],
        ])
        return self._needs_human(ctx, NeedsHuman("investigation", reasons, recommendation), public_safe=True)

    # ------------------------------------------------- terminal transitions

    def _needs_human(self, ctx: ExecutionContext, exc: NeedsHuman, public_safe: bool = False) -> IssueOutcome:
        reason = "; ".join(exc.reasons)
        self.log.warning(f"#{ctx.issue.number} requires human intervention: {reason}")
        report_path = self._write_human_report(ctx, exc)
        hi = self.config.human_intervention
        labeled = 0
        if not self.no_pr:
            sensitive = ctx.issue.has_label("security") and not public_safe
            lines = [
                "🤖 **AI backlog agent: human intervention required**",
                "",
                f"The agent stopped at stage `{exc.stage}` and will not process this issue automatically.",
                "",
                "**Reasons:**",
                *[f"- {r}" for r in exc.reasons],
            ]
            if exc.recommendation and not sensitive:
                lines += ["", "**Recommendation:**", "", exc.recommendation]
            if exc.evidence and not sensitive:
                lines += ["", "**Evidence:**", "", exc.evidence]
            if sensitive:
                lines += ["", "_Details are security-sensitive and were stored privately by the agent operator._"]
            lines += ["", f"Remove the `{hi.label}` label to let the agent pick this issue up again."]
            try:
                if hi.post_comment:
                    self.gh.comment_on_issue(self.repository, ctx.issue.number, "\n".join(lines))
                if hi.apply_label:
                    if hi.create_label_if_missing:
                        self.gh.ensure_label(self.repository, hi.label, "d93f0b", "AI agent: needs human intervention")
                    self.gh.add_label(self.repository, ctx.issue.number, hi.label)
                    labeled = 1
                blocked = self.project.status_option(self.config.project_status.blocked)
                if hi.set_blocked_status_if_available and blocked:
                    self.project.set_status(ctx.issue, blocked.name)
                elif ctx.status_changed:
                    self.project.set_status(ctx.issue, self.config.project_status.backlog)
            except (GitHubError, SystemicError) as gh_exc:
                if isinstance(gh_exc, SystemicError):
                    raise
                self.log.warning(f"Could not fully mark #{ctx.issue.number} on GitHub: {gh_exc}")
        self._discard_or_keep(ctx)
        self.state.finish(ctx.execution_id, ExecutionStatus.NEEDS_HUMAN, failure_stage=exc.stage, failure_reason=reason,
                          recommended_action=f"Human action required; briefing: {report_path}", human_labeled=labeled)
        return self._outcome(ctx, ExecutionStatus.NEEDS_HUMAN, failure_stage=exc.stage, failure_reason=reason,
                             recommended_action=f"See {report_path}")

    def _write_human_report(self, ctx: ExecutionContext, exc: NeedsHuman) -> str:
        path = self.config.state_dir / "reports" / "human" / f"issue-{ctx.issue.number}-{ctx.execution_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# #{ctx.issue.number} {ctx.issue.title}\n\nStage: {exc.stage}\n\n## Reasons\n\n"
            + "\n".join(f"- {r}" for r in exc.reasons)
            + f"\n\n## Recommendation\n\n{exc.recommendation}\n\n## Evidence\n\n{exc.evidence}\n\n## Analysis\n\n```json\n"
            + json.dumps(ctx.analysis, indent=2) + "\n```\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return str(path)

    def _failed(self, ctx: ExecutionContext, stage: str, reason: str, recommended: str) -> IssueOutcome:
        self.log.error(f"#{ctx.issue.number} failed at {stage}: {reason}")
        if stage_at_least(ctx.stage, Stage.COMMITTED):
            # The verified commit exists (and may be pushed): keep everything and let --resume finish
            # push/PR/status instead of redoing the work (a retry would collide with its own remote branch).
            recommended = "Fix the cause (network/permissions) and run with --resume to finish push/PR/status."
            self.state.mark_interrupted(ctx.execution_id, f"{stage}: {reason}")
            self.state.update(ctx.execution_id, failure_stage=stage, recommended_action=recommended)
            return self._outcome(ctx, ExecutionStatus.FAILED, failure_stage=stage, failure_reason=reason,
                                 recommended_action=recommended)
        if ctx.status_changed and self.config.project_status.revert_to_backlog_on_failure and not stage_at_least(ctx.stage, Stage.PR_CREATED):
            try:
                self.project.set_status(ctx.issue, self.config.project_status.backlog)
            except GitHubError as exc:
                self.log.warning(f"Could not revert status of #{ctx.issue.number}: {exc}")
        kept = self._discard_or_keep(ctx)
        if kept:
            recommended = f"{recommended} Work kept in {kept}."
        self.state.finish(ctx.execution_id, ExecutionStatus.FAILED, failure_stage=stage, failure_reason=reason,
                          recommended_action=recommended)
        return self._outcome(ctx, ExecutionStatus.FAILED, failure_stage=stage, failure_reason=reason,
                             recommended_action=recommended)

    def _discard_or_keep(self, ctx: ExecutionContext) -> str | None:
        """Remove a pristine worktree (+ its empty branch); keep anything containing work."""
        if not ctx.worktree or not self.worktrees.find(ctx.worktree):
            return None
        try:
            if self.worktrees.discard_if_pristine(ctx.worktree, ctx.branch, ctx.base_sha):
                return None
        except AgentError as exc:
            self.log.warning(f"Could not clean up worktree: {exc}")
        self.log.warning(f"Keeping worktree with work in progress: {ctx.worktree} (branch {ctx.branch})")
        return str(ctx.worktree)

    def _cleanup(self, ctx: ExecutionContext) -> None:
        if ctx.worktree and self.worktrees.cleanup(ctx.worktree):
            self.log.debug(f"Removed worktree {ctx.worktree} (branch {ctx.branch} kept locally)")

    # -------------------------------------------------------------- helpers

    def _abandon(self, ctx: ExecutionContext, reason: str) -> IssueOutcome:
        self.log.warning(f"Could not claim #{ctx.issue.number}: {reason}; skipping")
        self.state.finish(ctx.execution_id, ExecutionStatus.ABANDONED, failure_stage="claim", failure_reason=reason)
        return self._outcome(ctx, ExecutionStatus.ABANDONED, failure_stage="claim", failure_reason=reason)

    def _stage(self, ctx: ExecutionContext, stage: Stage, message: str, **fields: Any) -> None:
        ctx.stage = stage.value
        self.state.set_stage(ctx.execution_id, stage, message, **fields)

    def _safe_comment(self, number: int, body: str) -> None:
        try:
            self.gh.comment_on_issue(self.repository, number, body)
        except GitHubError as exc:
            self.log.warning(f"Could not comment on #{number}: {exc}")

    def _outcome(self, ctx: ExecutionContext, status: ExecutionStatus, **kw: Any) -> IssueOutcome:
        return IssueOutcome(
            issue_number=ctx.issue.number, title=ctx.issue.title, status=status, execution_id=ctx.execution_id,
            pr_url=ctx.pr_url, pr_number=ctx.pr_number,
            quality_summary=format_report(ctx.quality).replace("\n", "; ") if ctx.quality else None,
            mode=ctx.mode.value, **kw,
        )

    def _outcome_from_record(self, execution_id: str, status: ExecutionStatus) -> IssueOutcome:
        record = self.state.get(execution_id) or {}
        return IssueOutcome(record.get("issue_number", 0), record.get("issue_title", ""), status, execution_id,
                            failure_reason=record.get("failure_reason"))
