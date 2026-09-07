#!/usr/bin/env python3
"""Tests for to_tickets_runner.py. Run with: python3 test_to_tickets_runner.py"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import dev_status
import to_tickets_runner as runner


def make_item(slug, blocked_by=None, status="open"):
    return {
        "id": slug,
        "created": "2026-01-01",
        "updated": "2026-01-01",
        "status": status,
        "summary": f"Summary of {slug}",
        "category": "feature",
        "blocked_by": blocked_by or [],
        "related_files": [],
        "context": "",
        "next_steps": "",
    }


def make_ticket(tid, summary=None, blocked_by=None):
    return {
        "id": tid,
        "summary": summary or f"Summary of {tid}",
        "category": "feature",
        "context": "",
        "next_steps": "",
        "related_files": [],
        "blocked_by": blocked_by or [],
    }


class RunnerTestCase(unittest.TestCase):
    """Base fixture: isolated tempdirs standing in for the backlog store and batch file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        data_dir = Path(self.tmpdir) / "backlog"
        self.items_file = data_dir / "items.json"
        self.pending_file = data_dir / "pending_items.json"
        self.meta_file = data_dir / "_meta.json"
        self.lock_file = data_dir / ".backlog.lock"
        self.journal_file = data_dir / "journal.jsonl"
        self._patches = [
            patch.object(dev_status, "DATA_DIR", data_dir),
            patch.object(dev_status, "ITEMS_FILE", self.items_file),
            patch.object(dev_status, "PENDING_FILE", self.pending_file),
            patch.object(dev_status, "META_FILE", self.meta_file),
            patch.object(dev_status, "LOCK_FILE", self.lock_file),
            patch.object(dev_status, "JOURNAL_FILE", self.journal_file),
        ]
        for p in self._patches:
            p.start()

        self.batch_dir = Path(self.tmpdir) / "batches"
        self.batch_dir.mkdir(parents=True)
        self.batch_path = self.batch_dir / "sample-tickets-batch.json"

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def write_batch(self, tickets):
        self.batch_path.write_text(json.dumps(tickets))

    def seed_items(self, items):
        dev_status.save_items(items)

    def load_items(self):
        return dev_status.load_items()


# ── schema validation ──────────────────────────────────────────────────────


class ValidateBatchSchemaTests(unittest.TestCase):
    def test_rejects_non_list(self):
        with self.assertRaises(runner.BatchError):
            runner._validate_batch_schema({"id": "x"})

    def test_rejects_empty_list(self):
        with self.assertRaises(runner.BatchError):
            runner._validate_batch_schema([])

    def test_rejects_missing_id(self):
        with self.assertRaises(runner.BatchError):
            runner._validate_batch_schema([{"summary": "no id"}])

    def test_rejects_missing_summary(self):
        with self.assertRaises(runner.BatchError):
            runner._validate_batch_schema([{"id": "some-slug"}])

    def test_rejects_invalid_slug(self):
        with self.assertRaises(runner.BatchError):
            runner._validate_batch_schema([{"id": "Not_A_Valid_Slug", "summary": "s"}])

    def test_rejects_duplicate_id(self):
        with self.assertRaises(runner.BatchError):
            runner._validate_batch_schema(
                [
                    {"id": "some-slug", "summary": "one"},
                    {"id": "some-slug", "summary": "two"},
                ]
            )

    def test_rejects_non_string_blocked_by_entry(self):
        with self.assertRaises(runner.BatchError):
            runner._validate_batch_schema(
                [{"id": "some-slug", "summary": "s", "blocked_by": [1]}]
            )

    def test_accepts_minimal_ticket_and_fills_defaults(self):
        tickets = runner._validate_batch_schema([{"id": "some-slug", "summary": "s"}])
        self.assertEqual(tickets[0]["category"], "feature")
        self.assertEqual(tickets[0]["blocked_by"], [])
        self.assertEqual(tickets[0]["related_files"], [])


# ── ordering / cycle detection ──────────────────────────────────────────────


