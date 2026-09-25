#!/usr/bin/env python3
"""Wiring checks for the supported opencode trust plugin and CLI."""

import re
import shutil
import subprocess
import sys
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

# ── leader-chord denylist, derived from the parity doc ───────────────────────
#
# A harness plugin must not bind a leader chord opencode already owns: the chord
# silently stops doing what the user expects. That is not hypothetical -- a
# hand-typed denylist here shipped trust-session on <leader>y, which core binds
# to messages_copy, with the whole suite green.
#
# So the denylist is PARSED from a single complete table in
# opencode/CLAUDE_CODE_PARITY.md, not hand-typed. The literals below are
# verification snapshots, not the source: they exist to catch a table that was
# wrongly edited, which a bare "is it non-empty" check cannot.
#
# Provenance: opencode.ai/docs/keybinds, cross-checked against the installed
# binary's keybind Definitions object (162 keybinds). The two agree exactly.
#
# KNOWN LIMIT: nothing here re-reads opencode's real defaults, so a future
# opencode release that claims a new leader letter is invisible to CI. The doc
# carries a refresh note for that; closing it needs a test that reads the
# installed binary, which is out of scope here.

# Verification snapshot of the upstream letter set. NOT the source: the guard's
# denylist is _core_chords(), parsed from the doc.
CORE_LETTERS = frozenset("abceghlmnqrstuxy")

_PARITY = REPO_ROOT / "opencode" / "CLAUDE_CODE_PARITY.md"


def _anchored_block(anchor: str) -> str:
    """Return the body of one <!-- anchor:begin/end --> block in the parity doc.

    Presence is not enough: a duplicated or inverted pair would let the regex
    silently select an unintended span, which is the same class of silent-green
    failure as an empty denylist. So require exactly one begin, exactly one end,
    and begin before end.
    """
    doc = _PARITY.read_text()
    begin, end = f"<!-- {anchor}:begin -->", f"<!-- {anchor}:end -->"
    if doc.count(begin) != 1 or doc.count(end) != 1:
        raise AssertionError(
            f"opencode/CLAUDE_CODE_PARITY.md must contain exactly one {begin} and one "
            f"{end} pair for the {anchor!r} block; found {doc.count(begin)} and "
            f"{doc.count(end)}. The denylist is derived from that block, so a missing "
            f"or duplicated anchor silently changes which chords are considered taken."
        )
    start, stop = doc.index(begin), doc.index(end)
    if start > stop:
        raise AssertionError(
            f"opencode/CLAUDE_CODE_PARITY.md has {end} before {begin} for the "
            f"{anchor!r} block; begin must precede end."
        )
    return doc[start + len(begin) : stop]


def _cells(row: str) -> list[str]:
    """Split a markdown table row into stripped, unbackticked cells."""
    return [cell.strip().strip("`") for cell in row.strip().strip("|").split("|")]


def _chord_token(cell: str) -> str | None:
    """Normalise a first-cell chord to its BARE token ('<leader>y' -> 'y').

    Both sides of every comparison must be bare tokens. Storing '<leader>y' here
    while the binding under test normalises to 'y' would make every membership
    test false and the guard would pass for every chord -- green and broken.
    """
    if not cell.startswith("<leader>"):
        return None
    return cell.removeprefix("<leader>").strip()


def _core_chords() -> dict[str, set[str]]:
    """Bare chord token -> core command names that own it, from the doc table."""
    mapping: dict[str, set[str]] = {}
    for row in _anchored_block("leader-chords-core").splitlines():
        if not row.strip().startswith("|"):
            continue
        cells = _cells(row)
        token = _chord_token(cells[0])
        if token is None or set(token) <= set("-: "):
            continue
        assert token, f"core chord row has an empty token: {row!r}"
        assert " " not in token and "\u2026" not in token, (
            f"core chord token {token!r} is not a single bare token. A compressed "
            f"row like `<leader>1`\u2026`<leader>9` would capture a garbage token; the "
            f"generator emits one row per chord, so emit one row here too."
        )
        assert len(cells) > 1 and cells[1], f"core chord row has no command: {row!r}"
        mapping.setdefault(token, set()).add(cells[1])
    assert mapping, "the core chord block yielded no chords at all"
    return mapping


def _core_chord_rows() -> list[tuple[str, str]]:
    """Every (bare token, command) row in the core block, in document order.

    Kept as a list, not a dict, so a duplicated chord can be detected -- a dict
    would silently collapse the duplicate and leave the derived set unchanged, so
    the set-equality snapshot would not notice it.
    """
    rows: list[tuple[str, str]] = []
    for row in _anchored_block("leader-chords-core").splitlines():
        if not row.strip().startswith("|"):
            continue
        cells = _cells(row)
        token = _chord_token(cells[0])
        if token is None or set(token) <= set("-: "):
            continue
        rows.append((token, cells[1] if len(cells) > 1 else ""))
    return rows


def _core_chord_tokens() -> frozenset[str]:
    return frozenset(token for token, _ in _core_chord_rows())


def _taken_chords() -> frozenset[str]:
    """Every leader chord the table records as taken, letters and non-letters alike."""
    return _core_chord_tokens()


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


def test_core_chord_table_matches_verified_snapshot() -> None:
    """The parsed core table must match the upstream snapshot, letter for letter.

    A non-emptiness floor is not enough: deleting 15 of 16 rows still leaves a
    non-empty set, the guard still passes, and everything is green -- which is
    exactly how a chord collision shipped before. Set-equality is what detects a
    table that was wrongly edited.
    """
    rows = _core_chord_rows()
    tokens = _core_chord_tokens()
    assert len(tokens) >= 16, (
        f"core chord table yielded only {len(tokens)} chords ({''.join(sorted(tokens))}); "
        "expected at least 16 letters"
    )
    letters = frozenset(t for t in tokens if len(t) == 1 and t.isalpha())
    assert letters == CORE_LETTERS, (
        f"core chord letters {''.join(sorted(letters))} do not match the verified "
        f"snapshot {''.join(sorted(CORE_LETTERS))}. If opencode genuinely changed, "
        f"update CORE_LETTERS and the doc table together and note the refresh."
    )


