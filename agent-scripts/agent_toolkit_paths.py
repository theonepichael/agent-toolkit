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

    def path_for(self, domain: str) -> Path:
        """Resolve ``domain`` to a filesystem path for the current layout."""
        if domain not in _LEGACY_SEGMENTS:
            raise UnknownDomainError(domain)

        home, layout = self._load()
        segment = _LEGACY_SEGMENTS[domain]

        if layout == "legacy":
            return home / ".claude" / "data" / segment

        return self._toolkit_root(home) / "data" / segment


DEFAULT_RESOLVER: Resolver = Resolver()


def path_for(domain: str) -> Path:
    """Resolve ``domain`` using :data:`DEFAULT_RESOLVER`."""
    return DEFAULT_RESOLVER.path_for(domain)


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