class ComputeOrderTests(unittest.TestCase):
    def test_single_ticket_no_edges(self):
        tickets = [make_ticket("solo-ticket")]
        order = runner.compute_order(tickets, {})
        self.assertEqual(order, ["solo-ticket"])

    def test_linear_chain_in_declared_order(self):
        tickets = [
            make_ticket("step-one"),
            make_ticket("step-two", blocked_by=["step-one"]),
            make_ticket("step-three", blocked_by=["step-two"]),
        ]
        order = runner.compute_order(tickets, {})
        self.assertEqual(order, ["step-one", "step-two", "step-three"])

    def test_array_order_reversed_still_sorts_correctly(self):
        # Drafted in the opposite order of the true dependency chain — the
        # sort must still place step-one before step-two before step-three.
        tickets = [
            make_ticket("step-three", blocked_by=["step-two"]),
            make_ticket("step-two", blocked_by=["step-one"]),
            make_ticket("step-one"),
        ]
        order = runner.compute_order(tickets, {})
        self.assertEqual(order.index("step-one"), 0)
        self.assertLess(order.index("step-one"), order.index("step-two"))
        self.assertLess(order.index("step-two"), order.index("step-three"))

    def test_dependency_on_existing_slug_is_allowed(self):
        tickets = [make_ticket("new-ticket", blocked_by=["existing-item"])]
        index = {"existing-item": make_item("existing-item")}
        order = runner.compute_order(tickets, index)
        self.assertEqual(order, ["new-ticket"])

    def test_unknown_external_slug_raises(self):
        tickets = [make_ticket("new-ticket", blocked_by=["nonexistent-item"])]
        with self.assertRaises(runner.BatchError):
            runner.compute_order(tickets, {})

    def test_cycle_among_batch_entries_raises(self):
        tickets = [
            make_ticket("ticket-a", blocked_by=["ticket-b"]),
            make_ticket("ticket-b", blocked_by=["ticket-a"]),
        ]
        with self.assertRaises(runner.BatchError):
            runner.compute_order(tickets, {})


# ── state file ───────────────────────────────────────────────────────────


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.batch_path = Path(self.tmpdir) / "some-batch.json"
        self.batch_path.write_text(json.dumps([{"id": "x", "summary": "s"}]))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_state_path_derivation(self):
        expected = Path(self.tmpdir) / "some-batch.state.json"
        self.assertEqual(runner._state_path(self.batch_path), expected)

    def test_load_state_returns_none_when_absent(self):
        self.assertIsNone(runner.load_state(self.batch_path))

    def test_write_then_load_round_trips(self):
        runner.write_state(self.batch_path, {"batch_hash": "abc", "added": {"x": True}})
        state = runner.load_state(self.batch_path)
        self.assertEqual(state, {"batch_hash": "abc", "added": {"x": True}})

    def test_write_state_is_atomic_no_tmp_file_left_behind(self):
        runner.write_state(self.batch_path, {"batch_hash": "abc", "added": {}})
        tmp_leftover = runner._state_path(self.batch_path).with_name(
            runner._state_path(self.batch_path).name + ".tmp"
        )
        self.assertFalse(tmp_leftover.exists())

    def test_delete_state_removes_file(self):
        runner.write_state(self.batch_path, {"batch_hash": "abc", "added": {}})
        runner.delete_state(self.batch_path)
        self.assertFalse(runner._state_path(self.batch_path).exists())

    def test_delete_state_is_a_no_op_when_absent(self):
        runner.delete_state(self.batch_path)  # must not raise


# ── run(): end-to-end against a real (temp-redirected) store ──────────────


