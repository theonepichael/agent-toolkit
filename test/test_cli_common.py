"""Tests for cli_common.py."""

import argparse
import io
import json
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli_common
import pytest


class AddVerbosityArgsTests(unittest.TestCase):
    def test_adds_quiet_and_verbose_flags(self) -> None:
        parser = argparse.ArgumentParser()
        cli_common.add_verbosity_args(parser)
        args = parser.parse_args([])
        self.assertFalse(args.quiet)
        self.assertFalse(args.verbose)

    def test_short_flags_are_accepted(self) -> None:
        parser = argparse.ArgumentParser()
        cli_common.add_verbosity_args(parser)
        quiet_args = parser.parse_args(["-q"])
        self.assertTrue(quiet_args.quiet)
        verbose_args = parser.parse_args(["-v"])
        self.assertTrue(verbose_args.verbose)

    def test_quiet_and_verbose_are_mutually_exclusive(self) -> None:
        parser = argparse.ArgumentParser()
        cli_common.add_verbosity_args(parser)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--quiet", "--verbose"])


class VPrintTests(unittest.TestCase):
    def test_prints_when_verbose(self) -> None:
        out = io.StringIO()
        cli_common.vprint("hello", verbose=True, file=out)
        self.assertEqual(out.getvalue(), "hello\n")

    def test_is_silent_when_not_verbose(self) -> None:
        out = io.StringIO()
        cli_common.vprint("hello", verbose=False, file=out)
        self.assertEqual(out.getvalue(), "")

    def test_defaults_to_stderr(self) -> None:
        captured = io.StringIO()
        original_stderr = sys.stderr
        sys.stderr = captured
        try:
            cli_common.vprint("stderr msg", verbose=True)
        finally:
            sys.stderr = original_stderr
        self.assertEqual(captured.getvalue(), "stderr msg\n")


class QPrintTests(unittest.TestCase):
    def test_prints_when_not_quiet(self) -> None:
        out = io.StringIO()
        cli_common.qprint("hello", quiet=False, file=out)
        self.assertEqual(out.getvalue(), "hello\n")

    def test_is_silent_when_quiet(self) -> None:
        out = io.StringIO()
        cli_common.qprint("hello", quiet=True, file=out)
        self.assertEqual(out.getvalue(), "")

    def test_defaults_to_stdout(self) -> None:
        captured = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured
        try:
            cli_common.qprint("stdout msg", quiet=False)
        finally:
            sys.stdout = original_stdout
        self.assertEqual(captured.getvalue(), "stdout msg\n")


class GetLoggerTests(unittest.TestCase):
    """Tests for get_logger: stderr-only, idempotent, no I/O at call time."""

    USED_NAMES = ("test-log-a", "test-log-b", "test-log-c")

    def tearDown(self) -> None:
        for name in self.USED_NAMES:
            logging.getLogger(name).handlers.clear()

    def test_empty_name_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            cli_common.get_logger("")

    def test_attaches_only_a_stderr_stream_handler(self) -> None:
        logger = cli_common.get_logger("test-log-a")
        stderr_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        ]
        self.assertEqual(len(stderr_handlers), 1)

    def test_never_targets_stdout(self) -> None:
        cli_common.get_logger("test-log-a")
        logger = logging.getLogger("test-log-a")
        for handler in logger.handlers:
            if handler.stream is not None:
                self.assertIsNot(handler.stream, sys.stdout)

    def test_propagate_is_false(self) -> None:
        logger = cli_common.get_logger("test-log-a")
        self.assertFalse(logger.propagate)

    def test_idempotent_no_duplicate_handlers(self) -> None:
        cli_common.get_logger("test-log-a", verbose=True)
        cli_common.get_logger("test-log-a", verbose=False)
        logger = logging.getLogger("test-log-a")
        # Count only our stderr handlers: pytest's logging plugin attaches
        # its own LogCaptureHandler to every non-propagating logger during
        # each test phase and removes it afterwards, so the raw handler
        # count is not ours to assert.
        stderr_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        ]
        self.assertEqual(len(stderr_handlers), 1)

    def test_level_resets_on_repeated_calls(self) -> None:
        name = "test-log-a"
        self.assertEqual(cli_common.get_logger(name, verbose=True).level, logging.DEBUG)
        self.assertEqual(
            cli_common.get_logger(name, verbose=False).level, logging.WARNING
        )
        self.assertEqual(
            cli_common.get_logger(name, verbose=False).level, logging.WARNING
        )

    def test_quiet_suppresses_everything(self) -> None:
        logger = cli_common.get_logger("test-log-a", quiet=True)
        self.assertGreater(logger.level, logging.CRITICAL)

    def test_default_level_is_warning(self) -> None:
        logger = cli_common.get_logger("test-log-a")
        self.assertEqual(logger.level, logging.WARNING)

    def test_performs_no_io_at_call_time(self) -> None:
        with (
            patch("builtins.open") as mock_open,
            patch.object(Path, "mkdir") as mock_mkdir,
        ):
            cli_common.get_logger("test-log-a", verbose=True)
            self.assertFalse(mock_open.called)
            self.assertFalse(mock_mkdir.called)


