#!/usr/bin/env python3
"""Merge-time behaviour of githooks/post-merge.

Real git runs only via subprocess against throwaway fixture repos under
tmp_path (marked ``allow_real_subprocess`` per test/AGENTS.md). The hook is
copied into a fixture-only hooks directory, and ``install.sh`` in each
fixture is a stub that records its arguments and environment to a file
under tmp_path, so no real installer ever runs and nothing outside tmp_path
is touched.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "githooks" / "post-merge"

pytestmark = pytest.mark.allow_real_subprocess  # real git merge drives the
# hook; every invocation runs in a tmp_path repo with a stub installer.

STUB_INSTALL = """#!/usr/bin/env bash
printf '%s|%s\\n' "$*" "${AGENT_TOOLKIT_INSTALL_WRAPPER-unset}" >>"$STUB_LOG"
echo "stub installer output"
exit "${STUB_EXIT:-0}"
"""

# Stands in for coreutils timeout: records the bound it was given, then
# reports a timeout without running anything.
FAKE_TIMEOUT = """#!/usr/bin/env bash
printf '%s\\n' "$1" >>"$TIMEOUT_LOG"
exit 124
"""

SEED_FILES = {
    "links.toml": "# rows\n",
    "codex/skills/x/SKILL.md": "skill\n",
    "codex/skills/x/references/r.md": "reference\n",
    "pi/skills/a.md": "a\n",
    "other.txt": "other\n",
}


class Fixture:
    """A primary checkout on main with the hook, a stub installer and a log."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.log = tmp_path / "stub.log"
        self.env = {**os.environ, "STUB_LOG": str(self.log)}
        self.env.pop("AGENT_TOOLKIT_INSTALL_WRAPPER", None)
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        shutil.copy2(HOOK, hooks / "post-merge")
        self.root.mkdir()
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.git("config", "gc.auto", "0")
        self.git("config", "maintenance.auto", "false")
        for rel, text in SEED_FILES.items():
            self.write(rel, text)
        stub = self.root / "install.sh"
        stub.write_text(STUB_INSTALL)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.git("config", "core.hooksPath", str(hooks))

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd or self.root), *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        return result

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def opt_in(self) -> None:
        self.git("config", "--local", "agent-toolkit.postMergeInstall", "true")

    def branch_with(self, changes: dict[str, str | None], name: str = "feat") -> None:
        """Commit ``changes`` (None deletes) on a new branch off main, back on main."""
        self.git("checkout", "-q", "-b", name, "main")
        for rel, text in changes.items():
            if text is None:
                self.git("rm", "-q", rel)
            else:
                self.write(rel, text)
                self.git("add", rel)
        self.git("commit", "-q", "-m", f"change on {name}")
        self.git("checkout", "-q", "main")

    def merge(self, *args: str, cwd: Path | None = None) -> str:
        result = self.git("merge", "-q", *args, "feat", cwd=cwd)
        assert result.returncode == 0, result.stderr
        return result.stdout + result.stderr

    def runs(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_hook_live(self) -> None:
        """Positive control: on clean main, a links.toml merge does trigger."""
        before = len(self.runs())
        self.git("checkout", "-q", "-f", "main")
        self.git("clean", "-qfd")
        self.git("checkout", "-q", "-b", "control", "main")
        self.write("links.toml", "# rows\n# control row\n")
        self.git("commit", "-qam", "control")
        self.git("checkout", "-q", "main")
        result = self.git("merge", "-q", "control")
        assert result.returncode == 0, result.stderr
        assert len(self.runs()) == before + 1, "hook did not trigger on the control"


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


# ── opted in: the installer runs ─────────────────────────────────────────────


def test_links_change_runs_installer_once(fx: Fixture) -> None:
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    out = fx.merge()

    assert fx.runs() == ["--harness=all|unset"]
    assert "ran install.sh --harness=all" in out
    log = fx.root / ".git" / "post-merge-install.log"
    assert "stub installer output" in log.read_text()
    assert str(log) in out


def test_wrapper_marker_never_reaches_the_installer(fx: Fixture) -> None:
    """The marker would let the installer overwrite the personal overlay."""
    fx.opt_in()
    fx.env["AGENT_TOOLKIT_INSTALL_WRAPPER"] = "1"
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    fx.merge()

    assert fx.runs() == ["--harness=all|unset"]


def test_merge_commit_and_subdirectory_still_trigger(fx: Fixture) -> None:
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    fx.merge("--no-ff", "-m", "merge", cwd=fx.root / "pi")

    assert fx.runs() == ["--harness=all|unset"]


@pytest.mark.parametrize(
    ("changes", "triggers"),
    [
        ({"codex/skills/x/SKILL.md": "skill v2\n"}, True),
        ({"codex/skills/x/references/r.md": "reference v2\n"}, False),
        ({"pi/skills/b.md": "b\n"}, True),
        ({"pi/skills/a.md": None}, True),
        ({"pi/skills/a.md": "a v2\n"}, False),
        ({"codex/skills/x/sub/SKILL.md": "nested\n"}, False),
        ({"other.txt": "other v2\n"}, False),
    ],
    ids=[
        "codex-skill-edit",
        "codex-reference-edit",
        "pi-add",
        "pi-delete",
        "pi-edit",
        "codex-nested-skill-md",
        "unrelated",
    ],
)
def test_trigger_paths(fx: Fixture, changes: dict[str, str | None], triggers: bool) -> None:
    fx.opt_in()
    fx.branch_with(changes)

    out = fx.merge()

    assert (fx.runs() == ["--harness=all|unset"]) is triggers
    assert ("install.sh" in out) is triggers
    if not triggers:
        fx.assert_hook_live()


def test_rename_under_a_dir_link_root_triggers(fx: Fixture) -> None:
    """A rename is a delete plus an add for the per-file links."""
    fx.opt_in()
    fx.git("checkout", "-q", "-b", "feat", "main")
    fx.git("mv", "pi/skills/a.md", "pi/skills/renamed.md")
    fx.git("commit", "-q", "-m", "rename")
    fx.git("checkout", "-q", "main")

    fx.merge()

    assert fx.runs() == ["--harness=all|unset"]


def test_hook_exits_0_when_the_installer_fails(fx: Fixture, tmp_path: Path) -> None:
    """git ignores post-merge's status, so run the hook directly to see it."""
    fx.opt_in()
    fx.env["STUB_EXIT"] = "2"
    fx.branch_with({"links.toml": "# rows\n# new row\n"})
    fx.merge()

    result = subprocess.run(
        [str(tmp_path / "hooks" / "post-merge"), "0"],
        cwd=fx.root,
        env=fx.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "failed (exit 2)" in result.stdout
    assert result.returncode == 0


def test_runs_without_a_timeout_command(fx: Fixture, tmp_path: Path) -> None:
    """Stock macOS has no timeout; the hook must still run the installer."""
    bin_dir = tmp_path / "bin-no-timeout"
    bin_dir.mkdir()
    for tool in ("git", "bash", "env", "printf"):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})
    fx.env["PATH"] = str(bin_dir)

    out = fx.merge()

    assert fx.runs() == ["--harness=all|unset"]
    assert "ran install.sh --harness=all" in out


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        ("1", "ran install.sh --harness=all with skips"),
        ("2", "install.sh --harness=all failed (exit 2)"),
    ],
)
def test_summary_line_by_exit_status(fx: Fixture, exit_code: str, expected: str) -> None:
    fx.opt_in()
    fx.env["STUB_EXIT"] = exit_code
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    out = fx.merge()

    assert expected in out


