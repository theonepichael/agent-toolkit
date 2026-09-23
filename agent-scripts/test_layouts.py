#!/usr/bin/env python3
"""Test support: sandbox homes that hold the legacy and toolkit-home layouts.

The toolkit-home cutover needs tests where one path resolver sees two
layouts side by side, where the layout pointer flips inside one process
lifetime, and where a second, independent home plays the reconciliation
peer. Production modules resolve every data path at each use, so pointing
``HOME`` at a sandbox home is all a store fixture needs.

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

:func:`activate_sandbox_home` gives a unittest-style fixture one sandbox
home for the length of a test. That consumers follow a flip in the same
process is proved by test/test_path_for_per_use.py. Nothing in this module
reads or patches a production path.

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

import agent_toolkit_paths

Layout = agent_toolkit_paths.Layout
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
POINTER_RELPATH = agent_toolkit_paths.POINTER_RELPATH


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
        previous_home = os.environ.get("HOME")
        previous_override = os.environ.pop("AGENT_TOOLKIT_HOME", None)
        os.environ["HOME"] = str(self.home)
        try:
            yield self.home
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home
            if previous_override is not None:
                os.environ["AGENT_TOOLKIT_HOME"] = previous_override


@dataclass
class TwoLayoutHomes:
    local: SandboxHome
    peer: SandboxHome


def activate_sandbox_home(
    root: Path,
    add_cleanup: Callable[[Callable[[], object]], None],
    *,
    pointer_writer: PointerWriter = agent_toolkit_paths.write_pointer,
) -> SandboxHome:
    """Make ``root`` the ``HOME`` until cleanup; return it as a SandboxHome.

    ``add_cleanup`` is ``unittest.TestCase.addCleanup`` or pytest's
    ``request.addfinalizer``. Nothing is created under ``root``: the store
    code creates what it needs, exactly as on a fresh machine.
    """
    home = SandboxHome(root, pointer_writer)
    context = home.activated()
    context.__enter__()
    add_cleanup(lambda: context.__exit__(None, None, None))
    return home


def _build_legacy(data: Path) -> None:
    for name in LEGACY_DATA_DIRS:
        (data / name).mkdir(parents=True, exist_ok=True)
    for name in LEGACY_DATA_FILES:
        (data / name).touch()


def build_two_layout_homes(
    root: Path,
    *,
    pointer_writer: PointerWriter = agent_toolkit_paths.write_pointer,
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
