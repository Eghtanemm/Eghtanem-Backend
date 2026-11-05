"""Configuration loading and validation.

Defaults live in the dataclasses below; ``config.yaml`` (or the file passed via
``--config``) overrides them section by section. Unknown keys are rejected so a
typo can never silently disable a safety setting.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent


class ConfigError(Exception):
    pass


@dataclass
class GitHubConfig:
    organization: str = "Eghtanemm"
    project_number: int = 1
    repository: str = "auto"  # "owner/name" or auto (from git remote origin)
    base_branch: str = "auto"  # branch name or auto (repository default branch)
    remote: str = "origin"
    page_size: int = 50
    max_retries: int = 5


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    model: str | None = None  # None = Claude Code's configured default
    effort: str | None = None
    max_turns: int = 60
    analysis_max_turns: int = 30
    review_max_turns: int = 30
    timeout: int = 1800  # seconds per Claude invocation
    max_repair_attempts: int = 2
    write_tests: bool = False  # false = Claude must not add/modify files under tests/ (tests deferred)
    restricted: bool = True  # --restricted: file tools confined to the worktree
    resume_sessions: bool = True  # analysis -> plan -> implement share one session


@dataclass
class ExecutionConfig:
    mode: str = "sequential"
    default_limit: int | None = None
    continue_after_isolated_failure: bool = True
    stop_on_systemic_failure: bool = True
    allow_parallel: bool = False
    resume_enabled: bool = True
    max_attempts_per_issue: int = 2  # across runs, including --retry-failed
    worktree_root: str = "~/.cache/ai-backlog-agent/worktrees"
    state_dir: str = "state"  # relative to the agent directory
    claim_comment: bool = False


@dataclass
class SelectionConfig:
    priorities: list[str] = field(default_factory=lambda: ["P0", "P1", "P2", "P3"])
    priority_field: str = "Priority"
    priority_label_pattern: str = r"^(?:priority[:/ _-]?)?(P\d)$"
    size_field: str = "Size"
    size_order: list[str] = field(default_factory=lambda: ["XS", "S", "M", "L", "XL"])
    require_backlog_status: bool = True
    skip_existing_prs: bool = True
    skip_blocked_issues: bool = True
    skip_epics: bool = True
    min_body_chars: int = 80
    needs_verification_label: str = "needs-verification"
    human_required_labels: list[str] = field(
        default_factory=lambda: ["needs-human", "ai:needs-human", "human-required", "do-not-automate"]
    )
    investigate_human_required: bool = True
    # Labels whose issues need production infrastructure / external accounts: analysed and documented only.
    investigate_only_labels: list[str] = field(default_factory=lambda: ["production-readiness"])


@dataclass
class QualityGatesConfig:
    pint: bool = True
    pint_autofix: bool = True
    phpunit: bool = True
    phpstan: bool = True
    composer_audit: bool = True
    migrations: bool = True
    independent_review: bool = True
    php_runner: str = "auto"  # auto | docker | local
    docker_image: str = "auto"  # auto = "<repo-dir>-app" (docker compose default) if present
    on_unavailable: str = "fail"  # fail | allow (allow = PR is created but marked UNVERIFIED)
    phpunit_timeout: int = 1200
    blocking_review_severities: list[str] = field(default_factory=lambda: ["critical", "high"])


@dataclass
class PullRequestConfig:
    draft: bool = False  # true = open PRs as drafts
    description: str = "short"  # short | full
    auto_merge: bool = False
    close_issue_only_when_fully_resolved: bool = True
    comment_on_issue: bool = True
    title_max_length: int = 72


@dataclass
class SafetyConfig:
    require_human_approval_for_destructive_operations: bool = True
    allow_production_access: bool = False
    allow_production_deployment: bool = False
    allow_secret_modification: bool = False
    allow_protected_branch_push: bool = False
    protected_branches: list[str] = field(
        default_factory=lambda: ["main", "master", "staging", "production", "development", "develop", "test", "release"]
    )
    max_changed_files: int = 40
    max_changed_lines: int = 2000
    human_required_patterns: list[str] = field(
        default_factory=lambda: [
            r"\brotat\w*\b[^.\n]*\b(app_key|credential|secret|api[ _-]?key)s?\b",
            r"\b(app_key|credential|secret)s?\b[^.\n]*\b(rotat|revok)\w*",
            r"\brevok\w*\b[^.\n]*\b(credential|secret|app_key)s?\b",
            r"\bexposed (credential|secret|key)s?\b",
            r"\bproduction (secret|credential)s?\b",
            r"\bincident response\b",
            r"\brewrit\w* (the )?(git )?history\b",
            r"\bdeploy\w* to production\b",
        ]
    )
    forbidden_paths: list[str] = field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "!.env.example",
            "auth.json",
            "*.pem",
            "*.key",
            "storage/*.key",
            "id_rsa*",
            "database/migrations/*:modify",  # existing migrations must never be edited/deleted
        ]
    )


@dataclass
class ProjectStatusConfig:
    backlog: str = "Backlog"
    in_progress: str = "In Progress"
    in_review: str = "In Review"
    blocked: str = "Blocked"
    in_review_fallback: str | None = None  # used when in_review is missing; None = leave status unchanged
    revert_to_backlog_on_failure: bool = True


@dataclass
class HumanInterventionConfig:
    label: str = "ai:needs-human"
    apply_label: bool = True
    create_label_if_missing: bool = True
    post_comment: bool = True
    set_blocked_status_if_available: bool = True


@dataclass
class Config:
    github: GitHubConfig = field(default_factory=GitHubConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    quality_gates: QualityGatesConfig = field(default_factory=QualityGatesConfig)
    pull_requests: PullRequestConfig = field(default_factory=PullRequestConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    project_status: ProjectStatusConfig = field(default_factory=ProjectStatusConfig)
    human_intervention: HumanInterventionConfig = field(default_factory=HumanInterventionConfig)

    # Resolved at runtime (not from file)
    source_path: str | None = None

    @property
    def state_dir(self) -> Path:
        path = Path(os.path.expanduser(self.execution.state_dir))
        return path if path.is_absolute() else AGENT_DIR / path

    @property
    def worktree_root(self) -> Path:
        return Path(os.path.expanduser(self.execution.worktree_root)).resolve()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source_path", None)
        return data


def _parse_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text) or {}
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ConfigError(
            "PyYAML is required for YAML config files (sudo apt install python3-yaml), "
            "or use a .json config file."
        ) from exc
    return yaml.safe_load(text) or {}


def _apply(section: Any, values: dict[str, Any], path: str) -> None:
    if not isinstance(values, dict):
        raise ConfigError(f"{path} must be a mapping")
    known = {f.name: f for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ConfigError(f"Unknown config key: {path}.{key}")
        current = getattr(section, key)
        if is_dataclass(current):
            _apply(current, value or {}, f"{path}.{key}")
            continue
        if value == "configurable":
            continue  # placeholder in the example file: keep the default
        if isinstance(current, bool) and not isinstance(value, bool):
            raise ConfigError(f"{path}.{key} must be true/false")
        if isinstance(current, int) and not isinstance(current, bool) and value is not None:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"{path}.{key} must be an integer")
        if isinstance(current, list) and not isinstance(value, list):
            raise ConfigError(f"{path}.{key} must be a list")
        setattr(section, key, value)


def validate(config: Config) -> None:
    c = config
    if c.execution.mode != "sequential" or c.execution.allow_parallel:
        raise ConfigError("Only sequential execution is supported (execution.mode=sequential, allow_parallel=false)")
    if not c.execution.stop_on_systemic_failure:
        raise ConfigError("execution.stop_on_systemic_failure cannot be disabled")
    if c.pull_requests.auto_merge:
        raise ConfigError("pull_requests.auto_merge=true is not supported: PRs are never merged automatically")
    if c.pull_requests.description not in ("short", "full"):
        raise ConfigError("pull_requests.description must be short or full")
    unsafe = {
        "allow_production_access": c.safety.allow_production_access,
        "allow_production_deployment": c.safety.allow_production_deployment,
        "allow_secret_modification": c.safety.allow_secret_modification,
        "allow_protected_branch_push": c.safety.allow_protected_branch_push,
    }
    enabled = [k for k, v in unsafe.items() if v]
    if enabled:
        raise ConfigError(f"Unsafe safety options are not supported by this agent: {', '.join(enabled)}")
    if not c.safety.require_human_approval_for_destructive_operations:
        raise ConfigError("safety.require_human_approval_for_destructive_operations cannot be disabled")
    if c.quality_gates.php_runner not in ("auto", "docker", "local"):
        raise ConfigError("quality_gates.php_runner must be auto, docker or local")
    if c.quality_gates.on_unavailable not in ("fail", "allow"):
        raise ConfigError("quality_gates.on_unavailable must be fail or allow")
    if c.claude.max_turns < 1 or c.claude.timeout < 30:
        raise ConfigError("claude.max_turns must be >= 1 and claude.timeout >= 30 seconds")
    if c.claude.max_repair_attempts < 0:
        raise ConfigError("claude.max_repair_attempts must be >= 0")
    if c.execution.default_limit is not None and c.execution.default_limit < 1:
        raise ConfigError("execution.default_limit must be null or >= 1")
    if not c.selection.priorities:
        raise ConfigError("selection.priorities cannot be empty")


def load_config(path: str | None = None) -> Config:
    config = Config()
    candidate: Path | None = None
    if path:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise ConfigError(f"Config file not found: {candidate}")
    else:
        for name in ("config.yaml", "config.yml", "config.json"):
            if (AGENT_DIR / name).exists():
                candidate = AGENT_DIR / name
                break
    if candidate:
        data = _parse_file(candidate)
        if not isinstance(data, dict):
            raise ConfigError("Config root must be a mapping")
        _apply(config, data, "config")
        config.source_path = str(candidate)
    validate(config)
    return config
