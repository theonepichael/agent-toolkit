#!/usr/bin/env python3
"""link_inspect.py — link inspection, path classification, drift finding,
and the self-contained audit assembly for install.py's ``--check-links``
audit and link_drift_check.py's SessionStart hook.

The drift-*finding* logic was extracted verbatim from install.py's
links.toml-audit region so it is reusable without importing the 5300-line
installer. Nothing here prints or exits; every function is
catch-and-classify, and findings come back as plain data (bucket → message
lines, raw Paths). The pure TOML parsing (``load_links``/
``load_managed_dirs``), triple expansion, applicability gating, and the
manifest reader joined them so links.toml has exactly one parser: the
Context-coupled adapters — display formatting delegation, manifest
construction — stay behind as wrappers in install.py, which keeps the
``do_check_links`` entrypoint and the ``_cleanup_orphaned_links``
repair/deletion execution.

``audit_links`` is the one consolidated assembly entrypoint: it reads
links.toml and the history manifest and stats live destinations, so it does
real filesystem I/O — what it never does is print, exit, or mutate. Callers
supply the machine facts (platform booleans, harnesses, profile) and get
findings back as plain data.

Interface preserved for existing callers: install.py re-exports every moved
name (``install.CHECK_BUCKETS``, ``install._implied_repo_root``, ...), so
tests and callers resolve them through ``install`` exactly as before.

std library only; Python 3.12+. See ``test_link_inspect.py`` beside this
file for the isolated unit coverage; ``test/test_install.py`` still owns
the end-to-end audit coverage through the unchanged entrypoint.
"""

import fnmatch
import json
import os
import tomllib
from collections.abc import Callable, Iterable, Iterator, Sequence
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


# ── links.toml scope constants ───────────────────────────────────────────

VALID_HARNESSES = ("claude", "copilot", "opencode", "agy", "pi", "codex")
VALID_PROFILES = ("personal", "work")
DEFAULT_PROFILE = "personal"

_LINK_FIELDS = {
    "src",
    "dest",
    "harness",
    "platform",
    "wsl",
    "profile_exclude",
    "dir",
}

_MANAGED_DIR_FIELDS = {"dest", "ignore"}


# ── links.toml parsing ──────────────────────────────────────────────────


def load_links(path: Path) -> list[LinkSpec]:
    """Parse ``links.toml`` into an ordered list of link specs.

    Unknown keys and bad values are rejected loudly rather than ignored — a
    typo'd gate (``harnes = "claude"``) would otherwise silently widen a
    link to every run.

    Args:
        path: Path to the TOML table.

    Returns:
        The specs, in file order.

    Raises:
        ValueError: If the file is malformed or an entry is invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc

    rows = data.get("link", [])
    if not isinstance(rows, list):
        raise TypeError(f"{path}: expected a [[link]] array")

    specs: list[LinkSpec] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TypeError(f"{path}: entry {index} is not a table")
        unknown = sorted(set(row) - _LINK_FIELDS)
        if unknown:
            raise ValueError(f"{path}: entry {index} has unknown key(s): {unknown}")
        for required in ("src", "dest"):
            if not isinstance(row.get(required), str) or not row[required]:
                raise ValueError(f"{path}: entry {index} is missing '{required}'")
        harness = row.get("harness")
        if harness is not None and harness not in VALID_HARNESSES:
            raise ValueError(f"{path}: entry {index} has unknown harness {harness!r}")
        os_gate = row.get("platform")
        if os_gate is not None and os_gate not in ("mac", "linux"):
            raise ValueError(f"{path}: entry {index} has unknown platform {os_gate!r}")
        wsl = row.get("wsl")
        if wsl is not None and wsl not in ("only", "exclude"):
            raise ValueError(f"{path}: entry {index} has unknown wsl value {wsl!r}")
        excluded = row.get("profile_exclude", [])
        if not isinstance(excluded, list) or any(
            profile not in VALID_PROFILES for profile in excluded
        ):
            raise ValueError(f"{path}: entry {index} has invalid profile_exclude")
        dir_flag = row.get("dir", False)
        if not isinstance(dir_flag, bool):
            raise TypeError(f"{path}: entry {index} has non-bool 'dir'")
        specs.append(
            LinkSpec(
                src=row["src"],
                dest=row["dest"],
                harness=harness,
                platform=os_gate,
                wsl=wsl,
                profile_exclude=tuple(excluded),
                dir=dir_flag,
            )
        )
    return specs


def load_managed_dirs(path: Path) -> list[ManagedDirSpec]:
    """Parse the ``[[managed_dir]]`` rows declaring directories we own exclusively.

    Mirrors :func:`load_links` in refusing to guess — unknown keys and bad
    values are rejected loudly, because a typo'd row would otherwise silently
    widen or narrow the audit. An absent table is not an error: every
    ``links.toml`` predating this mechanism has none, and the audit then finds
    nothing declared.

    Args:
        path: Path to the TOML table.

    Returns:
        The specs, in file order.

    Raises:
        ValueError: If the file is malformed or an entry is invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc

    rows = data.get("managed_dir", [])
    if not isinstance(rows, list):
        raise TypeError(f"{path}: expected a [[managed_dir]] array")

    specs: list[ManagedDirSpec] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TypeError(f"{path}: managed_dir entry {index} is not a table")
        unknown = sorted(set(row) - _MANAGED_DIR_FIELDS)
        if unknown:
            raise ValueError(
                f"{path}: managed_dir entry {index} has unknown key(s): {unknown}"
            )
        dest = row.get("dest")
        if not isinstance(dest, str) or not dest:
            raise ValueError(f"{path}: managed_dir entry {index} is missing 'dest'")
        raw_ignore = row.get("ignore", [])
        if not isinstance(raw_ignore, list) or any(
            not isinstance(item, str) or not item for item in raw_ignore
        ):
            raise ValueError(f"{path}: managed_dir entry {index} has invalid 'ignore'")
        specs.append(ManagedDirSpec(dest=dest, ignore=tuple(raw_ignore)))
    return specs


