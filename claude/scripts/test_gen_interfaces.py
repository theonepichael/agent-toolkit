#!/usr/bin/env python3
"""Tests for gen_interfaces.py. Run with: python3 test_gen_interfaces.py

Exercises the extraction layer against small synthetic modules parsed with
`ast`, rather than asserting on INTERFACES.md's byte output — the document's
wording changes far more often than the facts pulled out of the source. Two
end-to-end tests are the exception: one parses the repo's real dev_status.py
(the module whose interface the hand-written INTERFACES.md got wrong), and one
asserts the committed INTERFACES.md is what the generator currently produces,
so a forgotten regeneration fails the suite instead of drifting quietly.

Nothing here imports or executes a harness module; every fixture is a string
parsed with ast.parse, exactly as the generator does it.
"""

import ast
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import gen_interfaces as gi

REPO_ROOT = Path(__file__).resolve().parents[2]


def parse(source: str) -> ast.Module:
    """Parse a dedented source fixture."""
    return ast.parse(textwrap.dedent(source))


def cli_of(source: str) -> gi.CliSpec:
    """Extract the CLI spec from a source fixture, failing if there is none."""
    tree = parse(source)
    spec = gi.extract_cli(tree, gi.first_paragraph(ast.get_docstring(tree)))
    assert spec is not None, "fixture was expected to define an argparse CLI"
    return spec


def subcommand(spec: gi.CliSpec, name: str) -> gi.Subcommand:
    """Return the subcommand with the given space-joined name."""
    for candidate in spec.subcommands:
        if candidate.name == name:
            return candidate
    raise AssertionError(
        f"no subcommand {name!r} in {[s.name for s in spec.subcommands]}"
    )


class TextHelperTests(unittest.TestCase):
    def test_first_paragraph_collapses_only_the_first_block(self) -> None:
        text = "line one\nline two\n\nsecond paragraph"
        self.assertEqual(gi.first_paragraph(text), "line one line two")

    def test_first_paragraph_of_nothing_is_empty(self) -> None:
        self.assertEqual(gi.first_paragraph(None), "")
        self.assertEqual(gi.first_paragraph("   "), "")

    def test_first_sentence_stops_at_terminal_punctuation(self) -> None:
        self.assertEqual(
            gi.first_sentence("Do the thing. Then do another thing."),
            "Do the thing.",
        )

    def test_first_sentence_without_punctuation_returns_whole_line(self) -> None:
        self.assertEqual(gi.first_sentence("no full stop here"), "no full stop here")

    def test_first_sentence_of_nothing_is_none(self) -> None:
        self.assertIsNone(gi.first_sentence(""))


class LiteralResolutionTests(unittest.TestCase):
    def resolve(self, expression: str, **bindings: str) -> str | None:
        node = ast.parse(expression, mode="eval").body
        return gi.literal_str(node, bindings)

    def test_plain_string(self) -> None:
        self.assertEqual(self.resolve("'hello'"), "hello")

    def test_bound_name(self) -> None:
        self.assertEqual(self.resolve("target", target="id"), "id")

    def test_unbound_name_is_unresolvable(self) -> None:
        self.assertIsNone(self.resolve("target"))

    def test_fstring_substitutes_bindings(self) -> None:
        self.assertEqual(
            self.resolve("f'required when <{name}> is numeric'", name="old_slug"),
            "required when <old_slug> is numeric",
        )

    def test_fstring_with_unresolvable_hole_is_unresolvable(self) -> None:
        self.assertIsNone(self.resolve("f'value is {compute()}'"))

    def test_concatenated_literals(self) -> None:
        self.assertEqual(self.resolve("'left ' + 'right'"), "left right")

    def test_runtime_call_is_unresolvable(self) -> None:
        self.assertIsNone(self.resolve("','.join(NAMES)"))

    def test_literal_sequence_of_constants(self) -> None:
        node = ast.parse("['a', 'b']", mode="eval").body
        self.assertEqual(gi.literal_sequence(node), ["a", "b"])

    def test_literal_sequence_of_a_call_is_none(self) -> None:
        node = ast.parse("sorted(VALID)", mode="eval").body
        self.assertIsNone(gi.literal_sequence(node))


class ArgumentRenderingTests(unittest.TestCase):
    def build(self, call_source: str, **bindings: str) -> gi.CliArgument:
        node = ast.parse(call_source, mode="eval").body
        assert isinstance(node, ast.Call)
        argument = gi.build_argument(node, bindings)
        assert argument is not None
        return argument

    def test_positional_uses_metavar_verbatim(self) -> None:
        argument = self.build("p.add_argument('id', metavar='<slug|N>')")
        self.assertEqual(argument.usage, "<slug|N>")
        self.assertEqual(argument.label, "id")

    def test_positional_without_metavar_is_angle_bracketed(self) -> None:
        self.assertEqual(self.build("p.add_argument('path')").usage, "<path>")

    def test_optional_positional_is_square_bracketed(self) -> None:
        argument = self.build("p.add_argument('decision_id', nargs='?')")
        self.assertEqual(argument.usage, "[<decision_id>]")

    def test_store_true_flag_takes_no_value(self) -> None:
        argument = self.build("p.add_argument('--apply', action='store_true')")
        self.assertEqual(argument.usage, "[--apply]")

    def test_required_flag_is_not_bracketed_and_is_noted(self) -> None:
        argument = self.build(
            "p.add_argument('--force', action='store_true', required=True)"
        )
        self.assertEqual(argument.usage, "--force")
        self.assertIn("required", argument.notes)

    def test_flag_value_falls_back_to_uppercased_dest(self) -> None:
        argument = self.build("p.add_argument('--focus-file', default=None)")
        self.assertEqual(argument.usage, "[--focus-file <FOCUS_FILE>]")

    def test_short_and_long_names_are_both_labelled(self) -> None:
        argument = self.build("p.add_argument('--session', '-s', default=None)")
        self.assertEqual(argument.label, "--session/-s")
        self.assertTrue(argument.usage.startswith("[--session"))

    def test_literal_choices_are_listed(self) -> None:
        argument = self.build("p.add_argument('--mode', choices=['a', 'b'])")
        self.assertIn("choices: a, b", argument.notes)

    def test_runtime_choices_are_flagged_not_guessed(self) -> None:
        argument = self.build("p.add_argument('--status', choices=sorted(VALID))")
        self.assertIn("choices computed at runtime", argument.notes)

    def test_non_none_default_is_noted(self) -> None:
        argument = self.build("p.add_argument('--retries', type=int, default=3)")
        self.assertIn("default: 3", argument.notes)

    def test_none_default_is_not_noted(self) -> None:
        argument = self.build("p.add_argument('--host', default=None)")
        self.assertEqual(argument.notes, [])

    def test_help_is_captured(self) -> None:
        argument = self.build("p.add_argument('--x', help='do the thing')")
        self.assertEqual(argument.help_text, "do the thing")


