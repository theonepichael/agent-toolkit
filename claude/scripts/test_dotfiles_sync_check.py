#!/usr/bin/env python3
"""Tests for dotfiles_sync_check.py. Run with: python3 test_dotfiles_sync_check.py"""

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import dotfiles_sync_check

pytestmark = pytest.mark.allow_real_subprocess


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class DotfilesSyncCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "dotfiles"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "file.txt").write_text("v1")
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-q", "-m", "initial")

        self.state_dir = Path(self.tmpdir) / "state"
        self.marker = self.state_dir / "last-bundled-commit"

        self._patches = [
            patch.object(dotfiles_sync_check, "REPO", self.repo),
            patch.object(dotfiles_sync_check, "STATE_DIR", self.state_dir),
            patch.object(dotfiles_sync_check, "MARKER", self.marker),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def commit(self, message: str) -> str:
        (self.repo / "file.txt").write_text(message)
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def run_check(self) -> str:
        out = io.StringIO()
        with patch("sys.stdout", out):
            dotfiles_sync_check.cmd_check()
        return out.getvalue().strip()

    def test_no_marker_prints_nothing(self) -> None:
        self.assertEqual(self.run_check(), "")

    def test_marker_matches_head_prints_nothing(self) -> None:
        head = git(self.repo, "rev-parse", "HEAD")
        with patch("sys.stdout", io.StringIO()):
            dotfiles_sync_check.cmd_mark(head)
        self.assertEqual(self.run_check(), "")

    def test_head_ahead_of_marker_reports_count(self) -> None:
        base = git(self.repo, "rev-parse", "HEAD")
        dotfiles_sync_check.cmd_mark(base)
        self.commit("second")
        self.commit("third")
        out = self.run_check()
        self.assertIn("2 commit(s) ahead", out)
        self.assertIn(base[:7], out)

    def test_mark_defaults_to_current_head(self) -> None:
        head = git(self.repo, "rev-parse", "HEAD")
        out = io.StringIO()
        with patch("sys.stdout", out):
            dotfiles_sync_check.cmd_mark(None)
        self.assertEqual(self.marker.read_text().strip(), head)
        self.assertIn(head[:7], out.getvalue())

    def test_missing_repo_prints_nothing(self) -> None:
        shutil.rmtree(self.repo)
        self.assertEqual(self.run_check(), "")

    def test_verbosity_flags_parse_after_every_leaf_subcommand(self) -> None:
        # A leaf added later without an entry here silently loses coverage.
        for cmd in ("check", "mark"):
            args = dotfiles_sync_check.build_parser().parse_args([cmd, "-q"])
            self.assertTrue(args.quiet)
            self.assertFalse(args.verbose)


class DotfilesSyncCheckDefaultRootTestCase(unittest.TestCase):
    def test_repo_default_derives_from_script_location_not_home(self) -> None:
        # Regression: REPO used to be hardcoded to ~/dotfiles, which breaks
        # the moment the checkout has any other name (e.g. a worktree, or
        # the ~/.dotfiles rename a GitHub-blocked work machine's zip-based
        # transfer workflow produces). It must be derived from the script's
        # own location instead, same convention install.py already uses.
        script_path = Path(dotfiles_sync_check.__file__).resolve()
        expected_root = script_path.parents[2]
        self.assertEqual(dotfiles_sync_check.REPO, expected_root)


if __name__ == "__main__":
    unittest.main()
