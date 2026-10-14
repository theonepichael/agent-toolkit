#!/usr/bin/env python3
"""Single source of truth for toolkit data paths.

Every domain resolves under ``<toolkit-root>/data/<segment>``, where
``<toolkit-root>`` is ``$AGENT_TOOLKIT_HOME`` if set, otherwise
``<home>/.agent-toolkit``. ``AGENT_TOOLKIT_HOME`` must be absolute.

Domains:

===================  ===========================================
Domain               Resolved path (under ``<toolkit-root>/data/``)
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
to ``"toolkit-home"`` (the ``"legacy"`` value was retired with the Release
0→1 cutover). It is read from ``<toolkit-root>/data/toolkit_state.json``.
When it is absent the layout is ``"toolkit-home"`` (a fresh install). A
malformed or unreadable pointer — or one that still says ``"legacy"`` —
raises :class:`LayoutError`, as does a relative ``AGENT_TOOLKIT_HOME``.

Caching
-------
A :class:`Resolver` caches a validated pointer keyed by ``<home>``, the
toolkit root, and the pointer's ``(st_ino, st_mtime_ns, st_size)`` (or a
sentinel for "missing"). Every call re-``stat``s the pointer and re-reads
when any part of the key changes.

Requires Python 3.12+.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "infrastructure"

DOMAINS: tuple[str, ...] = (
    "work-items",
    "out-of-scope",
    "decisions",
    "ticket-batches",
    "standups",
    "guard-rail-log",
    "backend-log",
)
TOOLKIT_HOME_POINTER_RELPATH = Path("data") / "toolkit_state.json"
POINTER_SCHEMA = 1
ENV_HOME = "AGENT_TOOLKIT_HOME"

DOMAIN_SEGMENTS: dict[str, str] = {
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
        self._cache: dict[str, object] = {"key": None}

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

    def _read_pointer(self, pointer: Path) -> str:
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
        if layout != "toolkit-home":
            raise LayoutError(
                f"stale or malformed layout pointer {pointer}: "
                f"expected 'toolkit-home', got {layout!r}; "
                "fix or remove it"
            )

        return "toolkit-home"

    def _signature(self, pointer: Path) -> object:
        try:
            stat = pointer.stat()
        except (FileNotFoundError, NotADirectoryError):
            return _MISSING
        except OSError as exc:
            raise LayoutError(f"cannot stat layout pointer {pointer}: {exc}") from exc
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _select(
        self,
        new: tuple[Path, object] | None,
        root_error: LayoutError | None,
    ) -> None:
        if new is not None:
            new_path, new_sig = new
            if new_sig is not _MISSING:
                self._read_pointer(new_path)
        if root_error is not None:
            raise root_error

    def _load(self) -> Path:
        home = self._home()
        root: Path | None
        try:
            root = self._toolkit_root(home)
            root_error = None
        except LayoutError as exc:
            root, root_error = None, exc
        new_path = None if root is None else root / TOOLKIT_HOME_POINTER_RELPATH
        for _attempt in range(_SELECT_ATTEMPTS):
            new_sig = _MISSING if new_path is None else self._signature(new_path)
            key = (str(home), str(root), new_sig)
            if key == self._cache["key"]:
                return home
            new = None if new_path is None else (new_path, new_sig)
            try:
                self._select(new, root_error)
            except _Vanished:
                continue
            self._cache = {"key": key}
            return home
        raise LayoutError(
            f"layout pointer kept changing while it was read ({new_path}); "
            "retry the command"
        )

    def invalidate(self) -> None:
        """Drop the cached pointer validation so the next call re-reads it."""
        self._cache = {"key": None}

    def _path_in(self, home: Path, domain: str) -> Path:
        if domain not in DOMAIN_SEGMENTS:
            raise UnknownDomainError(domain)
        segment = DOMAIN_SEGMENTS[domain]
        return self._toolkit_root(home) / "data" / segment

    def path_for(self, domain: str) -> Path:
        """Resolve ``domain`` to a filesystem path under the toolkit root."""
        home = self._load()
        return self._path_in(home, domain)


DEFAULT_RESOLVER: Resolver = Resolver()


def path_for(domain: str) -> Path:
    """Resolve ``domain`` using :data:`DEFAULT_RESOLVER`."""
    return DEFAULT_RESOLVER.path_for(domain)


def toolkit_root() -> Path:
    """Return ``$AGENT_TOOLKIT_HOME``, or ``<home>/.agent-toolkit``.

    Reads no pointer, so a malformed one cannot break a lookup of an install
    location. Raises :class:`LayoutError` for a relative override.
    """
    return Resolver()._toolkit_root(Path.home())
