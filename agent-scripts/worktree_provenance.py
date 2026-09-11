#!/usr/bin/env python3
"""Per-worktree backlog provenance: one explicit marker, shared predicates.

The problem this solves: attributing a write to the backlog item a worktree
belongs to used to rest on two heuristics — an exact ``related_files`` path
match, or the worktree's branch name being the item slug. A branch renamed
away from the slug broke attribution, and attributing by *repository* instead
would misfire whenever two items share a repo (the claim check denies unless
a claim is held on every pointed-at item).

The fix is a provenance marker: ``worktree.py`` stamps
``$(git rev-parse --git-dir)/devstatus_item`` with the item slug when it
creates or reuses a worktree for a backlog item. In a linked worktree
``--git-dir`` resolves to ``<repo>/.git/worktrees/<id>/``, so the marker is
per-worktree, lives outside the checkout (never committed, no gitignore
churn), survives branch renames, and attributes a worktree to exactly one
item. Verified on git 2.43: the linked worktree sees its marker, the main
checkout cannot. No ``extensions.worktreeConfig`` needed.

Attribution rules, as one pure predicate both ``guard_rails.py`` and
``dev_status_mutation.py`` call (duplicated detection is what drifted
before):

- A **live** marker (its slug is in the in-progress set) decides the
  worktree alone; branch matching does not also attribute, so concurrent
  same-repo items can never deny each other's claimed worktrees.
- An **unmarked or stale** (slug no longer in progress) worktree falls back
  to ``branch == slug``, matching the pre-marker behavior.
- Main checkouts never attribute via either rule — R2 owns those.

The completion-notice predicate is the simpler ``marker or branch`` form: it
runs after the item is already saved done, so the slug is by definition not
in any in-progress set, and its output is advisory (a journal note), not a
denial.

Nothing here mutates the backlog store. Git calls are bounded — a guard that
hangs is a guard that silently permits.

Usage (library):
    from worktree_provenance import (
        classify, marker_path, read_marker, read_marker_for_worktree,
        worktree_belongs_to_slug, worktree_points_at_item, write_marker,
    )

Environment
  None; this module reads no configuration. It honors nothing equivalent to
  GUARD_RAILS_OFF — callers own their own disable switches.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MARKER_FILENAME: Final = "devstatus_item"
GIT_TIMEOUT: Final = 2.0


@dataclass(frozen=True)
class WorktreeProvenance:
    """Git facts that answer "which item owns this directory's worktree?".

    ``toplevel`` is the worktree checkout root (needed by callers that run
    further ``git -C`` commands, e.g. the completion notice's merge-base
    check); ``git_dir`` is the absolute per-worktree metadata directory the
    marker lives in; ``is_linked_worktree`` distinguishes it from a main
    checkout (``git_dir != common_dir``).
    """

    git_dir: Path | None
    toplevel: Path | None
    branch: str
    marker_slug: str | None
    is_linked_worktree: bool


def _git(*args: str, cwd: Path | str | None = None) -> list[str] | None:
    """Run git, returning stdout lines, or None on any failure/timeout."""
    cmd = ["git", *(["-C", str(cwd)] if cwd is not None else []), *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _absolutize(value: str, base: Path | str) -> Path:
    """git -C <dir> prints relative paths against <dir>, not this process's
    cwd — resolve explicitly or an unrelated caller CWD silently breaks it.
    Mirrors guard_rails._absolutize; the git >= 2.5 floor cannot rely on
    --path-format=absolute."""
    path = Path(value)
    if not path.is_absolute():
        path = Path(base) / path
    return Path(path.resolve())


def marker_path(git_dir: Path) -> Path:
    """Where a worktree's provenance marker lives, given its git dir."""
    return git_dir / MARKER_FILENAME


