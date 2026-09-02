"""Tests for cli_common.py."""

import argparse
import io
import sys
import unittest

import cli_common


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


if __name__ == "__main__":
    unittest.main(verbosity=1)
