#!/usr/bin/env python3
"""Regression tests for the harness-neutral scripts directory layout.

The cross-harness runtime scripts moved from ``claude/scripts/`` to
``agent-scripts/`` (repo-side only). The runtime install dest
``~/.claude/scripts/`` is the permanent accepted shape — these tests pin
that: every links.toml ``src`` must live under ``agent-scripts/`` and
resolve on disk, and the dest set must stay byte-identical to the frozen
pre-move set (no dest may drift, appear, or vanish).

They also sweep tracked text for dead repo-path references —
``claude/scripts`` not preceded by a runtime-path marker — so the old
name cannot creep back through edits. Structural checks are the
load-bearing guard; the text sweep is belt-and-braces against prose.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Frozen dest set (32 script links + the managed_dir audit entry), updated
# in lockstep with each deliberate new agent-scripts/ addition -- a drift
# here must be an intentional edit to this list, never a silent add/remove.
# agent-toolkit's links.toml must keep exactly these dests pointing at
# ~/.claude/scripts; the runtime path is permanent (see the migration-cutover
# item's out-of-scope record: XDG relocation rejected).
FROZEN_SCRIPT_DESTS = frozenset(
    f"~/.claude/scripts/{name}"
    for name in (
        "analyze_sessions.py",
        "cli_common.py",
        "dev_status.py",
        "dev_status_formatting.py",
        "dev_status_impl.py",
        "dev_status_storage.py",
        "bundle_drift_check.py",
        "gen_interfaces.py",
        "gen_second_opinion.py",
        "gen_shell_completion.py",
        "gen_skills.py",
        "gen_skills_params.py",
        "grill.py",
        "guard_rails.py",
        "harness_discovery_check.py",
        "herdr_delegate.py",
        "link_drift_check.py",
        "link_inspect.py",
        "llm_backends.py",
        "notify.py",
        "outlook_calendar.py",
        "outlook_email.py",
        "refresh_guidance.py",
        "second_opinion.py",
        "sessionstart_checks.py",
        "settings_seed.py",
        "settings_seed_drift_check.py",
        "standup.py",
        "standup_adapters.py",
        "statusline.py",
        "seed_hook_subset_guard.py",
        "to_tickets_runner.py",
        "vitals_promotion.py",
    )
)
MANAGED_DIR_DEST = "~/.claude/scripts"

# Files where a bare `claude/scripts` mention is legitimate:
# - MIGRATION.md: historical narrative describing the origin repo's own
#   claude/scripts (gen_core_instructions.py lives there, not here).
TEXT_SWEEP_ALLOWLIST: frozenset[Path] = frozenset(
    {
        # this file itself: its patterns, frozen dest literals, and failure
        # messages necessarily contain the old-path spelling
        Path("test/test_agent_scripts_layout.py"),
        Path("MIGRATION.md"),
    }
)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [Path(p) for p in out.split("\0") if p]


def _load_links() -> list[dict[str, object]]:
    with open(REPO_ROOT / "links.toml", "rb") as f:
        data = tomllib.load(f)
    return data.get("link", [])


@pytest.mark.allow_real_subprocess  # reads the git index; writes nothing
def test_no_claude_scripts_dir_is_tracked() -> None:
    assert not (REPO_ROOT / "claude" / "scripts").exists(), (
        "claude/scripts/ still exists; cross-harness scripts live in agent-scripts/ now"
    )
    tracked = _tracked_files()
    stale = [p for p in tracked if p.parts[:2] == ("claude", "scripts")]
    assert not stale, f"tracked files still under claude/scripts/: {stale[:5]}"
    moved = [p for p in tracked if p.parts[0] == "agent-scripts"]
    assert moved, "agent-scripts/ has no tracked files"


def test_links_toml_srcs_live_in_agent_scripts() -> None:
    links = _load_links()
    script_links = [
        entry
        for entry in links
        if isinstance(entry.get("src"), str)
        and "~/.claude/scripts" in str(entry.get("dest", ""))
    ]
    assert len(script_links) == 33, f"expected 33 script links, got {len(script_links)}"
    bad = [
        entry["src"]
        for entry in script_links
        if not str(entry["src"]).startswith("agent-scripts/")
    ]
    assert not bad, f"script links whose src is not under agent-scripts/: {bad}"
    missing = [
        entry["src"]
        for entry in script_links
        if not (REPO_ROOT / str(entry["src"])).is_file()
    ]
    assert not missing, f"script links whose src does not resolve: {missing}"


def test_script_dests_frozen() -> None:
    links = _load_links()
    dests = {
        str(entry["dest"])
        for entry in links
        if isinstance(entry.get("dest"), str)
        and str(entry["dest"]).startswith("~/.claude/scripts")
    }
    script_dests = frozenset(d for d in dests if d != MANAGED_DIR_DEST)
    assert script_dests == FROZEN_SCRIPT_DESTS, (
        f"dest drift: added={sorted(script_dests - FROZEN_SCRIPT_DESTS)} "
        f"removed={sorted(FROZEN_SCRIPT_DESTS - script_dests)}"
    )
    with open(REPO_ROOT / "links.toml", "rb") as f:
        data = tomllib.load(f)
    managed = [
        m for m in data.get("managed_dir", []) if m.get("dest") == MANAGED_DIR_DEST
    ]
    assert managed, f"managed_dir entry for {MANAGED_DIR_DEST} disappeared"


@pytest.mark.allow_real_subprocess  # reads the git index; writes nothing
def test_no_dead_repo_path_references() -> None:
    # Matches `claude/scripts` only when it starts a path segment in repo
    # context: any preceding `.` means a home-anchored runtime form
    # (`~/.claude/scripts`, `$HOME/.claude/scripts`,
    # `${process.env.HOME}/.claude/scripts`, fake-home `".claude/scripts`
    # fixtures) — the installed location, must stay. Everything else is a
    # dead repo-path reference.
    pattern = re.compile(r"(?<!\.)claude/scripts")
    offenders: list[tuple[Path, int]] = []
    for path in _tracked_files():
        if path in TEXT_SWEEP_ALLOWLIST:
            continue
        full = REPO_ROOT / path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append((path, lineno))
    assert not offenders, (
        "dead repo-path references to claude/scripts/ (runtime-path forms "
        f"~/.claude/scripts are fine): {offenders[:10]}"
    )


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
