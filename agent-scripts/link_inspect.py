#!/usr/bin/env python3
"""link_inspect.py — pure link inspection, path classification, and drift
finding for install.py's ``--check-links`` audit.

Extracted verbatim from install.py's links.toml-audit region so the logic
is reusable without importing the 5300-line installer. Nothing here prints,
exits, or mutates the filesystem: every function is catch-and-classify, and
findings come back as plain data (bucket → message lines, raw Paths). The
Context-coupled wrappers — display formatting, manifest access, scoping —
stay behind as adapters in install.py, which keeps the ``do_check_links``
entrypoint and the ``_cleanup_orphaned_links`` repair/deletion execution.

Interface preserved for existing callers: install.py re-exports every moved
name (``install.CHECK_BUCKETS``, ``install._implied_repo_root``, ...), so
tests and callers resolve them through ``install`` exactly as before.

std library only; Python 3.12+. See ``test_link_inspect.py`` beside this
file for the isolated unit coverage; ``test/test_install.py`` still owns
the end-to-end audit coverage through the unchanged entrypoint.
"""

import fnmatch
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# ── links.toml row schema ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LinkSpec:
    """One row of ``links.toml``: a repo file and where it gets linked."""

    src: str
    dest: str
    harness: str | None = None
    platform: str | None = None
    wsl: str | None = None
    profile_exclude: tuple[str, ...] = ()
    dir: bool = False


@dataclass(frozen=True)
class ManagedDirSpec:
    """One row of ``links.toml``: a directory dotfiles owns exclusively.

    Exclusivity is opt-in rather than inferred. Inferring it from every link's
    ``dest.parent`` surfaces 230 unmanaged entries to find 2 real ones, 68 of
    them in ``$HOME`` alone, because seven separate rows happen to land there.
    """

    dest: str
    ignore: tuple[str, ...] = ()


JUNK_SUFFIXES = ("~", ".swp", ".swo", ".tmp")


def expand_dest(dest: str, home: Path) -> Path:
    """Expand a ``links.toml`` destination against ``home``.

    Explicit rather than :func:`os.path.expanduser` so the destination
    tracks the context's home directory (which tests point at a temporary
    one) instead of the process environment.
    """
    if dest == "~":
        return home
    if dest.startswith("~/"):
        return home / dest[2:]
    return Path(dest)


# ── path classification ───────────────────────────────────────────────────────


def is_symlink(path: Path) -> bool:
    """Return whether ``path`` is a symlink, catching OSError when unreadable."""
    try:
        return path.is_symlink()
    except OSError:
        return False


def path_exists(path: Path) -> bool:
    """Return whether ``path`` exists, catching OSError when unreadable."""
    try:
        return path.exists()
    except OSError:
        return False


def link_target(dest: Path) -> Path:
    """Return what ``dest`` points at, as an absolute path.

    ``symlink`` only ever writes absolute targets, but a link placed there
    by hand may be relative — resolve those against the link's own
    directory the way the kernel does, rather than against the cwd.
    """
    target = Path(os.readlink(dest))
    return target if target.is_absolute() else dest.parent / target


def same_path(left: Path, right: Path) -> bool:
    """Compare two paths that may or may not exist, ignoring symlinked parents.

    A plain string comparison is the common case; the ``resolve`` fallback
    catches an installer run whose repo path reached the link through a
    symlink (a symlinked home, ``/tmp`` → ``/private/tmp`` on macOS), which
    would otherwise read as a wrong target.
    """
    if left == right:
        return True
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def implied_repo_root(target: Path, relative_src: str) -> Path | None:
    """Return the repo root ``target`` implies, if it ends with ``relative_src``.

    ``/home/u/dotfiles-wt/claude/global-instructions.md`` with a
    ``claude/global-instructions.md`` entry implies ``/home/u/dotfiles-wt``.
    None if the tail doesn't match, which means the link points at
    something unrelated rather than at the same file in a different
    checkout.
    """
    tail = Path(relative_src).parts
    parts = target.parts
    if len(parts) <= len(tail) or parts[-len(tail) :] != tail:
        return None
    return Path(*parts[: -len(tail)])


