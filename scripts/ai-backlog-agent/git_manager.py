"""Git operations. Never force-pushes, never resets, never deletes remote branches."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from models import AgentError, RepositoryIntegrityError, UnsafeEnvironmentError
from process import CommandResult, CommandRunner
from safety import BRANCH_PREFIX, ChangedFile, assert_safe_branch

STOP_WORDS = {"a", "an", "and", "the", "of", "to", "for", "in", "on", "with", "via", "or", "by", "from", "after", "before"}


class GitError(AgentError):
    pass


def slugify(title: str, max_length: int = 40) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"\[[^\]]*\]", " ", text)  # drop [SECURITY][HIGH] style tags
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w and w not in STOP_WORDS]
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > max_length:
            break
        slug = candidate
    return slug or "issue"


def branch_name(issue_number: int, title: str) -> str:
    return f"{BRANCH_PREFIX}{issue_number}-{slugify(title)}"


def parse_remote_url(url: str) -> str | None:
    match = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


class GitManager:
    def __init__(self, runner: CommandRunner, repo_root: Path, remote: str = "origin", protected: list[str] | None = None):
        self.runner = runner
        self.repo_root = Path(repo_root)
        self.remote = remote
        self.protected = protected or []

    def git(self, args: list[str], cwd: Path | str | None = None, check: bool = True, timeout: float = 300) -> CommandResult:
        result = self.runner.run(["git", *args], cwd=str(cwd or self.repo_root), timeout=timeout)
        if check and not result.ok:
            raise GitError(f"git {' '.join(args[:4])} failed: {result.combined().strip()[:600]}")
        return result

    # ------------------------------------------------------------ discovery

    @staticmethod
    def find_repo_root(runner: CommandRunner, start: Path) -> Path:
        result = runner.run(["git", "rev-parse", "--show-toplevel"], cwd=str(start), timeout=30)
        if not result.ok:
            raise RepositoryIntegrityError(f"{start} is not inside a git repository")
        return Path(result.stdout.strip())

    def detect_repository(self) -> str:
        url = self.git(["remote", "get-url", self.remote]).stdout.strip()
        repo = parse_remote_url(url)
        if not repo:
            raise RepositoryIntegrityError(f"Remote '{self.remote}' is not a GitHub repository: {url}")
        return repo

    def integrity_check(self) -> list[str]:
        """Return problems that make the repository state uncertain."""
        problems = []
        git_dir = self.git(["rev-parse", "--git-common-dir"], check=False)
        if not git_dir.ok:
            return ["git rev-parse failed: repository may be corrupted"]
        common = Path(git_dir.stdout.strip())
        if not common.is_absolute():
            common = self.repo_root / common
        for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "BISECT_LOG"):
            if (common / marker).exists():
                problems.append(f"main checkout has an operation in progress ({marker})")
        if (common / "index.lock").exists():
            problems.append("git index.lock exists (another git process running or a crash left it behind)")
        if not self.git(["status", "--porcelain"], check=False).ok:
            problems.append("git status failed in the main checkout")
        return problems

    def fetch(self, branch: str | None = None) -> None:
        args = ["fetch", "--quiet", self.remote]
        if branch:
            args.append(branch)
        self.git(args, timeout=300)

    def resolve(self, ref: str, cwd: Path | None = None) -> str:
        result = self.git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cwd, check=False)
        if not result.ok or not result.stdout.strip():
            raise RepositoryIntegrityError(f"Cannot resolve git ref '{ref}'")
        return result.stdout.strip()

    def local_branch_exists(self, branch: str) -> bool:
        return self.git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).ok

    def remote_branches(self, prefix: str = BRANCH_PREFIX) -> list[str]:
        result = self.git(["ls-remote", "--heads", self.remote], timeout=120)
        branches = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                name = parts[1][len("refs/heads/"):]
                if name.startswith(prefix):
                    branches.append(name)
        return branches

    def remote_branches_for_issue(self, issue_number: int) -> list[str]:
        return self.remote_branches(f"{BRANCH_PREFIX}{issue_number}-")

    def local_branches_for_issue(self, issue_number: int) -> list[str]:
        result = self.git(["for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}{issue_number}-*"], check=False)
        return [l.strip() for l in result.stdout.splitlines() if l.strip()]

    # ------------------------------------------------------------- worktree

    def current_branch(self, cwd: Path) -> str:
        return self.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).stdout.strip()

    def head(self, cwd: Path) -> str:
        return self.git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()

    def status_porcelain(self, cwd: Path) -> list[str]:
        out = self.git(["status", "--porcelain", "--untracked-files=all"], cwd=cwd).stdout
        return [line for line in out.splitlines() if line.strip()]

    def stage_all(self, cwd: Path) -> None:
        self.git(["add", "-A"], cwd=cwd)

    def changed_files(self, cwd: Path, base_sha: str) -> list[ChangedFile]:
        """Files changed vs base (staged view). Call stage_all first."""
        name_status = self.git(["diff", "--cached", "--name-status", "-M", base_sha], cwd=cwd).stdout
        numstat = self.git(["diff", "--cached", "--numstat", "-M", base_sha], cwd=cwd).stdout
        counts: dict[str, tuple[int, int]] = {}
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                added = int(parts[0]) if parts[0].isdigit() else 0
                deleted = int(parts[1]) if parts[1].isdigit() else 0
                path = parts[-1]
                if " => " in path:  # rename notation
                    path = re.sub(r"\{[^}]* => ([^}]*)\}", r"\1", path)
                    path = path.split(" => ")[-1]
                counts[path] = (added, deleted)
        files = []
        for line in name_status.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0]
            if status.startswith(("R", "C")) and len(parts) >= 3:
                old, new = parts[1], parts[2]
            else:
                old, new = None, parts[1]
            added, deleted = counts.get(new, (0, 0))
            files.append(ChangedFile(new, status, added, deleted, old))
        return files

    def diff_text(self, cwd: Path, base_sha: str, max_chars: int = 120_000) -> str:
        text = self.git(["diff", "--cached", "-M", base_sha], cwd=cwd).stdout
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[diff truncated at {max_chars} characters]"
        return text

    def added_lines(self, cwd: Path, base_sha: str) -> dict[str, list[str]]:
        text = self.git(["diff", "--cached", "-M", "--unified=0", base_sha], cwd=cwd).stdout
        result: dict[str, list[str]] = {}
        current = None
        for line in text.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else None
                if current:
                    result.setdefault(current, [])
            elif line.startswith("+") and not line.startswith("+++") and current:
                result[current].append(line[1:])
        return result

    def file_at(self, cwd: Path, path: str) -> str:
        target = Path(cwd) / path
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def commit(self, cwd: Path, message: str, issue_number: int) -> str:
        branch = self.current_branch(cwd)
        assert_safe_branch(branch, self.protected, issue_number)
        self.git(["commit", "-m", message], cwd=cwd)  # hooks run normally; never --no-verify
        return self.head(cwd)

    def push(self, cwd: Path, branch: str, issue_number: int) -> None:
        assert_safe_branch(branch, self.protected, issue_number)
        if self.current_branch(cwd) != branch:
            raise UnsafeEnvironmentError(f"worktree is on '{self.current_branch(cwd)}', expected '{branch}'")
        # No --force, ever. Explicit refspec so nothing else can be pushed.
        self.git(["push", "--set-upstream", self.remote, f"refs/heads/{branch}:refs/heads/{branch}"], cwd=cwd, timeout=300)

    def remote_sha(self, branch: str) -> str | None:
        result = self.git(["ls-remote", "--heads", self.remote, f"refs/heads/{branch}"], check=False, timeout=120)
        line = result.stdout.strip().splitlines()
        return line[0].split("\t")[0] if line else None
