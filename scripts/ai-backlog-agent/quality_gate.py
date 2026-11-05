"""Quality gates: Pint, PHPUnit, PHPStan/Larastan, composer audit, migrations.

Gates are *baseline-aware*: the base commit is measured first (cached per base
SHA) and a change only fails a gate when it introduces something new, e.g. a
test that was passing on the base branch now fails. Pre-existing failures are
reported but don't block. A gate result is only ever ``passed`` when the tool
actually ran and said so; anything that could not run is ``unavailable``.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from models import GateResult, GateStatus, QualityReport
from php_env import PhpEnvironment
from safety import ChangedFile

PHPSTAN_CONFIGS = ("phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon")


def parse_junit(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    tests: list[str] = []
    failed: dict[str, str] = {}
    skipped: list[str] = []
    for case in root.iter("testcase"):
        cls = case.get("class") or case.get("classname") or case.get("file") or ""
        test_id = f"{cls}::{case.get('name')}"
        tests.append(test_id)
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            failed[test_id] = (problem.text or problem.get("message") or "").strip()[:1500]
        elif case.find("skipped") is not None:
            skipped.append(test_id)
    return {"tests": tests, "failed": failed, "skipped": skipped}


class QualityGate:
    def __init__(self, config, php: PhpEnvironment, state, logger, git, worktrees=None):
        self.worktrees = worktrees
        self.config = config
        self.gates = config.quality_gates
        self.php = php
        self.state = state
        self.logger = logger
        self.git = git

    # -------------------------------------------------------------- baseline

    def baseline_for(self, base_sha: str) -> dict[str, Any]:
        """Baseline measured in a separate scratch worktree, so it never mixes with the issue's changes."""
        cached = self.state.get_baseline(base_sha, "all") if self.state else None
        if cached:
            self.logger.debug(f"Using cached quality baseline for {base_sha[:10]}")
            return cached
        path = self.worktrees.create_detached(base_sha[:12], base_sha, "baseline")
        try:
            return self.baseline(path, base_sha)
        finally:
            self.worktrees.remove_detached(path)

    def baseline(self, worktree: Path, base_sha: str) -> dict[str, Any]:
        """Measure the untouched base commit (cached per SHA). Must run before any change."""
        cached = self.state.get_baseline(base_sha, "all") if self.state else None
        if cached:
            self.logger.info(f"Using cached quality baseline for {base_sha[:10]}")
            return cached
        self.logger.info(f"Measuring quality baseline on base commit {base_sha[:10]} (one-time per base commit)...")
        data: dict[str, Any] = {"base_sha": base_sha}
        if not self.php.available:
            data["unavailable"] = self.php.reason
            return data
        if self.gates.phpunit:
            data["phpunit"] = self._phpunit_raw(worktree)
        if self.gates.phpstan and self._phpstan_configured(worktree):
            data["phpstan"] = self._phpstan_raw(worktree)
        if data.get("phpunit", {}).get("ran") and self.state:
            self.state.save_baseline(base_sha, "all", data)
        return data

    # ------------------------------------------------------------------ run

    def run(self, worktree: Path, base_sha: str, files: list[ChangedFile], baseline: dict[str, Any]) -> QualityReport:
        results: list[GateResult] = []
        required_unavailable = self.gates.on_unavailable == "fail"
        if not self.php.available:
            for name, enabled in (("pint", self.gates.pint), ("phpunit", self.gates.phpunit)):
                if enabled:
                    results.append(GateResult(name, GateStatus.UNAVAILABLE, f"PHP environment unavailable: {self.php.reason}",
                                              required=required_unavailable))
            return QualityReport(results)

        paths = {f.path for f in files}
        if paths & {"composer.json", "composer.lock"}:
            results.append(self._prepare_vendor(worktree))
            if results[-1].status != GateStatus.PASSED:
                return QualityReport(results)

        if self.gates.pint:
            results.append(self._pint(worktree, files))
        if self.gates.phpunit:
            results.append(self._phpunit(worktree, baseline))
        if self.gates.phpstan:
            results.append(self._phpstan(worktree, baseline))
        if self.gates.migrations:
            results.append(self._migrations(worktree, files))
        if self.gates.composer_audit:
            results.append(self._composer_audit(worktree, base_sha, files))
        return QualityReport(results)

    # ----------------------------------------------------------------- pint

    def _pint(self, worktree: Path, files: list[ChangedFile]) -> GateResult:
        php_files = [f.path for f in files if f.path.endswith(".php") and not f.status.startswith("D")]
        if not php_files:
            return GateResult("pint", GateStatus.SKIPPED, "no PHP files changed", required=False)
        if not self.php.has_vendor_binary("pint", worktree):
            return self._unavailable("pint", "vendor/bin/pint is not installed")
        start = time.monotonic()
        if self.gates.pint_autofix:
            self.php.run(worktree, ["php", "vendor/bin/pint", *php_files], timeout=600)
        command = ["php", "vendor/bin/pint", "--test", *php_files]
        result = self.php.run(worktree, command, timeout=600)
        cmd_text = "vendor/bin/pint --test <changed PHP files>"
        if result.timed_out:
            return self._unavailable("pint", "pint timed out", cmd_text)
        if result.ok:
            return GateResult("pint", GateStatus.PASSED, f"{len(php_files)} changed PHP file(s) conform to Pint",
                              cmd_text, duration=time.monotonic() - start)
        return GateResult("pint", GateStatus.FAILED, "Pint style violations in changed files", cmd_text,
                          result.combined()[-4000:], time.monotonic() - start)

    # -------------------------------------------------------------- phpunit

    def _phpunit_raw(self, worktree: Path) -> dict[str, Any]:
        out_dir = Path(tempfile.mkdtemp(prefix="ai-agent-junit-"))
        try:
            out_dir.chmod(0o777)
            junit = self.php.out_path(out_dir, "junit.xml")
            result = self.php.run(
                worktree, ["php", "vendor/bin/phpunit", "--log-junit", junit, "--do-not-cache-result", "--colors=never"],
                out_dir=out_dir, timeout=self.gates.phpunit_timeout,
            )
            junit_file = out_dir / "junit.xml"
            if not junit_file.exists() or junit_file.stat().st_size == 0:
                return {"ran": False, "reason": "PHPUnit produced no JUnit report", "output": result.combined()[-3000:],
                        "timed_out": result.timed_out}
            parsed = parse_junit(junit_file.read_text(encoding="utf-8", errors="replace"))
            tail = "\n".join(result.stdout.strip().splitlines()[-3:])
            return {"ran": True, "exit_code": result.returncode, "summary_line": tail, "duration": result.duration, **parsed}
        except ET.ParseError as exc:
            return {"ran": False, "reason": f"invalid JUnit report: {exc}"}
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def _phpunit(self, worktree: Path, baseline: dict[str, Any]) -> GateResult:
        if not self.php.has_vendor_binary("phpunit", worktree):
            return self._unavailable("phpunit", "vendor/bin/phpunit is not installed")
        cmd_text = "vendor/bin/phpunit (full suite, SQLite in-memory, no network)"
        now = self._phpunit_raw(worktree)
        if not now.get("ran"):
            details = now.get("output", "")
            if now.get("timed_out"):
                return self._unavailable("phpunit", "PHPUnit timed out", cmd_text, details)
            return GateResult("phpunit", GateStatus.FAILED, f"PHPUnit did not complete: {now.get('reason')}", cmd_text, details)
        base = baseline.get("phpunit") or {}
        base_failed = set((base.get("failed") or {}).keys()) if base.get("ran") else set()
        failed = now["failed"]
        new_failures = {k: v for k, v in failed.items() if k not in base_failed}
        fixed = sorted(base_failed - set(failed))
        missing = sorted(set(base.get("tests") or []) - set(now["tests"])) if base.get("ran") else []
        summary = (f"{len(now['tests'])} tests, {len(failed)} failing "
                   f"({len(failed) - len(new_failures)} pre-existing on base, {len(new_failures)} new)")
        if fixed:
            summary += f", {len(fixed)} previously failing now pass"
        if not base.get("ran"):
            summary += "; WARNING: no baseline available, every failure counts as new"
        details = ""
        if new_failures:
            details = "\n\n".join(f"FAILED {k}\n{v}" for k, v in list(new_failures.items())[:15])
        if missing:
            details += "\n\nTests present on base but missing now:\n" + "\n".join(missing[:30])
        status = GateStatus.FAILED if new_failures else GateStatus.PASSED
        return GateResult("phpunit", status, summary, cmd_text, details, now.get("duration", 0.0))

    # -------------------------------------------------------------- phpstan

    def _phpstan_configured(self, worktree: Path) -> bool:
        return self.php.has_vendor_binary("phpstan", worktree) and any((worktree / c).exists() for c in PHPSTAN_CONFIGS)

    def _phpstan_raw(self, worktree: Path) -> dict[str, Any]:
        result = self.php.run(worktree, ["php", "-d", "memory_limit=1G", "vendor/bin/phpstan", "analyse",
                                         "--error-format=json", "--no-progress", "--no-interaction"], timeout=900)
        try:
            data = json.loads(result.stdout[result.stdout.find("{"):])
        except (json.JSONDecodeError, ValueError):
            return {"ran": False, "output": result.combined()[-3000:]}
        errors = []
        for file, info in (data.get("files") or {}).items():
            rel = re.sub(r"^/var/www/html/|^" + re.escape(str(worktree)) + "/", "", file)
            for msg in info.get("messages") or []:
                errors.append(f"{rel}: {msg.get('message')}")
        errors += [f"general: {e}" for e in data.get("errors") or []]
        return {"ran": True, "errors": errors}

    def _phpstan(self, worktree: Path, baseline: dict[str, Any]) -> GateResult:
        if not self._phpstan_configured(worktree):
            return GateResult("phpstan", GateStatus.SKIPPED, "PHPStan/Larastan is not installed/configured in this repository",
                              required=False)
        start = time.monotonic()
        now = self._phpstan_raw(worktree)
        if not now.get("ran"):
            return GateResult("phpstan", GateStatus.FAILED, "PHPStan did not complete", "vendor/bin/phpstan analyse", now.get("output", ""))
        base_errors = set((baseline.get("phpstan") or {}).get("errors") or [])
        new = [e for e in now["errors"] if e not in base_errors]
        status = GateStatus.FAILED if new else GateStatus.PASSED
        return GateResult("phpstan", status, f"{len(now['errors'])} errors ({len(new)} new vs base)",
                          "vendor/bin/phpstan analyse", "\n".join(new[:40]), time.monotonic() - start)

    # ----------------------------------------------------------- migrations

    def _migrations(self, worktree: Path, files: list[ChangedFile]) -> GateResult:
        new = [f.path for f in files if f.path.startswith("database/migrations/") and f.status.startswith("A")]
        if not new:
            return GateResult("migrations", GateStatus.SKIPPED, "no new migrations", required=False)
        out_dir = Path(tempfile.mkdtemp(prefix="ai-agent-db-"))
        try:
            out_dir.chmod(0o777)
            (out_dir / "gate.sqlite").touch()
            (out_dir / "gate.sqlite").chmod(0o666)
            env = {"DB_CONNECTION": "sqlite", "DB_DATABASE": self.php.out_path(out_dir, "gate.sqlite")}
            steps = [
                ["php", "artisan", "migrate", "--force", "--no-interaction"],
                ["php", "artisan", "migrate:rollback", f"--step={len(new)}", "--force", "--no-interaction"],
                ["php", "artisan", "migrate", "--force", "--no-interaction"],
            ]
            log = []
            start = time.monotonic()
            for step in steps:
                result = self.php.run(worktree, step, out_dir=out_dir, env=env, timeout=600)
                log.append(f"$ {' '.join(step)}\n{result.combined()[-1500:]}")
                if not result.ok:
                    return GateResult("migrations", GateStatus.FAILED,
                                      f"'{' '.join(step[1:3])}' failed on a disposable SQLite database",
                                      "artisan migrate / migrate:rollback / migrate (disposable SQLite)",
                                      "\n".join(log), time.monotonic() - start)
            return GateResult("migrations", GateStatus.PASSED,
                              f"{len(new)} new migration(s) migrate, roll back and re-migrate on a disposable SQLite DB",
                              "artisan migrate / migrate:rollback / migrate (disposable SQLite)", "\n".join(log),
                              time.monotonic() - start)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    # --------------------------------------------------------- composer audit

    def _prepare_vendor(self, worktree: Path) -> GateResult:
        self.logger.info("composer.json/lock changed: installing dependencies into the worktree...")
        result = self.php.install_vendor(worktree)
        if result.ok:
            return GateResult("composer-install", GateStatus.PASSED, "dependencies installed from the changed lock file",
                              "composer install --no-scripts", required=True)
        return GateResult("composer-install", GateStatus.FAILED, "composer install failed for the changed lock file",
                          "composer install --no-scripts", result.combined()[-4000:])

    def _audit(self, directory: Path) -> dict[str, Any]:
        result = self.php.run(directory, ["composer", "audit", "--locked", "--format=json", "--no-interaction"],
                              network=True, timeout=300, mount_vendor=False)
        text = result.stdout
        try:
            data = json.loads(text[text.find("{"):])
        except (json.JSONDecodeError, ValueError):
            return {"ran": False, "output": result.combined()[-2000:]}
        ids = []
        for package, advisories in (data.get("advisories") or {}).items():
            items = advisories.values() if isinstance(advisories, dict) else advisories
            for adv in items:
                ids.append(f"{package}:{adv.get('advisoryId') or adv.get('cve') or adv.get('title')}")
        return {"ran": True, "advisories": sorted(ids)}

    def _composer_audit(self, worktree: Path, base_sha: str, files: list[ChangedFile]) -> GateResult:
        if not {f.path for f in files} & {"composer.json", "composer.lock"}:
            return GateResult("composer-audit", GateStatus.SKIPPED, "dependencies unchanged", required=False)
        base_dir = Path(tempfile.mkdtemp(prefix="ai-agent-audit-"))
        try:
            for name in ("composer.json", "composer.lock"):
                content = self.git.git(["show", f"{base_sha}:{name}"], cwd=worktree, check=False).stdout
                (base_dir / name).write_text(content)
            base = self._audit(base_dir)
            now = self._audit(worktree)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)
        if not now.get("ran"):
            return self._unavailable("composer-audit", "composer audit could not run (network required)",
                                     "composer audit --locked", now.get("output", ""))
        new = sorted(set(now["advisories"]) - set(base.get("advisories") or []))
        status = GateStatus.FAILED if new else GateStatus.PASSED
        return GateResult("composer-audit", status,
                          f"{len(now['advisories'])} advisories ({len(new)} new vs base)", "composer audit --locked",
                          "\n".join(new))

    # ---------------------------------------------------------------- utils

    def _unavailable(self, name: str, summary: str, command: str = "", details: str = "") -> GateResult:
        return GateResult(name, GateStatus.UNAVAILABLE, summary, command, details,
                          required=self.gates.on_unavailable == "fail")


def format_report(report: QualityReport) -> str:
    lines = []
    for r in report.results:
        mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "unavailable": "N/A "}[r.status.value]
        lines.append(f"- [{mark}] {r.name}: {r.summary}" + ("" if r.required else " (non-blocking)"))
    return "\n".join(lines)
