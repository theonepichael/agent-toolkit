#!/usr/bin/env python3
"""Regenerate the opencode leader-chord table in the parity doc from the installed CLI.

The harness binds leader chords (trust-session on ``<leader>d``, permission-gate on
``<leader>p``). A chord opencode already owns does not fail loudly -- the two bindings
simply fight, and whichever loses stops responding. That is not hypothetical:
``<leader>y`` is core's ``messages_copy``, and a harness command shipped on it while
the whole suite stayed green.

So the table of taken chords is generated from the opencode binary actually installed
here, rather than hand-copied. A hand-copied table is only as fresh as the last person
who remembered to check, and nothing detects the day it stops being true.

This is a *generator*, not a test-time check, and that placement is deliberate. Parsing
a 178 MB compiled binary out of the test suite would be slow and, worse, could fail
*silently*: a parse that finds nothing yields an empty denylist and a guard that passes
everything. Here a parse failure raises, and a stale table fails ``--check`` loudly.

Files read: the opencode binary (via ``PATH``), and ``opencode/CLAUDE_CODE_PARITY.md``.
Files written: ``opencode/CLAUDE_CODE_PARITY.md`` -- only the text between the
``<!-- leader-chords-core:begin -->`` and ``<!-- leader-chords-core:end -->`` anchors.
Everything else in that file is hand-authored and is left byte-for-byte alone.

Standard library only, like every other script in this directory.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# Reads the opencode binary and rewrites one region of a tracked doc. Touches no
# toolkit data path -- no ~/.agent-toolkit/data, no ~/.local/state/agent-toolkit.
TOOLKIT_DATA = "none"

REPO_ROOT = Path(__file__).resolve().parent.parent
PARITY_DOC = REPO_ROOT / "opencode" / "CLAUDE_CODE_PARITY.md"

BEGIN_ANCHOR = "<!-- leader-chords-core:begin -->"
END_ANCHOR = "<!-- leader-chords-core:end -->"

# The keybind table is minified into the binary as a map of
# ``name:g("comma,separated,keys","description")``, where ``g`` builds
# ``{default, description}``. ``leader:g(`` is the first entry in that object, so it
# doubles as the anchor to brace-match from.
LEDGER_ANCHOR = b"leader:g("
ENTRY_RE = re.compile(r'([A-Za-z_][\w.]*):g\("([^"]*)"')
LEADER_PREFIX = "<leader>"

# Floors, so a parse that silently degrades raises instead of writing a table that
# looks authoritative and is wrong. A vacuous generator is worse than no generator:
# it would overwrite a good hand-curated table with an empty one and report success.
MIN_KEYBINDS_PARSED = 50
MIN_LEADER_CHORDS = 10


class KeybindExtractionError(RuntimeError):
    """The opencode keybind table could not be read with confidence."""


def opencode_binary() -> Path | None:
    """Path to the installed opencode binary, or None if it is not on PATH."""
    found = shutil.which("opencode")
    return Path(found) if found else None


def opencode_version(binary: Path) -> str:
    """The binary's reported version, for the doc's provenance line."""
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeybindExtractionError(
            f"could not run {binary} --version: {exc}"
        ) from exc
    line = result.stdout.strip().splitlines()
    return line[0].strip() if line else "unknown"


def _enclosing_object(data: bytes, marker_index: int) -> str:
    """The ``{...}`` object literal containing ``marker_index``, as text.

    Brace-matched rather than windowed. An earlier hand-extraction of this same
    table used a fixed-size character window and truncated it, which is how a
    fabricated "the binary and the docs disagree" conclusion got written down.
    Match the structure instead of guessing a size.
    """
    start = data.rfind(b"{", 0, marker_index)
    if start < 0:
        raise KeybindExtractionError("no opening brace before the keybind table marker")
    depth = 0
    for index in range(start, len(data)):
        char = data[index : index + 1]
        if char == b"{":
            depth += 1
        elif char == b"}":
            depth -= 1
            if depth == 0:
                return data[start : index + 1].decode("utf-8", "replace")
    raise KeybindExtractionError("unbalanced braces around the keybind table")


def leader_chords(binary: Path) -> dict[str, list[str]]:
    """Map each leader-chord token to the keybind names that own it.

    Tokens are bare (``d``, not ``<leader>d``) on both sides, so the caller can
    compare against a binding's suffix without a format mismatch silently making
    every comparison false.
    """
    data = binary.read_bytes()
    marker = data.find(LEDGER_ANCHOR)
    if marker < 0:
        raise KeybindExtractionError(
            f"{LEDGER_ANCHOR.decode()!r} not found in {binary}; the keybind table's "
            "shape changed, so the leader chords cannot be read with confidence"
        )
    table = _enclosing_object(data, marker)
    entries = ENTRY_RE.findall(table)
    if len(entries) < MIN_KEYBINDS_PARSED:
        raise KeybindExtractionError(
            f"parsed only {len(entries)} keybinds from {binary} "
            f"(floor {MIN_KEYBINDS_PARSED}); refusing to write a table that may be "
            "truncated"
        )

    owners: dict[str, set[str]] = defaultdict(set)
    for name, keys in entries:
        for part in keys.split(","):
            part = part.strip()
            if not part.startswith(LEADER_PREFIX):
                continue
            token = part[len(LEADER_PREFIX) :].strip()
            if token:
                owners[token].add(name)
    if len(owners) < MIN_LEADER_CHORDS:
        raise KeybindExtractionError(
            f"found only {len(owners)} leader chords in {binary} "
            f"(floor {MIN_LEADER_CHORDS}); refusing to write a table that may be "
            "truncated"
        )
    return {token: sorted(names) for token, names in sorted(owners.items())}


def render_block(chords: dict[str, list[str]]) -> str:
    """The markdown table body, without the surrounding anchors."""
    lines = ["| Chord | Core keybind |", "|---|---|"]
    for token, names in chords.items():
        joined = ", ".join(f"`{name}`" for name in names)
        lines.append(f"| `{LEADER_PREFIX}{token}` | {joined} |")
    return "\n".join(lines) + "\n"


def current_block(text: str) -> str:
    """The block currently committed between the anchors."""
    begin = text.find(BEGIN_ANCHOR)
    end = text.find(END_ANCHOR)
    if begin < 0 or end < 0:
        raise KeybindExtractionError(
            f"{PARITY_DOC} must contain {BEGIN_ANCHOR} and {END_ANCHOR}; the "
            "generator only edits the region between them"
        )
    if begin > end:
        raise KeybindExtractionError(f"{BEGIN_ANCHOR} appears after {END_ANCHOR}")
    return text[begin + len(BEGIN_ANCHOR) : end].strip("\n")


def splice_block(text: str, body: str) -> str:
    """Return ``text`` with the anchored region replaced by ``body``."""
    begin = text.find(BEGIN_ANCHOR)
    end = text.find(END_ANCHOR)
    return (
        text[: begin + len(BEGIN_ANCHOR)]
        + "\n\n"
        + body.rstrip("\n")
        + "\n\n"
        + text[end:]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed table is stale, without writing",
    )
    args = parser.parse_args()

    binary = opencode_binary()
    if binary is None:
        # Skipping is right here and wrong in the collision guard. This check exists
        # to catch an opencode upgrade; a machine without opencode cannot have been
        # affected by one, and failing there would just produce noise nobody acts on.
        print("opencode is not on PATH; skipping the leader-chord freshness check")
        return 0

    try:
        chords = leader_chords(binary)
        version = opencode_version(binary)
    except KeybindExtractionError as exc:
        print(f"gen_keybinds: {exc}", file=sys.stderr)
        return 2

    body = render_block(chords)
    existing = PARITY_DOC.read_text(encoding="utf-8")

    if args.check:
        try:
            committed = current_block(existing)
        except KeybindExtractionError as exc:
            print(f"gen_keybinds: {exc}", file=sys.stderr)
            return 1
        if committed.strip() == body.strip():
            print(
                f"gen_keybinds: leader-chord table is up to date "
                f"({len(chords)} chords, opencode {version})"
            )
            return 0
        print(
            f"gen_keybinds: leader-chord table is STALE against opencode {version}.\n"
            f"  committed: {sorted(_tokens(committed))}\n"
            f"  installed: {sorted(chords)}\n"
            f"Regenerate with: python3 agent-scripts/gen_keybinds.py\n"
            f"Then update CORE_LETTERS in test/test_opencode_trust_wiring.py to match, "
            f"and check whether a harness chord is among the new entries.",
            file=sys.stderr,
        )
        return 1

    PARITY_DOC.write_text(splice_block(existing, body), encoding="utf-8")
    print(
        f"gen_keybinds: wrote {len(chords)} leader chords to "
        f"{PARITY_DOC.relative_to(REPO_ROOT)} (opencode {version})"
    )
    return 0


def _tokens(block: str) -> set[str]:
    """Bare tokens named by a committed block, for the staleness message."""
    found = set()
    for match in re.finditer(rf"`{re.escape(LEADER_PREFIX)}([^`]+)`", block):
        found.add(match.group(1).strip())
    return found


if __name__ == "__main__":
    raise SystemExit(main())