CLI_FIXTURE = """
    \"\"\"fixture.py — a small CLI.\"\"\"

    import argparse


    def _add_id_arg(parser, name="id"):
        parser.add_argument(name, metavar="<slug|N>")


    def _add_if_rev_arg(parser, id_name="id"):
        parser.add_argument(
            "--if-rev", type=int, default=None, metavar="<N>",
            help=f"required when <{id_name}> is numeric",
        )


    def main():
        parser = argparse.ArgumentParser(description="a small CLI")
        parser.add_argument("--verbose", action="store_true", help="be loud")
        sub = parser.add_subparsers(dest="cmd")

        sub.add_parser("render", help="render it")

        p = sub.add_parser("show", help="show one")
        _add_id_arg(p)

        p = sub.add_parser("rename", help="rename one")
        _add_id_arg(p, "old_slug")
        p.add_argument("new_slug")
        _add_if_rev_arg(p, "old_slug")

        nested = sub.add_parser("pending", help="nested group")
        nested_sub = nested.add_subparsers(dest="pending_cmd")
        nested_sub.add_parser("list", help="list them")
        q = nested_sub.add_parser("update", help="patch one")
        _add_id_arg(q)
"""


class CliExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = cli_of(CLI_FIXTURE)

    def test_root_description_and_global_option(self) -> None:
        self.assertEqual(self.spec.description, "a small CLI")
        self.assertEqual([o.label for o in self.spec.options], ["--verbose"])

    def test_all_subcommands_are_found_in_source_order(self) -> None:
        self.assertEqual(
            [s.name for s in self.spec.subcommands],
            ["render", "show", "rename", "pending", "pending list", "pending update"],
        )

    def test_bare_add_parser_without_assignment_is_registered(self) -> None:
        self.assertEqual(subcommand(self.spec, "render").help_text, "render it")

    def test_helper_added_arguments_are_attributed_to_the_caller(self) -> None:
        show = subcommand(self.spec, "show")
        self.assertEqual([a.usage for a in show.arguments], ["<slug|N>"])

    def test_helper_call_site_overrides_parameter_default(self) -> None:
        rename = subcommand(self.spec, "rename")
        if_rev = next(a for a in rename.arguments if a.label == "--if-rev")
        self.assertEqual(if_rev.help_text, "required when <old_slug> is numeric")

    def test_helper_default_is_used_when_call_site_omits_it(self) -> None:
        update = subcommand(self.spec, "pending update")
        self.assertEqual([a.usage for a in update.arguments], ["<slug|N>"])

    def test_argument_order_follows_source_order(self) -> None:
        rename = subcommand(self.spec, "rename")
        self.assertEqual(
            [a.usage for a in rename.arguments],
            ["<slug|N>", "<new_slug>", "[--if-rev <N>]"],
        )

    def test_nested_subparsers_get_a_joined_path(self) -> None:
        self.assertEqual(
            subcommand(self.spec, "pending list").path, ("pending", "list")
        )

    def test_description_can_come_from_the_module_docstring(self) -> None:
        spec = cli_of(
            """
            \"\"\"fixture docstring.\"\"\"

            import argparse


            def main():
                parser = argparse.ArgumentParser(description=__doc__)
            """
        )
        self.assertEqual(spec.description, "fixture docstring.")

    def test_module_without_argparse_has_no_cli(self) -> None:
        tree = parse("def helper():\n    return 1\n")
        self.assertIsNone(gi.extract_cli(tree, ""))

    def test_argument_parser_subclass_is_recognised(self) -> None:
        spec = cli_of(
            """
            import argparse


            class _Parser(argparse.ArgumentParser):
                pass


            def parse_args(argv):
                parser = _Parser(add_help=False)
                parser.add_argument("--profile", default="personal")
            """
        )
        self.assertEqual([o.label for o in spec.options], ["--profile"])
        self.assertIn("default: personal", spec.options[0].notes)

    def test_nested_helper_body_is_not_walked_as_caller_statements(self) -> None:
        # `add_flag`'s parameter is also named `p`; without skipping nested
        # function bodies its argument would land on whatever `p` points at.
        spec = cli_of(
            """
            import argparse


            def main():
                parser = argparse.ArgumentParser()
                sub = parser.add_subparsers()

                def add_flag(p):
                    p.add_argument("--session", help="which session")

                p = sub.add_parser("first")
                p = sub.add_parser("second")
                add_flag(p)
            """
        )
        self.assertEqual(subcommand(spec, "first").arguments, [])
        self.assertEqual(
            [a.label for a in subcommand(spec, "second").arguments], ["--session"]
        )


HANDLER_FIXTURE = """
    \"\"\"fixture.py — a small CLI with real handlers.\"\"\"

    import argparse


    def main():
        parser = argparse.ArgumentParser(description="a small CLI")
        sub = parser.add_subparsers(dest="cmd")

        sub.add_parser("render", help="render it")
        sub.add_parser("show", help="show one")

        nested = sub.add_parser("pending", help="nested group")
        nested_sub = nested.add_subparsers(dest="pending_cmd")
        nested_sub.add_parser("list", help="list them")
        nested_sub.add_parser("update", help="patch one")

        oos = sub.add_parser("out-of-scope", help="rejected concepts")
        oos_sub = oos.add_subparsers(dest="oos_cmd")
        oos_sub.add_parser("add", help="record one")

        sub.add_parser("broken", help="has no matching handler")


    def cmd_render(args):
        \"\"\"Render the thing.\"\"\"


    def cmd_show(args):
        pass


    def cmd_pending_list(args):
        \"\"\"List pending items.\"\"\"


    def cmd_pending_update(args):
        \"\"\"Patch one pending item.\"\"\"


    def cmd_out_of_scope_add(args):
        \"\"\"Record a rejected concept.\"\"\"
"""

