#!/usr/bin/env python3
"""Tests for the proactive claim-liveness sweep in render/list/show.

A claim whose owning process is confirmed dead on this machine must be
reverted to `open` by the read paths instead of sitting IN PROGRESS until a
fresh start attempt on that exact item or the full claim TTL elapses.
"""

import io
import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))

import dev_status  # noqa: E402
from test_dev_status import BacklogFixture, _args, make_item  # noqa: E402


def _claim(machine_id, owner_pid=999999, pid=888888, harness="pi"):
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    claim = {
        "harness": harness,
        "machine_id": machine_id,
        "pid": pid,
        "claimed_at": now_iso,
        "last_active": now_iso,
    }
    if owner_pid is not None:
        claim["owner_pid"] = owner_pid
    return claim


class SweepDeadClaimsTestCase(BacklogFixture):
    """render/list/show revert same-machine dead-owner claims to open."""

    def _write_claimed(self, slug, claim):
        self.write_items(
            [make_item(slug, status="in-progress", summary=f"Summary of {slug}")]
        )
        items = dev_status.load_items()
        items[0]["claimed_by"] = claim
        self.write_items(items)

    def _run(self, cmd, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            cmd(_args(**kwargs))
        return out.getvalue(), err.getvalue()

    def test_render_reverts_dead_owner_claim(self):
        self._write_claimed("task-a", _claim(dev_status.machine_id()))
        with patch("dev_status._is_pid_alive", return_value=False):
            out, err = self._run(dev_status.cmd_render)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "open")
        self.assertNotIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 1)
        self.assertIn("task-a", err)
        self.assertIn("999999", err)
        # The reverted item shows under its new status in the same output.
        self.assertIn("task-a", out)

    def test_render_live_claim_untouched(self):
        self._write_claimed("task-a", _claim(dev_status.machine_id()))
        with patch("dev_status._is_pid_alive", return_value=True):
            self._run(dev_status.cmd_render)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "in-progress")
        self.assertIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 0)

    def test_render_cross_machine_claim_untouched(self):
        self._write_claimed("task-a", _claim("some-other-machine"))
        alive_probe = Mock(return_value=False)
        with patch("dev_status._is_pid_alive", alive_probe):
            self._run(dev_status.cmd_render)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "in-progress")
        self.assertIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 0)
        alive_probe.assert_not_called()

    def test_render_no_usable_pid_untouched(self):
        self._write_claimed(
            "task-a", _claim(dev_status.machine_id(), owner_pid=None, pid=None)
        )
        self._run(dev_status.cmd_render)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "in-progress")
        self.assertIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 0)

    def test_render_owner_alive_pid_dead_untouched(self):
        # The owner anchor is authoritative (matching _check_claim_collision's
        # ordering): a live owner keeps the claim even if the ephemeral child
        # pid is dead.
        self._write_claimed("task-a", _claim(dev_status.machine_id()))
        with patch(
            "dev_status._is_pid_alive",
            side_effect=lambda pid: pid == 999999,
        ):
            self._run(dev_status.cmd_render)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "in-progress")
        self.assertIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 0)

    def test_render_legacy_claim_pid_fallback(self):
        # A claim without owner_pid falls back to the ephemeral pid, keeping
        # parity with _check_claim_collision's legacy branch.
        self._write_claimed("task-a", _claim(dev_status.machine_id(), owner_pid=None))
        with patch("dev_status._is_pid_alive", return_value=False):
            self._run(dev_status.cmd_render)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "open")
        self.assertNotIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 1)

    def test_show_reverts_dead_owner_claim(self):
        self._write_claimed("task-a", _claim(dev_status.machine_id()))
        with patch("dev_status._is_pid_alive", return_value=False):
            out, err = self._run(dev_status.cmd_show, id="task-a")
        items = self.read_items()
        self.assertEqual(items[0]["status"], "open")
        self.assertNotIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 1)
        self.assertIn("task-a", err)
        record = json.loads(out)
        self.assertEqual(record["status"], "open")

    def test_list_reverts_dead_owner_claim(self):
        self._write_claimed("task-a", _claim(dev_status.machine_id()))
        with patch("dev_status._is_pid_alive", return_value=False):
            self._run(dev_status.cmd_list)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "open")
        self.assertNotIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 1)

    def test_rev_bumped_once_for_multiple_dead_claims(self):
        self.write_items(
            [
                make_item("task-a", status="in-progress"),
                make_item("task-b", status="in-progress"),
                make_item("task-c", status="open"),
            ]
        )
        items = dev_status.load_items()
        mid = dev_status.machine_id()
        items[0]["claimed_by"] = _claim(mid, owner_pid=999999)
        items[1]["claimed_by"] = _claim(mid, owner_pid=999998, harness="claude")
        self.write_items(items)
        with patch("dev_status._is_pid_alive", return_value=False):
            self._run(dev_status.cmd_render)
        items = self.read_items()
        statuses = {i["id"]: i["status"] for i in items}
        self.assertEqual(
            statuses,
            {"task-a": "open", "task-b": "open", "task-c": "open"},
        )
        for slug in ("task-a", "task-b"):
            self.assertNotIn("claimed_by", next(i for i in items if i["id"] == slug))
        self.assertEqual(self.read_rev(), 1)

    def test_notice_carries_forensics(self):
        self._write_claimed(
            "task-a", _claim(dev_status.machine_id(), harness="herdr-pi")
        )
        with patch("dev_status._is_pid_alive", return_value=False):
            _out, err = self._run(dev_status.cmd_show, id="task-a")
        self.assertIn("task-a", err)
        self.assertIn("herdr-pi", err)
        self.assertIn("999999", err)
        self.assertIn("claimed", err)

    def test_show_unrelated_item_still_sweeps_other_dead_claims(self):
        self.write_items(
            [
                make_item("task-a", status="in-progress"),
                make_item("task-b", status="open"),
            ]
        )
        items = dev_status.load_items()
        items[0]["claimed_by"] = _claim(dev_status.machine_id())
        self.write_items(items)
        with patch("dev_status._is_pid_alive", return_value=False):
            self._run(dev_status.cmd_show, id="task-b")
        items = self.read_items()
        self.assertEqual(items[0]["status"], "open")

    def test_non_in_progress_claim_untouched(self):
        # The dashboard shows the claim tag only on in-progress items; the
        # sweep matches that scope.
        self.write_items([make_item("task-a", status="in-review")])
        items = dev_status.load_items()
        items[0]["claimed_by"] = _claim(dev_status.machine_id())
        self.write_items(items)
        with patch("dev_status._is_pid_alive", return_value=False):
            self._run(dev_status.cmd_render)
        items = self.read_items()
        self.assertEqual(items[0]["status"], "in-review")
        self.assertIn("claimed_by", items[0])
        self.assertEqual(self.read_rev(), 0)

    def test_bump_happens_before_save_when_sweep_reverts(self):
        # House invariant (test_dev_status.py's 14f guard): the rev is bumped
        # ahead of every write, so a crash mid-write never leaves changed
        # data sitting under a stale rev.
        self.write_items([make_item("task-a", status="in-progress")])
        items = dev_status.load_items()
        items[0]["claimed_by"] = _claim(dev_status.machine_id())
        self.write_items(items)

        manager = unittest.mock.Mock()
        with (
            patch.object(
                dev_status, "bump_rev", wraps=dev_status.bump_rev
            ) as bump_mock,
            patch.object(
                dev_status, "save_items", wraps=dev_status.save_items
            ) as save_mock,
            patch("dev_status._is_pid_alive", return_value=False),
        ):
            manager.attach_mock(bump_mock, "bump_rev")
            manager.attach_mock(save_mock, "save_items")
            self._run(dev_status.cmd_render)

        self.assertEqual(bump_mock.call_count, 1)
        self.assertEqual(save_mock.call_count, 1)
        call_strs = [str(c) for c in manager.mock_calls]
        bump_pos = next(
            i for i, s in enumerate(call_strs) if s.startswith("call.bump_rev(")
        )
        save_pos = next(
            i for i, s in enumerate(call_strs) if s.startswith("call.save_items(")
        )
        self.assertLess(bump_pos, save_pos)

    def test_no_write_when_all_claims_alive(self):
        self._write_claimed("task-a", _claim(dev_status.machine_id()))
        with (
            patch("dev_status._is_pid_alive", return_value=True),
            patch.object(
                dev_status, "save_items", wraps=dev_status.save_items
            ) as save_mock,
            patch.object(
                dev_status, "bump_rev", wraps=dev_status.bump_rev
            ) as bump_mock,
        ):
            self._run(dev_status.cmd_render)
        bump_mock.assert_not_called()
        save_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
