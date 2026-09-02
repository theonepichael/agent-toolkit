#!/usr/bin/env python3
"""Tests for ../../scripts/watchcommit.py. Run with: python3 test_watchcommit.py

Uses real temporary git repos rather than mocking subprocess — watchcommit's
whole job is orchestrating git, so a scenario test against real repos catches
what a mocked unit test would miss (e.g. actual rebase/conflict behavior).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import watchcommit  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess


def sh(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.email", "test@test.com")
    sh(path, "git", "config", "user.name", "test")


class WatchcommitRebaseTestCase(unittest.TestCase):
    """Simulates two machines running watchcommit against the same remote —
    the race that used to leave a commit silently stuck forever."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # push()/commit_and_push() write activity state — keep it inside the
        # temp dir so tests never touch the real machine's
        # ~/.local/state/watchcommit/last-activity.json.
        state_dir = self.tmp / "state"
        self.enterContext(patch.object(watchcommit, "STATE_DIR", state_dir))
        self.enterContext(
            patch.object(
                watchcommit, "ACTIVITY_STATE_FILE", state_dir / "last-activity.json"
            )
        )

        remote = self.tmp / "remote.git"
        remote.mkdir()
        sh(remote, "git", "init", "-q", "--bare", "-b", "main")

        seed = self.tmp / "seed"
        init_repo(seed)
        (seed / "shared.txt").write_text("line1\n")
        (seed / "a-only.txt").write_text("a\n")
        (seed / "b-only.txt").write_text("b\n")
        sh(seed, "git", "add", "-A")
        sh(seed, "git", "commit", "-q", "-m", "seed")
        sh(seed, "git", "remote", "add", "origin", str(remote))
        sh(seed, "git", "push", "-q", "-u", "origin", "main")

        self.repo_a = self.tmp / "machine-a"
        self.repo_b = self.tmp / "machine-b"
        sh(self.tmp, "git", "clone", "-q", str(remote), str(self.repo_a))
        sh(self.tmp, "git", "clone", "-q", str(remote), str(self.repo_b))
        for r in (self.repo_a, self.repo_b):
            sh(r, "git", "config", "user.email", "test@test.com")
            sh(r, "git", "config", "user.name", "test")

    def ahead_behind(self, repo: Path) -> str:
        return sh(
            repo, "git", "rev-list", "--left-right", "--count", "origin/main...HEAD"
        ).stdout.strip()

    def test_nonconflicting_divergence_resolves(self) -> None:
        (self.repo_a / "a-only.txt").write_text("a-changed\n")
        watchcommit.commit_and_push(self.repo_a, "chore: a-only edit")

        (self.repo_b / "b-only.txt").write_text("b-changed\n")
        watchcommit.commit_and_push(self.repo_b, "chore: b-only edit")

        self.assertEqual(self.ahead_behind(self.repo_b), "0\t0")
        remote_log = sh(self.repo_b, "git", "log", "origin/main", "--oneline").stdout
        self.assertIn("a-only edit", remote_log)
        self.assertIn("b-only edit", remote_log)

    def test_stuck_unpushed_commit_gets_retried(self) -> None:
        # Reproduces the exact bug: B has a local commit + clean working
        # tree (has_changes() False), so the old loop would never look at
        # it again. has_unpushed_commits() exists precisely for this state.
        (self.repo_a / "a-only.txt").write_text("a-changed\n")
        watchcommit.commit_and_push(self.repo_a, "chore: a-only edit")

        (self.repo_b / "b-only.txt").write_text("b-changed\n")
        sh(self.repo_b, "git", "add", "-A")
        sh(self.repo_b, "git", "commit", "-q", "-m", "chore: b-only edit")

        self.assertTrue(watchcommit.has_unpushed_commits(self.repo_b))
        self.assertFalse(watchcommit.has_changes(self.repo_b))

        watchcommit.push(self.repo_b, "retried stuck commit", trigger="retry")
        self.assertEqual(self.ahead_behind(self.repo_b), "0\t0")

    def test_real_conflict_aborts_without_data_loss(self) -> None:
        (self.repo_a / "shared.txt").write_text("line1\nA's version\n")
        watchcommit.commit_and_push(self.repo_a, "chore: shared edit from A")

        (self.repo_b / "shared.txt").write_text("line1\nB's CONFLICTING version\n")
        watchcommit.commit_and_push(self.repo_b, "chore: shared edit from B")

        rebase_in_progress = (self.repo_b / ".git" / "rebase-merge").exists() or (
            self.repo_b / ".git" / "rebase-apply"
        ).exists()
        self.assertFalse(
            rebase_in_progress, "rebase --abort should leave no rebase state behind"
        )

        local_log = sh(self.repo_b, "git", "log", "--oneline", "-1").stdout
        self.assertIn(
            "shared edit from B",
            local_log,
            "B's commit must survive locally, not be lost",
        )

        remote_log = sh(
            self.repo_b, "git", "log", "origin/main", "--oneline", "-1"
        ).stdout
        self.assertNotIn(
            "shared edit from B",
            remote_log,
            "B's conflicting commit must not force-land on remote",
        )
        self.assertIn("shared edit from A", remote_log)