FLAT_HANDLER_FIXTURE = """
    \"\"\"flat.py — a bare top-level-flags CLI, no subcommands.\"\"\"

    import argparse


    def main():
        parser = argparse.ArgumentParser(description="a flat CLI")
        parser.add_argument("--verbose", action="store_true")
"""


class HandlerMatchingTests(unittest.TestCase):
    """Leaf-vs-group handler docstring matching (contract-fingerprint feature)."""

    def setUp(self) -> None:
        self.tree = parse(HANDLER_FIXTURE)
        self.cli = cli_of(HANDLER_FIXTURE)

    def test_leaf_subcommand_paths_excludes_group_nodes(self) -> None:
        leaves = gi.leaf_subcommand_paths(self.cli.subcommands)
        self.assertNotIn(("pending",), leaves)
        self.assertNotIn(("out-of-scope",), leaves)
        self.assertIn(("render",), leaves)
        self.assertIn(("pending", "list"), leaves)
        self.assertIn(("pending", "update"), leaves)
        self.assertIn(("out-of-scope", "add"), leaves)
        self.assertIn(("broken",), leaves)

    def test_top_level_leaf_handler_is_matched(self) -> None:
        docstrings, unmatched = gi.match_leaf_handlers(self.tree, self.cli)
        self.assertEqual(docstrings[("render",)], "Render the thing.")
        self.assertEqual(unmatched, [("broken",)])

    def test_leaf_handler_with_no_docstring_is_empty_not_unmatched(self) -> None:
        docstrings, unmatched = gi.match_leaf_handlers(self.tree, self.cli)
        self.assertEqual(docstrings[("show",)], "")
        self.assertNotIn(("show",), unmatched)

    def test_group_node_contributes_no_docstring_and_is_never_unmatched(self) -> None:
        docstrings, unmatched = gi.match_leaf_handlers(self.tree, self.cli)
        self.assertNotIn(("pending",), docstrings)
        self.assertNotIn(("pending",), unmatched)
        self.assertNotIn(("out-of-scope",), docstrings)
        self.assertNotIn(("out-of-scope",), unmatched)

    def test_nested_leaf_handler_is_matched_by_joined_path(self) -> None:
        docstrings, _ = gi.match_leaf_handlers(self.tree, self.cli)
        self.assertEqual(docstrings[("pending", "list")], "List pending items.")
        self.assertEqual(docstrings[("pending", "update")], "Patch one pending item.")

    def test_hyphenated_path_segment_normalizes_to_underscore(self) -> None:
        docstrings, unmatched = gi.match_leaf_handlers(self.tree, self.cli)
        self.assertEqual(
            docstrings[("out-of-scope", "add")], "Record a rejected concept."
        )
        self.assertNotIn(("out-of-scope", "add"), unmatched)

    def test_unmatched_leaf_is_reported_not_silently_dropped(self) -> None:
        docstrings, unmatched = gi.match_leaf_handlers(self.tree, self.cli)
        self.assertEqual(unmatched, [("broken",)])
        self.assertNotIn(("broken",), docstrings)

    def test_real_repo_scripts_have_zero_unmatched_leaf_handlers(self) -> None:
        """Empirical check behind the spec's design: every leaf subcommand
        across the 4 in-scope scripts resolves to a real cmd_<path> handler."""
        for name in ("dev_status.py", "grill.py", "second_opinion.py", "standup.py"):
            source = (REPO_ROOT / gi.SCRIPTS_DIR / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            cli = gi.extract_cli(tree, gi.first_paragraph(ast.get_docstring(tree)))
            assert cli is not None
            _, unmatched = gi.match_leaf_handlers(tree, cli)
            self.assertEqual(unmatched, [], f"{name} has unmatched leaf handlers")


class FingerprintTests(unittest.TestCase):
    """The contract-fingerprint line composition (spec Output format step 2)."""

    def setUp(self) -> None:
        self.cli = cli_of(HANDLER_FIXTURE)
        self.tree = parse(HANDLER_FIXTURE)
        self.docstrings, _ = gi.match_leaf_handlers(self.tree, self.cli)

    def fp(self, exit_codes: list[int] | None = None) -> list[str]:
        return gi.fingerprint_lines(
            self.cli, self.docstrings, "module purpose text", exit_codes or [0, 1]
        )

    def test_subcommand_help_text_contributes_a_line(self) -> None:
        lines = self.fp()
        self.assertTrue(any("render it" in line for line in lines))
        self.assertTrue(any("nested group" in line for line in lines))

    def test_leaf_handler_docstring_contributes_a_line(self) -> None:
        lines = self.fp()
        self.assertTrue(any("Render the thing." in line for line in lines))
        self.assertTrue(any("Patch one pending item." in line for line in lines))

    def test_group_node_docstring_does_not_appear(self) -> None:
        # "pending"/"out-of-scope" are groups -- match_leaf_handlers never
        # produces an entry for them, so nothing derived from a
        # (nonexistent) cmd_pending/cmd_out_of_scope docstring can leak in.
        lines = self.fp()
        joined = "\n".join(lines)
        self.assertNotIn("cmd_pending", joined)
        self.assertNotIn("cmd_out_of_scope", joined)

    def test_exit_codes_are_one_sorted_line(self) -> None:
        lines = self.fp(exit_codes=[3, 0, 1])
        exit_lines = [line for line in lines if "exit" in line.lower()]
        self.assertEqual(len(exit_lines), 1)
        self.assertIn("0", exit_lines[0])
        self.assertIn("1", exit_lines[0])
        self.assertIn("3", exit_lines[0])

    def test_reformatting_help_text_does_not_change_the_fingerprint(self) -> None:
        cli_a = cli_of(HANDLER_FIXTURE)
        cli_b = cli_of(HANDLER_FIXTURE.replace("render it", "render   it"))
        lines_a = gi.fingerprint_lines(cli_a, self.docstrings, "x", [0])
        lines_b = gi.fingerprint_lines(cli_b, self.docstrings, "x", [0])
        self.assertEqual(lines_a, lines_b)

    def test_changed_wording_changes_the_fingerprint(self) -> None:
        cli_a = cli_of(HANDLER_FIXTURE)
        cli_b = cli_of(HANDLER_FIXTURE.replace("render it", "render it now"))
        lines_a = gi.fingerprint_lines(cli_a, self.docstrings, "x", [0])
        lines_b = gi.fingerprint_lines(cli_b, self.docstrings, "x", [0])
        self.assertNotEqual(lines_a, lines_b)

    def test_flag_choices_default_required_are_captured_not_just_help(self) -> None:
        cli = cli_of(
            """
            import argparse


            def main():
                parser = argparse.ArgumentParser(description="x")
                parser.add_argument(
                    "--format", choices=["json", "text"], default="text",
                    help="output format",
                )
            """
        )
        lines_before = gi.fingerprint_lines(cli, {}, "x", [0])
        cli_after = cli_of(
            """
            import argparse


            def main():
                parser = argparse.ArgumentParser(description="x")
                parser.add_argument(
                    "--format", choices=["json", "text", "yaml"], default="text",
                    help="output format",
                )
            """
        )
        lines_after = gi.fingerprint_lines(cli_after, {}, "x", [0])
        self.assertNotEqual(lines_before, lines_after)

    def test_flat_cli_fingerprints_module_docstring_instead_of_handlers(self) -> None:
        flat_cli = cli_of(FLAT_HANDLER_FIXTURE)
        lines_a = gi.fingerprint_lines(flat_cli, {}, "does one thing", [0])
        lines_b = gi.fingerprint_lines(flat_cli, {}, "does a different thing", [0])
        self.assertNotEqual(lines_a, lines_b)
        self.assertTrue(any("does one thing" in line for line in lines_a))

    def test_output_is_sorted_regardless_of_source_order(self) -> None:
        lines = self.fp()
        self.assertEqual(lines, sorted(lines))


class ModuleFactExtractionTests(unittest.TestCase):
    def test_env_vars_from_every_access_form(self) -> None:
        tree = parse(
            """
            import os

            A = os.environ.get("ALPHA", "1")
            B = os.environ["BRAVO"]
            C = os.getenv("CHARLIE")
            D = os.environ.get(computed_name)
            """
        )
        self.assertEqual(gi.extract_env_vars(tree), ["ALPHA", "BRAVO", "CHARLIE"])

    def test_env_vars_via_one_hop_helper_indirection(self) -> None:
        # Mirrors second_opinion.py's _resolve_timeout: a helper that calls
        # os.environ.get directly on its own parameter.
        tree = parse(
            """
            import os

            def _resolve_timeout(env_var):
                return os.environ.get(env_var, "")

            def run_agy():
                return _resolve_timeout("SECOND_OPINION_AGY_TIMEOUT_SECONDS")
            """
        )
        self.assertEqual(
            gi.extract_env_vars(tree), ["SECOND_OPINION_AGY_TIMEOUT_SECONDS"]
        )

    def test_env_vars_via_two_hop_helper_indirection(self) -> None:
        # Mirrors second_opinion.py's _resolve_pooled_model: it doesn't call
        # os.environ.get itself, it forwards to _env_stripped/_parse_pool,
        # which do. Detection must chain through both hops.
        tree = parse(
            """
            import os

            def _env_stripped(var):
                return (os.environ.get(var) or "").strip()

            def _parse_pool(var):
                return os.environ.get(var, "").split(",")

            def _resolve_pooled_model(pool_env_var, single_env_var, model_index):
                single = _env_stripped(single_env_var)
                pool = _parse_pool(pool_env_var)
                return single or (pool[model_index] if pool else None)

            def run_opencode():
                return _resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    0,
                )
            """
        )
        self.assertEqual(
            gi.extract_env_vars(tree),
            ["SECOND_OPINION_OPENCODE_MODEL", "SECOND_OPINION_OPENCODE_MODEL_POOL"],
        )

    def test_env_vars_indirection_skips_non_literal_arguments(self) -> None:
        tree = parse(
            """
            import os

            def _resolve_timeout(env_var):
                return os.environ.get(env_var, "")

            def run_agy(computed_name):
                return _resolve_timeout(computed_name)
            """
        )
        self.assertEqual(gi.extract_env_vars(tree), [])

    def test_env_vars_indirection_only_marked_parameter_counts(self) -> None:
        # model_index is a plain int parameter, never forwarded to an env
        # accessor -- a literal string passed there must not be mistaken
        # for an env var name just because it's a literal at a call site
        # to a function that *also* has a marked parameter.
        tree = parse(
            """
            import os

            def _resolve_timeout(env_var, label):
                return os.environ.get(env_var, "") or label

            def run_agy():
                return _resolve_timeout("SECOND_OPINION_AGY_TIMEOUT_SECONDS", "not-an-env-var")
            """
        )
        self.assertEqual(
            gi.extract_env_vars(tree), ["SECOND_OPINION_AGY_TIMEOUT_SECONDS"]
        )

    def test_exit_codes_include_bare_exit_as_zero(self) -> None:
        tree = parse(
            """
            import sys

            def f():
                sys.exit(2)
                sys.exit()
                sys.exit(1)
                sys.exit(code)
            """
        )
        self.assertEqual(gi.extract_exit_codes(tree), [0, 1, 2])

    def test_raised_system_exit_counts_as_an_exit_code(self) -> None:
        tree = parse("def f():\n    raise SystemExit(2)\n")
        self.assertEqual(gi.extract_exit_codes(tree), [2])

    def test_path_constants_follow_derivation_chains(self) -> None:
        tree = parse(
            """
            from pathlib import Path

            DATA_DIR = Path.home() / ".claude"
            ITEMS_FILE = DATA_DIR / "items.json"
            lowercase = DATA_DIR / "ignored.json"
            UNRELATED = 42
            """
        )
        self.assertEqual(
            gi.extract_path_constants(tree),
            [
                "DATA_DIR = Path.home() / '.claude'",
                "ITEMS_FILE = DATA_DIR / 'items.json'",
            ],
        )

    def test_internal_imports_only_match_siblings(self) -> None:
        tree = parse(
            """
            import json
            import llm_backends
            from standup_adapters import ADAPTERS
            """
        )
        siblings = {"llm_backends", "standup_adapters", "grill"}
        self.assertEqual(
            gi.extract_internal_imports(tree, siblings),
            ["llm_backends", "standup_adapters"],
        )

    def test_uses_raw_argv(self) -> None:
        self.assertTrue(gi.uses_raw_argv(parse("import sys\nx = sys.argv[1]\n")))
        self.assertFalse(gi.uses_raw_argv(parse("x = 1\n")))


API_FIXTURE = """
    class BackendError(Exception):
        \"\"\"A backend failed. More detail here.\"\"\"


    class Session(TypedDict):
        \"\"\"One session.\"\"\"


    class _Private:
        pass


    def public_fn(a: int, b: str = "x") -> bool:
        \"\"\"Do a thing.\"\"\"


    def _private_fn() -> None:
        pass


    def main() -> None:
        pass


    def cmd_render(args) -> None:
        pass
"""


class ApiExtractionTests(unittest.TestCase):
    def test_symbols_are_split_by_kind(self) -> None:
        tree = parse(API_FIXTURE)
        exceptions, classes, functions, handlers = gi.extract_api(tree, gi.CliSpec())
        self.assertEqual(
            [s.signature for s in exceptions], ["class BackendError(Exception)"]
        )
        self.assertEqual([s.signature for s in classes], ["class Session(TypedDict)"])
        self.assertEqual(
            [s.signature for s in functions],
            ["public_fn(a: int, b: str = 'x') -> bool"],
        )
        self.assertEqual(handlers, ["cmd_render"])

    def test_summary_is_the_docstrings_first_sentence(self) -> None:
        tree = parse(API_FIXTURE)
        exceptions, _, _, _ = gi.extract_api(tree, gi.CliSpec())
        self.assertEqual(exceptions[0].summary, "A backend failed.")

    def test_handlers_stay_functions_when_there_is_no_cli(self) -> None:
        tree = parse(API_FIXTURE)
        _, _, functions, handlers = gi.extract_api(tree, None)
        self.assertEqual(handlers, [])
        self.assertIn("cmd_render(args) -> None", [s.signature for s in functions])

    def test_signature_renders_keyword_only_and_varargs(self) -> None:
        tree = parse("def f(a, *rest, model: str | None = None, **kw) -> str: ...\n")
        node = tree.body[0]
        assert isinstance(node, ast.FunctionDef)
        self.assertEqual(
            gi.render_signature(node),
            "f(a, *rest, model: str | None = None, **kw) -> str",
        )

    def test_signature_renders_bare_star_for_keyword_only(self) -> None:
        tree = parse("def f(prompt: str, *, timeout: float) -> str: ...\n")
        node = tree.body[0]
        assert isinstance(node, ast.FunctionDef)
        self.assertEqual(
            gi.render_signature(node), "f(prompt: str, *, timeout: float) -> str"
        )


class FrontmatterTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        target = directory / "doc.md"
        target.write_text(text, encoding="utf-8")
        return target

    def test_quoted_and_unquoted_values(self) -> None:
        path = self.write('---\nname: dashboard\ndescription: "shows it"\n---\nbody\n')
        self.assertEqual(
            gi.read_frontmatter(path), {"name": "dashboard", "description": "shows it"}
        )

    def test_missing_frontmatter_is_empty(self) -> None:
        self.assertEqual(gi.read_frontmatter(self.write("# just a heading\n")), {})

    def test_unterminated_frontmatter_is_empty(self) -> None:
        self.assertEqual(gi.read_frontmatter(self.write("---\nname: x\nbody\n")), {})


class LinkTableTests(unittest.TestCase):
    def load(self, toml_text: str) -> gi.LinkTable:
        root = Path(tempfile.mkdtemp())
        (root / "links.toml").write_text(textwrap.dedent(toml_text), encoding="utf-8")
        return gi.load_link_table(root)

    def test_one_source_with_several_destinations(self) -> None:
        table = self.load(
            """
            [[link]]
            src = "claude/CLAUDE.md"
            dest = "~/.claude/CLAUDE.md"
            harness = "claude"

            [[link]]
            src = "claude/CLAUDE.md"
            dest = "~/.gemini/GEMINI.md"
            harness = "agy"
            """
        )
        self.assertEqual(
            [t.dest for t in table["claude/CLAUDE.md"]],
            ["~/.claude/CLAUDE.md", "~/.gemini/GEMINI.md"],
        )

    def test_repeated_destination_merges_its_gates(self) -> None:
        table = self.load(
            """
            [[link]]
            src = "copilot/hooks/session-start.json"
            dest = "~/.copilot/hooks/session-start.json"
            harness = "copilot"
            platform = "mac"

            [[link]]
            src = "copilot/hooks/session-start.json"
            dest = "~/.copilot/hooks/session-start.json"
            harness = "copilot"
            platform = "linux"
            """
        )
        targets = table["copilot/hooks/session-start.json"]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].gates, ["copilot", "mac", "linux"])

    def test_profile_exclude_becomes_a_gate(self) -> None:
        table = self.load(
            """
            [[link]]
            src = "claude/scripts/watchcommit_activity.py"
            dest = "~/.claude/scripts/watchcommit_activity.py"
            profile_exclude = ["work"]
            """
        )
        target = table["claude/scripts/watchcommit_activity.py"][0]
        self.assertEqual(target.gates, ["not on work"])

    def test_missing_links_file_is_empty(self) -> None:
        self.assertEqual(gi.load_link_table(Path(tempfile.mkdtemp())), {})

    def test_rendering_ungated_and_unlinked_targets(self) -> None:
        self.assertEqual(
            gi.render_link_targets([gi.LinkTarget("~/x", [])]), "`~/x` (all harnesses)"
        )
        self.assertEqual(gi.render_link_targets([]), "not symlinked by `links.toml`")


