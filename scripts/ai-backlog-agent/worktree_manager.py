"""Isolated git worktree per issue.

Worktrees live outside the repository (default ~/.cache/ai-backlog-agent/...)
so Claude, confined to the worktree, can never see the main checkout, .env or
other issues' work. Nothing is ever reset or force-removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from git_manager import GitManager
from models import IssueFailure
from safety import assert_safe_branch


@dataclass
class WorktreeInfo:
    path: Path
    head: str
    branch: str | None


class WorktreeManager:
    def __init__(self, git: GitManager, root: Path, repository: str, logger=None):
        self.git = git
        self.root = Path(root) / repository.replace("/", "__")
        self.logger = logger

    def path_for(self, issue_number: int) -> Path:
        return self.root / f"issue-{issue_number}"

    def list(self) -> list[WorktreeInfo]:
        out = self.git.git(["worktree", "list", "--porcelain"]).stdout
        items, current = [], {}
        for line in out.splitlines() + [""]:
            if not line.strip():
                if current.get("worktree"):
                    branch = current.get("branch", "")
                    items.append(WorktreeInfo(Path(current["worktree"]), current.get("HEAD", ""),
                                              branch.replace("refs/heads/", "") or None))
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return items

    def find(self, path: Path) -> WorktreeInfo | None:
        resolved = Path(path).resolve()
        return next((w for w in self.list() if w.path.resolve() == resolved), None)

    def find_by_branch(self, branch: str) -> WorktreeInfo | None:
        return next((w for w in self.list() if w.branch == branch), None)

    def create(self, issue_number: int, branch: str, base_sha: str) -> Path:
        assert_safe_branch(branch, self.git.protected, issue_number)
        path = self.path_for(issue_number)
        existing = self.find(path)
        if existing or path.exists():
            raise IssueFailure(
                "workspace",
                f"worktree path {path} already exists (branch {existing.branch if existing else 'unknown'})",
                "Inspect the existing worktree; resume with --resume or remove it manually with "
                f"'git worktree remove {path}' once its changes are no longer needed.",
            )
        other = self.find_by_branch(branch)
        if other:
            raise IssueFailure("workspace", f"branch {branch} is already checked out at {other.path}",
                               "Inspect that worktree before re-running.")
        if self.git.local_branch_exists(branch):
            raise IssueFailure(
                "workspace",
                f"local branch {branch} already exists and is not tracked by an execution",
                f"Inspect it ('git log {branch}'); delete it with 'git branch -D {branch}' if obsolete, or use --resume.",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.git(["worktree", "add", "--quiet", "-b", branch, str(path), base_sha])
        self.verify(path, branch, base_sha)
        return path

    def attach_existing(self, issue_number: int, branch: str, path: Path) -> Path:
        """Re-use our own worktree during resume, after verifying it is what we expect."""
        assert_safe_branch(branch, self.git.protected, issue_number)
        info = self.find(path)
        if info:
            if info.branch != branch:
                raise IssueFailure("resume", f"worktree {path} is on {info.branch}, expected {branch}",
                                   "Inspect the worktree manually.")
            return path
        if self.git.local_branch_exists(branch):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git.git(["worktree", "add", "--quiet", str(path), branch])
            return path
        raise IssueFailure("resume", f"neither worktree {path} nor branch {branch} exist", "Re-run the issue with --retry-failed.")

    def verify(self, path: Path, branch: str, base_sha: str | None = None) -> None:
        if self.git.current_branch(path) != branch:
            raise IssueFailure("workspace", f"worktree {path} is not on {branch}")
        if base_sha and self.git.head(path) != base_sha:
            raise IssueFailure("workspace", f"worktree {path} HEAD does not match base {base_sha[:10]}")
        dirty = self.git.status_porcelain(path)
        if dirty:
            raise IssueFailure("workspace", f"fresh worktree is not clean: {dirty[:5]}")

    def archive(self, issue_number: int, branch: str, path: Path | None, tag: str) -> str:
        """Move a previous (agent-owned) attempt out of the way without losing anything.

        The worktree is moved to ``archive-issue-N-<tag>`` and the branch renamed to
        ``ai-archive/issue-N-<tag>``; nothing is reset or deleted.
        """
        assert_safe_branch(branch, self.git.protected, issue_number)
        archived_branch = f"ai-archive/issue-{issue_number}-{tag}"
        if path and self.find(path):
            target = self.root / f"archive-issue-{issue_number}-{tag}"
            self.git.git(["worktree", "move", str(path), str(target)])
        if self.git.local_branch_exists(branch):
            self.git.git(["branch", "-m", branch, archived_branch])
        if self.logger:
            self.logger.info(f"Archived previous attempt of #{issue_number} as branch {archived_branch}")
        return archived_branch

    def create_detached(self, key: int | str, sha: str, tag: str) -> Path:
        """Read-only scratch worktree at a commit (baseline measurement, investigations)."""
        path = self.root / f"{tag}-{key}"
        if self.find(path):
            self.remove_detached(path)
        if path.exists():
            raise IssueFailure("workspace", f"scratch path {path} exists but is not a worktree; remove it manually")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.git(["worktree", "add", "--quiet", "--detach", str(path), sha])
        return path

    def remove_detached(self, path: Path) -> None:
        """Scratch worktrees hold no work of value (detached, never edited by Claude), so --force is safe here."""
        info = self.find(path)
        if info and info.branch is None:
            self.git.git(["worktree", "remove", "--force", str(path)], check=False)
            self.git.git(["worktree", "prune"], check=False)

    def discard_if_pristine(self, path: Path, branch: str, base_sha: str) -> bool:
        """Remove worktree AND branch only when nothing was done in them (clean, HEAD == base)."""
        if not self.find(path):
            return True
        if self.git.status_porcelain(path) or self.git.head(path) != base_sha:
            return False
        assert_safe_branch(branch, self.git.protected)
        if not self.cleanup(path):
            return False
        if self.git.local_branch_exists(branch) and self.git.resolve(branch) == base_sha:
            self.git.git(["branch", "-D", branch], check=False)  # holds no commits of its own
        return True

    def cleanup(self, path: Path, *, require_clean: bool = True) -> bool:
        """Remove a worktree only when it holds no uncommitted work. Local branch is kept."""
        if not self.find(path):
            return True
        dirty = self.git.status_porcelain(path)
        if dirty and require_clean:
            if self.logger:
                self.logger.warning(f"Keeping worktree {path}: it has uncommitted changes ({len(dirty)} files)")
            return False
        result = self.git.git(["worktree", "remove", str(path)], check=False)
        if not result.ok:
            if self.logger:
                self.logger.warning(f"Could not remove worktree {path}: {result.stderr.strip()[:200]}")
            return False
        self.git.git(["worktree", "prune"], check=False)
        return True
