#!/usr/bin/env python3
"""Tests for toolkit_checkout.toolkit_checkout(): locating the checkout.

The checkout can live flat (``~/Workspace/agent-toolkit``) or nested
(``~/Workspace/agent-toolkit/agent-toolkit``, worktrees as siblings inside the
outer directory). In the nested layout the flat path still exists -- it is the
container -- so an existence check alone would accept the wrong directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

from toolkit_checkout import (  # noqa: E402
    CheckoutNotFoundError,
    is_toolkit_checkout,
    toolkit_checkout,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "agent-scripts" / "toolkit_checkout.py"


def make_checkout(path: Path) -> Path:
    """Create the minimal marker set that identifies a toolkit checkout."""
    (path / "agent-scripts").mkdir(parents=True)
    (path / "install.py").write_text("")
    (path / "links.toml").write_text("")
    return path


def script_in(checkout: Path) -> Path:
    """Where toolkit_checkout.py would sit inside ``checkout``."""
    return checkout / "agent-scripts" / "toolkit_checkout.py"


def test_is_toolkit_checkout_requires_every_marker(tmp_path: Path) -> None:
    full = make_checkout(tmp_path / "full")
    assert is_toolkit_checkout(full)
    for missing in ("install.py", "links.toml"):
        partial = make_checkout(tmp_path / f"no-{missing}")
        (partial / missing).unlink()
        assert not is_toolkit_checkout(partial)
    no_scripts = make_checkout(tmp_path / "no-scripts")
    (no_scripts / "agent-scripts").rmdir()
    (no_scripts / "agent-scripts").write_text("")  # a file, not a directory
    assert not is_toolkit_checkout(no_scripts)
    assert not is_toolkit_checkout(tmp_path / "absent")


def test_self_location_wins(tmp_path: Path) -> None:
    home = tmp_path / "home"
    checkout = make_checkout(tmp_path / "anywhere" / "agent-toolkit")
    make_checkout(home / "Workspace" / "agent-toolkit" / "agent-toolkit")
    got = toolkit_checkout(env={}, home=home, self_path=script_in(checkout))
    assert got == checkout.resolve()


def test_worktree_self_location_beats_env(tmp_path: Path) -> None:
    main = make_checkout(tmp_path / "agent-toolkit" / "agent-toolkit")
    worktree = make_checkout(tmp_path / "agent-toolkit" / "agent-toolkit-wt")
    got = toolkit_checkout(
        env={"AGENT_TOOLKIT_PATH": str(main)},
        home=tmp_path / "home",
        self_path=script_in(worktree),
    )
    assert got == worktree.resolve()


def test_self_location_follows_install_symlink(tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "src" / "agent-toolkit")
    script = script_in(checkout)
    script.write_text("")
    installed = tmp_path / "home" / ".agent-toolkit" / "scripts"
    installed.mkdir(parents=True)
    (installed / script.name).symlink_to(script)
    got = toolkit_checkout(
        env={}, home=tmp_path / "home", self_path=installed / script.name
    )
    assert got == checkout.resolve()


def test_copy_install_falls_back_to_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    copied = home / ".agent-toolkit" / "scripts" / "toolkit_checkout.py"
    copied.parent.mkdir(parents=True)
    copied.write_text("")
    checkout = make_checkout(tmp_path / "elsewhere")
    got = toolkit_checkout(
        env={"AGENT_TOOLKIT_PATH": str(checkout)}, home=home, self_path=copied
    )
    assert got == checkout.resolve()


def test_env_accepts_tilde_and_trailing_slash(tmp_path: Path) -> None:
    home = tmp_path / "home"
    checkout = make_checkout(home / "src" / "atk")
    got = toolkit_checkout(
        env={"AGENT_TOOLKIT_PATH": "~/src/atk/", "HOME": str(home)},
        home=home,
        self_path=tmp_path / "nowhere" / "x.py",
    )
    assert got == checkout.resolve()


def test_invalid_env_raises_instead_of_falling_through(tmp_path: Path) -> None:
    home = tmp_path / "home"
    make_checkout(home / "Workspace" / "agent-toolkit" / "agent-toolkit")
    with pytest.raises(CheckoutNotFoundError, match="AGENT_TOOLKIT_PATH"):
        toolkit_checkout(
            env={"AGENT_TOOLKIT_PATH": str(tmp_path / "not-a-checkout")},
            home=home,
            self_path=tmp_path / "nowhere" / "x.py",
        )


def test_nested_layout_skips_the_container(tmp_path: Path) -> None:
    home = tmp_path / "home"
    container = home / "Workspace" / "agent-toolkit"
    nested = make_checkout(container / "agent-toolkit")
    make_checkout(container / "agent-toolkit-some-branch")  # a sibling worktree
    got = toolkit_checkout(env={}, home=home, self_path=tmp_path / "x" / "y.py")
    assert got == nested.resolve()


def test_flat_layout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    flat = make_checkout(home / "Workspace" / "agent-toolkit")
    got = toolkit_checkout(env={}, home=home, self_path=tmp_path / "x" / "y.py")
    assert got == flat.resolve()


def test_nested_wins_over_flat(tmp_path: Path) -> None:
    home = tmp_path / "home"
    flat = make_checkout(home / "Workspace" / "agent-toolkit")
    nested = make_checkout(flat / "agent-toolkit")
    got = toolkit_checkout(env={}, home=home, self_path=tmp_path / "x" / "y.py")
    assert got == nested.resolve()


def test_container_alone_is_never_returned(tmp_path: Path) -> None:
    home = tmp_path / "home"
    container = home / "Workspace" / "agent-toolkit"
    (container / "agent-toolkit-wt").mkdir(parents=True)
    with pytest.raises(CheckoutNotFoundError) as exc:
        toolkit_checkout(env={}, home=home, self_path=tmp_path / "x" / "y.py")
    message = str(exc.value)
    assert str(container) in message
    assert str(container / "agent-toolkit") in message


# Runs this checkout's own toolkit_checkout.py with sys.executable; it only
# stats paths and prints, so a real interpreter is safe.
@pytest.mark.allow_real_subprocess
def test_cli_prints_the_checkout() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "checkout"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(REPO_ROOT)


# A copy of the module under tmp_path with a throwaway HOME: nothing real is read.
@pytest.mark.allow_real_subprocess
def test_cli_reports_failure_on_stderr(tmp_path: Path) -> None:
    copied = tmp_path / "toolkit_checkout.py"
    copied.write_text(SCRIPT.read_text())
    result = subprocess.run(
        [sys.executable, str(copied), "checkout"],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "agent-toolkit" in result.stderr

