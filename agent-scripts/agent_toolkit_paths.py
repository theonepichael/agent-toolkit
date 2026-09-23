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
Release 0 stores a pointer at ``<home>/.claude/data/toolkit_state.json``.
The file is a JSON object with ``schema`` set to ``1`` and ``layout`` set to
``"legacy"`` or ``"toolkit-home"``. When the pointer is missing the layout
is ``"legacy"``. A malformed pointer raises :class:`LayoutError`; removing
the pointer restores the legacy layout.

Toolkit-home root
-----------------
In ``"toolkit-home"`` layout, data lives under ``<toolkit-root>/data/``,
where ``<toolkit-root>`` is ``$AGENT_TOOLKIT_HOME`` if set, otherwise
``<home>/.agent-toolkit``. ``AGENT_TOOLKIT_HOME`` must be absolute.

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
A :class:`Resolver` caches the parsed layout keyed by the pointer's
``(st_ino, st_mtime_ns, st_size)`` (or a sentinel for "missing") and the
current ``<home>``. Every call re-``stat``s the pointer and invalidates the
cache when the signature or ``<home>`` changes.

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


class LayoutError(Exception):
    """Raised when the layout pointer is malformed or the override is invalid."""


class StaleLayoutError(LayoutError):
    """Raised when a path belongs to the layout that is no longer current."""


class UnknownDomainError(ValueError):
    """Raised when :func:`path_for` is asked for an unregistered domain."""

    def __init__(self, domain: str) -> None:
        super().__init__(
            f"unknown domain {domain!r}; valid domains: {', '.join(DOMAINS)}"
        )
        self.domain = domain


class Resolver:
    """Resolve toolkit data paths with per-process caching."""

    def __init__(self) -> None:
        self._cache: dict[str, object] = {
            "home": "",
            "signature": _MISSING,
            "layout": "legacy",
        }

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
        except FileNotFoundError:
            return "legacy"
        except OSError as exc:
            raise LayoutError(
                f"cannot read layout pointer {pointer}: {exc}; "
                "remove it to restore the legacy layout"
            ) from exc
        except UnicodeDecodeError as exc:
            raise LayoutError(
                f"malformed layout pointer {pointer}: {exc}; "
                "remove it to restore the legacy layout"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LayoutError(
                f"malformed layout pointer {pointer}: {exc}; "
                "remove it to restore the legacy layout"
            ) from exc

        if not isinstance(data, dict):
            raise LayoutError(
                f"malformed layout pointer {pointer}: not a JSON object; "
                "remove it to restore the legacy layout"
            )

        schema = data.get("schema")
        if type(schema) is not int or schema != POINTER_SCHEMA:
            raise LayoutError(
                f"malformed layout pointer {pointer}: "
                f"expected schema {POINTER_SCHEMA}, got {schema!r}; "
                "remove it to restore the legacy layout"
            )

        layout = data.get("layout")
        if layout not in ("legacy", "toolkit-home"):
            raise LayoutError(
                f"malformed layout pointer {pointer}: "
                f"unknown layout {layout!r}; "
                "remove it to restore the legacy layout"
            )

        return layout  # type: ignore[return-value]

    def _signature(self, pointer: Path) -> object:
        try:
            stat = pointer.stat()
        except OSError:
            return _MISSING
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _load(self) -> tuple[Path, Layout]:
        home = self._home()
        pointer = home / POINTER_RELPATH
        signature = self._signature(pointer)

        if (
            str(home) != self._cache["home"]  # type: ignore[comparison-overlap]
            or signature != self._cache["signature"]
        ):
            layout = self._read_pointer(pointer)
            self._cache = {
                "home": str(home),
                "signature": signature,
                "layout": layout,
            }

        return home, self._cache["layout"]  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Drop the cached layout so the next call re-reads the pointer."""
        self._cache = {
            "home": "",
            "signature": _MISSING,
            "layout": "legacy",
        }

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


def check_not_stale(path: Path) -> None:
    """Refuse a path from the non-current layout using :data:`DEFAULT_RESOLVER`."""
    DEFAULT_RESOLVER.check_not_stale(path)


def current_layout() -> Layout:
    """Return the current layout using :data:`DEFAULT_RESOLVER`."""
    return DEFAULT_RESOLVER.layout()


def write_pointer(home: Path, layout: Layout) -> None:
    """Atomically write the layout pointer under ``home``.

    Creates the parent directory. The write is atomic via a temporary file
    in the same directory followed by ``os.replace``.
    """
    pointer = home / POINTER_RELPATH
    pointer.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema": POINTER_SCHEMA, "layout": layout}) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=pointer.parent,
        prefix=".toolkit_state",
        suffix=".tmp",
        delete=False,
    ) as fh:
        fh.write(payload)
        tmp_path = Path(fh.name)
    os.replace(tmp_path, pointer)
