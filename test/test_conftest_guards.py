#!/usr/bin/env python3
"""Regression tests for the repo-root conftest.py autouse guard fixtures."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import conftest

_REAL_HOME = conftest._REAL_HOME


def test_home_is_sandboxed_by_default():
    assert Path.home() != _REAL_HOME
    assert os.environ["HOME"] != str(_REAL_HOME)


@pytest.mark.allow_production_paths
def test_home_restored_under_marker():
    assert Path.home() == _REAL_HOME


def test_real_subprocess_blocked_without_marker():
    with pytest.raises(RuntimeError, match="allow_real_subprocess"):
        subprocess.run(["true"], check=False)


@pytest.mark.allow_real_subprocess
def test_real_subprocess_allowed_with_marker():
    result = subprocess.run(["true"], check=False)
    assert result.returncode == 0


@pytest.mark.regression(
    "sandbox-guard-blocks-real-home-write",
    "Failed: DID NOT RAISE RuntimeError",
)
def test_real_home_write_blocked_without_marker(tmp_path):
    target = _REAL_HOME / ".claude" / f"conftest-guard-probe-{os.getpid()}.txt"
    with pytest.raises(RuntimeError, match="allow_production_paths"):
        target.write_text("should never land on disk")
    assert not target.exists()


@pytest.mark.allow_production_paths
def test_real_home_write_allowed_with_marker(tmp_path):
    target = _REAL_HOME / ".claude" / f"conftest-guard-probe-{os.getpid()}.txt"
    # ~/.claude may not exist yet on a fresh machine/CI runner -- mkdir is
    # guarded by the same allow_production_paths marker this test already
    # carries, so it's a safe no-op when the directory is already there.
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text("cleaned up immediately below")
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_real_config_write_blocked_without_marker(tmp_path):
    target = _REAL_HOME / ".config" / f"conftest-guard-probe-{os.getpid()}.txt"
    with pytest.raises(RuntimeError, match="allow_production_paths"):
        target.write_text("should never land on disk")
    assert not target.exists()


def test_builtins_open_write_blocked_without_marker(tmp_path):
    target = _REAL_HOME / ".claude" / f"conftest-guard-probe-{os.getpid()}.txt"
    with pytest.raises(RuntimeError, match="allow_production_paths"):
        with open(str(target), "w") as f:
            f.write("should fail")


def test_os_open_write_blocked_without_marker(tmp_path):
    target = _REAL_HOME / ".claude" / f"conftest-guard-probe-{os.getpid()}.txt"
    with pytest.raises(RuntimeError, match="allow_production_paths"):
        os.open(str(target), os.O_WRONLY | os.O_CREAT)


def test_agent_toolkit_home_is_cleared_by_sandbox():
    assert "AGENT_TOOLKIT_HOME" not in os.environ


@pytest.mark.allow_real_subprocess
@pytest.mark.regression(
    "sandbox-scrubs-inherited-state-paths",
    "AssertionError: '/somewhere/real 1' != 'None None'",
)
def test_sandbox_scrubs_inherited_state_paths(tmp_path):
    """The sandbox must drop inherited state paths, not just HOME.

    An inherited XDG_STATE_HOME or AGENT_TOOLKIT_TIMING would let a render's
    bounded timing.jsonl read (and any state write) land on the developer's
    real ~/.local/state. Spawned with both set, because the ambient
    environment may legitimately carry neither -- an in-process "is absent"
    assertion would then pass against the unfixed code too.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        "import test_bootstrap\n"
        "print(os.environ.get('XDG_STATE_HOME'), os.environ.get('AGENT_TOOLKIT_TIMING'))\n"
    )
    env = dict(
        os.environ,
        XDG_STATE_HOME="/somewhere/real",
        AGENT_TOOLKIT_TIMING="1",
        PYTHONPATH=str(Path(__file__).resolve().parent.parent / "agent-scripts"),
    )
    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == "None None"


def test_tmp_path_writes_allowed_without_marker(tmp_path):
    target = tmp_path / "normal_test_file.txt"
    target.write_text("allowed")
    assert target.read_text() == "allowed"
