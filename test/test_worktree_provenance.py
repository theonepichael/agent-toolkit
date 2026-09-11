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

    def test_completion_predicate_marker_or_branch(self) -> None:
        # The notice predicate: per-item advisory, marker OR branch matches,
        # and it works even though the completing slug is already `done`
        # (i.e. absent from any in-progress set).
        assert wp.worktree_belongs_to_slug(
            marker_slug="item-a", is_linked_worktree=True, branch="renamed",
            slug="item-a",
        )
        assert wp.worktree_belongs_to_slug(
            marker_slug=None, is_linked_worktree=True, branch="item-a",
            slug="item-a",
        )
        assert not wp.worktree_belongs_to_slug(
            marker_slug="item-a", is_linked_worktree=True, branch="item-a",
            slug="item-b",
        )
        assert not wp.worktree_belongs_to_slug(
            marker_slug=None, is_linked_worktree=False, branch="item-a",
            slug="item-a",
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


# ── _completion_merge_notice attribution (was dead code for worktree.py
#    worktrees: its old pre-filter compared the toplevel DIRNAME to the
#    slug, but worktree.py names dirs <repo>-<slug>) ────────────────────


class TestCompletionMergeNoticeAttribution:
    """The unmerged-completion notice must fire from the item's worktree.
    It runs AFTER the item is saved done, so the slug is no longer in any
    in-progress set — the notice predicate is marker-OR-branch by design."""

    def _unmerged_worktree(self, tmp_path: Path, slug: str) -> Path:
        repo = _init_repo(tmp_path / f"proj-{slug}")
        wt = tmp_path / f"proj-{slug}-{slug}"
        _git("worktree", "add", "-q", str(wt), "-b", slug, cwd=repo)
        (wt / "feature.txt").write_text("work\n")
        _git("add", "feature.txt", cwd=wt)
        _git("commit", "-qm", "unmerged work", cwd=wt)
        return wt

    def test_notice_fires_for_slug_branch_worktree(self, tmp_path, monkeypatch) -> None:
        import dev_status_mutation

        wt = self._unmerged_worktree(tmp_path, "slug-a")
        monkeypatch.chdir(wt)
        msg = dev_status_mutation._completion_merge_notice("slug-a")
        assert msg is not None and "not confirmed merged" in msg

    def test_notice_fires_for_renamed_branch_via_marker(
        self, tmp_path, monkeypatch
    ) -> None:
        import dev_status_mutation

        wt = self._unmerged_worktree(tmp_path, "slug-b")
        _git("branch", "-m", "renamed-away", cwd=wt)
        assert wp.write_marker(wt, "slug-b") is True
        monkeypatch.chdir(wt)
        msg = dev_status_mutation._completion_merge_notice("slug-b")
        assert msg is not None and "not confirmed merged" in msg

    def test_notice_silent_for_foreign_worktree(self, tmp_path, monkeypatch) -> None:
        """A different repo's checkout must not trigger the notice for an
        unrelated slug (the old root.name check's one true property)."""
        import dev_status_mutation

        wt = self._unmerged_worktree(tmp_path, "slug-c")
        monkeypatch.chdir(wt)
        assert dev_status_mutation._completion_merge_notice("other-item") is None

    def test_notice_none_outside_any_repo(self, tmp_path, monkeypatch) -> None:
        import dev_status_mutation

        plain = tmp_path / "bare-dir"
        plain.mkdir()
        monkeypatch.chdir(plain)
        assert dev_status_mutation._completion_merge_notice("anything") is None

