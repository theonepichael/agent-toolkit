#!/usr/bin/env python3
"""Tests for link_drift_check.py. Run with: python3 test_link_drift_check.py

Deliberately dependency-free stdlib unittest, like its siblings in this
directory, so the tool stays testable on a machine that has never run
`uv sync`. Every audit call is faked -- nothing here shells out.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-link-drift-check-"))
        self._env_patch = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmpdir)})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

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


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-link-drift-cache-"))
        self.cache_dir = self.tmpdir / "cache"
        self.cache_file = (
            self.cache_dir / "agent-toolkit" / "link-drift-check-cache.json"
        )
        self._env_patch = patch.dict(
            os.environ, {"XDG_CACHE_HOME": str(self.cache_dir)}
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cache_roundtrip_avoids_second_audit(self) -> None:
        audit_calls = 0

        def counting_audit(
            *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal audit_calls
            audit_calls += 1
            return subprocess.CompletedProcess(
                args=["install.py"], returncode=0, stdout=CLEAN_REPORT, stderr=""
            )

        buffer1 = io.StringIO()
        with redirect_stdout(buffer1):
            ldc.cmd_check(run_command=counting_audit)
        self.assertEqual(audit_calls, 1)

        buffer2 = io.StringIO()
        with redirect_stdout(buffer2):
            ldc.cmd_check(run_command=counting_audit)
        # Second run should hit cache and not call audit
        self.assertEqual(audit_calls, 1)

    def test_cache_drift_report_replayed_without_rerunning_audit(self) -> None:
        audit_calls = 0

        def counting_drift_audit(
            *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal audit_calls
            audit_calls += 1
            return subprocess.CompletedProcess(
                args=["install.py"], returncode=1, stdout=DRIFT_REPORT, stderr=""
            )

        buffer1 = io.StringIO()
        with redirect_stdout(buffer1):
            ldc.cmd_check(run_command=counting_drift_audit)
        self.assertEqual(audit_calls, 1)
        self.assertIn("wrong-target (1)", buffer1.getvalue())

        buffer2 = io.StringIO()
        with redirect_stdout(buffer2):
            ldc.cmd_check(run_command=counting_drift_audit)
        # Second run should hit cache and replay output
        self.assertEqual(audit_calls, 1)
        self.assertIn("wrong-target (1)", buffer2.getvalue())

    def test_corrupted_cache_falls_back_gracefully(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text("not json!!!")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ldc.cmd_check(run_command=fake_audit(0, CLEAN_REPORT))
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
