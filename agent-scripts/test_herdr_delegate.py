#!/usr/bin/env python3
"""Tests for herdr_delegate.py. Run with: python3 test_herdr_delegate.py

The script's source of truth is this repo (dotfiles' copy was retired in the
cutover; the live ~/.claude/scripts symlink points here). The CLI-contract
smoke tests never invoke a subprocess (help/usage paths never reach the herdr
binary). The launch-path tests fake herdr at the module's herdr() boundary --
the only function that shells out -- so nothing here reaches the real herdr
socket and the conftest sandbox needs no marking.
"""

import io
import os
import sys
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import herdr_delegate


def make_fake_herdr(
    *,
    start_error: str | None = None,
    prompt_error: str | None = None,
    close_error: str | None = None,
) -> tuple[Callable[[list[str]], dict[str, object]], list[list[str]]]:
    """Replace herdr_delegate.herdr with a scripted fake.

    Returns (fake, argvs): ``fake`` answers ``tab create`` with canned JSON and
    ``agent start`` / ``agent prompt`` / ``tab close`` from the given error
    strings (None = success). ``argvs`` records every argv it was given.
    """

    argvs: list[list[str]] = []

    def fake(argv: list[str]) -> dict[str, object]:
        argvs.append(argv)
        if argv[0] == "tab" and argv[1] == "create":
            return {
                "result": {
                    "root_pane": {"pane_id": "w:p1"},
                    "tab": {"tab_id": "w:t1"},
                }
            }
        if argv[0] == "agent" and argv[1] == "start" and start_error:
            raise herdr_delegate.RefusedError(start_error)
        if argv[0] == "agent" and argv[1] == "prompt" and prompt_error:
            raise herdr_delegate.RefusedError(prompt_error)
        if argv[0] == "tab" and argv[1] == "close" and close_error:
            raise herdr_delegate.RefusedError(close_error)
        return {"result": {"type": "ok"}}

    return fake, argvs


def run_launch(fake: Callable[[list[str]], dict[str, object]]) -> int:
    """Run ``launch --slug atk-example`` with herdr faked; return exit code."""
    with (
        mock.patch.object(herdr_delegate, "herdr", fake),
        mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
    ):
        argv = ["launch", "--slug", "atk-example", "--cwd", "/tmp"]
        sys.argv = ["herdr_delegate.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                herdr_delegate.main()
        except SystemExit as e:
            return int(e.code or 0)
        return 0


class LaunchFailureCleanupTests(unittest.TestCase):
    """The tab create -> agent start window.

    A failed ``agent start`` (e.g. agent_name_taken) must not leave the
    freshly created tab behind: the caller created it solely for this agent,
    and herdr leaves pane cleanup to the caller on that error, so nobody else
    will close it -- the contentless unknown-status tab seen after a failed
    2026-09-07 swarm launch.
    """

    def test_agent_start_failure_closes_the_created_tab(self) -> None:
        fake, argvs = make_fake_herdr(start_error="agent_name_taken: nope")
        code = run_launch(fake)
        self.assertEqual(code, 1)
        self.assertIn(["tab", "close", "w:t1"], argvs)

    def test_failed_cleanup_still_reports_the_launch_error(self) -> None:
        fake, argvs = make_fake_herdr(
            start_error="agent_name_taken: nope", close_error="tab close failed"
        )
        code = run_launch(fake)
        self.assertEqual(code, 1)
        # The close was attempted, but the launch error is what surfaces.
        self.assertIn(["tab", "close", "w:t1"], argvs)

    def test_agent_prompt_failure_leaves_the_tab(self) -> None:
        # A failed prompt means the agent DID start; the tab holds a live
        # agent, and closing it would kill it.
        fake, argvs = make_fake_herdr(prompt_error="agent_prompt_stalled")
        code = run_launch(fake)
        self.assertEqual(code, 1)
        self.assertNotIn(["tab", "close", "w:t1"], argvs)

    def test_success_path_never_closes_the_tab(self) -> None:
        fake, argvs = make_fake_herdr()
        code = run_launch(fake)
        self.assertEqual(code, 0)
        self.assertNotIn(["tab", "close", "w:t1"], argvs)


class SmokeTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        """Run main() with the given argv, returning (exit_code, stdout, stderr)."""
        sys.argv = ["herdr_delegate.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                herdr_delegate.main()
        except SystemExit as e:
            return int(e.code or 0), out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_help_exits_zero_and_names_both_subcommands(self) -> None:
        code, out, _ = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("plan", out)
        self.assertIn("launch", out)

    def test_bad_usage_exits_two(self) -> None:
        code, _, _ = self.run_main(["--no-such-flag"])
        self.assertEqual(code, 2)

    def test_launch_requires_a_selector(self) -> None:
        code, _, err = self.run_main(["launch"])
        self.assertEqual(code, 2)
        self.assertIn("--slug", err)


if __name__ == "__main__":
    unittest.main()
