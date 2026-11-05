"""Subprocess execution seam.

Every external command (git, gh, claude, docker, php) goes through
``CommandRunner`` so that the whole agent can be unit-tested with a fake
runner and so that timeouts, process-group cleanup and secret redaction are
handled in exactly one place.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def combined(self) -> str:
        return (self.stdout or "") + ("\n" + self.stderr if self.stderr else "")


class CommandRunner:
    """Runs commands in their own process group so timeouts kill children too."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                list(args),
                cwd=cwd,
                env=full_env,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return CommandResult(list(args), 127, "", str(exc), 0.0)
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
            return CommandResult(list(args), proc.returncode, stdout, stderr, time.monotonic() - started)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            stdout, stderr = proc.communicate()
            return CommandResult(
                list(args), -1, stdout or "", (stderr or "") + "\n[timed out]", time.monotonic() - started, True
            )
        except KeyboardInterrupt:
            _kill_group(proc)
            raise


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
