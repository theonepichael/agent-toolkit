#!/usr/bin/env python3
"""Regression tests for the shipped herdr_remote deploy artifacts.

The repo sets ``[tool.uv] package = false``, so ``python -m herdr_remote``
only resolves when the process cwd is the repo root — ``uv run --project``
does *not* cd there. Anything invoking the module must therefore pin the
cwd (systemd ``WorkingDirectory=`` / ``uv run --directory``), or it fails
with ModuleNotFoundError when run from anywhere else. Pure file checks.
"""

import importlib.util
import re
import subprocess
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICE = REPO_ROOT / "herdr_remote" / "deploy" / "herdr-remote-bridge.service"
README = REPO_ROOT / "herdr_remote" / "deploy" / "README.md"
DEPLOY_SH = REPO_ROOT / "herdr_remote" / "deploy" / "deploy.sh"

INSTALL_UNIT = REPO_ROOT / "herdr_remote" / "deploy" / "install_unit.py"

# The repo copy of the unit is a template; install_unit.py renders the checkout in.
TOOLKIT = "@AGENT_TOOLKIT_CHECKOUT@"


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



# --- install_unit.py: the only renderer of the unit template --------------


def _install_unit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_unit", INSTALL_UNIT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _checkout(path: Path) -> Path:
    (path / "agent-scripts").mkdir(parents=True)
    (path / "install.py").write_text("")
    (path / "links.toml").write_text("")
    return path


def test_template_has_exactly_the_two_placeholders() -> None:
    lines = SERVICE.read_text().splitlines()
    hits = [ln for ln in lines if TOOLKIT in ln]
    assert len(hits) == 2, hits
    assert hits[0].startswith("WorkingDirectory=")
    assert hits[1].startswith("ExecStart=")
    assert "Workspace" not in SERVICE.read_text()


def test_renders_the_checkout_into_the_unit_dir(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "Workspace" / "agent-toolkit" / "agent-toolkit")
    unit_dir = tmp_path / "units"
    code = _install_unit().main(
        ["--checkout", str(checkout), "--unit-dir", str(unit_dir), "--no-reload"]
    )
    assert code == 0
    rendered = (unit_dir / "herdr-remote-bridge.service").read_text()
    assert f"WorkingDirectory={checkout}\n" in rendered
    assert f"--project {checkout} python -m herdr_remote serve" in rendered
    assert not re.search(r"@[A-Z_]+@", rendered)


def test_reloads_systemd_unless_told_not_to(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "atk")
    module = _install_unit()
    with mock.patch.object(module.subprocess, "run") as run:
        code = module.main(
            ["--checkout", str(checkout), "--unit-dir", str(tmp_path / "u")]
        )
    assert code == 0
    run.assert_called_once_with(["systemctl", "--user", "daemon-reload"], check=True)


def test_check_writes_nothing(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "atk")
    unit_dir = tmp_path / "units"
    code = _install_unit().main(
        ["--checkout", str(checkout), "--unit-dir", str(unit_dir), "--check"]
    )
    assert code == 0
    assert not unit_dir.exists()


@pytest.mark.parametrize("bad", ["with space", "with%percent", "with#hash", "with$dollar"])
def test_refuses_paths_systemd_or_sed_would_mangle(
    tmp_path: Path, bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = _checkout(tmp_path / bad)
    unit_dir = tmp_path / "units"
    code = _install_unit().main(
        ["--checkout", str(checkout), "--unit-dir", str(unit_dir), "--no-reload"]
    )
    assert code == 1
    assert not unit_dir.exists()
    assert "unsupported character" in capsys.readouterr().err


def test_refuses_a_checkout_without_the_markers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_checkout = tmp_path / "container"
    not_checkout.mkdir()
    code = _install_unit().main(
        ["--checkout", str(not_checkout), "--unit-dir", str(tmp_path / "u"), "--no-reload"]
    )
    assert code == 1
    assert "not an agent-toolkit checkout" in capsys.readouterr().err


@pytest.mark.parametrize(
    "template",
    [
        "WorkingDirectory=@AGENT_TOOLKIT_CHECKOUT@\nExecStart=uv run --project @AGENT_TOOLKIT_CHEKOUT@\n",
        "WorkingDirectory=@AGENT_TOOLKIT_CHECKOUT@\n",
        "WorkingDirectory=@AGENT_TOOLKIT_CHECKOUT@\nExecStart=uv --project @AGENT_TOOLKIT_CHECKOUT@\n# @AGENT_TOOLKIT_CHECKOUT@\n",
        "WorkingDirectory=@AGENT_TOOLKIT_CHECKOUT@\nExecStart=uv --project @AGENT_TOOLKIT_CHECKOUT@ @OTHER@\n",
    ],
)
def test_rejects_a_malformed_template(template: str) -> None:
    module = _install_unit()
    with pytest.raises(module.UnitTemplateError):
        module.render_unit(template, Path("/home/u/atk"))


def test_deploy_validates_before_touching_either_half() -> None:
    lines = DEPLOY_SH.read_text().splitlines()
    set_line = lines.index("set -euo pipefail")
    following = next(ln for ln in lines[set_line + 1 :] if ln.strip())
    assert "install_unit.py" in following and "--check" in following, following
    text = "\n".join(lines)
    write = re.search(r"^.*install_unit\.py(?!.*--check).*$", text, re.MULTILINE)
    assert write, "deploy.sh never renders the unit for real"
    restart = text.index("systemctl --user restart herdr-remote-bridge.service")
    assert write.start() < restart


def test_readme_renders_absolute_paths_and_has_no_hardcoded_checkout() -> None:
    text = README.read_text()
    assert "config.toml <<'EOF'" not in text
    assert re.search(r"cat > ~/\.config/herdr-bridge/config\.toml <<EOF", text)
    assert 'assets_dir = "$R/herdr_remote/pwa"' in text
    assert "Workspace/agent-toolkit" not in text
    assert "install_unit.py" in text
