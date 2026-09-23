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

The completion-notice predicate was retired when review/approve/done grew
the pre-mutation committed-work guard (:func:`inspect_item_worktrees`):
the inspector inherits the marker-OR-branch attribution for attributed
targets and answers dirty/merged/problem facts, read-only, before any
lifecycle mutation runs.

Nothing here mutates the backlog store. Git calls are bounded — a guard that
hangs is a guard that silently permits.

Usage (library):
    from worktree_provenance import (
        classify, marker_path, read_marker, read_marker_for_worktree,
        worktree_points_at_item, write_marker,
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

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

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
    except (OSError, subprocess.SubprocessError, ValueError):
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


# ── the committed-work inspector behind review/approve/done ──────────────

_DIRTY_SAMPLE_TOKENS: Final = 5
_DIRTY_SAMPLE_MAX: Final = 300


@dataclass(frozen=True)
class WorktreeInspection:
    """Facts about one target attributable to a backlog item.

    ``target`` is a worktree checkout root, a branch ref name, or the
    related_files path that failed discovery; ``source`` says which
    (``worktree``, ``branch``, ``path``). ``dirty`` and ``dirty_sample``
    come from ``git status --porcelain=v1 -z --untracked-files=all
    --ignore-submodules=none`` — any non-empty output is dirty, and the
    sample is bounded raw NUL-terminated tokens for diagnostics.
    ``head_ancestor_of_default`` is the merge-base verdict against the
    resolved local default branch (``default_ref``); ``None`` when not
    determined. ``problem`` is non-None exactly when inspection failed —
    callers must refuse on it, never on absence."""

    target: str
    source: str  # "worktree" | "branch" | "path"
    dirty: bool = False
    dirty_sample: str = ""
    head_ancestor_of_default: bool | None = None
    default_ref: str = ""
    problem: str | None = None


def _git_raw(*args: str) -> tuple[int, str, str] | None:
    """(returncode, stdout, stderr), or None on spawn failure/timeout."""
    cmd = ["git", *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return result.returncode, result.stdout, result.stderr


def _one_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


def _dirty_sample(tokens: list[str]) -> str:
    shown = tokens[:_DIRTY_SAMPLE_TOKENS]
    sample = " | ".join(repr(t) if any(c.isspace() for c in t) else t for t in shown)
    if len(tokens) > _DIRTY_SAMPLE_TOKENS:
        sample += " …"
    return sample[:_DIRTY_SAMPLE_MAX]


def _related_file_paths(related_files: object) -> list[str]:
    """Deduplicated path strings from an item's related_files, accepting
    both stored-shape dicts and bare strings."""
    seen: dict[str, None] = {}
    if isinstance(related_files, (list, tuple)):
        for entry in related_files:
            path = entry.get("path") if isinstance(entry, dict) else entry
            if isinstance(path, str) and path.strip():
                seen.setdefault(path.strip(), None)
    return list(seen)


def _discover_repos(
    related_files: object,
) -> tuple[dict[Path, Path], list[WorktreeInspection]]:
    """Canonical common-dir → an existing representative directory, plus
    discovery problems. Relative entries and unclassifiable existing paths
    refuse (fail-closed); genuinely absent paths and paths git proves are
    outside any repository are simply not attributable."""
    repos: dict[Path, Path] = {}
    problems: list[WorktreeInspection] = []
    for raw in _related_file_paths(related_files):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            problems.append(
                WorktreeInspection(
                    target=raw,
                    source="path",
                    problem=(
                        "relative related_files path cannot be anchored — "
                        "store an absolute path"
                    ),
                )
            )
            continue
        probe = candidate if candidate.is_dir() else candidate.parent
        anchor: Path | None = None
        for p in (probe, *probe.parents):
            if p.exists():
                anchor = p
                break
        if anchor is None:
            continue  # genuinely absent: no attributable work here
        raw = _git_raw(
            "-C",
            str(anchor),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        if raw is None:
            problems.append(
                WorktreeInspection(
                    target=raw,
                    source="path",
                    problem=(f"git rev-parse timed out or could not run for {raw}"),
                )
            )
            continue
        rc, stdout, stderr = raw
        if rc == 0 and stdout.strip():
            repos.setdefault(Path(stdout.strip()).resolve(), anchor)
            continue
        if "not a git repository" in stderr:
            continue  # proven non-repo — not an error
        problems.append(
            WorktreeInspection(
                target=raw,
                source="path",
                problem=(
                    f"git could not classify {raw} (exit {rc}: {_one_line(stderr)})"
                ),
            )
        )
    return repos, problems


def _worktree_records(
    anchor: Path,
) -> tuple[list[tuple[Path, str]], str | None]:
    """(checkout path, branch or "") for every listed worktree, from
    NUL-delimited ``git worktree list --porcelain -z``; or ([]) and a
    problem reason. A detached worktree's record has no branch line."""
    raw = _git_raw("-C", str(anchor), "worktree", "list", "--porcelain", "-z")
    if raw is None:
        return [], "git worktree list timed out or could not run"
    rc, stdout, _stderr = raw
    if rc != 0:
        return [], f"git worktree list failed (exit {rc})"
    records: list[tuple[Path, str]] = []
    path: Path | None = None
    branch = ""
    for tok in stdout.split("\0"):
        if not tok.strip():
            # an empty attribute terminates the current record (and a
            # trailing one ends the output)
            if path is not None:
                records.append((path, branch))
            path, branch = None, ""
            continue
        if tok.startswith("worktree "):
            if path is not None:
                return [], "malformed git worktree list output"
            path = Path(tok[len("worktree ") :])
        elif tok.startswith("branch "):
            ref = tok[len("branch ") :].strip()
            if ref.startswith("refs/heads/"):
                branch = ref.removeprefix("refs/heads/")
        elif not tok.startswith(("HEAD ", "detached", "bare", "locked ")):
            return [], "malformed git worktree list output"
    if path is not None:
        records.append((path, branch))
    return records, None


def _local_default_ref(anchor: Path) -> tuple[str | None, str | None]:
    """(local default branch ref, problem). origin/HEAD's branch name
    resolves to the LOCAL refs/heads branch first — a stale remote tracking
    commit is never the merge target — then local main, then master. Exit 1
    from --quiet symbolic-ref means unset; any other failure is an error."""
    sym = _git_raw(
        "-C", str(anchor), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"
    )
    if sym is None:
        return None, "git symbolic-ref timed out or could not run"
    rc, stdout, _stderr = sym
    if rc == 0:
        name = stdout.strip()
        if name.startswith("refs/remotes/origin/"):
            local = f"refs/heads/{name[len('refs/remotes/origin/') :]}"
            ex = _git_raw("-C", str(anchor), "show-ref", "--verify", "--quiet", local)
            if ex is None:
                return None, "git show-ref timed out or could not run"
            if ex[0] == 0:
                return local, None
    elif rc != 1:
        return None, f"git symbolic-ref failed (exit {rc})"
    for candidate in ("main", "master"):
        ref = f"refs/heads/{candidate}"
        ex = _git_raw("-C", str(anchor), "show-ref", "--verify", "--quiet", ref)
        if ex is None:
            return None, "git show-ref timed out or could not run"
        if ex[0] == 0:
            return ref, None
        if ex[0] != 1:
            return None, f"git show-ref failed (exit {ex[0]})"
    return None, None


def _status_and_ancestry(
    *,
    target: str,
    source: str,
    git_dir_arg: str,
    head_arg: str,
    anchor: Path,
    check_dirty: bool = True,
) -> WorktreeInspection:
    """Dirtiness (optional — a surviving branch's own checkout is not the
    branch's state) and merge ancestry of one attributed target, with every
    inspection failure surfaced as ``problem`` — never as absence."""
    tokens: list[str] = []
    if check_dirty:
        st = _git_raw(
            "-C",
            git_dir_arg,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
        if st is None:
            return WorktreeInspection(
                target=target,
                source=source,
                problem="git status timed out or could not run",
            )
        rc, stdout, stderr = st
        if rc != 0:
            return WorktreeInspection(
                target=target,
                source=source,
                problem=f"git status failed (exit {rc}: {_one_line(stderr)})",
            )
        tokens = [t for t in stdout.split("\0") if t]
    default, problem = _local_default_ref(anchor)
    if problem is not None:
        return WorktreeInspection(target=target, source=source, problem=problem)
    if default is None:
        return WorktreeInspection(
            target=target,
            source=source,
            problem="no local default branch (main/master) to merge into",
        )
    mb = _git_raw("-C", git_dir_arg, "merge-base", "--is-ancestor", head_arg, default)
    if mb is None:
        return WorktreeInspection(
            target=target,
            source=source,
            problem="git merge-base timed out or could not run",
        )
    rc, _stdout, stderr = mb
    if rc == 0:
        ancestor: bool | None = True
    elif rc == 1:
        ancestor = False
    else:
        return WorktreeInspection(
            target=target,
            source=source,
            problem=f"git merge-base failed (exit {rc}: {_one_line(stderr)})",
        )
    return WorktreeInspection(
        target=target,
        source=source,
        dirty=bool(tokens),
        dirty_sample=_dirty_sample(tokens),
        head_ancestor_of_default=ancestor,
        default_ref=default,
    )


def _invalid_marker(wt: Path, prov: WorktreeProvenance) -> bool:
    """A marker file that exists but is not exactly one line — its slug is
    unknowable, so attribution cannot be decided."""
    return (
        prov.git_dir is not None
        and marker_path(prov.git_dir).exists()
        and read_marker(prov.git_dir) is None
    )


@dataclass(frozen=True)
class _OneWorktree:
    """_inspect_one_worktree's result: the inspection (None when the
    worktree is not attributable to the slug) plus whether a readable
    foreign marker decided attribution in this worktree — the caller uses
    that to skip the branch fallback, which would otherwise re-attribute
    what the marker excluded."""

    inspection: WorktreeInspection | None = None
    foreign_marker: bool = False


def _inspect_one_worktree(
    wt: Path, porcelain_branch: str, slug: str, *, allow_main: bool = False
) -> _OneWorktree:
    """One listed worktree's inspection, or (None) when not attributable to
    slug. Marker-wins precedence: a readable marker naming another item
    excludes the worktree even when the branch equals the slug (and the
    caller then skips that repo's branch fallback); a marker that exists
    but cannot be read refuses when the branch heuristic would otherwise
    attribute this worktree, and stays silent otherwise — a corrupt marker
    on someone else's worktree is not this item's evidence."""
    if not wt.exists():
        return _OneWorktree(
            WorktreeInspection(
                target=str(wt),
                source="worktree",
                problem=(
                    "attributed worktree is registered but missing on disk — "
                    "git worktree prune or restore it"
                ),
            )
        )
    prov = classify(wt)
    if prov is None:
        return _OneWorktree(
            WorktreeInspection(
                target=str(wt),
                source="worktree",
                problem="git could not classify a registered worktree",
            )
        )
    marker = read_marker(prov.git_dir)
    corrupt = _invalid_marker(wt, prov)
    if marker is not None and marker != slug:
        return _OneWorktree(None, foreign_marker=True)  # another item's marker wins
    branch_attributed = (
        (prov.is_linked_worktree or allow_main)
        and bool(porcelain_branch)
        and porcelain_branch == slug
    )
    if corrupt and not branch_attributed:
        return _OneWorktree()
    if corrupt:
        return _OneWorktree(
            WorktreeInspection(
                target=str(wt),
                source="worktree",
                problem=(
                    "invalid provenance marker (devstatus_item must be exactly "
                    "one line) — repair or delete it"
                ),
            )
        )
    if not (marker == slug or branch_attributed):
        return _OneWorktree()
    if not porcelain_branch:
        return _OneWorktree(
            WorktreeInspection(
                target=str(wt),
                source="worktree",
                problem=(
                    "worktree HEAD is detached — check the item branch back out "
                    "before completing"
                ),
            )
        )
    return _OneWorktree(
        _status_and_ancestry(
            target=str(wt),
            source="worktree",
            git_dir_arg=str(wt),
            head_arg="HEAD",
            anchor=wt,
        )
    )


def _inspect_branch_fallback(anchor: Path, slug: str) -> WorktreeInspection | None:
    """A surviving slug-named branch (worktree removed) inspected for merge
    ancestry. Absence (exit 1) is no attributable work; any other failure
    is an error, never "branch absent"."""
    ref = f"refs/heads/{slug}"
    ex = _git_raw("-C", str(anchor), "show-ref", "--verify", "--quiet", ref)
    if ex is None:
        return WorktreeInspection(
            target=ref,
            source="branch",
            problem="git show-ref timed out or could not run",
        )
    if ex[0] == 1:
        return None
    if ex[0] != 0:
        return WorktreeInspection(
            target=ref,
            source="branch",
            problem=f"git show-ref failed (exit {ex[0]})",
        )
    result = _status_and_ancestry(
        target=ref,
        source="branch",
        git_dir_arg=str(anchor),
        head_arg=ref,
        anchor=anchor,
        check_dirty=False,
    )
    # A merged survivor is the normal cleaned-up state — the fallback's
    # only job is to refuse an unmerged one, so a pass emits no entry.
    if result.problem is None and result.head_ancestor_of_default is True:
        return None
    return result


def inspect_item_worktrees(
    *,
    related_files: object,
    slug: str,
    cwd: str | Path | None = None,
) -> list[WorktreeInspection]:
    """Read-only committed-work facts for every target attributable to
    ``slug``: linked worktrees marked for the item (marker wins over the
    branch heuristic), the caller's current checkout when its branch
    equals the slug, and — only when no attributed worktree exists — each
    discovered repository's surviving slug branch. No mutating git calls,
    no network, no backlog-store access. Submodule paths discovered
    through related_files are skipped entirely: a submodule's pinned
    detached HEAD is its normal state, and its main checkout is never a
    linked worktree. Uncommitted work sitting directly in a main checkout
    is deliberately invisible here (guard-rails R2 owns that path)."""
    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    out: list[WorktreeInspection] = []
    repos, problems = _discover_repos(related_files)
    out.extend(problems)
    inspected: set[Path] = set()
    for _common, anchor in sorted(repos.items()):
        records, err = _worktree_records(anchor)
        if err is not None:
            out.append(
                WorktreeInspection(target=str(anchor), source="path", problem=err)
            )
            continue
        repo_attributed = False
        foreign_marker = False
        for wt, branch in records:
            res = _inspect_one_worktree(wt, branch, slug)
            foreign_marker = foreign_marker or res.foreign_marker
            if res.inspection is None:
                continue
            insp = res.inspection
            out.append(insp)
            if insp.source == "worktree":
                inspected.add(Path(insp.target).resolve())
                if insp.problem is None:
                    repo_attributed = True
        # A surviving slug branch is inspected only when this repo has no
        # attributed worktree — and never when a foreign marker decided a
        # worktree here, or the marker-wins rule would be meaningless.
        if not repo_attributed and not foreign_marker:
            fallback = _inspect_branch_fallback(anchor, slug)
            if fallback is not None:
                out.append(fallback)
    # The caller's own checkout: the one directory a session stands in, on
    # the same marker-or-branch heuristic. Its repository is otherwise not
    # seeded into discovery.
    if cwd_path.is_dir():
        prov = classify(cwd_path)
        if prov is not None and prov.toplevel is not None:
            toplevel = prov.toplevel.resolve()
            if toplevel not in inspected:
                res = _inspect_one_worktree(
                    prov.toplevel, prov.branch, slug, allow_main=True
                )
                insp = res.inspection
                if insp is not None:
                    out.append(insp)
    return out
