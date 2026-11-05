import unittest

from helpers import make_config

from models import UnsafeEnvironmentError
from safety import (
    ChangedFile, assert_safe_branch, environment_problems, find_secrets, human_required_reasons, migration_up_body,
    redact, scan_diff,
)

CFG = make_config().safety


def scan(files, added=None, contents=None, **kw):
    return scan_diff(files, added or {}, contents or {}, CFG.forbidden_paths, kw.get("max_files", 40), kw.get("max_lines", 2000))


class RedactionTest(unittest.TestCase):
    def test_tokens_and_keys_are_redacted(self):
        text = ("gh token ghp_" + "A" * 36 + " key sk-ant-api03-" + "x" * 30 +
                " APP_KEY=base64:" + "Q" * 44 + " Authorization: Bearer abcdefghijklmnopqrstuvwxyz123")
        out = redact(text)
        self.assertNotIn("ghp_", out)
        self.assertNotIn("sk-ant", out)
        self.assertNotIn("Q" * 44, out)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123", out)
        self.assertEqual(redact(""), "")


class SecretDetectionTest(unittest.TestCase):
    def test_hardcoded_credentials_detected(self):
        self.assertTrue(find_secrets(["MAIL_PASSWORD=sup3rS3cretValue"]))
        self.assertTrue(find_secrets(["'api_key' => 'AKIAABCDEFGHIJKLMNOP',"]))
        self.assertTrue(find_secrets(["-----BEGIN RSA PRIVATE KEY-----"]))

    def test_placeholders_and_env_calls_are_fine(self):
        self.assertEqual(find_secrets(["MAIL_PASSWORD=null", "'password' => env('DB_PASSWORD'),",
                                       "DB_PASSWORD=", "$request->validate(['password' => 'required|min:8'])",
                                       "'password' => Hash::make($data['password']),",
                                       "'password' => 'password_confirmation'"]), [])


class HumanRequiredTest(unittest.TestCase):
    P = CFG.human_required_patterns

    def test_credential_rotation_detected(self):
        self.assertTrue(human_required_reasons("Rotate exposed credentials and APP_KEY", "", [], self.P, []))

    def test_only_title_and_criteria_are_matched(self):
        body = "## Implementation guidance\n- Rotate the APP_KEY if needed\n\n## Acceptance criteria\n- [ ] Code fixed"
        self.assertEqual(human_required_reasons("Fix OTP", body, [], self.P, []), [])
        body2 = "## Acceptance criteria\n- [ ] Exposed mail credential is revoked at the provider"
        self.assertTrue(human_required_reasons("Fix mail", body2, [], self.P, []))

    def test_session_revocation_is_code(self):
        self.assertEqual(human_required_reasons("Revoke sessions and make reset tokens single-use", "", [], self.P, []), [])

    def test_label(self):
        self.assertTrue(human_required_reasons("x", "", ["Needs-Human"], self.P, ["needs-human"]))


