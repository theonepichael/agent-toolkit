#!/usr/bin/env python3
"""Unit tests for guard_rails.py -- payload shaping, verdict rendering, and
the failure posture. Git is mocked here; the topology behaviour that depends
on real git output is covered by test/test_guard_rails_topology.py against
actual repositories, because mocked git output would encode the very
assumptions under test."""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard_rails  # noqa: E402


class NormalizeToolTests(unittest.TestCase):
    def test_write_family_covers_every_harness_spelling(self) -> None:
        for name in (
            "Write",
            "Edit",
            "write",
            "edit",
            "write_to_file",
            "replace_file_content",
            "MultiEdit",
        ):
            self.assertEqual(guard_rails.tool_family(name), "write", name)

    def test_bash_and_unknown_tools_are_not_write_family(self) -> None:
        for name in ("Bash", "bash", "run_command", "Read", "", "Glob"):
            self.assertNotEqual(guard_rails.tool_family(name), "write", name)

    def test_bash_spelling_covers_both_cases(self) -> None:
        self.assertEqual(guard_rails.tool_family("Bash"), "bash")
        self.assertEqual(guard_rails.tool_family("bash"), "bash")

    def test_write_tools_are_not_bash_family(self) -> None:
        self.assertNotEqual(guard_rails.tool_family("Edit"), "bash")


class PayloadParsingTests(unittest.TestCase):
    def test_claude_payload(self) -> None:
        req = guard_rails.parse_payload(
            "claude",
            {
                "cwd": "/repo",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/repo/a.py"},
            },
        )
        self.assertEqual(req.tool, "write")
        self.assertEqual(req.path, "/repo/a.py")
        self.assertEqual(req.cwd, "/repo")

    def test_agy_payload_is_not_double_encoded(self) -> None:
        req = guard_rails.parse_payload(
            "agy",
            {
                "toolCall": {
                    "name": "write_to_file",
                    "args": {"TargetFile": "/repo/a.py"},
                }
            },
        )
        self.assertEqual(req.tool, "write")
        self.assertEqual(req.path, "/repo/a.py")

    def test_copilot_toolargs_is_a_json_string_needing_a_second_parse(self) -> None:
        req = guard_rails.parse_payload(
            "copilot",
            {"toolName": "edit", "toolArgs": json.dumps({"path": "/repo/a.py"})},
        )
        self.assertEqual(req.tool, "write")
        self.assertEqual(req.path, "/repo/a.py")

    def test_malformed_payload_yields_no_request_rather_than_raising(self) -> None:
        self.assertIsNone(guard_rails.parse_payload("claude", {"nonsense": True}))
        self.assertIsNone(guard_rails.parse_payload("agy", []))

    def test_claude_bash_payload_extracts_the_command(self) -> None:
        req = guard_rails.parse_payload(
            "claude",
            {
                "cwd": "/repo",
                "tool_name": "Bash",
                "tool_input": {"command": "git commit --no-verify -m x"},
            },
        )
        self.assertEqual(req.tool, "bash")
        self.assertEqual(req.command, "git commit --no-verify -m x")
        self.assertEqual(req.cwd, "/repo")

    def test_agy_and_copilot_get_no_bash_family_wiring(self) -> None:
        """Best-effort tier: even a harness whose own tool name happens to
        be literally "bash" must not get the companion check -- only
        claude is wired for bash-family."""
        self.assertIsNone(
            guard_rails.parse_payload(
                "agy", {"toolCall": {"name": "bash", "args": {"command": "ls"}}}
            )
        )
        self.assertIsNone(
            guard_rails.parse_payload(
                "copilot",
                {"toolName": "bash", "toolArgs": json.dumps({"command": "ls"})},
            )
        )


