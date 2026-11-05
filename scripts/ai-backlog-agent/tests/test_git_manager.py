import unittest
from pathlib import Path

from helpers import TempRepo, git

from git_manager import GitManager, branch_name, parse_remote_url, slugify
from models import IssueFailure, UnsafeEnvironmentError
from process import CommandRunner
from worktree_manager import WorktreeManager

PROTECTED = ["main", "master", "staging", "development"]


class NamingTest(unittest.TestCase):
    def test_branch_naming(self):
        self.assertEqual(branch_name(42, "[SECURITY][HIGH] Fix OTP expiration and verification"),
                         "ai/fix-issue-42-fix-otp-expiration-verification")
        self.assertEqual(branch_name(7, "Ünïcödé — weird!!! title"), "ai/fix-issue-7-unicode-weird-title")
        self.assertEqual(branch_name(1, "[X]"), "ai/fix-issue-1-issue")
        self.assertLessEqual(len(slugify("word " * 50)), 40)

    def test_remote_url_parsing(self):
        self.assertEqual(parse_remote_url("git@github.com:Eghtanemm/Eghtanem-Backend.git"), "Eghtanemm/Eghtanem-Backend")
        self.assertEqual(parse_remote_url("https://github.com/acme/app"), "acme/app")
        self.assertIsNone(parse_remote_url("https://gitlab.com/acme/app.git"))


class GitWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.git = GitManager(CommandRunner(), self.repo.root, "origin", PROTECTED)
        self.wt = WorktreeManager(self.git, self.repo.tmp / "worktrees", "acme/app")
        self.base = self.git.resolve("origin/main")

    def tearDown(self):
        self.repo.cleanup()

    def test_create_isolated_worktree_from_base(self):
        branch = branch_name(5, "Fix thing")
        path = self.wt.create(5, branch, self.base)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.git.current_branch(path), branch)
        self.assertEqual(self.git.head(path), self.base)
        self.assertNotEqual(path.resolve().parent, self.repo.root.resolve())
        # main checkout untouched
        self.assertEqual(self.git.current_branch(self.repo.root), "main")

    def test_existing_worktree_or_branch_is_not_overwritten(self):
        branch = branch_name(5, "Fix thing")
        path = self.wt.create(5, branch, self.base)
        (path / "app" / "Thing.php").write_text("<?php // user work\n")
        with self.assertRaises(IssueFailure):
            self.wt.create(5, branch, self.base)
        self.assertEqual((path / "app" / "Thing.php").read_text(), "<?php // user work\n")
        git("branch", "ai/fix-issue-6-other", cwd=self.repo.root)
        with self.assertRaises(IssueFailure):
            self.wt.create(6, "ai/fix-issue-6-other", self.base)

    def test_refuses_protected_or_mismatched_branch(self):
        with self.assertRaises(UnsafeEnvironmentError):
            self.wt.create(5, "main", self.base)
        with self.assertRaises(UnsafeEnvironmentError):
            self.wt.create(5, "ai/fix-issue-6-x", self.base)

    def test_changed_files_commit_and_push(self):
        branch = branch_name(5, "Fix thing")
        path = self.wt.create(5, branch, self.base)
        (path / "app" / "Thing.php").write_text("<?php\n\nclass Thing { public int $x = 1; }\n")
        (path / "tests" / "NewTest.php").write_text("<?php\n// new\n")
        (path / "vendor").mkdir()  # ignored, must not show up
        self.git.stage_all(path)
        files = {f.path: f for f in self.git.changed_files(path, self.base)}
        self.assertEqual(set(files), {"app/Thing.php", "tests/NewTest.php"})
        self.assertEqual(files["tests/NewTest.php"].status, "A")
        added = self.git.added_lines(path, self.base)
        self.assertIn("// new", added["tests/NewTest.php"])
        sha = self.git.commit(path, "fix(#5): fix thing", 5)
        self.assertEqual(self.git.head(path), sha)
        self.git.push(path, branch, 5)
        self.assertEqual(self.git.remote_sha(branch), sha)
        self.assertEqual(self.git.remote_branches_for_issue(5), [branch])
        self.assertEqual(self.git.remote_branches_for_issue(55), [])
        # remote main untouched
        self.assertEqual(self.git.remote_sha("main"), self.base)

    def test_push_refuses_wrong_branch(self):
        branch = branch_name(5, "Fix thing")
        path = self.wt.create(5, branch, self.base)
        with self.assertRaises(UnsafeEnvironmentError):
            self.git.push(path, "main", 5)
        with self.assertRaises(UnsafeEnvironmentError):
            self.git.push(path, "ai/fix-issue-6-x", 6)

    def test_cleanup_keeps_dirty_worktree(self):
        path = self.wt.create(5, branch_name(5, "x"), self.base)
        (path / "app" / "Thing.php").write_text("changed")
        self.assertFalse(self.wt.cleanup(path))
        self.assertTrue(path.exists())

    def test_cleanup_removes_committed_worktree_but_keeps_branch(self):
        branch = branch_name(5, "x")
        path = self.wt.create(5, branch, self.base)
        (path / "app" / "Thing.php").write_text("changed")
        self.git.stage_all(path)
        self.git.commit(path, "fix(#5): x", 5)
        self.assertTrue(self.wt.cleanup(path))
        self.assertFalse(path.exists())
        self.assertTrue(self.git.local_branch_exists(branch))

    def test_discard_pristine_removes_branch_but_not_work(self):
        branch = branch_name(5, "x")
        path = self.wt.create(5, branch, self.base)
        self.assertTrue(self.wt.discard_if_pristine(path, branch, self.base))
        self.assertFalse(self.git.local_branch_exists(branch))
        path = self.wt.create(5, branch, self.base)
        (path / "new.txt").write_text("work")
        self.assertFalse(self.wt.discard_if_pristine(path, branch, self.base))
        self.assertTrue(path.exists())

    def test_archive_moves_previous_attempt_aside(self):
        branch = branch_name(5, "x")
        path = self.wt.create(5, branch, self.base)
        (path / "new.txt").write_text("work")
        archived = self.wt.archive(5, branch, path, "abc")
        self.assertFalse(self.git.local_branch_exists(branch))
        self.assertTrue(self.git.local_branch_exists(archived))
        self.assertTrue((self.wt.root / "archive-issue-5-abc" / "new.txt").exists())
        # a fresh attempt can now be created
        self.wt.create(5, branch, self.base)

    def test_detached_scratch_worktree(self):
        path = self.wt.create_detached("base", self.base, "baseline")
        self.assertTrue(path.exists())
        self.wt.remove_detached(path)
        self.assertFalse(path.exists())

    def test_integrity_check(self):
        self.assertEqual(self.git.integrity_check(), [])
        common = Path(git("rev-parse", "--git-common-dir", cwd=self.repo.root))
        common = common if common.is_absolute() else self.repo.root / common
        (common / "MERGE_HEAD").write_text(self.base)
        self.assertTrue(self.git.integrity_check())

    def test_detect_repository_from_remote(self):
        git("remote", "set-url", "origin", "git@github.com:acme/app.git", cwd=self.repo.root)
        self.assertEqual(self.git.detect_repository(), "acme/app")


if __name__ == "__main__":
    unittest.main()
