#!/usr/bin/env python3
"""Behavioral twin of ``pi/extensions/swarm-lib/swarm-picker.ts``.

Pure parsing and navigation for ``question-tool.ts``'s rendered option picker
-- the same surface the swarm's blocked-relay reads off a worker pane, ported
to Python so ``herdr_delegate.py answer`` (the foreman's supported way to drive
a live worker's picker) and the Python test suite share one implementation.
No module state, no I/O. The fixtures live under ``test/fixtures/picker/`` next
to the TS suite's; both suites iterate the same files and must agree.

The two ports are deliberately kept line-for-line parallel so a drift is a
diff, not a semantics bug. ``navigation_keys`` returns the arrow prefix only
(the TS ``navigationKeys`` still appends ``"enter"``; parity is asserted on the
arrow prefix).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

OTHER_OPTION_LABEL = "Something else (type it)"
MIN_PARTIAL_ANSWER = 3

# question-tool.ts renders each option with a 2-visible-column prefix ("> "
# highlighted, "  " not) and wraps the label at renderWidth - 2, continuation
# lines carrying the same prefix; descriptions (and their continuations) carry
# a 5-space prefix. In a narrow pane the LABEL wraps too, so continuations are
# reattached keyed off the option line's number column (see DESCRIPTION_COLUMN
# below). Both "  1. X" and "> 1. X" put the number at column 2.
_OPTION_LINE = re.compile(r"^\s*(>)?\s*(\d+)\.\s+(.+?)\s*$")
_DESCRIPTION_COLUMN_OFFSET = 3
# A line that still SMELLS like an option but fails _OPTION_LINE (a multi-select
# picker's toggled row "[x] 2. Push only") must never be appended to the
# previous option's label as a continuation.
_SMELLS_LIKE_OPTION = re.compile(r"^\d+\.\s")
# The picker closes with one of three hint lines -- select, multi-select, and
# the free-text edit mode. Matched on the OPENING only: pi wraps the footer to
# the pane width before capture, and a full-sentence anchor missed in a 42-col
# pane (observed 2026-09-02).
_PICKER_FOOTER = re.compile(r"↑↓ navigate|Enter to submit")
# Full-width accent rule that wraps the whole block; the only mark in the pane
# that belongs to the live picker and nothing else.
_PICKER_RULE = re.compile(r"^─{3,}\s*$")
# The edit-mode footer, which also carries "Enter to submit" (matched by
# _PICKER_FOOTER) but is what parse_edit_mode anchors on to tell the two modes
# apart.
_ENTER_SUBMIT_FOOTER = re.compile(r"Enter to submit")
# question-tool.ts renders the OTHER option's label with a trailing " ✎" once
# edit mode is active; identity in both modes is the ordered labels with that
# marker stripped.
_MARKER = "✎"
# The edit-mode editor block is prefixed by a single space in the picker, so its
# borders are a leading space + a run of dashes (unlike the full-width picker
# rule, which has no leading space). A scroll indicator replaces some dashes
# with "─── ↑/↓ N more ".
_EDITOR_BORDER = re.compile(r"^ ─")
_EDITOR_BORDER_PLAIN = re.compile(r"^ ─{3,}\s*$")
_EDITOR_LABEL = "Your answer:"

_WORD_CHAR = re.compile(r"[a-z0-9]")


@dataclass(frozen=True)
class RenderedOption:
    """One option line: its 1-based index and its reassembled label."""

    index: int
    label: str


@dataclass(frozen=True)
class ParsedPicker:
    """A selection-mode picker: options and the currently highlighted index."""

    selected_index: int | None
    options: list[RenderedOption]


@dataclass(frozen=True)
class EditMode:
    """An edit-mode picker: options, the OTHER option's index, editor text."""

    options: list[RenderedOption]
    marked_index: int
    editor_text_lines: list[str]
    complete: bool


def _strip_marker(label: str) -> str:
    """Drop the trailing ``✎`` edit-mode marker (and any space before it)."""
    stripped = label.strip()
    if stripped.endswith(_MARKER):
        return stripped[: -len(_MARKER)].rstrip()
    return stripped


def _contains_as_word(haystack: str, needle: str) -> bool:
    """True when ``needle`` appears in ``haystack`` bounded by non-alphanumerics."""
    start = 0
    while True:
        at = haystack.find(needle, start)
        if at == -1:
            return False
        before = haystack[at - 1] if at > 0 else None
        after = haystack[at + len(needle)] if at + len(needle) < len(haystack) else None
        if not _is_word_char(before) and not _is_word_char(after):
            return True
        start = at + 1


def _is_word_char(char: str | None) -> bool:
    return char is not None and bool(_WORD_CHAR.match(char))


def _editor_block(
    lines: list[str], opening: int, footer: int
) -> tuple[int, int, int] | None:
    """Locate the edit-mode editor block: (label, top border, bottom border).

    Returns None when the screen has no ``Your answer:`` label (i.e. it is not
    in edit mode). The editor block is what ``parse_picker`` must skip and what
    ``parse_edit_mode`` additionally reads.
    """
    label_idx = -1
    for i in range(opening + 1, footer):
        if _EDITOR_LABEL in lines[i]:
            label_idx = i
            break
    if label_idx == -1:
        return None
    editor_top = -1
    for j in range(label_idx + 1, footer):
        if _EDITOR_BORDER.match(lines[j]):
            editor_top = j
            break
    if editor_top == -1:
        return None
    editor_bottom = -1
    for j in range(editor_top + 1, footer):
        if _EDITOR_BORDER.match(lines[j]):
            editor_bottom = j
            break
    if editor_bottom == -1:
        return None
    return (label_idx, editor_top, editor_bottom)