def test_installer_runs_under_a_60s_timeout(fx: Fixture, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "timeout"
    fake.write_text(FAKE_TIMEOUT)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    timeout_log = tmp_path / "timeout.log"
    fx.env["TIMEOUT_LOG"] = str(timeout_log)
    fx.env["PATH"] = f"{bin_dir}:{fx.env['PATH']}"
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    out = fx.merge()

    assert timeout_log.read_text().split() == ["60"]
    assert "timed out" in out


# ── not opted in, or dirty: reminder only ───────────────────────────────────


def test_not_opted_in_prints_reminder_only(fx: Fixture) -> None:
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    out = fx.merge()

    assert fx.runs() == []
    assert "./install.sh --harness=all" in out


@pytest.mark.parametrize("show_untracked", ["normal", "no"])
def test_dirty_trigger_path_prints_reminder_instead_of_running(
    fx: Fixture, show_untracked: str
) -> None:
    """The installer reads the working tree; local files must not deploy,
    whatever status.showUntrackedFiles hides."""
    fx.git("config", "status.showUntrackedFiles", show_untracked)
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})
    fx.write("pi/skills/local-draft.md", "draft\n")

    out = fx.merge()

    assert fx.runs() == []
    assert "./install.sh --harness=all" in out
    fx.assert_hook_live()


