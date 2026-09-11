#!/usr/bin/env python3
"""Tests for agent-scripts/worktree.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import worktree  # noqa: E402 — must follow sys.path.insert above


def _init_repo(path: Path) -> Path:
    """Initialize a git repository in path with an initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    readme = path / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial commit"], check=True)
    return path


@pytest.mark.allow_real_subprocess  # runs git commands against temporary repos in tmp_path
class TestWorktree:
    def test_explicit_repo_and_branch_zero_backlog_dependency(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "my-repo")
        config = worktree.resolve_worktree_config(
            repo=repo,
            branch="feat-test",
        )
        assert config.repo == repo.resolve()
        assert config.branch == "feat-test"
        expected_dest = tmp_path / "my-repo-feat-test"
        assert config.worktree_path == expected_dest.resolve()

        result = worktree.create_and_bootstrap_worktree(config)
        assert result.worktree_path == expected_dest.resolve()
        assert result.branch == "feat-test"
        assert not result.reused
        assert expected_dest.is_dir()
        assert (expected_dest / ".git").is_file()

        # Check branch in worktree
        branch_out = subprocess.run(
            ["git", "-C", str(expected_dest), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch_out == "feat-test"

    def test_backlog_item_resolution_via_related_files(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "proj")
        item_file = repo / "src" / "code.py"
        item_file.parent.mkdir(parents=True, exist_ok=True)
        item_file.write_text("print('hello')\n")

        items_data = {
            "schema_version": 2,
            "items": [
                {
                    "id": "proj-new-feature",
                    "created": "2026-09-10",
                    "updated": "2026-09-10",
                    "status": "open",
                    "summary": "Do something",
                    "category": "feat",
                    "blocked_by": [],
                    "related_files": [{"path": str(item_file)}],
                    "context": "ctx",
                    "next_steps": "steps",
                }
            ],
        }
        backlog_file = tmp_path / "items.json"
        backlog_file.write_text(json.dumps(items_data))

        config = worktree.resolve_worktree_config(
            "proj-new-feature",
            items_path=backlog_file,
            skip_bootstrap=True,
        )
        assert config.repo == repo.resolve()
        assert config.branch == "proj-new-feature"
        assert config.worktree_path == (tmp_path / "proj-proj-new-feature").resolve()

        result = worktree.create_and_bootstrap_worktree(config)
        assert result.worktree_path.exists()
        assert not result.reused

    def test_backlog_item_worktree_stamps_provenance_marker(
        self, tmp_path: Path
    ) -> None:
        """worktree.py <slug> stamps $(git rev-parse --git-dir)/devstatus_item
        so guard_rails can attribute the worktree even after a branch rename."""
        import worktree_provenance

        repo = _init_repo(tmp_path / "proj-mark")
        (repo / "code.py").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "code.py"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "code"], check=True
        )
        items = {
            "schema_version": 2,
            "items": [
                {
                    "id": "marked-item",
                    "status": "in-progress",
                    "summary": "s",
                    "related_files": [{"path": str(repo / "code.py")}],
                }
            ],
        }
        backlog_file = tmp_path / "items.json"
        backlog_file.write_text(json.dumps(items))
        config = worktree.resolve_worktree_config(
            "marked-item", items_path=backlog_file, skip_bootstrap=True
        )
        assert config.slug == "marked-item"
        worktree.create_and_bootstrap_worktree(config)
        wt = tmp_path / "proj-mark-marked-item"
        assert worktree_provenance.read_marker_for_worktree(wt) == "marked-item"

    def test_repo_flag_with_backlog_item_still_stamps(self, tmp_path: Path) -> None:
        """`worktree.py <item> --repo <repo>` — documented answer for
        multi-repo items — must still resolve the slug and stamp."""
        import worktree_provenance

        repo = _init_repo(tmp_path / "two-repo")
        (repo / "code.py").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "code.py"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "code"], check=True
        )
        items = {
            "schema_version": 2,
            "items": [
                {
                    "id": "multi-repo-item",
                    "status": "in-progress",
                    "summary": "s",
                    "related_files": [{"path": str(repo / "code.py")}],
                }
            ],
        }
        backlog_file = tmp_path / "items.json"
        backlog_file.write_text(json.dumps(items))
        config = worktree.resolve_worktree_config(
            "multi-repo-item",
            repo=repo,
            items_path=backlog_file,
            skip_bootstrap=True,
        )
        assert config.slug == "multi-repo-item"
        worktree.create_and_bootstrap_worktree(config)
        wt = tmp_path / "two-repo-multi-repo-item"
        assert worktree_provenance.read_marker_for_worktree(wt) == "multi-repo-item"

    def test_explicit_repo_and_branch_writes_no_marker(self, tmp_path: Path) -> None:
        """Ad-hoc --repo/--branch work (no backlog item) gets no marker —
        the write path stays attributed by branch==slug only."""
        import worktree_provenance

        repo = _init_repo(tmp_path / "adhoc")
        config = worktree.resolve_worktree_config(
            repo=repo, branch="adhoc-branch", skip_bootstrap=True
        )
        assert config.slug is None
        worktree.create_and_bootstrap_worktree(config)
        wt = tmp_path / "adhoc-adhoc-branch"
        assert worktree_provenance.read_marker_for_worktree(wt) is None

    def test_reuse_rewrites_marker_idempotently(self, tmp_path: Path) -> None:
        """Second run on the same item's worktree (reuse path) keeps the
        marker — the retag lifecycle."""
        import worktree_provenance

        repo = _init_repo(tmp_path / "reused")
        (repo / "code.py").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "code.py"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "code"], check=True
        )
        items = {
            "schema_version": 2,
            "items": [
                {
                    "id": "reused-item",
                    "status": "in-progress",
                    "summary": "s",
                    "related_files": [{"path": str(repo / "code.py")}],
                }
            ],
        }
        backlog_file = tmp_path / "items.json"
        backlog_file.write_text(json.dumps(items))
        config = worktree.resolve_worktree_config(
            "reused-item", items_path=backlog_file, skip_bootstrap=True
        )
        worktree.create_and_bootstrap_worktree(config)
        wt = tmp_path / "reused-reused-item"
        assert worktree_provenance.write_marker(wt, "other") is True
        result2 = worktree.create_and_bootstrap_worktree(config)
        assert result2.reused
        assert worktree_provenance.read_marker_for_worktree(wt) == "reused-item"

    def test_reuse_on_renamed_branch_raises(self, tmp_path: Path) -> None:
        """worktree.py <slug> reuse on a renamed branch raises WorktreeError;
        rerun with --branch <new> is the documented re-stamp path."""
        repo = _init_repo(tmp_path / "renamed-repo")
        (repo / "code.py").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "code.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "code"], check=True)
        items = {
            "schema_version": 2,
            "items": [
                {
                    "id": "renamed-item",
                    "status": "in-progress",
                    "summary": "s",
                    "related_files": [{"path": str(repo / "code.py")}],
                }
            ],
        }
        backlog_file = tmp_path / "items.json"
        backlog_file.write_text(json.dumps(items))
        config = worktree.resolve_worktree_config(
            "renamed-item", items_path=backlog_file, skip_bootstrap=True
        )
        worktree.create_and_bootstrap_worktree(config)
        wt = tmp_path / "renamed-repo-renamed-item"
        subprocess.run(["git", "-C", str(wt), "branch", "-m", "new-branch"], check=True)
        with pytest.raises(worktree.WorktreeError, match="not 'renamed-item'"):
            worktree.create_and_bootstrap_worktree(config)

        # Documented re-stamp path: specify --branch new-branch --dest <path>
        config_restamp = worktree.resolve_worktree_config(
            "renamed-item",
            branch="new-branch",
            dest=wt,
            items_path=backlog_file,
            skip_bootstrap=True,
        )
        result = worktree.create_and_bootstrap_worktree(config_restamp)
        assert result.reused

    def test_marker_write_failure_aborts_before_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure to write the marker raises WorktreeError immediately and
        aborts before bootstrap is run."""
        import worktree_provenance

        repo = _init_repo(tmp_path / "fail-repo")
        (repo / "code.py").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "code.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "code"], check=True)
        items = {
            "schema_version": 2,
            "items": [
                {
                    "id": "fail-item",
                    "status": "in-progress",
                    "summary": "s",
                    "related_files": [{"path": str(repo / "code.py")}],
                }
            ],
        }
        backlog_file = tmp_path / "items.json"
        backlog_file.write_text(json.dumps(items))
        config = worktree.resolve_worktree_config(
            "fail-item", items_path=backlog_file, skip_bootstrap=False
        )

        def mock_write_marker(path, slug):
            raise OSError("disk unwritable")

        bootstrap_called = False

        def mock_bootstrap(path, quiet=False):
            nonlocal bootstrap_called
            bootstrap_called = True
            return True, "cmd", []

        monkeypatch.setattr(worktree_provenance, "write_marker", mock_write_marker)
        monkeypatch.setattr(worktree, "bootstrap_worktree", mock_bootstrap)

        with pytest.raises(worktree.WorktreeError, match="Failed to write provenance marker"):
            worktree.create_and_bootstrap_worktree(config)
        assert not bootstrap_called

    def test_idempotent_reuse_of_existing_worktree(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-reuse")
        config = worktree.resolve_worktree_config(
            repo=repo,
            branch="reuse-branch",
            skip_bootstrap=True,
        )
        result1 = worktree.create_and_bootstrap_worktree(config)
        assert not result1.reused
        assert result1.worktree_path.exists()

        # Second call should reuse without failure
        result2 = worktree.create_and_bootstrap_worktree(config)
        assert result2.reused
        assert result2.worktree_path == result1.worktree_path

    def test_attaches_to_existing_branch(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-branch")
        # Create branch ahead of time
        subprocess.run(
            ["git", "-C", str(repo), "branch", "preexisting-branch"],
            check=True,
        )

        config = worktree.resolve_worktree_config(
            repo=repo,
            branch="preexisting-branch",
            skip_bootstrap=True,
        )
        result = worktree.create_and_bootstrap_worktree(config)
        assert result.worktree_path.exists()
        assert not result.reused

        branch_out = subprocess.run(
            ["git", "-C", str(result.worktree_path), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch_out == "preexisting-branch"

    def test_conflicting_directory_raises_worktree_error(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-conflict")
        dest = tmp_path / "repo-conflict-bad-branch"
        dest.mkdir(parents=True)
        (dest / "some-unrelated-file.txt").write_text("not a git repo")

        config = worktree.resolve_worktree_config(
            repo=repo,
            branch="bad-branch",
            dest=dest,
            skip_bootstrap=True,
        )
        with pytest.raises(worktree.WorktreeError, match="not a git worktree"):
            worktree.create_and_bootstrap_worktree(config)

    def test_different_branch_worktree_raises_worktree_error(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-diff")
        config1 = worktree.resolve_worktree_config(
            repo=repo,
            branch="branch-a",
            skip_bootstrap=True,
        )
        worktree.create_and_bootstrap_worktree(config1)

        # Same destination but asking for branch-b
        config2 = worktree.WorktreeConfig(
            repo=repo.resolve(),
            branch="branch-b",
            worktree_path=config1.worktree_path,
            skip_bootstrap=True,
        )
        with pytest.raises(worktree.WorktreeError, match="not 'branch-b'"):
            worktree.create_and_bootstrap_worktree(config2)

    def test_tier1_repo_bootstrap_script_executed(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-t1")
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir()
        bootstrap = scripts_dir / "bootstrap-worktree.sh"
        log = tmp_path / "bootstrap.log"
        bootstrap.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                printf 'ran\\t%s\\n' "$PWD" >> "{log}"
                exit 0
                """
            )
        )
        bootstrap.chmod(0o755)
        subprocess.run(["git", "-C", str(repo), "add", "scripts/bootstrap-worktree.sh"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add bootstrap script"], check=True)

        config = worktree.resolve_worktree_config(
            repo=repo,
            branch="tier1-branch",
        )
        result = worktree.create_and_bootstrap_worktree(config)
        assert result.bootstrap_executed
        assert log.exists()
        log_content = log.read_text().strip()
        assert log_content == f"ran\t{result.worktree_path}"

    def test_tier1_failing_script_raises_worktree_error(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-t1-fail")
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir()
        bootstrap = scripts_dir / "bootstrap-worktree.sh"
        bootstrap.write_text("#!/usr/bin/env bash\nexit 42\n")
        bootstrap.chmod(0o755)
        subprocess.run(["git", "-C", str(repo), "add", "scripts/bootstrap-worktree.sh"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add failing bootstrap script"], check=True)

        config = worktree.resolve_worktree_config(
            repo=repo,
            branch="tier1-fail",
        )
        with pytest.raises(worktree.WorktreeError, match="failed with exit code 42"):
            worktree.create_and_bootstrap_worktree(config)

    def test_tier2_lockfile_heuristics_with_stubs(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-t2")
        (repo / "pyproject.toml").write_text("[project]\nname='stub'\n")
        subprocess.run(["git", "-C", str(repo), "add", "pyproject.toml"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add pyproject"], check=True)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "tools.log"
        uv_stub = bin_dir / "uv"
        uv_stub.write_text(
            f'#!/usr/bin/env bash\nprintf "uv\\t%s\\t%s\\n" "$PWD" "$*" >> "{log}"\nexit 0\n'
        )
        uv_stub.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        try:
            config = worktree.resolve_worktree_config(
                repo=repo,
                branch="tier2-branch",
            )
            result = worktree.create_and_bootstrap_worktree(config)
            assert result.bootstrap_executed
            assert log.exists()
            lines = log.read_text().splitlines()
            assert any("uv\t" in line and "sync" in line for line in lines)
        finally:
            os.environ["PATH"] = old_path

    def test_skip_bootstrap_flag(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-skip")
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir()
        bootstrap = scripts_dir / "bootstrap-worktree.sh"
        log = tmp_path / "never_run.log"
        bootstrap.write_text(f'#!/usr/bin/env bash\necho ran >> "{log}"\nexit 0\n')
        bootstrap.chmod(0o755)
        subprocess.run(["git", "-C", str(repo), "add", "scripts/bootstrap-worktree.sh"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add bootstrap"], check=True)

        config = worktree.resolve_worktree_config(
            repo=repo,
            branch="skip-branch",
            skip_bootstrap=True,
        )
        result = worktree.create_and_bootstrap_worktree(config)
        assert not result.bootstrap_executed
        assert not log.exists()

    def test_cli_stdout_prints_worktree_path(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-cli")
        script_path = REPO_ROOT / "agent-scripts" / "worktree.py"

        res = subprocess.run(
            [sys.executable, str(script_path), "--repo", str(repo), "--branch", "cli-branch", "--skip-bootstrap"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        expected_dest = (tmp_path / "repo-cli-cli-branch").resolve()
        assert res.stdout.strip() == str(expected_dest)
        assert expected_dest.is_dir()

    def test_cli_json_output(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-json")
        script_path = REPO_ROOT / "agent-scripts" / "worktree.py"

        res = subprocess.run(
            [sys.executable, str(script_path), "--repo", str(repo), "--branch", "json-branch", "--skip-bootstrap", "--json"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        expected_dest = str((tmp_path / "repo-json-json-branch").resolve())
        assert data["worktree_path"] == expected_dest
        assert data["branch"] == "json-branch"
        assert not data["reused"]
        assert not data["bootstrap_executed"]
        assert isinstance(data["diagnostics"], list)

    def test_cli_quiet_suppresses_stderr_diagnostics(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-quiet")
        script_path = REPO_ROOT / "agent-scripts" / "worktree.py"

        res = subprocess.run(
            [sys.executable, str(script_path), "--repo", str(repo), "--branch", "quiet-branch", "--skip-bootstrap", "--quiet"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        assert res.stderr.strip() == ""

    def test_dev_status_worktree_cli(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo-devstatus-wt")
        script_path = REPO_ROOT / "agent-scripts" / "dev_status.py"

        res = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "worktree",
                "--repo",
                str(repo),
                "--branch",
                "devstatus-branch",
                "--skip-bootstrap",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        expected_dest = str((tmp_path / "repo-devstatus-wt-devstatus-branch").resolve())
        assert data["worktree_path"] == expected_dest
        assert data["branch"] == "devstatus-branch"
        assert not data["reused"]
        assert not data["bootstrap_executed"]

    def test_dev_status_worktree_missing_args(self) -> None:
        script_path = REPO_ROOT / "agent-scripts" / "dev_status.py"
        res = subprocess.run(
            [sys.executable, str(script_path), "worktree"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 2
        assert "either a backlog item slug/id or --repo must be provided" in res.stderr