class WatchcommitActivityStateTestCase(unittest.TestCase):
    """Covers the last-activity.json bookkeeping added so a session (or
    wc-status) can tell daemon-driven git state changes from manual ones."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-activity-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        state_dir = self.tmp / "state"
        self.activity_file = state_dir / "last-activity.json"
        self.enterContext(patch.object(watchcommit, "STATE_DIR", state_dir))
        self.enterContext(
            patch.object(watchcommit, "ACTIVITY_STATE_FILE", self.activity_file)
        )

        remote = self.tmp / "remote.git"
        remote.mkdir()
        sh(remote, "git", "init", "-q", "--bare", "-b", "main")

        seed = self.tmp / "seed"
        init_repo(seed)
        (seed / "seed.txt").write_text("seed\n")
        sh(seed, "git", "add", "-A")
        sh(seed, "git", "commit", "-q", "-m", "seed")
        sh(seed, "git", "remote", "add", "origin", str(remote))
        sh(seed, "git", "push", "-q", "-u", "origin", "main")

        self.repo = self.tmp / "local"
        sh(self.tmp, "git", "clone", "-q", str(remote), str(self.repo))
        sh(self.repo, "git", "config", "user.email", "test@test.com")
        sh(self.repo, "git", "config", "user.name", "test")

        self.other = self.tmp / "other"
        sh(self.tmp, "git", "clone", "-q", str(remote), str(self.other))
        sh(self.other, "git", "config", "user.email", "other@test.com")
        sh(self.other, "git", "config", "user.name", "other")

    def _activity(self) -> dict:
        return json.loads(self.activity_file.read_text())

    def test_commit_and_push_records_commit_and_push(self) -> None:
        (self.repo / "new.txt").write_text("hi\n")
        watchcommit.commit_and_push(self.repo, "chore: add new.txt")

        head_sha = sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        data = self._activity()
        self.assertEqual(data["last_commit"]["sha"], head_sha)
        self.assertEqual(data["last_commit"]["message"], "chore: add new.txt")
        self.assertEqual(data["last_push"]["sha"], head_sha)
        self.assertEqual(data["last_push"]["trigger"], "commit")

    def test_retried_push_records_retry_trigger(self) -> None:
        (self.repo / "new.txt").write_text("hi\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "chore: add new.txt")
        # simulate a prior tick that committed but failed to push: no
        # activity state should exist yet, since we bypassed push() above.
        self.assertFalse(self.activity_file.exists())

        watchcommit.push(
            self.repo, "pushed previously-stuck commit(s)", trigger="retry"
        )

        head_sha = sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        data = self._activity()
        self.assertEqual(data["last_push"]["sha"], head_sha)
        self.assertEqual(data["last_push"]["trigger"], "retry")

    def test_pull_ff_only_records_when_head_moves(self) -> None:
        (self.other / "from-other.txt").write_text("hi\n")
        sh(self.other, "git", "add", "-A")
        sh(self.other, "git", "commit", "-q", "-m", "commit from another machine")
        sh(self.other, "git", "push", "-q")

        before_sha = sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        watchcommit.pull_ff_only(self.repo)
        after_sha = sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()

        self.assertNotEqual(before_sha, after_sha)
        data = self._activity()
        self.assertEqual(data["last_pull"]["from_sha"], before_sha)
        self.assertEqual(data["last_pull"]["to_sha"], after_sha)

    def test_pull_ff_only_noop_does_not_record(self) -> None:
        watchcommit.pull_ff_only(self.repo)
        self.assertFalse(
            self.activity_file.exists(),
            "a no-op pull (nothing new on remote) must not write activity state",
        )


class AgentDetectionTestCase(unittest.TestCase):
    """Unit tests for agent_active() against a fake /proc layout in tmpdir.
    Redirects watchcommit.PROC_DIR and os.getpid via patch."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-proc-"))
        self.fake_proc = self.tmp / "proc"
        self.fake_proc.mkdir()
        self.repo = self.tmp / "repo"
        init_repo(self.repo)
        (self.repo / "seed.txt").write_text("seed\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "seed")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _fake_pid(
        self, pid: int, *, cwd: Path, comm: str, environ: bytes = b""
    ) -> None:
        d = self.fake_proc / str(pid)
        d.mkdir()
        # /proc/[pid]/cwd is a symlink — emulate so .resolve() works
        (d / "cwd").symlink_to(cwd)
        (d / "comm").write_text(comm)
        (d / "environ").write_bytes(environ)

    def test_cwd_inside_repo_name_match_detected(self) -> None:
        self._fake_pid(1234, cwd=self.repo, comm="claude")
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
        ):
            agents = watchcommit.agent_active(self.repo)
        self.assertEqual([a.pid for a in agents], [1234])
        self.assertEqual(agents[0].name, "claude")
        self.assertEqual(agents[0].via, "name")

    def test_cwd_outside_repo_not_detected(self) -> None:
        self._fake_pid(1234, cwd=Path("/tmp"), comm="claude")
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
        ):
            self.assertEqual(watchcommit.agent_active(self.repo), [])

    def test_env_marker_detected_when_name_not_in_list(self) -> None:
        env = b"WATCHCOMMIT_AGENT=1\x00HOME=/tmp\x00"
        # comm is not in default list
        self._fake_pid(5678, cwd=self.repo, comm="non-agent-name", environ=env)
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
        ):
            agents = watchcommit.agent_active(self.repo)
        self.assertEqual([a.pid for a in agents], [5678])
        self.assertEqual(agents[0].via, "marker")

    def test_env_marker_exact_match_no_substring_false_positive(self) -> None:
        # WATCHCOMMIT_AGENT=0 must NOT match — exact val comparison
        env = b"WATCHCOMMIT_AGENT=0\x00"
        self._fake_pid(5678, cwd=self.repo, comm="non-agent-name", environ=env)
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
        ):
            self.assertEqual(watchcommit.agent_active(self.repo), [])

    def test_own_pid_excluded(self) -> None:
        self._fake_pid(9999, cwd=self.repo, comm="claude")
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=9999),
        ):
            self.assertEqual(watchcommit.agent_active(self.repo), [])

    def test_watchcommit_ignore_agents_env_suppresses(self) -> None:
        self._fake_pid(1234, cwd=self.repo, comm="claude")
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
            patch.dict(os.environ, {"WATCHCOMMIT_IGNORE_AGENTS": "1"}, clear=False),
        ):
            self.assertEqual(watchcommit.agent_active(self.repo), [])

    def test_agent_names_env_extends_default_list(self) -> None:
        # 'foo' is not in the default list
        self._fake_pid(1234, cwd=self.repo, comm="foo")
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
            patch.dict(os.environ, {"WATCHCOMMIT_AGENT_NAMES": "foo,bar"}, clear=False),
        ):
            agents = watchcommit.agent_active(self.repo)
        self.assertEqual([a.pid for a in agents], [1234])

    def test_comm_truncation_15_chars(self) -> None:
        # Linux /proc/[pid]/comm stores at most COMM_MAX=15 chars. A
        # registered agent name of length 20 should match by prefix.
        long_name = "supercalafragilistic"
        self._fake_pid(1234, cwd=self.repo, comm=long_name[:15])
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
            patch.dict(os.environ, {"WATCHCOMMIT_AGENT_NAMES": long_name}, clear=False),
        ):
            agents = watchcommit.agent_active(self.repo)
        self.assertEqual([a.pid for a in agents], [1234])
        self.assertEqual(agents[0].name, long_name)

    def test_proc_read_permission_error_fail_safe(self) -> None:
        # broken cwd symlink — .resolve() raises (or returns the literal),
        # walker should skip not crash
        d = self.fake_proc / "7777"
        d.mkdir()
        (d / "cwd").symlink_to("/nonexistent-target-forbidden")
        (d / "comm").write_text("claude")
        # also add a well-formed agent proc to confirm walker continued past
        self._fake_pid(1234, cwd=self.repo, comm="claude")
        with (
            patch.object(watchcommit, "PROC_DIR", self.fake_proc),
            patch("os.getpid", return_value=1),
        ):
            agents = watchcommit.agent_active(self.repo)
        pids = {a.pid for a in agents}
        self.assertIn(1234, pids)
        self.assertNotIn(7777, pids)

    def test_non_linux_no_proc_returns_empty(self) -> None:
        # point PROC_DIR at a path that doesn't exist
        with patch.object(watchcommit, "PROC_DIR", Path("/no/such/proc")):
            self.assertEqual(watchcommit.agent_active(self.repo), [])