# ── machine scoping and state resolution ─────────────────────────────────


def detect_wsl(system: str) -> bool:
    """Return whether this is a WSL kernel (as opposed to native Linux)."""
    if system != "Linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def manifest_path(home: Path) -> Path:
    """Return the history-manifest path installs write under ``home``.

    The directory name ("agent-toolkit", not "dotfiles") is load-bearing:
    both repos' install.py write a symlink-creation manifest under a shared
    state directory, and orphan-cleanup deletes any manifest-recorded symlink
    the reading repo's own links.toml doesn't produce — a shared name means
    each repo's install treats the OTHER repo's still-wanted symlinks as its
    own stale orphans and deletes them. Reproduced directly: a fresh machine
    with dotfiles installed, then agent-toolkit installed on top, lost
    dotfiles' personal-only scripts with no warning under --quiet. Giving
    agent-toolkit its own state directory makes this permanently impossible,
    not just during a one-time cutover. install.py (the writer) and
    link_drift_check.py (the auditor) both resolve the path through this
    function, so the writer and the reader can never disagree.
    """
    return home / ".local" / "state" / "agent-toolkit" / "history.jsonl"


def read_manifest_entries(path: Path) -> list[dict[str, object]]:
    """Read every recorded entry from a history manifest, oldest first.

    Unparseable lines are dropped rather than raising: a truncated last line
    (power loss mid-append) must not make the whole history unrollbackable. A
    missing file (no run has ever recorded anything yet) returns an empty list
    rather than raising ``FileNotFoundError`` — callers on the normal (non-
    rollback) install path hit this on a fresh machine and have no external
    existence guard of their own.
    """
    if not path.is_file():
        return []
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def format_path(path: Path, home: Path) -> str:
    """Render ``path`` with the home directory shortened back to ``~``.

    install.py's ``Context.display`` delegates here, so the installer's
    report and any other consumer (the drift hook's cached findings) render
    destinations identically.
    """
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def link_applies(
    spec: LinkSpec,
    *,
    harnesses: Iterable[str],
    is_mac: bool,
    is_linux: bool,
    is_wsl: bool,
    profile: str,
) -> bool:
    """Return whether ``spec`` should be linked for this machine/options.

    The environment inputs arrive pre-resolved (a harness *set*, platform
    booleans, the profile) rather than bundled in a run Context, so both the
    installer and the SessionStart hook gate identically without either
    importing the other's plumbing.
    """
    harness_set = frozenset(harnesses)
    if spec.harness is not None and spec.harness not in harness_set:
        return False
    if spec.platform == "mac" and not is_mac:
        return False
    if spec.platform == "linux" and not is_linux:
        return False
    if spec.wsl == "exclude" and is_wsl:
        return False
    if spec.wsl == "only" and not is_wsl:
        return False
    return profile not in spec.profile_exclude


