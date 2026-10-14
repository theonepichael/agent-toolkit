#!/usr/bin/env python3
# rename-transition: remove after the clean-break renames
"""Temporary machinery for the clean-break renames: env fallbacks and store transforms.

The six public renames (``depart`` -> ``uninstall`` through ``dev_status`` ->
``work_tracker``) each ship as their own release. Two things cannot be
renamed by a text sweep, so each release carries a short transition window:

* Environment variables live in the user's shell config. :func:`env` reads the
  new name first and falls back to the old one with a one-line deprecation
  warning, and ``report`` lists every old name still set.
* Persisted values in stores need versioned rewrites. The store-transform
  framework below runs registered transforms from ``install.py`` after
  relinking, inside ``migration_lock.exclusive()``.

Everything here, and every call site that uses it, carries the removal tag
(``REMOVAL_TAG``), so a single grep finds all of it when the last rename has
reached every machine and the machinery is deleted. This ticket ships the
framework with empty tables; each rename ticket registers its own rows,
adapters and transforms.

Expand/contract contract for adapters: in the release that adds a transform,
the new readers accept every version in ``supported_versions |
{current_version}`` and writers write ``current_version``. ``install.py``
transforms only after a clean link audit, so old code never meets new data,
and an interrupted run leaves a readable mixed state that the next install
finishes.

Usage:
    rename_transition.py report [--session]
    rename_transition.py checklist OLD NEW --release LABEL [--file PATH ...]

Flags
  report --session   print nothing when no legacy variables are set, else one
                     prefixed line; always exit 0 (for the session-start hook)
  checklist --file   replace the default list of external files to scan
                     (repeatable)
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_toolkit_paths  # noqa: E402 — sibling dir inserted above

# Rewrites store files during a transform (see check_toolkit_paths).
TOOLKIT_DATA = "writer"
REMOVAL_TAG = "rename-transition: remove after the clean-break renames"
PLACEHOLDER = "{X}"


# ── environment variables ────────────────────────────────────────────────────


@dataclass(frozen=True)
class LegacyEnv:
    """One renamed environment variable: ``old`` is read only as a fallback."""

    old: str
    new: str
    removed_after: str


def family(
    old_template: str,
    new_template: str,
    members: Iterable[str],
    removed_after: str,
) -> tuple[LegacyEnv, ...]:
    """Expand a per-member variable family into explicit rows.

    Both templates must contain exactly one ``{X}``; each member replaces it.
    Explicit rows keep lookup and conflict checks exact (no pattern matching).
    """
    for template in (old_template, new_template):
        if template.count(PLACEHOLDER) != 1:
            raise ValueError(f"{template!r} must contain exactly one {PLACEHOLDER}")
    return tuple(
        LegacyEnv(
            old_template.replace(PLACEHOLDER, member),
            new_template.replace(PLACEHOLDER, member),
            removed_after,
        )
        for member in members
    )


# Each rename ticket adds its rows here.
LEGACY_ENV: tuple[LegacyEnv, ...] = ()

_warned: set[str] = set()


def _reset_warnings_for_tests() -> None:
    _warned.clear()


def validate_legacy_env(rows: Sequence[LegacyEnv] | None = None) -> None:
    """Raise ``ValueError`` on a duplicate, a self-map or an ``A -> B -> C`` chain."""
    rows = LEGACY_ENV if rows is None else rows
    olds = [row.old for row in rows]
    news = [row.new for row in rows]
    for label, names in (("old", olds), ("new", news)):
        dupes = sorted({name for name in names if names.count(name) > 1})
        if dupes:
            raise ValueError(f"duplicate {label} names in LEGACY_ENV: {dupes}")
    both = sorted(set(olds) & set(news))
    if both:
        raise ValueError(f"names used as both old and new (chains): {both}")


def env(
    new: str,
    default: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    rows: Sequence[LegacyEnv] | None = None,
) -> str | None:
    """Read ``new``, falling back to its old name with a once-per-process warning.

    "Set" means present in the environment, including an empty string.
    """
    environ = os.environ if environ is None else environ
    if new in environ:
        return environ[new]
    for row in LEGACY_ENV if rows is None else rows:
        if row.new == new and row.old in environ:
            if row.old not in _warned:
                _warned.add(row.old)
                print(
                    f"{row.old} is deprecated; rename it to {row.new} "
                    f"(fallback removed after {row.removed_after})",
                    file=sys.stderr,
                )
            return environ[row.old]
    return default


def legacy_env_report(
    environ: Mapping[str, str],
    *,
    rows: Sequence[LegacyEnv] | None = None,
) -> list[tuple[str, str]]:
    """Every ``(old, new)`` pair whose old name is set, sorted."""
    rows = LEGACY_ENV if rows is None else rows
    return sorted((row.old, row.new) for row in rows if row.old in environ)


def report_main(
    *,
    session: bool,
    environ: Mapping[str, str] | None = None,
    rows: Sequence[LegacyEnv] | None = None,
) -> int:
    """Print the legacy-variable report; see the module docstring for modes."""
    hits = legacy_env_report(os.environ if environ is None else environ, rows=rows)
    if session:
        if hits:
            pairs = ", ".join(f"{old} -> {new}" for old, new in hits)
            print(f"legacy env vars set: {pairs} — rename them in your shell config")
        return 0
    if not hits:
        print("no legacy environment variables set")
        return 0
    for old, new in hits:
        print(f"{old} -> {new}")
    return 1


# ── store transforms ─────────────────────────────────────────────────────────


class StoreFormatError(ValueError):
    """An adapter could not recognise a store file's shape or version."""