class AssetFilterTests(unittest.TestCase):
    def test_build_output_and_dotfiles_are_excluded(self) -> None:
        self.assertTrue(gi.is_generated_artifact("claude/scripts/__pycache__/x.pyc"))
        self.assertTrue(gi.is_generated_artifact("claude/.DS_Store"))
        self.assertFalse(gi.is_generated_artifact("copilot/aliases.zsh"))

    def test_untracked_files_are_excluded_from_the_asset_table(self) -> None:
        """Regression: an untracked file in one checkout must not reach the doc.

        A stray ``claude/settings.json.bak.<stamp>`` in one working copy once
        got baked into the committed inventory, so ``--check`` failed in every
        other clean checkout and in CI. The asset walk hits the filesystem, so
        the tracked-file set is what keeps output checkout-independent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "claude").mkdir()
            (root / "claude" / "real.md").write_text("tracked\n", encoding="utf-8")
            (root / "claude" / "settings.json.bak.20260806").write_text(
                "untracked\n", encoding="utf-8"
            )

            tracked = {"claude/real.md"}
            rendered = "\n".join(gi.render_assets(root, {}, tracked))
            self.assertIn("claude/real.md", rendered)
            self.assertNotIn("settings.json.bak", rendered)

            # None (git unavailable) must keep the old unfiltered behavior
            # rather than silently emptying the table.
            unfiltered = "\n".join(gi.render_assets(root, {}, None))
            self.assertIn("claude/real.md", unfiltered)
            self.assertIn("settings.json.bak", unfiltered)

    @pytest.mark.allow_real_subprocess
    def test_tracked_files_returns_none_outside_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(gi.tracked_files(Path(tmp)))

    @pytest.mark.allow_real_subprocess
    def test_tracked_files_lists_this_repo(self) -> None:
        tracked = gi.tracked_files(REPO_ROOT)
        assert tracked is not None
        self.assertIn("claude/scripts/gen_interfaces.py", tracked)


class RealSourceTests(unittest.TestCase):
    """Parse the actual scripts — the drift these tests exist to catch."""

    def spec_for(self, name: str) -> gi.CliSpec:
        source = (REPO_ROOT / gi.SCRIPTS_DIR / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        spec = gi.extract_cli(tree, gi.first_paragraph(ast.get_docstring(tree)))
        assert spec is not None
        return spec

    def test_dev_status_subcommands_are_enumerated(self) -> None:
        names = {s.name for s in self.spec_for("dev_status.py").subcommands}
        self.assertGreaterEqual(len(names), 20)
        self.assertLessEqual(
            {
                "render",
                "add",
                "start",
                "done",
                "review",
                "approve",
                "reject",
                "gate-set",
                "gate-pass",
                "block",
                "unblock",
                "prune",
                "recap",
                "pending add",
                "pending update",
                "pending list",
            },
            names,
        )

    def test_dev_status_helper_arguments_are_resolved(self) -> None:
        spec = self.spec_for("dev_status.py")
        approve = subcommand(spec, "approve")
        self.assertEqual(
            [a.usage for a in approve.arguments], ["<slug|N>", "[--if-rev <N>]"]
        )

    def test_grill_session_flag_reaches_every_subcommand_that_takes_it(self) -> None:
        spec = self.spec_for("grill.py")
        decide = subcommand(spec, "decide")
        self.assertIn("--session/-s", [a.label for a in decide.arguments])
        self.assertEqual([a.label for a in subcommand(spec, "list").arguments], [])

    def test_install_py_cli_is_found_through_its_parser_subclass(self) -> None:
        source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        spec = gi.extract_cli(ast.parse(source), "")
        assert spec is not None
        labels = [option.label for option in spec.options]
        self.assertIn("--harness", labels)
        self.assertIn("--dry-run", labels)
        self.assertIn("--rollback", labels)

    def test_llm_backends_is_a_library_with_no_cli(self) -> None:
        source = (REPO_ROOT / gi.SCRIPTS_DIR / "llm_backends.py").read_text(
            encoding="utf-8"
        )
        self.assertIsNone(gi.extract_cli(ast.parse(source), ""))


DEV_STATUS_START_FIXTURE = """
    import argparse

    def main() -> None:
        parser = argparse.ArgumentParser(prog="dev_status")
        sub = parser.add_subparsers(dest="command")
        start = sub.add_parser("start")
        start.add_argument("id", metavar="<slug|N>")
        start.add_argument("--if-rev", metavar="<N>")
        start.add_argument("--quiet", "-q", action="store_true")
        sub.add_parser("show")
