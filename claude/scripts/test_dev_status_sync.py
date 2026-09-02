#!/usr/bin/env python3
"""Tests for dev_status_sync.py. Run with: python3 test_dev_status_sync.py"""

import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import dev_status
import dev_status_sync as sync


def make_item(
    slug,
    status="open",
    blocked_by=None,
    updated="2026-01-01",
    summary=None,
    context="",
    next_steps="",
    created="2026-01-01",
    related_files=None,
    review_content_hash=None,
):
    item = {
        "id": slug,
        "created": created,
        "updated": updated,
        "status": status,
        "summary": summary or f"Summary of {slug}",
        "category": "feature",
        "blocked_by": blocked_by or [],
        "related_files": related_files if related_files is not None else [],
        "context": context,
        "next_steps": next_steps,
    }
    if review_content_hash is not None:
        item["review_content_hash"] = review_content_hash
    return item


def make_pending(slug, status="waiting_for_reply", updated="2026-01-01", blocking=None):
    return {
        "id": slug,
        "created": "2026-01-01",
        "updated": updated,
        "status": status,
        "description": f"Description of {slug}",
        "kind": "email",
        "source_ref": {},
        "context": "",
        "next_steps": [],
        "blocking": blocking or [],
        "outcome": None,
    }


class SyncTestCase(unittest.TestCase):
    """Base fixture: an isolated tempdir standing in for ~/.claude/data/backlog."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self.tmpdir) / "backlog"
        self.items_file = self.data_dir / "items.json"
        self.pending_file = self.data_dir / "pending_items.json"
        self.meta_file = self.data_dir / "_meta.json"
        self.lock_file = self.data_dir / ".backlog.lock"
        self.sync_base_file = self.data_dir / "_sync-base.json"
        self.conflict_log_file = self.data_dir / "_sync-conflicts.jsonl"
        self.journal_file = self.data_dir / "journal.jsonl"
        self.runs_file = self.data_dir / "runs.jsonl"
        self.machine_id_file = self.data_dir / "_machine_id"
        self._patches = [
            patch.object(dev_status, "DATA_DIR", self.data_dir),
            patch.object(dev_status, "ITEMS_FILE", self.items_file),
            patch.object(dev_status, "PENDING_FILE", self.pending_file),
            patch.object(dev_status, "META_FILE", self.meta_file),
            patch.object(dev_status, "LOCK_FILE", self.lock_file),
            patch.object(dev_status, "JOURNAL_FILE", self.journal_file),
            patch.object(dev_status, "RUNS_FILE", self.runs_file),
            patch.object(dev_status, "MACHINE_ID_FILE", self.machine_id_file),
            patch.object(sync, "SYNC_BASE_FILE", self.sync_base_file),
            patch.object(sync, "CONFLICT_LOG_FILE", self.conflict_log_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)


# ── equality / canonicalization / winner selection ────────────────────────────


class EqualityBasisTests(unittest.TestCase):
    def test_equal_ignore_updated_true_when_only_updated_differs(self):
        a = make_item("foo-bar", updated="2026-01-01")
        b = make_item("foo-bar", updated="2026-01-05")
        self.assertTrue(sync._equal_ignore_updated(a, b))

    def test_equal_ignore_updated_false_on_content_diff(self):
        a = make_item("foo-bar", status="open")
        b = make_item("foo-bar", status="in-progress")
        self.assertFalse(sync._equal_ignore_updated(a, b))

    def test_canonical_updated_is_max(self):
        a = make_item("foo-bar", updated="2026-01-01")
        b = make_item("foo-bar", updated="2026-01-09")
        self.assertEqual(sync._canonical_updated(a, b), "2026-01-09")
        self.assertEqual(sync._canonical_updated(b, a), "2026-01-09")

    def test_pick_winner_newer_updated_wins(self):
        local = make_item("foo-bar", updated="2026-01-01")
        remote = make_item("foo-bar", updated="2026-01-09")
        winner, side = sync._pick_winner(local, remote)
        self.assertEqual(side, "remote")
        self.assertIs(winner, remote)

    def test_pick_winner_same_day_tie_keeps_local(self):
        local = make_item("foo-bar", updated="2026-01-01", summary="local version")
        remote = make_item("foo-bar", updated="2026-01-01", summary="remote version")
        winner, side = sync._pick_winner(local, remote)
        self.assertEqual(side, "local")
        self.assertIs(winner, local)


# ── per-item merge: cases 0-6 ──────────────────────────────────────────────────


class MergeItemCaseTests(unittest.TestCase):
    """Table tests for the 3-way merge's seven cases (plus tie/conflict path)."""

    def test_case0_no_base_local_only(self):
        local = make_item("foo-bar")
        merged, conflict = sync.merge_item("foo-bar", None, local, None, "items")
        self.assertEqual(merged, local)
        self.assertIsNone(conflict)

    def test_case0_no_base_remote_only(self):
        remote = make_item("foo-bar")
        merged, conflict = sync.merge_item("foo-bar", None, None, remote, "items")
        self.assertEqual(merged, remote)
        self.assertIsNone(conflict)

    def test_case0_no_base_both_identical(self):
        local = make_item("foo-bar", updated="2026-01-01")
        remote = make_item("foo-bar", updated="2026-01-05")
        merged, conflict = sync.merge_item("foo-bar", None, local, remote, "items")
        self.assertIsNone(conflict)
        self.assertEqual(merged["updated"], "2026-01-05")  # canonicalized to max

    def test_case0_no_base_both_divergent_conflict_logged(self):
        local = make_item("foo-bar", updated="2026-01-01", summary="local wins (newer)")
        remote = make_item(
            "foo-bar", updated="2025-12-01", summary="remote loses (older)"
        )
        merged, conflict = sync.merge_item("foo-bar", None, local, remote, "items")
        self.assertEqual(merged["summary"], "local wins (newer)")
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["type"], "divergent")
        self.assertEqual(conflict["payload"]["winner"], "local")
        self.assertEqual(conflict["payload"]["local"], local)
        self.assertEqual(conflict["payload"]["remote"], remote)

    def test_case1_new_since_base_clean_add_local(self):
        # base=None here stands in for "base exists for other ids, but not this one" —
        # merge_item treats both "true first sync" and "id new since last sync" identically.
        local = make_item("foo-bar")
        merged, conflict = sync.merge_item("foo-bar", None, local, None, "items")
        self.assertEqual(merged, local)
        self.assertIsNone(conflict)

    def test_case2_both_equal_base_unchanged(self):
        base = make_item("foo-bar", updated="2026-01-01")
        local = make_item("foo-bar", updated="2026-01-01")
        remote = make_item("foo-bar", updated="2026-01-01")
        merged, conflict = sync.merge_item("foo-bar", base, local, remote, "items")
        self.assertEqual(merged, local)
        self.assertIsNone(conflict)

    def test_case3_local_changed_remote_unchanged_takes_local(self):
        base = make_item("foo-bar", status="open", updated="2026-01-01")
        local = make_item("foo-bar", status="in-progress", updated="2026-01-05")
        remote = make_item("foo-bar", status="open", updated="2026-01-01")
        merged, conflict = sync.merge_item("foo-bar", base, local, remote, "items")
        self.assertEqual(merged["status"], "in-progress")
        self.assertIsNone(conflict)

    def test_case3_remote_changed_local_unchanged_takes_remote(self):
        base = make_item("foo-bar", status="open", updated="2026-01-01")
        local = make_item("foo-bar", status="open", updated="2026-01-01")
        remote = make_item("foo-bar", status="in-review", updated="2026-01-05")
        merged, conflict = sync.merge_item("foo-bar", base, local, remote, "items")
        self.assertEqual(merged["status"], "in-review")
        self.assertIsNone(conflict)

    def test_case4_both_changed_identical_convergent_edit(self):
        base = make_item("foo-bar", status="open", updated="2026-01-01")
        local = make_item("foo-bar", status="done", updated="2026-01-03")
        remote = make_item("foo-bar", status="done", updated="2026-01-05")
        merged, conflict = sync.merge_item("foo-bar", base, local, remote, "items")
        self.assertEqual(merged["status"], "done")
        self.assertEqual(merged["updated"], "2026-01-05")  # canonicalized max
        self.assertIsNone(conflict)

    def test_case4_both_changed_divergent_conflict(self):
        base = make_item("foo-bar", status="open", updated="2026-01-01")
        local = make_item("foo-bar", status="in-progress", updated="2026-01-01")
        remote = make_item("foo-bar", status="done", updated="2026-01-09")
        merged, conflict = sync.merge_item("foo-bar", base, local, remote, "items")
        self.assertEqual(merged["status"], "done")  # remote newer, wins
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["type"], "divergent")
        self.assertEqual(conflict["payload"]["winner"], "remote")

    def test_case5_both_missing_dropped(self):
        base = make_item("foo-bar")
        merged, conflict = sync.merge_item("foo-bar", base, None, None, "items")
        self.assertIsNone(merged)
        self.assertIsNone(conflict)

    def test_case6_remote_deleted_local_unchanged_propagates_delete(self):
        base = make_item("foo-bar", updated="2026-01-01")
        local = make_item("foo-bar", updated="2026-01-01")
        merged, conflict = sync.merge_item("foo-bar", base, local, None, "items")
        self.assertIsNone(merged)
        self.assertIsNone(conflict)

    def test_case6_remote_deleted_local_changed_resurrects(self):
        base = make_item("foo-bar", status="open", updated="2026-01-01")
        local = make_item("foo-bar", status="in-progress", updated="2026-01-05")
        merged, conflict = sync.merge_item("foo-bar", base, local, None, "items")
        self.assertEqual(merged["status"], "in-progress")
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["type"], "resurrection")
        self.assertEqual(conflict["payload"]["deleted_by"], "remote")

    def test_case6_local_deleted_remote_unchanged_propagates_delete(self):
        base = make_item("foo-bar", updated="2026-01-01")
        remote = make_item("foo-bar", updated="2026-01-01")
        merged, conflict = sync.merge_item("foo-bar", base, None, remote, "items")
        self.assertIsNone(merged)
        self.assertIsNone(conflict)

    def test_case6_local_deleted_remote_changed_resurrects(self):
        base = make_item("foo-bar", status="open", updated="2026-01-01")
        remote = make_item("foo-bar", status="done", updated="2026-01-05")
        merged, conflict = sync.merge_item("foo-bar", base, None, remote, "items")
        self.assertEqual(merged["status"], "done")
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["type"], "resurrection")
        self.assertEqual(conflict["payload"]["deleted_by"], "local")

    def test_case1_divergent_add_both_sides_new_since_base(self):
        local = make_item("foo-bar", updated="2026-01-01", summary="local coined it")
        remote = make_item("foo-bar", updated="2026-01-09", summary="remote coined it")
        merged, conflict = sync.merge_item("foo-bar", None, local, remote, "items")
        self.assertEqual(merged["summary"], "remote coined it")
        self.assertEqual(conflict["type"], "divergent")


