#!/usr/bin/env python3
"""Tests for sync_from_dotfiles.py. Run with: python3 test_sync_from_dotfiles.py

Two tiers, per the plan this tool implements:

- Pure-function tests for the classification rule, blocklist/exclude
  subtraction, and conflict-set math — plain in-memory path sets, no git
  subprocess calls at all.
- A slower integration tier against synthetic throwaway git repos (built and
  torn down inside each test), exercising the real git-diffing and copy
  code paths. Marked ``allow_real_subprocess`` per test/AGENTS.md, since
  this tool's entire job is diffing two real external repos and the repo
  conftest.py blocks unmarked real subprocess calls.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sync_from_dotfiles as sfd


class ComputeCopySetTests(unittest.TestCase):
    def test_excludes_blocklist_and_exclude_entries(self) -> None:
        changed = frozenset({"a.py", sfd.BLOCKLIST[0], sfd.EXCLUDE[0], "b.py"})
        self.assertEqual(sfd.compute_copy_set(changed), frozenset({"a.py", "b.py"}))

    def test_is_derived_from_the_diff_not_a_fixed_list(self) -> None:
        changed = frozenset({"some/brand/new/path.py"})
        self.assertEqual(sfd.compute_copy_set(changed), changed)

    def test_empty_diff_is_empty_copy_set(self) -> None:
        self.assertEqual(sfd.compute_copy_set(frozenset()), frozenset())


class ComputeConflictSetTests(unittest.TestCase):
    def test_is_the_intersection(self) -> None:
        dotfiles_changed = frozenset({"a.py", "b.py", "c.py"})
        toolkit_changed = frozenset({"b.py", "c.py", "d.py"})
        self.assertEqual(
            sfd.compute_conflict_set(dotfiles_changed, toolkit_changed),
            frozenset({"b.py", "c.py"}),
        )

    def test_disjoint_changes_have_no_conflict(self) -> None:
        self.assertEqual(
            sfd.compute_conflict_set(frozenset({"a"}), frozenset({"b"})), frozenset()
        )

    def test_never_a_fixed_expected_value(self) -> None:
        # A repeatable tool's conflict set changes every run -- assert the
        # math, not a memorized 3-file answer from one historical run.
        dotfiles_changed = frozenset({"README.md", "links.toml", "new_thing.py"})
        toolkit_changed = frozenset({"README.md", "unrelated_toolkit_file.py"})
        self.assertEqual(
            sfd.compute_conflict_set(dotfiles_changed, toolkit_changed),
            frozenset({"README.md"}),
        )


class ClassifyConflictTests(unittest.TestCase):
    def test_interfaces_md_is_a_generated_artifact(self) -> None:
        self.assertEqual(sfd.classify_conflict("INTERFACES.md"), "generated_artifact")

    def test_skill_doc_globs_are_generated_artifacts(self) -> None:
        for path in (
            "claude/commands/spec.md",
            "pi/skills/spec/SKILL.md",
            "opencode/skills/second-opinion/SKILL.md",
            "copilot/skills/second-opinion/SKILL.md",
            "agy/skills/second-opinion/SKILL.md",
            "pi/prompts/spec.md",
            "opencode/command/spec.md",
            "templates/spec.md.tmpl",
            "claude/scripts/contract_fingerprints.json",
        ):
            self.assertEqual(sfd.classify_conflict(path), "generated_artifact", path)

    def test_unregistered_path_is_unclassified_by_default(self) -> None:
        # The safe default: a repeatable tool must never guess on a conflict
        # it has no verified rule for -- see CONFLICT_HANDLERS's docstring.
        self.assertEqual(sfd.classify_conflict("links.toml"), "unclassified")
        self.assertEqual(sfd.classify_conflict("README.md"), "unclassified")

    def test_registered_handler_path_is_handled(self) -> None:
        sfd.CONFLICT_HANDLERS["fixture/only.txt"] = lambda *_args: None
        try:
            self.assertEqual(sfd.classify_conflict("fixture/only.txt"), "handled")
        finally:
            del sfd.CONFLICT_HANDLERS["fixture/only.txt"]


class BlocklistShapeTests(unittest.TestCase):
    def test_blocklist_and_exclude_do_not_overlap(self) -> None:
        self.assertFalse(set(sfd.BLOCKLIST) & set(sfd.EXCLUDE))

    def test_blocklist_entries_are_unique(self) -> None:
        self.assertEqual(len(sfd.BLOCKLIST), len(set(sfd.BLOCKLIST)))


class StateFileTests(unittest.TestCase):
    def test_load_state_returns_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sfd.load_state(Path(tmp)))

    def test_write_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sfd.write_state(root, dotfiles_sha="abc123", toolkit_commit="def456")
            state = sfd.load_state(root)
            assert state is not None
            self.assertEqual(state["last_synced_dotfiles_sha"], "abc123")
            self.assertEqual(state["toolkit_commit"], "def456")
            self.assertIn("synced_at", state)

    def test_state_path_is_committed_not_ignored(self) -> None:
        gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".sync-state.json", gitignore)


class ResolveToolkitAnchorTests(unittest.TestCase):
    def test_uses_the_recorded_commit_when_present(self) -> None:
        anchor = sfd.resolve_toolkit_anchor(
            Path("/nonexistent"), {"toolkit_commit": "abc"}
        )
        self.assertEqual(anchor, "abc")

    @pytest.mark.allow_real_subprocess
    def test_ignores_a_blank_recorded_commit(self) -> None:
        # write_state() deliberately leaves toolkit_commit as None until a
        # wrapping caller fills it in after committing -- falling back must
        # not treat that None as a real anchor.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess_git_init_and_commit(root)
            anchor = sfd.resolve_toolkit_anchor(root, {"toolkit_commit": None})
            self.assertEqual(anchor, sfd.root_commit(root))


def subprocess_git_init_and_commit(repo: Path) -> None:
    """Real-subprocess helper shared by the integration tests below."""
    git(repo, "init", "-q", "-b", "main")
    (repo / ".keep").write_text("", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "root")


def git(repo: Path, *args: str) -> str:
    """Run a real git command with a fixed identity, no user config needed."""
    import os

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "sync-test",
        "GIT_AUTHOR_EMAIL": "sync-test@example.com",
        "GIT_COMMITTER_NAME": "sync-test",
        "GIT_COMMITTER_EMAIL": "sync-test@example.com",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def commit_file(repo: Path, relpath: str, content: str) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", relpath)
    git(repo, "commit", "-q", "-m", f"add {relpath}")


class SyntheticRepoIntegrationTests(unittest.TestCase):
    """Exercises real git diffing and copy code paths against throwaway repos.

    No real ~/dotfiles or ~/.claude state is touched -- both "dotfiles" and
    "toolkit" are throwaway repos under a temp directory, built and torn
    down inside this test.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dotfiles = Path(self.tmp.name) / "dotfiles"
        self.toolkit = Path(self.tmp.name) / "toolkit"
        self.dotfiles.mkdir()
        self.toolkit.mkdir()
        subprocess_git_init_and_commit(self.dotfiles)
        subprocess_git_init_and_commit(self.toolkit)

    @pytest.mark.allow_real_subprocess
    def test_copy_set_and_conflict_set_from_real_git_history(self) -> None:
        base = git(self.dotfiles, "rev-parse", "HEAD").strip()
        commit_file(self.dotfiles, "shared/new_thing.py", "print('hi')\n")
        tip = git(self.dotfiles, "rev-parse", "HEAD").strip()

        toolkit_anchor = git(self.toolkit, "rev-parse", "HEAD").strip()
        commit_file(self.toolkit, "toolkit_only.py", "x = 1\n")
        toolkit_head = git(self.toolkit, "rev-parse", "HEAD").strip()

        dotfiles_changed = sfd.changed_paths(self.dotfiles, base, tip)
        toolkit_changed = sfd.changed_paths(self.toolkit, toolkit_anchor, toolkit_head)
        self.assertEqual(dotfiles_changed, frozenset({"shared/new_thing.py"}))

        copy_set = sfd.compute_copy_set(dotfiles_changed)
        conflict_set = sfd.compute_conflict_set(dotfiles_changed, toolkit_changed)
        self.assertEqual(copy_set, frozenset({"shared/new_thing.py"}))
        self.assertEqual(conflict_set, frozenset())

        # BLOCKLIST is hardcoded against the real dotfiles repo, so every
        # entry is reported stale here -- this throwaway repo has none of
        # them. That noise is expected; assert the actual path under test
        # raised no problem of its own.
        problems = sfd.verify_invariants(self.dotfiles, base, tip, copy_set)
        self.assertFalse([p for p in problems if "shared/new_thing.py" in p], problems)

    @pytest.mark.allow_real_subprocess
    def test_verify_invariants_flags_a_stale_blocklist_entry(self) -> None:
        # BLOCKLIST names dotfiles paths that do not exist in this throwaway
        # dotfiles repo -- exactly the "typo/stale entry" case the check
        # exists to catch.
        base = git(self.dotfiles, "rev-parse", "HEAD").strip()
        tip = base
        problems = sfd.verify_invariants(self.dotfiles, base, tip, frozenset())
        self.assertTrue(any("BLOCKLIST entry" in p for p in problems), problems)

    @pytest.mark.allow_real_subprocess
    def test_verify_invariants_flags_an_unhandled_deletion(self) -> None:
        commit_file(self.dotfiles, "will_delete.py", "x = 1\n")
        base = git(self.dotfiles, "rev-parse", "HEAD").strip()
        (self.dotfiles / "will_delete.py").unlink()
        git(self.dotfiles, "add", "-A")
        git(self.dotfiles, "commit", "-q", "-m", "delete")
        tip = git(self.dotfiles, "rev-parse", "HEAD").strip()

        dotfiles_changed = sfd.changed_paths(self.dotfiles, base, tip)
        copy_set = sfd.compute_copy_set(dotfiles_changed)
        problems = sfd.verify_invariants(self.dotfiles, base, tip, copy_set)
        self.assertTrue(any("missing from dotfiles" in p for p in problems), problems)

    @pytest.mark.allow_real_subprocess
    def test_verify_invariants_flags_a_blocked_module_reference(self) -> None:
        commit_file(
            self.dotfiles,
            "claude/scripts/watchcommit_activity.py",
            "def record(): ...\n",
        )
        base = git(self.dotfiles, "rev-parse", "HEAD").strip()
        commit_file(
            self.dotfiles,
            "claude/scripts/uses_it.py",
            "import watchcommit_activity\n",
        )
        tip = git(self.dotfiles, "rev-parse", "HEAD").strip()

        dotfiles_changed = sfd.changed_paths(self.dotfiles, base, tip)
        copy_set = sfd.compute_copy_set(dotfiles_changed)
        self.assertEqual(copy_set, frozenset({"claude/scripts/uses_it.py"}))
        problems = sfd.verify_invariants(self.dotfiles, base, tip, copy_set)
        self.assertTrue(
            any("references blocklisted module" in p for p in problems), problems
        )

    @pytest.mark.allow_real_subprocess
    def test_apply_stages_new_files_before_generator_sweep_sees_them(self) -> None:
        """Regression test for the ordering defect found in the one-off run:
        a generator that enumerates git-TRACKED files silently omits a
        brand-new copied file unless that file is staged first."""
        base = git(self.dotfiles, "rev-parse", "HEAD").strip()
        commit_file(self.dotfiles, "brand_new_file.txt", "hello\n")
        tip = git(self.dotfiles, "rev-parse", "HEAD").strip()

        dotfiles_changed = sfd.changed_paths(self.dotfiles, base, tip)
        copy_set = sfd.compute_copy_set(dotfiles_changed)
        self.assertEqual(copy_set, frozenset({"brand_new_file.txt"}))

        generator = self.toolkit / "fake_generator.py"
        generator.write_text(
            "import subprocess\n"
            "from pathlib import Path\n"
            "tracked = subprocess.run(\n"
            "    ['git', 'ls-files'], capture_output=True, text=True, check=True\n"
            ").stdout\n"
            "Path('MANIFEST.txt').write_text(tracked)\n",
            encoding="utf-8",
        )
        git(self.toolkit, "add", "fake_generator.py")
        git(self.toolkit, "commit", "-q", "-m", "add fake generator")

        sfd.apply_sync(
            self.toolkit,
            self.dotfiles,
            tip,
            sorted(copy_set),
            [],
            base,
            generator_sweep=("fake_generator.py",),
            quiet=True,
        )

        manifest = (self.toolkit / "MANIFEST.txt").read_text(encoding="utf-8")
        self.assertIn("brand_new_file.txt", manifest)
        self.assertTrue((self.toolkit / "brand_new_file.txt").is_file())

        state = sfd.load_state(self.toolkit)
        assert state is not None
        self.assertEqual(state["last_synced_dotfiles_sha"], tip)
        self.assertIsNone(state["toolkit_commit"])


if __name__ == "__main__":
    unittest.main()