class TransformError(RuntimeError):
    """A store transform could not run; the message names the file."""


class StoreAdapter(Protocol):
    """How the framework reads, versions and writes one store's files.

    A store that persists no version today maps its unmarked shape to an
    explicit implicit version, and marks every later version explicitly. A
    line-oriented (JSONL) store marks the version per record, so appends and
    rotation keep every record self-describing.
    """

    name: str
    current_version: int
    supported_versions: frozenset[int]

    def discover(self, home: Path) -> list[Path]: ...

    def version_of(self, data: bytes) -> int: ...

    def load(self, data: bytes) -> object: ...

    def dump(self, obj: object, version: int) -> bytes: ...


@dataclass(frozen=True)
class Transform:
    """Lift one store's data from ``from_version`` to ``to_version``."""

    store: str
    from_version: int
    to_version: int
    apply: Callable[[object], object]
    describe: str


def store_path(domain: str) -> Path:
    """Resolve a toolkit data domain for an adapter's ``discover``.

    Adapters must locate toolkit data through this (that is, through
    ``agent_toolkit_paths``) rather than joining paths onto ``home``, so an
    ``AGENT_TOOLKIT_HOME`` override is honoured. ``home`` is passed to
    ``discover`` only for stores that genuinely live under it.
    """
    return agent_toolkit_paths.path_for(domain)


# Each rename ticket adds its adapter and transforms here.
ADAPTERS: tuple[StoreAdapter, ...] = ()
TRANSFORMS: tuple[Transform, ...] = ()


def validate_registry(
    adapters: Sequence[StoreAdapter] | None = None,
    transforms: Sequence[Transform] | None = None,
) -> None:
    """Raise ``ValueError`` unless every store has one contiguous chain to current."""
    adapters = ADAPTERS if adapters is None else adapters
    transforms = TRANSFORMS if transforms is None else transforms
    by_name = {adapter.name: adapter for adapter in adapters}
    if len(by_name) != len(adapters):
        raise ValueError("duplicate store adapter names")
    seen: set[tuple[str, int]] = set()
    for transform in transforms:
        if transform.store not in by_name:
            raise ValueError(f"transform for unknown store {transform.store!r}")
        if transform.to_version != transform.from_version + 1:
            raise ValueError(
                f"{transform.store}: transform {transform.from_version} -> "
                f"{transform.to_version} must step by exactly one version"
            )
        key = (transform.store, transform.from_version)
        if key in seen:
            raise ValueError(
                f"{transform.store}: two transforms from version "
                f"{transform.from_version}"
            )
        seen.add(key)
    for adapter in adapters:
        for version in adapter.supported_versions:
            if version >= adapter.current_version:
                raise ValueError(
                    f"{adapter.name}: supported version {version} is not below "
                    f"current {adapter.current_version}"
                )
            for step in range(version, adapter.current_version):
                if (adapter.name, step) not in seen:
                    raise ValueError(
                        f"{adapter.name}: no transform from version {step} "
                        f"(chain from {version} to {adapter.current_version})"
                    )