def is_dotfiles_checkout(root: Path) -> bool:
    """Return whether ``root`` looks like another checkout of this repo."""
    return (root / "links.toml").is_file() and (root / "install.py").is_file()


def is_main_checkout(root: Path) -> bool:
    """Return whether ``root`` is the repo's primary checkout, not a worktree.

    `git worktree add` gives a worktree a `.git` *file* holding a `gitdir:`
    pointer, while the primary checkout keeps `.git` as a directory. That
    difference is the whole test, and reading it costs one stat -- no git
    subprocess, which keeps this usable from the audit and from a test suite
    that blocks real subprocess calls.
    """
    try:
        return (root / ".git").is_dir()
    except OSError:
        return False


# ── drift finding ─────────────────────────────────────────────────────────────

CHECK_BUCKET_BROKEN_SOURCE = "broken-source"
CHECK_BUCKET_WRONG_TARGET = "wrong-target"
CHECK_BUCKET_NOT_A_SYMLINK = "not-a-symlink"
CHECK_BUCKET_ORPHANED = "orphaned"
CHECK_BUCKET_UNMANAGED = "unmanaged"
CHECK_BUCKET_NEVER_INSTALLED = "never-installed"

CHECK_BUCKETS = (
    CHECK_BUCKET_BROKEN_SOURCE,
    CHECK_BUCKET_WRONG_TARGET,
    CHECK_BUCKET_NOT_A_SYMLINK,
    CHECK_BUCKET_ORPHANED,
    CHECK_BUCKET_UNMANAGED,
    CHECK_BUCKET_NEVER_INSTALLED,
)


def check_applicable_links(
    links: Sequence[tuple[Path, Path, str, bool]],
    *,
    dotfiles: Path,
    format_path: Callable[[Path], str],
    manifest_entries: Iterable[dict[str, object]] = (),
    report_uninstalled: bool = False,
) -> tuple[dict[str, list[str]], dict[Path, int]]:
    """Report inconsistencies on destinations in scope for this machine.

    Entries whose destination does not exist at all are silently fine by
    default: that is simply a link this machine has not installed (yet), not
    a defect. That is also what makes the widened-harness default safe — an
    entry for a harness that was never provisioned has no destination to
    report on. Passing ``report_uninstalled`` additionally flags a missing
    destination whose source exists but that the manifest has never once
    recorded creating — genuinely never installed, as distinct from a link
    that was installed and later removed (rollback or manual cleanup), which
    a manifest record still explains and which stays silent either way.

    Args:
        links: Every gathered ``(src, dest, rel, applicable)`` triple.
        dotfiles: This checkout's root — the target live links are expected
            to point at.
        format_path: Renders a Path for a findings message (``Context.display``
            in install.py; pure formatting, no I/O).
        manifest_entries: The history manifest's recorded entries (only read
            when ``report_uninstalled`` is set).
        report_uninstalled: Also flag destinations the manifest never
            recorded creating; see above.

    Returns:
        The findings by bucket, and a count per *other* checkout the live
        links point into. The second value is not a finding: this repo
        mandates worktree-first development, so running the audit from a
        worktree while the machine's links point at the main checkout is
        the normal case, not a defect (see install.py's ``do_check_links``).
    """
    findings: dict[str, list[str]] = {bucket: [] for bucket in CHECK_BUCKETS}
    foreign: dict[Path, int] = {}
    installed_dests: set[Path] = set()
    if report_uninstalled:
        installed_dests = {
            Path(str(entry["dest"]))
            for entry in manifest_entries
            if entry.get("kind") == "symlink-created" and "dest" in entry
        }
    for src, dest, rel, applicable in links:
        if not applicable:
            continue
        if not is_symlink(dest) and not path_exists(dest):
            if report_uninstalled and path_exists(src) and dest not in installed_dests:
                findings[CHECK_BUCKET_NEVER_INSTALLED].append(
                    f"{format_path(dest)} — {src} exists in the repo but was "
                    "never linked here; run install.sh to link it"
                )
            continue

        if not is_symlink(dest):
            # Reached through a symlinked *parent* (a directory-level entry
            # linking the ancestor) the file is still correctly wired, even
            # though this path is not itself a link.
            if not same_path(dest, src):
                findings[CHECK_BUCKET_NOT_A_SYMLINK].append(
                    f"{format_path(dest)} — a real "
                    f"{'directory' if dest.is_dir() else 'file'} sits where a "
                    f"symlink to {src} belongs; the next install run would "
                    "back it up and replace it"
                )
            continue

        target = link_target(dest)
        if not same_path(target, src):
            other_root = implied_repo_root(target, rel)
            # Only a link into the PRIMARY checkout is excusable. The
            # direction matters and used to be ignored: auditing from a
            # worktree while the machine points at main is the normal state
            # under worktree-first development, but a link pointing INTO a
            # worktree is drift -- someone hand-pointed it for a live test and
            # left it there, and it dangles the moment that worktree is
            # removed, silently unloading whatever it provided. Both cases
            # answered "a different checkout?" the same way, so the second was
            # filed as a benign note and dropped from the audited count. It
            # has bitten three times: custom-footer.ts, then permission-gate.ts
            # and swarm-tool.ts on 2026-09-02.
            same_file_other_checkout = (
                other_root is not None
                and not same_path(other_root, dotfiles)
                and is_dotfiles_checkout(other_root)
                and is_main_checkout(other_root)
            )
            if same_file_other_checkout:
                # A dangling link is a real machine problem regardless of
                # which checkout it points into, so that still gets reported.
                if not path_exists(target):
                    findings[CHECK_BUCKET_BROKEN_SOURCE].append(
                        f"{format_path(dest)} — links to {target}, which no "
                        "longer exists (dangling symlink)"
                    )
                else:
                    assert other_root is not None  # narrowed by the guard above
                    foreign[other_root] = foreign.get(other_root, 0) + 1
                continue
            findings[CHECK_BUCKET_WRONG_TARGET].append(
                f"{format_path(dest)} — points at {target}, but links.toml says {src}"
            )
            continue

        if not path_exists(src):
            findings[CHECK_BUCKET_BROKEN_SOURCE].append(
                f"{format_path(dest)} — links to {src}, which no longer "
                "exists in the repo (dangling symlink)"
            )
    return findings, foreign


