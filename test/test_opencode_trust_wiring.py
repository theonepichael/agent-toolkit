#!/usr/bin/env python3
"""Wiring checks for the supported opencode trust plugin and CLI."""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINKS = tomllib.loads((REPO_ROOT / "links.toml").read_text())["link"]


def _source(path: str) -> str:
    return (REPO_ROOT / path).read_text()


def test_opencode_trust_files_are_installed() -> None:
    installed = {
        (link["src"], link["dest"])
        for link in LINKS
        if link.get("harness") == "opencode"
    }
    for source, dest in (
        (
            "opencode/plugins/trust-session.ts",
            "~/.config/opencode/plugins/trust-session.ts",
        ),
        ("opencode/tui/permission-gate.ts", "~/.config/opencode/tui/permission-gate.ts"),
        ("opencode/tui/trust-session.ts", "~/.config/opencode/tui/trust-session.ts"),
        ("opencode/trust-state.ts", "~/.config/opencode/trust-state.ts"),
        (
            "agent-scripts/opencode_trust.py",
            "~/.agent-toolkit/scripts/opencode_trust.py",
        ),
    ):
        assert (source, dest) in installed


def test_state_path_has_one_definition() -> None:
    state = _source("opencode/trust-state.ts")
    assert ".local/state/agent-toolkit/trust-sessions" in state
    for path in (
        "opencode/plugins/trust-session.ts",
        "opencode/tui/permission-gate.ts",
        "opencode/tui/trust-session.ts",
        "opencode/plugin/guard-rails.ts",
    ):
        assert ".local/state/agent-toolkit/trust-sessions" not in _source(path)


def test_permission_plugin_only_handles_permission_events() -> None:
    source = _source("opencode/plugins/trust-session.ts")
    assert 'permissionEvent.type !== "permission.asked"' in source
    assert "postSessionIdPermissionsPermissionId" in source
    assert 'response: "once"' in source
    assert "question" not in source


def test_tui_plugins_register_slash_commands() -> None:
    permission = _source("opencode/tui/permission-gate.ts")
    trust = _source("opencode/tui/trust-session.ts")
    tui_config = __import__("json").loads(_source("opencode/tui.json"))
    assert 'slashName: "permission-gate"' in permission
    assert 'slashName: "trust-session"' in trust
    assert "./tui/permission-gate.ts" in tui_config["plugin"]
    assert "./tui/trust-session.ts" in tui_config["plugin"]
    assert "writeTrustState" in permission
    assert "writeTrustState" in trust


def test_server_guard_uses_session_scoped_trust_state() -> None:
    source = _source("opencode/plugin/guard-rails.ts")
    assert "readTrustState(input.sessionID)" in source
    assert "input: { tool?: string; sessionID?: string }" in source
