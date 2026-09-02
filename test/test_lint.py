#!/usr/bin/env python3
"""Ensure the repository's pinned Ruff configuration stays clean."""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.allow_real_subprocess
def test_ruff_check_passes() -> None:
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
