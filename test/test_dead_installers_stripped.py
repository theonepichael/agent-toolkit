#!/usr/bin/env python3
"""Reintroduction guard for the dead package-installer strip.

The package/command/pref installer machinery (brew/apt/dnf batch installs,
uv/ruff/oh-my-posh/nerd-font/neovim pinning, Rectangle prefs, caps-lock
remap) is the origin repo's job — agent-toolkit's copies were an unwired port and
were deleted. This test pins them as gone: any reintroduction of a deleted
symbol, flag, or manifest writer fails here.

Three assertion groups because the deleted things live at different levels:
module-level defs/constants, class attributes, and the CLI string itself
(which no ``hasattr`` check can see).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import install  # noqa: E402

# Module-level defs and constants deleted from install.py.
DEAD_MODULE_SYMBOLS = (
    # dead installers
    "install_mac_packages",
    "install_linux_packages",
    "import_rectangle_prefs",
    "set_caps_lock_to_escape",
    "_install_uv",
    "_install_neovim_fallback",
    "_install_oh_my_posh",
    "_install_nerd_font",
    "_install_ruff_uv_tool",
    # transitively dead helpers
    "_apply_brew_shellenv",
    "_record_package_transaction",
    "_install_linux_packages_one_by_one",
    "_install_distro_packages",
    "_shim",
    "_prepend_path",
    "_fallback_already_pinned",
    "_install_extras",
    "_activate_nvm_node",
    # dead neovim-bootstrap family
    "install_vim_plug",
    "parse_neovim_version",
    "neovim_runtime_ok",
    "_neovim_status",
    "bootstrap_neovim",
    # dead constants
    "NERD_FONT_VERSION",
    "NERD_FONT_URL",
    "NEOVIM_FALLBACK_VERSION",
    "NEOVIM_FALLBACK_ASSETS",
    "BREW_FORMULAE",
    "BREW_CASKS",
    "LINUX_PACKAGES",
    "CAPS_LOCK_TO_ESCAPE",
)


@pytest.mark.parametrize("name", DEAD_MODULE_SYMBOLS)
def test_dead_module_symbols_stay_gone(name: str) -> None:
    assert not hasattr(install, name), f"install.{name} was reintroduced"


def test_options_no_nvim_pin_stay_gone() -> None:
    assert not hasattr(install.Options, "no_nvim_pin")


def test_manifest_record_package_stay_gone() -> None:
    assert not hasattr(install.Manifest, "record_package")


def test_no_nvim_pin_flag_unrecognized() -> None:
    with pytest.raises(SystemExit):
        install.parse_args(["--no-nvim-pin"])
