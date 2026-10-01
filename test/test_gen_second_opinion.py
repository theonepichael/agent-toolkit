#!/usr/bin/env python3
"""Tests for gen_second_opinion.py. Run with: python3 test_gen_second_opinion.py

Two kinds of coverage: small unit tests for the render function against a
tiny fixture template, and end-to-end tests that assert the committed
copies, the contract-shape check, the guard-phrase check, and the
row-comment check all currently pass against this repo's real
templates/second_opinion.md.tmpl and INTERFACES.md — so a forgotten
regeneration, a dropped flag, or an unnoted table edit fails the suite
instead of drifting quietly.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
import gen_second_opinion as gso

REPO_ROOT = Path(__file__).resolve().parents[1]

FIXTURE_PARAMS = gso.HarnessParams(
    frontmatter="---\nname: x\n---\n",
    backends_never="`a`/`b`",
    backends_none="neither `a` nor `b`",
    target_source_opening="Opening sentence.",
    target_source_echo="an echo",
    ask_choose_overwrite="ask overwrite",
    ask_choose_cap="ask cap",
    instructions_ref="the docs'",
    adversary_ref="the",
    io_entrypoint="the fixture entrypoint",
    usage_block="```\nfixture usage   block\n```",
    review_call="fixture review call",
    review_call_retry="fixture retry call",
    grill_plan_lookup="the fixture plan lookup",
)


class RenderBodyTests(unittest.TestCase):
    def test_substitutes_every_placeholder(self) -> None:
        template = (
            "never {{BACKENDS_NEVER}} nor {{BACKENDS_NONE}}.\n\n"
            "{{TARGET_SOURCE_OPENING}} echo: {{TARGET_SOURCE_ECHO}}.\n\n"
            "{{ASK_CHOOSE_OVERWRITE}} / {{ASK_CHOOSE_CAP}}.\n\n"
            "apply {{INSTRUCTIONS_REF}} rule, blame {{ADVERSARY_REF}} agent.\n"
        )
        rendered = gso.render_body(template, FIXTURE_PARAMS)
        self.assertNotIn("{{", rendered)
        self.assertIn("never `a`/`b` nor neither `a` nor `b`.", rendered)
        self.assertIn("Opening sentence. echo: an echo.", rendered)
        self.assertIn("ask overwrite / ask cap.", rendered)
        self.assertIn("apply the docs' rule, blame the agent.", rendered)

    def test_code_fences_pass_through_unwrapped(self) -> None:
        template = "prose {{BACKENDS_NEVER}}.\n\n```\nliteral   spacing\ncode\n```\n"
        rendered = gso.render_body(template, FIXTURE_PARAMS)
        self.assertIn("```\nliteral   spacing\ncode\n```", rendered)

    def test_a_lone_placeholder_line_is_emitted_verbatim(self) -> None:
        """A whole-line placeholder is not reflowed, so its value can carry
        its own fence, indentation, and line breaks — that is what lets one
        harness render a fenced command block where another renders prose."""
        template = "before.\n\n{{USAGE_BLOCK}}\n\nafter.\n"
        rendered = gso.render_body(template, FIXTURE_PARAMS)
        self.assertIn("```\nfixture usage   block\n```", rendered)

    def test_a_lone_placeholder_value_does_not_break_fence_tracking(self) -> None:
        """The emitted value's own ``` markers must not be read as opening a
        fence in the template, or every later paragraph stops being wrapped."""
        long_line = "word " * 40
        template = f"{{{{USAGE_BLOCK}}}}\n\n{long_line.strip()}\n"
        rendered = gso.render_body(template, FIXTURE_PARAMS)
        body_lines = [ln for ln in rendered.splitlines() if ln.startswith("word")]
        self.assertTrue(body_lines, rendered)
        self.assertTrue(all(len(ln) <= gso.WRAP_WIDTH for ln in body_lines), body_lines)

    def test_placeholders_inside_a_fence_are_substituted(self) -> None:
        """Fenced pseudocode still needs per-harness call forms substituted in,
        while everything else in the fence keeps its literal spacing."""
        template = "```\n    critique = {{REVIEW_CALL}}\n    literal   spacing\n```\n"
        rendered = gso.render_body(template, FIXTURE_PARAMS)
        self.assertIn("    critique = fixture review call", rendered)
        self.assertIn("    literal   spacing", rendered)
        self.assertNotIn("{{", rendered)

    def test_headings_are_not_wrapped_or_altered(self) -> None:
        template = "## A heading that names {{BACKENDS_NEVER}} plainly\n\nbody.\n"
        rendered = gso.render_body(template, FIXTURE_PARAMS)
        self.assertIn("## A heading that names `a`/`b` plainly", rendered)

    def test_long_paragraph_reflows_to_wrap_width(self) -> None:
        long_value = "x" * 200
        params = gso.HarnessParams(
            frontmatter="---\n---\n",
            backends_never=long_value,
            backends_none="n",
            target_source_opening="o",
            target_source_echo="e",
            ask_choose_overwrite="ow",
            ask_choose_cap="cap",
            instructions_ref="ref",
            adversary_ref="adv",
            io_entrypoint="entry",
            usage_block="```\nusage\n```",
            review_call="call",
            review_call_retry="retry",
            grill_plan_lookup="lookup",
        )
        rendered = gso.render_body("never {{BACKENDS_NEVER}} directly.\n", params)
        for line in rendered.splitlines():
            self.assertLessEqual(len(line), gso.WRAP_WIDTH + len(long_value))

    def test_render_file_places_marker_after_frontmatter(self) -> None:
        text = gso.render_file("body text.\n", "x", FIXTURE_PARAMS)
        self.assertEqual(
            text,
            "---\nname: x\n---\n\n" + gso.DO_NOT_EDIT_MARKER + "\n\nbody text.\n",
        )


class ContractShapeCheckTests(unittest.TestCase):
    def test_contract_shape_flags_a_missing_token(self) -> None:
        problems = gso.check_contract_shape(REPO_ROOT, "no flags mentioned here")
        self.assertTrue(problems)
        self.assertTrue(any("detect" in p for p in problems))

    def test_contract_shape_passes_when_every_token_present(self) -> None:
        template = " ".join(gso.CONTRACT_TOKENS)
        self.assertEqual(gso.check_contract_shape(REPO_ROOT, template), [])


class EndToEndTests(unittest.TestCase):
    def test_copies_are_not_stale(self) -> None:
        rendered = gso.render_all(REPO_ROOT)
        self.assertEqual(set(rendered), set(gso.HARNESS_TABLE))
        for relpath, expected in rendered.items():
            committed = (REPO_ROOT / relpath).read_text(encoding="utf-8")
            self.assertEqual(
                committed,
                expected,
                f"{relpath} is stale — run "
                "`python3 agent-scripts/gen_second_opinion.py`",
            )

    def test_contract_shape_holds_against_the_real_template(self) -> None:
        template_text = (REPO_ROOT / gso.TEMPLATE_PATH).read_text(encoding="utf-8")
        self.assertEqual(gso.check_contract_shape(REPO_ROOT, template_text), [])

    def test_every_harness_table_row_has_a_freshness_comment(self) -> None:
        self.assertEqual(gso.check_row_comments(REPO_ROOT), [])

    def test_each_copy_still_carries_the_required_contract_markers(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "test"))
        import test_second_opinion_docs as docs_test

        for relpath in docs_test.COPIES:
            text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
            missing = docs_test.missing_markers(relpath, text)
            self.assertFalse(missing, f"{relpath} missing markers: {missing}")



class PiProbeWordingTests(unittest.TestCase):
    """Pi reaches probe through its native tool, so its copies must name the
    tool's `probe` action and `backend` parameter, not the script's flag."""

    PI_COPIES = ("pi/prompts/second-opinion.md", "pi/skills/second-opinion/SKILL.md")

    def test_pi_copies_name_the_probe_action_and_backend_parameter(self) -> None:
        for relpath in self.PI_COPIES:
            text = " ".join((REPO_ROOT / relpath).read_text().split())
            self.assertIn("Action `probe` reports per-model availability", text)
            self.assertIn("pass the `backend` parameter deliberately", text)
            self.assertNotIn("pass `--backend` deliberately", text)

    def test_shell_harnesses_keep_the_backend_flag(self) -> None:
        for relpath, params in gso.HARNESS_TABLE.items():
            if relpath in self.PI_COPIES:
                continue
            self.assertEqual(gso.substitutions(params)["PROBE_BACKEND_REF"], "`--backend`")


class PiToolParameterWordingTests(unittest.TestCase):
    """Pi reaches second_opinion.py only through its native tool, so its
    copies must name the tool's parameters wherever the shared template names
    a script flag the tool exposes -- a Pi agent told to pass `--dir` has no
    such knob to turn."""

    PI_COPIES = PiProbeWordingTests.PI_COPIES
    EXPOSED_FLAGS = (
        "--backend",
        "--dir",
        "--focus-file",
        "--model-index",
        "--text-only",
        "--run-id",
    )
    # Mentions that stay literal on purpose: the quoted script error text Pi
    # receives verbatim through the tool, and a sentence about the flag's
    # own history.
    LITERAL_ALLOWLIST = (
        "`--model-index ... requires ... POOL ...`",
        '"--model-index ... requires ... POOL ..."',
        "the `--model-index` hard-error ship",
        "with `--allow-extra-round` themselves, from a shell outside Pi",
    )

    def flat(self, relpath: str) -> str:
        return " ".join((REPO_ROOT / relpath).read_text().split())

    def test_pi_copies_name_no_exposed_script_flag(self) -> None:
        for relpath in self.PI_COPIES:
            text = self.flat(relpath)
            for phrase in self.LITERAL_ALLOWLIST:
                text = text.replace(phrase, "")
            for flag in (*self.EXPOSED_FLAGS, "--quiet", "--allow-extra-round"):
                with self.subTest(relpath=relpath, flag=flag):
                    self.assertNotIn(flag, text)

    def test_pi_copies_name_the_tool_parameters(self) -> None:
        expected = (
            "(`dir` or current working directory)",
            "use `textOnly = true` to opt out",
            "retry with `textOnly = true`",
            "pass `focusFile = <that path>` to `review`",
            "Skip `focusFile` entirely",
            "`modelIndex` is a 0-based index",
            "without `modelIndex` the single override",
            "use `backend = <configured-backend>` to target",
            "target the backend with `backend = <name>`",
            "run `second_opinion.py review` with `--allow-extra-round` "
            "themselves, from a shell outside Pi",
            "the `second_opinion` tool has no quiet parameter",
        )
        for relpath in self.PI_COPIES:
            text = self.flat(relpath)
            for phrase in expected:
                with self.subTest(relpath=relpath, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_pi_pseudocode_quiet_comment(self) -> None:
        for relpath in self.PI_COPIES:
            text = (REPO_ROOT / relpath).read_text()
            self.assertIn("# (always shown\n", text)
            self.assertIn("# by the tool) naming\n", text)

    def test_no_copy_keeps_an_unresolved_marker(self) -> None:
        for relpath in gso.HARNESS_TABLE:
            self.assertNotIn("{{", (REPO_ROOT / relpath).read_text(), relpath)

    def test_flag_table_matches_the_tool(self) -> None:
        import re

        source = (REPO_ROOT / "pi/extensions/second-opinion-tool.ts").read_text()
        emitted = dict(
            re.findall(r'params\.(\w+)(?: !== undefined)? \? \["(--[a-z-]+)"', source)
        )
        self.assertEqual(
            {param: flag for flag, param in gso.PI_TOOL_PARAMS.items()}, emitted
        )

    def test_render_flag_forms(self) -> None:
        pi = gso.HARNESS_TABLE["pi/prompts/second-opinion.md"]
        shell = gso.HARNESS_TABLE["claude/commands/second-opinion.md"]
        self.assertEqual(gso.render_flag("--dir", shell), "`--dir`")
        self.assertEqual(
            gso.render_flag("--backend <name>", shell), "`--backend <name>`"
        )
        self.assertEqual(gso.render_flag("--dir", pi), "`dir`")
        self.assertEqual(gso.render_flag("--text-only", pi), "`textOnly = true`")
        self.assertEqual(gso.render_flag("--backend <name>", pi), "`backend = <name>`")
        with self.assertRaisesRegex(ValueError, "--nope"):
            gso.render_flag("--nope", pi)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main(verbosity=1)
