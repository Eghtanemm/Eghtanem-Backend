"""Eligibility rules and priority ordering (pure logic, no I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from models import Eligibility, ExecutionStatus, Issue, ProcessingMode, SkipCategory
from safety import human_required_reasons


@dataclass
class SelectionContext:
    repository: str
    executions: dict[int, dict[str, Any]] = field(default_factory=dict)  # latest execution per issue
    attempts: dict[int, int] = field(default_factory=dict)  # failed attempts per issue (all runs)
    locked: set[int] = field(default_factory=set)  # issues locked by a live execution
    attempted_this_run: set[int] = field(default_factory=set)
    retry_failed: bool = False


def acceptance_criteria_count(body: str) -> int:
    section = re.search(r"(?ims)^##+\s*acceptance criteria\s*$(.*?)(?=^##+\s|\Z)", body or "")
    if not section:
        return 0
    return len(re.findall(r"(?m)^\s*[-*]\s*(\[[ xX]\]\s*)?\S", section.group(1)))


class IssueSelector:
    def __init__(self, config):
        self.config = config
        self.sel = config.selection
        self.status_cfg = config.project_status

    # ------------------------------------------------------------ eligibility

    def evaluate(self, issue: Issue, ctx: SelectionContext) -> Eligibility:
        def skip(category: SkipCategory, *reasons: str) -> Eligibility:
            return Eligibility(issue, False, None, category, list(reasons))

        if issue.content_type != "Issue":
            return skip(SkipCategory.NOT_ISSUE, f"project item is a {issue.content_type}, not an issue")
        if issue.repository.lower() != ctx.repository.lower():
            return skip(SkipCategory.OTHER_REPO, f"belongs to {issue.repository}")
        if not issue.is_open:
            return skip(SkipCategory.CLOSED, "issue is closed")
        if issue.number in ctx.attempted_this_run:
            return skip(SkipCategory.ATTEMPTED, "already attempted in this run")
        if self.sel.skip_existing_prs and issue.active_prs:
            prs = ", ".join(f"#{p.number}" for p in issue.active_prs)
            return skip(SkipCategory.EXISTING_PR, f"has active pull request(s): {prs}")
        if self.sel.require_backlog_status and not _same(issue.status, self.status_cfg.backlog):
            return skip(SkipCategory.WRONG_STATUS, f"status is '{issue.status or 'unset'}', not '{self.status_cfg.backlog}'")
        if issue.number in ctx.locked:
            return skip(SkipCategory.IN_PROGRESS, "currently being processed by another execution")

        last = ctx.executions.get(issue.number)
        if last:
            status = last.get("status")
            if status == ExecutionStatus.RUNNING.value:
                return skip(SkipCategory.IN_PROGRESS, f"execution {last['execution_id']} is marked running (use --resume)")
            if status == ExecutionStatus.INTERRUPTED.value:
                return skip(SkipCategory.IN_PROGRESS, f"execution {last['execution_id']} was interrupted (use --resume)")
            if status == ExecutionStatus.SUCCEEDED.value:
                where = last.get("pr_url") or f"local branch {last.get('branch')} (--no-pr)"
                return skip(SkipCategory.COMPLETED, f"already processed: {where}")
            if status == ExecutionStatus.NEEDS_HUMAN.value:
                return skip(SkipCategory.NEEDS_HUMAN, f"previously flagged for human intervention: {last.get('failure_reason') or ''}".strip())
            if status == ExecutionStatus.FAILED.value:
                attempts = ctx.attempts.get(issue.number, 0)
                limit = self.config.execution.max_attempts_per_issue
                if not ctx.retry_failed:
                    return skip(SkipCategory.FAILED, f"previous attempt failed at {last.get('failure_stage')}: "
                                f"{(last.get('failure_reason') or '')[:160]} (use --retry-failed)")
                if attempts >= limit:
                    return skip(SkipCategory.FAILED, f"retry limit reached ({attempts}/{limit} failed attempts)")

        if any(issue.has_label(l) for l in self.sel.human_required_labels + [self.config.human_intervention.label]):
            return skip(SkipCategory.NEEDS_HUMAN, "labelled for human intervention")

        if self.config.selection.skip_blocked_issues and issue.unresolved_blockers:
            blockers = ", ".join(f"#{b.number}" + ("" if b.state != "UNKNOWN" else " (state unknown)")
                                 for b in issue.unresolved_blockers)
            return skip(SkipCategory.BLOCKED, f"blocked by unresolved {blockers}")
        if self.sel.skip_epics and issue.open_sub_issues:
            return skip(SkipCategory.EPIC, f"epic/tracking issue with {len(issue.open_sub_issues)} open sub-issue(s)")
        if len((issue.body or "").strip()) < self.sel.min_body_chars:
            return skip(SkipCategory.INSUFFICIENT_INFO, "issue description too short to implement safely")

        reasons = human_required_reasons(
            issue.title, issue.body, issue.labels, self.config.safety.human_required_patterns, []
        )
        reasons += [f"labelled '{label}': production infrastructure/external services need a human"
                    for label in self.sel.investigate_only_labels if issue.has_label(label)]
        if reasons:
            if not self.sel.investigate_human_required:
                return skip(SkipCategory.NEEDS_HUMAN, *reasons)
            mode = ProcessingMode.INVESTIGATE
        elif issue.has_label(self.sel.needs_verification_label):
            mode = ProcessingMode.VERIFY
            reasons = ["needs verification before any fix"]
        else:
            mode = ProcessingMode.IMPLEMENT

        return Eligibility(issue, True, mode, None, reasons, self.sort_key(issue))

    # --------------------------------------------------------------- ordering

    def sort_key(self, issue: Issue) -> tuple:
        priorities = [p.upper() for p in self.sel.priorities]
        prio_rank = priorities.index(issue.priority) if issue.priority in priorities else len(priorities)
        sizes = [s.upper() for s in self.sel.size_order]
        size = (issue.size or "").upper()
        size_rank = sizes.index(size) if size in sizes else len(sizes) // 2  # unknown size ~ medium
        dependency_count = len(issue.blocked_by) + len(issue.related)
        has_criteria = 0 if acceptance_criteria_count(issue.body) > 0 else 1
        return (prio_rank, dependency_count, size_rank, has_criteria, len(issue.body or ""), issue.number)

    def evaluate_all(self, issues: list[Issue], ctx: SelectionContext) -> list[Eligibility]:
        return [self.evaluate(issue, ctx) for issue in issues]

    def ordered_eligible(self, evaluations: list[Eligibility]) -> list[Eligibility]:
        return sorted((e for e in evaluations if e.eligible), key=lambda e: e.sort_key)

    def select_next(self, issues: list[Issue], ctx: SelectionContext) -> tuple[Eligibility | None, list[Eligibility]]:
        evaluations = self.evaluate_all(issues, ctx)
        ordered = self.ordered_eligible(evaluations)
        return (ordered[0] if ordered else None), evaluations


def _same(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").lower().split()) == " ".join((b or "").lower().split())
