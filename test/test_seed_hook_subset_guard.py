#!/usr/bin/env python3
"""Commit-time behaviour of seed_hook_subset_guard.py against REAL git repositories.

These deliberately do not mock git. The whole rule turns on what git actually
reports for the staged (index) blob versus the HEAD blob — including unmerged
index states and unborn HEADs — and mocked output would encode the very
assumptions under test. test_guard_rails_topology.py is the precedent for
reaching for the real-subprocess marker when the real thing is the point.

The lossy shape under test is the 35fc7f7 incident: a concurrent writer
deleted the matcher-'*' herdr SessionStart group from the worktree seed
between verification and commit, and the clobber was staged and committed.
The guard must refuse exactly that transition, keep passing the approved
9→wrapper collapse that kept the group, and keep out of the way otherwise.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import seed_hook_subset_guard as guard  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # real git index/HEAD behaviour
# is the subject under test; every invocation runs in a throwaway tmp_path
# repo with a throwaway user config, touching nothing outside it.

MAIN_SEED = "claude/settings.json"
WORK_SEED = "claude/settings.work.json"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "feature", ".", cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    return path


def _seed(groups: list[tuple[str | None, int]]) -> str:
    """Build a seed whose SessionStart has one group per (matcher, n_hooks)."""
    entries = []
    for matcher, n in groups:
        hooks = [{"type": "command", "command": f"echo hook-{i}"} for i in range(n)]
        group = {"hooks": hooks}
        if matcher is not None:
            group["matcher"] = matcher
        entries.append(group)
    import json

    return json.dumps({"hooks": {"SessionStart": entries}}, indent=2)


def _commit_seed(path: Path, seeds: dict[str, str], message: str = "seed") -> None:
    for rel, text in seeds.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git("add", rel, cwd=path)
    (path / "README.md").write_text("repo\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)


def _stage(path: Path, seeds: dict[str, str]) -> None:
    for rel, text in seeds.items():
        target = path / rel
        if text is None:
            _git("rm", "-q", "--cached", rel, cwd=path)
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git("add", rel, cwd=path)


def _run_guard(
    path: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("SEED_HOOK_ALLOW_DROP", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(guard.__file__)],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
    )


# ── the incident shape ────────────────────────────────────────────────────


def test_incident_shape_herdr_group_loss_fails(tmp_path: Path) -> None:
    """HEAD {None,'*'} → staged {None} must be refused, naming the lost group."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1)])})

    result = _run_guard(repo)

    assert result.returncode == 1
    assert "*"
    assert "matcher '*'" in result.stdout + result.stderr
    assert MAIN_SEED in result.stdout + result.stderr
    assert "SEED_HOOK_ALLOW_DROP" in result.stdout + result.stderr


def test_known_good_collapse_keeping_group_passes(tmp_path: Path) -> None:
    """The 15da306 shape: 9-entry group collapses to a wrapper, '*' is kept."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 9), ("*", 1)])})
    staged = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 sessionstart_checks.py",
                        }
                    ]
                },
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "bash herdr-agent-state.sh"}
                    ],
                },
            ]
        }
    }
    import json

    _stage(repo, {MAIN_SEED: json.dumps(staged, indent=2)})

    result = _run_guard(repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_loss_with_simultaneous_addition_still_fails(tmp_path: Path) -> None:
    """{None,'*'} → {None,'Beta'} loses '*' under an addition; a plain
    set-subset test would miss it."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1), ("Beta", 1)])})

    result = _run_guard(repo)

    assert result.returncode == 1
    assert "matcher '*'" in result.stdout + result.stderr


def test_duplicate_matcher_count_drop_fails(tmp_path: Path) -> None:
    """Two matcher-less groups losing one is a loss; sets alone would hide it."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), (None, 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1)])})

    result = _run_guard(repo)

    assert result.returncode == 1


# ── everything that must keep passing ─────────────────────────────────────


def test_unchanged_seed_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    seed = _seed([(None, 1), ("*", 1)])
    _commit_seed(repo, {MAIN_SEED: seed})
    _stage(repo, {MAIN_SEED: seed})

    assert _run_guard(repo).returncode == 0


def test_added_group_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})

    assert _run_guard(repo).returncode == 0


def test_in_group_rewrite_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 3)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 2)])})

    assert _run_guard(repo).returncode == 0


def test_staged_content_read_not_worktree(tmp_path: Path) -> None:
    """A concurrent writer can change the worktree file after staging — the
    guard must judge the index version (the race behind the incident)."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    # Rewrite the worktree file WITHOUT staging it: the index still holds the
    # intact seed, so the guard must pass even though the worktree is lossy.
    (repo / MAIN_SEED).write_text(_seed([(None, 1)]))

    assert _run_guard(repo).returncode == 0


def test_unrelated_file_staged_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    (repo / "other.txt").write_text("x\n")
    _git("add", "other.txt", cwd=repo)

    assert _run_guard(repo).returncode == 0


