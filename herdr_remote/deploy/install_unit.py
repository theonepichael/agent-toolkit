#!/usr/bin/env python3
"""Render herdr-remote-bridge.service for this machine's toolkit checkout.

The repo copy of the unit is a template: its ``WorkingDirectory=`` and
``ExecStart=`` name the checkout as ``@AGENT_TOOLKIT_CHECKOUT@``. This script
is the only thing that renders it -- the deploy README's one-time setup and
``deploy.sh`` (on every deploy) both run it -- so a moved checkout or an edited
template takes effect on the next deploy instead of leaving a stale path in
the installed unit.

The checkout is ``--checkout``, else the one this script lives in (resolved by
``toolkit_checkout.toolkit_checkout()``). It must be an agent-toolkit
checkout, and its absolute path may only contain ``[A-Za-z0-9._+/-]``: systemd
splits ``ExecStart=`` on whitespace and expands ``%`` specifiers, so anything
outside that set is refused rather than escaped.

Usage::

    install_unit.py [--checkout PATH] [--unit-dir DIR] [--no-reload] [--check]

``--unit-dir`` defaults to ``$SYSTEMD_USER_DIR``, else
``~/.config/systemd/user``. ``--check`` validates and renders in memory and
writes nothing. Without ``--no-reload`` (or ``--check``) a successful write is
followed by ``systemctl --user daemon-reload``. Exit 0 on success, 1 on any
refusal.

Requires Python 3.12+; standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PLACEHOLDER = "@AGENT_TOOLKIT_CHECKOUT@"
UNIT_NAME = "herdr-remote-bridge.service"
TEMPLATE = Path(__file__).resolve().parent / UNIT_NAME
SAFE_PATH = re.compile(r"/[A-Za-z0-9._+/-]+")
ANY_PLACEHOLDER = re.compile(r"@[A-Z_]+@")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-scripts"))

import toolkit_checkout  # noqa: E402


class UnitTemplateError(ValueError):
    """The template or the rendered unit is not what this script expects."""


def render_unit(template: str, checkout: Path) -> str:
    """Return ``template`` with the checkout placeholder replaced by ``checkout``.

    The template must hold the placeholder exactly twice, once on the
    ``WorkingDirectory=`` line and once on the ``ExecStart=`` line, and the
    result must hold no ``@NAME@`` token at all.
    """
    holders = [ln for ln in template.splitlines() if PLACEHOLDER in ln]
    if (
        template.count(PLACEHOLDER) != 2
        or len(holders) != 2
        or not holders[0].startswith("WorkingDirectory=")
        or not holders[1].startswith("ExecStart=")
    ):
        raise UnitTemplateError(
            f"template must use {PLACEHOLDER} exactly once on its "
            "WorkingDirectory= line and once on its ExecStart= line"
        )
    rendered = template.replace(PLACEHOLDER, str(checkout))
    leftover = ANY_PLACEHOLDER.search(rendered)
    if leftover:
        raise UnitTemplateError(f"unrendered token {leftover.group()} in the unit")
    return rendered


def _resolve_checkout(given: str | None) -> Path:
    if given is None:
        return toolkit_checkout.toolkit_checkout()
    checkout = Path(given).expanduser().resolve()
    if not toolkit_checkout.is_toolkit_checkout(checkout):
        raise toolkit_checkout.CheckoutNotFoundError(
            f"{checkout} is not an agent-toolkit checkout"
        )
    return checkout


def _check_safe_path(checkout: Path) -> None:
    text = str(checkout)
    if SAFE_PATH.fullmatch(text):
        return
    bad = next(c for c in text if not SAFE_PATH.fullmatch(f"/{c}"))
    raise UnitTemplateError(
        f"checkout path {text!r} has unsupported character {bad!r}; "
        "only [A-Za-z0-9._+/-] can be rendered into a systemd unit safely"
    )


def _unit_dir(given: str | None) -> Path:
    if given is not None:
        return Path(given)
    env = os.environ.get("SYSTEMD_USER_DIR")
    return Path(env) if env else Path.home() / ".config" / "systemd" / "user"


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render herdr-remote-bridge.service for this toolkit checkout."
    )
    parser.add_argument(
        "--checkout",
        help="agent-toolkit checkout to render in (default: the one this "
        "script lives in)",
    )
    parser.add_argument(
        "--unit-dir",
        help="where to write the unit (default: $SYSTEMD_USER_DIR, else "
        "~/.config/systemd/user)",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="skip `systemctl --user daemon-reload` after writing",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and render in memory only; write nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        checkout = _resolve_checkout(args.checkout)
        _check_safe_path(checkout)
        rendered = render_unit(TEMPLATE.read_text(encoding="utf-8"), checkout)
    except (toolkit_checkout.CheckoutNotFoundError, UnitTemplateError) as exc:
        print(f"install_unit: {exc}", file=sys.stderr)
        return 1
    if args.check:
        return 0
    target = _unit_dir(args.unit_dir) / UNIT_NAME
    _write_atomically(target, rendered)
    print(f"install_unit: wrote {target} for {checkout}")
    if not args.no_reload:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
