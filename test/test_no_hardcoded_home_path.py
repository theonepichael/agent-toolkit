#!/usr/bin/env python3
"""Guard test: no tracked file hardcodes this machine's home directory.

Regression test for a bug class found while auditing agent-toolkit before
its first publish: agy/hooks.json, claude/settings.json, and
claude/scripts/harness_discovery_check.py all hardcoded this maintainer's
own /home/yanil path, which would silently misbehave (a dead hook, an inert
fallback table) on a coworker's machine. See
~/.claude/data/grill/2026-09-03-atk-publish-remote-hardcoded-paths-plan.md
for the full investigation, including why two other real hits
(pi/CLAUDE_CODE_PARITY.md, pi/prompts/make-skill.md) are deliberately
exempted below rather than fixed by a substring substitution.
"""

import subprocess
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fixture/example paths in test data are not live config -- excluded.
# Verified: git pathspec ":!test" excludes only the test/ directory tree by
# prefix, not any path merely containing the substring "test" --
# claude/scripts/test_dev_status.py, for example, still matches a grep
# scoped with this exclusion.
#
# pi/CLAUDE_CODE_PARITY.md:128 is a deliberate exemption, not a fixture: it
# quotes a past bug's exact wrong value on purpose, as a postmortem record
# ("An earlier version of this file explicitly listed ... that was wrong").
# Rewriting the quote would misrepresent history.
#
# pi/prompts/make-skill.md is excluded wholesale, not just its two
# /home/yanil hits: the file has a third, unrelated reference to the
# maintainer's separate dotfiles repo with no username in it
# (`~/dotfiles/agy/skills/...`) that no pattern here would catch anyway, and
# the file needs a full content review against agent-toolkit's own layout,
# not a substring fix -- tracked as backlog item atk-pi-prompts-dotfiles-refs.
EXEMPT_PATHSPECS = (
    ":!test",
    ":!pi/test",
    ":!pi/CLAUDE_CODE_PARITY.md",
    ":!pi/prompts/make-skill.md",
)

# Two spellings of the same leak: the absolute path, and the shell-tilde
# shorthand for this specific user's home directory. NOT "/Users/yanil"
# (macOS) -- checked, and it also matches opencode/opencode.jsonc's WSL
# drive-mount permission entries (/mnt/c/Users/yanil/...), a different, real
# finding also tracked under atk-pi-prompts-dotfiles-refs rather than
# silently exempted here.
PATTERNS = ("/home/yanil", "~yanil")


class NoHardcodedHomePathTests(unittest.TestCase):
    # git grep is a real subprocess call, and the repo-root conftest.py
    # blocks unmarked real subprocess calls for the whole suite -- this one
    # is read-only and scoped to the checkout (no writes, no network).
    @pytest.mark.allow_real_subprocess
    def test_no_tracked_file_hardcodes_this_machines_home_directory(self) -> None:
        hits: list[str] = []
        for pattern in PATTERNS:
            result = subprocess.run(
                # -I: skip binary files -- a match inside one is noise, not
                # a leaked config path, and would otherwise fail this test
                # on a "Binary file ... matches" line with nothing to fix.
                ["git", "grep", "-I", "-l", pattern, "--", *EXEMPT_PATHSPECS],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            # git grep exits 1 when there are no matches -- that's the pass case.
            if result.returncode not in (0, 1):
                raise RuntimeError(f"git grep failed: {result.stderr}")
            hits += [
                f"{path} (pattern {pattern!r})"
                for path in result.stdout.splitlines()
                if path
            ]
        self.assertEqual(
            hits,
            [],
            "tracked file(s) hardcode this machine's home directory -- a "
            "coworker cloning the repo would get a config, fallback path, "
            "or doc example that only resolves here: " + ", ".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
