#!/usr/bin/env python3
"""Locate the agent-toolkit source checkout.

:func:`toolkit_checkout` returns the checkout this module was loaded from,
else ``$AGENT_TOOLKIT_PATH``, else ``~/Workspace/agent-toolkit/agent-toolkit``
(the nested layout, which keeps worktrees out of ``~/Workspace``), else
``~/Workspace/agent-toolkit`` (the flat layout). A candidate counts only when
:func:`is_toolkit_checkout` accepts it: in the nested layout the flat path is
the container of the checkout and its worktrees, so it exists but is rejected.
A set ``$AGENT_TOOLKIT_PATH`` that is not a checkout raises
:class:`CheckoutNotFoundError` rather than falling through.

This is the source tree, not the install home (``~/.agent-toolkit``, see
``agent_toolkit_paths.toolkit_root``). Installed copies under
``~/.agent-toolkit/scripts/`` are symlinks into ``agent-scripts/``, so the
module's own resolved path normally lands in the checkout.

Usage::

    python3 toolkit_checkout.py checkout   # print the checkout; exit 1 with the reason

Requires Python 3.12+; standard library only.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

ENV_CHECKOUT = "AGENT_TOOLKIT_PATH"
CHECKOUT_MARKERS: tuple[str, ...] = ("install.py", "links.toml", "agent-scripts")


class CheckoutNotFoundError(LookupError):
    """No candidate location is an agent-toolkit checkout."""


def is_toolkit_checkout(path: Path) -> bool:
    """True when ``path`` has install.py and links.toml files and an agent-scripts/ dir."""
    return (
        (path / "install.py").is_file()
        and (path / "links.toml").is_file()
        and (path / "agent-scripts").is_dir()
    )


def toolkit_checkout(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    self_path: Path | None = None,
) -> Path:
    """Return the agent-toolkit checkout, resolved.

    Tries this module's own checkout, then ``$AGENT_TOOLKIT_PATH``, then the
    nested and flat ``~/Workspace`` layouts. Raises
    :class:`CheckoutNotFoundError` when ``$AGENT_TOOLKIT_PATH`` is set but is
    not a checkout, or when no candidate is one.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    tried: list[str] = []

    own = Path(self_path if self_path is not None else __file__).resolve()
    own_checkout = own.parent.parent
    if is_toolkit_checkout(own_checkout):
        return own_checkout
    tried.append(f"{own_checkout} (this script's own location)")

    configured = env.get(ENV_CHECKOUT)
    if configured:
        expanded = configured
        if configured == "~" or configured.startswith("~/"):
            expanded = str(Path(env.get("HOME", str(home)))) + configured[1:]
        candidate = Path(expanded)
        if is_toolkit_checkout(candidate):
            return candidate.resolve()
        raise CheckoutNotFoundError(
            f"${ENV_CHECKOUT} is set to {configured!r}, which is not an "
            f"agent-toolkit checkout (needs {', '.join(CHECKOUT_MARKERS)})"
        )
    tried.append(f"${ENV_CHECKOUT} (unset)")

    for candidate in (
        home / "Workspace" / "agent-toolkit" / "agent-toolkit",
        home / "Workspace" / "agent-toolkit",
    ):
        if is_toolkit_checkout(candidate):
            return candidate.resolve()
        tried.append(str(candidate))

    raise CheckoutNotFoundError(
        "no agent-toolkit checkout found; tried: "
        + "; ".join(tried)
        + f". Set ${ENV_CHECKOUT} to the checkout."
    )


def main(argv: list[str] | None = None) -> int:
    """``checkout``: print the agent-toolkit checkout, or exit 1 with the reason."""
    args = sys.argv[1:] if argv is None else argv
    if args != ["checkout"]:
        print("usage: toolkit_checkout.py checkout", file=sys.stderr)
        return 2
    try:
        print(toolkit_checkout())
    except CheckoutNotFoundError as exc:
        print(f"toolkit_checkout: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
