#!/usr/bin/env python3
"""Tests for dev_status_storage.py. Run with: python3 test_dev_status_storage.py

Split out of test_dev_status.py (2026-09-18): dev_status_storage.py was
extracted from dev_status_impl.py as a standalone pure-persistence module
well before this file existed, but its direct unit tests stayed behind in
the mega test file. Everything else in test_dev_status.py exercises
dev_status_impl.py's CLI surface end-to-end (a characterization tier, not
unit tests of an extracted module), which is why it isn't also split by
this same rule -- see agent-scripts/AGENTS.md's "dev_status stack: module
map" for the pure-logic-vs-orchestration boundary that decides this.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import dev_status  # noqa: E402
import dev_status_storage  # noqa: E402


def make_item(slug, summary=None):
    return {
        "id": slug,
        "created": "2026-01-01",
        "updated": "2026-01-01",
        "status": "open",
        "summary": summary or f"Summary of {slug}",
        "category": "feature",
        "blocked_by": [],
        "related_files": [],
        "context": "",
        "next_steps": "",
    }


class BacklogStorageExtractionTestCase(unittest.TestCase):
    """Tests for the extracted dev_status_storage module and storage boundary."""

    def test_storage_module_import_and_exports(self):
        expected_exports = [
            "atomic_write_json",
            "load_items",
            "save_items",
            "load_pending",
            "save_pending",
            "load_rev",
            "bump_rev",
            "backlog_lock",
            "out_of_scope_lock",
            "append_journal_event",
            "read_journal_entries",
            "journal_entry",
            "parse_journal_ts",
            "journal_last_entry_within",
            "backup_before_bulk_delete",
            "load_runs",
            "write_runs_file",
            "append_run_record",
            "load_out_of_scope_index",
            "save_out_of_scope_index",
            "out_of_scope_md_path",
            "load_recap_cache",
            "save_recap_cache",
            "machine_id",
        ]
        for name in expected_exports:
            self.assertTrue(
                hasattr(dev_status_storage, name),
                f"dev_status_storage is missing expected export: {name}",
            )

    def test_storage_reexports_in_dev_status_impl(self):
        for name in (
            "load_items",
            "save_items",
            "load_pending",
            "save_pending",
            "load_rev",
            "bump_rev",
            "backlog_lock",
            "out_of_scope_lock",
            "append_journal_event",
            "read_journal_entries",
            "machine_id",
        ):
            self.assertTrue(
                hasattr(dev_status, name),
                f"dev_status (impl) missing backward-compatible export: {name}",
            )

    def test_patched_globals_affect_storage_load_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            custom_items = tmp_path / "custom_items.json"
            custom_items.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "items": [
                            make_item("storage-item-1", summary="from custom items")
                        ],
                    }
                )
            )
            with patch.object(dev_status, "ITEMS_FILE", custom_items):
                # Via dev_status
                items1 = dev_status.load_items()
                self.assertEqual([i["id"] for i in items1], ["storage-item-1"])
                # Via dev_status_storage default resolution
                items2 = dev_status_storage.load_items()
                self.assertEqual([i["id"] for i in items2], ["storage-item-1"])

                # Saving via storage updates the patched file
                items2.append(make_item("storage-item-2", summary="added"))
                dev_status_storage.save_items(items2)
                reloaded = dev_status.load_items()
                self.assertEqual(
                    [i["id"] for i in reloaded], ["storage-item-1", "storage-item-2"]
                )

    def test_storage_lock_reentrancy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lock_file = tmp_path / ".test.lock"
            # Same-thread nested acquisition should not block
            with dev_status_storage.backlog_lock(
                data_dir=tmp_path, lock_file=lock_file
            ), dev_status_storage.backlog_lock(
                data_dir=tmp_path, lock_file=lock_file
            ):
                pass


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
