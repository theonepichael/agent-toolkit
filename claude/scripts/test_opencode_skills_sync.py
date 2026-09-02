#!/usr/bin/env python3
"""Tests for ../../scripts/opencode_skills_sync.py.

Uses real temporary git repos rather than mocking subprocess — this
script's whole job is mirroring files and committing them, so a scenario
test against real repos catches what a mocked unit test would miss.
"""

import ast
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import opencode_skills_sync  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess

MODULE_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "opencode_skills_sync.py"
)


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


class MirrorTickTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ocss-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "source"
        self.source.mkdir()
        self.dest = self.tmp / "dest-worktree"
        init_repo(self.dest)

    def live(self, *parts: str) -> Path:
        return self.dest / "opencode" / "skills-live" / Path(*parts)

    def make_skill(self, name: str, files: dict[str, str]) -> Path:
        skill_dir = self.source / name
        skill_dir.mkdir()
        for rel, content in files.items():
            (skill_dir / rel).write_text(content)
        return skill_dir

    def test_copies_uncurated_skill_with_real_skill_md(self) -> None:
        self.make_skill("foo", {"SKILL.md": "hello"})
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertEqual(self.live("foo", "SKILL.md").read_text(), "hello")

    def test_skips_dir_that_is_itself_a_symlink(self) -> None:
        real = self.tmp / "real-curated"
        real.mkdir()
        (real / "SKILL.md").write_text("curated")
        (self.source / "curated").symlink_to(real, target_is_directory=True)
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertFalse(self.live("curated").exists())

    def test_skips_dir_whose_skill_md_is_a_symlink(self) -> None:
        target = self.tmp / "target-skill-md"
        target.write_text("curated")
        skill_dir = self.make_skill("curated2", {})
        (skill_dir / "SKILL.md").symlink_to(target)
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertFalse(self.live("curated2").exists())

    def test_skips_loose_non_directory_entry_without_raising(self) -> None:
        (self.source / "README.md").write_text("not a skill")
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertFalse(self.live("README.md").exists())

    def test_file_deleted_inside_surviving_skill_is_gone_after_next_tick(self) -> None:
        skill_dir = self.make_skill("bar", {"SKILL.md": "x", "extra.txt": "y"})
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertTrue(self.live("bar", "extra.txt").exists())

        (skill_dir / "extra.txt").unlink()
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertFalse(self.live("bar", "extra.txt").exists())
        self.assertTrue(self.live("bar", "SKILL.md").exists())

    def test_symlink_inside_copied_skill_is_preserved_not_dereferenced(self) -> None:
        linked_target = self.tmp / "linked-target.txt"
        linked_target.write_text("target content")
        skill_dir = self.make_skill("baz", {"SKILL.md": "x"})
        (skill_dir / "link.txt").symlink_to(linked_target)

        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertTrue(self.live("baz", "link.txt").is_symlink())

    def test_whole_skill_removed_from_source_is_removed_from_mirror(self) -> None:
        skill_dir = self.make_skill("gone", {"SKILL.md": "x"})
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertTrue(self.live("gone").exists())

        shutil.rmtree(skill_dir)
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertFalse(self.live("gone").exists())

    def test_skill_promoted_to_curated_has_stale_mirror_pruned(self) -> None:
        skill_dir = self.make_skill("promoted", {"SKILL.md": "x"})
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertTrue(self.live("promoted").exists())

        target = self.tmp / "promoted-skill-md-target"
        target.write_text("now curated")
        (skill_dir / "SKILL.md").unlink()
        (skill_dir / "SKILL.md").symlink_to(target)
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertFalse(self.live("promoted").exists())

    def test_prune_never_touches_anything_outside_skills_live(self) -> None:
        marker = self.dest / "opencode" / "marker.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("do not touch")
        top_marker = self.dest / "top-marker.txt"
        top_marker.write_text("do not touch either")

        self.make_skill("any", {"SKILL.md": "x"})
        opencode_skills_sync.mirror_tick(self.source, self.dest)

        self.assertTrue(marker.exists())
        self.assertTrue(top_marker.exists())

    def test_missing_source_directory_is_a_no_op_not_a_crash(self) -> None:
        missing_source = self.tmp / "does-not-exist"
        opencode_skills_sync.mirror_tick(missing_source, self.dest)
        self.assertTrue((self.dest / "opencode" / "skills-live").exists())

    def test_missing_source_directory_still_prunes_destination(self) -> None:
        self.make_skill("temp", {"SKILL.md": "x"})
        opencode_skills_sync.mirror_tick(self.source, self.dest)
        self.assertTrue(self.live("temp").exists())

        missing_source = self.tmp / "does-not-exist"
        opencode_skills_sync.mirror_tick(missing_source, self.dest)
        self.assertFalse(self.live("temp").exists())


class CommitStepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ocss-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "source"
        self.source.mkdir()
        self.dest = self.tmp / "dest-worktree"
        init_repo(self.dest)

    def head(self) -> str:
        return sh(self.dest, "git", "rev-parse", "HEAD").stdout.strip()

    def test_tick_with_changes_commits(self) -> None:
        (self.source / "one").mkdir()
        (self.source / "one" / "SKILL.md").write_text("x")
        before = self.head()
        committed = opencode_skills_sync.run_tick(self.source, self.dest)
        self.assertTrue(committed)
        self.assertNotEqual(before, self.head())

    def test_tick_with_no_changes_does_not_create_empty_commit(self) -> None:
        (self.source / "one").mkdir()
        (self.source / "one" / "SKILL.md").write_text("x")
        opencode_skills_sync.run_tick(self.source, self.dest)
        after_first = self.head()

        committed_again = opencode_skills_sync.run_tick(self.source, self.dest)
        self.assertFalse(committed_again)
        self.assertEqual(after_first, self.head())

    def test_commit_on_non_git_dest_logs_error_and_returns_false(self) -> None:
        non_git = self.tmp / "non-git"
        non_git.mkdir()
        with unittest.mock.patch("sys.stderr") as mock_stderr:
            committed = opencode_skills_sync.commit_if_changed(non_git)
        self.assertFalse(committed)
        mock_stderr.write.assert_called()


class PauseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ocss-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "source"
        self.source.mkdir()
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

    def head(self) -> str:
        return sh(self.dest, "git", "rev-parse", "HEAD").stdout.strip()

    def test_paused_tick_is_skipped(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.touch()
        (self.source / "one").mkdir()
        (self.source / "one" / "SKILL.md").write_text("x")

        before = self.head()
        committed = opencode_skills_sync.maybe_run_tick(self.source, self.dest)
        self.assertFalse(committed)
        self.assertEqual(before, self.head())
        self.assertFalse((self.dest / "opencode" / "skills-live" / "one").exists())

    def test_tick_resumes_once_pause_file_removed(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.touch()
        (self.source / "one").mkdir()
        (self.source / "one" / "SKILL.md").write_text("x")
        opencode_skills_sync.maybe_run_tick(self.source, self.dest)

        self.pause_file.unlink()
        committed = opencode_skills_sync.maybe_run_tick(self.source, self.dest)
        self.assertTrue(committed)
        self.assertTrue((self.dest / "opencode" / "skills-live" / "one").exists())


class NoRemoteCallsTestCase(unittest.TestCase):
    """Structural pin of 'this script never reaches a remote' — walks the
    module's AST for any call to the git() helper passing a literal
    push/fetch/pull argument, rather than a plain substring grep (which
    would false-positive on the module docstring's own prose about *not*
    pushing)."""

    def test_no_call_to_git_helper_passes_a_remote_verb(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        remote_verbs = {"push", "fetch", "pull"}
        found: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "git"
            ):
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value in remote_verbs
                    ):
                        found.append(arg.value)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