def _chain(
    adapter: StoreAdapter, version: int, transforms: Sequence[Transform]
) -> list[Transform]:
    steps = {t.from_version: t for t in transforms if t.store == adapter.name}
    return [steps[v] for v in range(version, adapter.current_version)]


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TransformError(f"{path}: could not read: {exc}") from exc


def _checked_version(adapter: StoreAdapter, path: Path, data: bytes) -> int:
    try:
        version = adapter.version_of(data)
    except Exception as exc:
        raise TransformError(f"{path}: unreadable {adapter.name} data: {exc}") from exc
    known = adapter.supported_versions | {adapter.current_version}
    if version not in known:
        raise TransformError(
            f"{path}: {adapter.name} version {version} is "
            f"{'newer than' if version > adapter.current_version else 'not one of'} "
            f"the versions this code handles ({sorted(known)})"
        )
    return version


def _plan(
    home: Path,
    adapters: Sequence[StoreAdapter],
    transforms: Sequence[Transform],
) -> list[tuple[StoreAdapter, Path, bytes, int]]:
    """Every file below its store's current version; validates all versions."""
    pending = []
    for adapter in adapters:
        try:
            paths = adapter.discover(home)
        except Exception as exc:
            raise TransformError(
                f"{adapter.name}: could not list store files: {exc}"
            ) from exc
        for path in paths:
            data = _read(path)
            if data is None:  # vanished between discovery and reading
                continue
            version = _checked_version(adapter, path, data)
            if version < adapter.current_version:
                _chain(adapter, version, transforms)  # KeyError means a bad registry
                pending.append((adapter, path, data, version))
    return pending


