#!/usr/bin/env python3
"""Test support: sandbox homes that hold the legacy and toolkit-home layouts.

The toolkit-home cutover needs tests where one path resolver sees two
layouts side by side, where the layout pointer flips inside one process
lifetime, and where a second, independent home plays the reconciliation
peer. Existing store fixtures patch one module-global ``DATA_DIR`` instead,
which cannot represent any of that.

:func:`build_two_layout_homes` builds:

- ``local``: one sandbox home holding BOTH the legacy layout under
  ``.claude/data`` and the toolkit-home layout under ``.agent-toolkit``.
- ``peer``: a separate sandbox home with the legacy layout only. Its layout
  pointer always says ``legacy``, so it never points at a layout it lacks.

Entries are created empty. Store content is the caller's business.

The layout pointer (``.claude/data/toolkit_state.json`` in release 0) is
written only through a pluggable ``PointerWriter``. The path resolver owns
the pointer's real format; :func:`provisional_pointer_writer` is a
placeholder until the resolver supplies its own writer.

What this does NOT prove: that any consumer switches layouts. Production
modules bake ``Path.home()``-rooted constants at import, so
:meth:`SandboxHome.activated` cannot redirect constants already computed.
Switching is proved by the resolver's own tests: the same resolver instance,
before and after :meth:`SandboxHome.flip`, with no module reload and no
constant patch. Nothing in this module reads or patches a production path
constant.

``activated()`` mutates ``os.environ``. That is safe across pytest-xdist
workers (separate processes), not across threads in one process.

Standard library only; never imports pytest.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Layout = Literal["legacy", "toolkit-home"]
PointerWriter = Callable[[Path, Layout], None]
"""``(home, layout)``: writes the layout pointer under ``home``."""

LEGACY_DATA_DIRS: tuple[str, ...] = (
    "backlog",
    "backlog-out-of-scope",
    "grill",
    "to-tickets",
    "standup",
)
LEGACY_DATA_FILES: tuple[str, ...] = (
    "guard_rails_audit.jsonl",
    "backend_calls.jsonl",
)
LEGACY_DATA_ENTRIES: tuple[str, ...] = LEGACY_DATA_DIRS + LEGACY_DATA_FILES
TOOLKIT_HOME_DIRS: tuple[str, ...] = ("data", "scripts", "hooks", "config")
POINTER_RELPATH = Path(".claude") / "data" / "toolkit_state.json"


def provisional_pointer_writer(home: Path, layout: Layout) -> None:
    """PROVISIONAL placeholder: writes ``{"layout": <layout>}`` as JSON.

    The path resolver owns the pointer's real format and replaces this.
    """
    path = home / POINTER_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"layout": layout}) + "\n")


@dataclass
class SandboxHome:
    home: Path
    pointer_writer: PointerWriter

    @property
    def legacy_data(self) -> Path:
        return self.home / ".claude" / "data"

    @property
    def toolkit_home(self) -> Path:
        return self.home / ".agent-toolkit"

    def flip(self, layout: Layout) -> None:
        """Point this home at ``layout`` through the pointer writer."""
        self.pointer_writer(self.home, layout)

    @contextmanager
    def activated(self) -> Iterator[Path]:
        """Set ``HOME`` to this home for the block; always restore it."""
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            yield self.home
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous


@dataclass
class TwoLayoutHomes:
    local: SandboxHome
    peer: SandboxHome


def _build_legacy(data: Path) -> None:
    for name in LEGACY_DATA_DIRS:
        (data / name).mkdir(parents=True, exist_ok=True)
    for name in LEGACY_DATA_FILES:
        (data / name).touch()


def build_two_layout_homes(
    root: Path,
    *,
    pointer_writer: PointerWriter = provisional_pointer_writer,
    initial: Layout = "legacy",
) -> TwoLayoutHomes:
    """Build ``root/local`` (both layouts) and ``root/peer`` (legacy only)."""
    local = SandboxHome(root / "local", pointer_writer)
    peer = SandboxHome(root / "peer", pointer_writer)
    _build_legacy(local.legacy_data)
    for name in TOOLKIT_HOME_DIRS:
        (local.toolkit_home / name).mkdir(parents=True, exist_ok=True)
    _build_legacy(peer.legacy_data)
    local.flip(initial)
    peer.flip("legacy")
    return TwoLayoutHomes(local=local, peer=peer)