def iter_concrete_links(
    spec: LinkSpec, *, dotfiles: Path, home: Path
) -> Iterator[tuple[Path, Path, str]]:
    """Expand one ``links.toml`` row into concrete ``(src, dest, relative_src)`` triples.

    A normal row (``dir`` unset) yields exactly one triple. A ``dir=true``
    row recursively globs its source directory, skipping plain subdirectory
    entries (walked into, never linked themselves) and junk — dotfiles and
    editor swap/backup files — so those never get symlinked in. A missing,
    empty, or unreadable source directory yields nothing, with no error and
    no special-casing: see the plan's cleanup design for why "can't confirm"
    and "confirmed empty" are deliberately not distinguished.
    """
    src_root = dotfiles / spec.src
    dest_root = expand_dest(spec.dest, home)
    if not spec.dir:
        yield src_root, dest_root, spec.src
        return
    try:
        candidates = sorted(src_root.rglob("*"))
    except OSError:
        return
    for path in candidates:
        if not (path.is_file() or path.is_symlink()):
            continue
        if path.name.startswith(".") or path.name.endswith(JUNK_SUFFIXES):
            continue
        relative = path.relative_to(src_root)
        yield path, dest_root / relative, f"{spec.src}/{relative}"


def _applies_kwargs(
    harnesses: Iterable[str],
    is_mac: bool,
    is_linux: bool,
    is_wsl: bool,
    profile: str,
) -> dict[str, object]:
    """Bundle the machine facts into one kwargs dict for the scoping calls."""
    return dict(
        harnesses=harnesses,
        is_mac=is_mac,
        is_linux=is_linux,
        is_wsl=is_wsl,
        profile=profile,
    )


def dir_applies(
    dir_spec: ManagedDirSpec,
    specs: Sequence[LinkSpec],
    *,
    dotfiles: Path,
    home: Path,
    harnesses: Iterable[str],
    is_mac: bool,
    is_linux: bool,
    is_wsl: bool,
    profile: str,
) -> bool:
    """Return whether a declared directory is in scope for this run.

    A declared directory inherits its harness, platform, WSL, and profile
    scoping from the ``[[link]]`` rows whose destinations fall inside it,
    rather than carrying gating fields of its own. Reusing
    :func:`link_applies` picks up all four for free. A directory no row targets
    is audited unconditionally, since there is no evidence to gate on.
    """
    directory = expand_dest(dir_spec.dest, home)
    related = [
        spec for spec in specs if expand_dest(spec.dest, home).is_relative_to(directory)
    ]
    if not related:
        return True
    scope = _applies_kwargs(harnesses, is_mac, is_linux, is_wsl, profile)
    return any(link_applies(spec, **scope) for spec in related)  # type: ignore[arg-type]


def gather_links(
    specs: Sequence[LinkSpec],
    *,
    dotfiles: Path,
    home: Path,
    harnesses: Iterable[str],
    is_mac: bool,
    is_linux: bool,
    is_wsl: bool,
    profile: str,
) -> list[tuple[Path, Path, str, bool]]:
    """Expand every ``links.toml`` row into concrete triples, once per run.

    Computed for every spec regardless of whether it applies to this run's
    machine/harness selection — the fourth element flags that separately.
    Creation and collision-detection only look at the applicable triples;
    orphan-detection's "still expected" set spans every triple regardless,
    so a harness/platform-gated row is never misreported as removed. This
    single pass feeds all three, rather than each recomputing its own
    expansion independently.
    """
    scope = _applies_kwargs(harnesses, is_mac, is_linux, is_wsl, profile)
    result: list[tuple[Path, Path, str, bool]] = []
    for spec in specs:
        applicable = link_applies(spec, **scope)  # type: ignore[arg-type]
        for src, dest, rel in iter_concrete_links(spec, dotfiles=dotfiles, home=home):
            result.append((src, dest, rel, applicable))
    return result


# ── consolidated audit entry point ──────────────────────────────────────


