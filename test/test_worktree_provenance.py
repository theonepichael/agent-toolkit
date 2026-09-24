#!/usr/bin/env python3
"""worktree_provenance.py: the per-worktree provenance marker and the pure
attribution predicates shared by guard_rails.py and dev_status_mutation.py.

Real subprocess here is bounded to `git` in tmp_path (plus one unrelated-CWD
invocation to catch relative --git-dir resolution), which is why this file
carries the allow_real_subprocess marker; the predicate tests themselves are
pure.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import pytest  # noqa: E402
import worktree_provenance as wp  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # git in tmp_path only


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    # Disable git's background maintenance -- it can create/remove a
    # maintenance.lock file well after commit returns. Confirmed live in CI
    # (2026-09-18) racing an explicit shutil.rmtree() teardown elsewhere in
    # this repo (test_bundle_drift_check.py); this repo's own tests use
    # pytest's tmp_path here instead, so the same immediate race is less
    # likely, but the config costs nothing and removes the class of risk.
    _git("config", "gc.auto", "0", cwd=path)
    _git("config", "maintenance.auto", "false", cwd=path)
    (path / "tracked.txt").write_text("x\n")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    return path


@pytest.fixture()
def repo_pair(tmp_path: Path) -> dict:
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-item-a"
    _git("worktree", "add", "-q", str(wt), "-b", "item-a", cwd=repo)
    return {"repo": repo, "wt": wt}


# ── pure predicates: marker precedence with the defined stale escape ───────


class TestWorktreePointsAtItemPredicate:
    def test_live_marker_decides_the_worktree(self) -> None:
        # marker names a still-in-progress item: attribution follows the
        # marker only; the branch (even == another live item's slug) does not
        # also point there. This is the no-double-attribution rule that keeps
        # _evaluate_claim's every-pointed-item claim requirement from
        # denying a legitimately-claimed worktree on a foreign item's account.
        assert wp.worktree_points_at_item(
            marker_slug="item-a",
            is_linked_worktree=True,
            branch="item-b",
            item_id="item-a",
            in_progress_ids={"item-a", "item-b"},
        )
        assert not wp.worktree_points_at_item(
            marker_slug="item-a",
            is_linked_worktree=True,
            branch="item-b",
            item_id="item-b",
            in_progress_ids={"item-a", "item-b"},
        )

    def test_stale_marker_falls_back_to_branch(self) -> None:
        # marker names a no-longer-in-progress item = STALE; fall back to
        # branch == slug so the worktree is attributed to item-b.
        assert wp.worktree_points_at_item(
            marker_slug="item-a",
            is_linked_worktree=True,
            branch="item-b",
            item_id="item-b",
            in_progress_ids={"item-b"},
        )
        assert not wp.worktree_points_at_item(
            marker_slug="item-a",
            is_linked_worktree=True,
            branch="item-b",
            item_id="item-a",
            in_progress_ids={"item-b"},
        )

    def test_unmarked_worktree_falls_back_to_branch(self) -> None:
        assert wp.worktree_points_at_item(
            marker_slug=None,
            is_linked_worktree=True,
            branch="item-a",
            item_id="item-a",
            in_progress_ids={"item-a"},
        )
        assert not wp.worktree_points_at_item(
            marker_slug=None,
            is_linked_worktree=True,
            branch="item-a",
            item_id="item-b",
            in_progress_ids={"item-b"},
        )

    def test_main_checkout_never_points_via_marker_or_branch(self) -> None:
        # is_linked_worktree=False: even an errant marker file or a
        # main==slug branch situation must not attribute (R2 owns main).
        assert not wp.worktree_points_at_item(
            marker_slug="item-a",
            is_linked_worktree=False,
            branch="item-a",
            item_id="item-a",
            in_progress_ids={"item-a"},
        )

# ── marker read/write on a real linked worktree ────────────────────────────


class TestMarkerIo:
    def test_write_then_read_roundtrip(self, repo_pair) -> None:
        wt = repo_pair["wt"]
        assert wp.write_marker(wt, "item-a") is True
        git_dir = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--git-dir"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Marker lives in the per-worktree metadata dir, outside the checkout.
        marker = (wt / git_dir if not os.path.isabs(git_dir) else Path(git_dir)) / "devstatus_item"
        assert marker.is_file(), f"expected marker at {marker}"
        assert wp.read_marker_for_worktree(wt) == "item-a"

    def test_marker_absent_reads_none(self, repo_pair) -> None:
        assert wp.read_marker_for_worktree(repo_pair["wt"]) is None

    def test_write_from_unrelated_cwd(self, repo_pair, monkeypatch, tmp_path) -> None:
        # git -C returns --git-dir relative to the -C dir; a naive bare
        # realpath would resolve it against the process CWD and miss.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert wp.write_marker(repo_pair["wt"], "item-a") is True
        assert wp.read_marker_for_worktree(repo_pair["wt"]) == "item-a"

    def test_write_marker_noop_on_main_checkout(self, repo_pair) -> None:
        repo = repo_pair["repo"]
        assert wp.write_marker(repo, "item-a") is False
        assert not (repo / ".git" / "devstatus_item").exists()
        assert wp.read_marker_for_worktree(repo) is None

    def test_write_marker_noop_outside_any_repo(self, tmp_path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert wp.write_marker(plain, "item-a") is False

    def test_corrupt_marker_reads_none(self, repo_pair) -> None:
        wt = repo_pair["wt"]
        assert wp.write_marker(wt, "item-a") is True
        prov = wp.classify(str(wt))
        assert prov is not None and prov.marker_slug == "item-a"
        wp.marker_path(prov.git_dir).write_text("multi\nline\ngarbage\n")
        assert wp.read_marker_for_worktree(wt) is None

    def test_retag_overwrites_idempotently(self, repo_pair) -> None:
        wt = repo_pair["wt"]
        assert wp.write_marker(wt, "item-a") is True
        assert wp.write_marker(wt, "item-a") is True
        assert wp.read_marker_for_worktree(wt) == "item-a"


# ── classify ───────────────────────────────────────────────────────────────


class TestClassify:
    def test_linked_worktree(self, repo_pair) -> None:
        prov = wp.classify(str(repo_pair["wt"]))
        assert prov is not None
        assert prov.is_linked_worktree
        assert prov.branch == "item-a"
        assert prov.toplevel is not None
        assert prov.marker_slug is None
        assert wp.write_marker(repo_pair["wt"], "item-a") is True
        prov2 = wp.classify(str(repo_pair["wt"]))
        assert prov2 is not None and prov2.marker_slug == "item-a"

    def test_main_checkout(self, repo_pair) -> None:
        prov = wp.classify(str(repo_pair["repo"]))
        assert prov is not None
        assert not prov.is_linked_worktree
        assert prov.branch == "main"

    def test_non_repo_returns_none(self, tmp_path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert wp.classify(str(plain)) is None


# ── inspect_item_worktrees: the committed-work inspection behind review /
#    approve / done. Replaces the retired _completion_merge_notice tests
#    (the notice's marker-OR-branch attribution lives on inside the
#    inspector); real git in tmp_path, one inspection per scenario. ──────


class TestInspectItemWorktrees:
    """Real-git coverage for the committed-work inspector."""

    def _unmerged_wt(self, tmp_path: Path, slug: str) -> tuple[Path, Path]:
        repo = _init_repo(tmp_path / f"proj-{slug}")
        wt = tmp_path / f"proj-{slug}-wt"
        _git("worktree", "add", "-q", str(wt), "-b", slug, cwd=repo)
        (wt / "feature.txt").write_text("work\n")
        _git("add", "feature.txt", cwd=wt)
        _git("commit", "-qm", "unmerged work", cwd=wt)
        return repo, wt

    def _merged_wt(self, tmp_path: Path, slug: str) -> tuple[Path, Path]:
        repo, wt = self._unmerged_wt(tmp_path, slug)
        _git("merge", "-q", "--no-ff", slug, cwd=repo)
        return repo, wt

    def test_dirty_worktree_refuses_with_sample(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-a")
        (wt / "extra.txt").write_text("uncommitted\n")
        out = wp.inspect_item_worktrees(
            related_files=[str(wt / "extra.txt")], slug="slug-a", cwd=tmp_path
        )
        assert len(out) == 1
        assert out[0].dirty
        assert "extra.txt" in out[0].dirty_sample

    def test_untracked_file_counts_as_dirty(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-b")
        (wt / "new.txt").write_text("untracked\n")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-b", cwd=tmp_path
        )
        assert len(out) == 1 and out[0].dirty

    def test_ignored_build_output_stays_clean(self, tmp_path) -> None:
        repo, wt = self._merged_wt(tmp_path, "slug-c")
        (wt / ".gitignore").write_text("build/\n")
        _git("add", ".gitignore", cwd=wt)
        _git("commit", "-qm", "ignore build", cwd=wt)
        _git("merge", "-q", "--no-ff", "slug-c", cwd=repo)
        (wt / "build").mkdir()
        (wt / "build" / "out.bin").write_bytes(b"\x00\x01")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-c", cwd=tmp_path
        )
        assert len(out) == 1 and not out[0].dirty

    def test_clean_unmerged_branch_refuses_with_target(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-d")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-d", cwd=tmp_path
        )
        assert len(out) == 1
        assert not out[0].dirty
        assert out[0].head_ancestor_of_default is False
        assert out[0].default_ref == "refs/heads/main"

    def test_merged_worktree_passes_without_push(self, tmp_path) -> None:
        repo, wt = self._merged_wt(tmp_path, "slug-e")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-e", cwd=tmp_path
        )
        assert len(out) == 1
        assert not out[0].dirty
        assert out[0].head_ancestor_of_default is True

    def test_marker_attribution_survives_branch_rename(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-f")
        _git("branch", "-m", "renamed-away", cwd=wt)
        assert wp.write_marker(wt, "slug-f") is True
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-f", cwd=tmp_path
        )
        assert len(out) == 1
        assert out[0].head_ancestor_of_default is False

    def test_foreign_marker_wins_over_slug_branch(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-g")
        assert wp.write_marker(wt, "another-item") is True
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-g", cwd=tmp_path
        )
        assert out == []

    def test_invalid_marker_refuses_fail_closed(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-h")
        prov = wp.classify(wt)
        assert prov is not None and prov.git_dir is not None
        (prov.git_dir / "devstatus_item").write_text("slug-h\nother\n")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-h", cwd=tmp_path
        )
        assert out
        assert any(i.problem is not None and "marker" in i.problem.lower() for i in out)

    def test_detached_head_refuses_when_marker_attributed(self, tmp_path) -> None:
        repo, wt = self._merged_wt(tmp_path, "slug-h2")
        assert wp.write_marker(wt, "slug-h2") is True
        _git("checkout", "--detach", "HEAD", cwd=wt)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-h2", cwd=tmp_path
        )
        assert len(out) == 1 and out[0].problem is not None
        assert "detached" in out[0].problem.lower()

    def test_removed_worktree_unmerged_slug_branch_refuses(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-i")
        _git("worktree", "remove", str(wt), cwd=repo)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-i", cwd=tmp_path
        )
        assert len(out) == 1
        assert out[0].source == "branch"
        assert out[0].head_ancestor_of_default is False

    def test_removed_worktree_merged_slug_branch_allows(self, tmp_path) -> None:
        repo, wt = self._merged_wt(tmp_path, "slug-j")
        _git("worktree", "remove", str(wt), cwd=repo)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-j", cwd=tmp_path
        )
        assert out == []

    def test_no_worktree_no_branch_allows(self, tmp_path) -> None:
        repo = _init_repo(tmp_path / "proj-k")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-k", cwd=tmp_path
        )
        assert out == []

    def test_main_checkout_dirtiness_is_not_this_check(self, tmp_path) -> None:
        """Uncommitted work sitting directly in a main checkout is the
        documented accepted gap (guard-rails R2 owns the in-session path);
        this inspector only sees attributed worktrees and slug branches."""
        repo = _init_repo(tmp_path / "proj-l")
        (repo / "tracked.txt").write_text("dirty\n")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-l", cwd=tmp_path
        )
        assert out == []

    def test_deleted_file_resolves_through_ancestor(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-m")
        out = wp.inspect_item_worktrees(
            related_files=[str(wt / "gone" / "deleted.py")],
            slug="slug-m",
            cwd=tmp_path,
        )
        assert len(out) == 1 and out[0].head_ancestor_of_default is False

    def test_path_outside_any_repo_is_not_an_error(self, tmp_path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "notes.txt").write_text("x\n")
        out = wp.inspect_item_worktrees(
            related_files=[str(plain / "notes.txt")], slug="slug-n", cwd=tmp_path
        )
        assert out == []

    def test_relative_related_file_refuses_fail_closed(self, tmp_path) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-o")
        out = wp.inspect_item_worktrees(
            related_files=["feature.txt"], slug="slug-o", cwd=tmp_path
        )
        assert len(out) == 1 and out[0].problem is not None

    def test_spaces_in_paths_survive(self, tmp_path) -> None:
        repo = _init_repo(tmp_path / "proj-p")
        wt = tmp_path / "proj-p wt dir"
        _git("worktree", "add", "-q", str(wt), "-b", "slug-p", cwd=repo)
        (wt / "feature.txt").write_text("work\n")
        _git("add", "feature.txt", cwd=wt)
        _git("commit", "-qm", "unmerged work", cwd=wt)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-p", cwd=tmp_path
        )
        assert len(out) == 1 and out[0].head_ancestor_of_default is False
        assert "wt dir" in out[0].target

    def test_submodule_path_is_skipped(self, tmp_path) -> None:
        """A submodule's pinned detached HEAD is normal state, never
        evidence of unmerged item work."""
        repo, wt = self._merged_wt(tmp_path, "slug-q")
        sub = _init_repo(tmp_path / "proj-q-sub")
        _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub", cwd=wt)
        _git("commit", "-qm", "add submodule", cwd=wt)
        _git("merge", "-q", "--no-ff", "slug-q", cwd=repo)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt"), str(wt / "sub" / "tracked.txt")],
            slug="slug-q",
            cwd=tmp_path,
        )
        # The super worktree passes; the submodule path contributes nothing
        # (its pinned detached HEAD is normal state, its checkout is the
        # sub-repo's main worktree).
        assert len(out) == 1
        assert out[0].problem is None
        assert out[0].head_ancestor_of_default is True

    def test_submodule_path_is_skipped_under_explicit_bare_repo_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.bareRepository")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "explicit")
        repo, wt = self._merged_wt(tmp_path, "slug-q2")
        sub = _init_repo(tmp_path / "proj-q2-sub")
        _git(
            "-c",
            "protocol.file.allow=always",
            "-c",
            "safe.bareRepository=all",
            "submodule",
            "add",
            "-q",
            str(sub),
            "sub",
            cwd=wt,
        )
        _git("commit", "-qm", "add submodule", cwd=wt)
        _git("merge", "-q", "--no-ff", "slug-q2", cwd=repo)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt"), str(wt / "sub" / "tracked.txt")],
            slug="slug-q2",
            cwd=tmp_path,
        )
        assert len(out) == 1
        assert out[0].problem is None
        assert out[0].head_ancestor_of_default is True


    def test_multiple_repos_all_inspected(self, tmp_path) -> None:
        repo1, wt1 = self._unmerged_wt(tmp_path, "slug-r")
        repo2 = _init_repo(tmp_path / "proj-r2")
        wt2 = tmp_path / "proj-r2-wt"
        _git("worktree", "add", "-q", str(wt2), "-b", "slug-r", cwd=repo2)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo1 / "tracked.txt"), str(repo2 / "tracked.txt")],
            slug="slug-r",
            cwd=tmp_path,
        )
        assert len(out) == 2
        bad = [i for i in out if i.dirty or i.head_ancestor_of_default is False]
        assert len(bad) == 1 and "proj-slug-r-wt" in bad[0].target

    def test_caller_cwd_on_slug_branch_is_inspected(self, tmp_path, monkeypatch) -> None:
        repo, wt = self._unmerged_wt(tmp_path, "slug-s")
        monkeypatch.chdir(wt)
        out = wp.inspect_item_worktrees(
            related_files=[], slug="slug-s", cwd=Path.cwd()
        )
        assert len(out) == 1
        assert out[0].head_ancestor_of_default is False


class TestDeclaredIntegrationBranch:
    """``target_branch`` replaces the local default branch as the merge
    target — work landed on an integration branch (never main) passes, and
    a declared branch that does not exist fails closed."""

    def _wt_on_integration(self, tmp_path: Path, slug: str) -> tuple[Path, Path]:
        repo = _init_repo(tmp_path / f"proj-{slug}")
        _git("branch", "release-1", cwd=repo)
        wt = tmp_path / f"proj-{slug}-wt"
        _git("worktree", "add", "-q", str(wt), "-b", slug, "release-1", cwd=repo)
        (wt / "feature.txt").write_text("work\n")
        _git("add", "feature.txt", cwd=wt)
        _git("commit", "-qm", "work", cwd=wt)
        _git("checkout", "-q", "release-1", cwd=repo)
        _git("merge", "-q", "--no-ff", slug, cwd=repo)
        _git("checkout", "-q", "main", cwd=repo)
        return repo, wt

    def test_work_merged_only_into_declared_branch_passes(self, tmp_path) -> None:
        repo, _wt = self._wt_on_integration(tmp_path, "slug-r1")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")],
            slug="slug-r1",
            cwd=tmp_path,
            target_branch="release-1",
        )
        assert len(out) == 1
        assert out[0].problem is None
        assert out[0].head_ancestor_of_default is True
        assert out[0].default_ref == "refs/heads/release-1"

    def test_undeclared_target_still_requires_default_branch(self, tmp_path) -> None:
        repo, _wt = self._wt_on_integration(tmp_path, "slug-r2")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-r2", cwd=tmp_path
        )
        assert len(out) == 1
        assert out[0].head_ancestor_of_default is False
        assert out[0].default_ref == "refs/heads/main"

    def test_work_on_main_but_not_declared_branch_refuses(self, tmp_path) -> None:
        repo = _init_repo(tmp_path / "proj-r3")
        _git("branch", "release-1", cwd=repo)
        wt = tmp_path / "proj-r3-wt"
        _git("worktree", "add", "-q", str(wt), "-b", "slug-r3", cwd=repo)
        (wt / "feature.txt").write_text("work\n")
        _git("add", "feature.txt", cwd=wt)
        _git("commit", "-qm", "work", cwd=wt)
        _git("merge", "-q", "--no-ff", "slug-r3", cwd=repo)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")],
            slug="slug-r3",
            cwd=tmp_path,
            target_branch="release-1",
        )
        assert len(out) == 1
        assert out[0].head_ancestor_of_default is False
        assert out[0].default_ref == "refs/heads/release-1"

    def test_missing_declared_branch_is_a_problem(self, tmp_path) -> None:
        repo, _wt = self._wt_on_integration(tmp_path, "slug-r4")
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")],
            slug="slug-r4",
            cwd=tmp_path,
            target_branch="release-9",
        )
        # Every target inspected against the missing ref refuses (the
        # worktree, and the slug-branch fallback a problem does not suppress).
        assert out
        for insp in out:
            assert insp.problem is not None
            assert "refs/heads/release-9" in insp.problem
            assert "integration_branch" in insp.problem

    def test_surviving_slug_branch_merged_into_declared_branch_allows(
        self, tmp_path
    ) -> None:
        repo, wt = self._wt_on_integration(tmp_path, "slug-r5")
        _git("worktree", "remove", str(wt), cwd=repo)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")],
            slug="slug-r5",
            cwd=tmp_path,
            target_branch="release-1",
        )
        assert out == []

    def test_caller_cwd_checkout_uses_declared_branch(
        self, tmp_path, monkeypatch
    ) -> None:
        repo, wt = self._wt_on_integration(tmp_path, "slug-r6")
        out = wp.inspect_item_worktrees(
            related_files=[], slug="slug-r6", cwd=wt, target_branch="release-1"
        )
        assert len(out) == 1
        assert out[0].head_ancestor_of_default is True
        assert out[0].default_ref == "refs/heads/release-1"


class TestBranchNameProblem:
    """``branch_name_problem`` validates a declared integration branch with
    git's own ``check-ref-format --branch`` rules; None means valid."""

    @pytest.mark.parametrize("name", ["release-1", "feat/x", "r1.2"])
    def test_valid_names(self, name: str) -> None:
        assert wp.branch_name_problem(name) is None

    @pytest.mark.parametrize(
        "name",
        ["", "foo..bar", "-x", "refs/heads/x", "a@{-1}", "@{-1}", "x.lock", "a b"],
    )
    def test_invalid_names(self, name: str) -> None:
        assert wp.branch_name_problem(name) is not None

    @pytest.mark.parametrize("value", [5, None, ["release-1"], {"a": 1}])
    def test_non_string_is_a_problem(self, value: object) -> None:
        assert wp.branch_name_problem(value) is not None


