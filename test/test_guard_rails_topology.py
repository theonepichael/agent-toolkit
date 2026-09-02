#!/usr/bin/env python3
"""Topology behaviour of guard_rails.py against REAL git repositories.

These deliberately do not mock git. The whole rule turns on how git actually
reports ``--git-dir`` / ``--git-common-dir`` -- relative or absolute, for a
worktree whose ``.git`` is a file, for a bare repo, for a submodule. Mocked
output would encode the very assumptions under test and pass while the real
thing failed. ``test/AGENTS.md`` names test_pi_ts_checks.py as the precedent
for reaching for the marker when the real thing is the point.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude" / "scripts"))

import guard_rails  # noqa: E402

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
    (path / "f.txt").write_text("a\n")
    _git("add", "f.txt", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


@pytest.fixture
def main_checkout(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "proj")


@pytest.fixture
def worktree(main_checkout: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "proj-feature"
    _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main_checkout)
    return wt


def test_main_checkout_is_not_reported_as_a_worktree(main_checkout: Path) -> None:
    info = guard_rails.repo_info(str(main_checkout))
    assert info is not None
    assert info.is_worktree is False
    assert info.is_bare is False
    assert info.branch == "main"


def test_linked_worktree_is_reported_as_a_worktree(worktree: Path) -> None:
    info = guard_rails.repo_info(str(worktree))
    assert info is not None
    assert info.is_worktree is True
    assert info.branch == "feature"


def test_worktree_and_main_checkout_share_a_common_dir(
    main_checkout: Path, worktree: Path
) -> None:
    """This is the identity R2 matches on."""
    assert guard_rails.common_dir_of(str(worktree)) == guard_rails.common_dir_of(
        str(main_checkout)
    )


def test_toplevel_would_NOT_match_and_that_is_why_it_is_not_used(
    main_checkout: Path, worktree: Path
) -> None:
    """Pins the fatal bug this design avoids: matching on --show-toplevel
    decides a worktree and its main checkout are different repositories, so
    R2 would allow the very edit it exists to deny."""
    assert _git("rev-parse", "--show-toplevel", cwd=worktree) != _git(
        "rev-parse", "--show-toplevel", cwd=main_checkout
    )


def test_common_dir_is_absolutized_against_the_target_not_the_process_cwd(
    main_checkout: Path, tmp_path: Path
) -> None:
    """git -C <dir> rev-parse --git-common-dir prints relative to <dir>. A
    bare realpath() would resolve it against wherever the guard runs, giving
    a path that does not exist -- and R2 would silently never fire."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = os.getcwd()
    os.chdir(elsewhere)
    try:
        resolved = guard_rails.common_dir_of(str(main_checkout))
    finally:
        os.chdir(before)
    assert resolved is not None
    assert Path(resolved).exists(), resolved
    assert str(elsewhere) not in resolved
    assert resolved == os.path.realpath(str(main_checkout / ".git"))


def test_bare_repo_is_detected_and_allowed(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git("init", "-q", "--bare", ".", cwd=bare)
    info = guard_rails.repo_info(str(bare))
    assert info is not None
    assert info.is_bare is True
    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(bare), str(bare / "x.txt"))
    )
    assert verdict.decision == "allow"


def test_submodule_resolves_to_its_own_repository(
    main_checkout: Path, tmp_path: Path
) -> None:
    """A submodule is a distinct repo and must be matched on its own
    identity, not folded into the superproject's."""
    inner = _init_repo(tmp_path / "inner")
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(inner),
        "sub",
        cwd=main_checkout,
    )
    sub = main_checkout / "sub"
    assert guard_rails.common_dir_of(str(sub)) != guard_rails.common_dir_of(
        str(main_checkout)
    )


def test_path_outside_any_repo_allows(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(plain), str(plain / "a.txt"))
    )
    assert verdict.decision == "allow"


