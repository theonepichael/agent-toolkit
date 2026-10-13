#!/usr/bin/env python3
"""Test support: sandbox homes that hold the legacy and toolkit-home layouts.

``check_not_stale`` still needs a home where the retired legacy layout
(``.claude/data``) and the toolkit-home layout (``.agent-toolkit``) coexist,
so a stale path under the legacy location can be refused. Production modules
resolve every data path at each use, so pointing ``HOME`` at a sandbox home
is all a store fixture needs.

:func:`build_two_layout_homes` builds:

- ``local``: one sandbox home holding BOTH the legacy layout under
  ``.claude/data`` and the toolkit-home layout under ``.agent-toolkit``.
- ``peer``: a separate sandbox home with the legacy layout only.

Entries are created empty. Store content is the caller's business. The
layout pointer is no longer written: post-cutover the resolver is always
toolkit-home, so no pointer is needed for these fixtures.

:func:`activate_sandbox_home` gives a unittest-style fixture one sandbox
home for the length of a test. Nothing in this module reads or patches a
production path.

``activated()`` mutates ``os.environ``. That is safe across pytest-xdist
workers (separate processes), not across threads in one process.

Standard library only; never imports pytest.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import agent_toolkit_paths

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

    @property
    def legacy_data(self) -> Path:
        return self.home / ".claude" / "data"

    @property
    def toolkit_home(self) -> Path:
        return self.home / ".agent-toolkit"

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
) -> SandboxHome:
    """Make ``root`` the ``HOME`` until cleanup; return it as a SandboxHome.

    ``add_cleanup`` is ``unittest.TestCase.addCleanup`` or pytest's
    ``request.addfinalizer``. Nothing is created under ``root``: the store
    code creates what it needs, exactly as on a fresh machine.
    """
    home = SandboxHome(root)
    context = home.activated()
    context.__enter__()
    add_cleanup(lambda: context.__exit__(None, None, None))
    return home


def _build_legacy(data: Path) -> None:
    for name in LEGACY_DATA_DIRS:
        (data / name).mkdir(parents=True, exist_ok=True)
    for name in LEGACY_DATA_FILES:
        (data / name).touch()


def build_two_layout_homes(root: Path) -> TwoLayoutHomes:
    """Build ``root/local`` (both layouts) and ``root/peer`` (legacy only)."""
    local = SandboxHome(root / "local")
    peer = SandboxHome(root / "peer")
    _build_legacy(local.legacy_data)
    for name in TOOLKIT_HOME_DIRS:
        (local.toolkit_home / name).mkdir(parents=True, exist_ok=True)
    _build_legacy(peer.legacy_data)
    return TwoLayoutHomes(local=local, peer=peer)
