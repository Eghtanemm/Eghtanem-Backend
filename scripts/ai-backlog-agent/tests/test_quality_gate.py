import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import make_config, ok, fail, quiet_logger

from models import GateStatus
from process import CommandResult
from quality_gate import QualityGate, format_report, parse_junit
from safety import ChangedFile


def junit(tests: dict[str, str | None]) -> str:
    cases = []
    for test_id, failure in tests.items():
        cls, name = test_id.split("::")
        inner = f"<failure>{failure}</failure>" if failure else ""
        cases.append(f'<testcase name="{name}" class="{cls}" file="x.php">{inner}</testcase>')
    return f'<?xml version="1.0"?><testsuites><testsuite name="all">{"".join(cases)}</testsuite></testsuites>'


class FakePhp:
    def __init__(self, junit_xml=None, pint_ok=True, available=True, phpstan=False, migrate_ok=True):
        self.junit_xml = junit_xml
        self.pint_ok = pint_ok
        self.available = available
        self.mode = "docker" if available else "unavailable"
        self.reason = "" if available else "no docker"
        self.phpstan = phpstan
        self.migrate_ok = migrate_ok
        self.commands = []

    def has_vendor_binary(self, name, worktree=None):
        return name != "phpstan" or self.phpstan

    def out_path(self, out_dir, name):
        return str(out_dir / name)

    def install_vendor(self, worktree):
        return ok()

    def run(self, worktree, command, *, out_dir=None, network=False, env=None, timeout=0, mount_vendor=None):
        self.commands.append((command, network, env))
        if "phpunit" in " ".join(command):
            if self.junit_xml is not None:
                (out_dir / "junit.xml").write_text(self.junit_xml)
            return CommandResult(command, 0, "OK (2 tests)", "", 1.0)
        if "pint" in " ".join(command):
            return ok() if (self.pint_ok or "--test" not in command) else fail("  ⨯ app/A.php")
        if "artisan" in command:
            return ok("migrated") if self.migrate_ok else fail("SQLSTATE error")
        if command[:2] == ["composer", "audit"]:
            return ok(json.dumps({"advisories": {}}))
        return ok()


class FakeState:
    def __init__(self):
        self.baselines = {}

    def get_baseline(self, sha, gate):
        return self.baselines.get((sha, gate))

    def save_baseline(self, sha, gate, data):
        self.baselines[(sha, gate)] = data


class FakeGit:
    def git(self, args, cwd=None, check=True):
        return ok("{}")


class QualityGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.files = [ChangedFile("app/A.php", "M", 3, 1), ChangedFile("tests/Feature/ATest.php", "A", 20, 0)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gate(self, php, state=None):
        return QualityGate(self.config, php, state or FakeState(), quiet_logger(), FakeGit())

    def test_parse_junit(self):
        parsed = parse_junit(junit({"A::ok": None, "A::bad": "boom"}))
        self.assertEqual(parsed["tests"], ["A::ok", "A::bad"])
        self.assertEqual(list(parsed["failed"]), ["A::bad"])

    def test_preexisting_failures_do_not_block(self):
        baseline = {"phpunit": {"ran": True, "tests": ["A::old", "A::ok"], "failed": {"A::old": "x"}}}
        php = FakePhp(junit({"A::old": "still failing", "A::ok": None, "A::new_test": None}))
        report = self.gate(php).run(self.tmp, "sha", self.files, baseline)
        phpunit = next(r for r in report.results if r.name == "phpunit")
        self.assertEqual(phpunit.status, GateStatus.PASSED)
        self.assertIn("1 pre-existing", phpunit.summary)
        self.assertTrue(report.passed)

    def test_new_failure_blocks(self):
        baseline = {"phpunit": {"ran": True, "tests": ["A::ok"], "failed": {}}}
        php = FakePhp(junit({"A::ok": "now broken"}))
        report = self.gate(php).run(self.tmp, "sha", self.files, baseline)
        phpunit = next(r for r in report.results if r.name == "phpunit")
        self.assertEqual(phpunit.status, GateStatus.FAILED)
        self.assertIn("A::ok", phpunit.details)
        self.assertFalse(report.passed)

    def test_phpunit_without_report_is_failure_not_pass(self):
        report = self.gate(FakePhp(junit_xml=None)).run(self.tmp, "sha", self.files, {})
        phpunit = next(r for r in report.results if r.name == "phpunit")
        self.assertEqual(phpunit.status, GateStatus.FAILED)

    def test_pint_runs_on_changed_php_files_only(self):
        php = FakePhp(junit({"A::ok": None}))
        self.gate(php).run(self.tmp, "sha", self.files + [ChangedFile("README.md", "M", 1, 1)], {})
        pint_cmds = [c for c, _, _ in php.commands if "vendor/bin/pint" in c]
        self.assertTrue(pint_cmds)
        for cmd in pint_cmds:
            self.assertNotIn("README.md", cmd)
            self.assertIn("app/A.php", cmd)

    def test_pint_failure_blocks(self):
        report = self.gate(FakePhp(junit({"A::ok": None}), pint_ok=False)).run(self.tmp, "sha", self.files, {})
        self.assertEqual(next(r for r in report.results if r.name == "pint").status, GateStatus.FAILED)
        self.assertFalse(report.passed)

    def test_phpstan_not_configured_is_skipped_not_passed(self):
        report = self.gate(FakePhp(junit({"A::ok": None}))).run(self.tmp, "sha", self.files, {})
        phpstan = next(r for r in report.results if r.name == "phpstan")
        self.assertEqual(phpstan.status, GateStatus.SKIPPED)
        self.assertFalse(phpstan.required)

    def test_unavailable_environment_blocks_by_default(self):
        report = self.gate(FakePhp(available=False)).run(self.tmp, "sha", self.files, {})
        self.assertFalse(report.passed)
        self.assertTrue(all(r.status == GateStatus.UNAVAILABLE for r in report.results))

    def test_unavailable_environment_allowed_by_policy_is_non_blocking(self):
        self.config.quality_gates.on_unavailable = "allow"
        report = self.gate(FakePhp(available=False)).run(self.tmp, "sha", self.files, {})
        self.assertTrue(report.passed)
        self.assertTrue(all(r.status == GateStatus.UNAVAILABLE for r in report.results))

    def test_new_migration_is_migrated_rolled_back_and_remigrated_on_sqlite(self):
        php = FakePhp(junit({"A::ok": None}))
        files = self.files + [ChangedFile("database/migrations/2026_01_01_x.php", "A", 20, 0)]
        report = self.gate(php).run(self.tmp, "sha", files, {})
        migrations = next(r for r in report.results if r.name == "migrations")
        self.assertEqual(migrations.status, GateStatus.PASSED)
        artisan = [(c, env) for c, _, env in php.commands if "artisan" in c]
        self.assertEqual([c[2] for c, _ in artisan], ["migrate", "migrate:rollback", "migrate"])
        self.assertTrue(all(env["DB_CONNECTION"] == "sqlite" for _, env in artisan))
        self.assertTrue(all(not net for c, net, _ in php.commands if "artisan" in c))

    def test_failing_migration_blocks(self):
        files = [ChangedFile("database/migrations/2026_01_01_x.php", "A", 20, 0)]
        report = self.gate(FakePhp(junit({"A::ok": None}), migrate_ok=False)).run(self.tmp, "sha", files, {})
        self.assertEqual(next(r for r in report.results if r.name == "migrations").status, GateStatus.FAILED)

    def test_composer_audit_only_when_dependencies_change(self):
        php = FakePhp(junit({"A::ok": None}))
        report = self.gate(php).run(self.tmp, "sha", self.files, {})
        self.assertEqual(next(r for r in report.results if r.name == "composer-audit").status, GateStatus.SKIPPED)
        report = self.gate(php).run(self.tmp, "sha", self.files + [ChangedFile("composer.lock", "M", 5, 5)], {})
        self.assertEqual(next(r for r in report.results if r.name == "composer-audit").status, GateStatus.PASSED)
        self.assertTrue(any(c[:2] == ["composer", "audit"] and net for c, net, _ in php.commands))

    def test_baseline_cached_per_sha(self):
        state = FakeState()
        php = FakePhp(junit({"A::ok": None}))
        gate = self.gate(php, state)
        first = gate.baseline(self.tmp, "sha1")
        self.assertTrue(first["phpunit"]["ran"])
        php.commands.clear()
        gate.baseline(self.tmp, "sha1")
        self.assertEqual(php.commands, [])

    def test_format_report(self):
        report = self.gate(FakePhp(junit({"A::ok": None}))).run(self.tmp, "sha", self.files, {})
        text = format_report(report)
        self.assertIn("[PASS] phpunit", text)
        self.assertIn("[SKIP] phpstan", text)


if __name__ == "__main__":
    unittest.main()
