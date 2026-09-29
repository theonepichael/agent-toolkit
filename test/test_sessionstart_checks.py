#!/usr/bin/env python3
"""Tests for sessionstart_checks.py — the SessionStart hook fan-out.

Run with: python3 test_sessionstart_checks.py  (or via pytest)

The wrapper's contract: outputs come back in the original list order
(regardless of completion order), a failing or hanging entry never aborts
the remaining ones, and the hang-guard bounds entries that would otherwise
block session start forever. Tests drive run_checks() with tiny shell
commands rather than the real CHECKS list — the real list mutates state
(e.g. grill's --consume) and must never run under test.
"""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
from pytest_shim import pytest
import sessionstart_checks  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # real bash for echo/sleep/exit


class RunChecksTestCase(unittest.TestCase):
    def test_output_in_list_order_not_completion_order(self) -> None:
        # The first command is slow, the second instant — a completion-order
        # implementation would swap them.
        out = sessionstart_checks.run_checks(
            [("echo first; sleep 0.3", 5), ("echo second", 5)]
        )
        self.assertIn("first\n", out)
        self.assertLess(out.index("first"), out.index("second"))

    def test_failing_check_does_not_abort_the_rest(self) -> None:
        out = sessionstart_checks.run_checks(
            [("echo before; exit 3", 5), ("echo alive", 5)]
        )
        self.assertIn("before\n", out)
        self.assertIn("alive\n", out)

    def test_stderr_is_included(self) -> None:
        out = sessionstart_checks.run_checks([("echo out; echo err >&2", 5)])
        self.assertIn("out\n", out)
        self.assertIn("err\n", out)

    def test_hang_guard_kills_and_reports(self) -> None:
        # A command that prints then hangs: the guard must bound it, keep
        # the partial output, and append a note naming the guard.
        out = sessionstart_checks.run_checks([("echo partial; sleep 30", 1)])
        self.assertIn("partial\n", out)
        self.assertIn("exceeded 1s guard", out)

    def test_empty_list_returns_empty(self) -> None:
        self.assertEqual(sessionstart_checks.run_checks([]), "")


class ForeignScriptTestCase(unittest.TestCase):
    """Entries naming an origin-repo script under ~/.claude/scripts/. HOME is
    the test sandbox, so these create and remove files there, never in the
    real ~/.claude."""

    def setUp(self) -> None:
        self.scripts = Path.home() / ".claude" / "scripts"
        self.scripts.mkdir(parents=True, exist_ok=True)
        self.created: list[Path] = []

    def tearDown(self) -> None:
        for path in self.created:
            path.unlink(missing_ok=True)

    def _script(self, name: str, body: str) -> None:
        path = self.scripts / name
        path.write_text(body)
        self.created.append(path)

    @pytest.mark.regression(
        "sessionstart-retired-foreign-script-is-silent",
        "AssertionError: '' != '[sessionstart] ~/.claude/scripts/retired_[115 chars]py\\n'",
    )
    def test_dangling_foreign_script_link_prints_notice(self) -> None:
        # The owning repo installs these as symlinks into its checkout; when
        # it deletes the source file the link dangles, and 2>/dev/null used
        # to swallow python's "can't open file" so the check printed nothing.
        link = self.scripts / "retired_activity_zz.py"
        link.symlink_to(self.scripts / "gone_source_zz.py")
        self.created.append(link)
        out = sessionstart_checks.run_checks(
            [("python3 ~/.claude/scripts/retired_activity_zz.py 2>/dev/null", 5)]
        )
        self.assertEqual(
            out,
            "[sessionstart] ~/.claude/scripts/retired_activity_zz.py is a broken"
            " link — its owning repo may have retired it; drop its CHECKS line"
            " in sessionstart_checks.py\n",
        )

    def test_absent_foreign_script_is_silent(self) -> None:
        # Never installed here (a work profile excluding it, or no origin
        # repo at all): a normal setup, not a retirement — no notice.
        out = sessionstart_checks.run_checks(
            [("python3 ~/.claude/scripts/never_installed_zz.py 2>/dev/null", 5)]
        )
        self.assertEqual(out, "")

    def test_existing_foreign_script_runs_with_stderr_suppressed(self) -> None:
        self._script(
            "present_activity_zz.py",
            "import sys\nprint('activity ok')\nprint('noise', file=sys.stderr)\n",
        )
        out = sessionstart_checks.run_checks(
            [("python3 ~/.claude/scripts/present_activity_zz.py 2>/dev/null", 5)]
        )
        self.assertEqual(out, "activity ok\n")

    def test_existing_foreign_script_that_fails_behaves_as_before(self) -> None:
        self._script(
            "broken_activity_zz.py",
            "import sys\nprint('partial')\nprint('boom', file=sys.stderr)\nsys.exit(2)\n",
        )
        out = sessionstart_checks.run_checks(
            [("python3 ~/.claude/scripts/broken_activity_zz.py 2>/dev/null", 5)]
        )
        self.assertEqual(out, "partial\n")

    def test_toolkit_script_paths_are_not_checked(self) -> None:
        # Only origin-repo scripts get the existence check; a toolkit script
        # keeps its own failure handling (e.g. `|| echo` fallbacks).
        self.assertIsNone(
            sessionstart_checks.foreign_script(
                "python3 ~/.agent-toolkit/scripts/nope.py || echo fallback"
            )
        )
        self.assertEqual(
            sessionstart_checks.foreign_script(
                "python3 ~/.claude/scripts/watchcommit_activity.py 2>/dev/null"
            ),
            "~/.claude/scripts/watchcommit_activity.py",
        )


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
