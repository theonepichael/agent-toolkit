#!/usr/bin/env python3
"""Wire test/lint_shell.sh (shellcheck + shfmt) into the pytest suite.

Unlike ruff — a uv-pinned project dependency — shellcheck and shfmt are system
binaries, so their presence is checked up front to fail with an actionable
message instead of a raw FileNotFoundError. Missing binaries are a hard fail,
never a skip: a silently absent gate is the bug this test exists to close.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.allow_real_subprocess
def test_shell_lint_passes() -> None:
    for binary in ("shellcheck", "shfmt"):
        if shutil.which(binary) is None:
            pytest.fail(
                f"{binary} is not on PATH — install it (e.g. "
                "'apt-get install -y shellcheck shfmt', as dotfiles' shell-lint "
                "workflow does) so test/lint_shell.sh can run"
            )
    result = subprocess.run(
        ["bash", "test/lint_shell.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
