"""Where PHP commands run.

``docker`` mode (default when PHP is not installed on the host) runs every
command in a throw-away container with **no network**, the issue worktree
mounted at /var/www/html and the main checkout's vendor/ mounted read-only.
Nothing in the container can reach a production database or service, and
.env is never present (it is git-ignored, so worktrees don't contain it).

``local`` mode runs PHP on the host; it is less isolated (documented).
"""

from __future__ import annotations

import base64
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from process import CommandResult, CommandRunner

CONTAINER_ROOT = "/var/www/html"
CONTAINER_OUT = "/agent-out"


def ephemeral_app_key() -> str:
    """Random throw-away key for the test process only (never a real secret)."""
    return "base64:" + base64.b64encode(os.urandom(32)).decode()


TEST_ENV = {
    "APP_ENV": "testing",
    "APP_DEBUG": "true",
    "CACHE_STORE": "array",
    "SESSION_DRIVER": "array",
    "QUEUE_CONNECTION": "sync",
    "MAIL_MAILER": "array",
    "DB_CONNECTION": "sqlite",
    "DB_DATABASE": ":memory:",
    "BROADCAST_CONNECTION": "log",
}


@dataclass
class PhpEnvironment:
    runner: CommandRunner
    mode: str  # docker | local | unavailable
    repo_root: Path
    image: str | None = None
    reason: str = ""

    @classmethod
    def detect(cls, runner: CommandRunner, repo_root: Path, php_runner: str = "auto", docker_image: str = "auto") -> "PhpEnvironment":
        if php_runner in ("auto", "local") and shutil.which("php"):
            return cls(runner, "local", repo_root)
        if php_runner == "local":
            return cls(runner, "unavailable", repo_root, reason="php_runner=local but 'php' is not on PATH")
        if not shutil.which("docker"):
            return cls(runner, "unavailable", repo_root, reason="neither 'php' nor 'docker' is available")
        candidates = [docker_image] if docker_image != "auto" else [
            f"{repo_root.name.lower()}-app", f"{repo_root.name.lower()}_app", f"{repo_root.name.lower()}-php",
        ]
        for image in candidates:
            if runner.run(["docker", "image", "inspect", image], timeout=60).ok:
                return cls(runner, "docker", repo_root, image=image)
        return cls(runner, "unavailable", repo_root,
                   reason=f"no docker image found ({', '.join(candidates)}); build it with 'docker compose build' "
                          "or set quality_gates.docker_image")

    @property
    def available(self) -> bool:
        return self.mode in ("docker", "local")

    @property
    def vendor_dir(self) -> Path:
        return self.repo_root / "vendor"

    def has_vendor_binary(self, name: str, worktree: Path | None = None) -> bool:
        base = worktree / "vendor" if worktree and (worktree / "vendor" / "autoload.php").exists() else self.vendor_dir
        return (base / "bin" / name).exists()

    def out_path(self, out_dir: Path, name: str) -> str:
        return f"{CONTAINER_OUT}/{name}" if self.mode == "docker" else str(out_dir / name)

    def _uses_own_vendor(self, worktree: Path) -> bool:
        vendor = worktree / "vendor"
        return vendor.exists() and not vendor.is_symlink() and (vendor / "autoload.php").exists()

    def run(self, worktree: Path, command: list[str], *, out_dir: Path | None = None, network: bool = False,
            env: dict[str, str] | None = None, timeout: float = 1200, mount_vendor: bool | None = None) -> CommandResult:
        if mount_vendor is None:
            mount_vendor = not self._uses_own_vendor(worktree)
        merged_env = {**TEST_ENV, "APP_KEY": ephemeral_app_key(), **(env or {})}
        if self.mode == "docker":
            (worktree / "vendor").mkdir(exist_ok=True)  # mount point (git-ignored), avoids a root-owned dir
            args = ["docker", "run", "--rm", "--init"]
            if not network:
                args += ["--network", "none"]
            args += ["--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{worktree}:{CONTAINER_ROOT}"]
            if mount_vendor:
                args += ["-v", f"{self.vendor_dir}:{CONTAINER_ROOT}/vendor:ro"]
            if out_dir:
                args += ["-v", f"{out_dir}:{CONTAINER_OUT}"]
            args += ["-w", CONTAINER_ROOT, "-e", "HOME=/tmp", "-e", "COMPOSER_HOME=/tmp/composer"]
            for key, value in merged_env.items():
                args += ["-e", f"{key}={value}"]
            args += [self.image or "", *command]
            return self.runner.run(args, timeout=timeout)
        if self.mode == "local":
            vendor = worktree / "vendor"
            if not vendor.exists():
                vendor.symlink_to(self.vendor_dir)  # git-ignored
            return self.runner.run(command, cwd=str(worktree), env=merged_env, timeout=timeout)
        return CommandResult(command, 127, "", f"PHP environment unavailable: {self.reason}", 0.0)

    def install_vendor(self, worktree: Path, timeout: float = 1200) -> CommandResult:
        """Install dependencies into the worktree (needed when composer.json/lock changed)."""
        vendor = worktree / "vendor"
        if vendor.is_symlink():
            vendor.unlink()
        elif vendor.exists() and not any(vendor.iterdir()):
            vendor.rmdir()
        vendor.mkdir(exist_ok=True)
        return self.run(worktree, ["composer", "install", "--no-interaction", "--no-progress", "--no-scripts"],
                        network=True, timeout=timeout, mount_vendor=False)