class DiffScanTest(unittest.TestCase):
    def test_clean_change_passes(self):
        result = scan([ChangedFile("app/Http/Controllers/A.php", "M", 5, 2), ChangedFile("tests/Feature/ATest.php", "A", 30, 0)])
        self.assertTrue(result.ok, result.blocking)

    def test_env_files_forbidden_but_example_allowed(self):
        self.assertFalse(scan([ChangedFile(".env", "M", 1, 1)]).ok)
        self.assertFalse(scan([ChangedFile(".env.production", "A", 1, 0)]).ok)
        self.assertTrue(scan([ChangedFile(".env.example", "M", 1, 1)]).ok)

    def test_existing_migration_edit_or_delete_blocked(self):
        self.assertFalse(scan([ChangedFile("database/migrations/2024_01_01_create_users.php", "M", 1, 1)]).ok)
        self.assertFalse(scan([ChangedFile("database/migrations/2024_01_01_create_users.php", "D", 0, 9)]).ok)

    def test_new_safe_migration_allowed_destructive_blocked(self):
        safe = ("<?php return new class { public function up() { Schema::table('users', fn($t) => $t->timestamp('otp_expires_at')->nullable()); }"
                " public function down() { Schema::table('users', fn($t) => $t->dropColumn('otp_expires_at')); } };")
        path = "database/migrations/2026_01_01_add_otp.php"
        self.assertTrue(scan([ChangedFile(path, "A", 10, 0)], contents={path: safe}).ok)
        destructive = "<?php return new class { public function up() { Schema::dropIfExists('videos'); } public function down() {} };"
        self.assertFalse(scan([ChangedFile(path, "A", 10, 0)], contents={path: destructive}).ok)

    def test_migration_up_body_excludes_down(self):
        src = "function up() { create(); } function down() { Schema::drop('x'); }"
        self.assertNotIn("drop", migration_up_body(src))

    def test_disabling_tests_blocked(self):
        self.assertFalse(scan([ChangedFile("tests/Feature/ATest.php", "D", 0, 50)]).ok)
        result = scan([ChangedFile("tests/Feature/ATest.php", "M", 1, 0)],
                      added={"tests/Feature/ATest.php": ["        $this->markTestSkipped('flaky');"]})
        self.assertFalse(result.ok)

    def test_secret_in_added_lines_blocked(self):
        result = scan([ChangedFile("config/mail.php", "M", 1, 0)], added={"config/mail.php": ["'password' => 'hunter2hunter2',"]})
        self.assertFalse(result.ok)

    def test_size_limits(self):
        files = [ChangedFile(f"app/F{i}.php", "M", 1, 1) for i in range(41)]
        self.assertFalse(scan(files).ok)
        self.assertFalse(scan([ChangedFile("app/A.php", "M", 3000, 0)]).ok)

    def test_empty_diff_blocked(self):
        self.assertFalse(scan([]).ok)


class BranchProtectionTest(unittest.TestCase):
    def test_protected_and_foreign_branches_refused(self):
        for branch in ("main", "staging", "development", "feature/x", ""):
            with self.assertRaises(UnsafeEnvironmentError):
                assert_safe_branch(branch, CFG.protected_branches)
        with self.assertRaises(UnsafeEnvironmentError):
            assert_safe_branch("ai/fix-issue-5-x", CFG.protected_branches, issue_number=6)
        assert_safe_branch("ai/fix-issue-5-x", CFG.protected_branches, issue_number=5)

    def test_production_environment_detected(self):
        self.assertTrue(environment_problems({"APP_ENV": "production"}))
        self.assertTrue(environment_problems({"DB_HOST": "prod-db.internal"}))
        self.assertEqual(environment_problems({"APP_ENV": "local"}), [])


class ConfigSafetyTest(unittest.TestCase):
    def test_unsafe_settings_rejected(self):
        from config import ConfigError, _apply, validate
        for section, key, value in [("pull_requests", "auto_merge", True), ("pull_requests", "draft", False),
                                    ("safety", "allow_production_deployment", True), ("execution", "allow_parallel", True),
                                    ("safety", "require_human_approval_for_destructive_operations", False)]:
            config = make_config()
            _apply(config, {section: {key: value}}, "config")
            with self.assertRaises(ConfigError, msg=f"{section}.{key}"):
                validate(config)

    def test_unknown_key_rejected(self):
        from config import ConfigError, _apply
        with self.assertRaises(ConfigError):
            _apply(make_config(), {"safety": {"allow_everything": True}}, "config")

    def test_example_config_loads(self):
        from config import AGENT_DIR, load_config
        config = load_config(str(AGENT_DIR / "config.example.yaml"))
        self.assertIsNone(config.execution.default_limit)
        self.assertEqual(config.github.organization, "Eghtanemm")


if __name__ == "__main__":
    unittest.main()
