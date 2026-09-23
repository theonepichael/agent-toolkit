#!/usr/bin/env python3
"""Regression tests for herdr SessionStart/PreInvocation hook commands.

Ensures that hook commands in claude/settings.json and agy/hooks.json tolerate
a missing herdr-agent-state.sh script on systems without herdr (e.g. Fedora),
exiting cleanly with status 0 rather than failing with exit code 127.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.allow_real_subprocess  # runs bash in a sandboxed tmp_path; touches no production state
@pytest.mark.regression(
    "herdr-hook-tolerates-missing-script",
    "assert res.returncode == 0",
)
def test_claude_settings_herdr_hook_tolerates_missing_script(tmp_path: Path) -> None:
    settings = json.loads((REPO_ROOT / "claude/settings.json").read_text(encoding="utf-8"))
    groups = settings.get("hooks", {}).get("SessionStart", [])
    herdr_group = next((g for g in groups if g.get("matcher") == "*"), None)
    assert herdr_group is not None, "claude/settings.json missing SessionStart group with matcher '*'"
    command = herdr_group["hooks"][0]["command"]

    # In an empty HOME where ~/.claude/hooks/herdr-agent-state.sh does not exist,
    # the command must exit with 0.
    env = dict(os.environ, HOME=str(tmp_path))
    res = subprocess.run(
        ["bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"command failed when script missing: {res.stderr}"


@pytest.mark.allow_real_subprocess  # runs bash in a sandboxed tmp_path; touches no production state
def test_claude_settings_herdr_hook_runs_when_present(tmp_path: Path) -> None:
    settings = json.loads((REPO_ROOT / "claude/settings.json").read_text(encoding="utf-8"))
    groups = settings.get("hooks", {}).get("SessionStart", [])
    herdr_group = next((g for g in groups if g.get("matcher") == "*"), None)
    assert herdr_group is not None
    command = herdr_group["hooks"][0]["command"]

    hook_dir = tmp_path / ".claude" / "hooks"
    hook_dir.mkdir(parents=True)
    hook_script = hook_dir / "herdr-agent-state.sh"
    marker_file = tmp_path / "marker.txt"
    hook_script.write_text(f'#!/bin/sh\necho "$1" > "{marker_file}"\n')
    hook_script.chmod(0o755)

    env = dict(os.environ, HOME=str(tmp_path))
    res = subprocess.run(
        ["bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"command failed: {res.stderr}"
    assert marker_file.read_text().strip() == "session"


@pytest.mark.allow_real_subprocess  # runs bash in a sandboxed tmp_path; touches no production state
def test_agy_hooks_herdr_hook_tolerates_missing_script(tmp_path: Path) -> None:
    hooks = json.loads((REPO_ROOT / "agy/hooks.json").read_text(encoding="utf-8"))
    herdr_pre = hooks.get("herdr", {}).get("PreInvocation", [])
    assert herdr_pre, "agy/hooks.json missing herdr.PreInvocation"
    command = herdr_pre[0]["command"]

    env = dict(os.environ, HOME=str(tmp_path))
    res = subprocess.run(
        ["bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"command failed when script missing: {res.stderr}"


@pytest.mark.allow_real_subprocess  # runs bash in a sandboxed tmp_path; touches no production state
def test_agy_hooks_herdr_hook_runs_when_present(tmp_path: Path) -> None:
    hooks = json.loads((REPO_ROOT / "agy/hooks.json").read_text(encoding="utf-8"))
    herdr_pre = hooks.get("herdr", {}).get("PreInvocation", [])
    assert herdr_pre
    command = herdr_pre[0]["command"]

    hook_dir = tmp_path / ".gemini" / "config" / "hooks"
    hook_dir.mkdir(parents=True)
    hook_script = hook_dir / "herdr-agent-state.sh"
    marker_file = tmp_path / "marker.txt"
    hook_script.write_text(f'#!/bin/sh\necho "$1" > "{marker_file}"\n')
    hook_script.chmod(0o755)

    env = dict(os.environ, HOME=str(tmp_path))
    res = subprocess.run(
        ["bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"command failed: {res.stderr}"
    assert marker_file.read_text().strip() == "session"
