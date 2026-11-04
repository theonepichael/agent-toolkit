#!/usr/bin/env python3
"""agy's shell tool (``run_command``) is now bash-family in guard_rails.py:
the same no-commit-on-main hook-defeat checks Claude Code's Bash tool gets,
plus agy-only ``sudo`` and ``rm -rf`` deny matching pi's guard-rails.ts.

Tests feed stdin fixtures in agy's hook contract (``{toolCall:{name,args}}``)
through ``guard_rails.main(["--harness","agy"])`` with mocked stdin/stdout,
under the repo-root conftest's sandboxed HOME. No real subprocess, no
production-path writes.
"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import test_bootstrap  # noqa: E402

import guard_rails  # noqa: E402
from pytest_shim import pytest  # noqa: E402


_MAIN_INFO = guard_rails.RepoInfo(
    toplevel="/repo",
    common_dir="/repo/.git",
    is_worktree=False,
    is_bare=False,
    branch="main",
)


def _payload(command: str, cwd: str | None = None) -> str:
    """An agy PreToolUse stdin payload for a ``run_command`` tool call."""
    args: dict[str, str] = {"CommandLine": command}
    if cwd is not None:
        args["Cwd"] = cwd
    return json.dumps({"toolCall": {"name": "run_command", "args": args}})


class AgyBashParsingTests(unittest.TestCase):
    def test_run_command_parses_to_bash_family(self) -> None:
        req = guard_rails.parse_payload(
            "agy",
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "ls -la", "Cwd": "/repo"},
                }
            },
        )
        self.assertEqual(req.tool, "bash")
        self.assertEqual(req.command, "ls -la")
        self.assertEqual(req.cwd, "/repo")

    def test_write_tool_is_still_write_family(self) -> None:
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


class AgyBashVerdictTests(unittest.TestCase):
    def _run(self, command: str, cwd: str | None = None) -> tuple[str, int]:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(_payload(command, cwd))):
            with mock.patch.object(sys, "stdout", out):
                code = guard_rails.main(["--harness", "agy"])
        return out.getvalue(), code

    def test_ordinary_command_allowed(self) -> None:
        with mock.patch.object(guard_rails, "repo_info", return_value=None):
            out, code = self._run("ls -la", "/repo")
        self.assertEqual(code, 0)
        self.assertIn('"decision": "allow"', out)

    @pytest.mark.regression(
        "agy-bash-sudo-denied",
        "AssertionError: '\"decision\": \"deny\"' not found in "
        "'{\"decision\": \"allow\"}\\n'",
    )
    def test_sudo_denied(self) -> None:
        out, code = self._run("sudo apt update")
        self.assertEqual(code, 0)
        self.assertIn('"decision": "deny"', out)
        self.assertIn("sudo", out)

    @pytest.mark.regression(
        "agy-bash-rmrf-denied",
        "AssertionError: '\"decision\": \"deny\"' not found in "
        "'{\"decision\": \"allow\"}\\n'",
    )
    def test_rm_rf_denied(self) -> None:
        out, code = self._run("rm -rf /tmp/scratch")
        self.assertEqual(code, 0)
        self.assertIn('"decision": "deny"', out)
        self.assertIn("rm", out)

    @pytest.mark.regression(
        "agy-bash-hookdefeat-denied",
        "AssertionError: '\"decision\": \"deny\"' not found in "
        "'{\"decision\": \"allow\"}\\n'",
    )
    def test_hook_defeat_denied_on_protected_branch(self) -> None:
        with mock.patch.object(guard_rails, "repo_info", return_value=_MAIN_INFO):
            out, code = self._run("git commit --no-verify -m x", "/repo")
        self.assertEqual(code, 0)
        self.assertIn('"decision": "deny"', out)
        self.assertIn("no-verify", out)

    def test_rm_double_dash_rf_is_a_filename_not_flags(self) -> None:
        """Port fidelity: pi's isDangerousRm splits the flag section at
        whitespace-``--``-whitespace, so ``rm -- -rf`` (a file literally named
        ``-rf``) has no flags and is allowed."""
        out, code = self._run("rm -- -rf")
        self.assertEqual(code, 0)
        self.assertIn('"decision": "allow"', out)

    @pytest.mark.regression(
        "agy-bash-multiline-rmrf-denied",
        "AssertionError: '\"decision\": \"deny\"' not found in "
        "'{\"decision\": \"allow\"}\n'",
    )
    def test_multiline_rm_rf_denied(self) -> None:
        """A newline-separated second command must not hide behind the first
        command's ``--`` filename marker."""
        out, code = self._run("rm -- /tmp/old-file\nrm -rf /tmp/scratch")
        self.assertEqual(code, 0)
        self.assertIn('"decision": "deny"', out)

    @pytest.mark.regression(
        "agy-bash-line-continuation-rmrf-denied",
        "AssertionError: '\"decision\": \"deny\"' not found in "
        "'{\"decision\": \"allow\"}\n'",
    )
    def test_line_continuation_rm_rf_denied(self) -> None:
        """A backslash-newline line continuation must not split the flags of
        one ``rm -r -f`` into two lines that each read as safe."""
        out, code = self._run("rm -r \\\n-f /tmp/scratch")
        self.assertEqual(code, 0)
        self.assertIn('"decision": "deny"', out)

    def test_guard_rails_off_bypasses_destructive_deny(self) -> None:
        with mock.patch.dict("os.environ", {"GUARD_RAILS_OFF": "1"}):
            for command in ("sudo apt update", "rm -rf /tmp/scratch"):
                out, code = self._run(command)
                self.assertEqual(code, 0, command)
                self.assertIn('"decision": "allow"', out, command)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
