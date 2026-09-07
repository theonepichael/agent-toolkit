#!/usr/bin/env python3
"""Guard test: no hand-authored claude/commands file references `~/dotfiles`.

Regression test for a bug class found while auditing wrong-repo path
references: claude/commands/skill-map.md hardcoded `~/dotfiles/INTERFACES.md`,
which only resolves inside the dotfiles checkout -- in agent-toolkit, the
file the skill step should read is this repo's own root `INTERFACES.md`.
Generated files are not covered here (gen_skills_params.py's edit_root()
already switches its phrasing by repo identity); this guard scopes to
claude/commands/, which is hand-authored.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PATTERN = "~/dotfiles"


def test_no_command_file_hardcodes_dotfiles_checkout_path() -> None:
    hits = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "claude" / "commands").rglob("*.md"))
        if PATTERN in path.read_text(encoding="utf-8")
    ]
    assert hits == [], (
        "claude/commands file(s) hardcode the dotfiles checkout path -- these "
        f"commands ship in agent-toolkit, where '{PATTERN}/...' does not "
        "resolve: " + ", ".join(hits)
    )
