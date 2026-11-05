"""CLI behaviour: argument handling, dry-run makes no changes, default run is unlimited."""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_config, make_issue

import main
from models import FieldOption, ProjectField, ProjectSchema
from test_engine import FakeProcessor


class FakeProjectClient:
    def __init__(self, issues):
        self.issues = {i.number: i for i in issues}
        self.mutations = 0
        self._schema = ProjectSchema("PVT", 1, "Roadmap", "", "https://x", {
            "Status": ProjectField("F", "Status", "SINGLE_SELECT",
                                   [FieldOption(n, n) for n in ("Backlog", "In progress", "In review", "Done")])})

    def schema(self, refresh=False):
        return self._schema

    def fetch_issues(self):
        return list(self.issues.values())

    def set_status(self, *a, **k):
        self.mutations += 1


class ArgsTest(unittest.TestCase):
    def test_defaults_are_unlimited(self):
        args = main.parse_args([])
        self.assertIsNone(args.limit)
        self.assertFalse(args.once)

    def test_scope_flags_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.parse_args(["--once", "--limit", "3"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.parse_args(["--limit", "0"])

    def test_overrides(self):
        config = make_config()
        main.apply_overrides(config, main.parse_args(["--max-turns", "9", "--timeout", "99", "--max-repair-attempts", "1",
                                                      "--model", "sonnet"]))
        self.assertEqual((config.claude.max_turns, config.claude.timeout, config.claude.max_repair_attempts,
                          config.claude.model), (9, 99, 1, "sonnet"))


class MainFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.project = FakeProjectClient([make_issue(i, "P2", repo="acme/app") for i in range(1, 6)]
                                         + [make_issue(9, "P1", blocked=[(1, "OPEN")])])
        self.env = mock.Mock(repository="acme/app", base_branch="main", project=self.project, warnings=[])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, argv, processor_factory=None):
        out = io.StringIO()
        with mock.patch.object(main, "load_config", return_value=self.config), \
             mock.patch.object(main, "preflight", return_value=self.env), \
             mock.patch.object(main, "build_processor", side_effect=processor_factory or (lambda *a, **k: None)), \
             contextlib.redirect_stdout(out):
            code = main.main(argv)
        return code, out.getvalue()

    def test_dry_run_changes_nothing(self):
        code, out = self.run_main(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("PROPOSED EXECUTION ORDER (5 eligible)", out)
        self.assertIn("blocked by unresolved #1", out)
        self.assertFalse(self.config.state_dir.exists())  # no state, logs, locks or reports written
        self.assertEqual(self.project.mutations, 0)

    def test_default_run_processes_whole_backlog_and_reports(self):
        holder = {}

        def factory(config, env, state, logger, run_id, no_pr):
            holder["proc"] = FakeProcessor(self.project, state, run_id)
            return holder["proc"]

        code, out = self.run_main([], factory)
        self.assertEqual(code, 0)
        self.assertEqual(holder["proc"].order, [1, 2, 3, 4, 5])
        self.assertIn("Draft PRs created: 5", out)
        self.assertTrue(list((self.config.state_dir / "reports").glob("run-*.md")))

    def test_once_processes_one(self):
        holder = {}

        def factory(config, env, state, logger, run_id, no_pr):
            holder["proc"] = FakeProcessor(self.project, state, run_id)
            return holder["proc"]

        self.run_main(["--once"], factory)
        self.assertEqual(holder["proc"].order, [1])

    def test_systemic_failure_exit_code(self):
        def factory(config, env, state, logger, run_id, no_pr):
            return FakeProcessor(self.project, state, run_id, systemic=1)

        code, out = self.run_main([], factory)
        self.assertEqual(code, main.EXIT_SYSTEMIC)
        self.assertIn("SYSTEMIC ERROR", out)


if __name__ == "__main__":
    unittest.main()
