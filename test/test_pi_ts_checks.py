#!/usr/bin/env python3
"""Pi extension TS tooling gate: runs the bun-based checks in pi/ so they're
exercised by the repo's normal pytest run.

Skips (never fails) when the environment can't run them: bun missing from
PATH, or pi/node_modules incomplete (an interrupted `bun install` leaves a
partial tree that a directory-existence check would miss — hence the
per-tool marker check). With all tools installed as local devDependencies
and the markers present, bunx never network-fetches.

Each stage is a separate subtest so one failing stage doesn't hide the
others; failure messages carry the captured stdout AND stderr so pytest
output shows the compiler/linter diagnostics, not just an exit code.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PI_DIR = Path(__file__).resolve().parent.parent / "pi"

# Every tool the wrapper invokes via bunx must exist as a local
# devDependency marker; a missing marker means "run bun install in pi/".
REQUIRED_BINARIES = ("prettier", "oxlint", "tsc")

STAGES = ("test", "typecheck", "lint", "format:check")


def _bun_missing() -> bool:
    return shutil.which("bun") is None


def _node_modules_incomplete() -> bool:
    bin_dir = PI_DIR / "node_modules" / ".bin"
    return any(not (bin_dir / name).exists() for name in REQUIRED_BINARIES)


def _run_stage(stage: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bun", "run", stage],
        cwd=PI_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.allow_real_subprocess  # runs bun itself; touches nothing outside pi/
@pytest.mark.parametrize("stage", STAGES)
def test_pi_ts_stage(stage: str, subtests) -> None:
    if _bun_missing():
        pytest.skip("bun not installed")
    if _node_modules_incomplete():
        pytest.skip(
            "run bun install in pi/ (missing tool markers in pi/node_modules/.bin)"
        )

    with subtests.test(msg=stage):
        result = _run_stage(stage)
        assert result.returncode == 0, (
            f"bun run {stage} failed (exit {result.returncode})\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
