"""Tests for scripts/bootstrap-worktree.sh.

Runs the real script against a temp fake repo with stub `uv`/`npm`
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


def _write_npm_stub(bin_dir: Path, log: Path, fail_dir: Path) -> None:
    stub = bin_dir / "npm"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf '%s\\t%s\\t%s\\n' "npm" "$PWD" "$*" >> "{log}"
            if [ "$PWD" = "{fail_dir}" ]; then
              exit 1
            fi
            exit 0
            """
        )
    )
    stub.chmod(0o755)


def _read_log(log: Path) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    for line in log.read_text().splitlines():
        name, cwd, args = line.split("\t", 2)
        entries.append((name, cwd, args))
    return entries


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fake-repo"
    for directory in ("pi", "opencode"):
        (repo / directory).mkdir(parents=True)
        (repo / directory / "package.json").write_text("{}\n")
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
    def test_runs_all_installs_in_their_directories(
        self, tmp_path: Path, script_in_fake_repo: Path
    ) -> None:
        repo = script_in_fake_repo.parent.parent
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        _write_stub(bin_dir, log, "uv", exit_code=0)
        _write_stub(bin_dir, log, "npm", exit_code=0)

        result = subprocess.run(
            ["bash", str(script_in_fake_repo)],
            cwd=tmp_path,  # NOT the repo root: script must self-locate
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert _read_log(log) == [
            ("uv", str(repo), "sync"),
            ("npm", str(repo / "pi"), "install"),
            ("npm", str(repo / "opencode"), "install"),
        ]

    @pytest.mark.parametrize("failure", ["uv", "pi", "opencode"])
    def test_failing_step_names_it_and_exits_nonzero(
        self,
        tmp_path: Path,
        script_in_fake_repo: Path,
        failure: str,
    ) -> None:
        repo = script_in_fake_repo.parent.parent
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        if failure == "uv":
            _write_stub(bin_dir, log, "uv", exit_code=1)
            _write_stub(bin_dir, log, "npm", exit_code=0)
        else:
            _write_stub(bin_dir, log, "uv", exit_code=0)
            _write_npm_stub(bin_dir, log, repo / failure)

        result = subprocess.run(
            ["bash", str(script_in_fake_repo)],
            cwd=tmp_path,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert failure in result.stderr
        assert _read_log(log) == [
            ("uv", str(repo), "sync"),
            ("npm", str(repo / "pi"), "install"),
            ("npm", str(repo / "opencode"), "install"),
        ]

    @pytest.mark.parametrize("missing", ["uv", "npm"])
    def test_missing_tool_warns_skips_and_still_runs_the_others(
        self,
        tmp_path: Path,
        script_in_fake_repo: Path,
        missing: str,
    ) -> None:
        repo = script_in_fake_repo.parent.parent
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        path_value = f"{bin_dir}:/usr/bin:/bin"
        if missing == "uv":
            _write_stub(bin_dir, log, "npm", exit_code=0)
            expected = [
                ("npm", str(repo / "pi"), "install"),
                ("npm", str(repo / "opencode"), "install"),
            ]
        else:
            _write_stub(bin_dir, log, "uv", exit_code=0)
            runtime_dir = tmp_path / "runtime"
            runtime_dir.mkdir()
            for command in ("bash", "dirname"):
                resolved = shutil.which(command)
                assert resolved is not None
                (runtime_dir / command).symlink_to(resolved)
            path_value = f"{bin_dir}:{runtime_dir}"
            expected = [("uv", str(repo), "sync")]

        result = subprocess.run(
            ["/bin/bash", str(script_in_fake_repo)],
            cwd=tmp_path,
            env={"PATH": path_value},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert missing in result.stderr
        if missing == "npm":
            assert "pi TypeScript checks" in result.stderr
            assert "opencode TypeScript checks" in result.stderr
        assert _read_log(log) == expected

    def test_script_exists_and_has_bash_shebang(self) -> None:
        assert SCRIPT.exists(), f"{SCRIPT} missing"
        first_line = SCRIPT.read_text().splitlines()[0]
        assert first_line == "#!/usr/bin/env bash"

    def test_unexpected_argument_exits_2_and_skips_installs(
        self, tmp_path: Path, script_in_fake_repo: Path
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        _write_stub(bin_dir, log, "uv", exit_code=0)
        _write_stub(bin_dir, log, "npm", exit_code=0)

        result = subprocess.run(
            ["bash", str(script_in_fake_repo), "/some/worktree/path"],
            cwd=tmp_path,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert "/some/worktree/path" in result.stderr
        assert not log.exists(), "no installer should run when an argument is rejected"

    def test_successful_run_prints_resolved_target(
        self, tmp_path: Path, script_in_fake_repo: Path
    ) -> None:
        repo = script_in_fake_repo.parent.parent
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        _write_stub(bin_dir, log, "uv", exit_code=0)
        _write_stub(bin_dir, log, "npm", exit_code=0)

        result = subprocess.run(
            ["bash", str(script_in_fake_repo)],
            cwd=tmp_path,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert str(repo) in result.stdout
