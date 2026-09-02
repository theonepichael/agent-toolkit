#!/usr/bin/env python3
"""Subprocess-level tests for ../../scripts/wc-guard. Run with:
python3 test_wc_guard.py

Companion to test_watchcommit.py's GuardActiveTestCase, which covers
guard_active()/is_paused() directly. These tests exercise wc-guard as a
real subprocess — exit-code passthrough, SIGINT, pre-existing manual pause,
and genuine concurrent overlap — since that's exactly the class of bug
found and fixed across this item's design (see
~/.claude/data/grill/meta-watchcommit-test-script-safety-spec.md).
"""

import os
import shutil
import signal
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

WC_GUARD = Path(__file__).parent.parent.parent / "scripts" / "wc-guard"


class WcGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-guard-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.xdg_state_home = self.tmp
        state_dir = self.tmp / "watchcommit"
        self.pause_file = state_dir / "paused"
        self.guard_pid_dir = state_dir / "guard-pids"
        self.enterContext(patch.object(watchcommit, "STATE_DIR", state_dir))
        self.enterContext(patch.object(watchcommit, "PAUSE_FILE", self.pause_file))
        self.enterContext(
            patch.object(watchcommit, "GUARD_PID_DIR", self.guard_pid_dir)
        )
        self.env = {**os.environ, "XDG_STATE_HOME": str(self.xdg_state_home)}

    def _wait_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_exit_code_zero_propagates(self) -> None:
        result = subprocess.run([str(WC_GUARD), "bash", "-c", "exit 0"], env=self.env)
        self.assertEqual(result.returncode, 0)

    def test_exit_code_nonzero_propagates(self) -> None:
        result = subprocess.run([str(WC_GUARD), "bash", "-c", "exit 7"], env=self.env)
        self.assertEqual(result.returncode, 7)

    def test_guard_active_during_run_gone_after(self) -> None:
        proc = subprocess.Popen(
            [str(WC_GUARD), "bash", "-c", "sleep 0.1"], env=self.env
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(
            self._wait_until(watchcommit.guard_active), "guard never registered"
        )
        proc.wait(timeout=3)
        self.assertTrue(
            self._wait_until(lambda: not watchcommit.guard_active()),
            "guard_active stayed True after normal exit",
        )

    def test_sigint_still_resumes(self) -> None:
        # start_new_session so this test can signal the whole process group,
        # the same way a real terminal's Ctrl-C hits both wc-guard and its
        # foreground child at once. (Signaling only the wrapper process, as
        # an earlier draft of this test did, doesn't interrupt a synchronous
        # foreground child promptly — a red herring from flawed test
        # methodology, not a wc-guard bug; see the spec's design history.)
        proc = subprocess.Popen(
            [str(WC_GUARD), "bash", "-c", "sleep 30"],
            env=self.env,
            start_new_session=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(self._wait_until(watchcommit.guard_active))
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        code = proc.wait(timeout=3)
        self.assertEqual(code, 130, "SIGINT exit code not propagated correctly")
        self.assertTrue(
            self._wait_until(lambda: not watchcommit.guard_active()),
            "guard_active stayed True after SIGINT",
        )

    def test_preexisting_manual_pause_untouched(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.touch()
        result = subprocess.run([str(WC_GUARD), "bash", "-c", "echo ran"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            self.pause_file.exists(), "wc-guard must never remove a manual pause"
        )

    def test_concurrent_overlap_stays_active_until_both_exit(self) -> None:
        # `cat` blocks on stdin, so the test controls each wrapped command's
        # lifetime by closing the pipe — no wall-clock sleeps, no timing
        # race: every state transition below is either polled for (both
        # wrappers registered, both exited) or already an established fact
        # (p2 registered when we assert it keeps the guard active). The
        # previous revision asserted on a fixed sleep margin, which flaked
        # on CI when p2's registration lost the race against p1's exit.
        def spawn() -> subprocess.Popen:
            return subprocess.Popen(
                [str(WC_GUARD), "cat"], env=self.env, stdin=subprocess.PIPE
            )

        def terminate(proc: subprocess.Popen) -> None:
            if proc.poll() is not None:
                return
            assert proc.stdin is not None
            proc.stdin.close()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

        def registered(pid: int) -> bool:
            # Same validity test guard_active() applies: an entry counts
            # only when its recorded start-time matches the live process.
            entry = self.guard_pid_dir / str(pid)
            if not entry.exists():
                return False
            recorded = entry.read_text().strip()
            actual = watchcommit._proc_start_time(pid)
            return actual is not None and actual == recorded

        p1 = spawn()
        self.addCleanup(terminate, p1)
        p2 = spawn()
        self.addCleanup(terminate, p2)

        self.assertTrue(
            self._wait_until(lambda: registered(p1.pid) and registered(p2.pid)),
            "guards never registered",
        )
        terminate(p1)
        # p1 exited but p2 is still registered — must still be paused.
        self.assertTrue(
            watchcommit.guard_active(), "un-paused too early while p2 still running"
        )
        terminate(p2)
        self.assertTrue(
            self._wait_until(lambda: not watchcommit.guard_active()),
            "guard_active stayed True after both exited",
        )

    def test_invoked_from_zsh_subshell(self) -> None:
        script = (
            f'wc_guard() {{ "{WC_GUARD}" "$@"; }}\n'
            f'wc_guard bash -c "exit 0"\n'
            f"echo exit=$?\n"
        )
        result = subprocess.run(
            ["zsh", "-c", script], env=self.env, capture_output=True, text=True
        )
        self.assertIn("exit=0", result.stdout)

    def test_sigint_from_zsh_subshell_still_resumes(self) -> None:
        script = f'"{WC_GUARD}" bash -c "sleep 30"\n'
        proc = subprocess.Popen(
            ["zsh", "-c", script],
            env=self.env,
            start_new_session=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(self._wait_until(watchcommit.guard_active))
        # signal the whole process group — matches a real terminal's Ctrl-C,
        # which hits every process in the foreground group at once.
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        code = proc.wait(timeout=3)
        self.assertEqual(code, 130, "SIGINT exit code not propagated correctly")
        self.assertTrue(
            self._wait_until(lambda: not watchcommit.guard_active()),
            "guard_active stayed True after SIGINT via zsh subshell",
        )


if __name__ == "__main__":
    unittest.main()
