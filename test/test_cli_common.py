"""Tests for cli_common.py."""

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))  # noqa: E402
import test_bootstrap  # noqa: E402
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

    def test_single_os_write_call_writes_whole_line(self) -> None:
        """The record travels as one bytes buffer written with a single os.write
        under O_APPEND — replaced the old Path.open("ab") assertion."""
        path = self.tmp / "out.jsonl"
        writes: list[tuple[int, bytes]] = []
        real_write = os.write

        def count_write(fd: int, data: bytes) -> int:
            writes.append((fd, data))
            return real_write(fd, data)

        with patch.object(os, "write", side_effect=count_write):
            cli_common.append_jsonl(path, {"i": 1})
        self.assertEqual(len(writes), 1)
        self.assertEqual(json.loads(writes[0][1].decode())["i"], 1)

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

    def test_default_mode_failure_emits_todays_stderr_unconfigured(self) -> None:
        """Criterion 11: a default-mode failure test with NO preconfigured
        logger must still show today's stderr output (the call forces the
        verbose debug level itself)."""
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        captured = io.StringIO()
        with patch("sys.stderr", new=captured):
            cli_common.append_jsonl(blocker / "child" / "out.jsonl", {"i": 1})
        self.assertIn("append_jsonl failed", captured.getvalue())

    def test_silent_mode_emits_no_stderr(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        captured = io.StringIO()
        with patch("sys.stderr", new=captured):
            cli_common.append_jsonl(
                blocker / "child" / "out.jsonl", {"i": 1}, on_error="silent"
            )
        self.assertEqual(captured.getvalue(), "")

    def test_log_mode_swallows_and_debug_logs(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        captured = io.StringIO()
        with patch("sys.stderr", new=captured):
            cli_common.get_logger("cli_common", verbose=True)
            cli_common.append_jsonl(
                blocker / "child" / "out.jsonl", {"i": 1}, on_error="log"
            )
        self.assertIn("append_jsonl failed", captured.getvalue())

    def test_raise_mode_unserializable_record_sets_path(self) -> None:
        """Criterion 1: an unserialisable record raises, before any file is
        opened, with the target path and no partial line."""
        path = self.tmp / "out.jsonl"
        with self.assertRaises(cli_common.JsonlWriteError) as ctx:
            cli_common.append_jsonl(path, {"bad": object()}, on_error="raise")
        self.assertEqual(ctx.exception.path, path)
        self.assertFalse(path.exists())

    def test_raise_mode_unwritable_parent_sets_path(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        path = blocker / "child" / "out.jsonl"
        with self.assertRaises(cli_common.JsonlWriteError) as ctx:
            cli_common.append_jsonl(path, {"i": 1}, on_error="raise")
        self.assertEqual(ctx.exception.path, path)

    def test_raise_mode_unopenable_path_sets_path(self) -> None:
        path = self.tmp / "dir"
        path.mkdir()
        with self.assertRaises(cli_common.JsonlWriteError) as ctx:
            cli_common.append_jsonl(path, {"i": 1}, on_error="raise")
        self.assertEqual(ctx.exception.path, path)

    def test_raise_mode_short_write_sets_path(self) -> None:
        """Criterion 1: a short write is a failure — os.write does not raise,
        so append_jsonl builds its own OSError and raises it."""
        path = self.tmp / "out.jsonl"
        path.write_text("")
        real_write = os.write

        def short_write(fd: int, data: bytes) -> int:
            return real_write(fd, data[:2])

        with patch.object(os, "write", side_effect=short_write):
            with self.assertRaises(cli_common.JsonlWriteError) as ctx:
                cli_common.append_jsonl(path, {"i": 1}, on_error="raise")
        self.assertEqual(ctx.exception.path, path)
        self.assertIn("short write", str(ctx.exception.__cause__))

    def test_raise_mode_write_and_close_failure_surfaces_write(self) -> None:
        """Criterion 12: when write and close both fail, the write error
        surfaces and the close error is suppressed."""
        path = self.tmp / "out.jsonl"
        path.write_text("")

        def bad_write(fd: int, data: bytes) -> int:
            raise OSError("write boom")

        with patch.object(os, "write", side_effect=bad_write), patch.object(
            os, "close", side_effect=OSError("close boom")
        ):
            with self.assertRaises(cli_common.JsonlWriteError) as ctx:
                cli_common.append_jsonl(path, {"i": 1}, on_error="raise")
        self.assertIn("write boom", str(ctx.exception.__cause__))
        self.assertNotIn("close boom", str(ctx.exception.__cause__))

    def test_raise_mode_close_failure_after_write_sets_path(self) -> None:
        """Criterion 12: a close failure after a successful write is itself
        the failure."""
        path = self.tmp / "out.jsonl"
        path.write_text("")

        def bad_close(fd: int) -> None:
            raise OSError("close boom")

        with patch.object(os, "close", side_effect=bad_close):
            with self.assertRaises(cli_common.JsonlWriteError) as ctx:
                cli_common.append_jsonl(path, {"i": 1}, on_error="raise")
        self.assertEqual(ctx.exception.path, path)
        self.assertIn("close boom", str(ctx.exception.__cause__))

    def test_default_mode_uses_todays_creation_mode(self) -> None:
        """Criterion 3: a new file gets today's default mode (0o666 masked),
        not 0o600 — set the umask so the assertion is umask-independent."""
        old = os.umask(0o022)
        try:
            path = self.tmp / "out.jsonl"
            cli_common.append_jsonl(path, {"i": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o666 & ~0o022)
        finally:
            os.umask(old)

    def test_explicit_mode_600_for_new_file(self) -> None:
        old = os.umask(0o022)
        try:
            path = self.tmp / "out.jsonl"
            cli_common.append_jsonl(path, {"i": 1}, mode=0o600)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600 & ~0o022)
        finally:
            os.umask(old)

    def test_existing_file_mode_unchanged(self) -> None:
        path = self.tmp / "out.jsonl"
        path.write_text("")
        os.chmod(path, 0o600)
        cli_common.append_jsonl(path, {"i": 1}, mode=0o666)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_rotation_under_max_bytes(self) -> None:
        """Phase A: when the file stays under the cap, rotation never fires
        and appends accumulate normally."""
        path = self.tmp / "out.jsonl"
        cli_common.append_jsonl(path, {"i": 1}, max_bytes=1024)
        self.assertFalse(path.with_suffix(path.suffix + ".1").exists())
        cli_common.append_jsonl(path, {"i": 2}, max_bytes=1024)
        self.assertFalse(path.with_suffix(path.suffix + ".1").exists())
        self.assertEqual(
            [json.loads(line)["i"] for line in path.read_text().splitlines()], [1, 2]
        )

    def test_rotation_keeps_one_generation(self) -> None:
        """Phase A: when the existing file already exceeds max_bytes, it is
        rotated to <name>.1 and a fresh file holds exactly the new line."""
        path = self.tmp / "out.jsonl"
        path.write_text(json.dumps({"i": 0}) + "\n" + "x" * 1024)
        max_bytes = path.stat().st_size - 512  # already over the cap
        cli_common.append_jsonl(path, {"i": 1}, max_bytes=max_bytes)
        rotated = path.with_suffix(path.suffix + ".1")
        self.assertTrue(rotated.exists(), "expected a .1 rotated sibling")
        self.assertEqual(len(rotated.read_text().splitlines()), 2)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["i"], 1)

    def test_rotation_replaces_existing_generation(self) -> None:
        """Phase A: a second rotation overwrites the stale .1, not a .2."""
        path = self.tmp / "out.jsonl"
        rotated = path.with_suffix(path.suffix + ".1")
        rotated.write_text(json.dumps({"old": True}) + "\n")
        path.write_text(json.dumps({"i": 0}) + "\n" + "x" * 1024)
        max_bytes = path.stat().st_size - 512
        cli_common.append_jsonl(path, {"i": 1}, max_bytes=max_bytes)
        lines = path.read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["i"], 1)
        # One generation only: the .1 holds the just-rotated prior run, not a
        # .2 sibling, and the pre-seeded stale .1 content is overwritten.
        self.assertFalse(path.with_suffix(".jsonl.2").exists())
        self.assertIn("i", rotated.read_text())

    def test_rotation_race_stat_gone_does_not_raise(self) -> None:
        """Phase A fix: a concurrent rotation can make stat() raise
        FileNotFoundError though exists() just returned True (the other writer
        renamed the file away in between). append_jsonl must swallow it — the
        append then creates a fresh file — and never raise into the caller,
        even with on_error='raise'."""
        class StatGonePath(type(self.tmp)):
            def stat(self, *, follow_symlinks: bool = True) -> object:
                raise FileNotFoundError

        path = StatGonePath(self.tmp / "out.jsonl")
        cli_common.append_jsonl(path, {"i": 1}, max_bytes=1, on_error="raise")
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["i"], 1)

    def test_rotation_failure_does_not_raise_or_block_append(self) -> None:
        """Phase A fix: a failed rotation (os.replace raises) is logged at
        debug and skipped, never honoured via on_error='raise', and the append
        still proceeds — the existing line is retained and the new one written."""
        path = self.tmp / "out.jsonl"
        path.write_text(json.dumps({"i": 0, "pad": "x" * 1024}) + "\n")

        def bad_replace(src: object, dst: object) -> object:
            raise OSError("rotation boom")

        with patch.object(os, "replace", side_effect=bad_replace):
            cli_common.append_jsonl(path, {"i": 1}, max_bytes=1, on_error="raise")
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)  # old line kept, new line appended
        self.assertEqual(json.loads(lines[1])["i"], 1)

    def test_rotation_failure_is_silent_when_on_error_silent(self) -> None:
        """Follow-up: a non-FileNotFoundError OSError during rotation (e.g.
        os.replace raises PermissionError) must emit nothing on stderr when
        the caller asked to stay silent -- the timing writer passes
        on_error='silent', so its rotation hiccups must not leak to stderr."""
        path = self.tmp / "out.jsonl"
        path.write_text(json.dumps({"i": 0, "pad": "x" * 1024}) + "\n")

        def bad_replace(src: object, dst: object) -> object:
            raise PermissionError("rotation denied")

        captured = io.StringIO()
        with (
            patch.object(os, "replace", side_effect=bad_replace),
            patch("sys.stderr", new=captured),
        ):
            cli_common.append_jsonl(
                path, {"i": 1}, max_bytes=1, on_error="silent"
            )
        self.assertEqual(captured.getvalue(), "")
        # The append still proceeds despite the silent rotation failure.
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)  # old line kept, new line appended
        self.assertEqual(json.loads(lines[1])["i"], 1)


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
    test_bootstrap.run_unittest_main(verbosity=1)
