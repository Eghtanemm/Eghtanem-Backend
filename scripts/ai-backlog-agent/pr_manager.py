"""Pull request creation (idempotent) and PR description rendering. Never merges."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from code_reviewer import format_findings
from github_client import GitHubClient
from models import GateStatus, GitHubError, Issue, QualityReport, ReviewResult
from safety import ChangedFile, assert_safe_branch

MAX_BODY = 60_000


@dataclass
class PullRequestInfo:
    number: int
    url: str
    is_draft: bool
    reused: bool = False


def short_description(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split()).strip().rstrip(".")
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text or "automated fix"


def commit_message(issue_number: int, summary: str) -> str:
    desc = short_description(summary)
    desc = desc[0].lower() + desc[1:] if desc and not desc[:2].isupper() else desc
    return f"fix(#{issue_number}): {desc}"


def pr_title(issue_number: int, summary: str, fully_resolved: bool, max_len: int = 72) -> str:
    prefix = f"[AI] Fix #{issue_number} - " if fully_resolved else f"[AI] Partially address #{issue_number} - "
    return prefix + short_description(summary, max(20, max_len - len(prefix)))


def post_merge_actions(*reports: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for report in reports:
        for action in (report or {}).get("post_merge_human_actions") or []:
            if action and action not in seen:
                seen.append(action)
    return seen


def is_fully_resolved(plan: dict[str, Any], implementation: dict[str, Any], review: ReviewResult | None,
                      analysis: dict[str, Any] | None = None) -> bool:
    if post_merge_actions(analysis or {}, plan, implementation):
        return False  # a person still has to act after merge: reference the issue, don't close it
    return bool(
        plan.get("fully_resolves_issue")
        and implementation.get("fully_resolves_issue")
        and implementation.get("status") == "completed"
        and (review is None or review.fully_resolves_issue)
    )


def _bullets(items: list[str] | None, empty: str = "_None._") -> str:
    items = [i for i in (items or []) if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else empty


def render_body(issue: Issue, analysis: dict[str, Any], plan: dict[str, Any], implementation: dict[str, Any],
                files: list[ChangedFile], quality: QualityReport, review: ReviewResult | None,
                safety_warnings: list[str], fully_resolved: bool, unverified: bool, close_only_when_full: bool,
                short: bool = False) -> str:
    follow_up = post_merge_actions(analysis, plan, implementation)
    closes = fully_resolved or not close_only_when_full
    link_line = f"Closes #{issue.number}" if closes else f"Refs #{issue.number} (partial: this PR does **not** close the issue)"
    if short:
        return render_short_body(link_line, analysis, plan, implementation, files, quality, review, follow_up, unverified)

    gate_lines = []
    for r in quality.results:
        icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️", "unavailable": "⚠️"}[r.status.value]
        gate_lines.append(f"| {r.name} | {icon} {r.status.value} | {r.summary} |")
    review_line = "| independent review | ⏭️ disabled | — |"
    if review:
        counts = {s: sum(1 for f in review.findings if f.severity == s) for s in ("critical", "high", "medium", "low")}
        review_line = (f"| independent review | {'✅' if review.passed else '❌'} {review.verdict} | "
                       f"critical {counts['critical']}, high {counts['high']}, medium {counts['medium']}, low {counts['low']} |")
    security_line = ("| deterministic safety scan | ✅ passed | no secrets, forbidden paths, edited migrations, "
                     "destructive migration ops or disabled tests |")

    testing = []
    for r in quality.results:
        if r.command:
            testing.append(f"- `{r.command}` → **{r.status.value}**: {r.summary}")
    tests_added = implementation.get("tests_added") or []

    files_md = "\n".join(f"- `{f.path}` ({f.status[0]}, +{f.added}/-{f.deleted})" for f in files[:60])
    if len(files) > 60:
        files_md += f"\n- … and {len(files) - 60} more"

    non_blocking = [f for f in (review.findings if review else []) if f.severity not in review.blocking_severities] if review else []
    risks = list(plan.get("risks") or []) + list(implementation.get("potential_risks") or [])
    limitations = list(implementation.get("known_limitations") or [])
    if not fully_resolved:
        limitations.insert(0, "This PR only partially resolves the issue; unaddressed: "
                           + ", ".join(plan.get("unaddressed_criteria") or ["see limitations"]))
    if unverified:
        limitations.insert(0, "⚠️ Some quality gates could not run in the agent environment; this change is **UNVERIFIED**.")

    body = f"""{link_line}

## Summary

{analysis.get('summary') or analysis.get('problem') or issue.title}

**Audit reference:** {analysis.get('audit_ref') or 'n/a'} · **Priority:** {issue.priority or 'n/a'} · **Verification:** {(analysis.get('verification') or {}).get('status', 'n/a')}

## Root Cause

{implementation.get('root_cause') or plan.get('root_cause') or analysis.get('root_cause')}

## Implementation

{plan.get('proposed_solution', '')}

{_bullets(implementation.get('changes_made'))}

## Files Changed

{files_md}

## Testing

Commands actually executed by the orchestrator (PHP runs in an isolated container, SQLite in-memory, no network):

{chr(10).join(testing) or '_No commands executed._'}

Tests added:

{_bullets(tests_added)}

## Quality Gates

