#!/usr/bin/env python3
"""Commit-time behaviour of the githooks-global slug-check pre-commit hook.

Real git runs only via subprocess against throwaway fixture repos under a
per-test sandboxed HOME (marked ``allow_real_subprocess`` per test/AGENTS.md);
the hook shells out to the repo's own ``dev_status.py slug-check``, which
reads the sandboxed store. Nothing touches real user state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import test_layouts  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # real git index/HEAD behaviour
# is the subject; every invocation runs in a throwaway tmp_path repo with a
# per-test sandbox HOME, touching nothing outside it.


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _store_file(home: Path) -> Path:
    return home / ".agent-toolkit" / "data" / "backlog" / "items.json"


def _write_store(home: Path, slugs: list[str]) -> None:
    target = _store_file(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "items": [
                    {
                        "id": s,
                        "status": "open",
                        "created": "2026-01-01",
                        "updated": "2026-01-01",
                        "summary": f"Summary of {s}",
                        "category": "feature",
                        "blocked_by": [],
                        "related_files": [],
                        "context": "",
                        "next_steps": "",
                    }
                    for s in slugs
                ],
            },
            indent=2,
        )
    )


def _write_corrupt_store(home: Path) -> None:
    target = _store_file(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ this is not valid json")


def _wire_repo(path: Path) -> None:
    """Init a feature-branch repo with one seed commit and the global hook.

    The seed commit happens *before* the hook is installed: the hook is
    fail-closed on a missing store, and the store is only written by each
    test after this fixture returns, so seeding with the hook active would
    make the seed commit refuse and leave the repo unborn (a detached-HEAD
    checkout on an unborn HEAD then fails too).
    """
    path.mkdir()
    _git(path, "init", "-q", "-b", "feature", ".")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "gc.auto", "0")
    _git(path, "config", "maintenance.auto", "false")
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", "seed.txt")
    seed = _git(path, "commit", "-q", "-m", "seed")
    assert seed.returncode == 0, seed.stderr
    _git(path, "config", "core.hooksPath", str(REPO_ROOT / "githooks-global"))


def _commit(repo: Path, relpath: str, text: str) -> subprocess.CompletedProcess[str]:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", relpath)
    return _git(repo, "commit", "-q", "-m", "change")


@pytest.fixture
def repo(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[Path, Path]:
    """A throwaway git repo wired to the real githooks-global hook, plus its
    per-test sandbox HOME (so the store is isolated per test)."""
    home = test_layouts.activate_sandbox_home(
        tmp_path / "home", request.addfinalizer
    ).home
    path = tmp_path / "repo"
    _wire_repo(path)
    return path, home


def test_clean_commit_passes(repo: tuple[Path, Path]) -> None:
    path, home = repo
    _write_store(home, ["atk-clean"])
    result = _commit(path, "f.txt", "clean content\n")
    assert result.returncode == 0, result.stderr


def test_slug_leak_refused(repo: tuple[Path, Path]) -> None:
    path, home = repo
    _write_store(home, ["atk-hook-leak"])
    result = _commit(path, "notes.md", "see atk-hook-leak for context\n")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "atk-hook-leak" in combined
    assert "--no-verify" in combined


def test_corrupt_store_fails_closed(repo: tuple[Path, Path]) -> None:
    path, home = repo
    _write_corrupt_store(home)
    result = _commit(path, "f.txt", "clean content\n")
    assert result.returncode != 0


def test_missing_store_fails_closed(repo: tuple[Path, Path]) -> None:
    path, _home = repo
    # No store written at all: slug-check refuses (fail-closed), never clean.
    result = _commit(path, "f.txt", "clean content\n")
    assert result.returncode != 0


def test_detached_head_slug_leak_refused(repo: tuple[Path, Path]) -> None:
    path, home = repo
    _write_store(home, ["atk-detached"])
    _git(path, "checkout", "-q", "--detach")
    # Guard the fixture: a detached HEAD has no symbolic ref, so this fails
    # iff the checkout above did not actually detach (e.g. unborn HEAD).
    detached = _git(path, "symbolic-ref", "--short", "-q", "HEAD")
    assert detached.returncode != 0
    result = _commit(path, "d.md", "about atk-detached\n")
    assert result.returncode != 0
    assert "atk-detached" in (result.stdout + result.stderr)


def test_main_commit_short_circuits_before_slug_check(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A commit that would move main is refused by the branch guard, and the
    slug scan is never reached -- its clean result prints to *stdout*, so
    absence from the combined stream (not just stderr) proves it did not run.
    """
    home = test_layouts.activate_sandbox_home(
        tmp_path / "home", request.addfinalizer
    ).home
    _write_store(home, ["atk-on-main"])
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main", ".")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "gc.auto", "0")
    _git(path, "config", "maintenance.auto", "false")
    _git(path, "config", "core.hooksPath", str(REPO_ROOT / "githooks-global"))
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-q", "-m", "seed")
    result = _commit(path, "leak.md", "mentions atk-on-main\n")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "blocked" in combined
    assert "slug-check" not in combined


def test_local_hook_also_refuses_slug_leak(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The repo's own githooks/pre-commit (wired via `core.hooksPath githooks`)
    runs the same slug scan as the global hook -- otherwise this checkout,
    whose local hook overrides the global one, would be the one repo the
    guard never protects. The seed guard passes (no claude/settings.json in
    HEAD), so the refusal is attributable to slug-check, not the seed guard.
    """
    home = test_layouts.activate_sandbox_home(
        tmp_path / "home", request.addfinalizer
    ).home
    _write_store(home, ["atk-local-hook"])
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "feature", ".")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "gc.auto", "0")
    _git(path, "config", "maintenance.auto", "false")
    # Seed first, without the hook: the hook fails closed on a missing store,
    # and the store is only written above, so seeding through the hook would
    # needlessly couple the seed commit to slug-check.
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", "seed.txt")
    seed = _git(path, "commit", "-q", "-m", "seed")
    assert seed.returncode == 0, seed.stderr
    # The local hook resolves agent-scripts/ and githooks-global/ relative to
    # the committing repo; symlink both so dev_status.py and the shared libs
    # resolve to the real checkout (dev_status.py's sibling imports follow the
    # symlink's realpath).
    (path / "agent-scripts").symlink_to(
        REPO_ROOT / "agent-scripts", target_is_directory=True
    )
    (path / "githooks-global").symlink_to(
        REPO_ROOT / "githooks-global", target_is_directory=True
    )
    (path / "githooks").mkdir()
    (path / "githooks" / "pre-commit").write_text(
        (REPO_ROOT / "githooks" / "pre-commit").read_text()
    )
    (path / "githooks" / "pre-commit").chmod(0o755)
    _git(path, "config", "core.hooksPath", "githooks")
    result = _commit(path, "notes.md", "mentions atk-local-hook\n")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "atk-local-hook" in combined
    assert "--no-verify" in combined


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
