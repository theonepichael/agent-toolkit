"""Opt-in timing behavior and backend retry/fallback coverage."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import cli_common
import llm_backends
import second_opinion


class TimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AGENT_TOOLKIT_TIMING": "1", "XDG_STATE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = Path(self.tmp.name) / "agent-toolkit/timing.jsonl"

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_disabled_and_unwritable_are_silent(self) -> None:
        with patch.dict(os.environ, {"AGENT_TOOLKIT_TIMING": "0"}):
            with cli_common.timing_span("disabled"):
                pass
        self.assertFalse(self.path.exists())
        output = io.StringIO()
        with patch.object(cli_common.os, "open", side_effect=OSError("secret")), redirect_stderr(output):
            with cli_common.timing_span("unwritable"):
                pass
        self.assertEqual(output.getvalue(), "")

    def test_nested_identity_and_exceptions(self) -> None:
        with cli_common.timing_span("outer"):
            with self.assertRaisesRegex(ValueError, "private"):
                with cli_common.timing_span("inner"):
                    raise ValueError("private")
        inner, outer = self.records()
        self.assertEqual(inner["parent_id"], outer["span_id"])
        self.assertEqual(inner["trace_id"], outer["trace_id"])
        self.assertEqual(inner["outcome"], "error")
        self.assertEqual(outer["outcome"], "success")
        self.assertNotIn("private", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with cli_common.timing_span("next"):
            pass
        self.assertIsNone(self.records()[-1]["parent_id"])

    def test_timeout_retry_success(self) -> None:
        first = Mock()
        first.communicate.side_effect = [subprocess.TimeoutExpired("private", 1), ("", "")]
        second = Mock(returncode=0)
        second.communicate.return_value = ("private output", "")
        with patch.object(llm_backends.subprocess, "Popen", side_effect=[first, second]), patch.object(llm_backends, "_kill_active_process"):
            result = llm_backends._run_command(["private argv"], 1, retries=1)
        self.assertEqual(result, (0, "private output", ""))
        rows = [r for r in self.records() if r["name"] == "backend_attempt"]
        self.assertEqual([r["outcome"] for r in rows], ["timeout", "success"])
        self.assertEqual([r["attempt"] for r in rows], [1, 2])
        self.assertNotIn("private", self.path.read_text())

    def test_terminal_timeout_and_start_failure(self) -> None:
        proc = Mock()
        proc.communicate.side_effect = [subprocess.TimeoutExpired("secret", 1), ("", "")]
        with patch.object(llm_backends.subprocess, "Popen", return_value=proc), patch.object(llm_backends, "_kill_active_process"), self.assertRaises(llm_backends.BackendTimeoutError):
            llm_backends._run_command(["secret"], 1)
        self.assertEqual(next(r for r in self.records() if r["name"] == "backend_attempt")["outcome"], "timeout")
        with patch.object(llm_backends.subprocess, "Popen", side_effect=OSError("secret")), self.assertRaises(llm_backends.BackendError):
            llm_backends._run_command(["secret"], 1)
        self.assertEqual(self.records()[-1]["outcome"], "error")

    def test_custom_opencode_and_fallback_linked(self) -> None:
        plan = Path(self.tmp.name) / "plan.md"
        plan.write_text("Private plan content")
        proc = Mock(returncode=0)
        proc.communicate.return_value = ('{"type":"text","part":{"text":"critique"}}\n', "")
        # Use main's actual parser to avoid coupling this test to argument destination names.
        args = second_opinion.build_parser().parse_args(["review", str(plan), "--quiet"])
        runners = {"agy": Mock(side_effect=llm_backends.BackendError("private")), "opencode": second_opinion.run_opencode}
        with patch.object(second_opinion, "available_backends", return_value=["agy", "opencode"]), patch.dict(second_opinion.BACKEND_RUNNERS, runners), patch.object(llm_backends, "build_isolated_command", return_value=["fake"]), patch.object(llm_backends.subprocess, "Popen", return_value=proc), redirect_stdout(io.StringIO()):
            with cli_common.timing_span("command"):
                second_opinion.cmd_review(args)
        rows = self.records()
        backends = [r for r in rows if r["name"] == "backend"]
        self.assertEqual([r["candidate"] for r in backends], [1, 2])
        self.assertEqual([r["outcome"] for r in backends], ["error", "success"])
        attempt = next(r for r in rows if r["name"] == "backend_attempt")
        call = next(r for r in rows if r["name"] == "backend_call")
        self.assertEqual(attempt["parent_id"], call["span_id"])
        self.assertEqual(call["parent_id"], backends[1]["span_id"])
        self.assertEqual(len({r["trace_id"] for r in rows}), 1)
        self.assertNotIn("Private", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
