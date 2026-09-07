#!/usr/bin/env python3
"""Tests for standup.py. Run with: python3 test_standup.py"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import standup


class ParserVerbosityTests(unittest.TestCase):
    def test_flags_parse_after_every_leaf_subcommand(self) -> None:
        # A leaf added later without an entry here silently loses coverage.
        cases = {
            "fetch": ("cmd_fetch", []),
        }
        for cmd, (target, extra) in cases.items():
            argv = ["standup.py", cmd, *extra, "-q"]
            with (
                patch.object(standup, target) as mock_cmd,
                patch.object(sys, "argv", argv),
            ):
                standup.main()
            self.assertTrue(mock_cmd.call_args.args[0].quiet)


if __name__ == "__main__":
    unittest.main(verbosity=1)
