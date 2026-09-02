#!/usr/bin/env python3
"""Every pi/extensions/*.ts must have a links.toml entry.

Pi loads extensions from ~/.pi/agent/extensions/, so an extension file that
lands in the repo without a matching [[link]] block is never installed. It
looks finished in git and does nothing on the machine.

This has already bitten once: custom-footer.ts was hand-symlinked from the
worktree it was written in, so when that worktree was removed the installed
link dangled and the extension silently stopped loading.

Standing exception: herdr-agent-state.ts in the installed directory is
installed by herdr itself, not by dotfiles — the [[managed_dir]] entry for
~/.pi/agent/extensions ignore-lists it. The tests below pin both halves of
that arrangement.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_DIR = REPO_ROOT / "pi" / "extensions"
LINKS_TOML = REPO_ROOT / "links.toml"


def _linked_extension_sources() -> set[str]:
    data = tomllib.loads(LINKS_TOML.read_text())
    return {
        link["src"]
        for link in data.get("link", [])
        if link.get("src", "").startswith("pi/extensions/")
    }


def test_every_extension_is_linked() -> None:
    on_disk = {f"pi/extensions/{p.name}" for p in EXTENSIONS_DIR.glob("*.ts")}
    linked = _linked_extension_sources()

    unlinked = sorted(on_disk - linked)
    assert not unlinked, (
        f"pi extensions with no links.toml entry: {unlinked} — "
        'add a [[link]] block with harness = "pi" so install.py installs them'
    )


def test_no_link_points_at_a_missing_extension() -> None:
    on_disk = {f"pi/extensions/{p.name}" for p in EXTENSIONS_DIR.glob("*.ts")}
    dangling = sorted(_linked_extension_sources() - on_disk)
    assert not dangling, f"links.toml references missing pi extensions: {dangling}"


def _pi_extensions_managed_dir_ignores() -> list[tuple[str, ...]]:
    data = tomllib.loads(LINKS_TOML.read_text())
    return [
        tuple(entry.get("ignore", ()))
        for entry in data.get("managed_dir", [])
        if entry.get("dest") == "~/.pi/agent/extensions"
    ]


def test_herdr_agent_state_ignore_entry_survives() -> None:
    ignores = _pi_extensions_managed_dir_ignores()
    assert ignores, "managed_dir entry for ~/.pi/agent/extensions is missing"
    assert any("herdr-agent-state.ts" in entry for entry in ignores), (
        "herdr-agent-state.ts is installed by herdr, not dotfiles — without "
        "it in the managed_dir ignore list, install.py --check-links reports "
        "the file as unmanaged and plain installs treat the directory as drift"
    )


def test_extension_links_target_the_pi_harness() -> None:
    data = tomllib.loads(LINKS_TOML.read_text())
    wrong = sorted(
        link["src"]
        for link in data.get("link", [])
        if link.get("src", "").startswith("pi/extensions/")
        and link.get("harness") != "pi"
    )
    assert not wrong, f'pi extension links missing harness = "pi": {wrong}'
