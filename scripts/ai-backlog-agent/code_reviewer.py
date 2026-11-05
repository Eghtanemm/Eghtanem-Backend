"""Independent review stage: a fresh Claude session that only sees the issue, the
actual diff and the executed gate results (the implementer's report is passed
as claims to verify, never as truth)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import schemas
from claude_runner import ClaudeRunner, render, tests_policy
from models import Issue, ReviewFinding, ReviewResult


def parse_review(data: dict[str, Any], blocking_severities: list[str]) -> ReviewResult:
    findings = []
    for f in data.get("findings") or []:
        findings.append(ReviewFinding(
            severity=str(f.get("severity", "low")).lower(),
            title=f.get("title", ""),
            description=f.get("description", ""),
            file=f.get("file", "") or "",
            line=f.get("line"),
            suggestion=f.get("suggestion", "") or "",
        ))
    return ReviewResult(
        verdict=str(data.get("verdict", "request_changes")).lower(),
        findings=findings,
        summary=data.get("summary", ""),
        fully_resolves_issue=bool(data.get("fully_resolves_issue")),
        blocking_severities=tuple(s.lower() for s in blocking_severities),
        raw=data,
    )


def format_findings(findings: list[ReviewFinding]) -> str:
    lines = []
    for i, f in enumerate(findings, 1):
        loc = f" ({f.file}{':' + str(f.line) if f.line else ''})" if f.file else ""
        lines.append(f"{i}. [{f.severity.upper()}] {f.title}{loc}\n   {f.description}"
                     + (f"\n   Suggestion: {f.suggestion}" if f.suggestion else ""))
    return "\n".join(lines) or "(none)"


class CodeReviewer:
    def __init__(self, claude: ClaudeRunner, config):
        self.claude = claude
        self.config = config

    def review(self, issue: Issue, worktree: Path, base_sha: str, diff: str, implementation: dict[str, Any],
               quality_summary: str, execution_id: str) -> tuple[ReviewResult, str | None]:
        prompt = render("review.md", {
            "issue_number": str(issue.number),
            "issue_title": issue.title,
            "issue_body": issue.body,
            "implementation_report": json.dumps(implementation, indent=2),
            "quality_summary": quality_summary,
            "base_sha": base_sha,
            "diff": diff,
            "tests_policy": tests_policy(self.config.claude.write_tests, "review"),
        })
        result = self.claude.run("review", prompt, worktree, schema=schemas.REVIEW,
                                 max_turns=self.config.claude.review_max_turns, execution_id=execution_id)
        return parse_review(result.structured or {}, self.config.quality_gates.blocking_review_severities), result.session_id
