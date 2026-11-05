"""Dry-run plan and end-of-run execution report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from engine import RunResult
from models import Eligibility, ExecutionStatus, SkipCategory

CATEGORY_LABELS = {
    SkipCategory.NEEDS_HUMAN: "requires human intervention",
    SkipCategory.BLOCKED: "blocked by dependencies",
    SkipCategory.EXISTING_PR: "existing pull request",
    SkipCategory.FAILED: "failed previously",
    SkipCategory.IN_PROGRESS: "in progress / interrupted",
    SkipCategory.COMPLETED: "already processed",
    SkipCategory.WRONG_STATUS: "not in Backlog",
    SkipCategory.EPIC: "epic / tracking issue",
    SkipCategory.INSUFFICIENT_INFO: "insufficient information",
    SkipCategory.CLOSED: "closed",
    SkipCategory.NOT_ISSUE: "not an issue",
    SkipCategory.OTHER_REPO: "other repository",
    SkipCategory.ATTEMPTED: "attempted this run",
}


def _deps(e: Eligibility) -> str:
    parts = [f"#{b.number}{'' if b.resolved else '*'}" for b in e.issue.blocked_by]
    return ",".join(parts) or "-"


def dry_run_report(evaluations: list[Eligibility], schema_info: dict[str, Any], ordered: list[Eligibility],
                   interrupted: list[dict[str, Any]]) -> str:
    lines = ["", "=" * 100, "DRY RUN - no files, branches, commits, PRs or project fields will be changed", "=" * 100, ""]
    lines.append(f"Project: {schema_info['title']} ({schema_info['url']})")
    lines.append(f"Repository: {schema_info['repository']}   Base branch: {schema_info['base_branch']}")
    lines.append(f"Status mapping: {schema_info['status_mapping']}")
    for warning in schema_info.get("warnings", []):
        lines.append(f"WARNING: {warning}")
    lines.append("")
    lines.append(f"{'#':>4}  {'Pri':<4} {'Status':<12} {'Mode/Skip':<22} {'Blocked by':<12} {'Labels':<44} Title")
    lines.append("-" * 140)
    order_index = {e.issue.number: i for i, e in enumerate(ordered, 1)}
    for e in sorted(evaluations, key=lambda x: (not x.eligible, order_index.get(x.issue.number, 999), x.issue.number)):
        tag = f"#{order_index[e.issue.number]} {e.mode.value}" if e.eligible else (e.category.value if e.category else "skip")
        labels = ",".join(e.issue.labels)[:44]
        lines.append(f"{e.issue.number:>4}  {(e.issue.priority or '-'):<4} {(e.issue.status or '-')[:12]:<12} {tag[:22]:<22} "
                     f"{_deps(e)[:12]:<12} {labels:<44} {e.issue.title[:60]}")
    lines.append("")
    lines.append("(* = unresolved dependency)")
    lines.append("")
    lines.append(f"PROPOSED EXECUTION ORDER ({len(ordered)} eligible):")
    for i, e in enumerate(ordered, 1):
        note = f" - {'; '.join(e.reasons)}" if e.reasons else ""
        lines.append(f"  {i:>2}. #{e.issue.number} [{e.issue.priority or '-'}] ({e.mode.value}) {e.issue.title[:80]}{note}")
    lines.append("")
    lines.append("SKIPPED ISSUES:")
    groups: dict[str, list[Eligibility]] = defaultdict(list)
    for e in evaluations:
        if not e.eligible:
            groups[CATEGORY_LABELS.get(e.category, "other")].append(e)
    for label, items in sorted(groups.items()):
        lines.append(f"  {label} ({len(items)}):")
        for e in items:
            lines.append(f"    - #{e.issue.number} {e.issue.title[:70]}: {'; '.join(e.reasons)}")
    if interrupted:
        lines.append("")
        lines.append("INTERRUPTED EXECUTIONS (resume with --resume):")
        for r in interrupted:
            lines.append(f"  - #{r['issue_number']} {r['execution_id']} at stage {r.get('stage')}: {r.get('failure_reason') or ''}")
    lines.append("")
    lines.append("Note: investigate-mode issues are only analysed and documented for a human; they never get code changes.")
    return "\n".join(lines)


def build_summary(result: RunResult, run_id: str) -> dict[str, Any]:
    outcomes = result.outcomes
    final = result.final_evaluations or result.initial_evaluations
    cat = Counter(e.category for e in final if not e.eligible)
    created = [o for o in outcomes if o.status == ExecutionStatus.SUCCEEDED and o.pr_url]
    return {
        "run_id": run_id,
        "stopped_reason": result.stopped_reason,
        "systemic_error": result.systemic_error,
        "totals": {
            "issues_discovered": len(result.initial_issues),
            "eligible_at_start": sum(1 for e in result.initial_evaluations if e.eligible),
            "attempted": len(outcomes),
            "succeeded": sum(1 for o in outcomes if o.status == ExecutionStatus.SUCCEEDED),
            "draft_prs_created": len(created),
            "failed": sum(1 for o in outcomes if o.status == ExecutionStatus.FAILED),
            "needs_human_this_run": sum(1 for o in outcomes if o.status == ExecutionStatus.NEEDS_HUMAN),
            "skipped_claim": sum(1 for o in outcomes if o.status == ExecutionStatus.ABANDONED),
            "requires_human_total": cat.get(SkipCategory.NEEDS_HUMAN, 0),
            "blocked_by_dependencies": cat.get(SkipCategory.BLOCKED, 0),
            "with_existing_prs": cat.get(SkipCategory.EXISTING_PR, 0),
            "remaining_eligible": sum(1 for e in final if e.eligible),
        },
        "pull_requests": [
            {"issue": o.issue_number, "title": o.title, "pr_number": o.pr_number, "pr_url": o.pr_url,
             "quality": o.quality_summary} for o in created
        ],
        "failures": [
            {"issue": o.issue_number, "title": o.title, "stage": o.failure_stage, "reason": o.failure_reason,
             "next_action": o.recommended_action} for o in outcomes if o.status == ExecutionStatus.FAILED
        ],
        "needs_human": [
            {"issue": o.issue_number, "title": o.title, "stage": o.failure_stage, "reason": o.failure_reason,
             "next_action": o.recommended_action} for o in outcomes if o.status == ExecutionStatus.NEEDS_HUMAN
        ],
        "backlog_status": [
            {"issue": e.issue.number, "title": e.issue.title, "category": e.category.value if e.category else None,
             "eligible": e.eligible, "reasons": e.reasons}
            for e in final if e.eligible or e.category not in (SkipCategory.CLOSED, SkipCategory.NOT_ISSUE)
        ],
    }


def render_summary(summary: dict[str, Any]) -> str:
    t = summary["totals"]
    out = ["", "=" * 80, "## Execution Summary", "=" * 80, ""]
    out += [
        f"- Run ID: {summary['run_id']}",
        f"- Stopped because: {summary['stopped_reason']}",
    ]
    if summary["systemic_error"]:
        out.append(f"- SYSTEMIC ERROR: {summary['systemic_error']}")
    out += [
        f"- Total issues discovered: {t['issues_discovered']}",
        f"- Eligible at start: {t['eligible_at_start']}",
        f"- Attempted this run: {t['attempted']}",
        f"- Successfully processed: {t['succeeded']}",
        f"- Draft PRs created: {t['draft_prs_created']}",
        f"- Failed: {t['failed']}",
        f"- Flagged for human intervention this run: {t['needs_human_this_run']}",
        f"- Skipped (claim lost/race): {t['skipped_claim']}",
        f"- Issues requiring human intervention (project-wide): {t['requires_human_total']}",
        f"- Issues blocked by dependencies: {t['blocked_by_dependencies']}",
        f"- Issues with existing PRs: {t['with_existing_prs']}",
        f"- Remaining eligible backlog: {t['remaining_eligible']}",
        "",
        "## Pull Requests",
        "",
    ]
    if summary["pull_requests"]:
        for pr in summary["pull_requests"]:
            out.append(f"- #{pr['issue']} {pr['title']}\n  PR #{pr['pr_number']}: {pr['pr_url']}\n  Quality: {pr['quality']}")
    else:
        out.append("- (none)")
    out += ["", "## Failed Issues", ""]
    if summary["failures"]:
        for f in summary["failures"]:
            out.append(f"- #{f['issue']} {f['title']}\n  Stage: {f['stage']}\n  Reason: {f['reason']}\n  Next action: {f['next_action']}")
    else:
        out.append("- (none)")
    if summary["needs_human"]:
        out += ["", "## Human Intervention Required (this run)", ""]
        for f in summary["needs_human"]:
            out.append(f"- #{f['issue']} {f['title']} ({f['stage']}): {f['reason']}\n  {f['next_action']}")
    out += ["", "## Backlog Status (verified against the project after the run)", ""]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in summary["backlog_status"]:
        key = "eligible (not yet processed)" if item["eligible"] else CATEGORY_LABELS.get(SkipCategory(item["category"]), item["category"])
        groups[key].append(item)
    for key, items in sorted(groups.items()):
        out.append(f"- {key}: {len(items)}")
        for item in items[:60]:
            out.append(f"    #{item['issue']} {item['title'][:70]} - {'; '.join(item['reasons'])[:140]}")
    return "\n".join(out)


def save_report(state_dir: Path, run_id: str, summary: dict[str, Any], text: str) -> Path:
    directory = state_dir / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"run-{run_id}.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    path = directory / f"run-{run_id}.md"
    path.write_text(text, encoding="utf-8")
    return path
