#!/usr/bin/env python3
"""Tests for the supported opencode trust control CLI."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "agent-scripts/opencode_trust.py"
SPEC = importlib.util.spec_from_file_location("opencode_trust", SCRIPT)
assert SPEC and SPEC.loader
opencode_trust = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(opencode_trust)


def test_state_is_session_scoped_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert opencode_trust.read_state("missing") == {"trusted": False}
    opencode_trust.write_state("one", True)
    opencode_trust.write_state("two", False)
    assert opencode_trust.read_state("one")["trusted"] is True
    assert opencode_trust.read_state("two")["trusted"] is False


def test_state_filename_encodes_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    opencode_trust.write_state("../outside", True)
    path = opencode_trust.state_path("../outside")
    assert path.parent == tmp_path / opencode_trust.STATE_RELATIVE
    assert "/" not in path.name
    assert opencode_trust.read_state("../outside")["trusted"] is True