class AgentDetectionIntegrationTestCase(unittest.TestCase):
    """Spawns a real subprocess with the WATCHCOMMIT_AGENT=1 env marker so
    agent_active() sees it via the live /proc. Real claude/opencode sessions
    on the host would also legitimately be detected — that's correct behavior,
    so each test filters the returned list down to its own spawned pid."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-int-"))
        self.repo = self.tmp / "repo"
        init_repo(self.repo)
        (self.repo / "seed.txt").write_text("seed\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "seed")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for p in self.children:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()

    def test_real_subprocess_env_marker_detected(self) -> None:
        env = {**os.environ, "WATCHCOMMIT_AGENT": "1"}
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            cwd=str(self.repo),
            env=env,
        )
        self.children.append(proc)
        agents = watchcommit.agent_active(self.repo)
        ours = [a for a in agents if a.pid == proc.pid]
        self.assertTrue(ours, f"spawned proc {proc.pid} not detected: {agents}")
        self.assertEqual(ours[0].via, "marker")

    def test_real_subprocess_without_marker_not_detected(self) -> None:
        # plain python3 in repo, no marker, name not in list → not detected
        env = {k: v for k, v in os.environ.items() if k != "WATCHCOMMIT_AGENT"}
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            cwd=str(self.repo),
            env=env,
        )
        self.children.append(proc)
        agents = watchcommit.agent_active(self.repo)
        ours = [a for a in agents if a.pid == proc.pid]
        self.assertEqual(
            ours, [], "non-agent python3 subprocess must not trigger detection"
        )


class GuardActiveTestCase(unittest.TestCase):
    """Unit tests for guard_active()/is_paused() — the wc-guard side of the
    watchcommit-can-auto-commit-mid-edit fix. wc-guard itself never touches
    PAUSE_FILE; watchcommit is the one that ORs the two signals together."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-guard-active-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # matches wc-guard's own resolution rule ($XDG_STATE_HOME/watchcommit)
        # so the real-subprocess test below and Python-side patches agree.
        self.xdg_state_home = self.tmp
        state_dir = self.tmp / "watchcommit"
        self.pause_file = state_dir / "paused"
        self.guard_pid_dir = state_dir / "guard-pids"
        self.enterContext(patch.object(watchcommit, "STATE_DIR", state_dir))
        self.enterContext(patch.object(watchcommit, "PAUSE_FILE", self.pause_file))
        self.enterContext(
            patch.object(watchcommit, "GUARD_PID_DIR", self.guard_pid_dir)
        )

    def _own_start_time(self) -> str:
        return Path(f"/proc/{os.getpid()}/stat").read_text().split()[21]

    def test_no_state_not_paused(self) -> None:
        self.assertFalse(watchcommit.guard_active())
        self.assertFalse(watchcommit.is_paused())

    def test_live_process_with_matching_start_time_is_active(self) -> None:
        self.guard_pid_dir.mkdir(parents=True)
        (self.guard_pid_dir / str(os.getpid())).write_text(self._own_start_time())
        self.assertTrue(watchcommit.guard_active())
        self.assertTrue(watchcommit.is_paused())

    def test_mismatched_start_time_not_active(self) -> None:
        # simulates PID reuse: this PID is alive, but the recorded
        # start-time doesn't match — must not be treated as our guard.
        self.guard_pid_dir.mkdir(parents=True)
        (self.guard_pid_dir / str(os.getpid())).write_text("999999999")
        self.assertFalse(watchcommit.guard_active())

    def test_dead_pid_not_active(self) -> None:
        # a PID that certainly doesn't exist right now.
        self.guard_pid_dir.mkdir(parents=True)
        (self.guard_pid_dir / "999999").write_text("123")
        self.assertFalse(watchcommit.guard_active())

    def test_pause_file_alone_is_paused(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.touch()
        self.assertFalse(watchcommit.guard_active())
        self.assertTrue(watchcommit.is_paused())

    def test_real_wc_guard_subprocess_marks_active(self) -> None:
        wc_guard = Path(__file__).parent.parent.parent / "scripts" / "wc-guard"
        env = {**os.environ, "XDG_STATE_HOME": str(self.xdg_state_home)}
        proc = subprocess.Popen([str(wc_guard), "sleep", "5"], env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not watchcommit.guard_active():
                time.sleep(0.05)
            self.assertTrue(watchcommit.guard_active(), "guard never registered")
        finally:
            proc.kill()
            proc.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and watchcommit.guard_active():
            time.sleep(0.05)
        self.assertFalse(
            watchcommit.guard_active(), "guard_active stayed True after kill"
        )


if __name__ == "__main__":
    unittest.main()
