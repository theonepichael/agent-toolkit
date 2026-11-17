#!/usr/bin/env python3
"""Ratchet guard: no new test smells without an allowlist entry.

Enforces the mechanical half of test/AGENTS.md's "A test must be able to
fail": no real sleeps (time.sleep / asyncio.sleep / setTimeout used to
wait), no wall-clock reads (time.time / monotonic / perf_counter), and no
assertion against a 40+ character string literal. Every current hit is
frozen in ``test_smell_allowlist.json`` keyed by relative file path and a
stable fingerprint (normalized matched text, never line numbers); the check
fails on any hit without an entry, and on any entry whose hit no longer
exists, so the list only shrinks as hits are fixed.

The scanner excludes its own file (by exact resolved path) because its
detector pattern strings and short-token assertions are not test subjects.
The allowlist is ``.json``, which none of the three scan globs selects.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "test" / "test_smell_allowlist.json"
_SELF = Path(__file__).resolve()

# (glob, language) scan surface, relative to REPO_ROOT.
_SCAN_TARGETS = (
    ("test/*.py", "python"),
    ("pi/test/*.ts", "typescript"),
    ("opencode/test/*.ts", "typescript"),
)

_SLEEP_MODULES = {"time", "asyncio"}
_CLOCK_ATTRS = {"time", "monotonic", "perf_counter"}
_CLOCK_BARE = {"monotonic", "perf_counter"}

# unittest assertion methods grouped by which operands are "asserted text"
# (so diagnostic `msg` args and callable inputs are not misread as hits).
_VALUE_COMPARE_METHODS = {
    "assertEqual",
    "assertNotEqual",
    "assertIs",
    "assertIsNot",
    "assertIsInstance",
    "assertNotIsInstance",
    "assertCountEqual",
    "assertLess",
    "assertLessEqual",
    "assertGreater",
    "assertGreaterEqual",
    "assertAlmostEqual",
    "assertNotAlmostEqual",
    "assertRegex",
    "assertNotRegex",
    "assertSequenceEqual",
    "assertListEqual",
    "assertTupleEqual",
    "assertSetEqual",
    "assertDictEqual",
    "assertMultiLineEqual",
    "assertIn",
    "assertNotIn",
}
_CONDITION_METHODS = {"assertTrue", "assertFalse"}
# Single-value assertions (obj, msg=None): scanning both args would read the
# diagnostic message as asserted text.
_SINGLE_VALUE_METHODS = {"assertIsNone", "assertIsNotNone"}
_REGEX_RAISE_METHODS = {"assertRaisesRegex", "assertWarnsRegex"}

# String-predicate methods whose literal argument is asserted text, e.g.
# ``assertTrue(x.startswith("..."))``. Distinct from input-producing calls
# like ``self.cli(...)``, which are deliberately not descended into.
_PREDICATE_METHODS = {
    "startswith",
    "endswith",
    "find",
    "rfind",
    "index",
    "rindex",
    "count",
    "match",
    "fullmatch",
    "search",
}

_JEST_MATCHERS = (
    "toContain",
    "toContainEqual",
    "toEqual",
    "toBe",
    "toStrictEqual",
    "toMatch",
    "toThrow",
    "toThrowError",
    "toMatchObject",
    "toHaveBeenCalledWith",
    "toHaveBeenCalled",
)
_NODE_ASSERT_MATCHERS = (
    "equal",
    "strictEqual",
    "deepEqual",
    "notEqual",
    "notStrictEqual",
    "notDeepEqual",
    "match",
    "doesNotMatch",
)
_TS_SLEEP_RE = re.compile(r"\bsetTimeout\b")
# A Jest/Vitest matcher (``.toX(``) or a node:assert comparison
# (``assert.equal(`` etc.) followed -- possibly across lines -- by a 40+
# character string literal in any of the three JS quote styles. The literal
# must come after the matcher's opening paren, so an input string passed to a
# nested call is not flagged. ``.`` matches embedded quotes of a different
# style (e.g. an apostrophe inside a double-quoted string); the backreference
# requires the same quote to terminate. The content is in group ``lit``.
_TS_PROSE_RE = re.compile(
    r"(?:\.(?P<jest>"
    + "|".join(_JEST_MATCHERS)
    + r")|assert\.(?P<node>"
    + "|".join(_NODE_ASSERT_MATCHERS)
    + r"))\s*\([^)]*?(?P<q>['\"`])(?P<lit>.{40,}?)(?P=q)",
)

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _literal_text(node: ast.AST) -> str | None:
    """The full literal text of a string-literal expression, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_text(node.left)
        right = _literal_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _is_predicate_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in _PREDICATE_METHODS


