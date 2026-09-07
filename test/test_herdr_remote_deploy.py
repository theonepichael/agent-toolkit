#!/usr/bin/env python3
"""Regression tests for the shipped herdr_remote deploy artifacts.

The repo sets ``[tool.uv] package = false``, so ``python -m herdr_remote``
only resolves when the process cwd is the repo root — ``uv run --project``
does *not* cd there. Anything invoking the module must therefore pin the
cwd (systemd ``WorkingDirectory=`` / ``uv run --directory``), or it fails
with ModuleNotFoundError when run from anywhere else. Pure file checks.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICE = REPO_ROOT / "herdr_remote" / "deploy" / "herdr-remote-bridge.service"
README = REPO_ROOT / "herdr_remote" / "deploy" / "README.md"
DEPLOY_SH = REPO_ROOT / "herdr_remote" / "deploy" / "deploy.sh"

TOOLKIT = "%h/Workspace/agent-toolkit"


def test_service_sets_working_directory_to_repo_root() -> None:
    text = SERVICE.read_text()
    lines = [ln.strip() for ln in text.splitlines()]
    working_dirs = [
        ln
        for ln in lines
        if ln.startswith("WorkingDirectory=")
        and not ln.startswith("WorkingDirectory=#")
    ]
    assert working_dirs, f"{SERVICE.name} sets no WorkingDirectory"
    assert working_dirs == [f"WorkingDirectory={TOOLKIT}"], working_dirs


def test_service_execstart_points_at_same_repo() -> None:
    text = SERVICE.read_text()
    exec_starts = [
        ln.strip() for ln in text.splitlines() if ln.strip().startswith("ExecStart=")
    ]
    assert len(exec_starts) == 1, exec_starts
    assert f"--project {TOOLKIT}" in exec_starts[0], exec_starts[0]
    assert "python -m herdr_remote" in exec_starts[0], exec_starts[0]


def test_readme_gen_token_commands_are_cwd_independent() -> None:
    text = README.read_text()
    # Every uv invocation of the module must cd first (--directory), never
    # rely on --project alone (which does not change the process cwd).
    module_runs = re.findall(r"^\s*uv run .*herdr_remote.*$", text, re.MULTILINE)
    assert module_runs, "README lost its gen-token commands?"
    for cmd in module_runs:
        assert "--directory" in cmd, f"cwd-dependent: {cmd.strip()}"
        assert "--project" not in cmd, f"cwd-dependent: {cmd.strip()}"


def test_deploy_script_restarts_and_verifies_the_workstation_bridge() -> None:
    # A deploy that only syncs the Fedora-side PWA assets (and leaves the
    # workstation bridge running old code) is the exact bug this guards
    # against: both halves must move together, in one script run.
    text = DEPLOY_SH.read_text()
    assert "systemctl --user restart herdr-remote-bridge.service" in text
    assert "/api/health" in text
    assert re.search(r"^\s*exit 1\s*$", text, re.MULTILINE), (
        "deploy.sh must fail loudly if the bridge does not come back healthy"
    )
    # /api/health is authed like every other /api/* route (only static
    # assets are not) — a health check without a bearer token always 401s.
    health_calls = [ln for ln in text.splitlines() if "curl" in ln and "HEALTH" in ln]
    assert health_calls
    assert all("Authorization" in ln for ln in health_calls), health_calls


def test_deploy_script_installs_caddy_snippet_with_loadable_extension() -> None:
    # Caddy's main Caddyfile only imports Caddyfile.d/*.caddyfile — a
    # snippet installed with any other extension loads silently as nothing,
    # no error, the site just never serves.
    text = DEPLOY_SH.read_text()
    installs = re.findall(r"Caddyfile\.d/[\w.-]+", text)
    assert installs, "deploy.sh lost its Caddyfile.d install target"
    assert all(name.endswith(".caddyfile") for name in installs), installs