def test_unwritable_log_is_reported_not_run(fx: Fixture) -> None:
    fx.opt_in()
    (fx.root / ".git" / "post-merge-install.log").mkdir()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    out = fx.merge()

    assert fx.runs() == []
    assert "could not write" in out


def test_cdpath_does_not_redirect_git_dir_resolution(fx: Fixture, tmp_path: Path) -> None:
    decoy = tmp_path / "decoy"
    (decoy / ".git").mkdir(parents=True)
    fx.env["CDPATH"] = str(decoy)
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    fx.merge()

    assert fx.runs() == ["--harness=all|unset"]


# ── silent: the hook must not act ───────────────────────────────────────────
# Each case reuses the links.toml change that test_links_change_runs_installer_once
# shows does trigger on this fixture, so a pass here is not vacuous.


def test_squash_merge_is_silent(fx: Fixture) -> None:
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})

    out = fx.merge("--squash")

    assert fx.runs() == []
    assert "install.sh" not in out
    fx.assert_hook_live()


def test_merge_on_another_branch_is_silent(fx: Fixture) -> None:
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})
    fx.git("checkout", "-q", "-b", "other", "main")

    out = fx.merge()

    assert fx.runs() == []
    assert "install.sh" not in out
    fx.assert_hook_live()


def test_merge_on_detached_head_is_silent(fx: Fixture) -> None:
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})
    fx.git("checkout", "-q", "--detach", "main")

    out = fx.merge()

    assert fx.runs() == []
    assert "install.sh" not in out
    fx.assert_hook_live()


def test_merge_in_linked_worktree_is_silent(fx: Fixture, tmp_path: Path) -> None:
    """An install from a worktree would repoint every link at the worktree."""
    fx.opt_in()
    fx.branch_with({"links.toml": "# rows\n# new row\n"})
    worktree = tmp_path / "wt"
    added = fx.git("worktree", "add", "-q", "-f", str(worktree), "main")
    assert added.returncode == 0, added.stderr

    out = fx.merge(cwd=worktree)

    assert fx.runs() == []
    assert "install.sh" not in out
    fx.git("worktree", "remove", "-f", str(worktree))
    fx.assert_hook_live()


# ── the trigger roots must cover every directory link ──────────────────────


def test_every_dir_link_is_under_a_hook_trigger_root() -> None:
    """A dir = true row is expanded into per-file links, so adding a file under
    it needs a rerun; a new directory link outside the hook's roots would be
    silently missed."""
    rows = tomllib.loads((REPO_ROOT / "links.toml").read_text()).get("link", [])
    match = re.search(r"^DIR_LINK_ROOTS=\((.*)\)$", HOOK.read_text(), re.MULTILINE)
    assert match, "DIR_LINK_ROOTS not found in the hook"
    roots = match.group(1).split()
    dir_srcs = [row["src"] for row in rows if row.get("dir")]
    assert dir_srcs, "expected at least one dir = true row"
    missing = [src for src in dir_srcs if src not in roots]
    assert missing == []