| Gate | Result | Details |
|------|--------|---------|
{chr(10).join(gate_lines)}
{security_line}
{review_line}

Pre-existing failures on the base branch are excluded from the PHPUnit gate; only *new* failures block.

## Independent Review

{review.summary if review else '_Review disabled in configuration._'}

{('Non-blocking findings:' + chr(10) + chr(10) + format_findings(non_blocking)) if non_blocking else ''}

## Risks and Limitations

{_bullets(risks + limitations + safety_warnings)}

{('## Required Human Follow-up' + chr(10) + chr(10) + 'This PR does not close the issue. After merging, a maintainer must:' + chr(10) + chr(10) + _bullets(follow_up) + chr(10)) if follow_up else ''}
## Database Changes

{implementation.get('database_changes') or plan.get('database_changes') or 'None.'}

Rollback: {plan.get('rollback') or 'Revert this PR.'}

API compatibility: {plan.get('api_compatibility') or 'n/a'}

## AI Disclosure

This pull request was prepared by an AI-assisted implementation workflow (Claude Code driven by
`scripts/ai-backlog-agent`): analysis, planning, implementation and an independent AI review, plus
automated quality gates. **It requires careful human review before merging.** It was not merged
automatically and must not be merged without a maintainer's approval.
"""
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n_[description truncated]_"
    return body


def render_short_body(link_line: str, analysis: dict[str, Any], plan: dict[str, Any], implementation: dict[str, Any],
                      files: list[ChangedFile], quality: QualityReport, review: ReviewResult | None,
                      follow_up: list[str], unverified: bool) -> str:
    """Compact description: what/why, changes, files, check results, follow-up."""
    summary = analysis.get("summary") or analysis.get("problem") or plan.get("root_cause") or ""
    summary = summary.split(". ")[0].rstrip(".") + "." if summary else ""
    checks = []
    for r in quality.results:
        if r.status.value == "skipped":
            continue
        icon = {"passed": "✅", "failed": "❌", "unavailable": "⚠️"}[r.status.value]
        checks.append(f"{icon} {r.name}: {r.summary}")
    if review:
        checks.append(f"{'✅' if review.passed else '❌'} AI review: {review.verdict}")
    parts = [link_line, "", summary, "", "**Changes**", _bullets((implementation.get("changes_made") or [])[:6]),
             "", "**Files:** " + ", ".join(f"`{f.path}`" for f in files[:15]) + (" …" if len(files) > 15 else ""),
             "", "**Checks:** " + " · ".join(checks)]
    if unverified:
        parts += ["", "⚠️ Some checks could not run; this change is **unverified**."]
    if follow_up:
        parts += ["", "**Required after merge (human):**", _bullets(follow_up)]
    parts += ["", "_Prepared by the AI backlog agent (Claude Code); needs human review before merging._"]
    return "\n".join(parts)


class PullRequestManager:
    def __init__(self, gh: GitHubClient, repository: str, base_branch: str, protected: list[str], logger=None,
                 draft: bool = False):
        self.draft = draft
        self.gh = gh
        self.repository = repository
        self.base_branch = base_branch
        self.protected = protected
        self.logger = logger

    def find_open(self, branch: str) -> PullRequestInfo | None:
        result = self.gh.gh(["pr", "list", "-R", self.repository, "--head", branch, "--state", "open",
                             "--json", "number,url,isDraft"])
        items = json.loads(result.stdout or "[]")
        if items:
            return PullRequestInfo(items[0]["number"], items[0]["url"], items[0]["isDraft"], reused=True)
        return None

    def create(self, issue_number: int, branch: str, title: str, body: str) -> PullRequestInfo:
        assert_safe_branch(branch, self.protected, issue_number)
        existing = self.find_open(branch)
        if existing:
            if self.logger:
                self.logger.info(f"PR already exists for {branch}: {existing.url} (not creating a duplicate)")
            return existing
        args = ["pr", "create", "-R", self.repository, "--base", self.base_branch,
                "--head", branch, "--title", title, "--body-file", "-"]
        if self.draft:
            args.insert(4, "--draft")
        result = self.gh.gh(args, input_text=body, retry=False)
        url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        info = self.verify(url or branch, branch)
        return info

    def verify(self, ref: str, branch: str) -> PullRequestInfo:
        result = self.gh.gh(["pr", "view", ref, "-R", self.repository, "--json",
                             "number,url,isDraft,state,headRefName,baseRefName"])
        data = json.loads(result.stdout)
        if data.get("headRefName") != branch or data.get("baseRefName") != self.base_branch:
            raise GitHubError(f"PR {data.get('url')} has unexpected head/base ({data.get('headRefName')} -> {data.get('baseRefName')})")
        if data.get("state") != "OPEN":
            raise GitHubError(f"PR {data.get('url')} is not open")
        if self.draft and not data.get("isDraft"):
            if self.logger:
                self.logger.warning(f"PR {data.get('url')} is not a draft; converting it to draft")
            self.gh.gh(["pr", "ready", str(data["number"]), "-R", self.repository, "--undo"])
        return PullRequestInfo(data["number"], data["url"], bool(data.get("isDraft")) or self.draft)

    def unverified(self, quality: QualityReport) -> bool:
        return any(r.status == GateStatus.UNAVAILABLE for r in quality.results)
