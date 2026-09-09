"""Opt-in real-harness claim-liveness integration matrix.

Each case asks one installed coding harness to run an isolated helper that
starts a dev_status item, then holds the harness open while this test invokes
``show`` from a separate process. This exercises real process ancestry rather
than only synthetic ``/proc`` trees. It makes live model calls, so run it only
when explicitly requested:

    LIVE_HARNESS_CLAIM_MATRIX=1 uv run pytest test/test_claim_liveness_live.py -m live_backends

To run a deliberately selected installed subset, set
``LIVE_HARNESS_CLAIM_HARNESSES`` to comma-separated harness names.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS_TIMEOUT_SECONDS = 180
READY_TIMEOUT_SECONDS = 90
ALL_HARNESSES = ("codex", "claude", "copilot", "opencode", "agy", "pi")

pytestmark = [
    pytest.mark.live_backends,
    pytest.mark.allow_real_subprocess,
    pytest.mark.allow_production_paths,
]

if os.environ.get("LIVE_HARNESS_CLAIM_MATRIX") != "1":
    pytest.skip(
        "set LIVE_HARNESS_CLAIM_MATRIX=1 to make real harness model calls",
        allow_module_level=True,
    )


def _seed_backlog(home: Path, item_ids: tuple[str, ...] = ("harness-claim",)) -> None:
    """Write disposable items for live harness helpers to claim."""
    data_dir = home / ".claude" / "data" / "backlog"
    data_dir.mkdir(parents=True)
    (data_dir / "items.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "items": [
                    {
                        "id": item_id,
                        "summary": f"Temporary live claim for {item_id}",
                        "status": "open",
                        "category": "chore",
                        "created": "2026-09-09",
                        "updated": "2026-09-09",
                        "blocked_by": [],
                        "related_files": [],
                        "context": "",
                        "next_steps": "",
                    }
                    for item_id in item_ids
                ],
            }
        )
    )
    (data_dir / "_meta.json").write_text(json.dumps({"rev": 1}))


def _live_harnesses() -> tuple[str, ...]:
    """Return the requested live harness matrix, validating every name."""
    selected = os.environ.get("LIVE_HARNESS_CLAIM_HARNESSES")
    if not selected:
        return ALL_HARNESSES
    harnesses = tuple(name.strip() for name in selected.split(",") if name.strip())
    invalid = set(harnesses) - set(ALL_HARNESSES)
    if invalid or not harnesses or len(set(harnesses)) != len(harnesses):
        pytest.fail(
            "LIVE_HARNESS_CLAIM_HARNESSES must name distinct supported harnesses: "
            + ", ".join(ALL_HARNESSES)
        )
    return harnesses


def _write_helper(workspace: Path, home: Path, item_id: str, harness: str) -> Path:
    """Write a command that claims the item then waits for test release."""
    helper = workspace / "claim_helper.py"
    ready = workspace / "ready"
    release = workspace / "release"
    scripts = workspace / "agent-scripts" / "dev_status.py"
    helper.write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                f"home = {str(home)!r}",
                f"script = {str(scripts)!r}",
                f"ready = {str(ready)!r}",
                f"release = {str(release)!r}",
                "env = dict(os.environ, HOME=home)",
                "result = subprocess.run(",
                "    [sys.executable, script, 'start', '--allow-main',",
                f"     '--claimed-by', {harness!r}, {item_id!r}],",
                "    env=env, check=False, capture_output=True, text=True,",
                ")",
                "if result.returncode:",
                "    print(result.stdout + result.stderr, file=sys.stderr)",
                "    raise SystemExit(result.returncode)",
                "open(ready, 'w').write('ready\\n')",
                "deadline = time.monotonic() + 120",
                "while not os.path.exists(release) and time.monotonic() < deadline:",
                "    time.sleep(0.1)",
                "if not os.path.exists(release):",
                "    raise SystemExit('test release marker was not written')",
            ]
        )
        + "\n"
    )
    return helper


def _command(harness: str, workspace: Path, helper: Path) -> list[str]:
    """Build one documented non-interactive invocation with shell access."""
    prompt = f"Run exactly `python3 {helper}` with your shell tool. Do nothing else."
    if harness == "codex":
        return [
            "codex",
            "exec",
            "--ephemeral",
            "--approve-for-me",
            "--skip-git-repo-check",
            "-C",
            str(workspace),
            prompt,
        ]
    if harness == "claude":
        return [
            "claude",
            "-p",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--permission-prompts",
            "none",
            prompt,
        ]
    if harness == "copilot":
        return [
            "copilot",
            "--no-custom-instructions",
            "--no-remote",
            "--allow-all-tools",
            "--allow-all-paths",
            "--silent",
            "-C",
            str(workspace),
            "-p",
            prompt,
        ]
    if harness == "opencode":
        return ["opencode", "run", "--auto", "--dir", str(workspace), prompt]
    if harness == "agy":
        return ["agy", "--dangerously-skip-permissions", "--print", prompt]
    if harness == "pi":
        return [
            "pi",
            "--no-session",
            "--no-context-files",
            "--no-skills",
            "--no-extensions",
            "--tools",
            "bash",
            "--approve",
            "-p",
            prompt,
        ]
    raise ValueError(f"unknown harness: {harness}")


def _wait_for_ready(ready: Path, proc: subprocess.Popen[str]) -> None:
    """Wait for a harness-run helper, failing early if its process exits."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready.exists():
            return
        if proc.poll() is not None:
            output, _ = proc.communicate()
            pytest.fail(f"harness exited before claiming item:\n{output}")
        time.sleep(0.2)
    proc.terminate()
    output, _ = proc.communicate(timeout=10)
    pytest.fail(f"harness did not start claim within {READY_TIMEOUT_SECONDS}s:\n{output}")


