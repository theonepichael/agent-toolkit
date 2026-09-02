#!/usr/bin/env python3
"""Class guard: no hook command in claude/settings*.json forces a non-zero
exit to look like success without going through the explicit allowlist
below. Run with: python3 test_hooks_config.py
"""

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_FILES = [
    REPO_ROOT / "claude" / "settings.json",
    REPO_ROOT / "claude" / "settings.work.json",
]

# Matches the common shell spellings of "unconditionally succeed regardless
# of what ran": trailing `; true`, `|| true`, `|| :`, `; :`, `|| exit 0`,
# `; exit 0` (with or without a trailing `;`). A string-pattern heuristic
# over the handful of one-liners these files actually contain, not a real
# shell parser -- the `$`-anchor scopes it to the command's trailing clause,
# so it doesn't false-positive on a mid-pipeline `exit 0` guard.
_SILENT_SUCCESS_RE = re.compile(r"(?:;|\|\|)\s*(?:true|:|exit\s+0)\s*;?\s*$")

# (event, matcher) pairs allowed to force success -- formatting a saved file
# shouldn't block the agent's turn, so PostToolUse's Write|Edit ruff hook is
# the one deliberate exception. Keyed by identity, not by matching against
# command text, so a future hook that merely *mentions* ruff isn't wrongly
# exempted.
_INTENTIONALLY_SILENT = {("PostToolUse", "Write|Edit")}


def _forces_silent_success(command: str) -> bool:
    return bool(_SILENT_SUCCESS_RE.search(command))


def _iter_hook_commands():
    """Yield (settings_path, event, matcher, command) for every hook command
    in every settings file, regardless of event name -- a future event type
    is covered without this needing an edit."""
    for path in SETTINGS_FILES:
        data = json.loads(path.read_text())
        for event, blocks in data.get("hooks", {}).items():
            for block in blocks:
                matcher = block.get("matcher")
                for entry in block.get("hooks", []):
                    command = entry.get("command")
                    if command is not None:
                        yield path, event, matcher, command


class HooksConfigTests(unittest.TestCase):
    def test_01_no_hook_forces_silent_success_unless_allowlisted(self):
        for path, event, matcher, command in _iter_hook_commands():
            with self.subTest(settings=path.name, event=event, command=command):
                if _forces_silent_success(command):
                    self.assertIn(
                        (event, matcher),
                        _INTENTIONALLY_SILENT,
                        f"{path.name}: {event} hook forces a silent success "
                        f"without being on the allowlist: {command!r}",
                    )

    def test_02_finds_hooks_in_both_files(self):
        # Guards against the walk silently iterating zero hooks (e.g. a
        # schema-shape assumption breaking on one of the two files) and the
        # test above passing vacuously.
        found = list(_iter_hook_commands())
        self.assertGreater(len(found), 0)
        settings_names = {path.name for path, *_ in found}
        self.assertEqual(settings_names, {"settings.json", "settings.work.json"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
