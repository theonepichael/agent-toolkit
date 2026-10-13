#!/usr/bin/env python3
"""Read-modify-write agy's ``trustedWorkspaces`` list safely.

agy gates an interactive startup with a "Do you trust the contents of this
project?" dialog unless the startup directory is listed verbatim in
``~/.gemini/antigravity-cli/settings.json`` under ``trustedWorkspaces`` (an
undocumented key — see the atk-delegate-agy-workspace-trust spec). There is no
parent-dir/glob trust and no skip flag, so callers pre-populate the exact path
before launching agy. This module does that edit atomically (mkstemp +
``os.replace``) under a sidecar ``fcntl.flock``, preserving every other key, and
fails closed on any shape it does not recognise: agy owns this file, so we never
guess its schema.

The lock serialises this module's own concurrent writers (swarm workers launching
agy at once). It does not serialise agy itself (agy never takes the lock); the
residual race is a lost agy edit only if agy rewrites the file during our
millisecond critical section. The ``(size, mtime_ns)`` re-check before replace
shrinks that window but cannot close it — the guarantee is "never torn", not
"never lost".
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

TOOLKIT_DATA = "none"

SETTINGS_DEFAULT = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
TRUST_KEY = "trustedWorkspaces"
TRUST_PROMPT_TEXT = "Do you trust the contents of this project?"

# Bounded re-read when the file changes under us between read and replace. The
# final re-stat cannot be made atomic with the replace, so this is detect-and-
# retry, not a guarantee.
MAX_RETRIES = 2


class TrustSettingsError(RuntimeError):
    """A settings edit that must not proceed, with a reason fit to show the user."""


def settings_path() -> Path:
    """The default agy settings.json path (expanduser'd)."""
    return SETTINGS_DEFAULT


def _stat_identity(path: Path) -> tuple[int, int]:
    """``(size, mtime_ns)``, or a refusal when the file is unusable."""
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise TrustSettingsError(
            f"agy settings file not found at {path}; run `agy` once interactively "
            "so it creates the file, then retry"
        ) from exc
    except OSError as exc:
        raise TrustSettingsError(
            f"cannot stat agy settings file {path}: {exc}"
        ) from exc
    return (st.st_size, st.st_mtime_ns)


def _read_obj(path: Path) -> dict[str, object]:
    """Parse settings.json; fail closed on missing/invalid/non-object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TrustSettingsError(
            f"agy settings file not found at {path}; run `agy` once interactively "
            "so it creates the file, then retry"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise TrustSettingsError(
            f"cannot read agy settings file {path}: {exc}"
        ) from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrustSettingsError(
            f"agy settings file {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(obj, dict):
        raise TrustSettingsError(
            f"agy settings file {path} has a non-object root; refusing to guess its schema"
        )
    return obj


def _trusted(obj: dict[str, object], path: Path) -> list[str]:
    """The ``trustedWorkspaces`` list, failing closed on any unexpected shape.

    A missing key is the normal fresh state and reads as the empty list (the key
    is optional per agy's own ``omitempty``).
    """
    if TRUST_KEY not in obj:
        return []
    value = obj[TRUST_KEY]
    if not isinstance(value, list):
        raise TrustSettingsError(
            f"agy settings key {TRUST_KEY!r} is a {type(value).__name__}, not a "
            "list; refusing to guess agy's schema"
        )
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TrustSettingsError(
                f"agy settings key {TRUST_KEY!r} contains a non-string entry; "
                "refusing to guess agy's schema"
            )
        entries.append(entry)
    return entries


def load_settings(path: object) -> dict[str, object]:
    """Parse ``path``; raises :class:`TrustSettingsError` on a missing file,
    invalid JSON, or a non-object root (the ``trustedWorkspaces`` shape is
    validated by :func:`trusted_entries`, not here)."""
    return _read_obj(Path(path))


def trusted_entries(path: object) -> list[str]:
    """The ``trustedWorkspaces`` entries of ``path`` (missing key -> ``[]``)."""
    p = Path(path)
    return _trusted(_read_obj(p), p)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively for the duration of the critical section.

    ``path`` is resolved first so the lock file and the write target agree even
    when settings.json is reached through a symlink — otherwise a caller using
    the symlink and one using the real path would hold two different locks for
    the same file.
    """
    path = path.resolve()
    lock = Path(str(path) + ".lock")
    fd = -1
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise TrustSettingsError(
            f"cannot open agy trust lock {lock}: {exc} (is the parent directory present?)"
        ) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _atomic_rewrite(path: Path, new_obj: dict[str, object]) -> None:
    """Atomically replace ``path`` with ``new_obj``, preserving the existing mode.

    ``path`` is resolved first: when settings.json is a symlink, ``os.replace``
    on the link would replace the link itself with a regular file and detach it
    from its target, so the write goes to the real target path instead.
    """
    path = path.resolve()
    parent = path.parent
    mode = 0o644
    with contextlib.suppress(OSError):
        mode = stat.S_IMODE(path.stat().st_mode)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1  # now owned by the stream
            json.dump(new_obj, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        raise TrustSettingsError(
            f"cannot write agy settings file {path}: {exc}"
        ) from exc
    finally:
        if fd != -1:
            with contextlib.suppress(OSError):
                os.close(fd)
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _locked_write[Result](
    path: Path,
    transform: Callable[[dict[str, object]], tuple[dict[str, object] | None, Result]],
) -> Result:
    """Run ``transform(obj) -> (new_obj | None, result)`` under the lock.

    ``new_obj is None`` means "no change"; ``result`` is returned to the caller.
    Re-reads once when the file changes between read and replace.
    """
    with _locked(path):
        for _ in range(MAX_RETRIES):
            before = _stat_identity(path)
            obj = _read_obj(path)
            new_obj, result = transform(obj)
            if new_obj is None:
                return result
            if _stat_identity(path) != before:
                continue
            _atomic_rewrite(path, new_obj)
            return result
        raise TrustSettingsError(
            f"agy settings file {path} changed concurrently; gave up after "
            f"{MAX_RETRIES} retries"
        )


def ensure_trusted(dir_path: object, *, path: object = None) -> bool:
    """Add ``dir_path`` (resolved) to ``trustedWorkspaces`` if its exact
    spelling is not already present; True if changed.

    agy matches the startup directory verbatim (no symlink resolution, no
    parent-dir inheritance), so a differently-spelled entry that merely
    resolves to the same directory must NOT suppress the add.
    """
    settings = Path(path) if path is not None else settings_path()
    target = str(Path(dir_path).resolve())

    def transform(obj: dict[str, object]) -> tuple[dict[str, object] | None, bool]:
        entries = _trusted(obj, settings)
        if target in entries:
            return None, False
        new_obj = dict(obj)
        new_obj[TRUST_KEY] = entries + [target]
        return new_obj, True

    return bool(_locked_write(settings, transform))


def remove_trusted(dir_path: object, *, path: object = None) -> bool:
    """Remove ``dir_path`` (resolved) from ``trustedWorkspaces`` if present; True if changed."""
    settings = Path(path) if path is not None else settings_path()
    target = str(Path(dir_path).resolve())

    def transform(obj: dict[str, object]) -> tuple[dict[str, object] | None, bool]:
        entries = _trusted(obj, settings)
        kept = [entry for entry in entries if entry != target]
        if len(kept) == len(entries):
            return None, False
        new_obj = dict(obj)
        new_obj[TRUST_KEY] = kept
        return new_obj, True

    return bool(_locked_write(settings, transform))


def prune_trusted(*, path: object = None) -> list[str]:
    """Remove every entry whose path no longer exists; returns the removed entries."""
    settings = Path(path) if path is not None else settings_path()

    def transform(obj: dict[str, object]) -> tuple[dict[str, object] | None, list[str]]:
        entries = _trusted(obj, settings)
        kept: list[str] = []
        removed: list[str] = []
        for entry in entries:
            (kept if Path(entry).exists() else removed).append(entry)
        if not removed:
            return None, removed
        new_obj = dict(obj)
        new_obj[TRUST_KEY] = kept
        return new_obj, removed

    return list(_locked_write(settings, transform))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--settings",
        help="path to settings.json (default: ~/.gemini/antigravity-cli/settings.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", parents=[common], help="trust a directory")
    add.add_argument("dir", help="directory path to trust")
    remove = sub.add_parser("remove", parents=[common], help="untrust a directory")
    remove.add_argument("dir", help="directory path to untrust")
    sub.add_parser(
        "prune", parents=[common], help="remove entries whose path no longer exists"
    )
    sub.add_parser("list", parents=[common], help="print trusted workspace entries")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    path = args.settings
    try:
        if args.command == "add":
            ensure_trusted(args.dir, path=path)
        elif args.command == "remove":
            remove_trusted(args.dir, path=path)
        elif args.command == "prune":
            print(json.dumps(prune_trusted(path=path)))
        elif args.command == "list":
            for entry in trusted_entries(path if path is not None else settings_path()):
                print(entry)
    except TrustSettingsError as exc:
        print(f"agy-workspace-trust: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