def _assertion_literals(expr: ast.AST) -> list[str]:
    """Normalized 40+ char string literals asserted against within ``expr``."""
    found: list[str] = []

    def visit(node: ast.AST) -> None:
        text = _literal_text(node)
        if text is not None:
            if len(text) >= 40:
                found.append(_normalize(text))
            return
        if isinstance(node, ast.Compare):
            visit(node.left)
            for comp in node.comparators:
                visit(comp)
            return
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                visit(value)
            return
        if isinstance(node, ast.UnaryOp):
            visit(node.operand)
            return
        if isinstance(node, ast.Call) and _is_predicate_call(node):
            for arg in node.args:
                visit(arg)
            for kw in node.keywords:
                visit(kw.value)
            return
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                visit(elt)
            return
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None:
                    visit(key)
            for value in node.values:
                visit(value)
            return
        # Anything else (Name, Attribute, Subscript, a non-predicate Call,
        # etc.) is not descended into: a long string there is test input.

    visit(expr)
    return found


def _sleep_fingerprint(node: ast.Call) -> str | None:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "sleep"
        and isinstance(func.value, ast.Name)
        and func.value.id in _SLEEP_MODULES
    ):
        return f"{func.value.id}.sleep"
    return None


def _clock_fingerprint(node: ast.Call) -> str | None:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _CLOCK_ATTRS
        and isinstance(func.value, ast.Name)
        and func.value.id == "time"
    ):
        return f"time.{func.attr}"
    if isinstance(func, ast.Name) and func.id in _CLOCK_BARE:
        return func.id
    return None


def scan_python_file(path: Path) -> Counter[str]:
    """Return a Counter of smell fingerprints for one Python test file."""
    hits: Counter[str] = Counter()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return hits
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            sleep = _sleep_fingerprint(node)
            if sleep is not None:
                hits[sleep] += 1
            clock = _clock_fingerprint(node)
            if clock is not None:
                hits[clock] += 1
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else None)
            )
            if name in _VALUE_COMPARE_METHODS:
                operands = node.args[:2]
            elif name in _CONDITION_METHODS or name in _SINGLE_VALUE_METHODS:
                operands = node.args[:1]
            elif name in _REGEX_RAISE_METHODS:
                operands = node.args[1:2]
            else:
                operands = ()
            for operand in operands:
                for literal in _assertion_literals(operand):
                    hits[literal] += 1
        elif isinstance(node, ast.Assert):
            for literal in _assertion_literals(node.test):
                hits[literal] += 1
    return hits


