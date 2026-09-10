#!/usr/bin/env python3
"""Pi extension TS tooling gate: runs the npm-based checks in pi/ so they're
exercised by the repo's normal pytest run.

Skips only when npm is missing from PATH. When npm exists but
pi/node_modules is incomplete (an interrupted `npm install` leaves a partial
tree that a directory-existence check would miss — hence the per-tool marker
check), the gate FAILS with the install instruction instead of skipping: a
fresh git worktree never has node_modules (untracked, unshared across
worktrees), and a skip there silently hides this gate from worktree-based
baselines. With all tools installed as local devDependencies and the markers
present, `npm run` resolves every stage from pi/node_modules/.bin without a
network fetch.

Each stage is a separate subtest so one failing stage doesn't hide the
others; failure messages carry the captured stdout AND stderr so pytest
output shows the compiler/linter diagnostics, not just an exit code.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PI_DIR = Path(__file__).resolve().parent.parent / "pi"

# Every tool a stage resolves from the local install must exist as a
# node_modules/.bin marker; a missing marker means "run scripts/
# bootstrap-worktree.sh (or `npm install` in pi/)". `esbuild` is listed here
# too because scripts/build-copilot-swarm.sh invokes that binary directly from
# pi/node_modules/.bin rather than through a package script, so an incomplete
# install would otherwise surface as a confusing bundler error instead.
REQUIRED_BINARIES = ("prettier", "oxlint", "tsc", "esbuild")

STAGES = ("test", "typecheck", "lint", "format:check")


def _npm_missing() -> bool:
    return shutil.which("npm") is None


def _node_modules_incomplete() -> bool:
    bin_dir = PI_DIR / "node_modules" / ".bin"
    return any(not (bin_dir / name).exists() for name in REQUIRED_BINARIES)


def _run_stage(stage: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npm", "run", stage],
        cwd=PI_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.allow_real_subprocess  # runs npm itself; touches nothing outside pi/
@pytest.mark.parametrize("stage", STAGES)
def test_pi_ts_stage(stage: str, subtests) -> None:
    if _npm_missing():
        pytest.skip("npm not installed")
    if _node_modules_incomplete():
        pytest.fail(
            "run scripts/bootstrap-worktree.sh (missing tool markers in "
            "pi/node_modules/.bin) — the gate fails rather than skips so a "
            "fresh worktree's silent skip can't hide it from a baseline"
        )

    with subtests.test(msg=stage):
        result = _run_stage(stage)
        assert result.returncode == 0, (
            f"npm run {stage} failed (exit {result.returncode})\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )


class TestGateAsymmetry:
    """The skip-vs-fail split is the gate's whole point, so it is asserted
    directly rather than only by reading the docstring: an incomplete install
    must fail loudly, and only a genuinely absent package manager may skip.
    """

    def test_incomplete_node_modules_fails_instead_of_skipping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("test_pi_ts_checks._npm_missing", lambda: False)
        monkeypatch.setattr("test_pi_ts_checks._node_modules_incomplete", lambda: True)

        with pytest.raises(pytest.fail.Exception) as excinfo:
            test_pi_ts_stage("test", subtests=None)  # type: ignore[arg-type]

        message = str(excinfo.value)
        assert "pi/node_modules/.bin" in message
        assert "bootstrap-worktree" in message

    def test_absent_npm_skips_rather_than_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("test_pi_ts_checks._npm_missing", lambda: True)
        # Deliberately also "incomplete": the missing-tool check has to win, or
        # a machine without npm would report a failure it cannot act on.
        monkeypatch.setattr("test_pi_ts_checks._node_modules_incomplete", lambda: True)

        with pytest.raises(pytest.skip.Exception):
            test_pi_ts_stage("test", subtests=None)  # type: ignore[arg-type]

    def test_esbuild_is_a_required_marker(self) -> None:
        # The bundler moved from a global `bun build` to pi/node_modules/.bin,
        # so an incomplete install must be caught by the gate, not by the
        # build script failing mid-suite.
        assert "esbuild" in REQUIRED_BINARIES

    def test_stages_match_package_scripts(self) -> None:
        package_json = (PI_DIR / "package.json").read_text()
        for stage in STAGES:
            assert f'"{stage}"' in package_json, f"{stage} is not a pi/ package script"

    def test_stages_are_driven_through_npm_not_bun(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression guard for the port itself: assert on the argv actually
        # passed to subprocess, so this fails if the gate ever shells out to
        # Bun again -- the Node-only environments this item targets stay
        # broken otherwise.
        seen: list[list[str]] = []

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        for stage in STAGES:
            _run_stage(stage)

        assert seen == [["npm", "run", stage] for stage in STAGES]
        assert all("bun" not in argv[0] for argv in seen)


def test_pi_package_scripts_never_mention_bun() -> None:
    """Every pi/ script value must be runnable by Node/npm alone."""
    scripts = json.loads((PI_DIR / "package.json").read_text())["scripts"]
    offenders = {name: value for name, value in scripts.items() if _mentions_bun(value)}
    assert not offenders, f"pi/ scripts still mention bun: {offenders}"


def _mentions_bun(value: str) -> bool:
    """Word-ish match on the tokens of a command string, so `bunfig` or a path
    containing "bun" cannot hide a real `bun` invocation, and an unrelated
    substring ("bundled") cannot trigger a false alarm."""
    tokens = value.replace("=", " ").replace("&", " ").replace("(", " ").replace(")", " ").split()
    return any(token == "bun" or token.startswith("bun:") or token == "bunx" for token in tokens)
