#!/usr/bin/env python3
"""Tests for the dev_status read-only facade."""

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import backlog_claim_lookup
import dev_status_impl
import dev_status_storage


def make_item(slug, status="open", blocked_by=None, claim=None):
    item = {
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
    if claim is not None:
        item["claimed_by"] = claim
    return item


class TestDevStatusRead(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.items_file = Path(self.tmpdir) / "items.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def write_items(self, items):
        self.items_file.write_text(
            json.dumps({"schema_version": 2, "items": items}, indent=2)
        )

    def test_import_and_queries_do_not_mutate_or_sweep(self):
        self.write_items([make_item("ready-one")])

        def fail(*args, **kwargs):
            raise AssertionError("read facade must not mutate or sweep")

        with (
            patch.object(dev_status_storage, "save_items", side_effect=fail),
            patch.object(dev_status_storage, "bump_rev", side_effect=fail),
            patch.object(dev_status_storage, "append_journal_event", side_effect=fail),
            patch.object(dev_status_impl, "_sweep_dead_claims", side_effect=fail),
        ):
            import dev_status_read

            dev_status_read = importlib.reload(dev_status_read)
            self.assertEqual(
                dev_status_read.get_item("ready-one", items_path=self.items_file)[
                    "id"
                ],
                "ready-one",
            )
            self.assertEqual(
                [i["id"] for i in dev_status_read.ready_items(items_path=self.items_file)],
                ["ready-one"],
            )
            self.assertEqual(
                dev_status_read.item_status("ready-one", items_path=self.items_file),
                "open",
            )

    def test_ready_items_use_dashboard_effective_blocker_semantics(self):
        self.write_items(
            [
                make_item("done-blocker", status="done"),
                make_item("ready-plain"),
                make_item("ready-done-blocker", blocked_by=["done-blocker"]),
                make_item("blocked-active", blocked_by=["ready-plain"]),
                make_item("blocked-missing", blocked_by=["missing-item"]),
                make_item("cycle-one", blocked_by=["cycle-two"]),
                make_item("cycle-two", blocked_by=["cycle-one"]),
                make_item("progress-one", status="in-progress"),
            ]
        )
        import dev_status_read

        ready = dev_status_read.ready_items(items_path=self.items_file)
        self.assertEqual([item["id"] for item in ready], ["ready-plain", "ready-done-blocker"])

    def test_query_filters_status_and_prefix(self):
        self.write_items(
            [
                make_item("atk-one"),
                make_item("meta-one"),
                make_item("atk-progress", status="in-progress"),
            ]
        )
        import dev_status_read

        query = dev_status_read.BacklogQuery(prefix="atk-")
        self.assertEqual(
            [item["id"] for item in dev_status_read.ready_items(query, items_path=self.items_file)],
            ["atk-one"],
        )
        no_ready = dev_status_read.BacklogQuery(status="in-progress")
        self.assertEqual(dev_status_read.ready_items(no_ready, items_path=self.items_file), [])

    def test_get_item_status_and_numeric_string_are_exact_slug_only(self):
        self.write_items([make_item("123-item"), make_item("normal-item")])
        import dev_status_read

        self.assertIsNone(dev_status_read.get_item("1", items_path=self.items_file))
        self.assertIsNone(dev_status_read.item_status("1", items_path=self.items_file))
        self.assertEqual(
            dev_status_read.get_item("123-item", items_path=self.items_file)["id"],
            "123-item",
        )

    def test_claim_lookup_uses_one_snapshot_and_protocol_shape(self):
        self.write_items(
            [
                make_item(
                    "claimed-item",
                    status="in-progress",
                    claim={
                        "machine_id": "machine-a",
                        "owner_pid": "42",
                        "pid": "7",
                        "last_active": "2026-01-01T00:00:00+00:00",
                    },
                )
            ]
        )
        import dev_status_read

        lookup = dev_status_read.DevStatusClaimLookup(self.items_file)
        self.assertIsInstance(lookup, backlog_claim_lookup.BacklogClaimLookup)

        self.write_items([make_item("replacement-item")])

        self.assertEqual([i["id"] for i in lookup.in_progress_items()], ["claimed-item"])
        claim = lookup.claim_info("claimed-item")
        self.assertEqual(claim, backlog_claim_lookup.ClaimInfo(
            machine_id="machine-a",
            owner_pid=42,
            pid=7,
            last_active="2026-01-01T00:00:00+00:00",
            claimed_at=None,
        ))
        self.assertEqual(lookup.ready_items(), [])

    def test_standalone_functions_take_fresh_snapshots(self):
        self.write_items([make_item("first-item")])
        import dev_status_read

        self.assertEqual(
            dev_status_read.item_status("first-item", items_path=self.items_file), "open"
        )
        self.write_items([make_item("second-item", status="in-progress")])
        self.assertIsNone(
            dev_status_read.item_status("first-item", items_path=self.items_file)
        )
        self.assertEqual(
            dev_status_read.item_status("second-item", items_path=self.items_file),
            "in-progress",
        )


if __name__ == "__main__":
    unittest.main()
