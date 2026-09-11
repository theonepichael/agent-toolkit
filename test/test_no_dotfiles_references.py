#!/usr/bin/env python3
"""Guard test: no tracked file references the dotfiles repo by name.

agent-toolkit is zero-coupled from the origin repo it was split from
(2026-09-02 split, 2026-09-10 CORE_INSTRUCTIONS.md sync-direction flip):
it must be portable to a machine where no dotfiles checkout exists. This
guard scans every tracked file (git ls-files) for the word "dotfiles" and
fails on any hit outside the explicit allowlist below.

MIGRATION.md is excluded wholesale: it is the historical cutover record,
deliberately kept for the planned work-repo transfer.

Allowlist design: every entry is a *narrow* functional substring, not bare
"dotfiles" — a hit only passes if the line carries one of that file's
allowed substrings, each of which names a load-bearing detection of the
real two-repo personal-machine convention (compose-guard checkout
detection, dev_status repo-name mapping, notify's Windows cache dir,
opencode permission rules, container-suite simulated layout). Files with
no entry must be zero-hit. Known limitation, deliberate: a *new* hit that
happens to reuse an existing allowed substring inside an allowlisted file
is not caught here — diff review of allowlisted files is the backstop.

Test files appear here only when they directly exercise allowlisted
functional behavior (the compose-guard tests, the prefix-map tests, the
state-dir-collision regression); every other test file must be zero.
"""

import subprocess
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF = "test/test_no_dotfiles_references.py"
EXCLUDED = {"MIGRATION.md", SELF}

# file -> allowed substrings, each with the functional reason it stays.
ALLOWLIST: dict[str, tuple[str, ...]] = {
    # ── compose-guard: detects the real ~/dotfiles checkout convention ──
    "install.py": (
        "dotfiles recomposes",  # comment naming what the guard protects
        "dotfiles' composed",  # docstring: the file the origin repo composes
        "overwriting dotfiles'",  # docstring continuation of the same
        "dotfiles/scripts/install-with-agent-toolkit.sh",  # the wrapper run instruction
        "dotfiles' own installer",  # docstring: what the wrapper re-runs
        '(ctx.home / "dotfiles")',  # the checkout-detection literal itself
        '"dotfiles is present and composes',  # the user-facing skip message
    ),
    "agent-scripts/link_inspect.py": (
        "the dotfiles repo checkout it",  # is_repo_checkout contract docstring
        "dotfiles recomposes",  # PERSONAL_OVERLAY_SRC_REL comment
        "The file dotfiles actually composes",  # composed-target docstring
        'home / "dotfiles" / "claude"',  # the composed-target literal itself
        "dotfiles/scripts/install-with-agent-",  # recompose-exemption comment
    ),
    # ── dev_status repo-name mapping (prefix map pins "dotfiles": "meta") ──
    "agent-scripts/dev_status_mutation.py": (
        'HARNESS_REPO = "dotfiles"',  # the mapping constant itself
        '"dotfiles": "meta"',  # the prefix-map row
        "name dotfiles goes by",  # docstring explaining the meta- prefix
        "name dotfiles work",  # docstring: which prefixes mean that repo
    ),
    "agent-scripts/dev_status_mutation.py": (
        'HARNESS_REPO = "dotfiles"',  # the mapping constant itself
        '"dotfiles": "meta"',  # the prefix-map row
        "name dotfiles goes by",  # docstring explaining the meta- prefix
        "name dotfiles work",  # docstring: which prefixes mean that repo
    ),
    # ── notify.py Windows icon cache lives under the origin repo's AppData dir ──
    "agent-scripts/notify.py": (
        '"dotfiles" / "icons"',  # pathlib literal
        "Local\\\\dotfiles",  # f-string Windows path
    ),
    # ── opencode permission rules: functional on the personal machine, inert at work ──
    "opencode/opencode.jsonc": (
        '"~/dotfiles',  # the two ~/dotfiles[...] allow rules
    ),
    # ── container suite simulates the personal-machine layout (repo at ~/dotfiles) ──
    "test/run.sh": (
        "/dotfiles",  # the container mount path + copied checkout location
    ),
    "test/scenarios.sh": (
        'REPO_ROOT="$HOME/dotfiles"',  # the simulated checkout location
    ),
    # ── INTERFACES.md renders the allowlisted docstrings verbatim ──
    "INTERFACES.md": (
        "the dotfiles repo checkout it",  # from link_inspect.is_repo_checkout
        "The file dotfiles actually composes",  # from personal_overlay_composed_target
    ),
    # ── tests directly exercising allowlisted functional behavior ──
    "test/test_install.py": (
        'home / "dotfiles"',  # compose-guard checkout simulations
        '"dotfiles" in capsys',  # asserts the functional skip message
        'assert "dotfiles" not in str(ctx.manifest.path)',  # state-dir scoping
        "~/.local/state/dotfiles",  # the origin repo's real state dir (collision bug)
        '/ "state" / "dotfiles"',  # same, as a pathlib literal
        "_when_dotfiles_present_unwrapped",  # compose-guard test name
        "_when_dotfiles_absent",  # compose-guard test name
        "dotfiles' composed global-instructions.md",  # compose-guard docstring
        "no dotfiles checkout",  # compose-guard docstring
        "dotfiles present and unwrapped",  # compose-guard docstring
        "_exempts_dotfiles_composed_personal_overlay",  # compose-guard test name
        "dotfiles legitimately recomposes",  # compose-guard docstring
        "dotfiles present or not",  # compose-guard docstring
        "without_dotfiles",  # compose-guard test name
        "No ~/dotfiles checkout",  # compose-guard docstring
    ),
    "agent-scripts/test_dev_status.py": (
        '"dotfiles"',  # pins the prefix-map row values
        "/dotfiles",  # fixture repo paths the prefix map resolves
        "`dotfiles-`",  # pins the real prefix list
        "dotfiles work",  # comment: what those prefixes mean
    ),
}


class NoDotfilesReferencesTests(unittest.TestCase):
    @pytest.mark.allow_real_subprocess  # git ls-files; read-only
    def test_no_tracked_file_references_the_dotfiles_repo(self) -> None:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        offenders: list[str] = []
        for path in result.stdout.splitlines():
            if path in EXCLUDED or not path:
                continue
            full = REPO_ROOT / path
            if not full.is_file():
                continue
            try:
                text = full.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # binary
            allowed = ALLOWLIST.get(path, ())
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "dotfiles" not in line.lower():
                    continue
                if any(sub in line for sub in allowed):
                    continue
                offenders.append(f"{path}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "tracked file(s) reference the dotfiles repo outside the "
            "functional allowlist — this repo is zero-coupled from it; "
            "purge the reference or, if it is genuinely functional, add a "
            "narrow allowlist entry with its reason:\n"
            + "\n".join(offenders[:15]),
        )


if __name__ == "__main__":
    unittest.main()