class MergeStoreTests(unittest.TestCase):
    def test_merge_store_deterministic_order_and_union(self):
        local = [make_item("aaa-one"), make_item("ccc-three")]
        remote = [make_item("bbb-two")]
        merged, conflicts = sync.merge_store(None, local, remote, "items")
        self.assertEqual([i["id"] for i in merged], ["aaa-one", "bbb-two", "ccc-three"])
        self.assertEqual(conflicts, [])

    def test_merge_store_pending_uses_same_algorithm(self):
        base = [make_pending("wait-one", updated="2026-01-01")]
        local = [
            make_pending("wait-one", status="reply_received", updated="2026-01-05")
        ]
        remote = [make_pending("wait-one", updated="2026-01-01")]
        merged, conflicts = sync.merge_store(base, local, remote, "pending_items")
        self.assertEqual(merged[0]["status"], "reply_received")
        self.assertEqual(conflicts, [])


# ── graph integrity: dangling blockers + cycle breaking ────────────────────────


class GraphIntegrityTests(SyncTestCase):
    def test_dangling_blocker_purged_via_compute_sync(self):
        base_items = [
            make_item("blocker-one"),
            make_item("dependent-one", blocked_by=["blocker-one"]),
        ]
        local_items = [
            make_item("dependent-one", blocked_by=["blocker-one"])
        ]  # local deleted blocker-one
        remote_items = [
            make_item("blocker-one"),
            make_item("dependent-one", blocked_by=["blocker-one"]),
        ]
        result = sync.compute_sync(base_items, [], local_items, [], remote_items, [])
        dependent = next(i for i in result.merged_items if i["id"] == "dependent-one")
        self.assertNotIn("blocker-one", dependent["blocked_by"])
        self.assertNotIn("blocker-one", [i["id"] for i in result.merged_items])

    def test_new_cycle_edge_dropped_and_logged(self):
        # base: a and b exist, no edges between them.
        base_items = [make_item("item-a"), make_item("item-b")]
        # local adds edge a blocked_by b; remote independently adds edge b blocked_by a -> cycle.
        local_items = [
            make_item("item-a", blocked_by=["item-b"], updated="2026-01-05"),
            make_item("item-b", updated="2026-01-01"),
        ]
        remote_items = [
            make_item("item-a", updated="2026-01-01"),
            make_item("item-b", blocked_by=["item-a"], updated="2026-01-05"),
        ]
        result = sync.compute_sync(base_items, [], local_items, [], remote_items, [])
        by_id = {i["id"]: i for i in result.merged_items}
        # One of the two new edges must have been accepted, the other dropped to avoid a cycle.
        total_edges = len(by_id["item-a"]["blocked_by"]) + len(
            by_id["item-b"]["blocked_by"]
        )
        self.assertEqual(total_edges, 1)
        cycle_conflicts = [c for c in result.conflicts if c["type"] == "cycle"]
        self.assertEqual(len(cycle_conflicts), 1)

    def test_inherited_base_edge_never_removed(self):
        base_items = [
            make_item("item-a", blocked_by=["item-b"]),
            make_item("item-b"),
        ]
        local_items = [
            make_item("item-a", blocked_by=["item-b"], updated="2026-01-05"),
            make_item("item-b", updated="2026-01-01"),
        ]
        remote_items = [
            make_item("item-a", blocked_by=["item-b"], updated="2026-01-01"),
            make_item("item-b", updated="2026-01-01"),
        ]
        result = sync.compute_sync(base_items, [], local_items, [], remote_items, [])
        by_id = {i["id"]: i for i in result.merged_items}
        self.assertEqual(by_id["item-a"]["blocked_by"], ["item-b"])
        self.assertEqual([c for c in result.conflicts if c["type"] == "cycle"], [])


