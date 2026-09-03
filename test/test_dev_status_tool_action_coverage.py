"""Coverage guard: pi's dev-status-tool ACTIONS vs dev_status.py's real CLI.

Adding the ``ready`` subcommand to ``claude/scripts/dev_status.py`` left
``pi/extensions/dev-status-tool.ts`` without it and nothing failed — bun
test stayed green with an action no pi session could reach, while the
contract-fingerprint check pointed at 21 skill docs, none of which
enumerates subcommands. This module closes that gap: ACTIONS must cover
every leaf subcommand dev_status.py actually defines, and stale entries
(in ACTIONS but no longer a CLI leaf) fail too.

The CLI surface comes from gen_interfaces.py's existing AST parser
(``extract_cli`` + ``leaf_subcommand_paths``), imported — never a second
parser, which would drift exactly the way this guard exists to catch.
The TS side is read as text with a small scanner; no bun/node toolchain
is involved.

MAPPING RULE (encoded once in :func:`mapped_action_name`, never per
case): a leaf subcommand path maps to the tool action name by joining
its parts with ``_`` after replacing every ``-`` with ``_`` —
``("pending", "add")`` -> ``pending_add``,
``("out-of-scope", "link")`` -> ``out_of_scope_link``,
``("gate-set",)`` -> ``gate_set``, ``("ready",)`` -> ``ready``.
The rule is total and IS the rename policy: a leaf that legitimately
needs a different action name means the rule itself gets revised, never
a per-case exception. ``ALLOWED_UNEXPOSED`` covers total omissions only
(a leaf deliberately absent from ACTIONS, on record with a reason); a
custom TS name for a real leaf is stale-flagged like any other entry —
a rename cannot be smuggled through the allowlist.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "claude" / "scripts"))

import gen_interfaces as gi  # noqa: E402  (needs the sys.path insert above)

DEV_STATUS_PATH = REPO_ROOT / "claude" / "scripts" / "dev_status.py"
TOOL_PATH = REPO_ROOT / "pi" / "extensions" / "dev-status-tool.ts"

#: Leaf subcommands intentionally not exposed as tool actions, keyed by
#: the **mapped action name**. Empty today — the structure exists so
#: "not exposed" is a decision on record with a reason, never an omission
#: indistinguishable from the silent-drift bug this guard exists to catch.
ALLOWED_UNEXPOSED: dict[str, str] = {}

_IDENTIFIER_RE = re.compile(r"[a-z0-9_]+")


def mapped_action_name(path: tuple[str, ...]) -> str:
    """Apply the MAPPING RULE: join path parts with ``_``, hyphens first."""
    return "_".join(segment.replace("-", "_") for segment in path)


# ── TS extraction ────────────────────────────────────────────────────────────


def extract_actions_from_ts_source(text: str) -> list[str]:
    """Extract the string literals of the ``const ACTIONS = [...]`` array.

    A small state machine, not a bracket count: double-quoted string
    literals (the only quoting style in dev-status-tool.ts) and ``//`` /
    ``/* */`` comments are consumed without interpreting any bracket
    inside them, so a ``]`` in either can never end the array early.

    Raises ``ValueError`` when the declaration is missing — a silent
    ``[]`` would be indistinguishable from the drift this guard catches.
    """
    declaration = "const ACTIONS = ["
    start = text.find(declaration)
    if start == -1:
        raise ValueError(
            "no `const ACTIONS = [` declaration found in the TypeScript source — "
            "the array was renamed, moved, or the file is not dev-status-tool.ts"
        )
    i = start + len(declaration)
    literals: list[str] = []
    depth = 1
    normal, string, line_comment, block_comment = range(4)
    state = normal
    while i < len(text):
        char = text[i]
        if state == normal:
            if char == '"':
                state = string
                i += 1
                literal_start = i
            elif text.startswith("//", i):
                state = line_comment
                i += 2
            elif text.startswith("/*", i):
                state = block_comment
                i += 2
            elif char == "[":
                depth += 1
                i += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return literals
                i += 1
            else:
                i += 1
        elif state == string:
            if char == "\\":
                i += 2
            elif char == '"':
                literals.append(text[literal_start:i])
                state = normal
                i += 1
            else:
                i += 1
        elif state == line_comment:
            if char == "\n":
                state = normal
            i += 1
        else:  # block_comment
            if text.startswith("*/", i):
                state = normal
                i += 2
            else:
                i += 1
    raise ValueError(
        "unterminated `const ACTIONS = [` array — the closing `]` was never "
        "found, so the TypeScript source is malformed"
    )


# ── coverage diff ────────────────────────────────────────────────────────────


@dataclasses.dataclass
class CoverageDiff:
    """Everything wrong with an ACTIONS array against the real CLI leaves."""

    #: (leaf path, mapped name) the tool cannot reach — the silent-drift bug.
    missing: list[tuple[tuple[str, ...], str]]
    #: ACTIONS entries no real CLI leaf maps to (removed/renamed subcommand).
    stale: list[str]
    #: mapped name -> the distinct leaf paths colliding onto it.
    collisions: dict[str, list[tuple[str, ...]]]
    #: (leaf path, mapped name) where the mapped name is not [a-z0-9_]+.
    malformed: list[tuple[tuple[str, ...], str]]
    #: ALLOWED_UNEXPOSED keys that ARE exposed in ACTIONS.
    exposed_allowlisted: list[str]


def compute_coverage_diff(
    actions: list[str], leaves: set[tuple[str, ...]], allowed: dict[str, str]
) -> CoverageDiff:
    """Diff an ACTIONS list against mapped CLI leaves under ``allowed``.

    Pure and total — every field is always computed, so one call reports
    every problem at once instead of failing on the first.
    """
    mapped: dict[str, tuple[str, ...]] = {}
    collisions: dict[str, list[tuple[str, ...]]] = {}
    malformed: list[tuple[tuple[str, ...], str]] = []
    for path in leaves:
        name = mapped_action_name(path)
        if not _IDENTIFIER_RE.fullmatch(name):
            malformed.append((path, name))
        if name in mapped:
            collisions.setdefault(name, [mapped[name]]).append(path)
        else:
            mapped[name] = path

    seen: set[str] = set()
    exposed_allowlisted = []
    for action in actions:
        if action in seen:
            continue
        seen.add(action)
        if action in allowed and action in mapped:
            exposed_allowlisted.append(action)

    missing = sorted(
        (path, name)
        for name, path in mapped.items()
        if name not in seen and name not in allowed
    )
    stale = sorted(a for a in seen if a not in mapped)
    return CoverageDiff(
        missing=missing,
        stale=stale,
        collisions={name: sorted(paths) for name, paths in sorted(collisions.items())},
        malformed=sorted(malformed),
        exposed_allowlisted=sorted(exposed_allowlisted),
    )


def describe_problems(diff: CoverageDiff) -> list[str]:
    """One human-readable line per problem, in stable order."""
    problems: list[str] = []
    for path, name in diff.missing:
        problems.append(
            f"missing action {name!r} — dev_status.py's leaf subcommand "
            f"`{' '.join(path)}` is unreachable from pi's dev-status-tool"
        )
    for action in diff.stale:
        problems.append(
            f"stale action {action!r} — no dev_status.py leaf subcommand maps "
            "to it; remove it from ACTIONS"
        )
    for name, paths in diff.collisions.items():
        joined = ", ".join(f"`{' '.join(p)}`" for p in paths)
        problems.append(
            f"collision on action name {name!r} — distinct leaf subcommands "
            f"{joined} all map to it, so an entry could hide a missing leaf"
        )
    for path, name in diff.malformed:
        problems.append(
            f"malformed action name {name!r} (from leaf `{' '.join(path)}`) — "
            "does not match [a-z0-9_]+"
        )
    for action in diff.exposed_allowlisted:
        problems.append(
            f"action {action!r} is both exposed in ACTIONS and on record in "
            "ALLOWED_UNEXPOSED as deliberately unexposed — pick one"
        )
    return problems


def allowlist_problems(
    allowed: dict[str, str], leaves: set[tuple[str, ...]]
) -> list[str]:
    """One message per malformed ALLOWED_UNEXPOSED entry.

    Catches the allowlist itself going stale (a key whose subcommand no
    longer exists) and the empty-reason shape the allowlist exists to
    prevent.
    """
    mapped = {mapped_action_name(path) for path in leaves}
    problems: list[str] = []
    for name, reason in sorted(allowed.items()):
        if name not in mapped:
            problems.append(
                f"ALLOWED_UNEXPOSED entry {name!r} is not a real dev_status.py "
                "leaf subcommand under the mapping rule — the subcommand was "
                "removed or renamed; delete the entry"
            )
        if not reason.strip():
            problems.append(
                f"ALLOWED_UNEXPOSED entry {name!r} has an empty reason — an "
                "unexposed action without a stated why is exactly the silent "
                "omission this allowlist exists to prevent"
            )
    return problems


# ── real-repo accessors ──────────────────────────────────────────────────────


def real_cli_leaves() -> set[tuple[str, ...]]:
    """Leaf subcommand paths of dev_status.py, via gen_interfaces' parser."""
    source = DEV_STATUS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    spec = gi.extract_cli(tree, ast.get_docstring(tree) or "")
    assert spec is not None, (
        "extract_cli found no CLI in claude/scripts/dev_status.py — the parser "
        "builder pattern changed and gen_interfaces' extractor no longer "
        "recognizes it; fix the extractor, never a second parser here"
    )
    assert spec.subcommands, (
        "extract_cli found zero subcommands in claude/scripts/dev_status.py — "
        "an empty coverage check would pass anything"
    )
    return gi.leaf_subcommand_paths(spec.subcommands)