def _strip_ts_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments, leaving string contents intact."""
    out: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def scan_typescript_file(path: Path) -> Counter[str]:
    """Return a Counter of smell fingerprints for one TypeScript test file."""
    hits: Counter[str] = Counter()
    try:
        text = _strip_ts_comments(path.read_text(encoding="utf-8"))
    except OSError:
        return hits
    n_sleep = len(_TS_SLEEP_RE.findall(text))
    if n_sleep:
        hits["setTimeout"] += n_sleep
    for match in _TS_PROSE_RE.finditer(text):
        hits[_normalize(match.group("lit"))] += 1
    return hits


def collect_targets() -> dict[str, Path]:
    """Map relative path -> Path for every scanned file (self excluded)."""
    targets: dict[str, Path] = {}
    for pattern, _lang in _SCAN_TARGETS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            resolved = path.resolve()
            if resolved == _SELF:
                continue
            targets[path.relative_to(REPO_ROOT).as_posix()] = path
    return targets


def scan_surface(targets: dict[str, Path] | None = None) -> dict[str, Counter[str]]:
    """Scan the real surface (or an explicit target map) into rel path -> hits."""
    if targets is None:
        targets = collect_targets()
    result: dict[str, Counter[str]] = {}
    for rel, path in sorted(targets.items()):
        if rel.endswith(".ts"):
            result[rel] = scan_typescript_file(path)
        else:
            result[rel] = scan_python_file(path)
    return result


def validate_allowlist(data: dict[str, list[dict[str, str]]]) -> list[str]:
    """Return a list of problems; empty means the allowlist is well-formed."""
    problems: list[str] = []
    for rel, entries in data.items():
        if not isinstance(entries, list):
            problems.append(f"{rel}: expected a list of entries")
            continue
        for entry in entries:
            fp = entry.get("fingerprint") if isinstance(entry, dict) else None
            reason = entry.get("reason") if isinstance(entry, dict) else None
            if not isinstance(fp, str) or not fp:
                problems.append(f"{rel}: entry with a missing/blank fingerprint")
            if not isinstance(reason, str) or not reason.strip():
                problems.append(f"{rel}: entry {fp!r} has a blank reason")
    return problems


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, list[dict[str, str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = validate_allowlist(data)
    if problems:
        raise ValueError("invalid allowlist:\n" + "\n".join(problems))
    return data


def diff(
    hits_by_file: dict[str, Counter[str]],
    allowlist: dict[str, list[dict[str, str]]],
) -> tuple[list[str], list[str]]:
    """Return (unlisted, stale) human-readable report lines."""
    unlisted: list[str] = []
    stale: list[str] = []
    for rel in sorted(set(hits_by_file) | set(allowlist)):
        hit_counts = Counter(hits_by_file.get(rel, Counter()))
        entry_counts = Counter(entry["fingerprint"] for entry in allowlist.get(rel, []))
        for fp, n in sorted((hit_counts - entry_counts).items()):
            unlisted.append(f"{rel}: {n}x {fp!r} not in the allowlist")
        for fp, n in sorted((entry_counts - hit_counts).items()):
            stale.append(f"{rel}: {n}x {fp!r} entry has no matching hit")
    return unlisted, stale


# --- tests ---

import pytest  # noqa: E402


def test_python_smells_are_detected() -> None:
    fixtures = REPO_ROOT / "test" / "fixtures" / "test_smell_ratchet"
    sleep_hits = scan_python_file(fixtures / "sleep_fixture.py")
    clock_hits = scan_python_file(fixtures / "wallclock_fixture.py")
    prose_hits = scan_python_file(fixtures / "longliteral_fixture.py")
    assert sleep_hits["time.sleep"] >= 1
    assert clock_hits["time.monotonic"] >= 1
    assert len(prose_hits) >= 5
    assert any(len(fp) >= 40 for fp in prose_hits)


def test_typescript_smells_are_detected() -> None:
    fixtures = REPO_ROOT / "test" / "fixtures" / "test_smell_ratchet"
    sleep_hits = scan_typescript_file(fixtures / "ts_sleep_fixture.ts")
    prose_hits = scan_typescript_file(fixtures / "ts_prose_fixture.ts")
    assert sleep_hits["setTimeout"] >= 1
    assert len(prose_hits) >= 1
    assert any(len(fp) >= 40 for fp in prose_hits)


def test_clean_fixture_has_no_hits() -> None:
    fixtures = REPO_ROOT / "test" / "fixtures" / "test_smell_ratchet"
    assert not scan_python_file(fixtures / "clean_fixture.py")


def test_forty_char_boundary(tmp_path: Path) -> None:
    under = "x" * 39
    over = "y" * 40
    path = tmp_path / "boundary.py"
    path.write_text(
        f'def _probe(out):\n    assert out == "{under}"\n    assert out == "{over}"\n',
        encoding="utf-8",
    )
    hits = scan_python_file(path)
    assert under not in hits
    assert over in hits
    assert hits[over] == 1


def test_ts_literal_with_embedded_other_quote_is_detected(tmp_path: Path) -> None:
    # Regression: an apostrophe inside a double-quoted matcher literal used to
    # stop the match and skip the whole assertion.
    literal = "this sentence isn't short and is comfortably over forty characters"
    assert len(literal) >= 40
    path = tmp_path / "apostrophe.ts"
    path.write_text(
        f'expect(out).toContain("{literal}");\n',
        encoding="utf-8",
    )
    hits = scan_typescript_file(path)
    assert _normalize(literal) in hits


def test_assert_is_none_msg_is_not_scanned(tmp_path: Path) -> None:
    # Regression: assertIsNone(value, msg) scans only the value, not the
    # diagnostic message.
    path = tmp_path / "msg.py"
    path.write_text(
        "def _probe(value):\n"
        '    assertIsNone(value, "this diagnostic message is over forty characters long")\n',
        encoding="utf-8",
    )
    assert not scan_python_file(path)


def test_ts_multiline_prose_is_detected(tmp_path: Path) -> None:
    # Regression: a matcher and its literal may sit on separate lines.
    literal = "candidate meta-a (selected earlier this wave)"
    assert len(literal) >= 40
    path = tmp_path / "multiline.ts"
    path.write_text(
        f'expect(res.reason).toContain(\n    "{literal}",\n);\n',
        encoding="utf-8",
    )
    assert _normalize(literal) in scan_typescript_file(path)


def test_ts_node_assert_prose_is_detected(tmp_path: Path) -> None:
    # Regression: opencode tests use node:assert rather than Jest matchers.
    literal = "this sentence is comfortably over forty characters long"
    path = tmp_path / "node-assert.ts"
    path.write_text(
        f'assert.equal(out, "{literal}");\n',
        encoding="utf-8",
    )
    assert _normalize(literal) in scan_typescript_file(path)


def test_ts_comment_is_not_a_sleep(tmp_path: Path) -> None:
    # Regression: a setTimeout mention in a comment is not a real sleep.
    path = tmp_path / "comment.ts"
    path.write_text(
        "// setTimeout is deliberately replaced by a fake clock\n",
        encoding="utf-8",
    )
    assert not scan_typescript_file(path)


def test_ts_nested_call_input_is_not_prose(tmp_path: Path) -> None:
    # Regression: a long string passed to a nested call before the matcher is
    # test input, not asserted prose.
    path = tmp_path / "input.ts"
    path.write_text(
        'expect(run("this sentence is comfortably over forty characters long")).toBe(0);\n',
        encoding="utf-8",
    )
    assert not scan_typescript_file(path)


def test_python_container_literal_is_detected(tmp_path: Path) -> None:
    # Regression: a 40+ char literal inside a list/tuple/set/dict comparison.
    literal = "this sentence is comfortably over forty characters long"
    path = tmp_path / "container.py"
    path.write_text(
        f'def _probe(out):\n    assert out == ["{literal}"]\n',
        encoding="utf-8",
    )
    assert _normalize(literal) in scan_python_file(path)


def test_unlisted_hit_is_reported() -> None:
    hits = {"test/a.py": Counter({"time.sleep": 1})}
    allowlist: dict[str, list[dict[str, str]]] = {}
    unlisted, stale = diff(hits, allowlist)
    assert any("time.sleep" in line for line in unlisted)
    assert not stale


def test_stale_entry_is_reported() -> None:
    hits: dict[str, Counter[str]] = {}
    allowlist = {"test/a.py": [{"fingerprint": "time.sleep", "reason": "gone"}]}
    unlisted, stale = diff(hits, allowlist)
    assert not unlisted
    assert any("time.sleep" in line for line in stale)


def test_multiset_duplicates_in_both_directions() -> None:
    two_hits_one_entry = diff(
        {"test/a.py": Counter({"time.sleep": 2})},
        {"test/a.py": [{"fingerprint": "time.sleep", "reason": "one"}]},
    )
    assert len(two_hits_one_entry[0]) == 1  # one unlisted
    assert not two_hits_one_entry[1]

    one_hit_two_entries = diff(
        {"test/a.py": Counter({"time.sleep": 1})},
        {
            "test/a.py": [
                {"fingerprint": "time.sleep", "reason": "one"},
                {"fingerprint": "time.sleep", "reason": "two"},
            ]
        },
    )
    assert not one_hit_two_entries[0]
    assert len(one_hit_two_entries[1]) == 1  # one stale


def test_blank_reason_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps({"test/a.py": [{"fingerprint": "time.sleep", "reason": "  "}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="blank reason"):
        load_allowlist(path)


def test_real_tree_matches_allowlist() -> None:
    targets = collect_targets()
    # Positive control: discovery must find the surface — an empty scan
    # against an empty allowlist is not agreement. Detector coverage is the
    # fixture tests' job (above), so completing the allowlist workdown to
    # zero hits does not break this test.
    assert len(targets) > 50, "scan discovered no files"
    hits = scan_surface(targets)
    allowlist = load_allowlist()
    unlisted, stale = diff(hits, allowlist)
    assert not unlisted, "unlisted test smells:\n" + "\n".join(unlisted)
    assert not stale, "stale allowlist entries:\n" + "\n".join(stale)
