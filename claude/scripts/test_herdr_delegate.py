#!/usr/bin/env python3
"""Smoke test for herdr_delegate.py. Run with: python3 test_herdr_delegate.py

The script itself is a verbatim sync from dotfiles (see sync_from_dotfiles.py)
-- this file never tests its internals, only that the copy is present,
importable, and its CLI contract is intact: --help exits 0 and describes both
subcommands; bad usage exits 2. No subprocess is invoked (help/usage paths
never reach the herdr binary), so the conftest sandbox needs no marking.
"""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import herdr_delegate


class SmokeTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        """Run main() with the given argv, returning (exit_code, stdout, stderr)."""
        old_argv = sys.argv
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
