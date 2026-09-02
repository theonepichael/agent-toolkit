#!/usr/bin/env python3
"""Tests for link_drift_check.py. Run with: python3 test_link_drift_check.py

Deliberately dependency-free stdlib unittest, like its siblings in this
directory, so the tool stays testable on a machine that has never run
`uv sync`. Every audit call is faked -- nothing here shells out.
"""

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import link_drift_check as ldc


def fake_audit(returncode: int, stdout: str = ""):
    """Stand in for subprocess.run, returning a canned audit result."""

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["install.py"], returncode=returncode, stdout=stdout, stderr=""
        )

    return run


def check_output(returncode: int, stdout: str = "") -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        ldc.cmd_check(run_command=fake_audit(returncode, stdout))
    return buffer.getvalue()


CLEAN_REPORT = """==> links.toml audit (read-only)
  136 of 136 entries checked — every applicable link is present, correct.
"""

# The real shape of the failure this hook exists for: a live link left
# pointing into a worktree after a hand-repoint for live verification.
DRIFT_REPORT = """==> links.toml audit (read-only)
  wrong-target (1):
    ~/.pi/agent/extensions/swarm-tool.ts — points at /home/u/dotfiles-wt/pi/extensions/swarm-tool.ts, but links.toml says /home/u/dotfiles/pi/extensions/swarm-tool.ts
⚠ 1 link problem(s) found — nothing was changed.
"""

TWO_BUCKET_REPORT = """==> links.toml audit (read-only)
  wrong-target (1):
    ~/.pi/agent/extensions/swarm-tool.ts — points somewhere else
  broken-source (2):
    ~/.claude/scripts/gone.py — links to a file that no longer exists
⚠ 3 link problem(s) found — nothing was changed.
"""


class CheckTests(unittest.TestCase):
    def test_clean_machine_prints_nothing(self) -> None:
        self.assertEqual(check_output(0, CLEAN_REPORT), "")

    def test_drift_names_the_bucket_and_the_full_audit_command(self) -> None:
        out = check_output(1, DRIFT_REPORT)
        self.assertIn("wrong-target (1)", out)
        self.assertIn("--check-links", out)

    def test_several_buckets_are_all_named(self) -> None:
        out = check_output(1, TWO_BUCKET_REPORT)
        self.assertIn("wrong-target (1)", out)
        self.assertIn("broken-source (2)", out)

    def test_indented_detail_lines_are_not_mistaken_for_buckets(self) -> None:
        """Only the bucket headers are echoed -- not the per-link detail under
        them, which would put a full path into every session's first screen."""
        out = check_output(1, DRIFT_REPORT)
        self.assertNotIn("/home/u/dotfiles-wt", out)

    def test_nonzero_exit_with_no_parsable_bucket_still_reports(self) -> None:
        out = check_output(1, "something unexpected\n")
        self.assertIn("see the full audit", out)

    def test_quiet_suppresses_the_note(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ldc.cmd_check(quiet=True, run_command=fake_audit(1, DRIFT_REPORT))
        self.assertEqual(buffer.getvalue(), "")

    def test_a_crashed_audit_stays_silent(self) -> None:
        """A broken checker must not itself become a session-start warning."""

        def explode(*_args: object, **_kwargs: object) -> object:
            raise OSError("no python")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ldc.cmd_check(run_command=explode)
        self.assertEqual(buffer.getvalue(), "")

    def test_timeout_stays_silent(self) -> None:
        def timeout(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="install.py", timeout=15)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ldc.cmd_check(run_command=timeout)
        self.assertEqual(buffer.getvalue(), "")


class ParserTests(unittest.TestCase):
    def test_check_is_the_default_subcommand(self) -> None:
        args = ldc.build_parser().parse_args([])
        self.assertIsNone(args.subcommand)

    def test_check_accepts_verbosity_flags(self) -> None:
        args = ldc.build_parser().parse_args(["check", "--quiet"])
        self.assertTrue(args.quiet)


if __name__ == "__main__":
    unittest.main()