class VerdictRenderingTests(unittest.TestCase):
    def test_claude_deny_uses_permission_decision(self) -> None:
        out, code = guard_rails.render("claude", guard_rails.Verdict("deny", "nope"))
        self.assertEqual(code, 0)
        body = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "PreToolUse")
        self.assertEqual(body["permissionDecision"], "deny")
        self.assertEqual(body["permissionDecisionReason"], "nope")

    def test_claude_warn_uses_additional_context_not_a_denial(self) -> None:
        out, _ = guard_rails.render("claude", guard_rails.Verdict("warn", "behind"))
        body = json.loads(out)["hookSpecificOutput"]
        self.assertNotEqual(body.get("permissionDecision"), "deny")
        self.assertIn("behind", body["additionalContext"])

    def test_agy_deny_and_warn_both_use_decision_field(self) -> None:
        deny = json.loads(
            guard_rails.render("agy", guard_rails.Verdict("deny", "x"))[0]
        )
        warn = json.loads(
            guard_rails.render("agy", guard_rails.Verdict("warn", "y"))[0]
        )
        self.assertEqual(deny["decision"], "deny")
        self.assertEqual(warn["decision"], "allow")
        self.assertIn("y", warn["reason"])

    def test_copilot_deny_uses_the_exit_code_since_json_is_ignored(self) -> None:
        """Probed live 2026-09-01: Copilot honours exit 2 only. A JSON verdict
        on exit 0 was ignored and the write proceeded."""
        _, code = guard_rails.render("copilot", guard_rails.Verdict("deny", "x"))
        self.assertEqual(code, 2)
        _, allow_code = guard_rails.render("copilot", guard_rails.Verdict("allow"))
        self.assertEqual(allow_code, 0)
        _, warn_code = guard_rails.render("copilot", guard_rails.Verdict("warn", "y"))
        self.assertEqual(warn_code, 0, "a warn must never block")

    def test_neutral_form_always_exits_zero(self) -> None:
        for decision in ("allow", "deny", "warn"):
            out, code = guard_rails.render(None, guard_rails.Verdict(decision, "r"))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["decision"], decision)


class EscapeHatchTests(unittest.TestCase):
    def test_guard_rails_off_short_circuits_every_rule(self) -> None:
        with mock.patch.dict("os.environ", {"GUARD_RAILS_OFF": "1"}):
            with mock.patch.object(guard_rails, "git") as git:
                verdict = guard_rails.evaluate(
                    guard_rails.Request("write", "/repo", "/repo/a.py")
                )
        self.assertEqual(verdict.decision, "allow")
        git.assert_not_called()


