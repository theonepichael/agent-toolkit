#!/usr/bin/env python3
"""Tests for scripts/check_toolkit_paths.py (path ownership + writer inventory)."""

import ast
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import check_toolkit_paths as c  # noqa: E402


# ── the real repository ──────────────────────────────────────────────────────


@pytest.mark.allow_real_subprocess  # git ls-files on this checkout
def test_real_repository_passes_both_checks():
    start = time.monotonic()
    _refs, problems = c.ownership(c.REPO)
    assert problems == []
    _counts, problems = c.inventory(c.REPO)
    assert problems == []
    assert time.monotonic() - start < 5


# ── classification rules ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("segs", "cls"),
    [
        (("scripts", "dev_status.py"), "toolkit"),
        (("scripts", "dev_status_sync.py"), "foreign"),
        (("data", "grill"), "toolkit"),
        (("data",), "toolkit"),
        (("hooks",), "toolkit"),
        (("projects",), "harness"),
        (("settings.json",), "harness"),
        ((), "harness"),
        (("data", "draft-issues"), None),
        (("mystery",), None),
    ],
)
def test_classify(segs, cls):
    assert c.classify(segs) == cls


def test_text_extraction_respects_boundaries_and_placeholders():
    refs = c.text_references(
        "a ~/.claude/data/grill/<slug>-plan.md b foo.claude c $HOME/.claude/commands\n"
        "d `~/.claude/**` e"
    )
    assert refs == [(1, ("data", "grill")), (1, ("commands",)), (2, ())]


def test_python_extraction_finds_split_literals_and_variables():
    tree = ast.parse(
        'A = Path.home() / ".claude" / "data" / "x"\n'
        "B = Path.home() / '.claude'\n"
        'C = B / "scripts" / "y.py"\n'
        'D = "prose ~/.claude/data/grill is not code"\n'
    )
    segs = {s for _l, s in c.python_references(tree)}
    assert ("data", "x") in segs
    assert ("scripts", "y.py") in segs
    assert not any(s[:2] == ("data", "grill") for s in segs)


def test_js_extraction_finds_join_calls():
    refs = c.js_references(
        'const P = join(homedir(), ".claude", "scripts", "dev_status.py");\n'
        'const Q = path.join(os.homedir(), ".claude", "mystery");\n'
    )
    assert refs == [(1, ("scripts", "dev_status.py")), (2, ("mystery",))]


# ── ownership check on fixture trees ─────────────────────────────────────────


