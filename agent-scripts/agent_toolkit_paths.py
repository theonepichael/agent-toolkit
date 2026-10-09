#!/usr/bin/env python3
"""Single source of truth for toolkit data paths.

Domains (release 0 legacy paths under ``<home>/.claude/data/``):

===================  ===========================================
Domain               Resolved path
===================  ===========================================
work-items           ``backlog/``
out-of-scope         ``backlog-out-of-scope/``
decisions            ``grill/``
ticket-batches       ``to-tickets/``
standups             ``standup/``
guard-rail-log       ``guard_rails_audit.jsonl``
backend-log          ``backend_calls.jsonl``
===================  ===========================================

Layout pointer
--------------
The pointer is a JSON object with ``schema`` set to ``1`` and ``layout`` set
to ``"legacy"`` or ``"toolkit-home"`` (:func:`pointer_payload`). Release 2
reads it from two places: ``<toolkit-root>/data/toolkit_state.json`` first,
then the legacy ``<home>/.claude/data/toolkit_state.json``. When both exist
the toolkit-home copy wins, never an error. When neither exists the layout
is ``"legacy"`` if a legacy domain entry exists under ``.claude/data`` (an
unmigrated machine) and ``"toolkit-home"`` otherwise (a fresh install). A
malformed or unreadable pointer raises :class:`LayoutError`, as does an
``AGENT_TOOLKIT_HOME`` that makes both locations the same file.
``install.sh --move-layout-pointer`` moves a legacy pointer to the new
location.

Toolkit-home root
-----------------
In ``"toolkit-home"`` layout, data lives under ``<toolkit-root>/data/``,
where ``<toolkit-root>`` is ``$AGENT_TOOLKIT_HOME`` if set, otherwise
``<home>/.agent-toolkit``. ``AGENT_TOOLKIT_HOME`` must be absolute.
:func:`toolkit_root` returns that root on its own. Install locations under it
(``scripts/``, ``hooks/``, ``icons/``) are never switched by layout.

Stale paths
-----------
Callers resolve paths at each use, never at import, so a long-lived process
follows a layout flip. A path captured before a flip can still reach a
writer through an explicit argument. :func:`check_not_stale` refuses such a
path (:class:`StaleLayoutError`) when it sits under a domain's path for the
layout that is not current. The current layout wins when both layouts map a
domain to the same place, and a path under neither layout passes.

Caching
-------
A :class:`Resolver` caches the parsed layout keyed by ``<home>``, the
toolkit root, and both pointers' ``(st_ino, st_mtime_ns, st_size)`` (or a
sentinel for "missing"). Every call re-``stat``s both pointers, legacy
first, and re-reads when any part of the key changes.

Requires Python 3.12+.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "infrastructure"

Layout = Literal["legacy", "toolkit-home"]

DOMAINS: tuple[str, ...] = (
    "work-items",
    "out-of-scope",
    "decisions",
    "ticket-batches",
    "standups",
    "guard-rail-log",
    "backend-log",
)
POINTER_RELPATH = Path(".claude") / "data" / "toolkit_state.json"
TOOLKIT_HOME_POINTER_RELPATH = Path("data") / "toolkit_state.json"
POINTER_SCHEMA = 1
ENV_HOME = "AGENT_TOOLKIT_HOME"

_LEGACY_SEGMENTS: dict[str, str] = {
    "work-items": "backlog",
    "out-of-scope": "backlog-out-of-scope",
    "decisions": "grill",
    "ticket-batches": "to-tickets",
    "standups": "standup",
    "guard-rail-log": "guard_rails_audit.jsonl",
    "backend-log": "backend_calls.jsonl",
}

_MISSING = object()
_SELECT_ATTEMPTS = 3


class LayoutError(Exception):
    """Raised when the layout pointer is malformed or the override is invalid."""


class StaleLayoutError(LayoutError):
    """Raised when a path belongs to the layout that is no longer current."""


class UpgradeRequiredError(LayoutError):
    """Raised when legacy toolkit data exists without a completed migration record."""


class UnknownDomainError(ValueError):
    """Raised when :func:`path_for` is asked for an unregistered domain."""

    def __init__(self, domain: str) -> None:
        super().__init__(
            f"unknown domain {domain!r}; valid domains: {', '.join(DOMAINS)}"
        )
        self.domain = domain


class _Vanished(Exception):
    """A pointer that ``stat`` just saw was gone by the time it was read."""


class Resolver:
    """Resolve toolkit data paths with per-process caching."""

    def __init__(self) -> None:
        self._cache: dict[str, object] = {"key": None, "layout": "legacy"}

    def _home(self) -> Path:
        return Path.home()

    def _toolkit_root(self, home: Path) -> Path:
        override = os.environ.get(ENV_HOME)
        if override is not None:
            root = Path(override)
            if not root.is_absolute():
                raise LayoutError(f"{ENV_HOME} must be an absolute path: {override!r}")
            return root
        return home / ".agent-toolkit"

    def _read_pointer(self, pointer: Path) -> Layout:
        try:
            raw = pointer.read_text()
        except FileNotFoundError as exc:
            raise _Vanished from exc
        except OSError as exc:
            raise LayoutError(
                f"cannot read layout pointer {pointer}: {exc}; fix or remove it"
            ) from exc
        except UnicodeDecodeError as exc:
            raise LayoutError(
                f"malformed layout pointer {pointer}: {exc}; fix or remove it"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LayoutError(
                f"malformed layout pointer {pointer}: {exc}; fix or remove it"
            ) from exc

        if not isinstance(data, dict):
            raise LayoutError(
                f"malformed layout pointer {pointer}: not a JSON object; "
                "fix or remove it"
            )

        schema = data.get("schema")
        if type(schema) is not int or schema != POINTER_SCHEMA:
            raise LayoutError(
                f"malformed layout pointer {pointer}: "
                f"expected schema {POINTER_SCHEMA}, got {schema!r}; "
                "fix or remove it"
            )

        layout = data.get("layout")
        if layout not in ("legacy", "toolkit-home"):
            raise LayoutError(
                f"malformed layout pointer {pointer}: "
                f"unknown layout {layout!r}; "
                "fix or remove it"
            )

        return layout  # type: ignore[return-value]

    def _signature(self, pointer: Path) -> object:
        try:
            stat = pointer.stat()
        except (FileNotFoundError, NotADirectoryError):
            return _MISSING
        except OSError as exc:
            raise LayoutError(f"cannot stat layout pointer {pointer}: {exc}") from exc
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _has_legacy_data(self, home: Path) -> bool:
        """True if any legacy domain entry exists under ``~/.claude/data``.

        Only the domain names count, so foreign leftovers in that directory
        do not make a pointerless home look like an unmigrated one.
        """
        data = home / ".claude" / "data"
        for segment in _LEGACY_SEGMENTS.values():
            try:
                (data / segment).lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                raise LayoutError(f"cannot inspect {data / segment}: {exc}") from exc
            return True
        return False

    def _select(
        self,
        home: Path,
        legacy: tuple[Path, object],
        new: tuple[Path, object] | None,
        root_error: LayoutError | None,
    ) -> Layout:
        legacy_path, legacy_sig = legacy
        if new is not None:
            new_path, new_sig = new
            # realpath, not samefile: the aliasing is a misconfiguration even
            # when neither pointer exists yet.
            if os.path.realpath(new_path) == os.path.realpath(legacy_path):
                raise LayoutError(
                    f"the toolkit-home pointer {new_path} and the legacy pointer "
                    f"{legacy_path} are the same file; {ENV_HOME} must not "
                    "point at ~/.claude"
                )
            if new_sig is not _MISSING:
                return self._read_pointer(new_path)
        if legacy_sig is not _MISSING:
            return self._read_pointer(legacy_path)
        if self._has_legacy_data(home):
            return "legacy"
        if root_error is not None:
            raise root_error
        return "toolkit-home"

    def _load(self) -> tuple[Path, Layout]:
        home = self._home()
        legacy_path = home / POINTER_RELPATH
        root: Path | None
        try:
            root = self._toolkit_root(home)
            root_error = None
        except LayoutError as exc:
            root, root_error = None, exc
        new_path = None if root is None else root / TOOLKIT_HOME_POINTER_RELPATH
        for _attempt in range(_SELECT_ATTEMPTS):
            # Legacy first: the move writes the new pointer before deleting the
            # old one, so this order can never observe both as absent.
            legacy_sig = self._signature(legacy_path)
            new_sig = _MISSING if new_path is None else self._signature(new_path)
            key = (str(home), str(root), legacy_sig, new_sig)
            if key == self._cache["key"]:
                return home, self._cache["layout"]  # type: ignore[return-value]
            new = None if new_path is None else (new_path, new_sig)
            try:
                layout = self._select(home, (legacy_path, legacy_sig), new, root_error)
            except _Vanished:
                continue
            self._cache = {"key": key, "layout": layout}
            return home, layout
        raise LayoutError(
            f"layout pointer kept changing while it was read ({legacy_path}, "
            f"{new_path}); retry the command"
        )

    def invalidate(self) -> None:
        """Drop the cached layout so the next call re-reads the pointer."""
        self._cache = {"key": None, "layout": "legacy"}

    def layout(self) -> Layout:
        """Return the current layout, reading the pointer if necessary."""
        return self._load()[1]

    def _path_in(self, home: Path, domain: str, layout: Layout) -> Path:
        if domain not in _LEGACY_SEGMENTS:
            raise UnknownDomainError(domain)
        segment = _LEGACY_SEGMENTS[domain]
        if layout == "legacy":
            return home / ".claude" / "data" / segment
        return self._toolkit_root(home) / "data" / segment

    def path_for(self, domain: str) -> Path:
        """Resolve ``domain`` to a filesystem path for the current layout."""
        home, layout = self._load()
        return self._path_in(home, domain, layout)

    def path_for_layout(self, domain: str, layout: Layout) -> Path:
        """Resolve ``domain`` for ``layout``, whatever the current layout is."""
        return self._path_in(self._home(), domain, layout)

    def check_not_stale(self, path: Path) -> None:
        """Raise :class:`StaleLayoutError` if ``path`` belongs to the other layout.

        ``path`` is stale when it equals or sits under some domain's path for
        the non-current layout and under no domain's path for the current
        one. Paths are compared after ``os.path.abspath``; symlinks are not
        followed. While ``legacy`` is current, an invalid
        ``AGENT_TOOLKIT_HOME`` is ignored here, because legacy resolution
        never reads it.
        """
        home, layout = self._load()
        other: Layout = "toolkit-home" if layout == "legacy" else "legacy"
        target = Path(os.path.abspath(path))
        current = [
            Path(os.path.abspath(self._path_in(home, d, layout))) for d in DOMAINS
        ]
        if any(target.is_relative_to(root) for root in current):
            return
        try:
            stale = [self._path_in(home, d, other) for d in DOMAINS]
        except LayoutError:
            if layout == "legacy":
                return
            raise
        for root in stale:
            if target.is_relative_to(os.path.abspath(root)):
                raise StaleLayoutError(
                    f"{path} belongs to the {other!r} layout, but the current "
                    f"layout is {layout!r}; it was resolved before a layout flip. "
                    "Re-run the command so it resolves the current path."
                )


DEFAULT_RESOLVER: Resolver = Resolver()


def path_for(domain: str) -> Path:
    """Resolve ``domain`` using :data:`DEFAULT_RESOLVER`."""
    return DEFAULT_RESOLVER.path_for(domain)


def path_for_layout(domain: str, layout: Layout) -> Path:
    """Resolve ``domain`` for ``layout`` using :data:`DEFAULT_RESOLVER`."""
    return DEFAULT_RESOLVER.path_for_layout(domain, layout)


def layout_path(home: Path, domain: str, layout: Layout) -> Path:
    """Resolve ``domain`` for ``layout`` under an explicit ``home``.

    Pure: reads no pointer and ignores the process ``HOME``. The toolkit-home
    side still honors ``AGENT_TOOLKIT_HOME``. For code that plans paths for a
    home other than the running one (the migration's path transform).
    """
    return Resolver()._path_in(home, domain, layout)


def toolkit_root() -> Path:
    """Return ``$AGENT_TOOLKIT_HOME``, or ``<home>/.agent-toolkit``.

    Independent of layout: reads no pointer, so a malformed one cannot break a
    lookup of an install location. Raises :class:`LayoutError` for a relative
    override.
    """
    return Resolver()._toolkit_root(Path.home())


def has_legacy_data(home: Path) -> bool:
    """True if a legacy domain entry exists in ``home``'s legacy data directory."""
    return Resolver()._has_legacy_data(home)


def check_not_stale(path: Path) -> None:
    """Refuse a path from the non-current layout using :data:`DEFAULT_RESOLVER`."""
    DEFAULT_RESOLVER.check_not_stale(path)


def current_layout() -> Layout:
    """Return the current layout using :data:`DEFAULT_RESOLVER`."""
    return DEFAULT_RESOLVER.layout()


def pointer_payload(layout: Layout) -> bytes:
    """The canonical bytes of a layout pointer saying ``layout``."""
    return (json.dumps({"schema": POINTER_SCHEMA, "layout": layout}) + "\n").encode()


def write_pointer(home: Path, layout: Layout, *, toolkit_root: Path | None) -> None:
    """Atomically write the layout pointer.

    ``toolkit_root=None`` writes the legacy pointer under ``home``; a path
    writes ``<toolkit_root>/data/toolkit_state.json``. Creates the parent
    directory. The write is atomic and durable: a temporary file in the same
    directory is fsynced, renamed over the pointer with ``os.replace``, and
    the directory is fsynced.
    """
    if toolkit_root is None:
        pointer = home / POINTER_RELPATH
    else:
        pointer = toolkit_root / TOOLKIT_HOME_POINTER_RELPATH
    pointer.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=pointer.parent,
        prefix=".toolkit_state",
        suffix=".tmp",
        delete=False,
    ) as fh:
        fh.write(pointer_payload(layout))
        fh.flush()
        os.fsync(fh.fileno())
        tmp_path = Path(fh.name)
    os.replace(tmp_path, pointer)
    fd = os.open(pointer.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check_upgrade_required(home: Path | None = None) -> None:
    """Raise :class:`UpgradeRequiredError` if legacy state exists without
    a completed migration record.
    """
    root = home or Path.home()
    legacy_data = root / ".claude" / "data"
    if not legacy_data.is_dir():
        return
    has_legacy_state = False
    for segment in _LEGACY_SEGMENTS.values():
        if (legacy_data / segment).exists():
            has_legacy_state = True
            break
    if not has_legacy_state:
        try:
            if any(p.name != POINTER_RELPATH.name for p in legacy_data.iterdir()):
                has_legacy_state = True
        except OSError:
            pass

    if not has_legacy_state:
        return

    history = root / ".local" / "state" / "agent-toolkit" / "history.jsonl"
    if history.is_file():
        try:
            for line in history.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if (
                    isinstance(entry, dict)
                    and entry.get("kind") == "migration"
                    and entry.get("outcome") in ("committed", "finalized")
                ):
                    return
        except OSError:
            pass

    raise UpgradeRequiredError(
        f"legacy toolkit data found at {legacy_data} without a completed "
        f"migration record in {history}; run 'install.sh --migrate-toolkit-home' to upgrade"
    )