@pytest.mark.parametrize("harness", ALL_HARNESSES)
def test_live_harness_claim_survives_separate_show(harness: str, tmp_path: Path) -> None:
    """A live harness process keeps its claimed item active through ``show``."""
    if shutil.which(harness) is None:
        pytest.skip(f"{harness} is not installed on this host")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shutil.copytree(REPO_ROOT / "agent-scripts", workspace / "agent-scripts")
    home = tmp_path / "home"
    _seed_backlog(home)
    helper = _write_helper(workspace, home, "harness-claim", harness)
    ready = workspace / "ready"
    release = workspace / "release"
    proc = subprocess.Popen(
        _command(harness, workspace, helper),
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_ready(ready, proc)
        show = subprocess.run(
            [sys.executable, workspace / "agent-scripts" / "dev_status.py", "show", "harness-claim"],
            env=dict(os.environ, HOME=str(home)),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert show.returncode == 0, show.stderr
        assert '"status": "in-progress"' in show.stdout, show.stdout + show.stderr
    finally:
        release.touch()
        try:
            proc.communicate(timeout=HARNESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.communicate(timeout=10)


def test_live_harnesses_share_dashboard_and_safe_takeover(tmp_path: Path) -> None:
    """Six live harnesses claim distinct temp items; a live claim cannot be stolen."""
    harnesses = _live_harnesses()
    missing = [harness for harness in harnesses if shutil.which(harness) is None]
    if missing:
        pytest.skip(f"installed harnesses required: {', '.join(missing)}")

    home = tmp_path / "home"
    item_ids = tuple(f"harness-claim-{harness}" for harness in harnesses)
    _seed_backlog(home, item_ids)
    procs: dict[str, subprocess.Popen[str]] = {}
    releases: dict[str, Path] = {}
    scripts: Path | None = None
    try:
        for harness, item_id in zip(harnesses, item_ids, strict=True):
            workspace = tmp_path / harness
            workspace.mkdir()
            shutil.copytree(REPO_ROOT / "agent-scripts", workspace / "agent-scripts")
            helper = _write_helper(workspace, home, item_id, harness)
            procs[harness] = subprocess.Popen(
                _command(harness, workspace, helper),
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            releases[harness] = workspace / "release"
            scripts = workspace / "agent-scripts" / "dev_status.py"

        for harness in harnesses:
            _wait_for_ready(tmp_path / harness / "ready", procs[harness])

        assert scripts is not None
        env = dict(os.environ, HOME=str(home))
        dashboard = subprocess.run(
            [sys.executable, scripts, "render"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert dashboard.returncode == 0, dashboard.stderr
        assert "IN PROGRESS" in dashboard.stdout
        for harness, item_id in zip(harnesses, item_ids, strict=True):
            assert item_id in dashboard.stdout
            assert f"[{harness}]" in dashboard.stdout

        target = item_ids[0]
        collision = subprocess.run(
            [sys.executable, scripts, "start", "--allow-main", target],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert collision.returncode == 1
        assert "actively claimed" in collision.stderr or "active for" in collision.stderr

        releases["codex"].touch()
        procs["codex"].communicate(timeout=HARNESS_TIMEOUT_SECONDS)
        replacement = subprocess.run(
            [sys.executable, scripts, "start", "--allow-main", target],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert replacement.returncode == 1
        assert "active for" in replacement.stderr
        forced_replacement = subprocess.run(
            [sys.executable, scripts, "start", "--allow-main", "--force", target],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert forced_replacement.returncode == 0, forced_replacement.stderr

        for harness, item_id in zip(harnesses[1:], item_ids[1:], strict=True):
            show = subprocess.run(
                [sys.executable, scripts, "show", item_id],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert show.returncode == 0, show.stderr
            assert '"status": "in-progress"' in show.stdout
            assert f'"harness": "{harness}"' in show.stdout
    finally:
        for release in releases.values():
            release.touch()
        for proc in procs.values():
            try:
                proc.communicate(timeout=HARNESS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.communicate(timeout=10)