def test_seed_not_in_head_passes(tmp_path: Path) -> None:
    """First addition of a seed: nothing in HEAD to lose."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {})
    _stage(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})

    assert _run_guard(repo).returncode == 0


def test_unborn_head_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, {MAIN_SEED: _seed([(None, 1)])})

    assert _run_guard(repo).returncode == 0


# ── per-file coverage and aggregation ─────────────────────────────────────


def test_work_seed_loss_fails_while_main_clean(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(
        repo,
        {
            MAIN_SEED: _seed([(None, 1), ("*", 1)]),
            WORK_SEED: _seed([(None, 4)]),
        },
    )
    _stage(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)]), WORK_SEED: _seed([])})

    result = _run_guard(repo)

    assert result.returncode == 1
    assert WORK_SEED in result.stdout + result.stderr


def test_both_files_losing_are_reported_together(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(
        repo,
        {
            MAIN_SEED: _seed([(None, 1), ("*", 1)]),
            WORK_SEED: _seed([(None, 4), ("*", 1)]),
        },
    )
    _stage(repo, {MAIN_SEED: _seed([(None, 1)]), WORK_SEED: _seed([(None, 4)])})

    result = _run_guard(repo)

    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert MAIN_SEED in combined
    assert WORK_SEED in combined


def test_seed_deleted_from_index_fails(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _stage(repo, {MAIN_SEED: None})

    result = _run_guard(repo)

    assert result.returncode == 1


# ── edge cases ────────────────────────────────────────────────────────────


def test_missing_sessionstart_keys_pass(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: '{"permissions": {"allow": []}}'})
    _stage(repo, {MAIN_SEED: '{"model": "opus"}'})

    assert _run_guard(repo).returncode == 0


def test_malformed_staged_json_fails(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1)])})
    _stage(repo, {MAIN_SEED: "{not json"})

    result = _run_guard(repo)

    assert result.returncode == 1
    assert MAIN_SEED in result.stdout + result.stderr
    assert "parse" in result.stdout + result.stderr


def test_unmerged_index_skips_with_note(tmp_path: Path) -> None:
    """Mid-merge: the resolution decides the content; skip, do not block."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _git("checkout", "-q", "-b", "side", cwd=repo)
    _stage(repo, {MAIN_SEED: _seed([(None, 2), ("*", 1)])})
    _git("commit", "-q", "-am", "side edit", cwd=repo)
    _git("checkout", "-q", "feature", cwd=repo)
    _stage(repo, {MAIN_SEED: _seed([(None, 3), ("*", 1)])})
    _git("commit", "-q", "-am", "feature edit", cwd=repo)
    merge = subprocess.run(
        ["git", "merge", "side"], cwd=repo, capture_output=True, text=True
    )
    assert merge.returncode != 0  # conflict
    for line in _git("ls-files", "-u", cwd=repo).splitlines():
        assert line, line

    result = _run_guard(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "unmerged" in combined.lower() or "conflict" in combined.lower()


def test_bypass_env_skips_guard(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1)])})

    result = _run_guard(repo, extra_env={"SEED_HOOK_ALLOW_DROP": "1"})

    assert result.returncode == 0


def test_guard_is_fast(tmp_path: Path) -> None:
    """The check itself must stay well under the spec's 100 ms budget."""
    import time

    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})

    start = time.monotonic()
    assert _run_guard(repo).returncode == 0
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"guard took {elapsed:.2f}s including interpreter start"


# ── the hook really runs it ───────────────────────────────────────────────


def test_wired_hook_refuses_lossy_commit(tmp_path: Path) -> None:
    """A commit through the repo's real githooks/pre-commit is refused on the
    incident shape. The gen_* checks later in the hook are never reached here
    (the guard exits first), so a throwaway repo without agent-scripts is
    enough; the hook's pass path is exercised by every wired commit in the
    real checkout instead."""
    repo = _init_repo(tmp_path)
    _commit_seed(repo, {MAIN_SEED: _seed([(None, 1), ("*", 1)])})
    _stage(repo, {MAIN_SEED: _seed([(None, 1)])})
    # Wire the real hook: copy the hook and the lib it sources.
    (repo / "githooks-global" / "lib").mkdir(parents=True)
    for src in (REPO_ROOT / "githooks-global" / "lib").iterdir():
        (repo / "githooks-global" / "lib" / src.name).write_text(src.read_text())
    (repo / "githooks").mkdir()
    (repo / "githooks" / "pre-commit").write_text(
        (REPO_ROOT / "githooks" / "pre-commit").read_text()
    )
    (repo / "githooks" / "pre-commit").chmod(0o755)
    # The hook sources its guard from the repo itself, so the temp repo needs
    # the script too.
    (repo / "agent-scripts").mkdir(parents=True)
    (repo / "agent-scripts" / "seed_hook_subset_guard.py").write_text(
        Path(guard.__file__).read_text()
    )

    result = subprocess.run(
        ["git", "-c", "core.hooksPath=githooks", "commit", "-m", "lossy"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "SessionStart" in combined
    assert "matcher '*'" in combined