def test_busy_main_checkout_denies_end_to_end(
    main_checkout: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole rule, against real git: an item whose related_files point at
    the main checkout, while the edit targets that same main checkout."""
    items = [
        {
            "id": "demo-slug",
            "status": "in-progress",
            "related_files": [{"path": str(main_checkout / "f.txt")}],
        }
    ]
    monkeypatch.setattr(guard_rails, "load_in_progress", lambda: items)
    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(main_checkout), str(main_checkout / "f.txt"))
    )
    assert verdict.decision == "deny"
    assert "demo-slug" in verdict.reason

    # The same item must not block work in the worktree -- that is the point.
    allowed = guard_rails.evaluate(
        guard_rails.Request("write", str(worktree), str(worktree / "f.txt"))
    )
    assert allowed.decision == "allow"


def test_no_matching_item_allows_the_main_checkout_edit(
    main_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard_rails, "load_in_progress", list)
    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(main_checkout), str(main_checkout / "f.txt"))
    )
    assert verdict.decision == "allow"


def test_stale_worktree_base_warns_and_never_denies(
    main_checkout: Path, tmp_path: Path
) -> None:
    """origin/main ahead of the worktree's HEAD must produce a warn, not a
    block, and must not need the network."""
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(main_checkout), str(clone)],
        capture_output=True,
        check=True,
    )
    _git("config", "user.email", "t@example.invalid", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    wt = tmp_path / "clone-feature"
    _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=clone)

    # Move origin/main forward without touching the worktree.
    (main_checkout / "f.txt").write_text("b\n")
    _git("add", "f.txt", cwd=main_checkout)
    _git("commit", "-q", "-m", "advance", cwd=main_checkout)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)

    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(wt), str(wt / "f.txt"))
    )
    assert verdict.decision == "warn"
    assert "origin/main" in verdict.reason


def test_up_to_date_worktree_does_not_warn(main_checkout: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone2"
    subprocess.run(
        ["git", "clone", "-q", str(main_checkout), str(clone)],
        capture_output=True,
        check=True,
    )
    wt = tmp_path / "clone2-feature"
    _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=clone)
    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(wt), str(wt / "f.txt"))
    )
    assert verdict.decision == "allow"


def test_repo_with_no_remote_never_warns(worktree: Path) -> None:
    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(worktree), str(worktree / "f.txt"))
    )
    assert verdict.decision == "allow"


def test_escape_hatch_allows_a_busy_main_checkout(
    main_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [
        {
            "id": "demo-slug",
            "status": "in-progress",
            "related_files": [{"path": str(main_checkout / "f.txt")}],
        }
    ]
    monkeypatch.setattr(guard_rails, "load_in_progress", lambda: items)
    monkeypatch.setenv("GUARD_RAILS_OFF", "1")
    verdict = guard_rails.evaluate(
        guard_rails.Request("write", str(main_checkout), str(main_checkout / "f.txt"))
    )
    assert verdict.decision == "allow"


# ── bash-family override check, against real repos ────────────────────────


def test_bash_override_denies_no_verify_on_main(main_checkout: Path) -> None:
    verdict = guard_rails.evaluate_bash_override(
        "git commit --no-verify -m x", str(main_checkout)
    )
    assert verdict.decision == "deny"


def test_bash_override_allows_no_verify_on_a_feature_branch(worktree: Path) -> None:
    verdict = guard_rails.evaluate_bash_override(
        "git commit --no-verify -m x", str(worktree)
    )
    assert verdict.decision == "allow"


def test_bash_override_allows_short_n_alias_on_main(main_checkout: Path) -> None:
    verdict = guard_rails.evaluate_bash_override(
        "git commit -n -m x", str(main_checkout)
    )
    assert verdict.decision == "allow"


def test_bash_override_denies_hookspath_unset_on_main(main_checkout: Path) -> None:
    verdict = guard_rails.evaluate_bash_override(
        "git config --unset core.hooksPath", str(main_checkout)
    )
    assert verdict.decision == "deny"


def test_bash_override_allows_hookspath_read_on_main(main_checkout: Path) -> None:
    verdict = guard_rails.evaluate_bash_override(
        "git config --get core.hooksPath", str(main_checkout)
    )
    assert verdict.decision == "allow"


def test_bash_override_allows_chained_hookspath_read_on_main(
    main_checkout: Path,
) -> None:
    verdict = guard_rails.evaluate_bash_override(
        "git config core.hooksPath || echo unset", str(main_checkout)
    )
    assert verdict.decision == "allow"


def test_bash_override_allows_on_detached_head(main_checkout: Path) -> None:
    _git("checkout", "-q", "--detach", cwd=main_checkout)
    verdict = guard_rails.evaluate_bash_override(
        "git config --unset core.hooksPath", str(main_checkout)
    )
    assert verdict.decision == "allow"


# ── the shipped git hook itself, against real commits ──────────────────────


def _install_global_hook(repo: Path) -> None:
    hooks_global = REPO_ROOT / "githooks-global"
    _git("config", "--local", "core.hooksPath", str(hooks_global), cwd=repo)


def test_hook_allows_the_very_first_commit_on_unborn_main(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _install_global_hook(repo)
    (repo / "f.txt").write_text("a\n")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)  # must not raise (check=True)


def test_hook_blocks_a_second_direct_commit_on_main(main_checkout: Path) -> None:
    _install_global_hook(main_checkout)
    (main_checkout / "f.txt").write_text("b\n")
    _git("add", "f.txt", cwd=main_checkout)
    result = subprocess.run(
        ["git", "commit", "-q", "-m", "second"],
        cwd=main_checkout,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "blocked" in result.stderr


def test_hook_allows_a_commit_on_a_feature_branch(worktree: Path) -> None:
    _install_global_hook(worktree)
    (worktree / "f.txt").write_text("c\n")
    _git("add", "f.txt", cwd=worktree)
    _git("commit", "-q", "-m", "feature commit", cwd=worktree)  # must not raise


def test_hook_allows_a_commit_from_detached_head(main_checkout: Path) -> None:
    _install_global_hook(main_checkout)
    _git("checkout", "-q", "--detach", cwd=main_checkout)
    (main_checkout / "f.txt").write_text("d\n")
    _git("add", "f.txt", cwd=main_checkout)
    _git("commit", "-q", "-m", "detached commit", cwd=main_checkout)  # must not raise


def test_repo_local_override_hook_still_runs_its_own_checks(
    main_checkout: Path,
) -> None:
    """A repo with its own local core.hooksPath override (like this dotfiles
    checkout's githooks/) picks up the branch check by direct addition, not
    by chaining to the global hook -- so a repo that has never pointed at
    githooks-global must still be protected once its own local hook sources
    the shared lib directly."""
    lib = REPO_ROOT / "githooks-global" / "lib" / "no-commit-on-main.sh"
    local_hooks = main_checkout / "myhooks"
    local_hooks.mkdir()
    (local_hooks / "pre-commit").write_text(
        f'#!/usr/bin/env bash\nset -uo pipefail\nsource "{lib}"\n'
        "refuse_if_protected_branch || exit $?\n"
    )
    (local_hooks / "pre-commit").chmod(0o755)
    _git("config", "--local", "core.hooksPath", str(local_hooks), cwd=main_checkout)

    (main_checkout / "f.txt").write_text("e\n")
    _git("add", "f.txt", cwd=main_checkout)
    result = subprocess.run(
        ["git", "commit", "-q", "-m", "should be blocked"],
        cwd=main_checkout,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "blocked" in result.stderr
