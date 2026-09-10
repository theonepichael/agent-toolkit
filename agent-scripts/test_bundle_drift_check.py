#!/usr/bin/env python3
"""Tests for bundle_drift_check.py. Run with: python3 test_bundle_drift_check.py"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import bundle_drift_check

pytestmark = pytest.mark.allow_real_subprocess


def git_env(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": str(repo / ".git-test-home"),
            "XDG_CONFIG_HOME": str(repo / ".git-test-xdg"),
        }
    )
    return env


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(repo),
    )
    return result.stdout.strip()


class BundleDriftCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "bundle-repo"
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
            patch.object(bundle_drift_check, "REPO", self.repo),
            patch.object(bundle_drift_check, "STATE_DIR", self.state_dir),
            patch.object(bundle_drift_check, "MARKER", self.marker),
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
            bundle_drift_check.cmd_check()
        return out.getvalue().strip()

    def test_no_marker_prints_nothing(self) -> None:
        self.assertEqual(self.run_check(), "")

    def test_marker_matches_head_prints_nothing(self) -> None:
        head = git(self.repo, "rev-parse", "HEAD")
        with patch("sys.stdout", io.StringIO()):
            bundle_drift_check.cmd_mark(head)
        self.assertEqual(self.run_check(), "")

    def test_head_ahead_of_marker_reports_count(self) -> None:
        base = git(self.repo, "rev-parse", "HEAD")
        bundle_drift_check.cmd_mark(base)
        self.commit("second")
        self.commit("third")
        out = self.run_check()
        self.assertIn("2 commit(s) ahead", out)
        self.assertIn(base[:7], out)

    def test_mark_defaults_to_current_head(self) -> None:
        head = git(self.repo, "rev-parse", "HEAD")
        out = io.StringIO()
        with patch("sys.stdout", out):
            bundle_drift_check.cmd_mark(None)
        self.assertEqual(self.marker.read_text().strip(), head)
        self.assertIn(head[:7], out.getvalue())

    def test_missing_repo_prints_nothing(self) -> None:
        shutil.rmtree(self.repo)
        self.assertEqual(self.run_check(), "")

    def test_verbosity_flags_parse_after_every_leaf_subcommand(self) -> None:
        # A leaf added later without an entry here silently loses coverage.
        for cmd in ("check", "mark"):
            args = bundle_drift_check.build_parser().parse_args([cmd, "-q"])
            self.assertTrue(args.quiet)
            self.assertFalse(args.verbose)


class BundleDriftCheckDefaultRootTestCase(unittest.TestCase):
    def test_repo_default_derives_from_script_location_not_home(self) -> None:
        # Regression: REPO used to be hardcoded to a fixed home-anchored
        # checkout path, which breaks the moment the checkout has any other
        # name (e.g. a worktree, or the `~/.` rename a GitHub-blocked work
        # machine's zip-based transfer workflow produces). It must be derived
        # from the script's own location instead, same convention install.py
        # already uses.
        script_path = Path(bundle_drift_check.__file__).resolve()
        expected_root = script_path.parents[1]
        self.assertEqual(bundle_drift_check.REPO, expected_root)


if __name__ == "__main__":
    unittest.main()
