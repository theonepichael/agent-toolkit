#!/usr/bin/env python3
"""Tests for agent-scripts/herdr_picker.py — the Python twin of
pi/extensions/swarm-lib/swarm-picker.ts.

The shared fixtures in test/fixtures/picker/ are iterated here and by
pi/test/swarm-tool.test.ts; a fixture without an expected file (or an expected
file without a fixture) fails both suites. Pure functions, no I/O, no herdr.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import herdr_picker  # noqa: E402


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "picker"


def _fixture_pairs() -> list[tuple[Path, Path]]:
    # Shared selection-mode fixtures, iterated by both ports. The generated
    # edit-*.txt set has a different shape and is read by parse_edit_mode only.
    txts = sorted(p for p in FIXTURE_DIR.glob("*.txt") if not p.name.startswith("edit-"))
    return [(txt, txt.with_suffix(".expected.json")) for txt in txts]


def _edit_fixture_pairs() -> list[tuple[Path, Path]]:
    txts = sorted(FIXTURE_DIR.glob("edit-*.txt"))
    return [(txt, txt.with_suffix(".expected.json")) for txt in txts]


def test_every_txt_has_an_expected_file() -> None:
    txts = {p.stem for p in FIXTURE_DIR.glob("*.txt")}
    expected = {p.stem.removesuffix(".expected") for p in FIXTURE_DIR.glob("*.expected.json")}
    assert not (txts - expected), f"fixtures missing .expected.json: {txts - expected}"


def test_every_expected_file_has_a_txt() -> None:
    txts = {p.stem for p in FIXTURE_DIR.glob("*.txt")}
    expected = {p.stem.removesuffix(".expected") for p in FIXTURE_DIR.glob("*.expected.json")}
    assert not (expected - txts), f"expected files with no .txt: {expected - txts}"


def test_shared_fixtures_parse_and_match_and_navigate() -> None:
    for txt, expected_path in _fixture_pairs():
        content = txt.read_text(encoding="utf-8")
        spec = json.loads(expected_path.read_text(encoding="utf-8"))
        parsed = herdr_picker.parse_picker(content)
        assert parsed.selected_index == spec["selectedIndex"], (
            f"{txt.stem}: selectedIndex {parsed.selected_index} != {spec['selectedIndex']}"
        )
        labels = [o.label for o in parsed.options]
        assert labels == spec["options"], f"{txt.stem}: {labels} != {spec['options']}"
        for case in spec["match"]:
            got = herdr_picker.match_option(case["answer"], parsed.options)
            got_label = got.label if got else None
            assert got_label == case["expect"], (
                f"{txt.stem}: matchOption({case['answer']!r}) = {got_label!r}, "
                f"expected {case['expect']!r}"
            )
        for nav in spec["navigation"]:
            assert herdr_picker.navigation_keys(nav["from"], nav["to"]) == nav["arrows"], (
                f"{txt.stem}: navigation_keys({nav['from']}, {nav['to']}) != {nav['arrows']}"
            )


def test_edit_fixtures_match_parse_edit_mode() -> None:
    for txt, expected_path in _edit_fixture_pairs():
        spec = json.loads(expected_path.read_text(encoding="utf-8"))
        parsed = herdr_picker.parse_edit_mode(txt.read_text(encoding="utf-8"))
        assert parsed is not None, f"{txt.stem}: not an edit-mode picker"
        assert [o.label for o in parsed.options] == spec["options"], txt.stem
        assert parsed.marked_index == spec["markedIndex"], txt.stem
        assert parsed.complete == spec["complete"], txt.stem
        if spec["complete"]:
            got = re.sub(r"\s+", "", "".join(parsed.editor_text_lines))
            want = re.sub(r"\s+", "", spec["editorText"])
            assert got == want, f"{txt.stem}: editor text {got!r} != {want!r}"


@pytest.mark.allow_real_subprocess
def test_edit_fixtures_match_a_fresh_render() -> None:
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    repo = Path(__file__).resolve().parent.parent
    script = repo / "pi" / "test" / "render-picker-fixtures.ts"
    result = subprocess.run(
        ["node", "--import", "tsx", str(script), "--check"],
        cwd=repo / "pi",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_needs_human_repro_is_not_a_parser_bug() -> None:
    # The 2026-10-14 misclassification reproduced: a capture with a footer and
    # closing rule but no opening rule. parse_picker must refuse it (no options)
    # -- there is genuinely no complete picker in the frame. See the fixture
    # README for the finding (a read-timing race, not a parser defect).
    content = (FIXTURE_DIR / "needs-human-repro.txt").read_text(encoding="utf-8")
    assert herdr_picker.parse_picker(content).options == []
    # The fully-rendered commit picker parses, so the refusal above is the
    # frame, not the parser.
    full = (FIXTURE_DIR / "numbered-plan.txt").read_text(encoding="utf-8")
    assert [o.label for o in herdr_picker.parse_picker(full).options] == [
        "Commit now (Recommended)",
        "Stop here",
        "Something else (type it)",
    ]


def _edit_mode_screen(*, width: int = 42, text: str = "") -> str:
    """Hand-built edit-mode picker, shaped like question-tool.ts's render."""
    rule = "─" * width
    editor_rule = " " + "─" * (width - 2)
    lines = [rule]
    lines.append(" Land (1/1)")
    lines.append(" Commit these changes?")
    lines.append("")
    lines.append("  1. Yes, commit (Recommended)")
    lines.append("     Commit the staged changes now.")
    lines.append("> 2. Something else (type it) ✎")
    lines.append("     Answer in your own words instead.")
    lines.append("")
    lines.append(" Your answer:")
    lines.append(editor_rule)
    if text:
        lines.append(f" {text}")
    else:
        lines.append(" ")
    lines.append(editor_rule)
    lines.append("")
    lines.append(" Enter to submit • Esc to go back")
    lines.append(rule)
    return "\n".join(lines) + "\n"


