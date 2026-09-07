#!/usr/bin/env python3
"""seed_hook_subset_guard.py — refuse a commit that drops a seed's SessionStart
hook groups.

Compares the STAGED (index) versions of ``claude/settings.json`` and
``claude/settings.work.json`` against their committed (HEAD) versions and
exits 1 when a group has been lost. Wired from ``githooks/pre-commit``.

What counts as a loss
---------------------
Groups are identified by their ``matcher`` value (a missing or JSON-``null``
matcher is ``None``) and counted as a multiset. A loss is any matcher HEAD
has that staged has fewer of — including when the rewrite also adds some
other group in the same commit, which a plain set-subset test would miss.
Contents inside a group are deliberately not compared: collapsing many
entries into one wrapper command inside the matcher-less startup group is an
approved refactor shape, and per-command loss is the live drift-check's
domain.

Why staged, not the worktree file
---------------------------------
A concurrent writer can change the worktree file between a verification and
the commit — that exact race produced a committed seed that had silently
lost its matcher-'*' herdr group (a lossy rewrite by a second agent working
the same checkout). The index is what the commit will actually contain, and
git holds the index lock for the whole pre-commit hook, so this read cannot
race a concurrent ``git add``.

Escape hatch
------------
``SEED_HOOK_ALLOW_DROP=1`` skips this guard only (checked before any git or
JSON work), leaving every other check in ``githooks/pre-commit`` —
no-commit-on-main and the generated-doc checks — untouched. It exists
because an intentional group removal must not require ``git commit
--no-verify``, which would disable those other checks too.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from pathlib import Path

SEED_PATHS = ("claude/settings.json", "claude/settings.work.json")
BYPASS_ENV = "SEED_HOOK_ALLOW_DROP"


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True)


def _unborn_head(repo_root: Path) -> bool:
    return _git(repo_root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0


def _staged_blob(repo_root: Path, path: str) -> tuple[str | None, str | None]:
    """The index version of ``path``. Returns ``(blob, note)``: a note means
    the file was skipped (unmerged index state) and contributes no verdict."""
    ls = _git(repo_root, "ls-files", "--stage", "--", path)
    entries = [line.split() for line in ls.stdout.splitlines() if line.strip()]
    if not entries:
        return None, None  # absent from the index: deletion or new file
    if any(entry[2] != "0" for entry in entries):
        return None, f"  (skipped: {path} is unmerged — resolve the conflict first)"
    show = _git(repo_root, "show", f":{path}")
    if show.returncode != 0:
        return None, f"  (skipped: {path} could not be read from the index)"
    return show.stdout, None


def _head_blob(repo_root: Path, path: str) -> str | None:
    ls = _git(repo_root, "ls-tree", "HEAD", "--", path)
    if not ls.stdout.strip():
        return None  # not in HEAD: a new addition, nothing to lose
    show = _git(repo_root, "show", f"HEAD:{path}")
    return show.stdout if show.returncode == 0 else None


class _SeedParseError(ValueError):
    """A staged seed exists but is not a settings-shaped JSON document."""


def _sessionstart_matcher_counts(text: str) -> collections.Counter[object]:
    """Count SessionStart hook groups per matcher value (missing/null → None)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _SeedParseError(f"cannot parse JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise _SeedParseError("top level is not a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise _SeedParseError("'hooks' is not an object")
    groups = hooks.get("SessionStart", [])
    if not isinstance(groups, list):
        raise _SeedParseError("'hooks.SessionStart' is not a list")
    counts: collections.Counter[object] = collections.Counter()
    for group in groups:
        matcher = group.get("matcher") if isinstance(group, dict) else None
        counts[matcher if matcher is None else str(matcher)] += 1
    return counts


def _describe(matcher: object) -> str:
    return "(no matcher)" if matcher is None else f"matcher {matcher!r}"


def check_path(repo_root: Path, path: str) -> tuple[str | None, str | None]:
    """Return ``(failure_message, skip_note)`` for one seed path — exactly one
    is non-None. ``failure_message`` is a multi-line report of the lost
    group(s); ``skip_note`` explains why the file contributed no verdict."""
    staged, skip = _staged_blob(repo_root, path)
    if skip is not None:
        return None, skip
    head = _head_blob(repo_root, path)
    if head is None and staged is None:
        return None, None
    if head is None:
        return None, None  # new addition — nothing in HEAD to lose
    try:
        head_counts = _sessionstart_matcher_counts(head)
        staged_counts = _sessionstart_matcher_counts(staged or "")
    except _SeedParseError as exc:
        return f"seed_hook_subset_guard: {path}: {exc}", None
    lost = [m for m, n in head_counts.items() if staged_counts[m] < n]
    if not lost:
        return None, None
    lines = [
        (
            f"seed_hook_subset_guard: staged {path} would LOSE SessionStart hook "
            "group(s) relative to HEAD:"
        )
    ]
    for matcher in lost:
        lines.append(
            f"  {_describe(matcher)}: HEAD has {head_counts[matcher]}, "
            f"staged has {staged_counts[matcher]}"
        )
    lines.append(
        "The seed looks lossy-rewritten (a concurrent editor's change may have\n"
        "been staged unintentionally). If dropping the group(s) is intentional,\n"
        f"re-run this commit with {BYPASS_ENV}=1 in the environment."
    )
    return "\n".join(lines), None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="refuse a commit that drops a seed's SessionStart hook groups"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="repository root (default: git's toplevel of the cwd)",
    )
    return parser.parse_args()


def main() -> int:
    if os.environ.get(BYPASS_ENV) == "1":
        return 0
    repo_root = _parse_args().repo_root or Path.cwd()
    if not repo_root.is_dir():
        return 0  # unusable root — fail open rather than block every commit
    top = _git(repo_root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return 0  # not a git repo — nothing to guard
    if _unborn_head(repo_root):
        return 0  # no HEAD yet — nothing to compare against

    failures: list[str] = []
    notes: list[str] = []
    for path in SEED_PATHS:
        failure, skip = check_path(repo_root, path)
        if failure is not None:
            failures.append(failure)
        elif skip is not None:
            notes.append(f"seed_hook_subset_guard:{skip.lstrip()}")
    for note in notes:
        print(note, file=sys.stderr)
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print(
            "pre-commit: commit refused — restore the lost group(s) and re-stage, "
            "or set SEED_HOOK_ALLOW_DROP=1 if the drop is intentional.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
