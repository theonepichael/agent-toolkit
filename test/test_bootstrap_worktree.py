"""Tests for scripts/bootstrap-worktree.sh.

Runs the real script against a temp fake repo with stub `uv`/`bun`
executables on PATH, so the per-directory install invocations are verified
without touching the real repo's venv or pi/node_modules.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bootstrap-worktree.sh"


def _write_stub(bin_dir: Path, log: Path, name: str, exit_code: int) -> None:
    stub = bin_dir / name
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf '%s\\t%s\\t%s\\n' "{name}" "$PWD" "$*" >> "{log}"
            exit {exit_code}
            """
        )
    )
    stub.chmod(0o755)


def _read_log(log: Path) -> list[tuple[str, str, str]]:
    return [
        line.split("\t", 2)  # type: ignore[misc]
        for line in log.read_text().splitlines()
    ]


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fake-repo"
    (repo / "pi").mkdir(parents=True)
    (repo / "pi" / "package.json").write_text("{}\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'fake'\n")
    return repo


@pytest.fixture
def script_in_fake_repo(tmp_path: Path) -> Path:
    """Copy the real script into the fake repo so its self-located repo-root
    resolution points at the sandbox, never at the real checkout."""
    repo = _fake_repo(tmp_path)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    dest = scripts_dir / SCRIPT.name
    shutil.copy(SCRIPT, dest)
    dest.chmod(0o755)
    return dest


@pytest.mark.allow_real_subprocess  # runs bash on the script + stubs; confined to tmp dirs
class TestBootstrapWorktree:
    def test_runs_both_installs_in_their_directories(
        self, tmp_path: Path, script_in_fake_repo: Path
    ) -> None:
        repo = script_in_fake_repo.parent.parent
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        _write_stub(bin_dir, log, "uv", exit_code=0)
        _write_stub(bin_dir, log, "bun", exit_code=0)

        result = subprocess.run(
            ["bash", str(script_in_fake_repo)],
            cwd=tmp_path,  # NOT the repo root: script must self-locate
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        calls = _read_log(log)
        tools = [name for name, _cwd, _args in calls]
        assert tools == ["uv", "bun"]
        # uv runs at the repo root, bun inside pi/ — regardless of caller cwd
        assert calls[0][1] == str(repo)
        assert calls[0][2] == "sync"
        assert calls[1][1] == str(repo / "pi")
        assert calls[1][2] == "install"

    def test_failing_step_names_it_and_exits_nonzero(
        self, tmp_path: Path, script_in_fake_repo: Path
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        _write_stub(bin_dir, log, "uv", exit_code=1)
        _write_stub(bin_dir, log, "bun", exit_code=0)

        result = subprocess.run(
            ["bash", str(script_in_fake_repo)],
            cwd=tmp_path,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert "uv sync" in result.stderr

    def test_missing_tool_warns_skips_and_still_runs_the_other(
        self, tmp_path: Path, script_in_fake_repo: Path
    ) -> None:
        repo = script_in_fake_repo.parent.parent
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        _write_stub(bin_dir, log, "bun", exit_code=0)  # no uv stub: uv "missing"

        result = subprocess.run(
            ["bash", str(script_in_fake_repo)],
            cwd=tmp_path,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "uv" in result.stderr  # warning names the skipped tool
        calls = _read_log(log)
        assert [(name, cwd, args) for name, cwd, args in calls] == [
            ("bun", str(repo / "pi"), "install")
        ]

    def test_script_exists_and_has_bash_shebang(self) -> None:
        assert SCRIPT.exists(), f"{SCRIPT} missing"
        first_line = SCRIPT.read_text().splitlines()[0]
        assert first_line == "#!/usr/bin/env bash"