def find_orphaned_links(
    links: Sequence[tuple[Path, Path, str, bool]],
    *,
    manifest_entries: Iterable[dict[str, object]],
) -> list[Path]:
    """Return manifest-recorded symlink destinations no current entry produces.

    Compared against *every* triple's destination rather than only the
    applicable ones: a triple that is merely gated off on this machine (a
    mac-only link seen from Linux, a harness not selected this run) has not
    been removed from links.toml, so its recorded destination is not an
    orphan — reporting it as one would be a false positive on every
    cross-platform machine, or on a run scoped to a different harness.

    A destination whose *live* target no longer matches what this repo's
    manifest recorded creating is not orphaned — it's claimed. Some other
    tool (most commonly another repo's own installer, sharing this same
    destination) has already repointed it, and unlinking it here would
    delete that tool's live symlink, not ours. 2026-09-07: dotfiles'
    orphan-cleanup deleted three ~/.claude/scripts/*.py symlinks
    agent-toolkit's installer had just created moments earlier in the same
    install-with-agent-toolkit.sh run, because this check didn't exist —
    _rollback_symlink already guards the equivalent case before removing
    anything; this mirrors that same guard here.
    """
    known = {dest for _src, dest, _rel, _applicable in links}
    orphans: list[Path] = []
    seen: set[Path] = set()
    for entry in manifest_entries:
        if entry.get("kind") != "symlink-created":
            continue
        dest = Path(str(entry.get("dest", "")))
        if dest in known or dest in seen:
            continue
        seen.add(dest)
        # A dest that no longer exists needs no report: a past --rollback,
        # or the user, already cleaned it up.
        if not is_symlink(dest) and not path_exists(dest):
            continue
        recorded_src = str(entry.get("src", ""))
        if is_symlink(dest) and recorded_src and os.readlink(dest) != recorded_src:
            continue
        orphans.append(dest)
    return orphans


