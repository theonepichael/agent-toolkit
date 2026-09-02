#!/usr/bin/env python3
"""Tests for ./opencode_skills_sync_activity.py."""

import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import opencode_skills_sync  # noqa: E402
import opencode_skills_sync_activity  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess


def sh(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    sh(path, "git", "init", "-q", "-b", "opencode-skills-live")
    sh(path, "git", "config", "user.email", "test@test.com")
    sh(path, "git", "config", "user.name", "test")
    (path / ".gitkeep").write_text("")
    sh(path, "git", "add", "-A")
    sh(path, "git", "commit", "-q", "-m", "seed")


class ReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ocss-activity-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = self.tmp / "dest-worktree"
        init_repo(self.dest)
        state_dir = self.tmp / "state"
        self.pause_file = state_dir / "paused"
        self.enterContext(
            unittest.mock.patch.object(opencode_skills_sync, "STATE_DIR", state_dir)
        )
        self.enterContext(
            unittest.mock.patch.object(
                opencode_skills_sync, "PAUSE_FILE", self.pause_file
            )
        )

    def test_no_snapshot_yet_reports_nothing_about_a_commit(self) -> None:
        report = opencode_skills_sync_activity.report(self.dest)
        self.assertNotIn("last snapshot", report)

    def test_reports_last_snapshot_after_a_daemon_commit(self) -> None:
        (self.dest / "opencode").mkdir()
        (self.dest / "opencode" / "marker.txt").write_text("x")
        sh(self.dest, "git", "add", "-A")
        sh(self.dest, "git", "commit", "-q", "-m", opencode_skills_sync.COMMIT_MESSAGE)

        report = opencode_skills_sync_activity.report(self.dest)
        self.assertIn("opencode-skills-sync: last snapshot", report)

    def test_reports_paused_state(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.touch()

        report = opencode_skills_sync_activity.report(self.dest)
        self.assertIn("opencode-skills-sync: paused", report)

    def test_missing_dest_worktree_reports_warning(self) -> None:
        missing = self.tmp / "does-not-exist"
        report = opencode_skills_sync_activity.report(missing)
        self.assertIn(
            f"opencode-skills-sync: mirror worktree missing at {missing}", report
        )
        self.assertIn("run git worktree add", report)

    def test_non_git_dest_worktree_reports_warning(self) -> None:
        non_git = self.tmp / "non-git-dir"
        non_git.mkdir(parents=True)
        report = opencode_skills_sync_activity.report(non_git)
        self.assertIn(
            f"opencode-skills-sync: mirror worktree missing at {non_git}", report
        )
        self.assertIn("run git worktree add", report)


if __name__ == "__main__":
    unittest.main()