def test_parse_edit_mode_recognises_the_editor_and_options() -> None:
    text = "Foreman review: read /x and act on it"
    screen = _edit_mode_screen(text=text)
    parsed = herdr_picker.parse_edit_mode(screen)
    assert parsed is not None
    assert parsed.complete is True
    assert parsed.marked_index == 2
    assert [o.label for o in parsed.options] == [
        "Yes, commit (Recommended)",
        "Something else (type it)",
    ]
    assert parsed.editor_text_lines == [text]


def test_parse_edit_mode_returns_none_for_selection_mode() -> None:
    screen = (FIXTURE_DIR / "numbered-plan.txt").read_text(encoding="utf-8")
    assert herdr_picker.parse_edit_mode(screen) is None


def test_parse_edit_mode_strips_the_marker_so_identity_matches_parse_picker() -> None:
    # Identity in both modes is the ordered labels with ✎ stripped: parse_picker
    # on the same edit-mode screen must agree with parse_edit_mode.
    screen = _edit_mode_screen(text="Foreman review: read /x and act on it")
    picker_options = [o.label for o in herdr_picker.parse_picker(screen).options]
    edit_options = [o.label for o in herdr_picker.parse_edit_mode(screen).options]
    assert picker_options == edit_options


def test_parse_edit_mode_scrolled_or_incomplete_is_not_complete() -> None:
    # A scroll indicator on the editor border -> complete is False.
    screen = _edit_mode_screen(text="x").replace(
        " " + "─" * 40,
        " ─── ↓ 2 more ────────────────────────────",
    )
    parsed = herdr_picker.parse_edit_mode(screen)
    assert parsed is not None
    assert parsed.complete is False


def test_match_option_excludes_the_free_text_escape_always() -> None:
    options = herdr_picker.parse_picker(
        (FIXTURE_DIR / "real.txt").read_text(encoding="utf-8")
    ).options
    assert herdr_picker.match_option("something else", options) is None
    assert herdr_picker.match_option("type it", options) is None


def test_navigation_keys_are_arrows_only_without_enter() -> None:
    assert herdr_picker.navigation_keys(1, 3) == ["down", "down"]
    assert herdr_picker.navigation_keys(3, 1) == ["up", "up"]
    assert herdr_picker.navigation_keys(2, 2) == []