def pending_transforms(
    home: Path,
    *,
    adapters: Sequence[StoreAdapter] | None = None,
    transforms: Sequence[Transform] | None = None,
) -> list[str]:
    """Unlocked, read-only preview: one "would apply" line per pending file."""
    adapters = ADAPTERS if adapters is None else adapters
    transforms = TRANSFORMS if transforms is None else transforms
    lines = []
    for adapter, path, _data, version in _plan(home, adapters, transforms):
        for step in _chain(adapter, version, transforms):
            lines.append(
                f"would apply {adapter.name} v{step.from_version}->v{step.to_version} "
                f"({step.describe}) to {path}"
            )
    return lines


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` durably, keeping its mode.

    Same shape as ``dev_status_storage.atomic_write_json``: temp file in the
    same directory, fsync it, ``os.replace``, fsync the directory.
    """
    mode = None
    with contextlib.suppress(FileNotFoundError):
        mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def run_transforms(
    home: Path,
    *,
    dry_run: bool,
    adapters: Sequence[StoreAdapter] | None = None,
    transforms: Sequence[Transform] | None = None,
) -> list[str]:
    """Bring every store file under ``home`` to its adapter's current version.

    An unlocked pre-scan validates every file and decides whether any work
    is pending; with none it returns ``[]`` and never touches the lock. Real
    work happens inside ``migration_lock.exclusive("rename-transform")``,
    which rediscovers and rereads everything. ``dry_run`` is pure reads.

    Raises:
        TransformError: a file is unreadable, at an unknown or newer
            version, or a transform failed. Files finished before the
            failure stay finished; re-running completes the rest.
    """
    adapters = ADAPTERS if adapters is None else adapters
    transforms = TRANSFORMS if transforms is None else transforms
    if dry_run:
        return pending_transforms(home, adapters=adapters, transforms=transforms)
    if not _plan(home, adapters, transforms):
        return []

    import migration_lock

    applied: list[str] = []
    with migration_lock.exclusive("rename-transform"):
        for adapter, path, data, version in _plan(home, adapters, transforms):
            try:
                obj = adapter.load(data)
            except Exception as exc:
                raise TransformError(
                    f"{path}: {adapter.name} data could not be loaded: {exc}"
                ) from exc
            target = version
            for step in _chain(adapter, version, transforms):
                try:
                    obj = step.apply(obj)
                except Exception as exc:
                    raise TransformError(
                        f"{path}: {adapter.name} transform "
                        f"v{step.from_version}->v{step.to_version} failed: {exc}"
                    ) from exc
                target = step.to_version
            try:
                out = adapter.dump(obj, target)
            except Exception as exc:
                raise TransformError(
                    f"{path}: {adapter.name} data could not be dumped: {exc}"
                ) from exc
            written = _checked_dump_version(adapter, path, out)
            if written != target:
                raise TransformError(
                    f"{path}: {adapter.name} dump produced version {written}, "
                    f"expected {target}; nothing written"
                )
            try:
                atomic_write_bytes(path, out)
            except OSError as exc:
                raise TransformError(f"{path}: could not write: {exc}") from exc
            applied.append(f"applied {adapter.name} v{version}->v{target} to {path}")
    return applied


def _checked_dump_version(adapter: StoreAdapter, path: Path, data: bytes) -> int:
    try:
        return adapter.version_of(data)
    except Exception as exc:
        raise TransformError(
            f"{path}: {adapter.name} dump is unreadable, so its version cannot be "
            f"checked; nothing written ({exc})"
        ) from exc


# ── per-rename checklist ─────────────────────────────────────────────────────

# The ledger's per-rename checklist, applied identically to each rename.
LEDGER_CHECKLIST = (
    "Defining source file renamed",
    "Every Python import updated",
    "`links.toml` destination and manifest entry updated",
    "`install.py` / `install.sh` references updated",
    "Workflow files regenerated, per harness, from templates — never hand-edited",
    "Generator parameter tables updated",
    "`INTERFACES.md` regenerated",
    "`GLOSSARY.md` term added with its first-use definition",
    "`README.md` references updated",
    "Tests and fixtures updated; drift checks pass",
    "Persisted field or value transform written, with a version bump",
    "Environment variable fallback added with a deprecation warning",
    "Manual-edit checklist emitted for external instruction files",
    "No undocumented alias left behind, except the env-var fallback window",
)

# External files no command edits; scanned for the old name. A machine whose
# ~/.claude/CLAUDE.md is composed from a personal overlay gets a reminder to
# edit the overlay source rather than the composed file (see checklist()).
DEFAULT_CHECKLIST_FILES = (
    "~/.claude/CLAUDE.md",
    "~/.claude/settings.local.json",
)


def checklist(
    old: str,
    new: str,
    release: str,
    *,
    files: Sequence[Path] | None = None,
) -> str:
    """Markdown checklist for one rename: the ledger items plus manual-edit hits."""
    if files is None:
        files = [Path(f).expanduser() for f in DEFAULT_CHECKLIST_FILES]
    lines = [f"## {release}: `{old}` -> `{new}`", "", "### Release checklist", ""]
    lines += [f"- [ ] {item}" for item in LEDGER_CHECKLIST]
    lines += ["", "### Manual edits outside the repository", ""]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            lines.append(f"- {path}: not present")
            continue
        hits = [
            (number, line.strip())
            for number, line in enumerate(text.splitlines(), start=1)
            if old in line
        ]
        if not hits:
            lines.append(f"- {path}: no occurrences")
        for number, line in hits:
            lines.append(f"- [ ] {path}:{number}: `{line}`")
    lines += [
        (
            "- [ ] If ~/.claude/CLAUDE.md is composed from a personal overlay, "
            "edit the overlay source, not the composed file"
        ),
        (
            "- [ ] The fork-installed work machine: the same files there "
            "(not readable from here)"
        ),
    ]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rename_transition.py",
        description="Clean-break rename transition helpers (temporary).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="list legacy env vars still set")
    report.add_argument("--session", action="store_true")
    check = sub.add_parser("checklist", help="print one rename's checklist")
    check.add_argument("old")
    check.add_argument("new")
    check.add_argument("--release", required=True)
    check.add_argument("--file", action="append", type=Path, dest="files")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "report":
        return report_main(session=args.session)
    print(checklist(args.old, args.new, args.release, files=args.files), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