def read_marker(git_dir: Path | None) -> str | None:
    """The slug in a git dir's marker, or None when absent/unreadable.

    A marker must be exactly one non-empty line; anything else is treated
    as no marker rather than trusted."""
    if git_dir is None:
        return None
    try:
        text = marker_path(git_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text or "\n" in text or "\r" in text:
        return None
    return text


def read_marker_for_worktree(worktree_dir: Path | str) -> str | None:
    """Resolve the worktree's own git dir, then read its marker."""
    lines = _git("rev-parse", "--git-dir", cwd=worktree_dir)
    if not lines:
        return None
    return read_marker(_absolutize(lines[0], worktree_dir))


def _resolve_roots(
    directory: Path | str,
) -> tuple[Path, Path] | None:
    """(git_dir, common_dir) for a directory, or None when git cannot
    answer. Both outputs are absolutized against the -C directory, never the
    process cwd."""
    lines = _git("rev-parse", "--git-dir", "--git-common-dir", cwd=directory)
    if not lines or len(lines) < 2:
        return None
    git_dir = _absolutize(lines[0], directory)
    common_dir = _absolutize(lines[1], directory)
    return git_dir, common_dir


def write_marker(worktree_dir: Path | str, slug: str) -> bool:
    """Stamp the provenance marker in a *linked* worktree.

    Returns True on success, False when the directory is not a linked
    worktree — a main checkout's ``--git-dir`` is ``<repo>/.git`` and a
    marker there would attribute the whole repository to one item, which is
    precisely the over-attribution the design rejects. Raises OSError only
    if the per-worktree metadata directory itself is unwritable.
    """
    roots = _resolve_roots(worktree_dir)
    if roots is None:
        return False
    git_dir, common_dir = roots
    if git_dir == common_dir:
        return False
    marker_path(git_dir).write_text(f"{slug}\n", encoding="utf-8")
    return True


def classify(directory: str | Path) -> WorktreeProvenance | None:
    """Full provenance snapshot for one directory, or None outside any repo.

    ``branch`` is ``git branch --show-current`` (empty on detached HEAD);
    marker attribution applies to linked worktrees only, decided by
    :func:`worktree_points_at_item` from the stored ``is_linked_worktree``.
    """
    path = Path(directory)
    roots = _resolve_roots(path)
    if roots is None:
        return None
    git_dir, common_dir = roots
    is_linked = git_dir != common_dir
    lines = _git("rev-parse", "--show-toplevel", cwd=path)
    toplevel = _absolutize(lines[0], path) if lines else None
    branch_lines = _git("branch", "--show-current", cwd=path)
    branch = branch_lines[0] if branch_lines else ""
    return WorktreeProvenance(
        git_dir=git_dir,
        toplevel=toplevel,
        branch=branch,
        marker_slug=read_marker(git_dir) if is_linked else None,
        is_linked_worktree=is_linked,
    )


def worktree_points_at_item(
    *,
    marker_slug: str | None,
    is_linked_worktree: bool,
    branch: str,
    item_id: str,
    in_progress_ids: set[str],
) -> bool:
    """Whether a write in this worktree points at backlog ``item_id``.

    Pure over primitives so every consumer (guard_rails from its RepoInfo,
    dev_status from classify's WorktreeProvenance) shares one precedence:
    a live marker decides the worktree alone; unmarked/stale falls back to
    branch==slug; main checkouts never attribute.
    """
    if not is_linked_worktree:
        return False
    if marker_slug is not None and marker_slug in in_progress_ids:
        return marker_slug == item_id
    return bool(branch) and branch == item_id


def worktree_belongs_to_slug(
    *,
    marker_slug: str | None,
    is_linked_worktree: bool,
    branch: str,
    slug: str,
) -> bool:
    """Advisory per-item attribution for the completion notice.

    Deliberately simpler than :func:`worktree_points_at_item`: the notice
    runs after the item left in-progress, so marker liveness can't be
    consulted, and its output is a journal note, never a denial. Main
    checkouts still never attribute."""
    if not is_linked_worktree:
        return False
    return marker_slug == slug or (bool(branch) and branch == slug)
