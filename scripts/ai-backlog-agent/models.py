"""Shared data structures and the exception hierarchy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class AgentError(Exception):
    """Base class for all agent errors."""


class SystemicError(AgentError):
    """Stops the whole batch: continuing could be unsafe or pointless."""


class AuthError(SystemicError):
    pass


class RepositoryIntegrityError(SystemicError):
    pass


class UnsafeEnvironmentError(SystemicError):
    pass


class CredentialExposureError(SystemicError):
    pass


class UsageLimitError(SystemicError):
    """Claude subscription usage / rate limit reached; every next issue would fail too."""


class IssueFailure(AgentError):
    """Isolated failure of one issue; the batch continues with the next issue."""

    def __init__(self, stage: str, reason: str, recommended_action: str = ""):
        super().__init__(f"[{stage}] {reason}")
        self.stage = stage
        self.reason = reason
        self.recommended_action = recommended_action


class NeedsHuman(AgentError):
    """The issue must be handled by a person; the agent documents why and moves on."""

    def __init__(self, stage: str, reasons: list[str], recommendation: str = "", evidence: str = ""):
        super().__init__(f"[{stage}] human intervention required: {'; '.join(reasons)}")
        self.stage = stage
        self.reasons = reasons
        self.recommendation = recommendation
        self.evidence = evidence


class ClaimFailed(AgentError):
    """Issue could not be claimed (became ineligible or is processed elsewhere)."""


class GitHubError(AgentError):
    pass


# --------------------------------------------------------------------------
# GitHub Project data
# --------------------------------------------------------------------------


@dataclass
class FieldOption:
    id: str
    name: str


@dataclass
class ProjectField:
    id: str
    name: str
    data_type: str
    options: list[FieldOption] = field(default_factory=list)

    def option(self, name: str | None) -> FieldOption | None:
        """Case/whitespace-insensitive lookup (project uses 'In progress', config may say 'In Progress')."""
        if not name:
            return None
        wanted = " ".join(name.lower().split())
        for opt in self.options:
            if " ".join(opt.name.lower().split()) == wanted:
                return opt
        return None


@dataclass
class ProjectSchema:
    id: str
    number: int
    title: str
    description: str
    url: str
    fields: dict[str, ProjectField]

    def field(self, name: str | None) -> ProjectField | None:
        if not name:
            return None
        for key, value in self.fields.items():
            if key.lower() == name.lower():
                return value
        return None


@dataclass
class PullRef:
    number: int
    url: str
    state: str  # OPEN / CLOSED / MERGED
    is_draft: bool = False
    head_ref: str = ""

    @property
    def active(self) -> bool:
        return self.state.upper() == "OPEN"


@dataclass
class IssueRef:
    number: int
    state: str  # OPEN / CLOSED / UNKNOWN
    repository: str = ""
    source: str = ""  # "native" | "body"

    @property
    def resolved(self) -> bool:
        return self.state.upper() == "CLOSED"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str
    state: str
    repository: str
    item_id: str
    status: str | None
    labels: list[str] = field(default_factory=list)
    priority: str | None = None
    size: str | None = None
    blocked_by: list[IssueRef] = field(default_factory=list)
    related: list[int] = field(default_factory=list)
    sub_issues: list[IssueRef] = field(default_factory=list)
    parent: int | None = None
    linked_prs: list[PullRef] = field(default_factory=list)
    content_type: str = "Issue"
    field_values: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"

    @property
    def active_prs(self) -> list[PullRef]:
        return [pr for pr in self.linked_prs if pr.active]

    @property
    def unresolved_blockers(self) -> list[IssueRef]:
        return [b for b in self.blocked_by if not b.resolved]

    @property
    def open_sub_issues(self) -> list[IssueRef]:
        return [s for s in self.sub_issues if not s.resolved]

    def has_label(self, name: str) -> bool:
        return name.lower() in (label.lower() for label in self.labels)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


class ProcessingMode(str, Enum):
    IMPLEMENT = "implement"
    VERIFY = "verify"  # needs-verification: investigate first, then implement if verified
    INVESTIGATE = "investigate"  # human-required: investigate + document only, never implement


class SkipCategory(str, Enum):
    NOT_ISSUE = "not_issue"
    CLOSED = "closed"
    WRONG_STATUS = "wrong_status"
    EXISTING_PR = "existing_pr"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    BLOCKED = "blocked"
    EPIC = "epic"
    INSUFFICIENT_INFO = "insufficient_info"
    OTHER_REPO = "other_repository"
    ATTEMPTED = "attempted_this_run"


@dataclass
class Eligibility:
    issue: Issue
    eligible: bool
    mode: ProcessingMode | None = None
    category: SkipCategory | None = None
    reasons: list[str] = field(default_factory=list)
    sort_key: tuple = ()


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


class Stage(str, Enum):
    CLAIMED = "claimed"
    WORKSPACE_READY = "workspace_ready"
    BASELINE_DONE = "baseline_done"
    ANALYZED = "analyzed"
    PLANNED = "planned"
    IMPLEMENTED = "implemented"
    QUALITY_PASSED = "quality_passed"
    REVIEWED = "reviewed"
    COMMITTED = "committed"
    PUSHED = "pushed"
    PR_CREATED = "pr_created"
    PROJECT_UPDATED = "project_updated"
    COMPLETED = "completed"


STAGE_ORDER = [s for s in Stage]


def stage_at_least(current: str | None, target: Stage) -> bool:
    if not current:
        return False
    try:
        return STAGE_ORDER.index(Stage(current)) >= STAGE_ORDER.index(target)
    except ValueError:
        return False


class ExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"


class GateStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"  # not applicable / not configured (never counted as passed)
    UNAVAILABLE = "unavailable"  # could not run (missing service/dependency)


@dataclass
class GateResult:
    name: str
    status: GateStatus
    summary: str
    command: str = ""
    details: str = ""
    duration: float = 0.0
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "command": self.command,
            "details": self.details[-6000:],
            "duration": round(self.duration, 1),
            "required": self.required,
        }


@dataclass
class QualityReport:
    results: list[GateResult]

    @property
    def blocking(self) -> list[GateResult]:
        return [r for r in self.results if r.required and r.status in (GateStatus.FAILED, GateStatus.UNAVAILABLE)]

    @property
    def passed(self) -> bool:
        return not self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "results": [r.to_dict() for r in self.results]}


@dataclass
class ReviewFinding:
    severity: str
    title: str
    description: str = ""
    file: str = ""
    line: int | None = None
    suggestion: str = ""


@dataclass
class ReviewResult:
    verdict: str
    findings: list[ReviewFinding]
    summary: str
    fully_resolves_issue: bool
    blocking_severities: tuple[str, ...] = ("critical", "high")
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking_findings(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity.lower() in self.blocking_severities]

    @property
    def passed(self) -> bool:
        return not self.blocking_findings and self.verdict.lower() != "reject"


@dataclass
class ClaudeResult:
    stage: str
    session_id: str | None
    text: str
    structured: dict[str, Any] | None
    is_error: bool
    subtype: str
    num_turns: int
    cost_usd: float
    permission_denials: list[dict[str, Any]]
    duration: float
    log_path: str = ""


@dataclass
class IssueOutcome:
    issue_number: int
    title: str
    status: ExecutionStatus
    execution_id: str
    pr_url: str | None = None
    pr_number: int | None = None
    failure_stage: str | None = None
    failure_reason: str | None = None
    recommended_action: str | None = None
    quality_summary: str | None = None
    mode: str | None = None