class TestInspectFailurePaths:
    """Inspection failures deny; they never collapse into absence."""

    def _repo_with_wt(self, tmp_path: Path, slug: str) -> tuple[Path, Path]:
        repo = _init_repo(tmp_path / f"proj-{slug}")
        wt = tmp_path / f"proj-{slug}-wt"
        _git("worktree", "add", "-q", str(wt), "-b", slug, cwd=repo)
        return repo, wt

    def test_git_status_failure_is_a_problem(self, tmp_path, monkeypatch) -> None:
        repo, wt = self._repo_with_wt(tmp_path, "slug-t")

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=2)

        monkeypatch.setattr(wp.subprocess, "run", boom)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-t", cwd=tmp_path
        )
        assert len(out) == 1 and out[0].problem is not None

    def test_malformed_worktree_record_is_a_problem(self, tmp_path, monkeypatch) -> None:
        repo, wt = self._repo_with_wt(tmp_path, "slug-u")
        real_run = wp.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if "worktree" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "bogus\0", "")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(wp.subprocess, "run", fake_run)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-u", cwd=tmp_path
        )
        assert out and all(i.problem is not None for i in out)

    def test_show_ref_failure_is_not_branch_absence(self, tmp_path, monkeypatch) -> None:
        repo, wt = self._repo_with_wt(tmp_path, "slug-v")
        _git("worktree", "remove", str(wt), cwd=repo)

        real_run = wp.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if "show-ref" in cmd:
                return subprocess.CompletedProcess(cmd, 2, "", "fatal: corrupt")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(wp.subprocess, "run", fake_run)
        out = wp.inspect_item_worktrees(
            related_files=[str(repo / "tracked.txt")], slug="slug-v", cwd=tmp_path
        )
        assert len(out) == 1 and out[0].problem is not None

