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
        (("hooks", "agy-elapsed.js"), "toolkit"),
        (("hooks", "herdr-agent-state.sh"), "foreign"),
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


# ── generator inventory and generated paths ──────────────────────────────────


@pytest.mark.allow_real_subprocess  # git ls-files on this checkout
def test_real_generators_claim_every_generated_file():
    assert c.generators(c.REPO) == []


def test_real_generated_outputs_name_no_legacy_toolkit_path():
    assert c.generated_legacy_references(c.REPO) == []


def test_generated_make_skill_keeps_its_harness_owned_commands_path():
    text = (REPO / "claude/commands/make-skill.md").read_text()
    refs = c.references_in("claude/commands/make-skill.md", text)
    assert any(r.segments[:1] == ("commands",) and r.cls == "harness" for r in refs)


def test_js_extraction_sees_bundler_renamed_join():
    refs = c.js_references('join2(homedir(), ".claude", "scripts", "dev_status.py")\n')
    assert refs == [(1, ("scripts", "dev_status.py"))]


def gen(outputs, link_lines=None):
    return c.Generator("cmd", None, (), lambda: list(outputs), link_lines)


@pytest.fixture
def links_root(tmp_path):
    write(
        tmp_path,
        "links.toml",
        '[[link]]\nsrc = "agent-scripts/x.py"\ndest = "~/.claude/scripts/x.py"\n',
    )
    return tmp_path


def test_legacy_toolkit_path_in_output_is_reported(links_root):
    write(links_root, "out.md", "run python3 ~/.claude/scripts/x.py\n")
    problems = c.generated_legacy_references(links_root, {"g": gen(["out.md"])})
    assert len(problems) == 1
    assert problems[0].startswith("out.md:1: .claude/scripts/x.py")


def test_harness_and_foreign_paths_in_output_pass(links_root):
    write(
        links_root,
        "out.md",
        "~/.claude/commands/x.md ~/.claude/scripts/dev_status_sync.py\n",
    )
    assert c.generated_legacy_references(links_root, {"g": gen(["out.md"])}) == []


def test_link_rendered_line_passes_only_for_a_real_destination(links_root):
    pattern = c.GENERATORS["gen_interfaces.py"].link_lines
    write(
        links_root,
        "I.md",
        "- Installed at: `~/.claude/scripts/x.py` (all harnesses)\n"
        "| `agent-scripts/x.py` | `~/.claude/scripts/x.py` (agy) |\n"
        "- Installed at: `~/.claude/scripts/gone.py` (all harnesses)\n",
    )
    problems = c.generated_legacy_references(
        links_root, {"g": gen(["I.md"], pattern)}
    )
    assert [p.split(":")[1] for p in problems] == ["3"]


def test_inventory_flags_missing_duplicate_and_unclaimed_outputs(tmp_path):
    write(tmp_path, "a.md", "x\n")
    orphan = write(
        tmp_path,
        "orphan.md",
        "<!-- generated by agent-scripts/gen_x.py — do not edit -->\n",
    )
    table = {
        "one": gen(["a.md", "missing.md"]),
        "two": gen(["a.md"]),
        "empty": gen([]),
    }
    problems = c.generators(tmp_path, table, ["a.md", orphan])
    assert any("missing.md is not tracked" in p for p in problems)
    assert any(p.startswith("a.md: claimed by one, two") for p in problems)
    assert any(p.startswith("orphan.md: header names gen_x.py") for p in problems)
    assert len(problems) == 3


# ── hand-authored check ──────────────────────────────────────────────────────


@pytest.mark.allow_real_subprocess  # git ls-files on this checkout
def test_real_hand_authored_files_name_no_legacy_toolkit_path():
    assert c.hand_authored_legacy_references(c.REPO) == []


def hand(root, files, exempt=None, table=None):
    return c.hand_authored_legacy_references(
        root,
        files=files,
        exempt={} if exempt is None else exempt,
        table={} if table is None else table,
    )


def test_legacy_toolkit_path_in_a_hand_authored_file_is_reported(tmp_path):
    rel = write(tmp_path, "skills/x/SKILL.md", "run ~/.claude/scripts/grill.py\n")
    problems = hand(tmp_path, [rel])
    assert len(problems) == 1
    assert problems[0].startswith("skills/x/SKILL.md:1: .claude/scripts/grill.py")


def test_ts_join_to_a_legacy_toolkit_path_is_reported(tmp_path):
    rel = write(
        tmp_path,
        "pi/extensions/x.ts",
        'const P = join(homedir(), ".claude", "scripts", "dev_status.py");\n',
    )
    assert len(hand(tmp_path, [rel])) == 1