class CrossPoolGuardTests(unittest.TestCase):
    def test_cross_pool_collision_raises_fatal(self):
        merged_items = [make_item("foo-bar")]
        merged_pending = [make_pending("foo-bar")]
        with self.assertRaises(sync.SyncFatalError):
            sync._check_cross_pool_uniqueness(merged_items, merged_pending)

    def test_no_collision_passes(self):
        merged_items = [make_item("foo-bar")]
        merged_pending = [make_pending("baz-qux")]
        sync._check_cross_pool_uniqueness(merged_items, merged_pending)  # no raise


# ── write-need flags ────────────────────────────────────────────────────────────


class WriteNeedTests(unittest.TestCase):
    def test_no_op_sync_needs_no_writes(self):
        items = [make_item("foo-bar")]
        result = sync.compute_sync(items, [], items, [], items, [])
        self.assertFalse(result.needs_local_items_write)
        self.assertFalse(result.needs_remote_items_write)

    def test_convergent_edit_with_stale_local_stamp_forces_local_write(self):
        base = [make_item("foo-bar", status="open", updated="2026-01-01")]
        # Both sides made the identical edit, but local's stamp is older.
        local = [make_item("foo-bar", status="done", updated="2026-01-01")]
        remote = [make_item("foo-bar", status="done", updated="2026-01-09")]
        result = sync.compute_sync(base, [], local, [], remote, [])
        self.assertTrue(result.needs_local_items_write)
        self.assertEqual(result.merged_items[0]["updated"], "2026-01-09")

    def test_remote_missing_change_needs_remote_write(self):
        local = [make_item("foo-bar")]
        result = sync.compute_sync(None, None, local, [], [], [])
        self.assertFalse(result.needs_local_items_write)
        self.assertTrue(result.needs_remote_items_write)


# ── related_files path mapping + hash recompute ────────────────────────────────


class PathMappingTests(unittest.TestCase):
    def test_rewrite_prefix_swap(self):
        item = make_item(
            "foo-bar",
            related_files=[{"path": "/home/theon/dotfiles/x.py", "note": "n"}],
        )
        rewritten = sync.rewrite_related_files_paths(item, "/home/theon", "/home/yanil")
        self.assertEqual(
            rewritten["related_files"][0]["path"], "/home/yanil/dotfiles/x.py"
        )

    def test_non_matching_path_untouched(self):
        item = make_item(
            "foo-bar", related_files=[{"path": "/mnt/c/Users/yanil/x.py", "note": "n"}]
        )
        rewritten = sync.rewrite_related_files_paths(item, "/home/theon", "/home/yanil")
        self.assertIs(rewritten, item)  # no change, same object returned

    def test_round_trip_is_identity(self):
        item = make_item(
            "foo-bar", related_files=[{"path": "/home/yanil/dotfiles/x.py"}]
        )
        there = sync.rewrite_related_files_paths(item, "/home/yanil", "/home/theon")
        back = sync.rewrite_related_files_paths(there, "/home/theon", "/home/yanil")
        self.assertEqual(back["related_files"], item["related_files"])

    def test_review_content_hash_recomputed_on_rewrite(self):
        item = make_item(
            "foo-bar",
            related_files=[{"path": "/home/theon/x.py"}],
            review_content_hash="stale-hash-value",
        )
        rewritten = sync.rewrite_related_files_paths(item, "/home/theon", "/home/yanil")
        expected = dev_status._content_hash(rewritten)
        self.assertEqual(rewritten["review_content_hash"], expected)
        self.assertNotEqual(rewritten["review_content_hash"], "stale-hash-value")

    def test_rewrite_paths_list_maps_every_item(self):
        items = [
            make_item("a", related_files=[{"path": "/home/theon/a.py"}]),
            make_item("b", related_files=[{"path": "/home/theon/b.py"}]),
        ]
        out = sync.rewrite_paths_list(items, "/home/theon", "/home/yanil")
        self.assertEqual(out[0]["related_files"][0]["path"], "/home/yanil/a.py")
        self.assertEqual(out[1]["related_files"][0]["path"], "/home/yanil/b.py")


# ── _sync-base.json: load/save round trip, schema fallback, corrupt handling ──