def write(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return rel


def test_unclassified_reference_fails_with_a_useful_message(tmp_path):
    rel = write(tmp_path, "docs/x.md", "see ~/.claude/mystery/thing\n")
    _refs, problems = c.ownership(tmp_path, [rel])
    assert len(problems) == 1
    assert problems[0].startswith("docs/x.md:1: .claude/mystery")
    assert "path-ownership" in problems[0]


def test_unknown_data_entry_fails(tmp_path):
    rel = write(tmp_path, "a.md", "~/.claude/data/draft-issues/x.md\n")
    assert c.ownership(tmp_path, [rel])[1]


def test_ts_join_to_an_unknown_segment_fails(tmp_path):
    rel = write(tmp_path, "pi/ext.ts", 'join(homedir(), ".claude", "mystery")\n')
    assert c.ownership(tmp_path, [rel])[1]


def test_foreign_harness_and_word_boundaries_pass(tmp_path):
    rel = write(
        tmp_path,
        "a.md",
        "~/.claude/scripts/dev_status_sync.py ~/.claude/projects foo.claude/bar\n",
    )
    refs, problems = c.ownership(tmp_path, [rel])
    assert problems == []
    assert {r.cls for r in refs} == {"foreign", "harness"}


def test_line_marker_classifies_and_is_reported(tmp_path):
    rel = write(tmp_path, "a.md", "~/.claude/odd  <!-- path-ownership: harness -->\n")
    refs, problems = c.ownership(tmp_path, [rel])
    assert problems == []
    assert refs[0].marked and refs[0].cls == "harness"


def test_test_trees_are_counted_but_never_fail(tmp_path):
    rel = write(tmp_path, "test/test_x.py", 'p = "~/.claude/g.md"\n')
    refs, problems = c.ownership(tmp_path, [rel])
    assert problems == []
    assert refs and refs[0].cls is None


# ── inventory check on fixture trees ─────────────────────────────────────────


@pytest.fixture()
def inv(tmp_path, monkeypatch):
    (tmp_path / "agent-scripts").mkdir()
    adoption = tmp_path / "adoption.py"
    adoption.write_text("# covers: good_writer\n")
    monkeypatch.setattr(c, "ADOPTION_TESTS", adoption)

    def mod(name: str, body: str) -> str:
        return write(tmp_path, f"agent-scripts/{name}.py", body)

    return tmp_path, mod


def test_python_module_without_a_marker_fails(inv):
    root, mod = inv
    src = mod("plain", "import os\n")
    assert any("TOOLKIT_DATA must be" in p for p in c.inventory(root, [src])[1])


def test_good_writer_passes(inv):
    root, mod = inv
    src = mod(
        "good_writer",
        'import agent_toolkit_paths\nimport migration_lock\nTOOLKIT_DATA = "writer"\n',
    )
    assert c.inventory(root, [src])[1] == []


def test_writer_that_does_not_lock_fails(inv):
    root, mod = inv
    src = mod("good_writer", 'import agent_toolkit_paths\nTOOLKIT_DATA = "writer"\n')
    assert any("does not reference migration_lock" in p for p in c.inventory(root, [src])[1])


def test_writer_without_an_adoption_test_fails(inv):
    root, mod = inv
    src = mod(
        "new_writer",
        'import agent_toolkit_paths\nimport migration_lock\nTOOLKIT_DATA = "writer"\n',
    )
    assert any("no execution test" in p for p in c.inventory(root, [src])[1])


def test_none_module_that_imports_the_resolver_fails(inv):
    root, mod = inv
    src = mod("sneaky", 'import agent_toolkit_paths\nTOOLKIT_DATA = "none"\n')
    assert any("marked 'none'" in p for p in c.inventory(root, [src])[1])


def test_none_module_may_mention_paths_in_prose(inv):
    root, mod = inv
    src = mod("prose", 'TEXT = "write to ~/.claude/data/grill/x.md"\nTOOLKIT_DATA = "none"\n')
    assert c.inventory(root, [src])[1] == []


def test_via_cycle_and_writer_listing_a_reader_fail(inv):
    root, mod = inv
    a = mod("mod_a", 'TOOLKIT_DATA = "reader"\nTOOLKIT_DATA_VIA = ("mod_b",)\n')
    b = mod("mod_b", 'TOOLKIT_DATA = "reader"\nTOOLKIT_DATA_VIA = ("mod_a",)\n')
    assert any("cycle" in p for p in c.inventory(root, [a, b])[1])
    w = mod("good_writer", 'TOOLKIT_DATA = "writer"\nTOOLKIT_DATA_VIA = ("mod_r",)\n')
    r = mod("mod_r", 'import agent_toolkit_paths\nTOOLKIT_DATA = "reader"\n')
    assert any("may only delegate to writers" in p for p in c.inventory(root, [w, r])[1])


def test_infrastructure_is_reserved(inv):
    root, mod = inv
    src = mod("random_mod", 'TOOLKIT_DATA = "infrastructure"\n')
    assert any("only for" in p for p in c.inventory(root, [src])[1])


def test_new_ts_file_needs_a_row_and_unlocked_needs_a_reason(inv, monkeypatch):
    root, _mod = inv
    ts = write(root, "pi/extensions/new-thing.ts", "export {}\n")
    assert any("no NON_PYTHON row" in p for p in c.inventory(root, [ts])[1])
    monkeypatch.setitem(c.NON_PYTHON, ts, ("unlocked", " "))
    assert any("bad NON_PYTHON row" in p for p in c.inventory(root, [ts])[1])


def test_unknown_installed_kind_fails(inv):
    root, _mod = inv
    odd = write(root, "bin/tool.exe", "x")
    assert any("unknown kind" in p for p in c.inventory(root, [odd])[1])