def test_harness_and_foreign_paths_in_a_hand_authored_file_pass(tmp_path):
    rel = write(
        tmp_path,
        "a.md",
        "~/.claude/commands/x.md ~/.claude/scripts/dev_status_sync.py\n"
        "~/.agent-toolkit/scripts/grill.py\n",
    )
    assert hand(tmp_path, [rel]) == []


def test_generated_output_test_tree_and_exempt_file_are_not_scanned(tmp_path):
    legacy = "~/.claude/scripts/grill.py\n"
    files = [
        write(tmp_path, "out.md", legacy),
        write(tmp_path, "test/test_x.py", legacy),
        write(tmp_path, "pi/test/x.test.ts", legacy),
        write(tmp_path, "agent-scripts/y.py", legacy),
    ]
    problems = hand(
        tmp_path,
        files,
        exempt={"agent-scripts/y.py": "owned elsewhere"},
        table={"g": gen(["out.md"])},
    )
    assert problems == []


def test_stale_exemption_is_reported(tmp_path):
    rel = write(tmp_path, "agent-scripts/y.py", "no legacy paths here\n")
    problems = hand(tmp_path, [rel], exempt={"agent-scripts/y.py": "owned elsewhere"})
    assert len(problems) == 1
    assert "agent-scripts/y.py" in problems[0]
    assert "stale" in problems[0]


def test_hand_authored_cli_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        c, "hand_authored_legacy_references", lambda repo: ["a.md:1: x: legacy"]
    )
    assert c.main(["hand-authored"]) == 1
    assert "a.md:1" in capsys.readouterr().out
    monkeypatch.setattr(c, "hand_authored_legacy_references", lambda repo: [])
    assert c.main(["hand-authored"]) == 0


@pytest.mark.allow_real_subprocess  # git ls-files on this checkout
def test_exemption_rows_name_single_tracked_files():
    """A wildcard row once hid live code (session-start hook commands, user
    hints) behind a docstring-only owner: each row must name one file, so
    its owner and its staleness are exact."""
    tracked = set(c.tracked_files(c.REPO))
    for pattern in c.LEGACY_PATH_EXEMPT:
        assert not any(ch in pattern for ch in "*?["), pattern
        assert pattern in tracked, pattern


# ── links: rows that install into a harness home they do not serve ──────────


def row(dest, harness=None, **kwargs):
    from link_inspect import LinkSpec

    return LinkSpec(src="x", dest=dest, harness=harness, **kwargs)


def test_real_links_install_into_no_foreign_harness_home():
    assert c.links_check(c.REPO) == []


def test_harness_homes_cover_every_harness():
    import harness_spec

    assert set(c.HARNESS_HOMES) == set(harness_spec.ALL_NAMES)


@pytest.mark.parametrize(
    "spec",
    [
        row("~/.claude/icons"),
        row("~/.claude/scripts/dev_status.py"),
        row("~/.claude/hooks/agy-elapsed.js", harness="agy"),
        row("~/.claude"),
        row("~/.config/opencode/plugin/x.ts", harness="pi"),
        row("~/.copilot/skills", harness="claude", dir=True),
    ],
    ids=lambda s: f"{s.harness}:{s.dest}",
)
def test_row_in_a_harness_home_it_does_not_serve_fails(spec):
    problems = c.harness_home_violations([spec])
    assert len(problems) == 1
    assert spec.dest in problems[0]


@pytest.mark.parametrize(
    "spec",
    [
        row("~/.agent-toolkit/scripts/dev_status.py"),
        row("~/.agent-toolkit/icons"),
        row("~/.agent-toolkit/hooks/agy-elapsed.js", harness="agy"),
        row("~/.agent-tools.zsh"),
        row("~/.config/other/x"),
        row("~/.claude-other/x"),
        row("~/.pi-foo"),
        row("~/.claude/CLAUDE.md", harness="claude"),
        row("~/.copilot/hooks/session-start.json", harness="copilot", platform="mac"),
        row("~/.pi/agent/skills", harness="pi", dir=True),
    ],
    ids=lambda s: f"{s.harness}:{s.dest}",
)
def test_row_outside_foreign_harness_homes_passes(spec):
    assert c.harness_home_violations([spec]) == []


def test_links_cli_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(c, "links_check", lambda repo: ["links.toml: x -> y: bad"])
    assert c.main(["links"]) == 1
    assert "links.toml: x -> y" in capsys.readouterr().out
    monkeypatch.setattr(c, "links_check", lambda repo: [])
    assert c.main(["links"]) == 0
