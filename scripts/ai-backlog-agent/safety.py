"""Safety rules: secret redaction, human-approval detection, diff scanning,
branch protection and environment checks.

These checks are deterministic and run independently of Claude, so a model
mistake cannot bypass them.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github_token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("aws_access_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("laravel_app_key", re.compile(r"\bbase64:[A-Za-z0-9+/]{40,}={0,2}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{20,}=*")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
]

# key=value style assignments of credentials with a non-trivial literal value
ASSIGNMENT_SECRET = re.compile(
    r"""(?ix)
    \b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*)
    ["']?\s*(?:=|=>|:)\s*
    ["']?([^\s"'#,;)]{8,})["']?
    """
)

PLACEHOLDER_VALUES = re.compile(
    r"(?i)^(null|none|true|false|env\(|\$|<|\{|your|change|example|placeholder|secret|password|xxx+|\*+|"
    r"redacted|test|testing|dummy|fake|sample|config\(|getenv|\[)"
)


def redact(text: str) -> str:
    if not text:
        return text
    for _, pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def find_secrets(lines: Iterable[str]) -> list[str]:
    """Return descriptions of secret-like values in added lines (values themselves are never returned)."""
    hits: list[str] = []
    for idx, line in enumerate(lines, 1):
        for name, pattern in SECRET_PATTERNS:
            if name == "bearer_token" and "$" in line:
                continue
            if pattern.search(line):
                hits.append(f"line {idx}: {name}")
        for match in ASSIGNMENT_SECRET.finditer(line):
            value = match.group(2)
            if PLACEHOLDER_VALUES.match(value) or "(" in value or value.startswith(("'", '"')):
                continue
            if re.fullmatch(r"[a-z_.]+", value):  # identifiers such as password_confirmation
                continue
            if "|" in value or re.match(r"(?i)(required|nullable|sometimes|string|confirmed|min:|max:|bail|hashed)", value):
                continue  # validation rules / casts
            hits.append(f"line {idx}: hard-coded value for {match.group(1)}")
    return hits


# --------------------------------------------------------------------------
# Human-approval detection
# --------------------------------------------------------------------------


def _section(body: str, heading: str) -> str:
    match = re.search(rf"(?ims)^##+\s*{re.escape(heading)}\s*$(.*?)(?=^##+\s|\Z)", body or "")
    return match.group(1) if match else ""


def human_required_reasons(title: str, body: str, labels: list[str], patterns: list[str], human_labels: list[str]) -> list[str]:
    """Rule-based detection of issues that must never be implemented automatically.

    Only the title and the acceptance criteria / expected outcome are matched:
    implementation hints in the body often mention credentials in passing and
    would cause false positives.
    """
    reasons: list[str] = []
    lowered = {label.lower() for label in labels}
    for label in human_labels:
        if label.lower() in lowered:
            reasons.append(f"labelled '{label}'")
    haystack = "\n".join([title or "", _section(body, "Acceptance criteria"), _section(body, "Expected outcome")])
    for pattern in patterns:
        match = re.search(pattern, haystack, re.IGNORECASE)
        if match:
            reasons.append(f"requires a privileged/irreversible operation (matched '{match.group(0).strip()[:80]}')")
            break  # one pattern is enough to route the issue to a human
    return reasons


# --------------------------------------------------------------------------
# Diff scanning
# --------------------------------------------------------------------------


@dataclass
class ChangedFile:
    path: str
    status: str  # A, M, D, R, C...
    added: int = 0
    deleted: int = 0
    old_path: str | None = None


@dataclass
class DiffScanResult:
    blocking: list[str] = field(default_factory=list)  # never commit; needs human
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking


DESTRUCTIVE_MIGRATION = re.compile(
    r"(?i)(->\s*drop\w*\s*\(|Schema::drop\w*\s*\(|Schema::rename\s*\(|->\s*renameColumn\s*\(|"
    r"\btruncate\s*\(|\bDROP\s+(TABLE|COLUMN|DATABASE|INDEX)\b|\bDELETE\s+FROM\b|->\s*delete\s*\(\s*\))"
)
SKIPPED_TEST = re.compile(r"(markTestSkipped|markTestIncomplete|@group\s+skip|->skip\(|#\[\s*Skip|\$this->skip)")


def _match_path(path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(os.path.basename(path), pattern)


def migration_up_body(source: str) -> str:
    """Text of the up() method only; drops inside down() are normal rollback code."""
    up = re.search(r"function\s+up\s*\(", source)
    if not up:
        return source
    down = re.search(r"function\s+down\s*\(", source[up.end():])
    return source[up.end(): up.end() + down.start()] if down else source[up.end():]


def scan_diff(
    files: list[ChangedFile],
    added_lines: dict[str, list[str]],
    new_file_contents: dict[str, str],
    forbidden_paths: list[str],
    max_files: int,
    max_lines: int,
) -> DiffScanResult:
    result = DiffScanResult()
    deny = [p for p in forbidden_paths if not p.startswith("!") and not p.endswith(":modify")]
    allow = [p[1:] for p in forbidden_paths if p.startswith("!")]
    modify_only = [p[: -len(":modify")] for p in forbidden_paths if p.endswith(":modify")]

    for f in files:
        paths = [f.path] + ([f.old_path] if f.old_path else [])
        for path in paths:
            if any(_match_path(path, a) for a in allow):
                continue
            if any(_match_path(path, d) for d in deny):
                result.blocking.append(f"forbidden path changed: {path}")
            if f.status[0] in "MDR" and any(fnmatch.fnmatch(path, m) for m in modify_only):
                result.blocking.append(f"existing protected file modified/deleted: {path} (create a new migration instead)")

        if f.path.startswith("database/migrations/") and f.status.startswith("A"):
            body = migration_up_body(new_file_contents.get(f.path, ""))
            if DESTRUCTIVE_MIGRATION.search(body):
                result.blocking.append(f"potentially destructive migration operation in up(): {f.path}")

        if f.status.startswith("D") and f.path.startswith("tests/"):
            result.blocking.append(f"test file deleted: {f.path} (tests must not be disabled)")

        added = added_lines.get(f.path, [])
        if f.path.startswith("tests/") and any(SKIPPED_TEST.search(line) for line in added):
            result.blocking.append(f"test skipping introduced in {f.path}")

        secret_hits = find_secrets(added)
        if secret_hits:
            result.blocking.append(f"possible secret added in {f.path}: {', '.join(secret_hits[:5])}")

    total_lines = sum(f.added + f.deleted for f in files)
    if len(files) > max_files:
        result.blocking.append(f"diff touches {len(files)} files (limit {max_files}); too broad for an automated PR")
    if total_lines > max_lines:
        result.blocking.append(f"diff changes {total_lines} lines (limit {max_lines}); too broad for an automated PR")
    if not files:
        result.blocking.append("no changes were produced")
    return result


# --------------------------------------------------------------------------
# Branch / environment protection
# --------------------------------------------------------------------------

BRANCH_PREFIX = "ai/fix-issue-"


def assert_safe_branch(branch: str, protected: list[str], issue_number: int | None = None) -> None:
    from models import UnsafeEnvironmentError

    if not branch or branch in protected or branch.lower() in (p.lower() for p in protected):
        raise UnsafeEnvironmentError(f"refusing to operate on protected branch '{branch}'")
    if not branch.startswith(BRANCH_PREFIX):
        raise UnsafeEnvironmentError(f"refusing to operate on non-agent branch '{branch}'")
    if issue_number is not None and not branch.startswith(f"{BRANCH_PREFIX}{issue_number}-"):
        raise UnsafeEnvironmentError(f"branch '{branch}' does not belong to issue #{issue_number}")


def environment_problems(env: dict[str, str] | None = None) -> list[str]:
    """Detect an execution environment pointing at production."""
    env = dict(os.environ if env is None else env)
    problems = []
    if env.get("APP_ENV", "").lower() in ("production", "prod"):
        problems.append("APP_ENV=production is set in the agent's environment")
    for key in ("DB_HOST", "DATABASE_URL", "DB_URL"):
        value = env.get(key, "")
        if value and re.search(r"(?i)prod", value):
            problems.append(f"{key} appears to point at a production database")
    return problems
