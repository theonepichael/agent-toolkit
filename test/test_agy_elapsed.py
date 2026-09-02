#!/usr/bin/env python3
"""Ensure the agy status line hook's own Node test suite stays green."""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "agy" / "hooks" / "agy-elapsed.test.js"


@pytest.mark.allow_real_subprocess
def test_agy_elapsed_node_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", str(TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
