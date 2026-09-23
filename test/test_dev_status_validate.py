#!/usr/bin/env python3
"""Tests for the read-only `validate` command (release-1 migration post-commit check).

The validator must not mutate the store it validates -- the dead-claim sweep
that render/list/show run is skipped -- and it must take no migration scope,
so it succeeds as a pure read while the toolkit-home migrator holds the
migration lock exclusively.
"""

import fcntl
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
import dev_status  # noqa: E402
import migration_lock  # noqa: E402

from test_dev_status import BacklogFixture, _args, make_item  # noqa: E402


def _hold_migration_lock(mode: int) -> int:
    """Take the migration lock on a private fd (a separate open file description).

    ``flock`` locks belong to the open file description, so a second ``os.open``
    in this same process conflicts with the lock the implementation takes via
    its own fd -- exactly the cross-process contention the migrator creates,
    without spawning a subprocess (which ``BacklogFixture`` mocks away).
    """
    path = migration_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, mode)
    return fd


def _release_migration_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


class ValidateStoreTestCase(BacklogFixture):
    """`validate` is a pure read: no sweep, no migration scope."""

    def _run_validate(self) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_validate(_args())
        return out.getvalue(), err.getvalue()

    def _write_dead_claim(self, slug: str) -> None:
        self.write_items(
            [make_item(slug, status="in-progress", summary=f"Summary of {slug}")]
        )
        items = dev_status.load_items()
        items[0]["claimed_by"] = {
            "harness": "pi",
            "machine_id": dev_status.machine_id(),
            "pid": 999999,
            "owner_pid": 999999,
            "claimed_at": "2026-01-01T00:00:00Z",
            "last_active": "2026-01-01T00:00:00Z",
        }
        self.write_items(items)

    def test_validate_renders_and_passes_on_a_valid_store(self):
        self.write_items([make_item("task-a")])
        out, err = self._run_validate()
        self.assertIn("task-a", out)
        self.assertIn("validate: passed", err)

    def test_validate_leaves_dead_claims_untouched(self):
        # A fixture of only unclaimed items passes either way, so the
        # meaningful case is in-progress items carrying dead claims.
        self._write_dead_claim("task-a")
        with patch("dev_status._is_pid_alive", return_value=False):
            self._run_validate()
        after = self.read_items()
        self.assertEqual(after[0]["status"], "in-progress")
        self.assertIn("claimed_by", after[0])
        self.assertEqual(self.read_rev(), 0)

    def test_validate_fails_on_a_corrupt_store(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "items.json").write_text("{ this is not valid json")
        with self.assertRaises(SystemExit) as ctx:
            self._run_validate()
        self.assertEqual(ctx.exception.code, 1)

    def test_validate_succeeds_under_exclusive_migration_lock(self):
        # The migrator holds the migration lock exclusively; the validator
        # must succeed as a pure read that never enters a migration scope.
        self.write_items([make_item("task-a")])
        fd = _hold_migration_lock(fcntl.LOCK_EX)
        try:
            _, err = self._run_validate()
        finally:
            _release_migration_lock(fd)
        self.assertIn("validate: passed", err)

    def test_validate_takes_no_migration_scope(self):
        # Control: a normal read path (render) takes backlog_lock, which
        # enters migration_lock.shared and is refused while the lock is held
        # exclusively. The validator must not be refused -- it proves it
        # never enters the migration scope.
        self.write_items([make_item("task-a")])
        fd = _hold_migration_lock(fcntl.LOCK_EX)
        try:
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(migration_lock.MigrationLockBusy):
                    dev_status.cmd_render(_args())
        finally:
            _release_migration_lock(fd)


if __name__ == "__main__":
    unittest.main()