class FailurePostureTests(unittest.TestCase):
    def test_not_a_repo_allows_without_reading_the_backlog(self) -> None:
        with mock.patch.object(guard_rails, "repo_info", return_value=None):
            with mock.patch.object(guard_rails, "load_in_progress") as load:
                verdict = guard_rails.evaluate(
                    guard_rails.Request("write", "/tmp", "/tmp/a.py")
                )
        self.assertEqual(verdict.decision, "allow")
        load.assert_not_called()

    def test_worktree_short_circuits_before_the_backlog_read(self) -> None:
        info = guard_rails.RepoInfo(
            toplevel="/wt",
            common_dir="/repo/.git",
            is_worktree=True,
            is_bare=False,
            branch="feature",
        )
        with mock.patch.object(guard_rails, "repo_info", return_value=info):
            with mock.patch.object(guard_rails, "load_in_progress") as load:
                # A worktree still gets the R3 base check; stub it so this
                # test stays about the backlog short-circuit.
                with mock.patch.object(
                    guard_rails, "_behind_origin_main", return_value=False
                ):
                    verdict = guard_rails.evaluate(
                        guard_rails.Request("write", "/wt", "/wt/a.py")
                    )
        self.assertEqual(verdict.decision, "allow")
        load.assert_not_called()

    def test_worktree_behind_origin_main_warns_without_reading_the_backlog(
        self,
    ) -> None:
        info = guard_rails.RepoInfo(
            toplevel="/wt",
            common_dir="/repo/.git",
            is_worktree=True,
            is_bare=False,
            branch="feature",
        )
        with mock.patch.object(guard_rails, "repo_info", return_value=info):
            with mock.patch.object(guard_rails, "load_in_progress") as load:
                with mock.patch.object(
                    guard_rails, "_behind_origin_main", return_value=True
                ):
                    verdict = guard_rails.evaluate(
                        guard_rails.Request("write", "/wt", "/wt/a.py")
                    )
        self.assertEqual(verdict.decision, "warn")
        self.assertIn("origin/main", verdict.reason)
        load.assert_not_called()

    def test_bare_repo_allows(self) -> None:
        info = guard_rails.RepoInfo(
            toplevel="",
            common_dir="/repo.git",
            is_worktree=False,
            is_bare=True,
            branch="main",
        )
        with mock.patch.object(guard_rails, "repo_info", return_value=info):
            with mock.patch.object(guard_rails, "load_in_progress") as load:
                verdict = guard_rails.evaluate(
                    guard_rails.Request("write", "/repo.git", "/repo.git/x")
                )
        self.assertEqual(verdict.decision, "allow")
        load.assert_not_called()

    def test_feature_branch_in_a_main_checkout_allows(self) -> None:
        info = guard_rails.RepoInfo(
            toplevel="/repo",
            common_dir="/repo/.git",
            is_worktree=False,
            is_bare=False,
            branch="feature",
        )
        with mock.patch.object(guard_rails, "repo_info", return_value=info):
            with mock.patch.object(guard_rails, "load_in_progress") as load:
                verdict = guard_rails.evaluate(
                    guard_rails.Request("write", "/repo", "/repo/a.py")
                )
        self.assertEqual(verdict.decision, "allow")
        load.assert_not_called()

    def test_unreadable_backlog_store_allows(self) -> None:
        info = guard_rails.RepoInfo(
            toplevel="/repo",
            common_dir="/repo/.git",
            is_worktree=False,
            is_bare=False,
            branch="main",
        )
        with mock.patch.object(guard_rails, "repo_info", return_value=info):
            with mock.patch.object(guard_rails, "load_in_progress", return_value=None):
                verdict = guard_rails.evaluate(
                    guard_rails.Request("write", "/repo", "/repo/a.py")
                )
        self.assertEqual(verdict.decision, "allow")


class BusyMainCheckoutTests(unittest.TestCase):
    MAIN = guard_rails.RepoInfo(
        toplevel="/repo",
        common_dir="/repo/.git",
        is_worktree=False,
        is_bare=False,
        branch="main",
    )

    def _evaluate(self, items, common_dirs):
        def fake_common_dir(directory: str) -> str | None:
            return common_dirs.get(directory)

        with mock.patch.object(guard_rails, "repo_info", return_value=self.MAIN):
            with mock.patch.object(guard_rails, "load_in_progress", return_value=items):
                with mock.patch.object(
                    guard_rails, "common_dir_of", side_effect=fake_common_dir
                ) as resolver:
                    verdict = guard_rails.evaluate(
                        guard_rails.Request("write", "/repo", "/repo/a.py")
                    )
        return verdict, resolver

    def test_item_whose_worktree_shares_a_common_dir_denies(self) -> None:
        """The item's related_files point at the main checkout while the work
        happens in a linked worktree. Their common dirs match; their toplevels
        would not."""
        items = [{"id": "slug", "related_files": [{"path": "/repo/x.py"}]}]
        verdict, _ = self._evaluate(items, {"/repo": "/repo/.git"})
        self.assertEqual(verdict.decision, "deny")
        self.assertIn("slug", verdict.reason)

    def test_item_in_a_different_repo_allows(self) -> None:
        items = [{"id": "other", "related_files": [{"path": "/elsewhere/x.py"}]}]
        verdict, _ = self._evaluate(items, {"/elsewhere": "/elsewhere/.git"})
        self.assertEqual(verdict.decision, "allow")

    def test_directories_are_deduplicated_before_any_git_call(self) -> None:
        items = [
            {
                "id": "slug",
                "related_files": [
                    {"path": "/other/a.py"},
                    {"path": "/other/b.py"},
                    {"path": "/other/c.py"},
                    {"path": "/other/nested/d.py"},
                ],
            }
        ]
        _, resolver = self._evaluate(
            items, {"/other": "/other/.git", "/other/nested": "/other/.git"}
        )
        seen = [call.args[0] for call in resolver.call_args_list]
        self.assertEqual(len(seen), len(set(seen)), f"duplicate git calls: {seen}")
        self.assertLessEqual(len(seen), 2)

    def test_stale_related_file_is_skipped_not_matched(self) -> None:
        items = [{"id": "slug", "related_files": [{"path": "/gone/x.py"}]}]
        verdict, _ = self._evaluate(items, {"/gone": None})
        self.assertEqual(verdict.decision, "allow")

    def test_item_with_no_related_files_does_not_match_everything(self) -> None:
        items = [{"id": "slug", "related_files": []}]
        verdict, _ = self._evaluate(items, {})
        self.assertEqual(verdict.decision, "allow")


