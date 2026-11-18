#!/usr/bin/env python3
"""Direct-call tests for guard_rails' git integration layer.

``git``, ``repo_info``, ``common_dir_of``, ``_behind_ref`` and
``_session_identity`` were only ever reached through a mocked
``guard_rails.git`` (``test_guard_rails.py``) or end to end through
``evaluate()``, so every branch in them stayed alive under mutation. These
call them directly, against real git.

The topology assertions ``test_guard_rails_topology.py`` already makes about
``repo_info``/``common_dir_of`` in a main checkout, a linked worktree, a bare
repo and a submodule are deliberately not repeated here; this module adds the
call sites that file has no test for.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dev_status_impl  # noqa: E402
import guard_rails  # noqa: E402
import worktree_provenance  # noqa: E402
from test_guard_rails_topology import _git, _init_repo  # noqa: E402

# Runs the real git binary (and, in the fallback test, a stub git on PATH that
# delegates to it) against throwaway repositories under pytest's tmp_path, and
# spawns two python children from tmp_path for the ancestor-walk test -- one
# of them under a harness basename, which is how the walk is controlled. No
# repository, backlog store or user path outside the sandbox is touched.
pytestmark = pytest.mark.allow_real_subprocess


@pytest.fixture
def main_checkout(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "proj")


@pytest.fixture
def worktree(main_checkout: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "proj-feature"
    _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main_checkout)
    return wt


class TestGit:
    def test_success_returns_stripped_stdout(self, main_checkout: Path) -> None:
        out = guard_rails.git("-C", str(main_checkout), "rev-parse", "--show-toplevel")
        # Stripped: git appends a newline, the caller gets the bare path.
        assert out == str(main_checkout.resolve())

    def test_a_failing_command_discards_the_stdout_it_produced(
        self, main_checkout: Path
    ) -> None:
        (main_checkout / "f.txt").write_text("changed\n")
        # Positive control: this exact command writes a non-empty diff to stdout
        # and still exits non-zero, so the None below is the returncode check
        # discarding real output -- not a wrapper that discarded every answer.
        diff_out = guard_rails.git("-C", str(main_checkout), "diff")
        assert isinstance(diff_out, str) and diff_out
        assert guard_rails.git("-C", str(main_checkout), "diff", "--exit-code") is None

    def test_a_cwd_git_cannot_enter_returns_none(self, tmp_path: Path) -> None:
        # subprocess cannot chdir into a missing cwd and raises, so this is the
        # OSError arm rather than git's own non-zero exit.
        assert guard_rails.git("--version", cwd=str(tmp_path / "gone")) is None


class TestRepoInfoDirect:
    def test_a_marker_in_a_linked_worktree_is_reported(self, worktree: Path) -> None:
        info = guard_rails.repo_info(str(worktree))
        assert info is not None
        assert info.is_worktree is True
        worktree_provenance.marker_path(Path(info.git_dir)).write_text("sample-item\n")
        assert guard_rails.repo_info(str(worktree)).marker_slug == "sample-item"  # type: ignore[union-attr]

    def test_a_marker_in_a_main_checkout_is_ignored(
        self, main_checkout: Path
    ) -> None:
        info = guard_rails.repo_info(str(main_checkout))
        assert info is not None
        assert info.is_worktree is False
        assert info.is_bare is False
        marker = worktree_provenance.marker_path(Path(info.git_dir))
        marker.write_text("sample-item\n")
        # Positive control: a valid marker really is sitting where repo_info
        # would read it, so the None below is the is_worktree-and-not-bare
        # gate refusing it, not a missing file.
        assert worktree_provenance.read_marker(Path(info.git_dir)) == "sample-item"
        assert guard_rails.repo_info(str(main_checkout)).marker_slug is None  # type: ignore[union-attr]

    def test_a_bare_repo_is_bare_and_carries_no_marker(
        self, tmp_path: Path
    ) -> None:
        bare = tmp_path / "bare.git"
        _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
        (bare / worktree_provenance.MARKER_FILENAME).write_text("sample-item\n")
        info = guard_rails.repo_info(str(bare))
        assert info is not None
        assert info.is_bare is True
        assert info.is_worktree is False
        assert info.marker_slug is None

    def test_a_directory_outside_any_repository_is_none(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        assert guard_rails.repo_info(str(outside)) is None
        assert guard_rails.common_dir_of(str(outside)) is None


class TestCommonDirFallback:
    def test_relative_common_dir_is_absolutised_against_the_target(
        self,
        main_checkout: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """git older than 2.31 has no ``--path-format=absolute``, so
        ``rev-parse --git-common-dir`` prints a path relative to the ``-C``
        directory. A stub git that rejects exactly that flag and delegates
        everything else reproduces that CLI; the real function under test and
        the real binary both still run."""
        real_git = shutil.which("git")
        assert real_git is not None
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "--path-format=absolute" ]; then\n'
            '    echo "error: unknown option \'--path-format=absolute\'" >&2\n'
            "    exit 129\n"
            "  fi\n"
            "done\n"
            f'exec "{real_git}" "$@"\n'
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
        # Outside the checkout: an absolutisation that wrongly resolves the
        # relative ".git" against the process cwd cannot land on .git by luck.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        # The stub is the git on PATH, and it refuses the modern flag ...
        assert (
            guard_rails.git(
                "-C",
                str(main_checkout),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
            is None
        )
        # ... while delegating the plain form, which answers relatively.
        assert (
            guard_rails.git("-C", str(main_checkout), "rev-parse", "--git-common-dir")
            == ".git"
        )

        expected = os.path.realpath(main_checkout / ".git")
        assert guard_rails.common_dir_of(str(main_checkout)) == expected
        info = guard_rails.repo_info(str(main_checkout))
        assert info is not None
        assert info.common_dir == expected


class TestBehindRef:
    def test_a_worktree_at_the_ref_is_not_behind(
        self, main_checkout: Path, worktree: Path
    ) -> None:
        base = _git("rev-parse", "HEAD", cwd=main_checkout)
        _git("update-ref", "refs/remotes/origin/main", base, cwd=main_checkout)
        assert guard_rails._behind_ref(str(worktree), "origin/main") is False

    def test_a_worktree_behind_the_ref_is_reported(
        self, main_checkout: Path, worktree: Path
    ) -> None:
        base = _git("rev-parse", "HEAD", cwd=main_checkout)
        _git("update-ref", "refs/remotes/origin/main", base, cwd=main_checkout)
        _git("commit", "-q", "--allow-empty", "-m", "next", cwd=main_checkout)
        ahead = _git("rev-parse", "main", cwd=main_checkout)
        _git("update-ref", "refs/remotes/origin/main", ahead, cwd=main_checkout)
        assert guard_rails._behind_ref(str(worktree), "origin/main") is True

    def test_a_missing_ref_is_never_stale(self, worktree: Path) -> None:
        assert guard_rails._behind_ref(str(worktree), "origin/absent") is False

    def test_an_unborn_head_that_cannot_be_compared_is_not_stale(
        self, main_checkout: Path, worktree: Path
    ) -> None:
        base = _git("rev-parse", "HEAD", cwd=main_checkout)
        _git("update-ref", "refs/remotes/origin/main", base, cwd=main_checkout)
        _git("checkout", "-q", "--orphan", "empty", cwd=worktree)
        # The ref still verifies while HEAD is unborn: rev-parse succeeds and
        # only rev-list fails, which is what the falsy-counts branch handles.
        assert (
            guard_rails.git(
                "-C", str(worktree), "rev-parse", "--verify", "--quiet", "origin/main"
            )
            is not None
        )
        assert guard_rails._behind_ref(str(worktree), "origin/main") is False


class TestSessionIdentity:
    def test_identity_uses_the_machine_id_the_store_stamps(self) -> None:
        machine, owner, chain = guard_rails._session_identity()
        # The guard has to agree with the backlog store on machine identity:
        # a divergent id makes every same-machine claim read as foreign.
        assert machine == dev_status_impl.machine_id()
        assert isinstance(owner, int) and owner > 0
        assert isinstance(chain, list)
        assert all(isinstance(pid, int) and pid > 0 for pid in chain)

    def test_the_owner_is_the_harness_named_ancestor(self, tmp_path: Path) -> None:
        """The owner is the harness process the guard runs under, not merely
        whatever `/proc` last said: the claim this session stamps is keyed on
        that PID, so a walk that always fell back to "this process, empty
        chain" would read its own claim as foreign and deny its own writes.

        The walk decides by argv[0] basename, so a symlink to the interpreter
        named after a harness controls it hermetically: that process is the
        grandchild's parent, and nothing on this machine is involved.
        """
        inner = (
            "import json, os, sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'agent-scripts')!r})\n"
            "import guard_rails\n"
            "print(json.dumps({"
            "'pid': os.getpid(), "
            "'identity': guard_rails._session_identity()}))\n"
        )
        middle = (
            "import os, subprocess, sys\n"
            "print(os.getpid())\n"
            "print(subprocess.run("
            "[sys.executable, '-c', " + repr(inner) + "],"
            " capture_output=True, text=True).stdout, end='')\n"
        )
        harness_named = tmp_path / "pi"
        harness_named.symlink_to(sys.executable)
        proc = subprocess.run(
            [str(harness_named), "-c", middle],
            capture_output=True,
            text=True,
            check=True,
        )
        middle_pid = int(proc.stdout.splitlines()[0])
        payload = json.loads(proc.stdout.splitlines()[1])
        _machine, owner, chain = payload["identity"]
        assert chain[0] == payload["pid"]
        assert owner == middle_pid
        assert middle_pid in chain
