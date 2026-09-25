#!/usr/bin/env python3
"""Wiring checks for the supported opencode trust plugin and CLI."""

import re
import tomllib
from pathlib import Path

import pytest

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


def test_tui_plugins_declare_palette_commands() -> None:
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


# ── capability invariant: a slashName must be dispatchable ───────────────────
#
# A TUI plugin's `slashName` only adds a completion-overlay entry and a ctrl+p
# palette row. It is NOT a typed slash command: those are opencode/command/*.md,
# and none exists for these toggles. So a plugin that advertises a slashName and
# binds nothing is a feature the user cannot reliably reach -- typing the name
# submits the text to the model instead. Requiring a real keybinding is what
# makes the advertised invocation path exist.

TUI_PLUGIN_DIR = REPO_ROOT / "opencode" / "tui"

# Leader chords already claimed by opencode, as the UNION of two sources that
# disagree: the installed binary's keybind-defaults table, and the chord list in
# opencode/CLAUDE_CODE_PARITY.md section 4. Reading either alone gives a wrong
# answer -- the binary does not bind <leader>u, while the doc records it as
# messages_undo. Unresolved which source is current, so take the union.
TAKEN_LEADER_CHORDS = frozenset("abceglmnqrstux") | frozenset(f"{n}" for n in range(1, 10))


def _balanced(text: str, opener: str, closer: str, start: int) -> str:
    """Return the substring of the bracket pair beginning at `start`."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opener:
            depth += 1
        elif text[index] == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced {opener}{closer} starting at offset {start}")


def _array_after(source: str, key: str) -> str:
    match = re.search(rf"\b{key}\s*:\s*\[", source)
    assert match is not None, f"no {key}: [ array found"
    return _balanced(source, "[", "]", match.end() - 1)


def _register_layer(source: str) -> str:
    match = re.search(r"\bregisterLayer\s*\(\s*\{", source)
    assert match is not None, "no keymap.registerLayer call found"
    return _balanced(source, "{", "}", match.end() - 1)


def _slash_name_commands(layer: str) -> list[str]:
    """Names of commands in this layer that advertise a slashName."""
    names = []
    for entry in re.findall(r"\{[^{}]*\}", _array_after(layer, "commands")):
        if "slashName:" not in entry:
            continue
        name = re.search(r'\bname\s*:\s*"([^"]+)"', entry)
        assert name is not None, f"slashName command without a name: {entry!r}"
        names.append(name.group(1))
    return names


def _binding_cmds(layer: str) -> list[str]:
    """Command names targeted by this layer's bindings."""
    return [
        match.group(1)
        for match in re.finditer(
            r'\bcmd\s*:\s*"([^"]+)"', _array_after(layer, "bindings")
        )
    ]


def _binding_keys(layer: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(
            r'\bkey\s*:\s*"([^"]+)"', _array_after(layer, "bindings")
        )
    ]


def _tui_plugin_files() -> list[Path]:
    files = sorted(TUI_PLUGIN_DIR.glob("*.ts"))
    # Floor, so a rename or an emptied directory cannot make the invariant below
    # pass by finding nothing. The glob is kept as well, so a newly added plugin
    # is covered without editing this test.
    assert {path.name for path in files} >= {
        "trust-session.ts",
        "permission-gate.ts",
    }, f"opencode/tui/ no longer holds the known plugins; found {[p.name for p in files]}"
    return files


@pytest.mark.regression(
    "tui-slashname-without-keybinding",
    "permission-gate.ts: command 'permission-gate.toggle' advertises a slashName but "
    "no binding in its layer dispatches it; bound commands are none. Add a binding "
    "whose cmd is that command's name, or drop the slashName.",
)
def test_every_tui_slash_command_has_a_keybinding() -> None:
    """A slashName is only reachable if some binding dispatches it.

    `bindings` is layer-level, not per-command, so a bare "the bindings array is
    non-empty" check would go green the moment a second unbound slashName command
    joined the same layer -- reproducing this exact bug. Pair them instead.
    """
    for path in _tui_plugin_files():
        layer = _register_layer(path.read_text())
        bound = set(_binding_cmds(layer))
        for name in _slash_name_commands(layer):
            assert name in bound, (
                f"{path.name}: command {name!r} advertises a slashName but no binding "
                f"in its layer dispatches it; bound commands are {sorted(bound) or 'none'}. "
                "Add a binding whose cmd is that command's name, or drop the slashName."
            )


def test_tui_bindings_use_unclaimed_leader_chords() -> None:
    """No plugin may take a leader chord opencode already owns.

    This catches our own mistakes -- picking a taken letter, or drifting the
    denylist. It CANNOT catch a future opencode release that claims one of these
    chords: no supported surface exposes the live default keymap (the SDK does
    not export it and there is no CLI dump), so upstream drift is left to the
    manual live check.
    """
    for path in _tui_plugin_files():
        for key in _binding_keys(_register_layer(path.read_text())):
            if not key.startswith("<leader>"):
                continue
            suffix = key.removeprefix("<leader>")
            assert suffix not in TAKEN_LEADER_CHORDS, (
                f"{path.name}: binds {key!r}, but {suffix!r} is already claimed by "
                "opencode or recorded as claimed in opencode/CLAUDE_CODE_PARITY.md "
                f"section 4. Taken: {''.join(sorted(TAKEN_LEADER_CHORDS))}"
            )


def test_bound_chords_are_documented_exactly_once() -> None:
    """Each chord a plugin binds must get exactly one row in the parity doc's table.

    Kept to table rows rather than counting the string across the whole file: a
    chord is *expected* to be mentioned in prose as well, and a count of every
    mention would fail on good documentation. What must not happen is the chord
    appearing in the keybind table zero times (undocumented) or twice (ambiguous
    about what it does).
    """
    parity = (REPO_ROOT / "opencode" / "CLAUDE_CODE_PARITY.md").read_text()
    rows = [line for line in parity.splitlines() if line.lstrip().startswith("|")]
    checked = 0
    for path in _tui_plugin_files():
        for key in _binding_keys(_register_layer(path.read_text())):
            checked += 1
            pattern = re.compile(rf"^\|\s*`?{re.escape(key)}`?\s*\|")
            entries = [row for row in rows if pattern.match(row.lstrip())]
            assert len(entries) == 1, (
                f"{path.name}: binds {key!r} but opencode/CLAUDE_CODE_PARITY.md has "
                f"{len(entries)} keybind-table row(s) for it; expected exactly 1. "
                f"Matching rows: {entries}"
            )
    # Floor. Without it this test passes vacuously the moment a plugin loses its
    # bindings -- there is nothing left to check, so the loop body never runs and
    # it reports green while verifying nothing. That is the same trap as an empty
    # glob, one level down.
    assert checked > 0, (
        "no keybindings found in opencode/tui/*.ts, so doc/code chord consistency "
        "was not checked at all"
    )