def parse_picker(content: str) -> ParsedPicker:
    """Parse question-tool.ts's rendered picker (plain text, no ANSI).

    Anchored to the LAST rendered picker: anything unexpected inside the window
    yields no options at all, and the caller falls back to needs_manual.
    """
    lines = content.split("\n")

    footer = -1
    for i in range(len(lines) - 1, -1, -1):
        if _PICKER_FOOTER.search(lines[i]):
            footer = i
            break
    if footer == -1:
        return ParsedPicker(None, [])

    opening = -1
    for i in range(footer - 1, -1, -1):
        if _PICKER_RULE.match(lines[i]):
            opening = i
            break
    if opening == -1:
        return ParsedPicker(None, [])

    options: list[RenderedOption] = []
    selected: list[int] = []
    baseline = 0
    editor = _editor_block(lines, opening, footer)
    skip_lo, skip_hi = (editor[0], editor[2]) if editor else (footer + 1, footer)
    for line_idx, line in enumerate(lines[opening + 1 : footer], start=opening + 1):
        if skip_lo <= line_idx <= skip_hi:
            continue
        match = _OPTION_LINE.match(line)
        if match:
            options.append(RenderedOption(int(match.group(2)), match.group(3)))
            if match.group(1) == ">":
                selected.append(int(match.group(2)))
            baseline = line.index(match.group(2))
            continue
        if not options:
            continue
        trimmed = line.strip()
        if not trimmed:
            continue
        indent = len(line) - len(line.lstrip())
        if indent >= baseline + _DESCRIPTION_COLUMN_OFFSET or _SMELLS_LIKE_OPTION.match(
            trimmed
        ):
            continue
        last = options[-1]
        options[-1] = RenderedOption(last.index, f"{last.label} {trimmed}")

    contiguous = bool(options) and all(
        option.index == i + 1 for i, option in enumerate(options)
    )
    if not contiguous:
        return ParsedPicker(None, [])

    stripped = [RenderedOption(o.index, _strip_marker(o.label)) for o in options]
    return ParsedPicker(selected[0] if len(selected) == 1 else None, stripped)


def match_option(
    answer: str, options: Sequence[RenderedOption]
) -> RenderedOption | None:
    """Match a free-text answer to a listed option.

    Case-insensitive exact match first, then a whole-word fragment match, both
    excluding the always-present free-text escape option. Returns None on no
    match or an ambiguous match -- never guesses.
    """
    candidates = [o for o in options if o.label.lower() != OTHER_OPTION_LABEL.lower()]
    needle = answer.strip().lower()
    if not needle:
        return None

    exact = [o for o in candidates if o.label.lower() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(needle) < MIN_PARTIAL_ANSWER:
        return None

    partial = [o for o in candidates if _contains_as_word(o.label.lower(), needle)]
    if len(partial) == 1:
        return partial[0]

    stripped_needle = re.sub(r"\s+", "", needle)
    stripped = [
        o for o in candidates if stripped_needle in re.sub(r"\s+", "", o.label.lower())
    ]
    if len(stripped) == 1:
        return stripped[0]

    return None


def navigation_keys(from_index: int, to_index: int) -> list[str]:
    """Arrow-key presses to move from one option to another (no trailing enter)."""
    steps = to_index - from_index
    key = "down" if steps > 0 else "up"
    return [key] * abs(steps)


def parse_edit_mode(content: str) -> EditMode | None:
    """Parse question-tool.ts's edit-mode picker (free-text entry).

    Returns None when the screen is not in edit mode (no ``Enter to submit``
    footer). The editor block (its top and bottom borders and the ``Your
    answer:`` label) is recognised and skipped, so editor borders are never
    taken for the picker rule and editor lines never fold into option labels.
    """
    lines = content.split("\n")

    footer = -1
    for i in range(len(lines) - 1, -1, -1):
        if _ENTER_SUBMIT_FOOTER.search(lines[i]):
            footer = i
            break
    if footer == -1:
        return None

    opening = -1
    for i in range(footer - 1, -1, -1):
        if _PICKER_RULE.match(lines[i]):
            opening = i
            break
    if opening == -1:
        return None

    editor_block = _editor_block(lines, opening, footer)
    if editor_block is None:
        return None
    label_idx, editor_top, editor_bottom = editor_block

    editor_text_lines = [
        lines[j].removeprefix(" ") for j in range(editor_top + 1, editor_bottom)
    ]

    complete = bool(
        _EDITOR_BORDER_PLAIN.match(lines[editor_top])
        and _EDITOR_BORDER_PLAIN.match(lines[editor_bottom])
    )

    options: list[RenderedOption] = []
    marked = -1
    baseline = 0
    for i in range(opening + 1, footer):
        if label_idx <= i <= editor_bottom:
            continue
        match = _OPTION_LINE.match(lines[i])
        if match:
            options.append(
                RenderedOption(int(match.group(2)), _strip_marker(match.group(3)))
            )
            if match.group(1) == ">":
                marked = int(match.group(2))
            baseline = lines[i].index(match.group(2))
            continue
        if not options:
            continue
        trimmed = lines[i].strip()
        if not trimmed:
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if indent >= baseline + _DESCRIPTION_COLUMN_OFFSET or _SMELLS_LIKE_OPTION.match(
            trimmed
        ):
            continue
        last = options[-1]
        options[-1] = RenderedOption(last.index, f"{last.label} {trimmed}")

    contiguous = bool(options) and all(
        option.index == i + 1 for i, option in enumerate(options)
    )
    if not contiguous or marked == -1:
        return None

    return EditMode(options, marked, editor_text_lines, complete)