def test_core_chord_table_has_no_duplicate_chords() -> None:
    """A duplicated chord row would hide behind the set-equality snapshot.

    The derived denylist is a set, so a second row for the same chord collapses
    silently and the letter set still matches the snapshot. It would also make
    the chord-to-command diagnostic misleading about what is actually bound.
    """
    seen: dict[str, int] = {}
    for token, _ in _core_chord_rows():
        seen[token] = seen.get(token, 0) + 1
    duplicated = {t: n for t, n in seen.items() if n > 1}
    assert not duplicated, f"core chord table has duplicate rows for {duplicated}"


@pytest.mark.regression(
    "harness-chord-collides-with-core-chord",
    "trust-session.ts: binds '<leader>y', but opencode already owns that leader "
    "chord: messages_copy. Pick a free letter or move the core command in tui.json "
    "instead of shadowing it.",
)
def test_tui_bindings_use_unclaimed_leader_chords() -> None:
    """No plugin may take a leader chord opencode already owns.

    Carries the regression marker because this is the assertion that failed when
    trust-session shipped on <leader>y, colliding with core messages_copy. The
    failure names the core command via the table's second column, so the report
    says what was hijacked rather than just a bare letter.
    """
    core = _core_chords()
    taken = _taken_chords()
    for path in _tui_plugin_files():
        for key in _binding_keys(_register_layer(path.read_text())):
            if not key.startswith("<leader>"):
                continue
            token = _chord_token(key)
            assert token is not None, f"unnormalizable binding key {key!r}"
            assert token not in taken, (
                f"{path.name}: binds {key!r}, but opencode already owns that leader "
                f"chord: {', '.join(sorted(core.get(token, {'a non-letter chord'})))}. "
                f"Pick a free letter or move the core command in tui.json instead of "
                f"shadowing it."
            )


def test_documented_harness_chords_are_claimable() -> None:
    """Neither doc table may advertise a chord the core already owns.

    Catches a stale chord surviving an edit: the parity doc mentions harness
    chords in three places (section 4 prose, the section 4 table's first cell, and
    the section 8 cheatsheet's SECOND cell), and only the first-cell form is
    visible to a first-cell matcher. A leftover <leader>y here would advertise a
    binding the plugin no longer has, over a chord core owns.
    """
    core = _core_chords()
    for anchor in ("leader-chords-harness", "cheatsheet-chords"):
        for row in _anchored_block(anchor).splitlines():
            if not row.strip().startswith("|"):
                continue
            for cell in _cells(row):
                token = _chord_token(cell)
                if token is None or set(token) <= set("-: "):
                    continue
                assert token not in core, (
                    f"opencode/CLAUDE_CODE_PARITY.md {anchor!r} block advertises "
                    f"<leader>{token}> in row {row.strip()!r}, but opencode already "
                    f"owns that chord: {', '.join(sorted(core[token]))}"
                )


def test_bound_chords_are_documented_exactly_once() -> None:
    """Each chord a plugin binds must get exactly one row in the harness table.

    Scoped to the harness block rather than every `|` line in the file: the core
    table has the same first-cell shape, and scanning the whole document would let
    a core row satisfy a harness-chord lookup -- or vice versa.
    """
    rows = [row for row in _anchored_block("leader-chords-harness").splitlines() if row.strip().startswith("|")]
    checked = 0
    for path in _tui_plugin_files():
        for key in _binding_keys(_register_layer(path.read_text())):
            checked += 1
            pattern = re.compile(rf"^\|\s*`?{re.escape(key)}`?\s*\|")
            entries = [row for row in rows if pattern.match(row.strip())]
            assert len(entries) == 1, (
                f"{path.name}: binds {key!r} but the harness chord table has "
                f"{len(entries)} row(s) for it; expected exactly 1. Rows: {entries}"
            )
    # Floor. Without it this test passes vacuously the moment a plugin loses its
    # bindings -- nothing left to check, so the loop body never runs and it reports
    # green while verifying nothing.
    assert checked > 0, (
        "no keybindings found in opencode/tui/*.ts, so doc/code chord consistency "
        "was not checked at all"
    )


@pytest.mark.allow_real_subprocess  # runs the generator, which reads the opencode binary
def test_leader_chord_table_is_not_stale() -> None:
    """The committed core-chord table must match the installed opencode.

    The collision guard derives its denylist from the parity doc, so a table that
    has fallen behind the installed CLI would leave a harness chord able to
    shadow a built-in with every other test still green. That is exactly how
    <leader>y shipped colliding with messages_copy.

    The table is generated by agent-scripts/gen_keybinds.py, which extracts it
    from the opencode binary actually installed here. The generator is
    deliberately not run inside the collision guard: a parse that silently
    degrades would yield an empty denylist and a guard that passes everything.
    Here it raises, and staleness is a plain non-zero exit.

    Skips when opencode is not on PATH -- a machine without it cannot have been
    affected by an opencode upgrade -- matching the SDK-pin check's precedent.
    """
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI not installed; chord-table freshness not enforced")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "agent-scripts" / "gen_keybinds.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"gen_keybinds.py --check exited {result.returncode}; the committed "
        f"leader-chord table no longer matches the installed opencode.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n"
        f"Regenerate with: python3 agent-scripts/gen_keybinds.py"
    )