"""
# A second subcommand ("show") keeps cli.subcommands non-empty when a test
# below drops "start" -- an empty list is indistinguishable from a script
# that never had subcommands (validate_invocation then validates top-level
# options instead), which real scripts never hit since they don't drop
# every subcommand at once.


class DocDriftTests(unittest.TestCase):
    """Regression tests for the doc<->script contract-drift check."""

    def start_cli(self) -> gi.CliSpec:
        return cli_of(DEV_STATUS_START_FIXTURE)

    def test_matching_invocation_has_no_problems(self) -> None:
        cli = self.start_cli()
        tokens = ["dev_status.py", "start", "5", "--if-rev", "3"]
        self.assertEqual(gi.validate_invocation(cli, tokens), [])

    def test_removed_subcommand_is_flagged(self) -> None:
        cli = self.start_cli()
        cli.subcommands = [sc for sc in cli.subcommands if sc.path != ("start",)]
        problems = gi.validate_invocation(cli, ["dev_status.py", "start", "5"])
        self.assertEqual(len(problems), 1)
        self.assertIn("start", problems[0])

    def test_removed_flag_under_a_resolved_subcommand_is_flagged(self) -> None:
        cli = self.start_cli()
        for sc in cli.subcommands:
            sc.arguments = [a for a in sc.arguments if "--if-rev" not in a.label]
        problems = gi.validate_invocation(
            cli, ["dev_status.py", "start", "5", "--if-rev", "3"]
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("--if-rev", problems[0])

    def test_double_dash_delimiter_stops_flag_scanning(self) -> None:
        cli = self.start_cli()
        tokens = ["dev_status.py", "start", "5", "--", "--not-a-real-flag"]
        self.assertEqual(gi.validate_invocation(cli, tokens), [])

    def test_negative_number_flag_value_is_not_misread_as_a_flag(self) -> None:
        cli = self.start_cli()
        tokens = ["dev_status.py", "start", "5", "--if-rev", "-3"]
        self.assertEqual(gi.validate_invocation(cli, tokens), [])

    def test_inline_equals_flag_value_is_recognised(self) -> None:
        cli = self.start_cli()
        tokens = ["dev_status.py", "start", "5", "--if-rev=3"]
        self.assertEqual(gi.validate_invocation(cli, tokens), [])

    def test_flag_takes_value_distinguishes_boolean_from_valued_flags(self) -> None:
        cli = self.start_cli()
        start = subcommand(cli, "start")
        by_label = {a.label: a for a in start.arguments}
        self.assertFalse(gi.flag_takes_value(by_label["--quiet/-q"]))
        self.assertTrue(gi.flag_takes_value(by_label["--if-rev"]))

    def test_interpreter_and_path_qualified_invocations_are_matched(self) -> None:
        self.assertIsNotNone(
            gi.invocation_tokens(["python3", "dev_status.py", "show"], "dev_status.py")
        )
        self.assertIsNotNone(
            gi.invocation_tokens(
                ["python3", "~/.claude/scripts/dev_status.py", "show"], "dev_status.py"
            )
        )
        self.assertIsNotNone(
            gi.invocation_tokens(
                ["DEVSTATUS_AGENT=1", "python3", "dev_status.py", "show"],
                "dev_status.py",
            )
        )

    def test_non_invocation_shell_command_is_not_matched(self) -> None:
        self.assertIsNone(
            gi.invocation_tokens(["cp", "dev_status.py", "backup_dir"], "dev_status.py")
        )
        self.assertIsNone(
            gi.invocation_tokens(["nano", "dev_status.py"], "dev_status.py")
        )

    def test_bracket_wrapped_flag_is_tokenized_as_a_real_flag(self) -> None:
        tokens = gi.tokenize_invocation_line("standup.py fetch [--date YYYY-MM-DD]")
        self.assertEqual(tokens, ["standup.py", "fetch", "--date", "YYYY-MM-DD"])

    def test_coverage_section_is_sorted_regardless_of_input_order(self) -> None:
        coverage = {
            "standup.py": {"z-doc.md": True, "a-doc.md": False},
            "dev_status.py": {"m-doc.md": True},
        }
        lines = gi.render_doc_drift_section(coverage)
        script_headings = [line for line in lines if line.startswith("### ")]
        self.assertEqual(script_headings, ["### `dev_status.py`", "### `standup.py`"])
        doc_rows = [line for line in lines if line.startswith("| `")]
        self.assertEqual(doc_rows[1], "| `a-doc.md` | MISSING |")
        self.assertEqual(doc_rows[2], "| `z-doc.md` | OK |")

    def test_repo_current_docs_have_zero_drift(self) -> None:
        links = gi.load_link_table(REPO_ROOT)
        modules = [
            gi.analyze_module(
                REPO_ROOT / gi.SCRIPTS_DIR / name, REPO_ROOT, set(), links
            )
            for name in ("dev_status.py", "grill.py", "standup.py")
        ]
        problems, coverage = gi.check_doc_drift(REPO_ROOT, modules)
        self.assertEqual(problems, [])
        self.assertGreater(sum(len(docs) for docs in coverage.values()), 0)

    def _write_synthetic_repo(self, root: Path, doc_invocation: str) -> None:
        scripts = root / gi.SCRIPTS_DIR
        scripts.mkdir(parents=True)
        (scripts / "foo.py").write_text(
            textwrap.dedent(
                '''\
                #!/usr/bin/env python3
                """foo.py — test fixture."""
                import argparse

                def main() -> None:
                    parser = argparse.ArgumentParser(prog="foo")
                    sub = parser.add_subparsers(dest="command")
                    run = sub.add_parser("run")
                    run.add_argument("--slow", action="store_true")

                if __name__ == "__main__":
                    main()
                '''
            ),
            encoding="utf-8",
        )
        commands = root / "claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "foo.md").write_text(doc_invocation, encoding="utf-8")
        for harness in ("copilot", "opencode", "agy"):
            (root / harness).mkdir()

    def test_synthetic_repo_with_matching_doc_has_no_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --slow` to go.\n")
            links = gi.load_link_table(root)
            module = gi.analyze_module(
                scripts_path(root, "foo.py"), root, {"foo"}, links
            )
            problems, _ = gi.check_doc_drift(root, [module])
            self.assertEqual(problems, [])

    def test_synthetic_repo_with_a_drifted_flag_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --fast` to go.\n")
            links = gi.load_link_table(root)
            module = gi.analyze_module(
                scripts_path(root, "foo.py"), root, {"foo"}, links
            )
            problems, _ = gi.check_doc_drift(root, [module])
            self.assertEqual(len(problems), 1)
            self.assertIn("--fast", problems[0].detail)

    @pytest.mark.allow_real_subprocess
    def test_check_exits_3_for_doc_drift_even_when_the_file_is_also_stale(
        self,
    ) -> None:
        """A stale INTERFACES.md must never mask a doc-drift finding under
        it (round 3 caught this: rendering the coverage section into a
        document that also differs from disk could make the generic
        stale-file exit 1 fire first and hide the real, differently-fixed
        doc-drift problem underneath it)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --fast` to go.\n")
            # deliberately no INTERFACES.md on disk -- the file is "stale"
            # (missing) *and* a doc has drifted, at the same time
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / gi.SCRIPTS_DIR / "gen_interfaces.py"),
                    "--check",
                    "--repo-root",
                    str(root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("--fast", result.stderr)


class ContractFingerprintTests(unittest.TestCase):
    """The `--check`/`--update-fingerprints` semantic-doc-drift layer."""

    def _write_synthetic_repo(
        self, root: Path, doc_invocation: str, handler_body: str = "pass\n"
    ) -> None:
        scripts = root / gi.SCRIPTS_DIR
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "foo.py").write_text(
            textwrap.dedent(
                f'''\
                #!/usr/bin/env python3
                """foo.py — test fixture."""
                import argparse

                def main() -> None:
                    parser = argparse.ArgumentParser(prog="foo")
                    sub = parser.add_subparsers(dest="command")
                    run = sub.add_parser("run", help="run it")
                    run.add_argument("--slow", action="store_true")

                def cmd_run(args) -> None:
                    {handler_body}

                if __name__ == "__main__":
                    main()
                '''
            ),
            encoding="utf-8",
        )
        commands = root / "claude" / "commands"
        commands.mkdir(parents=True, exist_ok=True)
        (commands / "foo.md").write_text(doc_invocation, encoding="utf-8")
        for harness in ("copilot", "opencode", "agy"):
            (root / harness).mkdir(exist_ok=True)

    def _modules_and_coverage(
        self, root: Path
    ) -> tuple[list[gi.ModuleInterface], dict[str, dict[str, bool]]]:
        links = gi.load_link_table(root)
        module = gi.analyze_module(scripts_path(root, "foo.py"), root, {"foo"}, links)
        _, coverage = gi.check_doc_drift(root, [module])
        return [module], coverage

    def test_missing_fingerprints_file_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(gi.load_contract_fingerprints(Path(tmp)), {})

    def test_malformed_json_loads_as_empty_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / gi.SCRIPTS_DIR).mkdir(parents=True)
            (root / gi.CONTRACT_FINGERPRINTS_PATH).write_text(
                "{not valid json", encoding="utf-8"
            )
            self.assertEqual(gi.load_contract_fingerprints(root), {})

    def test_no_recorded_fingerprint_is_a_problem_not_a_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --slow` to go.\n")
            modules, coverage = self._modules_and_coverage(root)
            problems = gi.check_contract_fingerprints(root, modules, coverage)
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0].kind, "mismatch")
            self.assertIn("no prior fingerprint", problems[0].message)

    def test_matching_recorded_fingerprint_has_no_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --slow` to go.\n")
            gi.write_contract_fingerprints(root)
            modules, coverage = self._modules_and_coverage(root)
            problems = gi.check_contract_fingerprints(root, modules, coverage)
            self.assertEqual(problems, [])

    def test_changed_handler_docstring_is_a_mismatch_with_a_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(
                root, "Use `foo.py run --slow` to go.\n", handler_body='"""Old."""\n'
            )
            gi.write_contract_fingerprints(root)
            self._write_synthetic_repo(
                root, "Use `foo.py run --slow` to go.\n", handler_body='"""New."""\n'
            )
            modules, coverage = self._modules_and_coverage(root)
            problems = gi.check_contract_fingerprints(root, modules, coverage)
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0].kind, "mismatch")
            self.assertIn("Old", problems[0].message)
            self.assertIn("New", problems[0].message)

    def test_unmatched_leaf_handler_is_reported_distinctly_and_blocks_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --slow` to go.\n")
            # rename the handler so cmd_run no longer resolves
            source = scripts_path(root, "foo.py").read_text(encoding="utf-8")
            scripts_path(root, "foo.py").write_text(
                source.replace("def cmd_run", "def cmd_run_renamed"), encoding="utf-8"
            )
            modules, coverage = self._modules_and_coverage(root)
            problems = gi.check_contract_fingerprints(root, modules, coverage)
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0].kind, "unmatched-handler")
            self.assertIn("run", problems[0].message)

    def test_update_fingerprints_seeds_a_file_check_then_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --slow` to go.\n")
            written = gi.write_contract_fingerprints(root)
            self.assertIn("foo.py", written)
            on_disk = json.loads(
                (root / gi.CONTRACT_FINGERPRINTS_PATH).read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk, written)
            modules, coverage = self._modules_and_coverage(root)
            self.assertEqual(
                gi.check_contract_fingerprints(root, modules, coverage), []
            )

    def test_update_fingerprints_drops_a_script_no_longer_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(root, "Use `foo.py run --slow` to go.\n")
            gi.write_contract_fingerprints(root)
            (root / "claude" / "commands" / "foo.md").write_text(
                "no longer mentions the script\n", encoding="utf-8"
            )
            written = gi.write_contract_fingerprints(root)
            self.assertNotIn("foo.py", written)

    @pytest.mark.allow_real_subprocess
    def test_check_exits_3_on_fingerprint_mismatch_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_synthetic_repo(
                root, "Use `foo.py run --slow` to go.\n", handler_body='"""Old."""\n'
            )
            update = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / gi.SCRIPTS_DIR / "gen_interfaces.py"),
                    "--update-fingerprints",
                    "--repo-root",
                    str(root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(update.returncode, 0, update.stderr)
            self._write_synthetic_repo(
                root, "Use `foo.py run --slow` to go.\n", handler_body='"""New."""\n'
            )
            # avoid the unrelated stale-INTERFACES.md exit-1 path masking this
            gi.build_document(root)
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / gi.SCRIPTS_DIR / "gen_interfaces.py"),
                    "--check",
                    "--repo-root",
                    str(root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("contract", result.stderr.lower())
            self.assertIn("update-fingerprints", result.stderr)


class SkillMentionTests(unittest.TestCase):
    def write(self, name: str, text: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        target = directory / f"{name}.md"
        target.write_text(text, encoding="utf-8")
        return target

    def test_plain_whole_word_mention_is_found(self) -> None:
        source = self.write("backlog-item", "See also /spec for details.\n")
        self.assertEqual(
            gi.extract_skill_mentions(source, ["backlog-item", "spec"]), ["spec"]
        )

    def test_mention_inside_a_longer_word_is_not_found(self) -> None:
        source = self.write("backlog-item", "Write a specific specification.\n")
        self.assertEqual(
            gi.extract_skill_mentions(source, ["backlog-item", "spec"]), []
        )

    def test_skill_never_mentions_itself(self) -> None:
        source = self.write("spec", "This is the spec skill. Use /spec.\n")
        self.assertEqual(gi.extract_skill_mentions(source, ["spec", "grill-me"]), [])


def scripts_path(root: Path, name: str) -> Path:
    """Return the path to a script under ``root``'s ``SCRIPTS_DIR``."""
    return root / gi.SCRIPTS_DIR / name


class GeneratedDocumentTests(unittest.TestCase):
    @pytest.mark.allow_real_subprocess
    def test_document_has_every_module_section(self) -> None:
        document = gi.build_document(REPO_ROOT)
        for module in (REPO_ROOT / gi.SCRIPTS_DIR).glob("*.py"):
            if module.name.startswith("test_"):
                continue
            self.assertIn(f"### `claude/scripts/{module.name}`", document)
        for name in gi.ROOT_ENTRYPOINTS:
            self.assertIn(f"### `{name}`", document)

    def test_install_py_is_credited_to_its_tests_in_the_test_directory(self) -> None:
        self.assertIn(
            "test/test_install.py", gi.find_tests(REPO_ROOT / "install.py", REPO_ROOT)
        )

    @pytest.mark.allow_real_subprocess
    def test_committed_interfaces_md_is_not_stale(self) -> None:
        committed = (REPO_ROOT / gi.OUTPUT_NAME).read_text(encoding="utf-8")
        self.assertEqual(
            committed,
            gi.build_document(REPO_ROOT),
            f"{gi.OUTPUT_NAME} is stale — run "
            f"`python3 {gi.SCRIPTS_DIR}/gen_interfaces.py`",
        )


if __name__ == "__main__":
    unittest.main(verbosity=1)
