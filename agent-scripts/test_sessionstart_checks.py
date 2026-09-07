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

import pytest

sys.path.insert(0, str(Path(__file__).parent))
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


if __name__ == "__main__":
    unittest.main()
