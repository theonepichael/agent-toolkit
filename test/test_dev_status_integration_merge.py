#!/usr/bin/env python3
"""Integration tests for dev_status.py integration-merge.

Tests that integration-branch work is merged through a temporary worktree,
updating the target integration branch without touching the main checkout's
HEAD or branch.
"""

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys_path = str(REPO_ROOT / "agent-scripts")
import sys
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import dev_status_impl
import test_bootstrap

pytestmark = pytest.mark.allow_real_subprocess


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", branch, ".", cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "gc.auto", "0", cwd=path)
    _git("config", "maintenance.auto", "false", cwd=path)
    (path / "f.txt").write_text("initial\n")
    _git("add", "f.txt", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


def _create_item(slug: str, integration_branch: str | None, related_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "id": slug,
        "summary": "test item",
        "status": "in-progress",
        "related_files": [{"path": str(related_path)}],
    }
    if integration_branch is not None:
        item["integration_branch"] = integration_branch
    monkeypatch.setattr(dev_status_impl, "load_items", lambda: [item])
    monkeypatch.setattr(dev_status_impl, "load_pending", lambda: [])


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        for k in ("quiet", "verbose", "push", "repo", "branch"):
            if k not in kwargs:
                setattr(self, k, False if k in ("quiet", "verbose", "push") else None)


def test_integration_merge_merges_without_touching_main_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    # Create integration branch 'release-1' pointing at initial commit
    _git("branch", "release-1", cwd=repo)
    # Create feature branch 'atk-my-feature'
    _git("branch", "atk-my-feature", "release-1", cwd=repo)
    # Commit to feature branch in a separate worktree
    feat_wt = tmp_path / "feat-wt"
    _git("worktree", "add", "-q", str(feat_wt), "atk-my-feature", cwd=repo)
    (feat_wt / "feat.txt").write_text("feature work\n")
    _git("add", "feat.txt", cwd=feat_wt)
    _git("commit", "-q", "-m", "feat: my work", cwd=feat_wt)
    _git("worktree", "remove", str(feat_wt), cwd=repo)

    # Note the main checkout's branch and HEAD before merge
    main_branch_before = _git("branch", "--show-current", cwd=repo)
    main_head_before = _git("rev-parse", "HEAD", cwd=repo)
    assert main_branch_before == "main"

    _create_item("atk-my-feature", "release-1", repo / "f.txt", monkeypatch)

    # Run integration-merge
    args = _Args(id="atk-my-feature", repo=str(repo))
    dev_status_impl.cmd_integration_merge(args)

    # Verify main checkout's branch and HEAD are UNTOUCHED
    assert _git("branch", "--show-current", cwd=repo) == main_branch_before
    assert _git("rev-parse", "HEAD", cwd=repo) == main_head_before

    # Verify release-1 now has the feature commit and is a merge commit (--no-ff)
    rel_log = _git("log", "--oneline", "release-1", cwd=repo)
    assert "feat: my work" in rel_log
    parents = _git("rev-list", "--parents", "-n", "1", "release-1", cwd=repo).split()
    assert len(parents) == 3  # [commit_sha, parent1_sha, parent2_sha]


def test_integration_merge_refuses_when_no_integration_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _create_item("atk-no-int-branch", None, repo / "f.txt", monkeypatch)

    args = _Args(id="atk-no-int-branch", repo=str(repo))
    with pytest.raises(SystemExit) as exc:
        dev_status_impl.cmd_integration_merge(args)
    assert exc.value.code == 1


def test_integration_merge_with_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Bare origin
    bare = tmp_path / "origin.git"
    _git("init", "--bare", str(bare), cwd=tmp_path)

    # Local clone
    repo = _init_repo(tmp_path / "local")
    _git("remote", "add", "origin", str(bare), cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)
    _git("branch", "release-1", cwd=repo)
    _git("push", "-u", "origin", "release-1", cwd=repo)

    # Feature branch
    _git("branch", "atk-push-feat", "release-1", cwd=repo)
    feat_wt = tmp_path / "push-wt"
    _git("worktree", "add", "-q", str(feat_wt), "atk-push-feat", cwd=repo)
    (feat_wt / "push.txt").write_text("pushed\n")
    _git("add", "push.txt", cwd=feat_wt)
    _git("commit", "-q", "-m", "feat: push me", cwd=feat_wt)
    _git("worktree", "remove", str(feat_wt), cwd=repo)

    _create_item("atk-push-feat", "release-1", repo / "f.txt", monkeypatch)

    args = _Args(id="atk-push-feat", repo=str(repo), push=True)
    dev_status_impl.cmd_integration_merge(args)

    # Verify origin has the commit on release-1
    bare_log = _git("log", "--oneline", "release-1", cwd=bare)
    assert "feat: push me" in bare_log


def test_integration_merge_already_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git("branch", "release-1", cwd=repo)
    _create_item("atk-already", "release-1", repo / "f.txt", monkeypatch)

    args = _Args(id="atk-already", repo=str(repo), branch="main")
    dev_status_impl.cmd_integration_merge(args)
    captured = capsys.readouterr()
    assert "already merged" in captured.out


def test_integration_merge_conflict_aborts_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git("branch", "release-1", cwd=repo)

    # Make conflicting change on release-1 via worktree
    rel_wt = tmp_path / "rel-wt"
    _git("worktree", "add", "-q", str(rel_wt), "release-1", cwd=repo)
    (rel_wt / "f.txt").write_text("conflict branch 1\n")
    _git("add", "f.txt", cwd=rel_wt)
    _git("commit", "-q", "-m", "rel commit", cwd=rel_wt)
    _git("worktree", "remove", str(rel_wt), cwd=repo)

    # Feature branch with conflict
    _git("branch", "atk-conflict-feat", "main", cwd=repo)
    feat_wt = tmp_path / "feat-wt"
    _git("worktree", "add", "-q", str(feat_wt), "atk-conflict-feat", cwd=repo)
    (feat_wt / "f.txt").write_text("conflict branch 2\n")
    _git("add", "f.txt", cwd=feat_wt)
    _git("commit", "-q", "-m", "feat commit", cwd=feat_wt)
    _git("worktree", "remove", str(feat_wt), cwd=repo)

    _create_item("atk-conflict-feat", "release-1", repo / "f.txt", monkeypatch)

    args = _Args(id="atk-conflict-feat", repo=str(repo))
    with pytest.raises(SystemExit) as exc:
        dev_status_impl.cmd_integration_merge(args)
    assert exc.value.code == 1

    # Check that no temporary worktrees remain
    worktrees = _git("worktree", "list", cwd=repo)
    assert "merge-release-1" not in worktrees


def test_integration_merge_resolves_repo_from_related_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git("branch", "release-1", cwd=repo)
    _git("branch", "atk-auto-repo", "release-1", cwd=repo)

    _create_item("atk-auto-repo", "release-1", repo / "f.txt", monkeypatch)

    # Note: repo is NOT passed in args, inferred from related_files
    args = _Args(id="atk-auto-repo")
    dev_status_impl.cmd_integration_merge(args)


def test_integration_merge_refuses_when_target_branch_checked_out_in_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git("branch", "release-1", cwd=repo)
    _git("branch", "atk-feat", "release-1", cwd=repo)

    # Check out release-1 in a worktree
    rel_wt = tmp_path / "rel-wt"
    _git("worktree", "add", "-q", str(rel_wt), "release-1", cwd=repo)

    _create_item("atk-feat", "release-1", repo / "f.txt", monkeypatch)

    args = _Args(id="atk-feat", repo=str(repo))
    with pytest.raises(SystemExit) as exc:
        dev_status_impl.cmd_integration_merge(args)
    assert exc.value.code == 1

    captured = capsys.readouterr()
    assert "currently checked out in a worktree" in captured.err
    assert "desync" in captured.err


def test_integration_merge_fails_on_concurrent_ref_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git("branch", "release-1", cwd=repo)
    _git("branch", "atk-concurrent-feat", "release-1", cwd=repo)

    # Commit to feature branch via worktree
    feat_wt = tmp_path / "feat-wt"
    _git("worktree", "add", "-q", str(feat_wt), "atk-concurrent-feat", cwd=repo)
    (feat_wt / "feat.txt").write_text("feature work\n")
    _git("add", "feat.txt", cwd=feat_wt)
    _git("commit", "-q", "-m", "feat: work", cwd=feat_wt)
    _git("worktree", "remove", str(feat_wt), cwd=repo)

    _create_item("atk-concurrent-feat", "release-1", repo / "f.txt", monkeypatch)

    # Monkeypatch _run_git so right after merge (in tmp_dir), release-1 in repo_root moves!
    (repo / "other.txt").write_text("concurrent\n")
    _git("add", "other.txt", cwd=repo)
    _git("commit", "-q", "-m", "concurrent commit", cwd=repo)

    orig_run_git = dev_status_impl._run_git

    def hooked_run_git(cmd, cwd=None, **kwargs):
        res = orig_run_git(cmd, cwd, **kwargs)
        if cmd[0] == "merge":
            # Concurrently advance release-1 in repo_root to the new commit
            subprocess.run(
                ["git", "branch", "-f", "release-1", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
        return res

    monkeypatch.setattr(dev_status_impl, "_run_git", hooked_run_git)

    args = _Args(id="atk-concurrent-feat", repo=str(repo))
    with pytest.raises(SystemExit) as exc:
        dev_status_impl.cmd_integration_merge(args)
    assert exc.value.code == 1

    captured = capsys.readouterr()
    assert "error updating branch 'release-1'" in captured.err
