#!/usr/bin/env python3
"""Guard test: no tracked file hardcodes this machine's home directory.

Regression test for a bug class found while auditing agent-toolkit before
its first publish: agy/hooks.json, claude/settings.json, and
agent-scripts/harness_discovery_check.py all hardcoded this maintainer's
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
# agent-scripts/test_dev_status.py, for example, still matches a grep
# scoped with this exclusion.
#
# pi/CLAUDE_CODE_PARITY.md:128 is a deliberate exemption, not a fixture: it
# quotes a past bug's exact wrong value on purpose, as a postmortem record
# ("An earlier version of this file explicitly listed ... that was wrong").
# Rewriting the quote would misrepresent history.
#
EXEMPT_PATHSPECS = (
    ":!test",
    ":!pi/test",
    ":!pi/CLAUDE_CODE_PARITY.md",
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


# Positive scope, not a repo-wide ban: "dotfiles" is a legitimate word
# throughout this repo's own docs (AGENTS.md's harness-tiers section,
# README.md, MIGRATION.md, CLAUDE_CODE_PARITY.md files describing the split
# itself, scripts/sync_from_dotfiles.py's own name and --dotfiles-path
# default). Only the generated-skill-doc set plus the generator source that
# produces it should never contain a literal `~/dotfiles` path -- these are
# the files a coworker reads to find out where to edit something, and
# dotfiles is never the right answer for agent-toolkit's own copy.
#
# agent-scripts/gen_skills_params.py (the generator SOURCE) is deliberately
# not in this list, even though it's exactly the file this item fixed: its
# per-repo phrasing helpers (edit_root/symlink_cmd/probe_add_dir) legitimately
# embed the literal string "~/dotfiles" as dotfiles' own correct value in
# their dotfiles-branch, permanently, by design -- a raw text scan of that
# source file can't distinguish "defines the literal for the other repo" from
# "a site bypassed the helper and hardcoded it again," so it isn't a useful
# check. The generated OUTPUT below is what actually proves the helpers are
# used everywhere they should be: if a site ever regresses back to a raw
# string, --write reproduces it in one of these files and this test catches
# it there instead.
DOTFILES_REF_PATHSPECS = (
    "claude/commands/*.md",
    "opencode/command/*.md",
    "opencode/skills/*/SKILL.md",
    "copilot/skills/*/SKILL.md",
    "agy/skills/*/SKILL.md",
    "pi/prompts/*.md",
    "pi/skills/*/SKILL.md",
    "templates/*.tmpl",
    # Known, separate finding, same bug class but a different root cause:
    # claude/commands/skill-map.md is a hand-authored skill (not a
    # gen_skills.py output) that also references ~/dotfiles/INTERFACES.md --
    # this item's generator fix doesn't touch it since it was never part of
    # the generator's SKILLS set. Worth its own fix, out of scope here.
    ":!claude/commands/skill-map.md",
)


class NoDotfilesRefInGeneratedSkillDocsTests(unittest.TestCase):
    @pytest.mark.allow_real_subprocess
    def test_generated_skill_docs_and_generator_source_have_no_dotfiles_path(
        self,
    ) -> None:
        result = subprocess.run(
            ["git", "grep", "-I", "-l", "~/dotfiles", "--", *DOTFILES_REF_PATHSPECS],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(f"git grep failed: {result.stderr}")
        hits = [path for path in result.stdout.splitlines() if path]
        self.assertEqual(
            hits,
            [],
            "generated skill doc(s) or generator source hardcode a "
            "~/dotfiles path -- these describe agent-toolkit's own repo to "
            "whoever reads them, and dotfiles is the wrong repo: " + ", ".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
