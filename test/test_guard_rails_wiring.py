#!/usr/bin/env python3
"""Every harness must actually reach guard_rails.py.

A guard that is implemented but not wired reads as covered while doing
nothing, which is worse than no guard at all. These assert the wiring rather
than the logic: each harness's config names the script, and links.toml puts
it somewhere the harness will find it.
"""

import json
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "guard_rails.py"


def _links() -> list[dict]:
    return tomllib.loads((REPO_ROOT / "links.toml").read_text())["link"]


@pytest.mark.parametrize("seed", ["claude/settings.json", "claude/settings.work.json"])
def test_claude_seeds_declare_a_pretooluse_guard(seed: str) -> None:
    hooks = json.loads((REPO_ROOT / seed).read_text())["hooks"]
    assert "PreToolUse" in hooks, f"{seed} has no PreToolUse block"
    commands = [h["command"] for group in hooks["PreToolUse"] for h in group["hooks"]]
    assert any(SCRIPT in c for c in commands), commands
    matchers = [group.get("matcher", "") for group in hooks["PreToolUse"]]
    assert any("Write" in m and "Edit" in m for m in matchers), matchers


def test_agy_declares_a_pretooluse_guard() -> None:
    d = json.loads((REPO_ROOT / "agy/hooks.json").read_text())
    groups = [g for hookset in d.values() for g in hookset.get("PreToolUse", [])]
    assert groups, "agy/hooks.json has no PreToolUse group"
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any(SCRIPT in c for c in commands), commands
    # agy names its write tools differently from every other harness.
    matchers = " ".join(g.get("matcher", "") for g in groups)
    assert "write_to_file" in matchers and "replace_file_content" in matchers


def test_copilot_declares_a_pretooluse_guard() -> None:
    path = REPO_ROOT / "copilot/hooks/pre-tool-use.json"
    assert path.exists(), "copilot preToolUse hook file is missing"
    handlers = json.loads(path.read_text())["hooks"]["preToolUse"]
    assert any(SCRIPT in h["bash"] for h in handlers), handlers


def test_opencode_plugin_exists_and_uses_the_before_hook() -> None:
    src = (REPO_ROOT / "opencode/plugin/guard-rails.ts").read_text()
    assert "tool.execute.before" in src
    assert SCRIPT in src
    # opencode blocks by throwing; anything else is silently ignored upstream.
    assert "throw new Error" in src


def test_pi_extension_delegates_to_the_shared_script() -> None:
    src = (REPO_ROOT / "pi/extensions/guard-rails.ts").read_text()
    assert SCRIPT in src, "pi still evaluates the worktree rules on its own"
    assert "block: true" in src


def test_pi_keeps_its_own_interactive_guards() -> None:
    """Those need ctx.ui.confirm() and must not have been swept into the
    shared script."""
    src = (REPO_ROOT / "pi/extensions/guard-rails.ts").read_text()
    for kept in ("isDangerousRm", "sudo", "isProtectedPath", "getGitCommitTarget"):
        assert kept in src, f"pi lost its {kept} guard"


@pytest.mark.parametrize(
    "src_path",
    [
        "claude/scripts/guard_rails.py",
        "copilot/hooks/pre-tool-use.json",
        "opencode/plugin/guard-rails.ts",
    ],
)
def test_new_files_are_linked_into_place(src_path: str) -> None:
    """A production file with no links.toml entry silently never exists at
    the path the harness looks in."""
    assert any(link["src"] == src_path for link in _links()), src_path


def test_the_guard_script_is_stdlib_only() -> None:
    """claude/scripts/ has to run on a machine that never ran `uv sync`."""
    src = (REPO_ROOT / "claude/scripts/guard_rails.py").read_text()
    imports = [
        line.split()[1].split(".")[0]
        for line in src.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    allowed = {
        "argparse",
        "json",
        "os",
        "re",
        "shlex",
        "subprocess",
        "sys",
        "dataclasses",
        "pathlib",
        "cli_common",
    }
    assert set(imports) <= allowed, set(imports) - allowed