def real_tool_actions() -> list[str]:
    """ACTIONS from dev-status-tool.ts, with extraction failure as failure."""
    try:
        return extract_actions_from_ts_source(TOOL_PATH.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AssertionError(str(exc)) from exc


# ── unit tests: pure helpers on synthetic data ───────────────────────────────


def test_mapping_rule_examples() -> None:
    assert mapped_action_name(("pending", "add")) == "pending_add"
    assert mapped_action_name(("out-of-scope", "link")) == "out_of_scope_link"
    assert mapped_action_name(("gate-set",)) == "gate_set"
    assert mapped_action_name(("ready",)) == "ready"


def test_missing_leaf_is_reported_naming_the_action() -> None:
    diff = compute_coverage_diff(["render"], {("render",), ("ready",)}, {})
    assert diff.missing == [(("ready",), "ready")]


def test_stale_entry_is_reported() -> None:
    diff = compute_coverage_diff(["render", "removed_thing"], {("render",)}, {})
    assert diff.stale == ["removed_thing"]


def test_collision_is_reported_naming_both_paths() -> None:
    diff = compute_coverage_diff(["a_b"], {("a", "b"), ("a-b",)}, {})
    assert diff.collisions == {"a_b": [("a", "b"), ("a-b",)]}
    assert not diff.missing  # the collision is its own failure, not a miss


def test_malformed_mapped_name_is_reported() -> None:
    diff = compute_coverage_diff([], {("Weird",)}, {})
    assert diff.malformed == [(("Weird",), "Weird")]


def test_allowed_entry_suppresses_missing() -> None:
    diff = compute_coverage_diff(
        ["render"], {("render",), ("prune",)}, {"prune": "policy"}
    )
    assert not diff.missing


def test_exposed_allowlisted_key_is_reported() -> None:
    diff = compute_coverage_diff(["prune"], {("prune",)}, {"prune": "policy"})
    assert diff.exposed_allowlisted == ["prune"]


def test_allowlist_key_for_nonexistent_leaf_is_flagged() -> None:
    problems = allowlist_problems({"ghost_action": "why"}, {("render",)})
    assert len(problems) == 1
    assert "not a real dev_status.py leaf" in problems[0]


def test_allowlist_empty_reason_is_flagged() -> None:
    for reason in ("", "   "):
        problems = allowlist_problems({"render": reason}, {("render",)})
        assert len(problems) == 1
        assert "empty reason" in problems[0]


def test_extract_multi_line_trailing_commas_and_comments() -> None:
    text = (
        "const ACTIONS = [\n"
        '  "render", // first\n'
        "  /* block\n"
        "     comment ] with bracket */\n"
        '  "list",\n'
        "] as const;\n"
    )
    assert extract_actions_from_ts_source(text) == ["render", "list"]


def test_extract_string_literal_containing_bracket() -> None:
    text = 'const ACTIONS = ["a]b", "c"] as const;'
    assert extract_actions_from_ts_source(text) == ["a]b", "c"]


def test_extract_empty_array() -> None:
    assert extract_actions_from_ts_source("const ACTIONS = [] as const;") == []


def test_extract_missing_declaration_raises() -> None:
    try:
        extract_actions_from_ts_source("const OTHER = [] as const;")
    except ValueError as exc:
        assert "const ACTIONS" in str(exc)
    else:
        raise AssertionError("missing declaration must raise, never return []")


# ── integration: real files ──────────────────────────────────────────────────


def test_actions_cover_every_leaf_subcommand() -> None:
    diff = compute_coverage_diff(
        real_tool_actions(), real_cli_leaves(), ALLOWED_UNEXPOSED
    )
    problems = describe_problems(diff)
    assert not problems, "\n".join(problems)


def test_allowed_unexposed_entries_exist() -> None:
    problems = allowlist_problems(ALLOWED_UNEXPOSED, real_cli_leaves())
    assert not problems, "\n".join(problems)


# ── mutation proof: the guard fails when coverage actually regresses ────────


def test_mutation_removing_an_actions_entry_is_caught() -> None:
    """Delete one real entry in memory; the diff must name exactly it.

    Green on the unmutated tree is not evidence (that is exactly how the
    original bug slipped through) — this proves the check can fail.
    """
    actions = real_tool_actions()
    leaves = real_cli_leaves()
    assert not describe_problems(
        compute_coverage_diff(actions, leaves, ALLOWED_UNEXPOSED)
    ), "the real tree is expected to be clean; fix live drift first"
    victim = "ready" if "ready" in actions else actions[0]
    mutated = [action for action in actions if action != victim]
    diff = compute_coverage_diff(mutated, leaves, ALLOWED_UNEXPOSED)
    assert [(path, name) for path, name in diff.missing] and [
        name for _, name in diff.missing
    ] == [victim]


def test_mutation_fake_subcommand_is_caught() -> None:
    """A new dev_status.py leaf the tool never learned about fails, named."""
    synthetic = (
        "import argparse\n"
        "\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        '    parser = argparse.ArgumentParser(description="synthetic")\n'
        '    sub = parser.add_subparsers(dest="action")\n'
        '    sub.add_parser("render", help="r")\n'
        '    pending = sub.add_parser("pending", help="group")\n'
        '    psub = pending.add_subparsers(dest="pending_action")\n'
        '    psub.add_parser("add", help="a")\n'
        '    sub.add_parser("brand_new_subcommand", help="future")\n'
        "    return parser\n"
    )
    tree = ast.parse(synthetic)
    spec = gi.extract_cli(tree, "synthetic")
    assert spec is not None
    leaves = gi.leaf_subcommand_paths(spec.subcommands)
    assert ("pending",) not in leaves  # routing nodes stay out
    diff = compute_coverage_diff(["render", "pending_add"], leaves, {})
    assert [name for _, name in diff.missing] == ["brand_new_subcommand"]