_MAIN_INFO = guard_rails.RepoInfo(
    toplevel="/repo",
    common_dir="/repo/.git",
    is_worktree=False,
    is_bare=False,
    branch="main",
)
_FEATURE_INFO = guard_rails.RepoInfo(
    toplevel="/repo",
    common_dir="/repo/.git",
    is_worktree=False,
    is_bare=False,
    branch="feature",
)


class BashOverrideTests(unittest.TestCase):
    """evaluate_bash_override's deny/allow logic, with repo_info mocked so
    these stay fast and branch-independent; the real end-to-end behaviour
    against actual git repos is covered by
    test/test_guard_rails_topology.py."""

    def _on_main(self, command: str) -> guard_rails.Verdict:
        with mock.patch.object(guard_rails, "repo_info", return_value=_MAIN_INFO):
            return guard_rails.evaluate_bash_override(command, "/repo")

    def _on_feature(self, command: str) -> guard_rails.Verdict:
        with mock.patch.object(guard_rails, "repo_info", return_value=_FEATURE_INFO):
            return guard_rails.evaluate_bash_override(command, "/repo")

    def test_evaluate_dispatches_bash_family_here_not_to_write_logic(self) -> None:
        with mock.patch.object(
            guard_rails,
            "evaluate_bash_override",
            return_value=guard_rails.Verdict("deny", "x"),
        ) as ev:
            verdict = guard_rails.evaluate(
                guard_rails.Request(
                    "bash", "/repo", "", command="git commit --no-verify"
                )
            )
        ev.assert_called_once_with("git commit --no-verify", "/repo")
        self.assertEqual(verdict.decision, "deny")

    def test_not_a_repo_or_unknown_branch_allows(self) -> None:
        with mock.patch.object(guard_rails, "repo_info", return_value=None):
            verdict = guard_rails.evaluate_bash_override(
                "git config --unset core.hooksPath", "/tmp"
            )
        self.assertEqual(verdict.decision, "allow")

    def test_feature_branch_allows_every_override_attempt(self) -> None:
        for command in (
            "git commit --no-verify -m x",
            "git -c core.hooksPath=/tmp/x commit -m x",
            "git config --unset core.hooksPath",
            "GIT_CONFIG_GLOBAL=/tmp/x git commit -m x",
        ):
            self.assertEqual(self._on_feature(command).decision, "allow", command)

    def test_no_verify_denied_but_short_n_alias_allowed(self) -> None:
        self.assertEqual(self._on_main("git commit --no-verify -m x").decision, "deny")
        self.assertEqual(self._on_main("git commit -n -m x").decision, "allow")

    def test_config_override_flag_denied(self) -> None:
        self.assertEqual(
            self._on_main("git -c core.hooksPath=/tmp/evil commit -m x").decision,
            "deny",
        )

    def test_config_override_flag_denied_when_not_the_first_dash_c(self) -> None:
        self.assertEqual(
            self._on_main(
                "git -c other=y -c core.hooksPath=/tmp/evil commit -m x"
            ).decision,
            "deny",
        )

    def test_env_var_forms_denied(self) -> None:
        for command in (
            "GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp/x GIT_CONFIG_COUNT=1 git commit -m x",
            "GIT_CONFIG_GLOBAL=/tmp/evil git commit -m x",
            "GIT_CONFIG_SYSTEM=/tmp/evil git commit -m x",
            "GIT_CONFIG_PARAMETERS=\"'core.hooksPath='\" git commit -m x",
        ):
            self.assertEqual(self._on_main(command).decision, "deny", command)

    def test_git_config_mutation_forms_denied(self) -> None:
        for command in (
            "git config --unset core.hooksPath",
            "git config --unset-all core.hooksPath",
            "git config --replace-all core.hooksPath /tmp/x",
            "git config --edit",
            "git config core.hooksPath /tmp/x",
            "git config set core.hooksPath /tmp/x",
            "git config unset core.hooksPath",
        ):
            self.assertEqual(self._on_main(command).decision, "deny", command)

    def test_git_config_read_forms_and_chained_reads_allowed(self) -> None:
        for command in (
            "git config --get core.hooksPath",
            "git config core.hooksPath",
            "git config get core.hooksPath",
            "git config core.hooksPath || echo unset",
            "git config --get core.hooksPath && echo ok",
            "git config core.hooksPath | grep -q githooks",
        ):
            self.assertEqual(self._on_main(command).decision, "allow", command)

    def test_dollar_or_backtick_near_hookspath_denied(self) -> None:
        self.assertEqual(
            self._on_main('VAL=""; git config core.hooksPath $VAL').decision, "deny"
        )
        self.assertEqual(
            self._on_main("git config core.hooksPath `echo x`").decision, "deny"
        )

    def test_git_config_file_direct_write_denied_bare_reference_allowed(self) -> None:
        for command in (
            "> .git/config",
            "echo x >> .git/config",
            "sed -i s/x/y/ .git/config",
            "tee .git/config",
        ):
            self.assertEqual(self._on_main(command).decision, "deny", command)
        for command in (
            "cat .git/config",
            "grep hooksPath .git/config",
            "test -f .git/config",
        ):
            self.assertEqual(self._on_main(command).decision, "allow", command)

    def test_combined_benign_and_denied_call_in_one_command_line_is_caught(
        self,
    ) -> None:
        verdict = self._on_main(
            "git config user.name && git config --unset core.hooksPath"
        )
        self.assertEqual(verdict.decision, "deny")

    def test_ordinary_commands_allowed(self) -> None:
        for command in ("ls -la", "git status", "git log --oneline -5"):
            self.assertEqual(self._on_main(command).decision, "allow", command)


class MainTests(unittest.TestCase):
    def _run(self, argv, stdin_text=""):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
            with mock.patch.object(sys, "stdout", out):
                code = guard_rails.main(argv)
        return out.getvalue(), code

    def test_bash_calls_reach_evaluate_as_bash_family(self) -> None:
        """Bash calls are no longer ignored entirely -- they reach evaluate()
        as tool="bash", which dispatches to evaluate_bash_override rather
        than the write-family R2/R3 logic (see BashOverrideTests)."""
        payload = json.dumps(
            {
                "cwd": "/repo",
                "tool_name": "Bash",
                "tool_input": {"command": "git commit"},
            }
        )
        with mock.patch.object(
            guard_rails, "evaluate", return_value=guard_rails.Verdict("allow")
        ) as ev:
            _, code = self._run(["--harness", "claude"], payload)
        self.assertEqual(code, 0)
        ev.assert_called_once()
        (req,), _ = ev.call_args
        self.assertEqual(req.tool, "bash")
        self.assertEqual(req.command, "git commit")

    def test_unparseable_stdin_allows_and_does_not_raise(self) -> None:
        _, code = self._run(["--harness", "claude"], "not json at all")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