def audit_links(
    *,
    dotfiles: Path,
    home: Path,
    harnesses: Iterable[str],
    is_mac: bool,
    is_linux: bool,
    is_wsl: bool,
    profile: str,
    manifest_file: Path,
    format_path: Callable[[Path], str],
    report_uninstalled: bool = False,
    specs: Sequence[LinkSpec] | None = None,
    managed_dirs: Sequence[ManagedDirSpec] | None = None,
) -> tuple[dict[str, list[str]], dict[Path, int], int]:
    """Run the full read-only link audit and return its findings as plain data.

    Self-contained — it parses ``links.toml`` and stats live destinations, so
    it does real filesystem I/O — but it never prints, exits, or mutates.
    The assembly sequence lives here and nowhere else: install.py's
    ``do_check_links`` delegates its computation to this call (keeping its
    own printing), and the drift hook calls it directly, so the two consumers
    can never drift apart about what counts as drift.

    Args:
        dotfiles: This checkout's root — the target live links are expected
            to point at.
        home: The home directory destinations expand against.
        harnesses: The harness selection in scope (``do_check_links`` widens
            no-``--harness`` to every harness; see install.py).
        manifest_file: The history manifest's path (read for orphans,
            never-installed, and live-backup exemptions).
        format_path: Renders a Path for a findings message (pure formatting).
        report_uninstalled: Also flag destinations the manifest never
            recorded creating; see :func:`check_applicable_links`.
        specs, managed_dirs: Pre-parsed links.toml rows, for a caller that
            already parsed the table (the drift hook fingerprints it); parsed
            here when omitted.

    Returns:
        The findings by bucket, the per-other-checkout link counts (see
        :func:`check_applicable_links` — not a finding), and how many
        declared directories were audited. Malformed links.toml propagates
        the parsers' ``ValueError``/``TypeError`` so the caller decides how
        loudly to fail.
    """
    links_toml = dotfiles / "links.toml"
    if specs is None:
        specs = load_links(links_toml)
    if managed_dirs is None:
        managed_dirs = load_managed_dirs(links_toml)
    scope = _applies_kwargs(harnesses, is_mac, is_linux, is_wsl, profile)
    links = gather_links(  # type: ignore[arg-type]
        specs, dotfiles=dotfiles, home=home, **scope
    )
    entries = read_manifest_entries(manifest_file)
    findings, foreign = check_applicable_links(
        links,
        dotfiles=dotfiles,
        format_path=format_path,
        manifest_entries=entries,
        report_uninstalled=report_uninstalled,
        home=home,
    )
    check_orphaned_links(
        links, findings, format_path=format_path, manifest_entries=entries
    )
    dirs_audited = check_unmanaged_files(
        managed_dirs,
        links,
        home=home,
        format_path=format_path,
        dir_applies=lambda dir_spec: dir_applies(  # type: ignore[arg-type]
            dir_spec, specs, dotfiles=dotfiles, home=home, **scope
        ),
        findings=findings,
        manifest_entries=entries,
    )
    return findings, foreign, dirs_audited


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


# The links.toml src whose destinations dotfiles recomposes with a personal
# overlay on any machine that has both repos checked out. Mirrors install.py's
# write-side ``_personal_overlay_unwrapped``/``_PERSONAL_OVERLAY_SRC_REL`` (the
# 2026-09-07 incident guard) — install.py imports this constant rather than
# keeping its own copy, so the two halves of the guard can't drift apart.
PERSONAL_OVERLAY_SRC_REL = "claude/CORE_INSTRUCTIONS.md"


def personal_overlay_composed_target(home: Path) -> Path:
    """The file dotfiles actually composes and symlinks ``PERSONAL_OVERLAY_SRC_REL``'s
    destinations to, on a machine with both repos checked out."""
    return home / "dotfiles" / "claude" / "global-instructions.md"


def check_applicable_links(
    links: Sequence[tuple[Path, Path, str, bool]],
    *,
    dotfiles: Path,
    format_path: Callable[[Path], str],
    manifest_entries: Iterable[dict[str, object]] = (),
    report_uninstalled: bool = False,
    home: Path | None = None,
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
        home: The home directory to check the personal-overlay exemption
            against (see ``PERSONAL_OVERLAY_SRC_REL``). ``None`` (the
            default, and what every pre-existing caller passes) disables
            the exemption entirely rather than guessing at a home — a unit
            test exercising an unrelated entry has no reason to know about
            dotfiles composition.

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
        if (
            home is not None
            and rel == PERSONAL_OVERLAY_SRC_REL
            and same_path(target, personal_overlay_composed_target(home))
        ):
            # Correctly recomposed by dotfiles/scripts/install-with-agent-
            # toolkit.sh, not a mismatch — see PERSONAL_OVERLAY_SRC_REL.
            continue
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
