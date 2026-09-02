"""Guards the repo's agent-instruction file convention.

No single filename reaches every harness this repo supports. Measured
2026-08-30 against the installed binaries: Claude Code 2.1.251 loads
``CLAUDE.md`` and ignores ``AGENTS.md`` entirely; opencode 1.18.25 loads
``AGENTS.md`` and never ``CLAUDE.md``; Pi 0.84.4 prefers ``AGENTS.md`` and
falls back to ``CLAUDE.md``; Copilot 1.0.80 reads all of them. So every
directory carrying agent instructions holds one real ``AGENTS.md`` plus a
``CLAUDE.md`` symlink pointing at it -- one source of truth, two names.

Existence alone is not enough to keep that working, which is why these
assertions read the git index rather than the worktree:

* A ``CLAUDE.md`` committed as a regular file (what a ``core.symlinks=false``
  checkout produces) still "exists" and still passes a worktree check, but
  every harness then reads the single line ``AGENTS.md`` as the whole
  instruction set.
* Resolving the target through the worktree with ``Path.readlink()`` raises
  ``OSError`` on exactly that checkout -- crashing the runner instead of
  failing an assertion. Reading the symlink blob out of the index gives one
  data source and a legible failure.

Directories are discovered, never listed, so a directory added later is
covered with no edit here.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SYMLINK_MODE = "120000"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "-c", "core.quotepath=false", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _tracked_entries() -> dict[str, tuple[str, str]]:
    """Map every tracked path to its (mode, blob hash) from the index.

    ``git ls-files -s`` emits ``<mode> <hash> <stage>\\tpath``. The path is
    split on the tab, never on spaces, and mode/hash are fields 0 and 1 of
    the left half so the stage column can never be mistaken for the hash.
    """
    entries: dict[str, tuple[str, str]] = {}
    for line in _git("ls-files", "-s").splitlines():
        if not line:
            continue
        meta, _, path = line.partition("\t")
        mode, obj_hash = meta.split()[:2]
        entries[path] = (mode, obj_hash)
    return entries


def _paths_named(entries: dict[str, tuple[str, str]], name: str) -> list[str]:
    """Exact basename matches only.

    A ``*CLAUDE*`` glob or an ``endswith`` would sweep in the four
    CLAUDE_CODE_PARITY.md files (agy/, copilot/, opencode/, pi/) and fail
    immediately on files that have nothing to do with this convention.
    """
    return sorted(p for p in entries if Path(p).name == name)


def _symlink_target(obj_hash: str) -> str:
    """A symlink blob's content is its target path."""
    return _git("cat-file", "-p", obj_hash).strip()


@pytest.mark.allow_real_subprocess  # reads the git index; writes nothing
def test_every_agents_md_has_a_claude_md_symlink() -> None:
    entries = _tracked_entries()
    problems: list[str] = []

    for agents_path in _paths_named(entries, "AGENTS.md"):
        sibling = str(Path(agents_path).parent / "CLAUDE.md")
        if sibling == ".":  # pragma: no cover - defensive
            continue
        if sibling not in entries:
            problems.append(f"{agents_path}: no {sibling} tracked beside it")
            continue

        mode, obj_hash = entries[sibling]
        if mode != SYMLINK_MODE:
            problems.append(
                f"{sibling}: committed with mode {mode}, expected "
                f"{SYMLINK_MODE} (a symlink). A regular file here means "
                f"every harness reads its contents as the whole instruction "
                f"set."
            )
            continue

        target = _symlink_target(obj_hash)
        if target != "AGENTS.md":
            problems.append(
                f"{sibling}: points at {target!r}, expected 'AGENTS.md' -- "
                f"the relative sibling in its own directory."
            )

    assert not problems, "agent-instruction pairing broken:\n" + "\n".join(problems)


@pytest.mark.allow_real_subprocess  # reads the git index; writes nothing
def test_no_claude_md_without_an_agents_md() -> None:
    """The reverse direction: a CLAUDE.md with no AGENTS.md beside it is a
    real file that only Claude Code and Copilot can read, which is the
    situation this convention exists to end."""
    entries = _tracked_entries()
    orphans = [
        p
        for p in _paths_named(entries, "CLAUDE.md")
        if str(Path(p).parent / "AGENTS.md") not in entries
    ]

    assert not orphans, (
        "CLAUDE.md with no AGENTS.md beside it: "
        f"{orphans}. Make AGENTS.md the real file and CLAUDE.md a symlink to "
        "it."
    )