class SyncBaseTests(SyncTestCase):
    def test_missing_base_returns_none_none(self):
        items, pending = sync.load_sync_base({"items": 2, "pending_items": 1})
        self.assertIsNone(items)
        self.assertIsNone(pending)

    def test_round_trip(self):
        schema = {"items": 2, "pending_items": 1}
        items = [make_item("foo-bar")]
        pending = [make_pending("baz-qux")]
        sync.save_sync_base(schema, items, pending)
        loaded_items, loaded_pending = sync.load_sync_base(schema)
        self.assertEqual(loaded_items, items)
        self.assertEqual(loaded_pending, pending)

    def test_schema_mismatch_falls_back_to_none_per_store(self):
        sync.save_sync_base(
            {"items": 2, "pending_items": 1}, [make_item("foo-bar")], []
        )
        # local items schema upgraded to 3; pending schema still matches.
        items, pending = sync.load_sync_base({"items": 3, "pending_items": 1})
        self.assertIsNone(items)
        self.assertEqual(pending, [])

    def test_corrupt_base_file_is_fatal(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sync_base_file.write_text("{not valid json")
        with self.assertRaises(sync.SyncFatalError):
            sync.load_sync_base({"items": 2, "pending_items": 1})


# ── local_commit: three independently-conditioned writes ───────────────────────


class LocalCommitTests(SyncTestCase):
    def test_local_wins_still_flushes_conflict_log(self):
        """The modal conflict shape (local wins) must not suppress the conflict-log flush."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        local_items = [make_item("foo-bar", status="in-progress", updated="2026-01-09")]
        dev_status.save_items(local_items)
        result = sync.SyncComputation(
            merged_items=local_items,  # merge result == local's pre-merge content (local won)
            merged_pending=[],
            conflicts=[
                sync._make_conflict(
                    "divergent",
                    "items",
                    "foo-bar",
                    {"local": local_items[0], "remote": {}, "winner": "local"},
                )
            ],
            needs_local_items_write=False,  # local already matches the merge result
            needs_local_pending_write=False,
            needs_remote_items_write=True,
            needs_remote_pending_write=False,
        )
        new_rev = sync.local_commit(
            {"items": 2, "pending_items": 1}, result, local_items, [], local_items, []
        )
        self.assertIsNone(new_rev)  # no local content write -> no rev bump
        self.assertTrue(self.conflict_log_file.exists())
        lines = self.conflict_log_file.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["item_id"], "foo-bar")

    def test_needs_write_bumps_rev_and_writes_items(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        dev_status.save_items([make_item("foo-bar", status="open")])
        merged = [make_item("foo-bar", status="done")]
        result = sync.SyncComputation(
            merged_items=merged,
            merged_pending=[],
            conflicts=[],
            needs_local_items_write=True,
            needs_local_pending_write=False,
            needs_remote_items_write=False,
            needs_remote_pending_write=False,
        )
        new_rev = sync.local_commit(
            {"items": 2, "pending_items": 1},
            result,
            [make_item("foo-bar", status="open")],
            [],
            None,
            None,
        )
        self.assertEqual(new_rev, 1)
        self.assertEqual(dev_status.load_items()[0]["status"], "done")

    def test_base_written_even_when_local_already_matches(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        merged = [make_item("foo-bar", status="done")]
        result = sync.SyncComputation(
            merged_items=merged,
            merged_pending=[],
            conflicts=[],
            needs_local_items_write=False,
            needs_local_pending_write=False,
            needs_remote_items_write=True,
            needs_remote_pending_write=False,
        )
        sync.local_commit(
            {"items": 2, "pending_items": 1}, result, merged, [], None, None
        )
        self.assertTrue(self.sync_base_file.exists())

    def test_genuinely_no_op_sync_writes_nothing(self):
        items = [make_item("foo-bar")]
        result = sync.SyncComputation(
            merged_items=items,
            merged_pending=[],
            conflicts=[],
            needs_local_items_write=False,
            needs_local_pending_write=False,
            needs_remote_items_write=False,
            needs_remote_pending_write=False,
        )
        new_rev = sync.local_commit(
            {"items": 2, "pending_items": 1}, result, items, [], items, []
        )
        self.assertIsNone(new_rev)
        self.assertFalse(self.sync_base_file.exists())
        self.assertFalse(self.conflict_log_file.exists())

    def test_local_write_appends_one_sync_journal_event(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        dev_status.save_items([make_item("foo-bar", status="open")])
        merged = [make_item("foo-bar", status="done")]
        result = sync.SyncComputation(
            merged_items=merged,
            merged_pending=[],
            conflicts=[],
            needs_local_items_write=True,
            needs_local_pending_write=False,
            needs_remote_items_write=False,
            needs_remote_pending_write=False,
        )
        new_rev = sync.local_commit(
            {"items": 2, "pending_items": 1},
            result,
            [make_item("foo-bar", status="open")],
            [],
            None,
            None,
            "fedora",
        )
        self.assertTrue(self.journal_file.exists())
        lines = self.journal_file.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["cmd"], "sync")
        self.assertEqual(entry["kind"], "sync")
        self.assertEqual(entry["rev"], new_rev)
        self.assertIn("fedora", entry["summary"])

    def test_no_op_sync_appends_no_journal_event(self):
        items = [make_item("foo-bar")]
        result = sync.SyncComputation(
            merged_items=items,
            merged_pending=[],
            conflicts=[],
            needs_local_items_write=False,
            needs_local_pending_write=False,
            needs_remote_items_write=False,
            needs_remote_pending_write=False,
        )
        sync.local_commit(
            {"items": 2, "pending_items": 1}, result, items, [], items, [], "fedora"
        )
        self.assertFalse(self.journal_file.exists())


# ── framed JSON (stdout/stdin sentinel markers) ────────────────────────────────


class FramedJsonTests(unittest.TestCase):
    def test_round_trip(self):
        payload = {"protocol_version": 1, "items": [1, 2, 3]}
        import secrets

        nonce = secrets.token_hex(8)
        text = (
            f"some shell noise from .zshenv\n"
            f"==={sync._FRAME_MARK}_START:{nonce}===\n"
            f"{json.dumps(payload)}\n"
            f"==={sync._FRAME_MARK}_END:{nonce}===\n"
            f"trailing noise\n"
        )
        extracted = sync._extract_framed_json(text)
        self.assertEqual(extracted, payload)

    def test_missing_markers_is_fatal(self):
        with self.assertRaises(sync.SyncFatalError):
            sync._extract_framed_json("just some plain output, no markers")

    def test_malformed_json_inside_markers_is_fatal(self):
        text = f"==={sync._FRAME_MARK}_START:abc===\nnot json\n==={sync._FRAME_MARK}_END:abc===\n"
        with self.assertRaises(sync.SyncFatalError):
            sync._extract_framed_json(text)


# ── protocol / schema guards ────────────────────────────────────────────────────


class GuardTests(unittest.TestCase):
    def test_protocol_version_mismatch_is_fatal(self):
        with self.assertRaises(sync.SyncFatalError):
            sync._check_protocol_version({"protocol_version": 99})

    def test_protocol_version_match_passes(self):
        sync._check_protocol_version(
            {"protocol_version": sync.PROTOCOL_VERSION}
        )  # no raise

    def test_schema_mismatch_is_fatal(self):
        with self.assertRaises(sync.SyncFatalError):
            sync._check_schema_versions(
                {"items": 2, "pending_items": 1}, {"items": 3, "pending_items": 1}
            )

    def test_schema_match_passes(self):
        sync._check_schema_versions(
            {"items": 2, "pending_items": 1}, {"items": 2, "pending_items": 1}
        )  # no raise

    def test_missing_store_on_one_side_is_not_a_mismatch(self):
        # A None schema_version means that store's file doesn't exist yet on
        # that machine (e.g. no pending item ever created there) — not a
        # real cross-machine disagreement. Must not raise.
        sync._check_schema_versions(
            {"items": 2, "pending_items": 1}, {"items": 2, "pending_items": None}
        )
        sync._check_schema_versions(
            {"items": 2, "pending_items": None}, {"items": 2, "pending_items": 1}
        )

    def test_real_mismatch_still_caught_when_other_store_is_missing(self):
        with self.assertRaises(sync.SyncFatalError):
            sync._check_schema_versions(
                {"items": 2, "pending_items": None}, {"items": 3, "pending_items": None}
            )


# ── local lock (timeout + mutual exclusion) ────────────────────────────────────


class LocalLockTests(SyncTestCase):
    def test_lock_acquired_and_released(self):
        with sync.local_lock(timeout=1.0):
            pass
        # A second acquisition after release must succeed promptly.
        with sync.local_lock(timeout=1.0):
            pass

    def test_lock_timeout_raises_retryable(self):
        import fcntl

        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                with (
                    self.assertRaises(sync.SyncRetryableError),
                    sync.local_lock(timeout=0.3),
                ):
                    pass
            finally:
                fcntl.flock(holder, fcntl.LOCK_UN)


# ── export / import round trip (in-process, no SSH) ─────────────────────────────


class ExportImportTests(SyncTestCase):
    def _args(self, **overrides):

        ns = argparse.Namespace(
            lock_timeout=5.0,
            if_rev=None,
        )
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def test_export_then_import_round_trip(self):
        import io as _io

        self.data_dir.mkdir(parents=True, exist_ok=True)
        dev_status.save_items([make_item("foo-bar")])
        dev_status.save_pending([make_pending("baz-qux")])

        captured = _io.StringIO()
        with patch("sys.stdout", captured):
            sync.cmd_export(self._args())
        exported = sync._extract_framed_json(captured.getvalue())
        self.assertEqual(exported["items"][0]["id"], "foo-bar")
        self.assertEqual(exported["rev"], 0)

        # Modify the exported payload and import it back with a matching --if-rev.
        exported["items"][0]["status"] = "done"
        nonce = "deadbeef"
        framed = (
            f"==={sync._FRAME_MARK}_START:{nonce}===\n"
            f"{json.dumps(exported)}\n"
            f"==={sync._FRAME_MARK}_END:{nonce}===\n"
        )
        with patch("sys.stdin", _io.StringIO(framed)):
            sync.cmd_import(self._args(if_rev=0))
        self.assertEqual(dev_status.load_items()[0]["status"], "done")
        self.assertEqual(dev_status.load_rev(), 1)

    def test_import_stale_rev_is_retryable(self):
        import io as _io

        self.data_dir.mkdir(parents=True, exist_ok=True)
        dev_status.save_items([make_item("foo-bar")])
        dev_status.save_pending([])
        payload = {
            "protocol_version": sync.PROTOCOL_VERSION,
            "schema_version": {"items": 2, "pending_items": 1},
            "items": [make_item("foo-bar", status="done")],
            "pending_items": [],
        }
        framed = (
            f"==={sync._FRAME_MARK}_START:abc===\n"
            f"{json.dumps(payload)}\n"
            f"==={sync._FRAME_MARK}_END:abc===\n"
        )
        with (
            patch("sys.stdin", _io.StringIO(framed)),
            self.assertRaises(sync.SyncRetryableError),
        ):
            sync.cmd_import(self._args(if_rev=99))


class MergeRunsTests(unittest.TestCase):
    def test_union_dedupes_and_preserves_local_order(self):
        a = {"run_id": "a", "item": "x"}
        b = {"run_id": "b", "item": "x"}
        c = {"run_id": "c", "item": "y"}
        self.assertEqual(sync.merge_runs([a, b], [c, a]), [a, b, c])

    def test_both_empty(self):
        self.assertEqual(sync.merge_runs([], []), [])

    def test_malformed_rows_dropped(self):
        good = {"run_id": "a", "item": "x"}
        merged = sync.merge_runs([good, "garbage", {"item": "x"}, {"run_id": ""}], [])
        self.assertEqual(merged, [good])


class RunsSyncTests(SyncTestCase):
    """runs.jsonl rides along the export/import payload (union-by-run_id)."""

    def _args(self, **overrides):
        ns = argparse.Namespace(lock_timeout=5.0, if_rev=None)
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def _seed_run(self, run_id="aaaa", item="foo-bar"):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        dev_status.append_run_record(
            {
                "run_id": run_id,
                "item": item,
                "command": "pytest -q",
                "exit_code": 0,
                "timed_out": False,
                "started_at": "2026-09-01T18:00:00+00:00",
                "duration_s": 1.0,
                "cwd": "/tmp",
            }
        )

    def _read_runs(self):
        return dev_status.load_runs()

    def test_export_includes_runs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._seed_run(run_id="local-one")

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync.cmd_export(self._args())
        exported = sync._extract_framed_json(captured.getvalue())
        self.assertEqual([r["run_id"] for r in exported["runs"]], ["local-one"])

    def test_import_writes_merged_runs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._seed_run(run_id="local-one")
        payload = {
            "protocol_version": sync.PROTOCOL_VERSION,
            "schema_version": {"items": 2, "pending_items": 1},
            "items": [],
            "pending_items": [],
            "runs": [
                {
                    "run_id": "remote-one",
                    "item": "foo-bar",
                    "command": "uv run pytest",
                    "exit_code": 0,
                    "timed_out": False,
                    "started_at": "2026-09-01T19:00:00+00:00",
                    "duration_s": 2.0,
                    "cwd": "/home/other",
                }
            ],
        }
        nonce = "abcdef01"
        framed = (
            f"==={sync._FRAME_MARK}_START:{nonce}===\n"
            f"{json.dumps(payload)}\n"
            f"==={sync._FRAME_MARK}_END:{nonce}===\n"
        )
        with patch("sys.stdin", io.StringIO(framed)):
            sync.cmd_import(self._args(if_rev=0))
        run_ids = [r["run_id"] for r in self._read_runs()]
        self.assertEqual(run_ids, ["local-one", "remote-one"])

    def test_import_duplicate_run_id_not_duplicated(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._seed_run(run_id="local-one")
        payload = {
            "protocol_version": sync.PROTOCOL_VERSION,
            "schema_version": {"items": 2, "pending_items": 1},
            "items": [],
            "pending_items": [],
            "runs": [dict(self._read_runs()[0])],
        }
        nonce = "abcdef02"
        framed = (
            f"==={sync._FRAME_MARK}_START:{nonce}===\n"
            f"{json.dumps(payload)}\n"
            f"==={sync._FRAME_MARK}_END:{nonce}===\n"
        )
        with patch("sys.stdin", io.StringIO(framed)):
            sync.cmd_import(self._args(if_rev=0))
        self.assertEqual(len(self._read_runs()), 1)

    def test_compute_sync_flags_runs_write_needs(self):
        local_only = [{"run_id": "l", "item": "x"}]
        remote_only = [{"run_id": "r", "item": "x"}]
        result = sync.compute_sync(
            None,
            None,
            [],
            [],
            [],
            [],
            local_runs=local_only,
            remote_runs=remote_only,
        )
        self.assertEqual(result.merged_runs, local_only + remote_only)
        self.assertTrue(result.needs_remote_runs_write)
        self.assertTrue(result.needs_local_runs_write)

        same = [{"run_id": "l", "item": "x"}]
        result2 = sync.compute_sync(
            None, None, [], [], [], [], local_runs=same, remote_runs=same
        )
        self.assertFalse(result2.needs_local_runs_write)
        self.assertFalse(result2.needs_remote_runs_write)

    def test_local_commit_writes_merged_runs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._seed_run(run_id="local-one")
        result = sync.SyncComputation(
            merged_items=[],
            merged_pending=[],
            merged_runs=[
                *self._read_runs(),
                {"run_id": "remote-one", "item": "foo-bar"},
            ],
            needs_local_runs_write=True,
        )
        sync.local_commit({}, result, [], [], None, None)
        run_ids = [r["run_id"] for r in self._read_runs()]
        self.assertEqual(run_ids, ["local-one", "remote-one"])


class ParserVerbosityTests(unittest.TestCase):
    def test_flags_parse_after_every_leaf_subcommand(self):
        # A leaf added later without an entry here silently loses coverage.
        cases = {
            "sync": [],
            "status": [],
            "export": [],
            "import": ["--if-rev", "0"],
        }
        for cmd, extra in cases.items():
            args = sync.build_parser().parse_args([cmd, "-q", *extra])
            self.assertTrue(args.quiet)
            self.assertFalse(args.verbose)

    def test_rejects_quiet_and_verbose_together(self):
        with self.assertRaises(SystemExit):
            sync.build_parser().parse_args(["status", "-q", "-v"])


# ── artifact collection (grill/ scope only) ───────────────────────────────────


class ArtifactCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "home"
        self.grill = self.home / ".claude" / "data" / "grill"
        self.grill.mkdir(parents=True)
        self.spec = self.grill / "spec.md"
        self.spec.write_text("spec")
        self.vitals = self.grill / "vitals"
        self.vitals.mkdir()
        self.proj = self.home / "proj"
        self.proj.mkdir()
        (self.proj / "db.ts").write_text("x")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _item(self, path):
        return make_item("foo", related_files=[{"path": path}])

    def test_grill_file_collected(self):
        out = sync.collect_artifact_paths([self._item(str(self.spec))], str(self.home))
        self.assertEqual(out, [self.spec])

    def test_project_source_excluded(self):
        out = sync.collect_artifact_paths(
            [self._item(str(self.proj / "db.ts"))], str(self.home)
        )
        self.assertEqual(out, [])

    def test_mixed_store_majority_non_grill(self):
        items = [self._item(str(self.proj / f"src{i}.py")) for i in range(10)]
        items.append(self._item(str(self.spec)))
        out = sync.collect_artifact_paths(items, str(self.home))
        self.assertEqual(out, [self.spec])

    def test_dedupe_same_path_in_multiple_items(self):
        out = sync.collect_artifact_paths(
            [self._item(str(self.spec)), self._item(str(self.spec))], str(self.home)
        )
        self.assertEqual(out, [self.spec])

    def test_dotdot_escape_skipped(self):
        escape = str(self.grill / ".." / "db.ts")
        out = sync.collect_artifact_paths([self._item(escape)], str(self.home))
        self.assertEqual(out, [])

    def test_symlink_entry_skipped(self):
        target = self.proj / "secret.md"
        target.write_text("s")
        link = self.grill / "link.md"
        link.symlink_to(target)
        out = sync.collect_artifact_paths([self._item(str(link))], str(self.home))
        self.assertEqual(out, [])

    def test_grill_dir_self_skipped(self):
        out = sync.collect_artifact_paths([self._item(str(self.grill))], str(self.home))
        self.assertEqual(out, [])

    def test_directory_under_grill_allowed(self):
        out = sync.collect_artifact_paths(
            [self._item(str(self.vitals))], str(self.home)
        )
        self.assertEqual(out, [self.vitals])

    def test_malformed_entries_skipped(self):
        item = {"id": "foo", "related_files": ["notadict", {"note": "nopath"}, 5]}
        out = sync.collect_artifact_paths([item], str(self.home))
        self.assertEqual(out, [])


class ArtifactContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "home"
        self.grill = self.home / ".claude" / "data" / "grill"
        self.grill.mkdir(parents=True)
        self.spec = self.grill / "spec.md"
        self.spec.write_text("spec")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_pass_when_grill_path_present_and_collected(self):
        merged = [make_item("foo", related_files=[{"path": str(self.spec)}])]
        sync.assert_artifact_contract(merged, str(self.home))  # no raise

    def test_fail_when_local_form_grill_but_not_collected(self):
        # starts with grill prefix but absent locally -> collect returns empty
        merged = [
            make_item("foo", related_files=[{"path": str(self.grill / "missing.md")}])
        ]
        with self.assertRaises(AssertionError):
            sync.assert_artifact_contract(merged, str(self.home))

    def test_no_fail_when_only_non_grill_paths(self):
        merged = [
            make_item("foo", related_files=[{"path": str(self.home / "proj" / "x.py")}])
        ]
        sync.assert_artifact_contract(merged, str(self.home))  # no raise


class ArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "home"
        self.grill = self.home / ".claude" / "data" / "grill"
        self.grill.mkdir(parents=True)
        self.spec = self.grill / "spec.md"
        self.spec.write_text("spec")
        self.remote = Path(self.tmp) / "remote"
        self.remote.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _item(self):
        return make_item("foo", related_files=[{"path": str(self.spec)}])

    def test_push_argv_and_flags(self):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(None, 0, b"", b"")
            attempted, failed = sync.push_artifacts(
                "fedora",
                [self._item()],
                str(self.home),
                str(self.remote),
                20.0,
                20.0,
                quiet=False,
                dry_run=False,
            )
        self.assertEqual(attempted, 1)
        self.assertEqual(failed, 0)
        argv = run.call_args[0][0]
        self.assertIn("-rptDuR", argv)  # -u is folded into the combined flag
        tidx = argv.index("--timeout")
        self.assertEqual(argv[tidx + 1], "20")  # integer, never "20.0"
        dests = [a for a in argv if a.startswith("fedora:") and a.endswith("/")]
        self.assertEqual(len(dests), 1)
        self.assertTrue(any("/./" in a for a in argv))

    def test_pull_argv_and_flags(self):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(None, 0, b"", b"")
            attempted, failed = sync.pull_artifacts(
                "fedora",
                [self._item()],
                str(self.home),
                str(self.remote),
                20.0,
                20.0,
                quiet=False,
                dry_run=False,
            )
        argv = run.call_args[0][0]
        self.assertIn("-rptDuR", argv)  # -u is folded into the combined flag
        self.assertTrue(any(a.startswith("fedora:") for a in argv))
        self.assertIn(str(self.home) + "/", argv)

    def test_dry_run_no_rsync(self):
        with patch("subprocess.run") as run:
            attempted, failed = sync.push_artifacts(
                "fedora",
                [self._item()],
                str(self.home),
                str(self.remote),
                20.0,
                20.0,
                quiet=False,
                dry_run=True,
            )
        self.assertEqual(attempted, 1)
        run.assert_not_called()

    def test_missing_local_source_dropped(self):
        miss = make_item("foo", related_files=[{"path": str(self.grill / "ghost.md")}])
        with patch("subprocess.run") as run:
            attempted, failed = sync.push_artifacts(
                "fedora",
                [miss],
                str(self.home),
                str(self.remote),
                20.0,
                20.0,
                quiet=False,
                dry_run=False,
            )
        self.assertEqual(attempted, 0)
        run.assert_not_called()

    def test_nonzero_rsync_is_fatal_push(self):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(None, 23, b"", b"err")
            with self.assertRaises(sync.SyncFatalError):
                sync.push_artifacts(
                    "fedora",
                    [self._item()],
                    str(self.home),
                    str(self.remote),
                    20.0,
                    20.0,
                    quiet=False,
                    dry_run=False,
                )

    def test_nonzero_rsync_is_fatal_pull(self):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(None, 23, b"", b"err")
            with self.assertRaises(sync.SyncFatalError):
                sync.pull_artifacts(
                    "fedora",
                    [self._item()],
                    str(self.home),
                    str(self.remote),
                    20.0,
                    20.0,
                    quiet=False,
                    dry_run=False,
                )

    def test_empty_set_no_rsync(self):
        proj = make_item(
            "foo", related_files=[{"path": str(self.home / "proj" / "x.py")}]
        )
        with patch("subprocess.run") as run:
            attempted, failed = sync.push_artifacts(
                "fedora",
                [proj],
                str(self.home),
                str(self.remote),
                20.0,
                20.0,
                quiet=False,
                dry_run=False,
            )
        self.assertEqual((attempted, failed), (0, 0))
        run.assert_not_called()

    def test_pull_invokes_rsync_even_if_local_exists(self):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(None, 0, b"", b"")
            attempted, failed = sync.pull_artifacts(
                "fedora",
                [self._item()],
                str(self.home),
                str(self.remote),
                20.0,
                20.0,
                quiet=False,
                dry_run=False,
            )
        self.assertEqual(attempted, 1)
        run.assert_called_once()


# ── cmd_sync artifact integration (rsync + ssh mocked) ───────────────────────


class ArtifactSyncIntegrationTests(SyncTestCase):
    def _home_paths(self):
        local_home = self.data_dir.parent / "localhome"
        remote_home = self.data_dir.parent / "remotehome"
        grill = local_home / ".claude" / "data" / "grill"
        grill.mkdir(parents=True)
        (grill / "spec.md").write_text("spec")
        return str(local_home), str(remote_home), str(grill / "spec.md")

    def _args(self, **kw):
        ns = argparse.Namespace(
            lock_timeout=5.0,
            host="fedora",
            remote_script="x",
            ssh_timeout=20.0,
            max_retries=1,
            user_map={"yanil": "/L", "theon": "/R"},
            local_user="yanil",
            remote_user="theon",
            dry_run=False,
            quiet=False,
            verbose=False,
            no_artifacts=False,
            rsync_io_timeout=None,
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def _payload(self, items, pending=None, rev=0):
        return {
            "protocol_version": sync.PROTOCOL_VERSION,
            "schema_version": {"items": 2, "pending_items": 1},
            "items": items,
            "pending_items": pending or [],
            "rev": rev,
        }

    def _seed_local(self, item):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        dev_status.save_items([item])

    def test_push_before_import_and_pull_before_commit(self):
        local_home, remote_home, spec = self._home_paths()
        # local wins the merge (newer updated) so a remote write is needed.
        local_item = make_item(
            "foo", updated="2026-03-01", related_files=[{"path": spec}]
        )
        remote_item = make_item(
            "foo", updated="2026-02-01", related_files=[{"path": spec}]
        )
        self._seed_local(local_item)

        order = []
        with (
            patch.object(
                sync, "ssh_export", lambda *a, **k: self._payload([remote_item])
            ),
            patch.object(sync, "ssh_import", lambda *a, **k: order.append("import")),
            patch.object(sync, "local_commit", lambda *a, **k: order.append("commit")),
            patch.object(sync, "remote_has_rsync", lambda *a, **k: True),
            patch(
                "subprocess.run",
                lambda argv, **k: (
                    order.append("rsync"),
                    subprocess.CompletedProcess(argv, 0, b"", b""),
                )[1],
            ),
        ):
            sync.cmd_sync(
                self._args(user_map={"yanil": local_home, "theon": remote_home})
            )

        rsync_idx = [i for i, x in enumerate(order) if x == "rsync"]
        self.assertEqual(len(rsync_idx), 2, order)
        push_idx, pull_idx = rsync_idx
        self.assertLess(push_idx, order.index("import"))
        self.assertLess(pull_idx, order.index("commit"))

    def test_push_failure_prevents_import(self):
        local_home, remote_home, spec = self._home_paths()
        item = make_item("foo", related_files=[{"path": spec}])
        self._seed_local(item)
        order = []

        def bad_run(argv, **k):
            return subprocess.CompletedProcess(argv, 23, b"", b"err")

        with (
            patch.object(sync, "ssh_export", lambda *a, **k: self._payload([item])),
            patch.object(sync, "ssh_import", lambda *a, **k: order.append("import")),
            patch.object(sync, "remote_has_rsync", lambda *a, **k: True),
            patch("subprocess.run", bad_run),
            self.assertRaises(sync.SyncFatalError),
        ):
            sync.cmd_sync(
                self._args(user_map={"yanil": local_home, "theon": remote_home})
            )
        self.assertNotIn("import", order)

    def test_artifacts_run_even_when_json_not_dirty(self):
        local_home, remote_home, spec = self._home_paths()
        item = make_item("foo", related_files=[{"path": spec}])
        self._seed_local(item)
        order = []

        with (
            patch.object(sync, "ssh_export", lambda *a, **k: self._payload([item])),
            patch.object(sync, "ssh_import", lambda *a, **k: order.append("import")),
            patch.object(sync, "remote_has_rsync", lambda *a, **k: True),
            patch(
                "subprocess.run",
                lambda argv, **k: (
                    order.append("rsync"),
                    subprocess.CompletedProcess(argv, 0, b"", b""),
                )[1],
            ),
        ):
            sync.cmd_sync(
                self._args(user_map={"yanil": local_home, "theon": remote_home})
            )
        # identical remote -> no remote JSON write, but artifacts still transfer
        self.assertNotIn("import", order)
        self.assertIn("rsync", order)

    def test_no_artifacts_skips_transfer(self):
        local_home, remote_home, spec = self._home_paths()
        item = make_item("foo", related_files=[{"path": spec}])
        self._seed_local(item)
        order = []

        with (
            patch.object(sync, "ssh_export", lambda *a, **k: self._payload([item])),
            patch.object(sync, "remote_has_rsync", lambda *a, **k: True),
            patch(
                "subprocess.run",
                lambda argv, **k: (
                    order.append("rsync"),
                    subprocess.CompletedProcess(argv, 0, b"", b""),
                )[1],
            ),
        ):
            sync.cmd_sync(
                self._args(
                    no_artifacts=True,
                    user_map={"yanil": local_home, "theon": remote_home},
                )
            )
        self.assertNotIn("rsync", order)

    def test_rsync_unavailable_push_is_fatal(self):
        local_home, remote_home, spec = self._home_paths()
        item = make_item("foo", related_files=[{"path": spec}])
        self._seed_local(item)

        with (
            patch.object(sync, "ssh_export", lambda *a, **k: self._payload([item])),
            patch("shutil.which", lambda name: None if name == "rsync" else "/bin/x"),
            self.assertRaises(sync.SyncFatalError),
        ):
            sync.cmd_sync(
                self._args(user_map={"yanil": local_home, "theon": remote_home})
            )

    def test_warn_nonlocal_fires_only_on_escape(self):
        local_home, remote_home, spec = self._home_paths()
        escape = str(Path(spec).parent / ".." / "secret.md")
        item = make_item("foo", related_files=[{"path": escape}])
        self._seed_local(item)

        import io

        buf = io.StringIO()
        with (
            patch.object(sync, "ssh_export", lambda *a, **k: self._payload([item])),
            patch.object(sync, "remote_has_rsync", lambda *a, **k: True),
            patch(
                "subprocess.run",
                lambda argv, **k: subprocess.CompletedProcess(argv, 0, b"", b""),
            ),
            patch("sys.stderr", buf),
        ):
            sync.cmd_sync(
                self._args(user_map={"yanil": local_home, "theon": remote_home})
            )
        self.assertIn("escaping grill dir", buf.getvalue())


class ArtifactPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "home"
        self.grill = self.home / ".claude" / "data" / "grill"
        self.grill.mkdir(parents=True)
        self.spec = self.grill / "spec.md"
        self.spec.write_text("spec")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_preview_lists_grill_artifact(self):
        import io

        buf = io.StringIO()
        merged = [make_item("foo", related_files=[{"path": str(self.spec)}])]
        with patch("sys.stdout", buf):
            sync.artifact_preview(merged, str(self.home), "/R", "fedora", quiet=False)
        out = buf.getvalue()
        self.assertIn("artifacts (1 file", out)
        self.assertIn(str(self.home) + "/./.claude/data/grill/spec.md", out)

    def test_preview_none_when_no_grill_paths(self):
        import io

        buf = io.StringIO()
        merged = [
            make_item("foo", related_files=[{"path": str(self.home / "proj" / "x.py")}])
        ]
        with patch("sys.stdout", buf):
            sync.artifact_preview(merged, str(self.home), "/R", "fedora", quiet=False)
        self.assertIn("artifacts: none", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