class AppendJsonlTests(unittest.TestCase):
    """Tests for append_jsonl: JSONL write path, best-effort never-raises."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        logging.getLogger("cli_common").handlers.clear()
        self._tmp.cleanup()

    def test_writes_one_valid_jsonl_line(self) -> None:
        path = self.tmp / "out.jsonl"
        record = {"k": "v", "n": 1}
        cli_common.append_jsonl(path, record)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), record)

    def test_appends_without_clobbering_existing_content(self) -> None:
        path = self.tmp / "out.jsonl"
        cli_common.append_jsonl(path, {"i": 1})
        cli_common.append_jsonl(path, {"i": 2})
        lines = path.read_text().splitlines()
        self.assertEqual([json.loads(line)["i"] for line in lines], [1, 2])

    def test_creates_parent_directories_lazily(self) -> None:
        path = self.tmp / "deep" / "nested" / "out.jsonl"
        self.assertFalse(path.parent.exists())
        cli_common.append_jsonl(path, {"ok": True})
        self.assertTrue(path.exists())

    def test_single_write_call_under_append_binary_mode(self) -> None:
        path = self.tmp / "out.jsonl"
        real_open = Path.open
        with patch.object(
            Path, "open", autospec=True, side_effect=real_open
        ) as mock_open:
            cli_common.append_jsonl(path, {"i": 1})
        mock_open.assert_called_once_with(path, "ab", buffering=0)

    def test_never_raises_when_parent_is_a_file(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        path = blocker / "child" / "out.jsonl"
        self.assertIsNone(cli_common.append_jsonl(path, {"i": 1}))
        self.assertFalse((blocker / "child").exists())

    def test_never_raises_on_unserializable_record(self) -> None:
        path = self.tmp / "out.jsonl"
        self.assertIsNone(cli_common.append_jsonl(path, {"bad": object()}))

    def test_failure_is_debug_logged_not_silent(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        captured = io.StringIO()
        with patch("sys.stderr", new=captured):
            cli_common.get_logger("cli_common", verbose=True)
            cli_common.append_jsonl(blocker / "child" / "out.jsonl", {"i": 1})
        self.assertIn("append_jsonl failed", captured.getvalue())


class RedactSecretsTests(unittest.TestCase):
    """Tests for redact_secrets: named patterns, redact-then-truncate."""

    def test_bearer_token_masked(self) -> None:
        text = "Authorization: Bearer abc123.def456ghi"
        self.assertNotIn("abc123", cli_common.redact_secrets(text))
        self.assertIn("[REDACTED]", cli_common.redact_secrets(text))

    def test_sk_prefix_masked(self) -> None:
        text = "openai key sk-abcdefghij0123456789"
        self.assertNotIn("abcdefghij0123456789", cli_common.redact_secrets(text))

    def test_ghp_and_gho_prefixes_masked(self) -> None:
        text = "ghp_abcdefghij0123456789 and gho_abcdefghij0123456789"
        result = cli_common.redact_secrets(text)
        self.assertNotIn("abcdefghij0123456789", result)

    def test_akia_aws_key_masked(self) -> None:
        text = "aws key AKIAIOSFODNN7EXAMPLE"
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", cli_common.redact_secrets(text))

    def test_assignment_context_masked(self) -> None:
        text = "api_key=abc123 token=xyz789 password=hunter2"
        result = cli_common.redact_secrets(text)
        self.assertNotIn("abc123", result)
        self.assertNotIn("xyz789", result)
        self.assertNotIn("hunter2", result)

    def test_git_sha_and_uuid_untouched(self) -> None:
        sha = "1f2e3d4c5b6a798877665544332211ff0dff1e2d"
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        text = f"commit {sha} on {uuid} in branch feature/log-helper"
        self.assertEqual(cli_common.redact_secrets(text), text)

    def test_redaction_runs_before_truncation(self) -> None:
        # Secret straddles the max_length boundary: must still be masked.
        text = "x" * 195 + " token=supersecretvalue123"
        result = cli_common.redact_secrets(text, max_length=200)
        self.assertLessEqual(len(result), 200)
        self.assertNotIn("supersecret", result)

    def test_input_clamped_before_pattern_pass(self) -> None:
        # Secret sits beyond the max_length + 200 clamp window: the regex
        # pass must never see it.
        text = "a" * 250 + " token=deepsecretvalue456"
        result = cli_common.redact_secrets(text, max_length=200)
        self.assertNotIn("[REDACTED]", result)

    def test_truncates_to_max_length(self) -> None:
        text = "y" * 1000
        self.assertEqual(len(cli_common.redact_secrets(text)), 200)
        self.assertEqual(len(cli_common.redact_secrets(text, max_length=50)), 50)


class AppendJsonlConcurrencyIntegrationTests(unittest.TestCase):
    """Real multi-process concurrency: no existing test covers this."""

    @pytest.mark.allow_real_subprocess  # spawns `python -c` children that
    # only append JSONL lines to a tmp_path file — no network, no real
    # ~/.claude access; this is the concurrency property under test, which
    # a mock cannot exercise.
    def test_concurrent_processes_yield_uncorrupted_lines(self) -> None:
        procs, records_per_proc = 8, 10
        script_dir = Path(cli_common.__file__).parent
        child_code = (
            "import sys\n"
            f"sys.path.insert(0, {str(script_dir)!r})\n"
            "from pathlib import Path\n"
            "import cli_common\n"
            "for i in range(int(sys.argv[2])):\n"
            "    cli_common.append_jsonl(Path(sys.argv[1]),"
            " {'proc': int(sys.argv[3]), 'i': i})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "concurrent.jsonl"
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child_code,
                        str(log_path),
                        str(records_per_proc),
                        str(p),
                    ],
                )
                for p in range(procs)
            ]
            for proc in processes:
                proc.wait(timeout=60)
            lines = log_path.read_text().splitlines()
            self.assertEqual(len(lines), procs * records_per_proc)
            for line in lines:
                record = json.loads(line)  # raises on any corrupted line
                self.assertIn("proc", record)


if __name__ == "__main__":
    unittest.main(verbosity=1)
