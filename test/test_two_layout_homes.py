#!/usr/bin/env python3
"""Tests for the two-layout sandbox fixture in agent-scripts/test_layouts.py."""

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import test_layouts  # noqa: E402


def _pointer(home: Path) -> str:
    return json.loads((home / test_layouts.POINTER_RELPATH).read_text())["layout"]


def test_local_holds_both_layouts(two_layout_homes):
    local = two_layout_homes.local
    for name in test_layouts.LEGACY_DATA_DIRS:
        assert (local.legacy_data / name).is_dir(), name
    for name in test_layouts.LEGACY_DATA_FILES:
        path = local.legacy_data / name
        assert path.is_file() and path.read_text() == "", name
    for name in test_layouts.TOOLKIT_HOME_DIRS:
        assert (local.toolkit_home / name).is_dir(), name


def test_peer_is_independent_and_legacy_only(two_layout_homes):
    local, peer = two_layout_homes.local.home, two_layout_homes.peer.home
    assert local != peer
    assert not local.is_relative_to(peer) and not peer.is_relative_to(local)
    for name in test_layouts.LEGACY_DATA_ENTRIES:
        assert (two_layout_homes.peer.legacy_data / name).exists(), name
    assert not two_layout_homes.peer.toolkit_home.exists()


def test_peer_pointer_stays_legacy_whatever_initial_is(tmp_path):
    homes = test_layouts.build_two_layout_homes(tmp_path, initial="toolkit-home")
    assert _pointer(homes.local.home) == "toolkit-home"
    assert _pointer(homes.peer.home) == "legacy"


def test_flip_is_seen_by_a_fresh_read_in_the_same_process(two_layout_homes):
    local = two_layout_homes.local
    assert _pointer(local.home) == "legacy"
    local.flip("toolkit-home")
    assert _pointer(local.home) == "toolkit-home"
    local.flip("legacy")
    assert _pointer(local.home) == "legacy"
    assert _pointer(two_layout_homes.peer.home) == "legacy"


def test_custom_pointer_writer_replaces_the_default(tmp_path):
    calls = []
    homes = test_layouts.build_two_layout_homes(
        tmp_path, pointer_writer=lambda home, layout: calls.append((home, layout))
    )
    homes.local.flip("toolkit-home")
    assert calls == [
        (homes.local.home, "legacy"),
        (homes.peer.home, "legacy"),
        (homes.local.home, "toolkit-home"),
    ]
    assert not (homes.local.home / test_layouts.POINTER_RELPATH).exists()


def test_activated_sets_and_restores_home_even_on_error(two_layout_homes):
    before = os.environ["HOME"]
    with two_layout_homes.peer.activated() as home:
        assert os.environ["HOME"] == str(home) == str(two_layout_homes.peer.home)
        assert Path.home() == two_layout_homes.peer.home
    assert os.environ["HOME"] == before
    with pytest.raises(RuntimeError), two_layout_homes.local.activated():
        raise RuntimeError("boom")
    assert os.environ["HOME"] == before


def test_module_touches_no_production_path_constant():
    source = Path(test_layouts.__file__).read_text()
    code = source.split('"""', 2)[2]  # skip the module docstring
    assert not re.search(r"\bDATA_DIR\b", code)
    assert "import dev_status" not in code
