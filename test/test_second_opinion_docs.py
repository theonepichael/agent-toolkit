#!/usr/bin/env python3
"""Keep the second-opinion guidance copies in sync.

The second-opinion contract lives in five hand-maintained copies
(claude/commands/second-opinion.md plus four sibling skill/command forms).
They have drifted before and each drift caused a real bug. This test asserts
every copy still carries the required contract markers, so a future edit to
one copy that forgets the model-pool rotation / no-pool safety net fails
loudly here. The single-source generator (meta-second-opinion-single-source)
is meant to replace these copies eventually; until then this holds the line.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every copy must carry all of these markers. Keep in sync with
# claude/commands/second-opinion.md (the canonical copy).
REQUIRED_MARKERS = (
    # the no-pool safety-net description
    "don't assume a pool is configured",
    # the POOL-config-error retry inside the loop
    "retry, no index",
    # the focus-hints mechanism (callers + risk areas)
    "--focus-file",
    # the caller-check rule for tooling-script changes
    "Caller check for tooling changes",
    # the backlog-recording step
    "Recording it in the backlog",
    # the plan-file path convention callers (backlog-item.md, grill-me.md,
    # spec.md) depend on
    "~/.claude/data/grill/<topic-slug>-plan.md",
    # the critique-notes companion-file path convention those same callers
    # depend on
    "-critique-notes.md",
)

# The usage block and the loop's per-round call are the two sections whose
# form is genuinely per-harness: five copies show runnable commands naming
# the CLI flags, while pi's name the tool's parameters instead, because pi
# calls the native second_opinion tool rather than shelling out. Both forms
# must still expose the model-index knob in both places — these are the
# markers for it. Everything else in REQUIRED_MARKERS is identical across
# every copy and stays there.
MODEL_INDEX_MARKERS = {
    "pi/prompts/second-opinion.md": ("modelIndex", "modelIndex = <round - 1>"),
    "pi/skills/second-opinion/SKILL.md": ("modelIndex", "modelIndex = <round - 1>"),
}
DEFAULT_MODEL_INDEX_MARKERS = ("[--model-index N]", "--model-index <round - 1>")

# Resolving the target plan reaches for a grill session's `plan_path`. Five
# copies say so as a `grill.py show` CLI invocation; pi's names its native
# `grill` tool's `show` action instead, because grill.py is not on
# pi/extensions/permission-gate.ts's bash allowlist — a bash call to it there
# stalls on a confirmation prompt or fails outright headless.
GRILL_LOOKUP_MARKERS = {
    "pi/prompts/second-opinion.md": ("`grill` tool's `show` action",),
    "pi/skills/second-opinion/SKILL.md": ("`grill` tool's `show` action",),
}
DEFAULT_GRILL_LOOKUP_MARKERS = ("`grill.py show`",)

COPIES = (
    "claude/commands/second-opinion.md",
    "opencode/command/second-opinion.md",
    "opencode/skills/second-opinion/SKILL.md",
    "copilot/skills/second-opinion/SKILL.md",
    "agy/skills/second-opinion/SKILL.md",
    "pi/prompts/second-opinion.md",
    "pi/skills/second-opinion/SKILL.md",
)


def missing_markers(rel_path: str, text: str) -> list[str]:
    """Return the contract markers absent from one copy's ``text``.

    Shared with ``claude/scripts/test_gen_second_opinion.py``'s end-to-end
    check so the marker logic has one source of truth.

    Markers are matched whitespace-insensitively: gen_second_opinion.py
    reflows every prose paragraph with textwrap.fill, so any phrase can be
    split across a line break depending on unrelated wording elsewhere in
    the paragraph. Matching on collapsed whitespace checks the contract
    wording itself, not where the wrapper happened to break a line.
    """
    index_markers = MODEL_INDEX_MARKERS.get(rel_path, DEFAULT_MODEL_INDEX_MARKERS)
    grill_markers = GRILL_LOOKUP_MARKERS.get(rel_path, DEFAULT_GRILL_LOOKUP_MARKERS)
    flat_text = " ".join(text.split())
    return [
        m
        for m in (*REQUIRED_MARKERS, *index_markers, *grill_markers)
        if " ".join(m.split()) not in flat_text
    ]


@pytest.mark.parametrize("rel_path", COPIES)
def test_second_opinion_copy_carries_contract(rel_path: str) -> None:
    path = REPO_ROOT / rel_path
    assert path.exists(), f"missing second-opinion copy: {rel_path}"
    text = path.read_text(encoding="utf-8")
    missing = missing_markers(rel_path, text)
    assert not missing, (
        f"{rel_path} is missing second-opinion contract markers: {missing}"
    )