def check_orphaned_links(
    links: Sequence[tuple[Path, Path, str, bool]],
    findings: dict[str, list[str]],
    *,
    format_path: Callable[[Path], str],
    manifest_entries: Iterable[dict[str, object]],
) -> None:
    """Add manifest-recorded symlinks that links.toml no longer produces."""
    for dest in find_orphaned_links(links, manifest_entries=manifest_entries):
        if is_symlink(dest):
            detail = f"still symlinked → {link_target(dest)}"
        else:
            detail = "still present as a real file"
        findings[CHECK_BUCKET_ORPHANED].append(
            f"{format_path(dest)} — recorded by a past install run, but no "
            f"links.toml entry produces it anymore; {detail}"
        )


def live_backup_paths(manifest_entries: Iterable[dict[str, object]]) -> set[Path]:
    """Return manifest-recorded backups that are still live ``--rollback`` payload.

    Liveness means "the destination is still present at all", not "it
    resolves": ``shutil.move(backup, dest)`` replaces a dangling symlink just
    as readily as a healthy one, so a broken link does not make its backup
    disposable. Reporting one would tell the user to delete the only copy of
    their pre-dotfiles original, which is the opposite of what the backup is
    for.
    """
    live: set[Path] = set()
    for entry in manifest_entries:
        if entry.get("kind") != "file-backed-up":
            continue
        dest = Path(str(entry.get("dest", "")))
        backup = Path(str(entry.get("backup", "")))
        if not dest.parts or not backup.parts:
            continue
        if path_exists(backup) and (path_exists(dest) or is_symlink(dest)):
            live.add(backup)
    return live


def check_unmanaged_files(
    managed_dirs: Sequence[ManagedDirSpec],
    links: Sequence[tuple[Path, Path, str, bool]],
    *,
    home: Path,
    format_path: Callable[[Path], str],
    dir_applies: Callable[[ManagedDirSpec], bool],
    findings: dict[str, list[str]],
    manifest_entries: Iterable[dict[str, object]] = (),
) -> int:
    """Report foreign entries in directories ``links.toml`` owns exclusively.

    Nothing else catches these. ``--rollback`` only inspects what the history
    recorded, ``--depart`` compares against an install-time baseline, and the
    repo-to-links.toml parity tests check both directions of the mapping yet
    cannot see a file that exists only on the installed side.

    Args:
        managed_dirs: Parsed ``[[managed_dir]]`` rows.
        links: Every gathered triple, applicable or not — a gated row's
            destination is still ours, so it must never read as foreign.
        home: The home directory destinations expand against.
        format_path: Renders a Path for a findings message (pure formatting).
        dir_applies: Scope predicate for a declared directory (install.py's
            ``_dir_applies``, which needs the run's Context to inherit the
            gating of the ``[[link]]`` rows inside each directory).
        findings: Bucket map to append into.
        manifest_entries: The history manifest's recorded entries (live
            backups are exempt from the foreign-file report).

    Returns:
        How many declared directories were actually audited.
    """
    live_backups = live_backup_paths(manifest_entries)
    audited = 0
    for dir_spec in managed_dirs:
        directory = expand_dest(dir_spec.dest, home)
        if not directory.is_dir():
            continue
        if not dir_applies(dir_spec):
            continue
        audited += 1
        managed = {
            dest
            for _src, dest, _rel, _applicable in links
            if dest.is_relative_to(directory)
        }
        try:
            entries = sorted(os.listdir(directory))
        except OSError as exc:
            findings[CHECK_BUCKET_UNMANAGED].append(
                f"{format_path(directory)} — declared exclusive, but unreadable "
                f"({exc.strerror or exc}), so it could not be audited"
            )
            continue
        for name in entries:
            path = directory / name
            if path in managed or path in live_backups:
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in dir_spec.ignore):
                continue
            # Hidden files are skipped: macOS drops .DS_Store into any directory
            # the user merely opens in Finder.
            if name.startswith(".") or name.endswith(JUNK_SUFFIXES):
                continue
            if path.is_dir() and not is_symlink(path):
                continue
            findings[CHECK_BUCKET_UNMANAGED].append(
                f"{format_path(path)} — {dir_spec.dest} is declared exclusive "
                "to dotfiles, but no links.toml entry produces it"
            )
    return audited
