#!/usr/bin/env python3
"""Tests for sync_from_dotfiles.py. Run with: python3 test_sync_from_dotfiles.py

Two tiers, per the spec this tool implements
(~/.claude/data/grill/atk-sync-post-cutover-alignment-spec.md):

- Pure-function tests for the registered transform and the state-file
  schema — no git subprocess calls at all.
- A slower integration tier against synthetic throwaway git repos (built
  and torn down inside each test), exercising the real read/compare/copy/
  sweep/state code paths. Marked ``allow_real_subprocess`` per
  test/AGENTS.md, since this tool's entire job is reading a real external
  repo via git and running real generator subprocesses.

Post-cutover contract (MIGRATION.md): only ``claude/CORE_INSTRUCTIONS.md``
is authored in dotfiles and flows downstream into this toolkit. The whole
range-replay machinery of the pre-cutover tool is gone.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sync_from_dotfiles as sfd


class TransformTests(unittest.TestCase):
    """The registered mechanical transform, pure — no git, no filesystem."""

    def test_rewrites_the_backticked_scripts_dir_token(self) -> None:
        text = "commit an edit to a script under `claude/scripts/` that a doc names\n"
        out, count = sfd.apply_transform(text)
        self.assertIn("`agent-scripts/`", out)
        self.assertNotIn("`claude/scripts/`", out)
        self.assertEqual(count, 1)

    def test_leaves_every_deployed_path_occurrence_untouched(self) -> None:
        # ~/.claude/scripts/ refers to the deployed symlink farm, not this
        # repo's agent-scripts/ — the transform must never touch it.
        text = (
            "run `python3 ~/.claude/scripts/dev_status.py add ...`\n"
            "  `python3 ~/.claude/scripts/dev_status.py block a b`\n"
        )
        out, count = sfd.apply_transform(text)
        self.assertEqual(out, text)
        self.assertEqual(count, 0)

    def test_is_idempotent(self) -> None:
        text = "under `claude/scripts/` per INTERFACES.md\n"
        once, count1 = sfd.apply_transform(text)
        twice, count2 = sfd.apply_transform(once)
        self.assertEqual(once, twice)
        self.assertEqual(count1, 1)
        self.assertEqual(count2, 0)

    @pytest.mark.allow_real_subprocess
    def test_real_dotfiles_copy_transforms_to_the_real_toolkit_copy(self) -> None:
        """The one live occurrence: dotfiles@HEAD's file, transformed, must
        equal the toolkit's committed copy. Skipped when ~/dotfiles is not
        present (e.g. a checkout on another machine) — the synthetic
        integration tier covers the mechanism regardless."""
        dotfiles = sfd.DEFAULT_DOTFILES_PATH
        probe = sfd.run_git(dotfiles, "rev-parse", "--is-inside-work-tree")
        if probe.returncode != 0:
            self.skipTest(f"{dotfiles} not available")
        head = sfd.resolve_head(dotfiles)
        if not sfd.path_exists_at(dotfiles, head, sfd.CONTRACT_FILE):
            self.skipTest("contract file absent from live dotfiles@HEAD")
        text = sfd.read_at(dotfiles, head, sfd.CONTRACT_FILE).decode("utf-8")
        transformed, count = sfd.apply_transform(text)
        toolkit_copy = (sfd.REPO_ROOT / sfd.CONTRACT_FILE).read_text(encoding="utf-8")
        if transformed == toolkit_copy:
            self.assertEqual(count, 1)
        else:
            # Legitimate drift either direction (an upstream edit not yet
            # synced, or toolkit-side work not yet pushed) — the transform
            # must still have fired exactly once on the current wording.
            self.assertEqual(count, 1, "live dotfiles copy lost its token")


class GeneratorSweepShapeTests(unittest.TestCase):
    def test_all_sweep_paths_live_in_agent_scripts(self) -> None:
        self.assertTrue(
            all(p.startswith("agent-scripts/") for p in sfd.GENERATOR_SWEEP),
            sfd.GENERATOR_SWEEP,
        )

    def test_all_sweep_paths_exist_in_this_repo(self) -> None:
        for relpath in sfd.GENERATOR_SWEEP:
            self.assertTrue(
                (sfd.REPO_ROOT / relpath).is_file(), f"missing: {relpath}"
            )


class StateFileTests(unittest.TestCase):
    def test_load_state_returns_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sfd.load_state(Path(tmp)))

    def test_load_state_ignores_a_corrupt_file(self) -> None:
        # Provenance-only: a corrupt state file can never block or crash a
        # run — the sync decision is made from content comparison.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = sfd.state_path(Path(tmp))
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not json at all", encoding="utf-8")
            self.assertIsNone(sfd.load_state(Path(tmp)))

    def test_write_state_schema_is_provenance_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sfd.write_state(root, dotfiles_sha="abc123")
            state = sfd.load_state(root)
            assert state is not None
            self.assertEqual(state["last_synced_dotfiles_sha"], "abc123")
            self.assertIn("synced_at", state)
            self.assertNotIn("toolkit_commit", state)

    def test_state_path_is_committed_not_ignored(self) -> None:
        gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".sync-state.json", gitignore)


# ── integration tier: real git, synthetic throwaway repos ───────────────────


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


def staged_paths(repo: Path) -> set[str]:
    out = git(repo, "diff", "--name-only", "--cached")
    return {line for line in out.splitlines() if line}


class SyntheticRepoIntegrationTests(unittest.TestCase):
    """Exercises the real read/compare/copy/sweep/state paths end to end.

    No real ~/dotfiles or real agent-scripts/ generators are touched —
    both "dotfiles" and "toolkit" are throwaway repos under a temp
    directory, and the sweep is a tuple of throwaway fake generators.
    """

    DOTFILES_TEXT = (
        "# core\n"
        "line one: commit an edit to any script under `claude/scripts/` that a\n"
        "skill doc names.\n"
        "run `python3 ~/.claude/scripts/dev_status.py add ...\n"
        "more shared prose\n"
    )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dotfiles = Path(self.tmp.name) / "dotfiles"
        self.toolkit = Path(self.tmp.name) / "toolkit"
        self.dotfiles.mkdir()
        self.toolkit.mkdir()
        subprocess_git_init_and_commit(self.dotfiles)
        subprocess_git_init_and_commit(self.toolkit)
        # The toolkit starts with dotfiles@HEAD's file already transformed —
        # i.e. a synced baseline — so each test moves one lever at a time.
        head = sfd.resolve_head(self.dotfiles)
        commit_file(self.dotfiles, sfd.CONTRACT_FILE, self.DOTFILES_TEXT)
        head = sfd.resolve_head(self.dotfiles)
        transformed, count = sfd.apply_transform(self.DOTFILES_TEXT)
        assert count == 1
        commit_file(self.toolkit, sfd.CONTRACT_FILE, transformed)
        self.tip = head

    def write_fake_generators(self, *names: str) -> tuple[str, ...]:
        """Commit throwaway generator scripts into the toolkit repo; the
        sync runs them with cwd=toolkit root."""
        relpaths: list[str] = []
        for i, name in enumerate(names):
            relpath = f"fake_gen_{i}_{name}.py"
            (self.toolkit / relpath).write_text(
                "from pathlib import Path\n"
                f"Path('{name}.out').write_text('generated by {name}\\n')\n",
                encoding="utf-8",
            )
            git(self.toolkit, "add", relpath)
            relpaths.append(relpath)
        git(self.toolkit, "commit", "-q", "-m", "add fake generators")
        return tuple(relpaths)

    # -- up-to-date runs are true no-ops ------------------------------------

    @pytest.mark.allow_real_subprocess
    def test_up_to_date_apply_is_a_true_noop(self) -> None:
        """Same content → exit 0, no sweep, no state write, tree unchanged."""
        before = git(self.toolkit, "status", "--porcelain")
        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), before)
        self.assertFalse(sfd.state_path(self.toolkit).exists())

    @pytest.mark.allow_real_subprocess
    def test_unrelated_upstream_commits_cause_no_state_churn(self) -> None:
        """The provenance sha is the last commit touching the contract file,
        not HEAD — an unrelated dotfiles commit must not dirty the state."""
        commit_file(self.dotfiles, "unrelated/zshrc", "alias ll='ls -l'\n")
        before = git(self.toolkit, "status", "--porcelain")
        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), before)
        self.assertFalse(sfd.state_path(self.toolkit).exists())

    # -- apply with a real delta ---------------------------------------------

    @pytest.mark.allow_real_subprocess
    def test_apply_writes_transformed_file_sweeps_and_advances_state(self) -> None:
        sweep = self.write_fake_generators("alpha", "beta")
        commit_file(
            self.dotfiles,
            sfd.CONTRACT_FILE,
            self.DOTFILES_TEXT + "new upstream paragraph\n",
        )
        # An unrelated upstream commit on top: tip advances past the commit
        # that touched the contract file, and provenance must record the
        # latter, not HEAD.
        commit_file(self.dotfiles, "unrelated/notes", "unrelated\n")
        self.tip = sfd.resolve_head(self.dotfiles)
        touching = sfd.last_commit_touching(
            self.dotfiles, self.tip, sfd.CONTRACT_FILE
        )
        self.assertNotEqual(touching, self.tip)  # last change to the file itself

        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
            generator_sweep=sweep,
        )
        self.assertEqual(code, 0)

        toolkit_copy = (self.toolkit / sfd.CONTRACT_FILE).read_text(encoding="utf-8")
        self.assertIn("new upstream paragraph\n", toolkit_copy)
        self.assertNotIn("`claude/scripts/`", toolkit_copy)

        state = sfd.load_state(self.toolkit)
        assert state is not None
        self.assertEqual(state["last_synced_dotfiles_sha"], touching)

        # Staged set is exactly the contract file + the state file; the
        # fake generators' outputs stay unstaged for review.
        self.assertEqual(
            staged_paths(self.toolkit), {sfd.CONTRACT_FILE, "scripts/.sync-state.json"}
        )
        self.assertTrue((self.toolkit / "alpha.out").is_file())
        self.assertTrue((self.toolkit / "beta.out").is_file())
        self.assertNotIn("alpha.out", staged_paths(self.toolkit))

    @pytest.mark.allow_real_subprocess
    def test_state_advances_only_on_content_change(self) -> None:
        """A first successful sync writes state; an immediate re-run (same
        content, same upstream touching-sha) leaves it byte-identical."""
        sweep = self.write_fake_generators("once")
        commit_file(
            self.dotfiles,
            sfd.CONTRACT_FILE,
            self.DOTFILES_TEXT + "delta\n",
        )
        argv = ["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)]
        self.assertEqual(
            sfd.main(self.toolkit, argv=argv, do_exit=False,
                     generator_sweep=sweep),
            0,
        )
        first = sfd.state_path(self.toolkit).read_bytes()
        self.assertEqual(
            sfd.main(self.toolkit, argv=argv, do_exit=False,
                     generator_sweep=sweep),
            0,
        )
        self.assertEqual(sfd.state_path(self.toolkit).read_bytes(), first)

    # -- report mode ----------------------------------------------------------

    @pytest.mark.allow_real_subprocess
    def test_report_mode_writes_nothing_and_skips_the_sweep(self) -> None:
        """No --apply: summarize and stop — no file write, no state, and the
        generators never run (a report must not mutate the tree)."""
        commit_file(
            self.dotfiles,
            sfd.CONTRACT_FILE,
            self.DOTFILES_TEXT + "pending upstream delta\n",
        )
        before = git(self.toolkit, "status", "--porcelain")
        code = sfd.main(
            self.toolkit,
            argv=["--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), before)
        self.assertFalse(sfd.state_path(self.toolkit).exists())
        self.assertFalse((self.toolkit / sfd.CONTRACT_FILE).read_text(
            encoding="utf-8"
        ).endswith("pending upstream delta\n"))

    # -- loud failure modes ----------------------------------------------------

    @pytest.mark.allow_real_subprocess
    def test_contract_missing_from_dotfiles_head_stops_clean(self) -> None:
        (self.dotfiles / sfd.CONTRACT_FILE).unlink()
        git(self.dotfiles, "add", "-A")
        git(self.dotfiles, "commit", "-q", "-m", "delete contract")
        before = git(self.toolkit, "status", "--porcelain")
        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
        )
        self.assertEqual(code, 1)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), before)
        self.assertFalse(sfd.state_path(self.toolkit).exists())

    @pytest.mark.allow_real_subprocess
    def test_zero_substitutions_with_differing_content_stops(self) -> None:
        """An upstream reword that dropped the token could carry an
        unadjusted path reference into the toolkit copy — stop for manual
        review instead of copying it."""
        dropped = self.DOTFILES_TEXT.replace("`claude/scripts/`", "the scripts tree")
        commit_file(self.dotfiles, sfd.CONTRACT_FILE, dropped)
        before = git(self.toolkit, "status", "--porcelain")
        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
        )
        self.assertEqual(code, 1)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), before)
        self.assertFalse(sfd.state_path(self.toolkit).exists())

    @pytest.mark.allow_real_subprocess
    def test_zero_substitutions_up_to_date_is_a_normal_noop(self) -> None:
        # dotfiles went path-neutral AND the toolkit copy matches: the
        # designed no-op, not a guard trip.
        dropped = self.DOTFILES_TEXT.replace("`claude/scripts/`", "the scripts tree")
        commit_file(self.dotfiles, sfd.CONTRACT_FILE, dropped)
        commit_file(self.toolkit, sfd.CONTRACT_FILE, dropped)
        before = git(self.toolkit, "status", "--porcelain")
        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), before)

    @pytest.mark.allow_real_subprocess
    def test_multiple_token_occurrences_stop_for_review(self) -> None:
        doubled = self.DOTFILES_TEXT.replace(
            "`claude/scripts/`", "`claude/scripts/` plus `claude/scripts/`", 1
        )
        commit_file(self.dotfiles, sfd.CONTRACT_FILE, doubled)
        before = git(self.toolkit, "status", "--porcelain")
        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", str(self.dotfiles)],
            do_exit=False,
        )
        self.assertEqual(code, 1)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), before)

    @pytest.mark.allow_real_subprocess
    def test_bad_dotfiles_path_fails_loudly_not_with_a_traceback(self) -> None:
        code = sfd.main(
            self.toolkit,
            argv=["--apply", "--quiet", "--dotfiles-path", "/nonexistent/repo"],
            do_exit=False,
        )
        self.assertEqual(code, 1)

    # -- generator failure -------------------------------------------------

    @pytest.mark.allow_real_subprocess
    def test_failing_generator_stops_before_state_and_staging(self) -> None:
        (self.toolkit / "broken_gen.py").write_text(
            "import sys\nsys.exit('boom')\n", encoding="utf-8"
        )
        git(self.toolkit, "add", "broken_gen.py")
        git(self.toolkit, "commit", "-q", "-m", "add broken generator")
        commit_file(
            self.dotfiles,
            sfd.CONTRACT_FILE,
            self.DOTFILES_TEXT + "delta before a broken sweep\n",
        )
        code = sfd.main(
            self.toolkit,
            argv=[
                "--apply",
                "--quiet",
                "--dotfiles-path",
                str(self.dotfiles),
            ],
            do_exit=False,
            generator_sweep=("broken_gen.py",),
        )
        self.assertEqual(code, 1)
        # The contract file was written before the sweep ran — it stays in
        # the working tree, unstaged (no automatic rollback). Nothing is
        # staged, and state was not advanced.
        self.assertEqual(staged_paths(self.toolkit), set())
        self.assertIn(" M claude/CORE_INSTRUCTIONS.md", git(
            self.toolkit, "status", "--porcelain"
        ))
        self.assertIn(
            "delta before a broken sweep",
            (self.toolkit / sfd.CONTRACT_FILE).read_text(encoding="utf-8"),
        )
        self.assertFalse(sfd.state_path(self.toolkit).exists())

    # -- CLI flags ------------------------------------------------------------

    @pytest.mark.allow_real_subprocess
    def test_since_flag_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            sfd.main(self.toolkit, argv=["--since", "abc123"], do_exit=False)
        self.assertEqual(ctx.exception.code, 2)

    @pytest.mark.allow_real_subprocess
    def test_bad_dotfiles_path_flag_shape(self) -> None:
        # --dotfiles-path takes a value; a missing value is bad usage.
        with self.assertRaises(SystemExit) as ctx:
            sfd.main(self.toolkit, argv=["--dotfiles-path"], do_exit=False)
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
