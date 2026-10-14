#!/usr/bin/env python3
"""Test support: sandbox homes for the toolkit-home layout.

Production modules resolve every data path at each use, so pointing ``HOME``
at a sandbox home is all a store fixture needs. Nothing in this module reads
or patches a production path.

:func:`activate_sandbox_home` gives a unittest-style fixture one sandbox
home for the length of a test.

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


@dataclass
class SandboxHome:
    home: Path

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
