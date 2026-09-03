"""Guards the Pi question-tool capability facts across the repo.

`pi/extensions/question-tool.ts` supplies a structured multi-choice
``question`` tool for interactive Pi sessions. Several repo files were
written before it landed and still claim the opposite -- that Pi has no
structured-choice mechanism and so must ask enumerable questions in plain
text. `claude/global-instructions.md` is read by every harness (symlinked to
each harness's instruction file), so the stale claim was the worst offender;
the hand-maintained `pi/prompts/*.md` ports repeated it.

Why these assertions look where they do:

* The global-instructions check parses the actual sentence that carries the
  claim -- the "harnesses without one (...)" parenthetical in the
  Judgment-calls section -- and compares the *set* of harness names, not
  exact prose, so harmless editorial changes (reordering, rephrasing) don't
  false-positive. A break here after a *legitimate* change (e.g. Copilot
  gains a widget) is a feature: the list should be updated consciously.
* opencode legitimately contains the string "`question` tool", so the
  global-instructions check keys on the parenthetical and on Pi appearing
  *outside* it, never on a whole-file grep.
* `make-skill.md` intentionally keeps plain text as the default for
  *authored* skills: they live in `agy/skills/` and are read by agy as
  well as Pi, so a skill must not depend on a widget only Pi has. The
  check therefore guards the *justification* (agy-sharing, hedged "at
  present"), not the plain-text guidance itself.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_global_instructions_do_not_list_pi_without_a_widget() -> None:
    text = _read("claude/global-instructions.md")
    section = re.search(r"### Judgment calls.*?(?=\n### |\Z)", text, re.DOTALL).group(0)

    without = re.search(r"harnesses\s+without\s+one\s+\(([^)]*)\)", section)
    assert without is not None, "judgment-calls section lost its without-widget list"
    names = {name.strip() for name in without.group(1).split(",")}
    assert names == {"Copilot CLI", "agy"}

    # Pi must now appear in the with-widget list (i.e. outside the
    # parenthetical we just checked), with the -ne/headless caveats.
    remainder = section.replace(without.group(0), "")
    assert "Pi" in remainder
    assert "`question` tool" in remainder
    assert "--no-extensions" in remainder
    assert "headless" in remainder


def test_readme_does_not_claim_pi_lacks_a_structured_prompt() -> None:
    text = _read("README.md")
    assert "has no structured multi-choice prompt either" not in text
    assert "`question` tool" in text


def test_gen_second_opinion_comments_do_not_claim_pi_lacks_a_widget() -> None:
    text = _read("claude/scripts/gen_second_opinion.py")
    assert "no structured multi-choice widget for Pi" not in text


def test_prompt_ports_route_enumerable_choices_to_the_question_tool() -> None:
    # The stale conclusion was "state them in plain text because Pi has no
    # structured-choice mechanism". The premise that Pi ships no *built-in*
    # tool is still true and correctly framed in the fixed files; these
    # assertions target the conclusions that were wrong.
    stale_conclusions = {
        "pi/prompts/spec.md": "state them in plain text",
        "pi/prompts/to-tickets.md": "State the options in plain text",
        "pi/prompts/grill-me.md": "don't design this step around one",
    }
    for relpath, stale in stale_conclusions.items():
        text = _read(relpath)
        assert "`question` tool" in text, (
            f"{relpath} still routes choices to plain text"
        )
        assert stale not in text, (
            f"{relpath} still carries the stale plain-text conclusion"
        )


def test_generated_pi_to_tickets_skill_routes_choices_to_the_question_tool() -> None:
    # pi/skills/to-tickets/SKILL.md is generated from gen_skills_params.py's
    # pi row; it must match the fixed pi/prompts/to-tickets.md convention:
    # question tool first, plain text only where the tool is absent.
    text = _read("pi/skills/to-tickets/SKILL.md")
    assert "`question` tool" in text, (
        "generated pi to-tickets skill still routes choices to plain text"
    )
    assert "State the options in plain text" not in text, (
        "generated pi to-tickets skill still leads with plain text before "
        "the question tool"
    )
    assert "only in a session where the tool is genuinely absent" in text, (
        "generated pi to-tickets skill lost the plain-text-only-as-fallback framing"
    )


def test_make_skill_keeps_plain_text_only_for_agy_shared_skills() -> None:
    text = _read("pi/prompts/make-skill.md")
    # The stale justification ("Pi's built-in tools... no built-in
    # question/select tool among them... must be written as plain
    # conversational text") is gone.
    assert "no built-in question/select tool" not in text
    assert "must be written as plain conversational text" not in text
    # The honest rationale is present and hedged for future agy changes.
    assert "structured-choice widget" in text
    assert text.count("agy") >= 2