class RunTests(RunnerTestCase):
    def test_single_ticket_creates_one_item(self):
        self.write_batch([make_ticket("solo-ticket")])
        created = runner.run(self.batch_path)
        self.assertEqual(created, ["solo-ticket"])
        items = self.load_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "solo-ticket")

    def test_linear_chain_creates_in_dependency_order(self):
        self.write_batch(
            [
                make_ticket("step-one"),
                make_ticket("step-two", blocked_by=["step-one"]),
            ]
        )
        created = runner.run(self.batch_path)
        self.assertEqual(created, ["step-one", "step-two"])
        items = {i["id"]: i for i in self.load_items()}
        self.assertEqual(items["step-two"]["blocked_by"], ["step-one"])

    def test_reversed_array_order_still_creates_correctly(self):
        self.write_batch(
            [
                make_ticket("step-two", blocked_by=["step-one"]),
                make_ticket("step-one"),
            ]
        )
        created = runner.run(self.batch_path)
        self.assertEqual(created, ["step-one", "step-two"])

    def test_blocked_by_existing_item_is_linked(self):
        self.seed_items([make_item("existing-item")])
        self.write_batch([make_ticket("new-ticket", blocked_by=["existing-item"])])
        runner.run(self.batch_path)
        items = {i["id"]: i for i in self.load_items()}
        self.assertEqual(items["new-ticket"]["blocked_by"], ["existing-item"])

    def test_state_file_deleted_after_full_success(self):
        self.write_batch([make_ticket("solo-ticket")])
        runner.run(self.batch_path)
        self.assertFalse(runner._state_path(self.batch_path).exists())

    def test_unknown_external_slug_raises_before_any_mutation(self):
        self.write_batch([make_ticket("new-ticket", blocked_by=["nonexistent"])])
        with self.assertRaises(runner.BatchError):
            runner.run(self.batch_path)
        self.assertEqual(self.load_items(), [])

    def test_cycle_raises_before_any_mutation(self):
        self.write_batch(
            [
                make_ticket("ticket-a", blocked_by=["ticket-b"]),
                make_ticket("ticket-b", blocked_by=["ticket-a"]),
            ]
        )
        with self.assertRaises(runner.BatchError):
            runner.run(self.batch_path)
        self.assertEqual(self.load_items(), [])

    def test_resume_after_partial_completion_skips_already_added(self):
        self.write_batch(
            [
                make_ticket("step-one"),
                make_ticket("step-two", blocked_by=["step-one"]),
            ]
        )
        # Simulate a prior run that created step-one and recorded state,
        # then was interrupted before step-two.
        self.seed_items([make_item("step-one")])
        runner.write_state(
            self.batch_path,
            {
                "batch_hash": runner._batch_hash(self.batch_path),
                "added": {"step-one": True},
            },
        )
        created = runner.run(self.batch_path)
        self.assertEqual(created, ["step-one", "step-two"])
        items = self.load_items()
        self.assertEqual(len(items), 2)  # step-one not duplicated

    def test_stale_state_file_refuses_to_resume(self):
        self.write_batch([make_ticket("solo-ticket")])
        runner.write_state(
            self.batch_path, {"batch_hash": "stale-hash-does-not-match", "added": {}}
        )
        with self.assertRaises(runner.BatchError):
            runner.run(self.batch_path)
        # The stale state file must not be silently consumed/deleted either.
        self.assertTrue(runner._state_path(self.batch_path).exists())

    def test_unrecorded_slug_collision_aborts_without_skipping(self):
        # An unrelated, pre-existing item happens to share a slug with a
        # freshly drafted ticket — no state file marks it as added by this
        # batch, so this must abort loudly, not be treated as a resume.
        self.seed_items([make_item("solo-ticket")])
        self.write_batch([make_ticket("solo-ticket")])
        with self.assertRaises(runner.SlugCollisionError):
            runner.run(self.batch_path)

    def test_pending_slug_collision_aborts(self):
        dev_status.save_pending(
            [
                {
                    "id": "solo-ticket",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "d",
                    "kind": "email",
                    "source_ref": {},
                    "context": "",
                    "next_steps": [],
                    "blocking": [],
                    "outcome": None,
                }
            ]
        )
        self.write_batch([make_ticket("solo-ticket")])
        with self.assertRaises(runner.SlugCollisionError):
            runner.run(self.batch_path)

    def test_journal_event_recorded_per_ticket(self):
        self.write_batch([make_ticket("solo-ticket")])
        runner.run(self.batch_path)
        self.assertTrue(self.journal_file.exists())
        lines = self.journal_file.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["cmd"], "add")
        self.assertEqual(entry["slug"], "solo-ticket")


# ── data directory self-ensure ─────────────────────────────────────────────


class DataDirSelfEnsureTests(unittest.TestCase):
    """DATA_DIR holds batch files agents write, so to_tickets_runner.py owns it.

    The to-tickets skill has the agent write its batch JSON there with its own
    file tools before invoking this script — the same shape that had agents
    running `mkdir -p ~/.claude/data/grill` defensively. Every invocation
    ensures the directory, so that reflex has nothing left to guard against.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self.tmpdir) / "to-tickets"
        self._patch = patch.object(runner, "DATA_DIR", self.data_dir)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        shutil.rmtree(self.tmpdir)

    def test_ensure_data_dir_creates_missing_directory(self) -> None:
        self.assertFalse(self.data_dir.exists())
        runner.ensure_data_dir()
        self.assertTrue(self.data_dir.is_dir())

    def test_ensure_data_dir_is_idempotent(self) -> None:
        runner.ensure_data_dir()
        marker = self.data_dir / "keep-tickets-batch.json"
        marker.write_text("[]")
        runner.ensure_data_dir()
        self.assertEqual(marker.read_text(), "[]")

    def test_run_subcommand_creates_the_directory(self) -> None:
        self.assertFalse(self.data_dir.exists())
        with (
            patch.object(sys, "argv", ["to_tickets_runner.py", "run", "batch.json"]),
            patch.object(runner, "cmd_run"),
        ):
            runner.main()
        self.assertTrue(self.data_dir.is_dir())

    def test_data_dir_is_not_the_grill_dir(self) -> None:
        """Sharing grill's dir would break its private session glob — assert we don't."""
        import grill

        self._patch.stop()
        try:
            self.assertNotEqual(runner.DATA_DIR, grill.DATA_DIR)
        finally:
            self._patch.start()


if __name__ == "__main__":
    unittest.main()
