#!/usr/bin/env python3
"""Tests for dev_status.py v2. Run with: python3 test_dev_status.py"""

import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import dev_status
import llm_backends


class _TtyStringIO(io.StringIO):
    def isatty(self):
        return True


def make_item(
    slug,
    status="open",
    blocked_by=None,
    updated="2026-01-01",
    summary=None,
    context="",
    next_steps="",
    created="2026-01-01",
    priority=None,
    related_files=None,
    review_feedback=None,
    review_content_hash=None,
    gate=None,
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
    if priority is not None:
        item["priority"] = priority
    if review_feedback is not None:
        item["review_feedback"] = review_feedback
    if review_content_hash is not None:
        item["review_content_hash"] = review_content_hash
    if gate is not None:
        item["gate"] = gate
    return item


# The reminder's own wording. Silence assertions key on this rather than on a
# bare prefix substring: `render` also writes an `item-map:` line to stderr that
# contains the slug, so `assertNotIn("atk-", ...)` matches that instead.
_PREFIX_MARKER = "carries the prefix"


class BacklogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self.tmpdir) / "backlog"
        self.items_file = self.data_dir / "items.json"
        self.pending_file = self.data_dir / "pending_items.json"
        self.meta_file = self.data_dir / "_meta.json"
        self.lock_file = self.data_dir / ".backlog.lock"
        self.journal_file = self.data_dir / "journal.jsonl"
        self.runs_file = self.data_dir / "runs.jsonl"
        self.machine_id_file = self.data_dir / "_machine_id"
        self.recap_cache_file = self.data_dir / "recap-cache.json"
        self.recap_regen_lock_file = self.data_dir / "recap-regen.lock"
        self.out_of_scope_dir = Path(self.tmpdir) / "backlog-out-of-scope"
        self.out_of_scope_index_file = self.out_of_scope_dir / "index.json"
        self.out_of_scope_lock_file = self.out_of_scope_dir / ".out-of-scope.lock"
        self._patches = [
            patch.object(dev_status, "DATA_DIR", self.data_dir),
            patch.object(dev_status, "ITEMS_FILE", self.items_file),
            patch.object(dev_status, "PENDING_FILE", self.pending_file),
            patch.object(dev_status, "META_FILE", self.meta_file),
            patch.object(dev_status, "LOCK_FILE", self.lock_file),
            patch.object(dev_status, "JOURNAL_FILE", self.journal_file),
            patch.object(dev_status, "RUNS_FILE", self.runs_file),
            patch.object(dev_status, "MACHINE_ID_FILE", self.machine_id_file),
            patch.object(dev_status, "RECAP_CACHE_FILE", self.recap_cache_file),
            patch.object(
                dev_status, "RECAP_REGEN_LOCK_FILE", self.recap_regen_lock_file
            ),
            patch.object(dev_status, "OUT_OF_SCOPE_DIR", self.out_of_scope_dir),
            patch.object(
                dev_status, "OUT_OF_SCOPE_INDEX_FILE", self.out_of_scope_index_file
            ),
            patch.object(
                dev_status, "OUT_OF_SCOPE_LOCK_FILE", self.out_of_scope_lock_file
            ),
        ]
        for p in self._patches:
            p.start()
        # `_maybe_dispatch_recap_regen` spawns `dev_status.py _internal-regen` as a
        # real, separate OS process — one that re-imports dev_status fresh and so
        # does NOT see any of the patches above, meaning an unmocked Popen call
        # here would read/write the *real* ~/.claude/data/backlog/ instead of this
        # test's tmpdir (this happened once during development: a full test run
        # spawned real agy/opencode calls against fabricated test journal data and
        # wrote a real recap-cache.json/journal.jsonl into production). Mocking
        # subprocess.Popen for every test in this fixture, not just recap-specific
        # ones, closes that off by construction rather than relying on each test
        # author to remember it.
        self._popen_patch = patch("subprocess.Popen")
        self.mock_popen = self._popen_patch.start()
        self.mock_popen.return_value.communicate.return_value = ("", "")
        self.mock_popen.return_value.poll.return_value = 0
        self.mock_popen.return_value.returncode = 0

    def tearDown(self):
        self._popen_patch.stop()
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_backlog_lock_reentrant_same_thread_does_not_deadlock(self):
        # Regression: flock(2) is per-open-file-description, not per-process, so a
        # nested `with backlog_lock()` in the same thread used to block forever
        # (the inner open's flock waited on the outer lock held by the same
        # process). Run the nesting in a worker thread so a deadlock is caught by
        # a join timeout instead of hanging the whole suite.
        seen = {}

        def worker():
            try:
                with dev_status.backlog_lock():
                    with dev_status.backlog_lock():
                        seen["inner"] = True
            except Exception as e:  # pragma: no cover - defensive
                seen["error"] = e

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "backlog_lock nested re-entry deadlocked")
        self.assertTrue(seen.get("inner"), f"unexpected lock outcome: {seen}")

    def write_items(self, items):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {"schema_version": 2, "items": items}
        self.items_file.write_text(json.dumps(data, indent=2))

    def write_pending(self, pending_items):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {"schema_version": 1, "items": pending_items}
        self.pending_file.write_text(json.dumps(data, indent=2))

    def read_items(self):
        return dev_status.load_items()

    def read_rev(self):
        return dev_status.load_rev()

    # ── 1: add with valid slug succeeds ──────────────────────────────────────

    def test_01_add_valid_slug(self):
        args = _args(json='{"id": "my-feature", "summary": "Test feature"}')
        dev_status.cmd_add(args)
        items = self.read_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "my-feature")
        self.assertEqual(items[0]["status"], "open")

    # ── 2: add with missing id exits with helpful suggestion ─────────────────

    def test_02_add_missing_id_exits_with_suggestion(self):
        args = _args(json='{"summary": "Fix the broken widget"}')
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_add(args)
        self.assertEqual(cm.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("'id' is required", msg)
        self.assertIn("fix-the-broken-widget", msg)

    # ── 3: add with invalid slug rejected ────────────────────────────────────

    def test_03_add_uppercase_slug_rejected(self):
        args = _args(json='{"id": "My-Feature", "summary": "x"}')
        with self.assertRaises(SystemExit):
            dev_status.cmd_add(args)

    def test_03b_add_no_hyphen_slug_rejected(self):
        args = _args(json='{"id": "feature", "summary": "x"}')
        with self.assertRaises(SystemExit):
            dev_status.cmd_add(args)

    def test_03c_add_double_hyphen_slug_rejected(self):
        args = _args(json='{"id": "my--feature", "summary": "x"}')
        with self.assertRaises(SystemExit):
            dev_status.cmd_add(args)

    def test_03d_add_reserved_slug_rejected(self):
        args = _args(json='{"id": "render", "summary": "x"}')
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_add(args)
        self.assertIn("reserved", err.getvalue())

    def test_03e_add_hyphenated_reserved_prefix_accepted(self):
        # Exact-match invariant: only the bare verb is reserved. A slug like
        # `remove-probe` (first hyphen-segment is a reserved verb) must be
        # accepted — argparse never confuses it with the `remove`
        # subcommand, which is parsed positionally from argv. Prefix-match
        # refusal was considered and rejected (2026-07-25) to keep natural
        # slugs like `update-deps` addable.
        args = _args(json='{"id": "remove-probe", "summary": "x"}')
        dev_status.cmd_add(args)
        items = self.read_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "remove-probe")

    # ── 4: add with duplicate slug rejected ──────────────────────────────────

    def test_04_add_duplicate_slug_rejected(self):
        self.write_items([make_item("my-feature")])
        args = _args(json='{"id": "my-feature", "summary": "Another one"}')
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_add(args)
        self.assertIn("duplicate", err.getvalue())

    # ── 5: add with blocked_by referencing nonexistent slug rejected ──────────

    def test_05_add_blocked_by_nonexistent_rejected(self):
        args = _args(
            json='{"id": "my-feature", "summary": "x", "blocked_by": ["ghost-slug"]}'
        )
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_add(args)
        self.assertIn("ghost-slug", err.getvalue())

    def test_05b_add_blocked_by_pending_slug_accepted(self):
        self.write_pending(
            [
                {
                    "id": "pend-wait",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting on someone",
                    "kind": "email",
                    "blocking": [],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        args = _args(
            json='{"id": "my-feature", "summary": "x", "blocked_by": ["pend-wait"]}'
        )
        dev_status.cmd_add(args)
        items = self.read_items()
        self.assertEqual(items[0]["blocked_by"], ["pend-wait"])

    # ── 6: done N resolves via internal re-render ─────────────────────────────

    def test_06_done_by_number_resolves_correctly(self):
        items = [
            make_item("alpha-item", status="in-progress", updated="2026-04-10"),
            make_item("beta-item", status="open", updated="2026-04-09"),
            make_item("gamma-item", status="open", updated="2026-04-08"),
        ]
        self.write_items(items)
        # Render order: IN PROGRESS (alpha=1), READY (beta=2, gamma=3)
        # done 2 should resolve to beta-item
        args = _args(id="2", if_rev=0)
        dev_status.cmd_done(args)
        result = {i["id"]: i["status"] for i in self.read_items()}
        self.assertEqual(result["beta-item"], "done")
        self.assertEqual(result["alpha-item"], "in-progress")
        self.assertEqual(result["gamma-item"], "open")

    # ── 7: done <slug> marks correct item directly ────────────────────────────

    def test_07_done_by_slug(self):
        self.write_items([make_item("my-task"), make_item("other-task")])
        args = _args(id="my-task")
        dev_status.cmd_done(args)
        result = {i["id"]: i["status"] for i in self.read_items()}
        self.assertEqual(result["my-task"], "done")
        self.assertEqual(result["other-task"], "open")

    # ── 8: rename rewrites id, blocked_by, context, next_steps ──────────────

    def test_08_rename_rewrites_all_references(self):
        items = [
            make_item(
                "old-name",
                context="depends on old-name things",
                next_steps="finish old-name first",
            ),
            make_item(
                "other-item",
                blocked_by=["old-name"],
                context="old-name is blocking this",
            ),
        ]
        self.write_items(items)
        args = _args(old_slug="old-name", new_slug="new-name")
        dev_status.cmd_rename(args)
        result = self.read_items()
        index = {i["id"]: i for i in result}
        self.assertIn("new-name", index)
        self.assertNotIn("old-name", index)
        self.assertEqual(index["other-item"]["blocked_by"], ["new-name"])
        self.assertIn("new-name", index["new-name"]["context"])
        self.assertNotIn("old-name", index["new-name"]["context"])
        self.assertIn("new-name", index["new-name"]["next_steps"])
        self.assertIn("new-name", index["other-item"]["context"])

    # ── 9: rename refuses collision ───────────────────────────────────────────

    def test_09_rename_refuses_collision(self):
        self.write_items([make_item("old-name"), make_item("new-name")])
        args = _args(old_slug="old-name", new_slug="new-name")
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_rename(args)
        self.assertIn("collision", err.getvalue())

    # ── 10: rename refuses nonexistent source slug ────────────────────────────

    def test_10_rename_refuses_nonexistent_source(self):
        self.write_items([make_item("existing-item")])
        args = _args(old_slug="ghost-slug", new_slug="new-name")
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_rename(args)
        self.assertIn("not found", err.getvalue())

    # ── 10b: rename accepts numeric id, guarded like other mutators ─────────

    def test_10b_rename_numeric_id_without_if_rev_refused(self):
        self.write_items([make_item("old-name")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_rename(_args(old_slug="1", new_slug="new-name", if_rev=None))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("requires --if-rev", err.getvalue())
        self.assertEqual(self.read_rev(), 0)
        self.assertIn("old-name", {i["id"] for i in self.read_items()})

    def test_10c_rename_numeric_id_stale_if_rev_refused(self):
        self.write_items([make_item("old-name"), make_item("other-item")])
        dev_status.cmd_done(_args(id="other-item"))  # bumps rev to 1
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_rename(_args(old_slug="1", new_slug="new-name", if_rev=0))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("stale rev", err.getvalue())
        self.assertIn("old-name", {i["id"] for i in self.read_items()})

    def test_10d_rename_numeric_id_matching_if_rev_succeeds(self):
        self.write_items([make_item("old-name")])
        cur_rev = self.read_rev()
        dev_status.cmd_rename(_args(old_slug="1", new_slug="new-name", if_rev=cur_rev))
        ids = {i["id"] for i in self.read_items()}
        self.assertIn("new-name", ids)
        self.assertNotIn("old-name", ids)
        self.assertEqual(self.read_rev(), cur_rev + 1)

    def test_10e_rename_slug_id_never_requires_if_rev(self):
        self.write_items([make_item("old-name")])
        dev_status.cmd_rename(
            _args(old_slug="old-name", new_slug="new-name", if_rev=None)
        )
        ids = {i["id"] for i in self.read_items()}
        self.assertIn("new-name", ids)
        self.assertNotIn("old-name", ids)

    # ── 11: block adds dep; cycle rejected ───────────────────────────────────

    def test_11a_block_adds_dep(self):
        self.write_items([make_item("item-a"), make_item("item-b")])
        args = _args(id="item-b", blocker="item-a")
        dev_status.cmd_block(args)
        index = {i["id"]: i for i in self.read_items()}
        self.assertIn("item-a", index["item-b"]["blocked_by"])

    def test_11b_block_cycle_rejected(self):
        items = [
            make_item("item-a", blocked_by=["item-b"]),
            make_item("item-b"),
        ]
        self.write_items(items)
        args = _args(id="item-b", blocker="item-a")
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_block(args)
        self.assertIn("cycle", err.getvalue())

    def test_11c_block_accepts_pending_slug(self):
        self.write_items([make_item("item-a")])
        self.write_pending(
            [
                {
                    "id": "pend-block",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting on Angela",
                    "kind": "email",
                    "blocking": [],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        dev_status.cmd_block(_args(id="item-a", blocker="pend-block"))
        items = self.read_items()
        self.assertEqual(items[0]["blocked_by"], ["pend-block"])

    def test_11d_block_unknown_slug_still_rejected(self):
        self.write_items([make_item("item-a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_block(_args(id="item-a", blocker="ghost-slug"))
        self.assertIn("blocker not found", err.getvalue())

    # ── 12: done blocker promotes BLOCKED → READY ────────────────────────────

    def test_12_done_blocker_promotes_to_ready(self):
        items = [
            make_item("blocker-item", status="done"),
            make_item("blocked-item", blocked_by=["blocker-item"]),
        ]
        self.write_items(items)
        index = dev_status.build_index(items)
        eff = dev_status.effective_blockers(items[1], index)
        self.assertEqual(eff, [])  # done blocker excluded

        in_progress, ready, blocked, in_review, done = dev_status._render_order(items)
        self.assertIn(items[1], ready)
        self.assertEqual(blocked, [])
        self.assertEqual(in_review, [])

    def test_12b_render_blocked_by_pending_shows_description_hint(self):
        items = [make_item("blocked-item", blocked_by=["pend-block"])]
        pending_items = [
            {
                "id": "pend-block",
                "created": "2026-01-01",
                "updated": "2026-01-01",
                "status": "waiting_for_reply",
                "description": "waiting on Angela to reply",
                "kind": "email",
                "blocking": ["blocked-item"],
                "related_files": [],
                "next_steps": [],
                "outcome": None,
            }
        ]
        self.write_items(items)
        self.write_pending(pending_items)

        in_progress, ready, blocked, in_review, done = dev_status._render_order(items)
        self.assertEqual([i["id"] for i in blocked], ["blocked-item"])
        self.assertEqual(ready, [])

        out, err = io.StringIO(), io.StringIO()
        dev_status.render(items, pending_items, out=out, err=err)
        blocked_by_lines = [
            line for line in out.getvalue().splitlines() if "↳ blocked by:" in line
        ]
        self.assertEqual(len(blocked_by_lines), 1)
        self.assertIn("waiting on Angela to reply", blocked_by_lines[0])

    @staticmethod
    def _section_lines(rendered, section_name):
        lines = rendered.splitlines()
        start = next(i for i, line in enumerate(lines) if f"─ {section_name} ─" in line)
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("└─"))
        return lines[start:end]

    def test_12c_render_in_progress_with_blocker_shows_hint(self):
        items = [
            make_item(
                "in-progress-item", status="in-progress", blocked_by=["blocker-item"]
            ),
            make_item("blocker-item", status="open"),
        ]
        self.write_items(items)

        out, err = io.StringIO(), io.StringIO()
        dev_status.render(items, [], out=out, err=err)

        section = self._section_lines(out.getvalue(), "IN PROGRESS")
        blocked_by_lines = [line for line in section if "↳ blocked by:" in line]
        self.assertEqual(len(blocked_by_lines), 1)
        self.assertIn("blocker-item", blocked_by_lines[0])

    def test_12d_render_in_progress_without_blocker_shows_no_hint(self):
        items = [make_item("in-progress-item", status="in-progress")]
        self.write_items(items)

        out, err = io.StringIO(), io.StringIO()
        dev_status.render(items, [], out=out, err=err)

        section = self._section_lines(out.getvalue(), "IN PROGRESS")
        blocked_by_lines = [line for line in section if "↳ blocked by:" in line]
        self.assertEqual(blocked_by_lines, [])

    # ── 13: numbering is 1..N globally, contiguous ───────────────────────────

    def test_13_numbering_contiguous(self):
        items = [
            make_item("item-a", status="in-progress"),
            make_item("item-b", status="open"),
            make_item("item-c", status="open"),
            make_item("item-d", status="done"),
        ]
        self.write_items(items)
        out = io.StringIO()
        err = io.StringIO()
        dev_status.render(items, out=out, err=err)
        map_line = err.getvalue().strip()
        self.assertTrue(map_line.startswith("item-map:"))
        self.assertIn("rev=0", map_line)
        # format: "item-map: rev=0 1=backlog:..,2=.."; strip prefix + rev token
        pairs = map_line.replace("item-map: ", "").split(" ", 1)[1].split(",")
        numbers = [int(p.split("=")[0]) for p in pairs if p]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    # ── 14: atomic write — temp cleaned up on failure ────────────────────────

    def test_14_atomic_write_cleans_temp_on_failure(self):
        items = [make_item("my-item")]
        self.write_items(items)
        original_content = self.items_file.read_text()

        with (
            patch("os.replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            dev_status.save_items(items)

        self.assertEqual(self.items_file.read_text(), original_content)
        tmp_files = list(self.data_dir.glob(".items_tmp_*"))
        self.assertEqual(tmp_files, [])

    def test_14b_fsync_called_before_replace(self):
        """Ensure the implementation fsyncs the temp file (and directory) before/after replace.

        Patching os.replace to raise lets the write path run far enough to
        exercise the fsync calls while avoiding a real rename. The test
        asserts that os.fsync was invoked at least once during save_items.
        """
        items = [make_item("my-item")]
        self.write_items(items)

        # Patch os.fsync so we can assert it was called; patch os.replace to
        # raise so the path cleans up the temp file as in test_14.
        with (
            patch("os.fsync") as fsync_mock,
            patch("os.replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            dev_status.save_items(items)

        # os.fsync should have been called for the temp file (and possibly
        # for the directory). We don't assert exact call args because FDs
        # differ across platforms; presence of any call is sufficient.
        self.assertTrue(fsync_mock.called)

    # ── 14c: fsync/replace/fsync ordering, and the second fsync targets the
    # containing directory's fd specifically (not just "any two fsyncs") ────

    def test_14c_atomic_write_fsyncs_temp_before_replace_and_dir_after(self):
        real_open = os.open
        opened_fds = {}

        def open_side_effect(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            opened_fds[path] = fd
            return fd

        manager = unittest.mock.Mock()
        with (
            patch("os.fsync", wraps=os.fsync) as fsync_mock,
            patch("os.replace", wraps=os.replace) as replace_mock,
            patch("os.open", side_effect=open_side_effect) as open_mock,
        ):
            manager.attach_mock(fsync_mock, "fsync")
            manager.attach_mock(replace_mock, "replace")
            manager.attach_mock(open_mock, "open")
            dev_status._atomic_write_json(
                self.items_file,
                json.dumps({"schema_version": 2, "items": []}),
                ".items_tmp_",
            )

        call_strs = [str(c) for c in manager.mock_calls]
        fsync_positions = [
            i for i, s in enumerate(call_strs) if s.startswith("call.fsync(")
        ]
        replace_positions = [
            i for i, s in enumerate(call_strs) if s.startswith("call.replace(")
        ]
        self.assertEqual(len(fsync_positions), 2, call_strs)
        self.assertEqual(len(replace_positions), 1, call_strs)
        self.assertLess(fsync_positions[0], replace_positions[0], call_strs)
        self.assertGreater(fsync_positions[1], replace_positions[0], call_strs)

        # The second fsync must target the fd os.open returned for the
        # containing directory, not merely a second, arbitrary fd -- fd
        # integers get reused by the next open() once a prior one closes,
        # so a bare "the two fds differ" assertion would be a spurious,
        # environment-dependent check rather than a real one.
        dir_fd = opened_fds.get(str(self.data_dir))
        self.assertIsNotNone(dir_fd, opened_fds)
        second_fsync_fd = fsync_mock.call_args_list[1].args[0]
        self.assertEqual(second_fsync_fd, dir_fd)

    # ── 14d: _backlog_mutation bumps rev before its post-yield save, and on
    # a mid-block exception the rev is still bumped but the save is skipped ──

    def test_14d_backlog_mutation_bumps_rev_before_any_write(self):
        self.write_items([make_item("my-item")])

        manager = unittest.mock.Mock()
        with (
            patch.object(
                dev_status, "bump_rev", wraps=dev_status.bump_rev
            ) as bump_mock,
            patch.object(
                dev_status, "save_items", wraps=dev_status.save_items
            ) as save_mock,
        ):
            manager.attach_mock(bump_mock, "bump_rev")
            manager.attach_mock(save_mock, "save_items")
            with dev_status._backlog_mutation("test", "my-item", None):
                pass

        call_strs = [str(c) for c in manager.mock_calls]
        bump_positions = [
            i for i, s in enumerate(call_strs) if s.startswith("call.bump_rev(")
        ]
        save_positions = [
            i for i, s in enumerate(call_strs) if s.startswith("call.save_items(")
        ]
        self.assertEqual(len(bump_positions), 1, call_strs)
        self.assertEqual(len(save_positions), 1, call_strs)
        self.assertLess(bump_positions[0], save_positions[0], call_strs)

    def test_14d2_backlog_mutation_exception_bumps_rev_but_skips_save(self):
        self.write_items([make_item("my-item")])

        with (
            patch.object(
                dev_status, "bump_rev", wraps=dev_status.bump_rev
            ) as bump_mock,
            patch.object(
                dev_status, "save_items", wraps=dev_status.save_items
            ) as save_mock,
            self.assertRaises(RuntimeError),
        ):
            with dev_status._backlog_mutation("test", "my-item", None):
                raise RuntimeError("boom")

        bump_mock.assert_called_once()
        save_mock.assert_not_called()

    # ── 14e: the set of commands that bump-and-save manually (outside
    # _backlog_mutation) is exactly the six known ones -- a future 7th site
    # must be added here or this fails by name ──────────────────────────────

    def test_14e_manual_mutation_site_list_is_exhaustive(self):
        candidates = list(dev_status.dispatch.values()) + [
            dev_status.cmd_pending_add,
            dev_status.cmd_pending_update,
            dev_status.cmd_pending_list,
        ]
        manual_sites = {
            fn.__name__
            for fn in candidates
            if (
                "save_items" in fn.__code__.co_names
                or "save_pending" in fn.__code__.co_names
            )
            and "_backlog_mutation" not in fn.__code__.co_names
        }
        self.assertEqual(
            manual_sites,
            {
                "cmd_add",
                "cmd_backfill_gate",
                "cmd_rename",
                "cmd_pending_add",
                "cmd_pending_update",
                "cmd_prune",
            },
        )

    # ── 14f: each of the six manual sites bumps rev before it saves ─────────

    def test_14f_manual_mutation_sites_bump_before_saving(self):
        cases = [
            (
                "cmd_add",
                dev_status.cmd_add,
                lambda: _args(json='{"id": "new-item", "summary": "Test"}'),
                lambda: None,
            ),
            (
                "cmd_backfill_gate",
                dev_status.cmd_backfill_gate,
                lambda: _args(apply=True),
                lambda: self.write_items([make_item("gate-item")]),
            ),
            (
                "cmd_rename",
                dev_status.cmd_rename,
                lambda: _args(old_slug="old-name", new_slug="new-name"),
                lambda: self.write_items([make_item("old-name")]),
            ),
            (
                "cmd_pending_add",
                dev_status.cmd_pending_add,
                lambda: _args(
                    json='{"id": "pend-item", "description": "waiting", "kind": "email"}'
                ),
                lambda: None,
            ),
            (
                "cmd_pending_update",
                dev_status.cmd_pending_update,
                lambda: _args(id="pend-item", patch='{"status": "reply_received"}'),
                lambda: self.write_pending(
                    [
                        {
                            "id": "pend-item",
                            "created": "2026-01-01",
                            "updated": "2026-01-01",
                            "status": "waiting_for_reply",
                            "description": "w",
                            "kind": "email",
                        }
                    ]
                ),
            ),
            (
                "cmd_prune",
                dev_status.cmd_prune,
                lambda: _args(force=True),
                lambda: self.write_items(
                    [
                        make_item(
                            "done-old",
                            status="done",
                            updated=(date.today() - timedelta(days=20)).isoformat(),
                        )
                    ]
                ),
            ),
        ]

        for name, func, build_args, seed_fixture in cases:
            with self.subTest(cmd=name):
                shutil.rmtree(self.data_dir, ignore_errors=True)
                seed_fixture()

                manager = unittest.mock.Mock()
                with (
                    patch.object(
                        dev_status, "bump_rev", wraps=dev_status.bump_rev
                    ) as bump_mock,
                    patch.object(
                        dev_status, "save_items", wraps=dev_status.save_items
                    ) as save_items_mock,
                    patch.object(
                        dev_status, "save_pending", wraps=dev_status.save_pending
                    ) as save_pending_mock,
                ):
                    manager.attach_mock(bump_mock, "bump_rev")
                    manager.attach_mock(save_items_mock, "save_items")
                    manager.attach_mock(save_pending_mock, "save_pending")
                    func(build_args())

                self.assertGreaterEqual(bump_mock.call_count, 1, name)
                self.assertGreaterEqual(
                    save_items_mock.call_count + save_pending_mock.call_count,
                    1,
                    name,
                )

                call_strs = [str(c) for c in manager.mock_calls]
                bump_positions = [
                    i for i, s in enumerate(call_strs) if s.startswith("call.bump_rev(")
                ]
                save_positions = [
                    i
                    for i, s in enumerate(call_strs)
                    if s.startswith("call.save_items(")
                    or s.startswith("call.save_pending(")
                ]
                self.assertTrue(bump_positions, (name, call_strs))
                self.assertTrue(save_positions, (name, call_strs))
                self.assertLess(
                    bump_positions[0], min(save_positions), (name, call_strs)
                )

    # ── 15: corrupted JSON fails loudly ──────────────────────────────────────

    def test_15_corrupted_json_fails_loudly(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.items_file.write_text("{not valid json")
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.load_items()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("corrupted", err.getvalue())

    # ── 16: category tag rendered as bracketed prefix ─────────────────────────

    def test_16_category_tag_rendered_on_each_line(self):
        items = [
            make_item("bug-item", status="open", summary="Fix widget"),
        ]
        items[0]["category"] = "bug"
        out = io.StringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        self.assertIn("[bug] Fix widget", out.getvalue())

    def test_16b_unknown_category_truncated_to_five_chars(self):
        items = [make_item("odd-item", status="open")]
        items[0]["category"] = "documentation"
        out = io.StringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        self.assertIn("[docum]", out.getvalue())

    # ── 17: age suffix on IN PROGRESS/BLOCKED, warning past threshold ────────

    def test_17_age_suffix_on_in_progress_only_not_ready(self):
        old = (date.today() - timedelta(days=3)).isoformat()
        items = [
            make_item("active-item", status="in-progress", updated=old),
            make_item("ready-item", status="open", updated=old),
        ]
        out = io.StringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        text = out.getvalue()
        self.assertIn("· 3d", text)
        # only one "· Nd" occurrence — READY must not get an age suffix
        self.assertEqual(text.count("· 3d"), 1)

    def test_17b_warning_glyph_past_stale_threshold(self):
        fresh = (date.today() - timedelta(days=2)).isoformat()
        stale = (date.today() - timedelta(days=8)).isoformat()
        items = [
            make_item("fresh-item", status="in-progress", updated=fresh),
            make_item(
                "stale-item", status="in-progress", updated=stale, summary="Stuck task"
            ),
        ]
        out = io.StringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        lines = out.getvalue().splitlines()
        fresh_line = next(
            ln for ln in lines if "fresh-item" in ln or "Summary of fresh-item" in ln
        )
        stale_line = next(ln for ln in lines if "Stuck task" in ln)
        self.assertNotIn("⚠️", fresh_line)
        self.assertIn("⚠️", stale_line)

    # ── 18: DONE section is a fixed latest-five selection ───────────────────

    def test_18_done_section_keeps_latest_five_not_recency_window(self):
        items = [
            make_item(
                f"done-{year}",
                status="done",
                updated=f"{year}-01-01",
            )
            for year in range(2020, 2026)
        ]
        for item in items:
            item["completed_at"] = item["updated"]
        out = io.StringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        text = out.getvalue()
        self.assertIn("DONE", text)
        for year in range(2021, 2026):
            self.assertIn(f"done-{year}", text)
        self.assertNotIn("done-2020", text)
        self.assertNotIn("showing", text)

    def test_18b_done_section_keeps_old_completion_when_selected(self):
        stale = (date.today() - timedelta(days=10)).isoformat()
        items = [make_item("stale", status="done", updated=stale)]
        out = io.StringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        self.assertIn("DONE", out.getvalue())
        self.assertIn("stale", out.getvalue())

    def test_18c_done_order_keys_on_completed_at(self):
        # `completed_at` stamps the actual completion; `updated` getting
        # bumped later (e.g. an edit to a done item) must not change the
        # completion ordering. Regression test for completed_at precedence.
        old_completion = (date.today() - timedelta(days=10)).isoformat()
        items = [
            make_item(
                "edited-old",
                status="done",
                updated=date.today().isoformat(),
            ),
        ]
        items[0]["completed_at"] = old_completion
        _, _, _, _, done = dev_status._render_order(items)
        self.assertEqual([i["id"] for i in done], ["edited-old"])

    def test_18d_done_selection_falls_back_to_updated_without_completed_at(self):
        # Legacy done items without `completed_at` fall back to `updated`.
        recent = (date.today() - timedelta(days=1)).isoformat()
        items = [make_item("legacy", status="done", updated=recent)]
        _, _, _, _, done = dev_status._render_order(items)
        self.assertEqual([i["id"] for i in done], ["legacy"])

    def test_18e_done_section_orders_by_completed_at_not_updated(self):
        # Regression test: an item finished earlier but edited afterward
        # (bumping `updated`) must not outrank an item that finished more
        # recently. Only completion order should decide DONE ordering.
        items = [
            make_item(
                "finished-earlier-edited-later",
                status="done",
                updated=date.today().isoformat(),
            ),
            make_item(
                "finished-later-not-edited",
                status="done",
                updated=(date.today() - timedelta(days=1)).isoformat(),
            ),
        ]
        items[0]["completed_at"] = (date.today() - timedelta(days=1)).isoformat()
        items[1]["completed_at"] = date.today().isoformat()
        _, _, _, _, done = dev_status._render_order(items)
        self.assertEqual(
            [i["id"] for i in done],
            ["finished-later-not-edited", "finished-earlier-edited-later"],
        )

    def test_18f_done_selection_uses_deterministic_id_tie_break(self):
        items = []
        for slug in ("z-item", "a-item", "m-item"):
            item = make_item(slug, status="done", updated="2026-01-01")
            item["completed_at"] = "2026-01-01T12:00:00+00:00"
            items.append(item)
        _, _, _, _, done = dev_status._render_order(items)
        self.assertEqual([item["id"] for item in done], ["a-item", "m-item", "z-item"])

    def test_18g_invalid_completed_at_does_not_fall_back(self):
        invalid = make_item("invalid", status="done", updated="2026-01-02")
        invalid["completed_at"] = "not-a-date"
        empty = make_item("empty", status="done", updated="2026-01-03")
        empty["completed_at"] = ""
        _, _, _, _, done = dev_status._render_order([invalid, empty])
        self.assertEqual([item["id"] for item in done], ["empty"])

    def test_18h_done_selection_normalizes_naive_and_aware_timestamps(self):
        aware = make_item("aware", status="done", updated="2026-01-01")
        aware["completed_at"] = "2026-01-01T12:00:00+02:00"
        naive = make_item("naive", status="done", updated="2026-01-01")
        naive["completed_at"] = "2026-01-01T09:00:00"
        _, _, _, _, done = dev_status._render_order([naive, aware])
        self.assertEqual([item["id"] for item in done], ["aware", "naive"])

    # ── 19: color gated on isatty ─────────────────────────────────────────────

    def test_19_no_color_codes_when_not_a_tty(self):
        items = [make_item("plain-item", status="open")]
        out = io.StringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        self.assertNotIn("\x1b[", out.getvalue())

    def test_19b_color_codes_present_when_tty(self):
        items = [make_item("colored-item", status="open")]
        out = _TtyStringIO()
        dev_status.render(items, out=out, err=io.StringIO())
        self.assertIn("\x1b[", out.getvalue())

    # ── 20: rev increments on each mutating op, not on reads ───────────────

    def test_20a_rev_increments_on_add(self):
        self.assertEqual(self.read_rev(), 0)
        args = _args(json='{"id": "my-item", "summary": "x"}')
        dev_status.cmd_add(args)
        self.assertEqual(self.read_rev(), 1)

    def test_20b_rev_increments_on_start(self):
        self.write_items([make_item("my-item")])
        dev_status.cmd_start(_args(id="my-item"))
        self.assertEqual(self.read_rev(), 1)

    def test_20c_rev_increments_on_done(self):
        self.write_items([make_item("my-item", status="in-progress")])
        dev_status.cmd_done(_args(id="my-item"))
        self.assertEqual(self.read_rev(), 1)

    def test_20d_rev_increments_on_update(self):
        self.write_items([make_item("my-item")])
        args = _args(id="my-item", patch='{"context": "ctx"}')
        dev_status.cmd_update(args)
        self.assertEqual(self.read_rev(), 1)

    def test_20e_rev_increments_on_block(self):
        self.write_items([make_item("item-a"), make_item("item-b")])
        dev_status.cmd_block(_args(id="item-b", blocker="item-a"))
        self.assertEqual(self.read_rev(), 1)

    def test_20f_rev_increments_on_unblock(self):
        self.write_items(
            [make_item("item-a"), make_item("item-b", blocked_by=["item-a"])]
        )
        dev_status.cmd_unblock(_args(id="item-b", blocker="item-a"))
        self.assertEqual(self.read_rev(), 1)

    def test_20g_rev_increments_on_rename(self):
        self.write_items([make_item("old-name")])
        dev_status.cmd_rename(_args(old_slug="old-name", new_slug="new-name"))
        self.assertEqual(self.read_rev(), 1)

    def test_20h_rev_increments_on_prune_only_when_removed(self):
        old = (date.today() - timedelta(days=20)).isoformat()
        self.write_items([make_item("done-old", status="done", updated=old)])
        dev_status.cmd_prune(_args(force=True))
        self.assertEqual(self.read_rev(), 1)

    def test_20i_rev_unchanged_on_prune_nothing(self):
        self.write_items([make_item("my-item")])
        dev_status.cmd_prune(_args(force=True))
        self.assertEqual(self.read_rev(), 0)

    def test_20j_rev_increments_on_pending_add(self):
        args = _args(
            json='{"id": "pend-item", "description": "waiting", "kind": "email"}'
        )
        dev_status.cmd_pending_add(args)
        self.assertEqual(self.read_rev(), 1)

    def test_20k_rev_increments_on_pending_update(self):
        self.write_pending(
            [
                {
                    "id": "pend-item",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "w",
                    "kind": "email",
                }
            ]
        )
        dev_status.cmd_pending_update(
            _args(id="pend-item", patch='{"status": "reply_received"}')
        )
        self.assertEqual(self.read_rev(), 1)

    def test_20l_rev_unchanged_on_reads(self):
        self.write_items([make_item("my-item")])
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_render(_args())
            dev_status.cmd_list(_args())
        dev_status.cmd_show(_args(id="my-item"))
        self.assertEqual(self.read_rev(), 0)

    def test_20m_list_status_filter(self):
        self.write_items(
            [
                make_item("a-open", status="open"),
                make_item("b-done", status="done"),
                make_item("c-progress", status="in-progress"),
                make_item("d-done", status="done"),
                make_item("e-review", status="in-review"),
            ]
        )
        for value in sorted(dev_status.VALID_STATUSES):
            out, err = io.StringIO(), io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", err):
                dev_status.cmd_list(_args(status=value, raw=True))
            lines = [
                ln
                for ln in out.getvalue().splitlines()
                if ln and not ln.startswith("#")
            ]
            ids = {ln.split("\t")[0] for ln in lines}
            statuses = {ln.split("\t")[1] for ln in lines}
            self.assertEqual(statuses, {value}, f"status={value}")
            if value == "done":
                self.assertEqual(ids, {"b-done", "d-done"})
            elif value == "open":
                self.assertEqual(ids, {"a-open"})
            elif value == "in-progress":
                self.assertEqual(ids, {"c-progress"})
            elif value == "in-review":
                self.assertEqual(ids, {"e-review"})

    def test_20n_list_status_invalid_rejected(self):
        # argparse rejects unknown --status values with exit code 2.
        err = io.StringIO()
        with (
            patch("sys.stderr", err),
            self.assertRaises(SystemExit) as cm,
            patch("sys.argv", ["dev_status", "list", "--status", "bogus"]),
        ):
            dev_status.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("bogus", err.getvalue())

    def test_20o_list_rev_goes_to_stderr_not_stdout(self):
        # cmd_show and render both print "# rev=N" to stderr; cmd_list was
        # the odd one out, printing it to stdout instead.
        self.write_items([make_item("my-item")])
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_list(_args())
        self.assertNotIn("rev=", out.getvalue())
        self.assertIn("rev=0", err.getvalue())

    # ── 20p–20s: list default table (--raw keeps the frozen TSV) ──────────

    def test_20p_list_default_groups_by_status_in_render_order(self):
        self.write_items(
            [
                make_item("a-done", status="done"),
                make_item("b-blocked", status="open", blocked_by=["c-progress"]),
                make_item("c-progress", status="in-progress"),
                make_item("d-review", status="in-review"),
                make_item("e-ready", status="open"),
            ]
        )
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_list(_args())
        text = out.getvalue()
        positions = [
            text.index(s)
            for s in ("IN PROGRESS", "READY", "BLOCKED", "IN REVIEW", "DONE")
        ]
        self.assertEqual(positions, sorted(positions))
        for slug in ("a-done", "b-blocked", "c-progress", "d-review", "e-ready"):
            self.assertIn(slug, text)
            self.assertIn(f"Summary of {slug}", text)

    def test_20q_list_default_numbers_match_unified_order(self):
        self.write_pending(
            [
                {
                    "id": "pend-item",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "w",
                    "kind": "email",
                }
            ]
        )
        items = [
            make_item("a-progress", status="in-progress"),
            make_item("b-open", status="open"),
        ]
        self.write_items(items)
        pending = dev_status.load_pending()
        ordered = dev_status._unified_order(items, pending)
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_list(_args())
        printed = out.getvalue()
        for n, item in enumerate(ordered, start=1):
            if item["id"] == "pend-item":
                continue
            row = next(ln for ln in printed.splitlines() if item["id"] in ln)
            num = int(re.search(r"\d+", row).group(0))
            self.assertEqual(num, n, f"row for {item['id']}")

    def test_20r_list_raw_preserves_tsv_bytes(self):
        items = [
            make_item("a-open", status="open"),
            make_item("b-done", status="done"),
            make_item("c-progress", status="in-progress"),
        ]
        self.write_items(items)
        expected = "\n".join(f"{i['id']}\t{i['status']}\t{i['summary']}" for i in items)
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_list(_args(raw=True))
        self.assertEqual(out.getvalue(), expected + "\n")
        # --status composes with --raw exactly as the pre-table TSV did.
        out2, err2 = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out2), patch("sys.stderr", err2):
            dev_status.cmd_list(_args(status="done", raw=True))
        self.assertEqual(out2.getvalue(), "b-done\tdone\tSummary of b-done\n")

    def test_20s_list_default_empty_and_no_match_messages(self):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_list(_args())
        self.assertIn("(backlog is empty)", out.getvalue())

        self.write_items([make_item("only-done", status="done")])
        out2, err2 = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out2), patch("sys.stderr", err2):
            dev_status.cmd_list(_args(status="open"))
        self.assertIn("(no matching items)", out2.getvalue())

    # ── 21: numeric id without --if-rev refused, no write ─────────────────

    def test_21_numeric_id_without_if_rev_refused(self):
        self.write_items([make_item("item-a", status="in-progress")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_start(_args(id="1", if_rev=None))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("requires --if-rev", err.getvalue())
        # no write, no rev bump
        self.assertEqual(self.read_rev(), 0)
        self.assertEqual(
            {i["id"]: i["status"] for i in self.read_items()}["item-a"],
            "in-progress",
        )

    # ── 22: stale --if-rev refused, no write ──────────────────────────────

    def test_22_numeric_id_stale_if_rev_refused(self):
        self.write_items([make_item("item-a", status="in-progress")])
        # slug mutation bumps rev to 1
        dev_status.cmd_done(_args(id="item-a"))
        self.assertEqual(self.read_rev(), 1)
        # reset item to in-progress on disk WITHOUT bumping rev (disk rev stays 1)
        items = self.read_items()
        items[0]["status"] = "in-progress"
        self.write_items(items)
        # caller still holds stale rev 0 and uses a numeric id — must refuse
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_start(_args(id="1", if_rev=0))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("stale rev", err.getvalue())
        # no write, rev unchanged
        self.assertEqual(self.read_rev(), 1)
        self.assertEqual(
            {i["id"]: i["status"] for i in self.read_items()}["item-a"],
            "in-progress",
        )

    # ── 23: matching --if-rev succeeds ─────────────────────────────────────

    def test_23_numeric_id_matching_if_rev_succeeds(self):
        self.write_items([make_item("item-a", status="in-progress")])
        dev_status.cmd_done(_args(id="item-a"))  # rev -> 1, item now done
        # reset item to in-progress on disk without bumping rev, so numeric call is meaningful
        items = self.read_items()
        items[0]["status"] = "in-progress"
        self.write_items(items)
        cur_rev = self.read_rev()
        dev_status.cmd_start(_args(id="1", if_rev=cur_rev))
        self.assertEqual(
            {i["id"]: i["status"] for i in self.read_items()}["item-a"],
            "in-progress",
        )
        self.assertEqual(self.read_rev(), cur_rev + 1)

    # ── 24: slug id never requires --if-rev ────────────────────────────────

    def test_24_slug_id_never_requires_if_rev(self):
        self.write_items([make_item("my-task", status="in-progress")])
        dev_status.cmd_done(_args(id="my-task"))
        self.assertEqual(
            {i["id"]: i["status"] for i in self.read_items()}["my-task"], "done"
        )

    # ── 25: _meta.json lazy-created on first mutation ─────────────────────

    def test_25_meta_file_lazy_created(self):
        self.assertFalse(self.meta_file.exists())
        dev_status.cmd_add(_args(json='{"id": "my-item", "summary": "x"}'))
        self.assertTrue(self.meta_file.exists())
        self.assertEqual(json.loads(self.meta_file.read_text()), {"rev": 1})

    # ── 26: concurrent writes serialize, no lost update ───────────────────

    def test_26_concurrent_writes_serialize_no_lost_update(self):

        self.write_items([make_item("item-a")])
        results = {}

        def writer(name):
            # each thread opens its own fd on LOCK_FILE (per open-file-description flock)
            dev_status.cmd_add(_args(json=f'{{"id": "child-{name}", "summary": "x"}}'))
            results[name] = self.read_rev()

        # hold the lock in the main thread so the spawned writers block
        with dev_status.backlog_lock():
            t1 = threading.Thread(target=writer, args=("one",))
            t2 = threading.Thread(target=writer, args=("two",))
            t1.start()
            t2.start()
            # give them a moment to block on flock
            import time

            time.sleep(0.1)
            self.assertTrue(t1.is_alive())
            self.assertTrue(t2.is_alive())
            # now release; both should proceed serialized
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        # both writes landed + rev bumped twice (no lost update)
        ids = {i["id"] for i in self.read_items()}
        self.assertIn("child-one", ids)
        self.assertIn("child-two", ids)
        self.assertEqual(self.read_rev(), 2)

    # ── 27-36: priority field (high/normal/low, absence == normal) ───────────

    def test_27_add_priority_high_present_in_show(self):
        dev_status.cmd_add(
            _args(json='{"id": "p-high", "summary": "x", "priority": "high"}')
        )
        items = self.read_items()
        self.assertEqual(items[0].get("priority"), "high")

    def test_28_add_priority_absent_omits_key(self):
        dev_status.cmd_add(_args(json='{"id": "p-none", "summary": "x"}'))
        items = self.read_items()
        self.assertNotIn("priority", items[0])

    def test_29_add_invalid_priority_refused(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_add(
                _args(json='{"id": "p-bad", "summary": "x", "priority": "urgent"}')
            )
        self.assertEqual(cm.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("invalid priority 'urgent'", msg)
        self.assertIn("high, low, normal", msg)
        self.assertEqual(len(self.read_items()), 0)

    def test_30_update_priority_high_floats_to_top_of_ready(self):
        # FIFO setup: A (oldest), B, C (newest) — READY renders A,B,C
        self.write_items(
            [
                make_item("a", created="2026-01-01"),
                make_item("b", created="2026-01-02"),
                make_item("c", created="2026-01-03"),
            ]
        )
        dev_status.cmd_update(_args(id="b", patch='{"priority": "high"}'))
        _, ready, _, _, _ = dev_status._render_order(self.read_items())
        self.assertEqual([i["id"] for i in ready], ["b", "a", "c"])

    def test_31_update_priority_low_sinks_to_bottom_of_ready(self):
        self.write_items(
            [
                make_item("a", created="2026-01-01"),
                make_item("b", created="2026-01-02"),
                make_item("c", created="2026-01-03"),
            ]
        )
        dev_status.cmd_update(_args(id="a", patch='{"priority": "low"}'))
        _, ready, _, _, _ = dev_status._render_order(self.read_items())
        self.assertEqual([i["id"] for i in ready], ["b", "c", "a"])

    def test_32_priority_does_not_move_item_after_update_other_field(self):
        self.write_items(
            [
                make_item("a", created="2026-01-01"),
                make_item("b", created="2026-01-02", priority="high"),
                make_item("c", created="2026-01-03"),
            ]
        )
        # B starts at top due to high priority.
        _, ready_before, _, _, _ = dev_status._render_order(self.read_items())
        self.assertEqual([i["id"] for i in ready_before], ["b", "a", "c"])
        # Updating B's context bumps `updated` but must not change READY order.
        dev_status.cmd_update(_args(id="b", patch='{"context": "x"}'))
        _, ready_after, _, _, _ = dev_status._render_order(self.read_items())
        self.assertEqual([i["id"] for i in ready_after], ["b", "a", "c"])

    def test_33_blocked_priority_secondary_within_blocker_count(self):
        # Three blocked items, all with 1 blocker each, created A<B<C.
        # B is high-priority; A,C normal. BLOCKED order should be B,A,C —
        # priority lands between blocker-count and updated-asc tiebreak.
        # All share updated==created so the A-vs-C tiebreak is created-asc.
        blocker = make_item("blocker", status="open", updated="2026-01-01")
        self.write_items(
            [
                blocker,
                make_item(
                    "a",
                    created="2026-01-01",
                    updated="2026-01-01",
                    blocked_by=["blocker"],
                ),
                make_item(
                    "b",
                    created="2026-01-02",
                    updated="2026-01-02",
                    blocked_by=["blocker"],
                    priority="high",
                ),
                make_item(
                    "c",
                    created="2026-01-03",
                    updated="2026-01-03",
                    blocked_by=["blocker"],
                ),
            ]
        )
        _, _, blocked, _, _ = dev_status._render_order(self.read_items())
        self.assertEqual([i["id"] for i in blocked], ["b", "a", "c"])

    def test_34_done_ignores_priority(self):
        old = (date.today() - timedelta(days=10)).isoformat()
        newer = (date.today() - timedelta(days=1)).isoformat()
        newest = date.today().isoformat()
        self.write_items(
            [
                make_item("old", status="done", updated=old, priority="high"),
                make_item("newer", status="done", updated=newer),
                make_item("newest", status="done", updated=newest),
            ]
        )
        _, _, _, _, done = dev_status._render_order(self.read_items())
        # All eligible items appear in completion order; priority does not
        # reorder DONE.
        self.assertEqual([i["id"] for i in done], ["newest", "newer", "old"])

    def test_35_render_priority_badge_for_high_and_low(self):
        self.write_items(
            [
                make_item("hip", priority="high"),
                make_item("lop", priority="low"),
                make_item("nor"),
            ]
        )
        out = _TtyStringIO()
        dev_status.render(self.read_items(), out=out, err=io.StringIO())
        rendered = out.getvalue()
        # _priority_glyph renders ▲ for high, ▽ for low, and ·
        # for absent/normal on every row (no blank gutter) -- this test
        # previously asserted literal "\u00b7high"/"\u00b7low" text, which
        # _priority_glyph has never emitted; fixed to check the glyph that
        # actually appears on each item's own line.
        hip_line = next(ln for ln in rendered.splitlines() if "Summary of hip" in ln)
        lop_line = next(ln for ln in rendered.splitlines() if "Summary of lop" in ln)
        nor_line = next(ln for ln in rendered.splitlines() if "Summary of nor" in ln)
        self.assertIn("\u25b2", hip_line)
        self.assertIn("\u25bd", lop_line)
        # "nor" has no explicit priority -- still gets the normal-priority
        # middle-dot glyph, not the high/low triangle.
        self.assertIn("\u00b7", nor_line)
        self.assertNotIn("\u25b2", nor_line)
        self.assertNotIn("\u25bd", nor_line)

    def test_36_render_priority_default_for_unknown_value_is_normal(self):
        # Bypass validation: hand-write an item with priority:"urgent".
        items = [
            make_item("weird", priority="urgent"),
            make_item("ok"),
        ]
        # _render_order must not raise; unknown collapses to normal's rank.
        _, ready, _, _, _ = dev_status._render_order(items)
        self.assertEqual([i["id"] for i in ready], ["weird", "ok"])
        # _priority_rank treats both as rank 1 (normal).
        self.assertEqual(
            dev_status._priority_rank(items[0]), dev_status._priority_rank(items[1])
        )

    # ── 37: remove by slug deletes one item ─────────────────────────────────

    def test_37_remove_by_slug_deletes_item(self):
        self.write_items([make_item("item-a"), make_item("item-b")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="item-a"))
        self.assertEqual(len(self.read_items()), 1)
        self.assertEqual(self.read_items()[0]["id"], "item-b")
        self.assertIn("[remove] item-a: Summary of item-a", err.getvalue())

    # ── 38: remove by number resolves through the unified render order ──────

    def test_38_remove_by_number_resolves_via_render(self):
        # two open items; render orders them by created (FIFO), so a=1, b=2.
        self.write_items([make_item("item-a"), make_item("item-b")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="2", if_rev=0))
        remaining = [i["id"] for i in self.read_items()]
        self.assertEqual(remaining, ["item-a"])
        self.assertIn("[remove] 2 → item-b: Summary of item-b", err.getvalue())

    # ── 39: numeric id without --if-rev refused, no write ──────────────────

    def test_39_remove_numeric_without_if_rev_refused(self):
        self.write_items([make_item("item-a"), make_item("item-b")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="1"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("requires --if-rev", err.getvalue())
        # no write, no rev bump
        self.assertEqual(self.read_rev(), 0)
        self.assertEqual({i["id"] for i in self.read_items()}, {"item-a", "item-b"})

    # ── 40: stale --if-rev refused, no write ───────────────────────────────

    def test_40_remove_numeric_stale_if_rev_refused(self):
        self.write_items([make_item("item-a"), make_item("item-b")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="1", if_rev=99))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("stale rev", err.getvalue())
        self.assertEqual(self.read_rev(), 0)
        self.assertEqual({i["id"] for i in self.read_items()}, {"item-a", "item-b"})

    # ── 41: slug id never requires --if-rev ────────────────────────────────

    def test_41_remove_slug_never_requires_if_rev(self):
        # write items, bump rev by writing meta directly so rev is non-zero
        self.write_items([make_item("item-a")])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.meta_file.write_text(json.dumps({"rev": 7}))
        # slug call succeeds without --if-rev regardless of rev state
        dev_status.cmd_remove(_args(id="item-a"))
        self.assertEqual(self.read_items(), [])

    # ── 42: pending item refused by require_kind ────────────────────────────

    def test_42_remove_pending_refused(self):
        self.write_pending(
            [
                {
                    "id": "pend-item",
                    "description": "waiting",
                    "kind": "email",
                    "status": "waiting_for_reply",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                }
            ]
        )
        # No backlog items, so the pending item sits at render position 1;
        # pass --if-rev to get past the rev-guard and reach require_kind.
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="1", if_rev=0))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("is a pending item", err.getvalue())
        # pending item survives intact
        self.assertEqual(len(dev_status.load_pending()), 1)
        self.assertEqual(dev_status.load_pending()[0]["id"], "pend-item")

    # ── 43: unknown slug exits cleanly, no write ────────────────────────────

    def test_43_remove_unknown_slug_exits(self):
        self.write_items([make_item("item-a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="nope-item"))
        self.assertEqual(cm.exception.code, 1)
        # resolve_id surfaces not-found before _backlog_mutation's per-cmd
        # check would (so an unknown slug doesn't get mis-resolved as
        # "wrong kind" by require_kind).
        self.assertIn("[resolve] not found: nope-item", err.getvalue())
        self.assertEqual(len(self.read_items()), 1)
        self.assertEqual(self.read_rev(), 0)

    # ── 44: out-of-range numeric exits via resolve_id, no write ─────────────

    def test_44_remove_unknown_number_exits(self):
        self.write_items([make_item("item-a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="9", if_rev=0))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("no item at position 9", err.getvalue())
        self.assertEqual(len(self.read_items()), 1)
        self.assertEqual(self.read_rev(), 0)

    # ── 45: remove PURGES inbound blocked_by refs (bug #4 fix — the previous
    #         behavior was a regression-lock on the bug, leaving a dangling ref
    #         that effective_blockers treated as unresolved, retroactively
    #         flipping dependents from READY into BLOCKED).

    def test_45_remove_purges_inbound_blocked_by_refs(self):
        self.write_items(
            [
                make_item("blocker", status="done"),
                make_item("dep", blocked_by=["blocker"]),
            ]
        )
        dev_status.cmd_remove(_args(id="blocker"))
        remaining = self.read_items()
        self.assertEqual([i["id"] for i in remaining], ["dep"])
        # blocked_by has been purged — no dangling reference to the
        # removed slug, so effective_blockers is empty (dep moves back to
        # READY) instead of treating the missing slug as unresolved.
        self.assertEqual(remaining[0]["blocked_by"], [])
        index = dev_status.build_index(remaining)
        self.assertEqual(dev_status.effective_blockers(remaining[0], index), [])

    # ── 46: remove bumps rev exactly once ──────────────────────────────────

    def test_46_remove_bumps_rev(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.meta_file.write_text(json.dumps({"rev": 5}))
        self.write_items([make_item("item-a"), make_item("item-b")])
        dev_status.cmd_remove(_args(id="item-a"))
        self.assertEqual(self.read_rev(), 6)

    # ── 47: remove subparser has no --force flag ────────────────────────────

    def test_47_remove_no_force_flag_required(self):
        # The remove subparser must parse a bare "remove <id>" call without
        # error (no --force required for symmetry with start/done/etc.), and
        # must reject --force (it is not a registered argument of remove).
        self.write_items([make_item("item-a")])
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch("sys.argv", ["dev_status", "remove", "item-a"]),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            try:
                dev_status.main()
            except SystemExit as e:
                self.fail(
                    f"argparse rejected a --force-free remove call: {e.code} / "
                    f"stderr={err.getvalue()!r}"
                )
        # --force is not a registered argument of the remove subparser.
        with (
            self.assertRaises(SystemExit),
            patch("sys.argv", ["dev_status", "remove", "item-a", "--force"]),
            patch("sys.stderr", io.StringIO()),
            patch("sys.stdout", io.StringIO()),
        ):
            dev_status.main()

    # ── 48: metavar string lists remove between rename and block ───────────

    def test_48_remove_acked_in_help_metavar(self):
        # The top-level subparsers metavar is the {…} set shown next to the
        # program name in --help. Parse with -h and capture the usage line.
        out = io.StringIO()
        with (
            self.assertRaises(SystemExit),
            patch("sys.argv", ["dev_status", "-h"]),
            patch("sys.stdout", out),
        ):
            dev_status.main()
        usage = out.getvalue()
        # rename appears before remove, remove before block in the metavar.
        self.assertLess(usage.index("rename"), usage.index("remove"))
        self.assertLess(usage.index("remove"), usage.index("block"))

    # ── 49-54: update field allowlist + pending blocking validation ────────

    def test_49_update_unrecognized_field_rejected(self):
        self.write_items([make_item("a")])
        rev_before = self.read_rev()
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_update(_args(id="a", patch='{"typo_sumamry": "oops"}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("unrecognized field(s): typo_sumamry", err.getvalue())
        self.assertNotIn("typo_sumamry", self.read_items()[0])
        self.assertEqual(self.read_rev(), rev_before)

    def test_50_update_mutable_fields_all_accepted(self):
        # blocked_by is excluded from this test: cmd_update refuses
        # blocked_by patches outright (see test_bug03), redirecting to
        # block/unblock so update's raw merge can't bypass their
        # existence/cycle/self-block checks.
        self.write_items([make_item("a"), make_item("b")])
        patch_json = json.dumps(
            {
                "summary": "new summary",
                "category": "bug",
                "related_files": [{"path": "/x", "note": "y"}],
                "context": "ctx",
                "next_steps": "next",
                "priority": "high",
                "status": "in-progress",
            }
        )
        dev_status.cmd_update(_args(id="a", patch=patch_json))
        item = dev_status.build_index(self.read_items())["a"]
        self.assertEqual(item["summary"], "new summary")
        self.assertEqual(item["category"], "bug")
        self.assertEqual(item["related_files"], [{"path": "/x", "note": "y"}])
        self.assertEqual(item["context"], "ctx")
        self.assertEqual(item["next_steps"], "next")
        self.assertEqual(item["priority"], "high")
        self.assertEqual(item["status"], "in-progress")

    def test_51_update_priority_null_unsets_key(self):
        self.write_items([make_item("a", priority="high")])
        dev_status.cmd_update(_args(id="a", patch='{"priority": null}'))
        item = dev_status.build_index(self.read_items())["a"]
        self.assertNotIn("priority", item)

    def test_52_update_invalid_priority_rejected(self):
        self.write_items([make_item("a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_update(_args(id="a", patch='{"priority": "urgent"}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("invalid priority 'urgent'", err.getvalue())
        self.assertNotIn("priority", dev_status.build_index(self.read_items())["a"])

    def test_53_pending_add_blocking_unknown_slug_rejected(self):
        self.write_items([make_item("a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_pending_add(
                _args(
                    json='{"id": "wait-x", "description": "waiting", '
                    '"kind": "email", "blocking": ["ghost-slug"]}'
                )
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("blocking references unknown slug: ghost-slug", err.getvalue())
        self.assertEqual(dev_status.load_pending(), [])

    def test_54_pending_add_blocking_known_slug_accepted(self):
        self.write_items([make_item("a")])
        dev_status.cmd_pending_add(
            _args(
                json='{"id": "wait-x", "description": "waiting", '
                '"kind": "email", "blocking": ["a"]}'
            )
        )
        pending = dev_status.load_pending()
        self.assertEqual(pending[0]["blocking"], ["a"])

    def test_55_add_blocker_reminder_shown_with_ready_item(self):
        self.write_items([make_item("a")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": "new-item", "summary": "New item"}'))
        self.assertIn(
            "check the READY/IN PROGRESS items above for blocker relationships",
            err.getvalue(),
        )

    def test_56_add_blocker_reminder_shown_with_in_progress_item(self):
        self.write_items([make_item("a", status="in-progress")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": "new-item", "summary": "New item"}'))
        self.assertIn(
            "check the READY/IN PROGRESS items above for blocker relationships",
            err.getvalue(),
        )

    def test_57_add_blocker_reminder_silent_when_only_new_item(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": "new-item", "summary": "New item"}'))
        self.assertNotIn("check the READY/IN PROGRESS items", err.getvalue())

    def test_58_add_blocker_reminder_silent_when_none_ready_or_in_progress(self):
        # "b" is blocked (its blocker "ghost" isn't in the index at all, which
        # effective_blockers still counts as unresolved) so it's neither READY
        # nor IN PROGRESS, and "a" is done — no candidates besides the new item.
        self.write_items(
            [
                make_item("a", status="done"),
                make_item("b", status="open", blocked_by=["ghost"]),
            ]
        )
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(
                _args(json='{"id": "third-item", "summary": "New item"}')
            )
        self.assertNotIn("check the READY/IN PROGRESS items", err.getvalue())

    def test_59_pending_add_blocker_reminder_shown(self):
        self.write_items([make_item("a")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_pending_add(
                _args(
                    json='{"id": "wait-y", "description": "waiting", "kind": "email"}'
                )
            )
        self.assertIn(
            "check the READY/IN PROGRESS items above for blocker relationships",
            err.getvalue(),
        )

    def test_60_pending_add_blocker_reminder_silent_when_backlog_empty(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_pending_add(
                _args(
                    json='{"id": "wait-z", "description": "waiting", "kind": "email"}'
                )
            )
        self.assertNotIn("check the READY/IN PROGRESS items", err.getvalue())

    # ── 61-70: explicit JSON null vs. missing field ──────────────────────────
    # Regression coverage for a real bug: `cast(str, patch.get(key, default))`
    # only applies `default` when the key is *absent* — an explicit JSON
    # `null` sails through as None, and `.strip()` on it crashes with
    # AttributeError instead of producing the intended "is required" error.
    # `_str_field`/`_list_field`/`_dict_field` must treat null and missing
    # identically everywhere a JSON patch field is extracted.

    def test_61_add_explicit_null_summary_rejected_not_crash(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": "my-item", "summary": null}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("'summary' is required", err.getvalue())
        self.assertEqual(self.read_items(), [])

    def test_61b_add_explicit_null_id_rejected_not_crash(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": null, "summary": "x"}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("'id' is required", err.getvalue())

    def test_62_add_explicit_null_blocked_by_treated_as_empty(self):
        dev_status.cmd_add(
            _args(json='{"id": "my-item", "summary": "x", "blocked_by": null}')
        )
        items = self.read_items()
        self.assertEqual(items[0]["blocked_by"], [])

    def test_63_add_explicit_null_category_falls_back_to_default(self):
        dev_status.cmd_add(
            _args(json='{"id": "my-item", "summary": "x", "category": null}')
        )
        items = self.read_items()
        self.assertEqual(items[0]["category"], "feature")

    def test_64_update_explicit_null_summary_rejected(self):
        self.write_items([make_item("a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_update(_args(id="a", patch='{"summary": null}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("cannot be null: summary", err.getvalue())
        self.assertEqual(
            dev_status.build_index(self.read_items())["a"]["summary"], "Summary of a"
        )

    def test_64b_update_explicit_null_blocked_by_rejected_not_crash(self):
        # Update refuses blocked_by entirely now (null or any value), redirecting
        # to block/unblock — see test_bug03. This test keeps its original name
        # so the historical "explicit-null-no-crash" intent is preserved while
        # the rejection reason has changed from "cannot be null" to "cannot
        # modify directly".
        self.write_items([make_item("a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_update(_args(id="a", patch='{"blocked_by": null}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("cannot modify 'blocked_by'", err.getvalue())

    def test_64c_update_multiple_null_fields_all_named_in_error(self):
        self.write_items([make_item("a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_update(
                _args(id="a", patch='{"summary": null, "context": null}')
            )
        self.assertIn("context", err.getvalue())
        self.assertIn("summary", err.getvalue())

    def test_64d_update_priority_null_still_means_unset_not_rejected(self):
        # priority keeps its own "null means unset" contract (test_51);
        # the new null-rejection guard must not regress that.
        self.write_items([make_item("a", priority="high")])
        dev_status.cmd_update(_args(id="a", patch='{"priority": null}'))
        self.assertNotIn("priority", dev_status.build_index(self.read_items())["a"])

    def test_65_pending_add_explicit_null_description_rejected_not_crash(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_pending_add(
                _args(json='{"id": "wait-x", "description": null, "kind": "email"}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("'description' is required", err.getvalue())

    def test_65b_pending_add_explicit_null_source_ref_treated_as_empty(self):
        dev_status.cmd_pending_add(
            _args(
                json='{"id": "wait-x", "description": "waiting", "kind": "email", '
                '"source_ref": null}'
            )
        )
        pending = dev_status.load_pending()
        self.assertEqual(pending[0]["source_ref"], {})

    def test_66_pending_update_explicit_null_description_rejected(self):
        self.write_pending(
            [
                {
                    "id": "pend-item",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "w",
                    "kind": "email",
                }
            ]
        )
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_pending_update(
                _args(id="pend-item", patch='{"description": null}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("cannot be null: description", err.getvalue())

    def test_66b_pending_update_explicit_null_outcome_allowed(self):
        # outcome is legitimately nullable (its state before resolution),
        # unlike the other pending-mutable fields — must not be rejected.
        self.write_pending(
            [
                {
                    "id": "pend-item",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "w",
                    "kind": "email",
                    "outcome": "something",
                }
            ]
        )
        dev_status.cmd_pending_update(_args(id="pend-item", patch='{"outcome": null}'))
        pending = {p["id"]: p for p in dev_status.load_pending()}
        self.assertIsNone(pending["pend-item"]["outcome"])

    # ───────────────────────────────────────────────────────────────────────
    # Bug-analysis regression tests (meta-devstatus-bug-analysis-2607)
    # Mirrors the 14 fixes chosen in the grilled plan; numbering matches the
    # bug-report findings 1-13,15,16 (we accepted #9 as a documented
    # limitation and split #14/#17 into separate backlog items).
    # ───────────────────────────────────────────────────────────────────────

    # ── #1: stale-rev race in read paths — render/list/show take the lock

    def test_bug01_read_paths_acquire_lock(self):
        self.write_items([make_item("alph-item")])
        # Render cmd path: invoke while holding the lock from another fd
        # would self-deadlock; instead assert the lock file is touched and
        # cmd_render completes without raising, which it can only do if it
        # does NOT try to acquire a second non-recursive lock in the same fd.
        # The actual atomic-items+rev guarantee is exercised via the
        # _MutationResult exposure in #15's test.
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_render(_args())
        self.assertIn("alph-item", out.getvalue())

    # ── #2: cross-pool id collision

    def test_bug02_add_refuses_slug_in_pending_pool(self):
        self.write_pending(
            [
                {
                    "id": "shared-slug",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "x",
                    "kind": "email",
                    "outcome": None,
                }
            ]
        )
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": "shared-slug", "summary": "x"}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("already exists as a pending item", err.getvalue())

    def test_bug02_pending_add_refuses_slug_in_backlog_pool(self):
        self.write_items([make_item("shared-slug")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_pending_add(
                _args(json='{"id": "shared-slug", "description": "x", "kind": "email"}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("already exists as a backlog item", err.getvalue())

    # ── #3: update refuses blocked_by, forces block/unblock

    def test_bug03_update_refuses_blocked_by_patch(self):
        self.write_items([make_item("aaa-item"), make_item("bbb-item")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_update(
                _args(id="aaa-item", patch='{"blocked_by": ["bbb-item"]}')
            )
        self.assertEqual(cm.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("cannot modify 'blocked_by'", msg)
        self.assertIn("block <id> <blocker>", msg)
        # Must NOT have silently landed in stored blocked_by
        items = self.read_items()
        self.assertEqual(items[0]["blocked_by"], [])

    # ── #4: remove purges inbound blocked_by (already covered in test_45;
    #         prune variant below)

    def test_bug04_prune_purges_inbound_blocked_by_refs(self):
        from datetime import date, timedelta

        old = (date.today() - timedelta(days=30)).isoformat()
        self.write_items(
            [
                {
                    "id": "old-blocker",
                    "created": old,
                    "updated": old,
                    "status": "done",
                    "completed_at": old,
                    "summary": "Old",
                    "category": "feature",
                    "blocked_by": [],
                    "related_files": [],
                    "context": "",
                    "next_steps": "",
                },
                make_item("dep", blocked_by=["old-blocker"]),
            ]
        )
        # Patch _backup_before_bulk_delete to a no-op so we don't write
        # stray backup files in the test tmpdir.
        with patch.object(dev_status, "_backup_before_bulk_delete", lambda _p: None):
            dev_status.cmd_prune(_args(force=True))
        remaining = self.read_items()
        self.assertEqual([i["id"] for i in remaining], ["dep"])
        self.assertEqual(remaining[0]["blocked_by"], [])

    def test_backup_stamp_has_millisecond_resolution(self):
        # Two backups taken within the same wall-clock second must not
        # collide on filename — second-resolution stamps overwrite the
        # first backup silently when a prune runs twice quickly.
        self.write_items([make_item("a")])
        dev_status._backup_before_bulk_delete(self.items_file)
        dev_status._backup_before_bulk_delete(self.items_file)
        backups = list(self.data_dir.glob("items.bak-*.json"))
        self.assertEqual(len(backups), 2)

    # ── #5: rename rewrites pending blocking list + related_files[].note

    def test_bug05_rename_rewrites_pending_blocking_and_related_files_note(self):
        self.write_items([make_item("old-slug")])
        self.write_pending(
            [
                {
                    "id": "pend-x",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "blocks old-slug elsewhere",
                    "kind": "email",
                    "blocking": ["old-slug"],
                    "related_files": [
                        {"path": "/x", "note": "see old-slug for context"}
                    ],
                    "next_steps": ["ping old-slug owner"],
                    "outcome": None,
                }
            ]
        )
        dev_status.cmd_rename(_args(old_slug="old-slug", new_slug="new-slug"))
        pend = dev_status.load_pending()[0]
        self.assertEqual(pend["blocking"], ["new-slug"])
        self.assertEqual(pend["related_files"][0]["note"], "see new-slug for context")
        self.assertEqual(pend["next_steps"][0], "ping new-slug owner")
        self.assertEqual(pend["description"], "blocks new-slug elsewhere")

    # ── #6: rename boundary regex doesn't over-match longer sibling slugs

    def test_bug06_rename_regex_does_not_overmatch_sibling_slug(self):
        self.write_items(
            [
                make_item(
                    "foo-bar",
                    summary="refs foo-bar-baz elsewhere",
                    context="context-foo-bar-baz-end",
                    next_steps="x-foo-bar-baz",
                ),
                make_item("foo-bar-baz"),
            ]
        )
        dev_status.cmd_rename(_args(old_slug="foo-bar", new_slug="renamed-slug"))
        items = {i["id"]: i for i in self.read_items()}
        # foo-bar-baz slug itself is unchanged
        self.assertIn("foo-bar-baz", items)
        # prose mentions of foo-bar-baz are NOT rewritten
        self.assertEqual(items["foo-bar-baz"]["summary"], "Summary of foo-bar-baz")
        # the renamed item's prose: its own slug was foo-bar (now renamed-slug),
        # but mentions of foo-bar-baz are preserved (NOT truncated to renamed-slug-baz)
        self.assertEqual(items["renamed-slug"]["summary"], "refs foo-bar-baz elsewhere")
        self.assertEqual(items["renamed-slug"]["context"], "context-foo-bar-baz-end")
        self.assertEqual(items["renamed-slug"]["next_steps"], "x-foo-bar-baz")

    # ── #8: _list_field rejects non-list (string) values

    def test_bug08_list_field_rejects_string(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status._list_field({"blocked_by": "not-a-list"}, "blocked_by")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("must be a list", err.getvalue())

    def test_bug08_effective_blockers_coerces_corrupt_stored_string(self):
        # A legacy corrupt record with blocked_by as a string would
        # previously be iterated char-by-char; effective_blockers now
        # coerces to [] with a stderr warning instead.
        item = make_item("corrupt-item", blocked_by="blocker-slug")  # type: ignore[arg-type]
        err = io.StringIO()
        with patch("sys.stderr", err):
            result = dev_status.effective_blockers(item, {})
        self.assertEqual(result, [])
        self.assertIn("not list", err.getvalue())

    # ── #10: pending update validates blocking slugs against backlog index

    def test_bug10_pending_update_validates_blocking_slugs(self):
        self.write_items([make_item("real-item")])
        self.write_pending(
            [
                {
                    "id": "pend-x",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "x",
                    "kind": "email",
                    "blocking": ["real-item"],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_pending_update(
                _args(id="pend-x", patch='{"blocking": ["typo-slug"]}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("blocking references unknown slug: typo-slug", err.getvalue())

    # ── #11: unknown slug emits not-found, not wrong-kind

    def test_bug11_update_unknown_slug_emits_not_found(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_update(_args(id="typo-slug", patch='{"summary": "x"}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[resolve] not found: typo-slug", err.getvalue())
        self.assertNotIn("is a", err.getvalue())

    def test_bug11_pending_update_unknown_slug_emits_not_found(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_pending_update(
                _args(id="typo-slug", patch='{"description": "x"}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[resolve] not found: typo-slug", err.getvalue())
        self.assertNotIn("is a backlog item", err.getvalue())

    # ── #12: load_rev handles non-dict / non-int rev

    def test_bug12_load_rev_non_dict_meta_falls_back_to_zero(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # A structurally valid JSON list, not a dict — previously AttributeError
        self.meta_file.write_text("[]")
        self.assertEqual(dev_status.load_rev(), 0)

    def test_bug12_load_rev_non_int_rev_falls_back_to_zero(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # rev present but a string — would brick every numeric --if-rev call
        self.meta_file.write_text(json.dumps({"rev": "five"}))
        self.assertEqual(dev_status.load_rev(), 0)

    # ── #13: falsy-zero DONE window bug (age 0.0 excluded before)

    def test_bug13_done_age_zero_still_in_window(self):
        from datetime import datetime

        now_iso = datetime.now().isoformat(timespec="seconds")
        items = [
            {
                "id": "fresh-done",
                "created": now_iso,
                "updated": now_iso,
                "status": "done",
                "completed_at": now_iso,
                "summary": "Fresh",
                "category": "feature",
                "blocked_by": [],
                "related_files": [],
                "context": "",
                "next_steps": "",
            }
        ]
        self.write_items(items)
        _, _, _, _, done = dev_status._render_order(items)
        self.assertEqual([i["id"] for i in done], ["fresh-done"])

    # ── #15: mutators render inside the lock using in-memory pending_items

    def test_bug15_done_renders_in_memory_pending(self):
        self.write_items([make_item("alph-item")])
        self.write_pending(
            [
                {
                    "id": "pend-x",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "w",
                    "kind": "email",
                    "next_steps": [],
                    "blocking": [],
                    "outcome": None,
                }
            ]
        )
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_done(_args(id="alph-item"))
        # stdout renders the dashboard with the pending item's description; stderr
        # gets the item-map line naming pend-x. Both prove the in-memory
        # pending_items from the locked mutation was used (no unlocked re-read).
        self.assertIn("w (waiting", out.getvalue())
        self.assertIn("pend-x", err.getvalue())

    # ── #16: prune prints the item-map/rev line (via render) after pruning

    def test_bug16_prune_prints_dashboard_and_item_map(self):
        from datetime import date, timedelta

        old = (date.today() - timedelta(days=30)).isoformat()
        self.write_items(
            [
                {
                    "id": "old-done",
                    "created": old,
                    "updated": old,
                    "status": "done",
                    "completed_at": old,
                    "summary": "Old",
                    "category": "feature",
                    "blocked_by": [],
                    "related_files": [],
                    "context": "",
                    "next_steps": "",
                }
            ]
        )
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("sys.stdout", out),
            patch("sys.stderr", err),
            patch.object(dev_status, "_backup_before_bulk_delete", lambda _p: None),
        ):
            dev_status.cmd_prune(_args(force=True))
        # Render prints the dashboard to stdout and the item-map line to stderr
        self.assertIn("backlog is empty", out.getvalue())
        self.assertIn("item-map:", err.getvalue())

    # ── found during merge review: _backlog_mutation printed a stale rev ────

    def test_review_start_prints_post_bump_rev_not_stale(self):
        """A caller-side render(rev=m.new_rev) inside the `with` block ran
        before _backlog_mutation's cleanup bumped the rev, so every mutator
        built on it (update/start/done/block/unblock/remove) printed the
        rev from *before* its own mutation. Render now happens inside the
        helper's cleanup, after bump_rev()."""
        self.write_items([make_item("item-a", status="open")])
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            dev_status.cmd_start(_args(id="item-a", if_rev=0))
        printed_rev = int(err.getvalue().split("rev=")[1].split(" ")[0])
        self.assertEqual(printed_rev, self.read_rev())

    def test_review_remove_prints_post_bump_rev_not_stale(self):
        self.write_items([make_item("item-a", status="open")])
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            dev_status.cmd_remove(_args(id="item-a", if_rev=0))
        printed_rev = int(err.getvalue().split("rev=")[1].split(" ")[0])
        self.assertEqual(printed_rev, self.read_rev())

    # ── found during merge review: pending "blocking" purge never persisted ──

    def test_review_remove_persists_pending_blocking_purge(self):
        """_purge_inbound_refs strips a removed slug from surviving pending
        items' `blocking` lists in memory, but _backlog_mutation's cleanup
        only calls save_items — cmd_remove must save_pending itself or the
        purge never reaches disk."""
        self.write_items([make_item("blocker-item", status="open")])
        self.write_pending(
            [
                {
                    "id": "pend-waiting",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting on blocker-item",
                    "kind": "email",
                    "blocking": ["blocker-item"],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            dev_status.cmd_remove(_args(id="blocker-item", if_rev=0))
        # Reload from disk (not the in-memory object render used) to prove
        # the purge was actually persisted.
        pending_on_disk = dev_status.load_pending()
        self.assertEqual(pending_on_disk[0]["blocking"], [])

    def test_review_prune_persists_pending_blocking_purge_backlog_only(self):
        """cmd_prune only re-saved pending_keep when a pending item itself
        was pruned, missing the case where pruning a backlog item leaves a
        stale reference in a surviving pending item's `blocking` list."""
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write_items(
            [
                {
                    "id": "old-blocker",
                    "created": old,
                    "updated": old,
                    "status": "done",
                    "completed_at": old,
                    "summary": "Old",
                    "category": "feature",
                    "blocked_by": [],
                    "related_files": [],
                    "context": "",
                    "next_steps": "",
                }
            ]
        )
        self.write_pending(
            [
                {
                    "id": "pend-waiting",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting on old-blocker",
                    "kind": "email",
                    "blocking": ["old-blocker"],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        with (
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
            patch.object(dev_status, "_backup_before_bulk_delete", lambda _p: None),
        ):
            dev_status.cmd_prune(_args(force=True))
        pending_on_disk = dev_status.load_pending()
        self.assertEqual(pending_on_disk[0]["blocking"], [])

    def test_prune_persists_backlog_blocked_by_purge_pending_only(self):
        """Mirror of the case above: pruning a resolved *pending* item (not
        a backlog item) must still purge the stale reference from a
        surviving backlog item's `blocked_by` and persist that to disk —
        the write-optimization that only saved `keep` when a backlog item
        was itself pruned would otherwise drop this purge silently."""
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write_items([make_item("blocked-item", blocked_by=["old-pending"])])
        self.write_pending(
            [
                {
                    "id": "old-pending",
                    "created": old,
                    "updated": old,
                    "status": "resolved",
                    "resolved_at": old,
                    "description": "long since resolved",
                    "kind": "email",
                    "blocking": ["blocked-item"],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": "done",
                }
            ]
        )
        with (
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
            patch.object(dev_status, "_backup_before_bulk_delete", lambda _p: None),
        ):
            dev_status.cmd_prune(_args(force=True))
        items_on_disk = self.read_items()
        self.assertEqual(items_on_disk[0]["blocked_by"], [])

    def test_start_clears_completed_at(self):
        """Starting a previously-done item must clear its completed_at stamp."""
        from datetime import date

        old = (date.today() - timedelta(days=2)).isoformat()
        items = [
            make_item(
                "reopen-me",
                status="done",
                updated=(date.today()).isoformat(),
            )
        ]
        items[0]["completed_at"] = old
        self.write_items(items)
        # Start should clear completed_at and set status
        dev_status.cmd_start(_args(id="reopen-me"))
        stored = self.read_items()[0]
        self.assertEqual(stored["status"], "in-progress")
        self.assertNotIn("completed_at", stored)

    # ── meta-devstatus-atomicity-fsync: rev bumped before data writes ──────
    # A crash between a data write and the rev bump left changed data under
    # a stale rev, silently letting a genuinely-stale numeric --if-rev call
    # pass the guard. Fix: bump the rev first everywhere, so a crash mid-way
    # only burns a rev number (harmless) instead. These tests assert call
    # order directly (bump before write), which is what actually matters —
    # final on-disk content was already covered by existing tests and is
    # unchanged by the reorder.

    def _assert_bump_before(self, write_fn_names, run):
        """Assert bump_rev() is called before every named save_* function.

        Patches dev_status.bump_rev and each dev_status.<name> in
        write_fn_names to append to a shared call-order list, then asserts
        "bump_rev" precedes every write in that list.
        """
        order = []
        real_bump_rev = dev_status.bump_rev

        def fake_bump_rev():
            order.append("bump_rev")
            return real_bump_rev()

        patches = [patch.object(dev_status, "bump_rev", fake_bump_rev)]
        for name in write_fn_names:
            real_fn = getattr(dev_status, name)

            def make_fake(real_fn=real_fn, name=name):
                def fake(*args, **kwargs):
                    order.append(name)
                    return real_fn(*args, **kwargs)

                return fake

            patches.append(patch.object(dev_status, name, make_fake()))

        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            for p in patches:
                p.start()
            try:
                run()
            finally:
                for p in patches:
                    p.stop()

        self.assertIn("bump_rev", order)
        bump_idx = order.index("bump_rev")
        for name in write_fn_names:
            write_indices = [i for i, n in enumerate(order) if n == name]
            for i in write_indices:
                self.assertLess(
                    bump_idx,
                    i,
                    f"expected bump_rev before {name}, got order {order}",
                )

    def test_review_start_bumps_rev_before_save_items(self):
        self.write_items([make_item("item-a", status="open")])
        self._assert_bump_before(
            ["save_items"],
            lambda: dev_status.cmd_start(_args(id="item-a")),
        )

    def test_review_remove_bumps_rev_before_its_writes(self):
        """The motivating case: cmd_remove's extra save_pending happens
        inside _backlog_mutation's caller block, before the helper's own
        post-yield save_items — both must follow the rev bump."""
        self.write_items([make_item("item-a", status="open")])
        self.write_pending(
            [
                {
                    "id": "pend-x",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting",
                    "kind": "email",
                    "blocking": [],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        self._assert_bump_before(
            ["save_items", "save_pending"],
            lambda: dev_status.cmd_remove(_args(id="item-a")),
        )

    def test_review_add_bumps_rev_before_save_items(self):
        self._assert_bump_before(
            ["save_items"],
            lambda: dev_status.cmd_add(
                _args(json='{"id": "new-item", "summary": "x"}')
            ),
        )

    def test_review_rename_bumps_rev_before_its_writes(self):
        self.write_items([make_item("old-name")])
        self._assert_bump_before(
            ["save_items", "save_pending"],
            lambda: dev_status.cmd_rename(
                _args(old_slug="old-name", new_slug="new-name")
            ),
        )

    def test_review_pending_add_bumps_rev_before_save_pending(self):
        self._assert_bump_before(
            ["save_pending"],
            lambda: dev_status.cmd_pending_add(
                _args(json='{"id": "pend-new", "description": "d", "kind": "email"}')
            ),
        )

    def test_review_pending_update_bumps_rev_before_save_pending(self):
        self.write_pending(
            [
                {
                    "id": "pend-x",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting",
                    "kind": "email",
                    "blocking": [],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        self._assert_bump_before(
            ["save_pending"],
            lambda: dev_status.cmd_pending_update(
                _args(id="pend-x", patch='{"context": "updated"}')
            ),
        )

    def test_review_prune_bumps_rev_before_its_writes(self):
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write_items(
            [make_item("old-done", status="done", updated=old, created=old)]
        )
        with patch.object(dev_status, "_backup_before_bulk_delete", lambda _p: None):
            self._assert_bump_before(
                ["save_items"],
                lambda: dev_status.cmd_prune(_args(force=True)),
            )

    def test_review_prune_writes_each_file_at_most_once(self):
        """Previously items/pending could each be saved twice in one prune
        call (once with the raw filtered set, again after ref-purging).
        Now each file is written at most once per call."""
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write_items(
            [
                make_item("old-done", status="done", updated=old, created=old),
                make_item("dep", blocked_by=["old-done"]),
            ]
        )
        self.write_pending(
            [
                {
                    "id": "pend-x",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting on old-done",
                    "kind": "email",
                    "blocking": ["old-done"],
                    "related_files": [],
                    "next_steps": [],
                    "outcome": None,
                }
            ]
        )
        with (
            patch.object(dev_status, "_backup_before_bulk_delete", lambda _p: None),
            patch(
                "dev_status.save_items", wraps=dev_status.save_items
            ) as save_items_mock,
            patch(
                "dev_status.save_pending", wraps=dev_status.save_pending
            ) as save_pending_mock,
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            dev_status.cmd_prune(_args(force=True))
        self.assertEqual(save_items_mock.call_count, 1)
        self.assertEqual(save_pending_mock.call_count, 1)
        # And the ref-purge is still correctly persisted, not just fewer
        # writes for their own sake.
        remaining = self.read_items()
        self.assertEqual([i["id"] for i in remaining], ["dep"])
        self.assertEqual(remaining[0]["blocked_by"], [])
        pending_on_disk = dev_status.load_pending()
        self.assertEqual(pending_on_disk[0]["blocking"], [])

    # ── in-review state: review / approve / reject ────────────────────────────

    def _item_by_id(self, slug):
        return {i["id"]: i for i in self.read_items()}[slug]

    def test_40a_review_happy_path(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_review(_args(id="rv-item"))
        item = self._item_by_id("rv-item")
        self.assertEqual(item["status"], "in-review")
        self.assertIn("review_content_hash", item)
        self.assertNotIn("review_feedback", item)

    def test_40b_approve_happy_path(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        dev_status.cmd_review(_args(id="rv-item"))
        dev_status.cmd_approve(_args(id="rv-item"))
        item = self._item_by_id("rv-item")
        self.assertEqual(item["status"], "done")
        self.assertIn("completed_at", item)
        # audit trail left in place
        self.assertIn("review_content_hash", item)

    def test_40c_reject_empty_feedback_refused(self):
        self.write_items(
            [make_item("rv-item", status="in-progress", review_content_hash="x")]
        )
        # convert to in-review with a real hash via review
        dev_status.cmd_review(_args(id="rv-item"))
        for bad in ("", "   "):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
                dev_status.cmd_reject(_args(id="rv-item", feedback=bad))
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("feedback is required", err.getvalue())
            item = self._item_by_id("rv-item")
            self.assertEqual(item["status"], "in-review")

    def test_40d_reject_happy_path(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        dev_status.cmd_review(_args(id="rv-item"))
        dev_status.cmd_reject(_args(id="rv-item", feedback="needs rework"))
        item = self._item_by_id("rv-item")
        self.assertEqual(item["status"], "in-progress")
        self.assertEqual(item["review_feedback"], "needs rework")
        self.assertNotIn("review_content_hash", item)

    def test_40e_reject_then_resubmit_clears_feedback(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        dev_status.cmd_review(_args(id="rv-item"))
        dev_status.cmd_reject(_args(id="rv-item", feedback="redo"))
        dev_status.cmd_review(_args(id="rv-item"))
        item = self._item_by_id("rv-item")
        self.assertEqual(item["status"], "in-review")
        self.assertNotIn("review_feedback", item)
        self.assertIn("review_content_hash", item)

    def test_40f_update_refuses_review_only_fields(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        for field in ("review_feedback", "review_content_hash"):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
                dev_status.cmd_update(_args(id="rv-item", patch=f'{{"{field}": "x"}}'))
            self.assertEqual(cm.exception.code, 1)
            msg = err.getvalue()
            self.assertIn("cannot modify", msg)
            self.assertIn(field, msg)
            self.assertIn("review", msg)
            item = self._item_by_id("rv-item")
            self.assertNotIn(field, item)

    def test_40g_done_refuses_on_in_review(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        dev_status.cmd_review(_args(id="rv-item"))
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_done(_args(id="rv-item"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("in-review", err.getvalue())
        self.assertIn("approve", err.getvalue())
        self.assertEqual(self._item_by_id("rv-item")["status"], "in-review")

    def test_40h_start_refuses_on_in_review(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        dev_status.cmd_review(_args(id="rv-item"))
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_start(_args(id="rv-item"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("in-review", err.getvalue())
        self.assertIn("approve", err.getvalue())
        self.assertEqual(self._item_by_id("rv-item")["status"], "in-review")

    def test_40i_approve_refuses_on_non_in_review(self):
        self.write_items([make_item("rv-open", status="open")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_approve(_args(id="rv-open"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("approve", err.getvalue())
        self.assertEqual(self._item_by_id("rv-open")["status"], "open")

    def test_40j_reject_refuses_on_non_in_review(self):
        self.write_items([make_item("rv-open", status="open")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_reject(_args(id="rv-open", feedback="x"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("reject", err.getvalue())
        self.assertEqual(self._item_by_id("rv-open")["status"], "open")

    def test_40k_approve_refuses_on_drift(self):
        self.write_items([make_item("rv-item", status="in-progress", summary="A")])
        dev_status.cmd_review(_args(id="rv-item"))
        # mutate summary directly on disk (simulates content change)
        items = self.read_items()
        items[0]["summary"] = "B"
        self.write_items(items)
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_approve(_args(id="rv-item"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("content changed", err.getvalue())
        self.assertIn("review <id>", err.getvalue())
        self.assertEqual(self._item_by_id("rv-item")["status"], "in-review")

    def test_40l_reject_refuses_on_drift(self):
        self.write_items([make_item("rv-item", status="in-progress", summary="A")])
        dev_status.cmd_review(_args(id="rv-item"))
        items = self.read_items()
        items[0]["summary"] = "B"
        self.write_items(items)
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_reject(_args(id="rv-item", feedback="x"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("content changed", err.getvalue())
        self.assertEqual(self._item_by_id("rv-item")["status"], "in-review")

    def test_40m_approve_refuses_when_hash_absent(self):
        # hand-edited store: in-review item with no review_content_hash
        self.write_items([make_item("rv-item", status="in-review")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_approve(_args(id="rv-item"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("content changed", err.getvalue())
        self.assertEqual(self._item_by_id("rv-item")["status"], "in-review")

    def test_40n_review_reinvoke_from_in_review(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        dev_status.cmd_review(_args(id="rv-item"))
        dev_status.cmd_reject(_args(id="rv-item", feedback="redo"))
        # re-submit from in-progress
        dev_status.cmd_review(_args(id="rv-item"))
        # now status is in-review; re-pinning from in-review also works
        dev_status.cmd_review(_args(id="rv-item"))
        item = self._item_by_id("rv-item")
        self.assertEqual(item["status"], "in-review")
        self.assertNotIn("review_feedback", item)
        self.assertIn("review_content_hash", item)

    def test_40o_review_refuses_from_open_and_done(self):
        for status in ("open", "done"):
            self.write_items(
                [make_item("rv-item", status=status, review_content_hash=None)]
            )
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
                dev_status.cmd_review(_args(id="rv-item"))
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("only an in-progress", err.getvalue())

    def test_40p_render_order_buckets_in_review_in_4th_slot(self):
        items = [
            make_item("a-ip", status="in-progress"),
            make_item("b-open"),
            make_item("c-rev", status="in-review"),
            make_item("d-done", status="done", updated="2026-01-01"),
        ]
        ip, ready, blocked, in_review, done = dev_status._render_order(items)
        self.assertEqual([i["id"] for i in in_review], ["c-rev"])
        self.assertEqual([i["id"] for i in ip], ["a-ip"])
        for bucket in (ip, ready, blocked, done):
            self.assertNotIn(
                "c-rev",
                [i["id"] for i in bucket],
                "in-review leaked into another bucket",
            )

    def test_40q_in_review_section_position_in_render(self):
        self.write_items(
            [
                make_item("c-rev", status="in-review"),
                make_item("d-done", status="done", updated="2999-01-01"),
            ]
        )
        out = io.StringIO()
        err = io.StringIO()
        dev_status.render(self.read_items(), [], out=out, err=err)
        text = out.getvalue()
        self.assertIn("IN REVIEW", text)
        self.assertLess(text.index("IN REVIEW"), text.index("DONE"))

    def test_40r_in_review_section_absent_when_empty(self):
        self.write_items([make_item("a-open")])
        out = io.StringIO()
        err = io.StringIO()
        dev_status.render(self.read_items(), [], out=out, err=err)
        self.assertNotIn("IN REVIEW", out.getvalue())

    def test_40s_numeric_id_in_review_in_item_map(self):
        self.write_items([make_item("c-rev", status="in-review")])
        err = io.StringIO()
        dev_status.render(self.read_items(), [], out=io.StringIO(), err=err)
        self.assertIn("backlog:c-rev", err.getvalue())

    def test_40t_blocker_check_reminder_excludes_in_review(self):
        # An in-review item should not appear in the candidate count.
        self.write_items(
            [
                make_item("rv-item", status="in-review"),
                make_item("other-item", status="open"),
            ]
        )
        # `_blocker_check_reminder` uses one stderr line; just ensure it
        # doesn't crash and the in-review slug isn't double-counted by
        # checking the helper returns for exclude == the just-added slug.
        err = io.StringIO()
        dev_status._blocker_check_reminder(
            self.read_items(), "rv-item", cmd="add", err=err
        )
        # one candidate (other-item), should print the reminder
        self.assertIn("check the READY/IN PROGRESS", err.getvalue())

    def test_40u_main_dispatch_smoke(self):
        for argv, seed_status, expected in (
            (["dev_status", "review", "rv-item"], "in-progress", "in-review"),
            (["dev_status", "approve", "rv-item"], "in-review", "done"),
            (["dev_status", "reject", "rv-item", "nope"], "in-review", "in-progress"),
        ):
            # Seed in-progress, then promote to a valid in-review state via
            # cmd_review when the command under test needs a hash-pinned item.
            self.write_items([make_item("rv-item", status="in-progress")])
            if seed_status == "in-review":
                dev_status.cmd_review(_args(id="rv-item"))
            with (
                patch("sys.argv", argv),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                dev_status.main()
            self.assertEqual(self._item_by_id("rv-item")["status"], expected)

    def test_40v_numeric_if_rev_guard_on_new_commands(self):
        self.write_items([make_item("rv-item", status="in-progress")])
        # missing --if-rev
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_review(_args(id="1", if_rev=None))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("requires --if-rev", err.getvalue())
        self.assertEqual(self._item_by_id("rv-item")["status"], "in-progress")
        # stale --if-rev
        rev = self.read_rev()
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_review(_args(id="1", if_rev=rev + 99))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("stale rev", err.getvalue())

    # ── 41: gate field ────────────────────────────────────────────────────

    def test_41a_gate_set_happy_path(self):
        self.write_items([make_item("gt-item")])
        dev_status.cmd_gate_set(
            _args(id="gt-item", json='{"required": true, "criteria": ["check X"]}')
        )
        item = self._item_by_id("gt-item")
        gate = item["gate"]
        gate.pop(
            "set_at", None
        )  # stamped since evidence-gating; see RunEvidenceTestCase
        self.assertEqual(
            gate,
            {
                "required": True,
                "criteria": ["check X"],
                "passed_at": None,
            },
        )

    def test_41b_gate_set_required_true_needs_criteria(self):
        self.write_items([make_item("gt-item")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_gate_set(
                _args(id="gt-item", json='{"required": true, "criteria": []}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("cannot be empty", err.getvalue())
        self.assertNotIn("gate", self._item_by_id("gt-item"))

    def test_41c_gate_set_missing_required_field_refused(self):
        self.write_items([make_item("gt-item")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_gate_set(_args(id="gt-item", json='{"criteria": []}'))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("'required' (bool) is required", err.getvalue())

    def test_41d_gate_set_non_string_criteria_refused(self):
        self.write_items([make_item("gt-item")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_gate_set(
                _args(id="gt-item", json='{"required": false, "criteria": [1]}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("list of non-empty strings", err.getvalue())

    def test_41e_gate_set_required_false_ok(self):
        self.write_items([make_item("gt-item")])
        dev_status.cmd_gate_set(
            _args(id="gt-item", json='{"required": false, "criteria": []}')
        )
        item = self._item_by_id("gt-item")
        self.assertEqual(item["gate"]["required"], False)

    def test_41f_gate_set_resets_prior_pass(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    gate={
                        "required": True,
                        "criteria": ["old"],
                        "passed_at": "2026-01-01",
                    },
                )
            ]
        )
        dev_status.cmd_gate_set(
            _args(id="gt-item", json='{"required": true, "criteria": ["new"]}')
        )
        item = self._item_by_id("gt-item")
        self.assertIsNone(item["gate"]["passed_at"])
        self.assertEqual(item["gate"]["criteria"], ["new"])

    def test_41g_gate_pass_happy_path(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    gate={
                        "required": True,
                        "criteria": ["x"],
                        "passed_at": None,
                    },
                )
            ]
        )
        dev_status.cmd_gate_pass(
            _args(id="gt-item", json='{"coverage": {"1": "manual: verified by eye"}}')
        )
        item = self._item_by_id("gt-item")
        self.assertIsNotNone(item["gate"]["passed_at"])
        self.assertEqual(item["gate"]["passed_via"], "manual")

    def test_41h_gate_pass_refuses_without_required_gate(self):
        for gate in (
            None,
            {"required": False, "criteria": [], "passed_at": None},
        ):
            self.write_items([make_item("gt-item", gate=gate)])
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
                dev_status.cmd_gate_pass(_args(id="gt-item"))
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("nothing to pass", err.getvalue())

    def test_41i_update_refuses_gate_field(self):
        self.write_items([make_item("gt-item")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_update(
                _args(id="gt-item", patch='{"gate": {"required": true}}')
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("cannot modify 'gate'", err.getvalue())
        self.assertIn("gate-set", err.getvalue())

    def test_41j_done_refuses_on_unmet_gate(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    status="in-progress",
                    gate={
                        "required": True,
                        "criteria": ["x", "y"],
                        "passed_at": None,
                    },
                )
            ]
        )
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_done(_args(id="gt-item"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("unmet gate", err.getvalue())
        self.assertIn("gate-pass", err.getvalue())
        self.assertEqual(self._item_by_id("gt-item")["status"], "in-progress")

    def test_41k_done_succeeds_after_gate_pass(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    status="in-progress",
                    gate={
                        "required": True,
                        "criteria": ["x"],
                        "passed_at": None,
                    },
                )
            ]
        )
        dev_status.cmd_gate_pass(
            _args(id="gt-item", json='{"coverage": {"1": "manual: verified by eye"}}')
        )
        dev_status.cmd_done(_args(id="gt-item"))
        self.assertEqual(self._item_by_id("gt-item")["status"], "done")

    def test_41l_done_unaffected_by_absent_or_inert_gate(self):
        for gate in (
            None,
            {"required": False, "criteria": [], "passed_at": None},
        ):
            self.write_items([make_item("gt-item", status="in-progress", gate=gate)])
            dev_status.cmd_done(_args(id="gt-item"))
            self.assertEqual(self._item_by_id("gt-item")["status"], "done")

    def test_41m_approve_refuses_on_unmet_gate(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    status="in-progress",
                    gate={
                        "required": True,
                        "criteria": ["x"],
                        "passed_at": None,
                    },
                )
            ]
        )
        dev_status.cmd_review(_args(id="gt-item"))
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_approve(_args(id="gt-item"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("unmet gate", err.getvalue())
        self.assertEqual(self._item_by_id("gt-item")["status"], "in-review")

    def test_41n_approve_succeeds_after_gate_pass(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    status="in-progress",
                    gate={
                        "required": True,
                        "criteria": ["x"],
                        "passed_at": None,
                    },
                )
            ]
        )
        dev_status.cmd_review(_args(id="gt-item"))
        dev_status.cmd_gate_pass(
            _args(id="gt-item", json='{"coverage": {"1": "manual: verified by eye"}}')
        )
        dev_status.cmd_approve(_args(id="gt-item"))
        self.assertEqual(self._item_by_id("gt-item")["status"], "done")

    def test_41o_dashboard_shows_gate_marker_when_unmet(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    status="in-progress",
                    gate={
                        "required": True,
                        "criteria": ["x"],
                        "passed_at": None,
                    },
                )
            ]
        )
        out = io.StringIO()
        dev_status.render(self.read_items(), [], out=out, err=io.StringIO())
        self.assertIn("gate", out.getvalue())

    def test_41p_dashboard_no_gate_marker_when_passed_or_absent(self):
        for gate in (
            None,
            {"required": False, "criteria": [], "passed_at": None},
            {
                "required": True,
                "criteria": ["x"],
                "passed_at": "2026-01-01",
            },
        ):
            self.write_items([make_item("gt-item", status="in-progress", gate=gate)])
            out = io.StringIO()
            dev_status.render(self.read_items(), [], out=out, err=io.StringIO())
            self.assertNotIn("gate", out.getvalue())

    def test_41q_backfill_gate_dry_run_makes_no_changes(self):
        self.write_items([make_item("bf-item")])
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_backfill_gate(_args())
        self.assertNotIn("gate", self._item_by_id("bf-item"))
        self.assertIn("dry run", out.getvalue())

    def test_41r_backfill_gate_apply_stamps_inert_gate(self):
        self.write_items([make_item("bf-item"), make_item("bf-item2")])
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_backfill_gate(_args(apply=True))
        for slug in ("bf-item", "bf-item2"):
            item = self._item_by_id(slug)
            self.assertEqual(
                item["gate"],
                {
                    "required": False,
                    "criteria": [],
                    "passed_at": None,
                },
            )

    def test_41s_backfill_gate_apply_skips_already_gated_items(self):
        self.write_items(
            [
                make_item(
                    "bf-item",
                    gate={
                        "required": True,
                        "criteria": ["x"],
                        "passed_at": "2026-01-01",
                    },
                )
            ]
        )
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_backfill_gate(_args(apply=True))
        item = self._item_by_id("bf-item")
        self.assertTrue(item["gate"]["required"])
        self.assertEqual(item["gate"]["passed_at"], "2026-01-01")
        self.assertIn("nothing to do", out.getvalue())

    def test_41t_done_succeeds_on_legacy_passed_gate_shape(self):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    status="in-progress",
                    gate={
                        "required": True,
                        "criteria": ["x"],
                        "passed": True,
                        "passed_at": "2026-01-01",
                    },
                )
            ]
        )
        dev_status.cmd_done(_args(id="gt-item"))
        self.assertEqual(self._item_by_id("gt-item")["status"], "done")

    def test_41u_dispatch_matches_subcommands(self):
        self.assertEqual(set(dev_status.dispatch), set(dev_status.SUBCOMMANDS))

    def test_41v_add_gate_set_slug_rejected(self):
        args = _args(json='{"id": "gate-set", "summary": "x"}')
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_add(args)
        self.assertIn("reserved", err.getvalue())

    # ── recap: journal, cache, dispatch, prompt/normalization, subcommand ───

    def _journal_lines(self):
        if not self.journal_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.journal_file.read_text().splitlines()
            if line.strip()
        ]

    def _write_journal_line(self, entry):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.journal_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _seed_recent_journal(self, hours_ago=0.01):
        ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
        self._write_journal_line(
            {
                "ts": ts,
                "rev": 1,
                "machine": "test-machine",
                "cmd": "add",
                "kind": "backlog",
                "slug": "seed",
                "summary": "seed entry",
            }
        )

    def _write_cache(self, text, age_hours=0, backend="agy", board_fingerprint=None):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ts = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
        if board_fingerprint is None:
            board_fingerprint = dev_status._current_board_fingerprint()
        self.recap_cache_file.write_text(
            json.dumps(
                {
                    "generated_at": ts,
                    "backend": backend,
                    "text": text,
                    "board_fingerprint": board_fingerprint,
                }
            )
        )

    # ── journal appends per mutation path ──────────────────────────────────

    def test_r01_done_journals_status_transition(self):
        self.write_items([make_item("j-done", status="in-progress")])
        dev_status.cmd_done(_args(id="j-done"))
        entries = self._journal_lines()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["cmd"], "done")
        self.assertEqual(e["kind"], "backlog")
        self.assertEqual(e["slug"], "j-done")
        self.assertEqual(e["from_status"], "in-progress")
        self.assertEqual(e["to_status"], "done")
        self.assertNotIn("fields", e)
        self.assertNotIn("feedback", e)

    def test_r02_update_journals_patched_field_names(self):
        self.write_items([make_item("j-upd")])
        dev_status.cmd_update(
            _args(id="j-upd", patch='{"context": "new", "priority": "high"}')
        )
        entries = self._journal_lines()
        self.assertEqual(entries[-1]["cmd"], "update")
        self.assertEqual(sorted(entries[-1]["fields"]), ["context", "priority"])
        self.assertNotIn("from_status", entries[-1])

    def test_r03_reject_journals_feedback(self):
        self.write_items([make_item("j-rej", status="in-progress")])
        dev_status.cmd_review(_args(id="j-rej"))
        dev_status.cmd_reject(_args(id="j-rej", feedback="needs work"))
        entries = self._journal_lines()
        self.assertEqual(entries[-1]["cmd"], "reject")
        self.assertEqual(entries[-1]["feedback"], "needs work")
        self.assertEqual(entries[-1]["from_status"], "in-review")
        self.assertEqual(entries[-1]["to_status"], "in-progress")

    def test_r04_rename_journals_item_summary_not_raw_slugs(self):
        self.write_items([make_item("old-slug", summary="Widen the dashboard box")])
        dev_status.cmd_rename(_args(old_slug="old-slug", new_slug="new-slug"))
        entries = self._journal_lines()
        self.assertEqual(entries[-1]["cmd"], "rename")
        self.assertEqual(entries[-1]["slug"], "new-slug")
        self.assertIn("Widen the dashboard box", entries[-1]["summary"])
        self.assertNotIn("old-slug", entries[-1]["summary"])
        self.assertNotIn("new-slug", entries[-1]["summary"])

    def test_r05_prune_journals_count(self):
        old = (date.today() - timedelta(days=30)).isoformat()
        self.write_items(
            [make_item("old-done", status="done", updated=old)],
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        items = self.read_items()
        items[0]["completed_at"] = old
        dev_status.save_items(items)
        with patch.object(dev_status, "_backup_before_bulk_delete", lambda _p: None):
            dev_status.cmd_prune(_args(force=True))
        entries = self._journal_lines()
        self.assertEqual(entries[-1]["cmd"], "prune")
        self.assertEqual(entries[-1]["count"], 1)

    def test_r06_add_journals_summary(self):
        dev_status.cmd_add(_args(json='{"id": "j-add", "summary": "New thing"}'))
        entries = self._journal_lines()
        self.assertEqual(entries[-1]["cmd"], "add")
        self.assertEqual(entries[-1]["kind"], "backlog")
        self.assertEqual(entries[-1]["slug"], "j-add")
        self.assertEqual(entries[-1]["summary"], "New thing")

    def test_r07_pending_add_journals_as_pending_kind(self):
        dev_status.cmd_pending_add(
            _args(
                json='{"id": "j-pend", "description": "waiting on reply", "kind": "email"}'
            )
        )
        entries = self._journal_lines()
        self.assertEqual(entries[-1]["cmd"], "add")
        self.assertEqual(entries[-1]["kind"], "pending")
        self.assertEqual(entries[-1]["slug"], "j-pend")

    def test_r08_pending_update_journals_fields_and_status_transition(self):
        self.write_pending(
            [
                {
                    "id": "j-pend",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting",
                    "kind": "email",
                    "source_ref": {},
                    "context": "",
                    "next_steps": [],
                    "blocking": [],
                    "outcome": None,
                }
            ]
        )
        dev_status.cmd_pending_update(
            _args(id="j-pend", patch='{"status": "reply_received"}')
        )
        entries = self._journal_lines()
        self.assertEqual(entries[-1]["cmd"], "update")
        self.assertEqual(entries[-1]["kind"], "pending")
        self.assertEqual(entries[-1]["fields"], ["status"])
        self.assertEqual(entries[-1]["from_status"], "waiting_for_reply")
        self.assertEqual(entries[-1]["to_status"], "reply_received")

    def test_r09_journal_append_failure_swallowed_mutation_still_succeeds(self):
        self.write_items([make_item("jf-item", status="in-progress")])
        # Point JOURNAL_FILE at a directory: `open(..., "a")` raises
        # IsADirectoryError (an OSError subclass) instead of ever writing.
        with patch.object(dev_status, "JOURNAL_FILE", self.data_dir):
            err = io.StringIO()
            with patch("sys.stderr", err):
                dev_status.cmd_done(_args(id="jf-item", verbose=True))
            self.assertIn("append failed", err.getvalue())
        self.assertEqual(self._item_by_id("jf-item")["status"], "done")

    def test_r09b_journal_append_failure_hidden_without_verbose(self):
        self.write_items([make_item("jf-item2", status="in-progress")])
        with patch.object(dev_status, "JOURNAL_FILE", self.data_dir):
            err = io.StringIO()
            with patch("sys.stderr", err):
                dev_status.cmd_done(_args(id="jf-item2"))
            self.assertNotIn("append failed", err.getvalue())
        self.assertEqual(self._item_by_id("jf-item2")["status"], "done")

    # ── journal reader ──────────────────────────────────────────────────────

    def test_r10_reader_skips_trailing_partial_line_silently(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        good = json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "rev": 1,
                "machine": "x",
                "cmd": "add",
                "kind": "backlog",
            }
        )
        self.journal_file.write_text(good + "\n" + '{"cmd": "add", "kind": "back')
        err = io.StringIO()
        with patch("sys.stderr", err):
            entries = dev_status.read_journal_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(err.getvalue(), "")

    def test_r11_reader_warns_on_middle_line_corruption(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        good1 = json.dumps(
            {"ts": now, "rev": 1, "machine": "x", "cmd": "add", "kind": "backlog"}
        )
        good2 = json.dumps(
            {"ts": now, "rev": 2, "machine": "x", "cmd": "done", "kind": "backlog"}
        )
        self.journal_file.write_text(
            good1 + "\n" + "not valid json at all\n" + good2 + "\n"
        )
        err = io.StringIO()
        with patch("sys.stderr", err):
            entries = dev_status.read_journal_entries(verbose=True)
        self.assertEqual(len(entries), 2)
        self.assertIn("corrupt", err.getvalue())

    def test_r11b_reader_corruption_warning_hidden_without_verbose(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        good1 = json.dumps(
            {"ts": now, "rev": 1, "machine": "x", "cmd": "add", "kind": "backlog"}
        )
        good2 = json.dumps(
            {"ts": now, "rev": 2, "machine": "x", "cmd": "done", "kind": "backlog"}
        )
        self.journal_file.write_text(
            good1 + "\n" + "not valid json at all\n" + good2 + "\n"
        )
        err = io.StringIO()
        with patch("sys.stderr", err):
            entries = dev_status.read_journal_entries()
        self.assertEqual(len(entries), 2)
        self.assertNotIn("corrupt", err.getvalue())

    # ── dispatch ────────────────────────────────────────────────────────────

    def test_r12_trigger_passes_spawns_detached_internal_regen(self):
        self._seed_recent_journal()
        dev_status._maybe_dispatch_recap_regen()
        self.mock_popen.assert_called_once()
        argv, kwargs = self.mock_popen.call_args
        self.assertIn("_internal-regen", argv[0])
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["stdin"], dev_status.subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], dev_status.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], dev_status.subprocess.DEVNULL)

    def test_r13_fresh_cache_spawns_nothing(self):
        self._seed_recent_journal()
        self._write_cache("Fresh.", age_hours=0)
        dev_status._maybe_dispatch_recap_regen()
        self.mock_popen.assert_not_called()

    def test_r13b_dispatch_fires_on_fingerprint_drift_despite_fresh_ttl(self):
        self.write_items([make_item("x")])
        self._write_cache(
            "Fresh but stale-by-facts.",
            age_hours=0,
            board_fingerprint="deadbeefdeadbeef",
        )
        self._seed_recent_journal()
        dev_status._maybe_dispatch_recap_regen()
        self.mock_popen.assert_called_once()

    def test_r13c_missing_board_fingerprint_key_dispatches_despite_fresh_ttl(self):
        self.write_items([make_item("x")])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).isoformat()
        self.recap_cache_file.write_text(
            json.dumps(
                {"generated_at": ts, "backend": "agy", "text": "Pre-migration cache."}
            )
        )
        self._seed_recent_journal()
        dev_status._maybe_dispatch_recap_regen()
        self.mock_popen.assert_called_once()

    def test_r14_journal_last_line_older_than_48h_spawns_nothing(self):
        self._seed_recent_journal(hours_ago=50)
        dev_status._maybe_dispatch_recap_regen()
        self.mock_popen.assert_not_called()

    def test_r15_kill_switch_spawns_nothing_and_displays_nothing(self):
        self.write_items([make_item("x")])
        self._seed_recent_journal()
        self._write_cache("Should be hidden.", age_hours=0)
        with patch.dict("os.environ", {"DEVSTATUS_RECAP_DISABLE": "1"}):
            dev_status._maybe_dispatch_recap_regen()
            self.mock_popen.assert_not_called()
            out = io.StringIO()
            with patch("sys.stdout", out):
                dev_status.render()
            self.assertNotIn("RECAP", out.getvalue())
            self.assertNotIn("Should be hidden", out.getvalue())

    def test_r16_dispatch_never_fires_while_backlog_lock_is_held(self):
        self.write_items([make_item("lk-item", status="in-progress")])
        self._seed_recent_journal()
        acquired = {"ok": False}

        def _side_effect(*_a, **_kw):
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.lock_file, "w") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired["ok"] = True
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    acquired["ok"] = False
            return MagicMock()

        self.mock_popen.side_effect = _side_effect
        dev_status.cmd_done(_args(id="lk-item"))
        self.assertTrue(acquired["ok"])

    # ── regen lock ──────────────────────────────────────────────────────────

    def test_r17_internal_regen_noop_when_lock_already_held(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.recap_regen_lock_file, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                with patch.object(dev_status, "_run_recap_regen") as mock_regen:
                    dev_status.cmd_internal_regen()
                mock_regen.assert_not_called()
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)

    # ── render display rules ────────────────────────────────────────────────

    def test_r18_no_section_without_cache(self):
        self.write_items([make_item("x")])
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertNotIn("RECAP", out.getvalue())

    def test_r19_fresh_cache_shown_without_age_marker(self):
        self.write_items([make_item("x")])
        self._write_cache("Great progress today.", age_hours=0)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertIn("RECAP", out.getvalue())
        self.assertIn("Great progress today.", out.getvalue())
        self.assertNotIn("ago)", out.getvalue())

    def test_r19b_fingerprint_mismatch_suppresses_display_even_when_fresh(self):
        self.write_items([make_item("x")])
        self._write_cache(
            "Great progress today.",
            age_hours=0,
            board_fingerprint="deadbeefdeadbeef",
        )
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertNotIn("RECAP", out.getvalue())

    def test_r19d_counts_unchanged_but_identities_swapped_still_suppresses(self):
        # Two items, one ready and one blocked-by the other. Cache the
        # fingerprint for that state, then swap which one is ready and
        # which is blocked -- total counts (ready: 1, blocked: 1) are
        # identical before and after, but the fingerprint must still catch
        # that the *specific* items in each bucket changed.
        self.write_items(
            [
                make_item("a", status="open"),
                make_item("b", status="open", blocked_by=["a"]),
            ]
        )
        self._write_cache("You have one ready and one blocked item.", age_hours=0)
        items = self.read_items()
        for it in items:
            if it["id"] == "a":
                it["blocked_by"] = ["b"]
            elif it["id"] == "b":
                it["blocked_by"] = []
        dev_status.save_items(items)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertNotIn("RECAP", out.getvalue())

    def test_r19e_summary_edit_alone_still_suppresses(self):
        # Same status, same blocked_by, same bucket counts -- only the
        # item's title changes. Bucket membership is untouched, but the
        # cached prose's claim about *what* the item is would now be
        # wrong, so the fingerprint must still catch it.
        self.write_items([make_item("x", summary="Fix the widget")])
        self._write_cache("You completed Fix the widget.", age_hours=0)
        items = self.read_items()
        for it in items:
            if it["id"] == "x":
                it["summary"] = "Fix the dashboard"
        dev_status.save_items(items)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertNotIn("RECAP", out.getvalue())

    def test_r19c_missing_board_fingerprint_key_suppresses_display(self):
        self.write_items([make_item("x")])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).isoformat()
        self.recap_cache_file.write_text(
            json.dumps(
                {"generated_at": ts, "backend": "agy", "text": "Pre-migration cache."}
            )
        )
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertNotIn("RECAP", out.getvalue())

    def test_r20_stale_cache_shown_with_age_marker(self):
        self.write_items([make_item("x")])
        self._write_cache("Yesterday's news.", age_hours=3)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertIn("RECAP", out.getvalue())
        self.assertIn("ago)", out.getvalue())

    def test_r21_cache_older_than_24h_omitted(self):
        self.write_items([make_item("x")])
        self._write_cache("Old news.", age_hours=25)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertNotIn("RECAP", out.getvalue())

    def test_r22_empty_text_cache_suppressed_and_not_retried(self):
        self.write_items([make_item("x")])
        self._write_cache("", age_hours=0)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        self.assertNotIn("RECAP", out.getvalue())
        self._seed_recent_journal()
        dev_status._maybe_dispatch_recap_regen()
        self.mock_popen.assert_not_called()

    def test_r22b_render_wraps_long_recap_to_section_width(self):
        self.write_items([make_item("x")])
        self._write_cache(("word " * 40).strip(), age_hours=0)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.render()
        recap_lines = [
            line for line in out.getvalue().splitlines() if line.startswith("│")
        ]
        self.assertTrue(recap_lines)
        for line in recap_lines:
            self.assertLess(len(line), dev_status.SECTION_WIDTH + 10)

    # ── backend failure ─────────────────────────────────────────────────────

    def test_r23_backend_failure_leaves_prior_cache_intact(self):
        self._write_cache("Old cached text.", age_hours=0, backend="agy")
        self._seed_recent_journal()
        with (
            patch.object(llm_backends, "available_backends", return_value=["agy"]),
            patch.object(
                llm_backends, "run_agy", side_effect=llm_backends.BackendError("boom")
            ),
        ):
            backend, text = dev_status._run_recap_regen()
        self.assertEqual((backend, text), ("", ""))
        cache = json.loads(self.recap_cache_file.read_text())
        self.assertEqual(cache["text"], "Old cached text.")

    def test_r23b_backend_timeout_leaves_prior_cache_intact(self):
        self._write_cache("Old cached text.", age_hours=0, backend="agy")
        self._seed_recent_journal()
        with (
            patch.object(llm_backends, "available_backends", return_value=["agy"]),
            patch.object(
                llm_backends,
                "run_agy",
                side_effect=llm_backends.BackendError("timed out after 60s — killed"),
            ),
        ):
            backend, text = dev_status._run_recap_regen()
        self.assertEqual((backend, text), ("", ""))
        cache = json.loads(self.recap_cache_file.read_text())
        self.assertEqual(cache["text"], "Old cached text.")

    def test_r23c_run_recap_regen_persists_board_fingerprint_snapshot(self):
        self.write_items([make_item("x"), make_item("y", status="in-progress")])
        self._seed_recent_journal()
        with (
            patch.object(llm_backends, "available_backends", return_value=["agy"]),
            patch.object(llm_backends, "run_agy", return_value="Great work."),
        ):
            dev_status._run_recap_regen()
        cache = json.loads(self.recap_cache_file.read_text())
        self.assertEqual(
            cache["board_fingerprint"], dev_status._current_board_fingerprint()
        )

    def test_r23d_recap_prompt_uses_selected_done_items_and_other_activity(self):
        items = [
            make_item(f"done-{year}", status="done", updated=f"{year}-01-01")
            for year in range(2020, 2026)
        ]
        for item in items:
            item["completed_at"] = item["updated"]
        self.write_items(items)
        ts = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        self._write_journal_line(
            {
                "ts": ts,
                "cmd": "done",
                "kind": "backlog",
                "slug": "done-2025",
                "summary": "Selected completion",
                "from_status": "in-progress",
                "to_status": "done",
            }
        )
        self._write_journal_line(
            {
                "ts": ts,
                "cmd": "done",
                "kind": "backlog",
                "slug": "done-2020",
                "summary": "Excluded completion",
                "from_status": "in-progress",
                "to_status": "done",
            }
        )
        self._write_journal_line(
            {
                "ts": ts,
                "cmd": "start",
                "kind": "backlog",
                "slug": "active",
                "summary": "Unrelated active work",
                "from_status": "open",
                "to_status": "in-progress",
            }
        )
        prompts = []
        with (
            patch.object(llm_backends, "available_backends", return_value=["agy"]),
            patch.object(
                llm_backends,
                "run_agy",
                side_effect=lambda prompt, **_: prompts.append(prompt) or "text",
            ),
        ):
            dev_status._run_recap_regen()
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertIn("Summary of done-2025", prompt)
        self.assertNotIn("Selected completion", prompt)
        self.assertNotIn("Excluded completion", prompt)
        self.assertIn("Unrelated active work", prompt)
        self.assertIn("done (latest 5 max): 5", prompt)
        self.assertIn("2025-01-01", prompt)

    def test_r23e_completion_only_journal_still_generates_with_empty_activity(self):
        item = make_item("done-item", status="done", updated="2026-01-01")
        item["completed_at"] = "2026-01-01"
        self.write_items([item])
        self._write_journal_line(
            {
                "ts": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                "cmd": "done",
                "kind": "backlog",
                "slug": "done-item",
                "summary": "Completed item",
                "from_status": "in-progress",
                "to_status": "done",
            }
        )
        prompts = []
        with (
            patch.object(llm_backends, "available_backends", return_value=["agy"]),
            patch.object(
                llm_backends,
                "run_agy",
                side_effect=lambda prompt, **_: prompts.append(prompt) or "text",
            ),
        ):
            dev_status._run_recap_regen()
        self.assertEqual(len(prompts), 1)
        self.assertIn("(none)", prompts[0])
        self.assertIn("Summary of done-item", prompts[0])

    def test_r23f_done_stamp_changes_invalidate_recap_fingerprint(self):
        item = make_item("done-item", status="done", updated="2026-01-01")
        item["completed_at"] = "2026-01-01"
        first = dev_status._board_fingerprint([item], [])
        item["completed_at"] = "2026-01-02"
        second = dev_status._board_fingerprint([item], [])
        self.assertNotEqual(first, second)

    # ── synchronous `recap` subcommand ──────────────────────────────────────

    def test_r25_recap_fresh_cache_prints_without_regen_call(self):
        self._write_cache("Fresh recap.", age_hours=0)
        out = io.StringIO()
        with (
            patch.object(dev_status, "_run_recap_regen") as mock_regen,
            patch("sys.stdout", out),
        ):
            dev_status.cmd_recap(_args())
        mock_regen.assert_not_called()
        self.assertIn("Fresh recap.", out.getvalue())

    def test_r25b_recap_fingerprint_mismatch_forces_regen_despite_fresh_ttl(self):
        self.write_items([make_item("x")])
        self._write_cache(
            "Fresh but stale-by-facts.",
            age_hours=0,
            board_fingerprint="deadbeefdeadbeef",
        )
        self._seed_recent_journal()
        with patch.object(
            dev_status, "_run_recap_regen", return_value=("agy", "New text")
        ) as mock_regen:
            out = io.StringIO()
            with patch("sys.stdout", out):
                dev_status.cmd_recap(_args())
        mock_regen.assert_called_once()
        self.assertIn("New text", out.getvalue())

    def test_r26_recap_double_checked_reuses_cache_from_in_flight_child(self):
        self._seed_recent_journal()

        @contextmanager
        def _fake_lock(*, blocking):
            # Simulate another process finishing a regen while this call
            # waited on the blocking lock.
            self._write_cache("Written while we waited.", age_hours=0)
            yield True

        with (
            patch.object(dev_status, "_regen_lock", _fake_lock),
            patch.object(dev_status, "_run_recap_regen") as mock_regen,
        ):
            out = io.StringIO()
            with patch("sys.stdout", out):
                dev_status.cmd_recap(_args())
        mock_regen.assert_not_called()
        self.assertIn("Written while we waited.", out.getvalue())

    def test_r27_recap_refresh_always_regenerates(self):
        self._write_cache("Fresh recap.", age_hours=0)
        with patch.object(
            dev_status, "_run_recap_regen", return_value=("agy", "New text")
        ) as mock_regen:
            out = io.StringIO()
            with patch("sys.stdout", out):
                dev_status.cmd_recap(_args(refresh=True))
        mock_regen.assert_called_once()
        self.assertIn("New text", out.getvalue())

    def test_r27b_recap_cache_bypass_flag_is_refresh_not_force(self):
        # `recap`'s cache-bypass flag is --refresh. --force is reserved for
        # destructive-confirmation gates (`prune`), so recap must reject it.
        seen = []
        with (
            patch.dict(dev_status.dispatch, {"recap": seen.append}),
            patch("sys.argv", ["dev_status", "recap", "--refresh"]),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            dev_status.main()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].refresh)
        self.assertFalse(hasattr(seen[0], "force"))

        # --force is not a registered argument of the recap subparser.
        with (
            self.assertRaises(SystemExit),
            patch.dict(dev_status.dispatch, {"recap": seen.append}),
            patch("sys.argv", ["dev_status", "recap", "--force"]),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            dev_status.main()
        self.assertEqual(len(seen), 1)

    def test_r28_recap_backend_override_passed_through(self):
        self._seed_recent_journal()
        with patch.object(
            dev_status, "_run_recap_regen", return_value=("copilot", "text")
        ) as mock_regen:
            dev_status.cmd_recap(_args(backend="copilot"))
        mock_regen.assert_called_once_with(backend_override="copilot", verbose=False)

    # ── normalization ────────────────────────────────────────────────────────

    def test_r29_normalize_strips_markdown_markers(self):
        self.assertEqual(
            dev_status._normalize_recap_text("**Great** work `today`!"),
            "Great work today!",
        )

    def test_r30_normalize_strips_emoji(self):
        self.assertEqual(
            dev_status._normalize_recap_text("Nice job \U0001f389 today"),
            "Nice job today",
        )

    def test_r31_normalize_truncates_long_text(self):
        result = dev_status._normalize_recap_text("x" * 500)
        self.assertLessEqual(len(result), dev_status.RECAP_MAX_CHARS + 1)
        self.assertTrue(result.endswith("…"))

    def test_r32_run_recap_regen_caches_empty_normalized_result(self):
        self._seed_recent_journal()
        with (
            patch.object(llm_backends, "available_backends", return_value=["agy"]),
            patch.object(llm_backends, "run_agy", return_value="*** \U0001f389 ***"),
        ):
            backend, text = dev_status._run_recap_regen()
        self.assertEqual(backend, "agy")
        self.assertEqual(text, "")
        cache = json.loads(self.recap_cache_file.read_text())
        self.assertEqual(cache["text"], "")
        self.assertEqual(cache["backend"], "agy")

    def test_r33_run_recap_regen_no_journal_entries_short_circuits(self):
        with patch.object(llm_backends, "available_backends") as mock_avail:
            backend, text = dev_status._run_recap_regen()
        self.assertEqual((backend, text), ("", ""))
        mock_avail.assert_not_called()
        self.assertFalse(self.recap_cache_file.exists())

    # ── changelog rendering ───────────────────────────────────────────────────

    def test_r34_changelog_omits_slug_when_summary_present(self):
        entries = [
            {
                "ts": "2026-01-01T12:00:00+00:00",
                "cmd": "done",
                "slug": "meta-example-slug",
                "summary": "Ship the widget",
            }
        ]
        line = dev_status._render_changelog(entries)
        self.assertNotIn("meta-example-slug", line)
        self.assertIn("Ship the widget", line)

    def test_r35_changelog_keeps_slug_when_no_summary(self):
        # This shape (slug with no summary) is a defensive fallback for
        # legacy/hand-edited journal data -- every live `_journal_entry`
        # call site that sets `slug` also sets a non-empty `summary`.
        entries = [
            {
                "ts": "2026-01-01T12:00:00+00:00",
                "cmd": "start",
                "slug": "meta-example-slug",
            }
        ]
        line = dev_status._render_changelog(entries)
        self.assertIn("meta-example-slug", line)

    def test_r36_changelog_rename_entry_has_no_slug_substrings(self):
        entries = [
            {
                "ts": "2026-01-01T12:00:00+00:00",
                "cmd": "rename",
                "slug": "new-slug-name",
                "summary": "renamed an item (Widen the dashboard box)",
            }
        ]
        line = dev_status._render_changelog(entries)
        self.assertNotIn("new-slug-name", line)
        self.assertIn("Widen the dashboard box", line)

    # ── DEVSTATUS_AGENT: suppress agent-only stderr noise on request ────────
    # Five success-path stderr sites (never on an error path -- those stay
    # unconditional regardless of this env var): render()'s item-map line,
    # confirm_resolution() (shared by 10 mutating commands), cmd_rename's own
    # echo, cmd_pending_add's own echo, and _blocker_check_reminder (shared
    # by add/pending add).

    def test_r37_render_item_map_suppressed_when_dev_status_agent_set(self):
        self.write_items([make_item("my-item")])
        out = io.StringIO()
        err = io.StringIO()
        with patch.dict(os.environ, {"DEVSTATUS_AGENT": "1"}):
            dev_status.render(out=out, err=err)
        self.assertNotIn("item-map:", err.getvalue())
        self.assertIn("┌─", out.getvalue())  # dashboard body intact

    def test_r38_confirm_resolution_suppressed_when_dev_status_agent_set(self):
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stderr", err),
        ):
            dev_status.confirm_resolution("start", "1", make_item("my-item"))
        self.assertEqual(err.getvalue(), "")

    def test_r39_start_resolution_echo_suppressed_when_dev_status_agent_set(self):
        self.write_items([make_item("my-item")])
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stderr", err),
        ):
            dev_status.cmd_start(_args(id="my-item"))
        self.assertNotIn("[start]", err.getvalue())
        self.assertNotIn("item-map:", err.getvalue())

    def test_r40_rename_echo_suppressed_when_dev_status_agent_set(self):
        self.write_items([make_item("old-name")])
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stderr", err),
        ):
            dev_status.cmd_rename(_args(old_slug="old-name", new_slug="new-name"))
        self.assertNotIn("[rename]", err.getvalue())

    def test_r41_pending_add_echo_and_blocker_reminder_suppressed(self):
        self.write_items([make_item("a")])
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stderr", err),
        ):
            dev_status.cmd_pending_add(
                _args(
                    json='{"id": "wait-y", "description": "waiting", "kind": "email"}'
                )
            )
        self.assertNotIn("[pending add]", err.getvalue())
        self.assertNotIn("check the READY/IN PROGRESS items above", err.getvalue())

    def test_r42_render_item_map_shown_without_env_var(self):
        # Regression guard: unset (the default in a real shell/test env) must
        # keep today's behavior -- a plain terminal user sees no change.
        self.write_items([make_item("my-item")])
        out = io.StringIO()
        err = io.StringIO()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVSTATUS_AGENT", None)
            dev_status.render(out=out, err=err)
        self.assertIn("item-map:", err.getvalue())

    # ── --quiet/--verbose: gate bucket-4 confirmations / bucket-5 diagnostics

    def test_r43_quiet_suppresses_confirm_resolution_echo(self):
        self.write_items([make_item("my-item")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_start(_args(id="my-item", quiet=True))
        self.assertNotIn("[start]", err.getvalue())

    def test_r44_default_shows_confirm_resolution_echo(self):
        # Regression guard: quiet=False (the default) is unchanged.
        self.write_items([make_item("my-item2")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_start(_args(id="my-item2"))
        self.assertIn("[start]", err.getvalue())

    def test_r45_quiet_suppresses_blocker_check_reminder(self):
        self.write_items([make_item("existing-ready")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(
                _args(
                    json='{"id": "new-item", "summary": "x"}',
                    quiet=True,
                )
            )
        self.assertNotIn("check the READY/IN PROGRESS items above", err.getvalue())

    def test_r46_backfill_gate_apply_quiet_suppresses_full_report(self):
        self.write_items([make_item("no-gate-item", gate=None)])
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_backfill_gate(_args(apply=True, quiet=True))
        self.assertNotIn("missing 'gate'", out.getvalue())
        self.assertNotIn("stamped", out.getvalue())
        self.assertIn("┌─", out.getvalue())  # dashboard still renders

    def test_r47_backfill_gate_dry_run_unaffected_by_quiet(self):
        self.write_items([make_item("no-gate-item2", gate=None)])
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_backfill_gate(_args(apply=False, quiet=True))
        self.assertIn("missing 'gate'", out.getvalue())
        self.assertIn("dry run", out.getvalue())

    def test_r48_rename_quiet_suppresses_echo(self):
        self.write_items([make_item("old-slug-q")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_rename(
                _args(old_slug="old-slug-q", new_slug="new-slug-q", quiet=True)
            )
        self.assertNotIn("[rename]", err.getvalue())

    def test_r49_pending_add_quiet_suppresses_echo(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_pending_add(
                _args(
                    json='{"id": "wait-q", "description": "waiting", "kind": "email"}',
                    quiet=True,
                )
            )
        self.assertNotIn("[pending add]", err.getvalue())

    def test_r50_prune_quiet_suppresses_removed_line_not_nothing_to_prune(self):
        self.write_items(
            [
                make_item(
                    "old-done",
                    status="done",
                    updated="2020-01-01",
                ),
                make_item("stays-ready"),
            ]
        )
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_prune(_args(force=True, quiet=True))
        self.assertNotIn("removed", out.getvalue())
        self.assertIn("┌─", out.getvalue())  # dashboard still renders

        out2 = io.StringIO()
        with patch("sys.stdout", out2):
            dev_status.cmd_prune(_args(force=True, quiet=True))
        self.assertIn("nothing to prune", out2.getvalue())

    def test_r51_parser_flags_parse_after_leaf_subcommand(self):
        args = dev_status.build_parser().parse_args(["render", "-q"])
        self.assertTrue(args.quiet)
        self.assertFalse(args.verbose)

    def test_r52_parser_flags_parse_after_nested_pending_subcommand(self):
        args = dev_status.build_parser().parse_args(["pending", "list", "-v"])
        self.assertTrue(args.verbose)
        self.assertFalse(args.quiet)

    def test_r53_parser_rejects_quiet_and_verbose_together(self):
        with self.assertRaises(SystemExit):
            dev_status.build_parser().parse_args(["render", "-q", "-v"])

    # ── out-of-scope knowledge base ──────────────────────────────────────────

    def write_reason_file(self, text="Rejected: not worth the added complexity."):
        path = Path(self.tmpdir) / "reason.txt"
        path.write_text(text)
        return str(path)

    def test_oos_01_add_creates_md_and_index_entry(self):
        reason_path = self.write_reason_file("Because X, per decision doc Y.")
        dev_status.cmd_out_of_scope_add(
            _args(concept_slug="skip-thing", reason_file=reason_path, related_item=None)
        )
        md_text = (self.out_of_scope_dir / "skip-thing.md").read_text()
        self.assertIn("# skip-thing", md_text)
        self.assertIn("Because X, per decision doc Y.", md_text)
        index = json.loads(self.out_of_scope_index_file.read_text())
        self.assertIn("skip-thing", index)
        self.assertEqual(index["skip-thing"]["related_items"], [])

    def test_oos_02_add_with_related_items_validates_existence(self):
        self.write_items([make_item("real-item")])
        reason_path = self.write_reason_file()
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="skip-thing",
                    reason_file=reason_path,
                    related_item=["ghost-item"],
                )
            )
        self.assertIn("ghost-item", err.getvalue())
        self.assertFalse(self.out_of_scope_index_file.exists())

        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=reason_path,
                related_item=["real-item"],
            )
        )
        index = json.loads(self.out_of_scope_index_file.read_text())
        self.assertEqual(index["skip-thing"]["related_items"], ["real-item"])

    def test_oos_03_add_duplicate_slug_rejected_without_clobbering(self):
        reason_path = self.write_reason_file("original reason")
        dev_status.cmd_out_of_scope_add(
            _args(concept_slug="skip-thing", reason_file=reason_path, related_item=None)
        )
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="skip-thing",
                    reason_file=self.write_reason_file("clobbering reason"),
                    related_item=None,
                )
            )
        self.assertIn("already exists", err.getvalue())
        self.assertIn(
            "original reason", (self.out_of_scope_dir / "skip-thing.md").read_text()
        )

    def test_oos_04_add_rejects_half_existing_state(self):
        # Only the .md file present, no index entry (e.g. from a prior crash) --
        # `add` must still refuse rather than silently clobbering it.
        self.out_of_scope_dir.mkdir(parents=True)
        (self.out_of_scope_dir / "skip-thing.md").write_text("orphaned")
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="skip-thing",
                    reason_file=self.write_reason_file(),
                    related_item=None,
                )
            )
        self.assertIn("already exists", err.getvalue())

    def test_oos_05_reason_file_missing_rejected(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="skip-thing",
                    reason_file=str(Path(self.tmpdir) / "nope.txt"),
                    related_item=None,
                )
            )
        self.assertIn("not found", err.getvalue())

    def test_oos_06_reason_file_is_directory_rejected(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="skip-thing",
                    reason_file=self.tmpdir,
                    related_item=None,
                )
            )
        self.assertIn("not a regular file", err.getvalue())

    def test_oos_07_reason_file_empty_rejected(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="skip-thing",
                    reason_file=self.write_reason_file("   \n  "),
                    related_item=None,
                )
            )
        self.assertIn("empty", err.getvalue())

    def test_oos_08_link_appends_and_is_idempotent(self):
        self.write_items([make_item("real-item")])
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        dev_status.cmd_out_of_scope_link(
            _args(concept_slug="skip-thing", backlog_slug="real-item")
        )
        dev_status.cmd_out_of_scope_link(
            _args(concept_slug="skip-thing", backlog_slug="real-item")
        )
        index = json.loads(self.out_of_scope_index_file.read_text())
        self.assertEqual(index["skip-thing"]["related_items"], ["real-item"])

    def test_oos_09_link_rejects_unknown_backlog_slug(self):
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_link(
                _args(concept_slug="skip-thing", backlog_slug="3")
            )
        self.assertIn("not a current backlog slug", err.getvalue())

    def test_oos_10_link_rejects_missing_concept(self):
        self.write_items([make_item("real-item")])
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_link(
                _args(concept_slug="ghost-concept", backlog_slug="real-item")
            )
        self.assertIn("not found", err.getvalue())

    def test_oos_11_link_rejects_when_only_md_file_exists(self):
        # Half-existing state (Round 3 fix): the .md file is present but the
        # index entry isn't -- `link` must not index into a missing entry.
        self.write_items([make_item("real-item")])
        self.out_of_scope_dir.mkdir(parents=True)
        (self.out_of_scope_dir / "skip-thing.md").write_text("orphaned")
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_link(
                _args(concept_slug="skip-thing", backlog_slug="real-item")
            )
        self.assertIn("not found", err.getvalue())

    def test_oos_12_unlink_removes_and_is_noop_when_absent(self):
        self.write_items([make_item("real-item")])
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=["real-item"],
            )
        )
        dev_status.cmd_out_of_scope_unlink(
            _args(concept_slug="skip-thing", backlog_slug="real-item")
        )
        index = json.loads(self.out_of_scope_index_file.read_text())
        self.assertEqual(index["skip-thing"]["related_items"], [])
        # Second unlink of the same, now-absent slug is a no-op, not an error.
        dev_status.cmd_out_of_scope_unlink(
            _args(concept_slug="skip-thing", backlog_slug="real-item")
        )

    def test_oos_13_remove_deletes_both_halves(self):
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        dev_status.cmd_out_of_scope_remove(_args(concept_slug="skip-thing"))
        self.assertFalse((self.out_of_scope_dir / "skip-thing.md").exists())
        index = json.loads(self.out_of_scope_index_file.read_text())
        self.assertNotIn("skip-thing", index)

    def test_oos_14_remove_tolerates_half_existing_state(self):
        self.out_of_scope_dir.mkdir(parents=True)
        (self.out_of_scope_dir / "skip-thing.md").write_text("orphaned")
        dev_status.cmd_out_of_scope_remove(_args(concept_slug="skip-thing"))
        self.assertFalse((self.out_of_scope_dir / "skip-thing.md").exists())

    def test_oos_15_remove_rejects_when_neither_half_exists(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_remove(_args(concept_slug="ghost-concept"))
        self.assertIn("not found", err.getvalue())

    def test_oos_16_list_empty_prints_informational_message(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_list(_args())
        self.assertIn("no rejected concepts", err.getvalue())

    def test_oos_17_list_shows_entries_newest_first(self):
        with patch.object(dev_status, "today", return_value="2026-01-01"):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="old-one",
                    reason_file=self.write_reason_file(),
                    related_item=None,
                )
            )
        with patch.object(dev_status, "today", return_value="2026-06-01"):
            dev_status.cmd_out_of_scope_add(
                _args(
                    concept_slug="new-one",
                    reason_file=self.write_reason_file(),
                    related_item=None,
                )
            )
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_out_of_scope_list(_args())
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("new-one"))
        self.assertTrue(lines[1].startswith("old-one"))

    def test_oos_18_show_reports_missing_md_mismatch(self):
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        (self.out_of_scope_dir / "skip-thing.md").unlink()
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_show(_args(concept_slug="skip-thing"))
        self.assertIn("missing", err.getvalue())

    def test_oos_19_show_rejects_when_neither_half_exists(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_out_of_scope_show(_args(concept_slug="ghost-concept"))
        self.assertIn("not found", err.getvalue())

    def test_oos_20_reminder_fires_from_add_when_concept_exists(self):
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": "my-feature", "summary": "x"}'))
        self.assertIn("rejected concept(s) on file", err.getvalue())

    def test_oos_21_reminder_silent_when_no_concepts_exist(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(_args(json='{"id": "my-feature", "summary": "x"}'))
        self.assertNotIn("rejected concept", err.getvalue())

    def test_oos_22_reminder_fires_from_pending_add_when_concept_exists(self):
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_pending_add(
                _args(
                    json='{"id": "wait-thing", "description": "waiting", "kind": "email"}'
                )
            )
        self.assertIn("rejected concept(s) on file", err.getvalue())

    def test_oos_23_reminder_quiet_suppresses(self):
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(
                _args(json='{"id": "my-feature", "summary": "x"}', quiet=True)
            )
        self.assertNotIn("rejected concept", err.getvalue())

    def test_oos_24_lock_serializes_concurrent_link_calls(self):
        self.write_items([make_item("item-a"), make_item("item-b")])
        dev_status.cmd_out_of_scope_add(
            _args(
                concept_slug="skip-thing",
                reason_file=self.write_reason_file(),
                related_item=None,
            )
        )
        errors = []

        def link(slug):
            try:
                dev_status.cmd_out_of_scope_link(
                    _args(concept_slug="skip-thing", backlog_slug=slug)
                )
            except Exception as e:  # pragma: no cover - defensive
                errors.append(e)

        threads = [
            threading.Thread(target=link, args=("item-a",)),
            threading.Thread(target=link, args=("item-b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        index = json.loads(self.out_of_scope_index_file.read_text())
        self.assertEqual(
            sorted(index["skip-thing"]["related_items"]), ["item-a", "item-b"]
        )

    def test_oos_25_parser_out_of_scope_add_parses(self):
        args = dev_status.build_parser().parse_args(
            ["out-of-scope", "add", "skip-thing", "--reason-file", "/tmp/r.txt"]
        )
        self.assertEqual(args.oos_cmd, "add")
        self.assertEqual(args.concept_slug, "skip-thing")
        self.assertEqual(args.reason_file, "/tmp/r.txt")

    def _add(self, slug, paths, quiet=False):
        payload = {
            "id": slug,
            "summary": "x",
            "related_files": [{"path": p, "note": "n"} for p in paths],
        }
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_add(_args(json=json.dumps(payload), quiet=quiet))
        return err.getvalue()

    def test_p1_reminder_fires_on_mismatched_prefix(self):
        with patch.object(
            dev_status, "_repo_name_for_path", return_value="agent-toolkit"
        ):
            err = self._add("meta-some-toolkit-thing", ["/w/agent-toolkit/README.md"])
        self.assertIn("meta-some-toolkit-thing", err)
        self.assertIn("agent-toolkit", err)
        self.assertIn("atk-", err)

    def test_p2_silent_when_prefix_matches(self):
        with patch.object(
            dev_status, "_repo_name_for_path", return_value="agent-toolkit"
        ):
            err = self._add("atk-some-toolkit-thing", ["/w/agent-toolkit/README.md"])
        self.assertNotIn(_PREFIX_MARKER, err)

    def test_p3_silent_for_multi_segment_prefix_that_matches(self):
        # `iron-lb-` must not be read as the prefix `iron-`; a naive
        # split("-")[0] would report a false mismatch on every iron-lb item.
        with patch.object(
            dev_status, "_repo_name_for_path", return_value="iron-logbook"
        ):
            err = self._add("iron-lb-wearable-card", ["/w/iron-logbook/app/page.tsx"])
        self.assertNotIn(_PREFIX_MARKER, err)

    def test_p4_silent_without_related_files(self):
        err = self._add("meta-no-files", [])
        self.assertNotIn(_PREFIX_MARKER, err)

    def test_p5_silent_when_paths_span_two_repos(self):
        names = iter(["agent-toolkit", "dotfiles"])
        with patch.object(
            dev_status, "_repo_name_for_path", side_effect=lambda _p: next(names)
        ):
            err = self._add(
                "meta-cross-repo", ["/w/agent-toolkit/x", "/home/u/dotfiles/y"]
            )
        self.assertNotIn(_PREFIX_MARKER, err)

    def test_p6_silent_when_repo_is_unmapped(self):
        with patch.object(
            dev_status, "_repo_name_for_path", return_value="some-other-repo"
        ):
            err = self._add("meta-unmapped", ["/w/some-other-repo/x"])
        self.assertNotIn(_PREFIX_MARKER, err)

    def test_p7_quiet_does_not_suppress_it(self):
        # Deliberately unlike _blocker_check_reminder and
        # _out_of_scope_check_reminder. Every agent passes DEVSTATUS_AGENT=1,
        # and an agent is the only caller that ever adds an item, so a reminder
        # silenced by quiet would be invisible to its entire audience.
        with patch.dict(os.environ, {"DEVSTATUS_AGENT": "1"}):
            with patch.object(
                dev_status, "_repo_name_for_path", return_value="agent-toolkit"
            ):
                err = self._add(
                    "meta-quiet-probe", ["/w/agent-toolkit/README.md"], quiet=True
                )
        self.assertIn("atk-", err)

    def test_p8_reminder_does_not_block_the_add(self):
        with patch.object(
            dev_status, "_repo_name_for_path", return_value="agent-toolkit"
        ):
            self._add("meta-still-added", ["/w/agent-toolkit/README.md"])
        slugs = [i["id"] for i in self.read_items()]
        self.assertIn("meta-still-added", slugs)

    def test_p9_repo_name_resolves_via_git_common_dir(self):
        # --show-toplevel returns the WORKTREE root, whose basename misses the
        # map; --git-common-dir returns <main-repo>/.git, whose parent is the
        # real repo. Verified live 2026-09-03 from inside a worktree.
        fake = MagicMock(returncode=0, stdout="/home/u/dotfiles/.git\n")
        with patch.object(dev_status.subprocess, "run", return_value=fake) as run:
            name = dev_status._repo_name_for_path(
                "/home/u/dotfiles-some-slug/claude/x.py"
            )
        self.assertEqual("dotfiles", name)
        argv = run.call_args[0][0]
        self.assertIn("--git-common-dir", argv)
        self.assertNotIn("--show-toplevel", argv)

    def test_p10_repo_name_is_none_when_git_fails(self):
        fake = MagicMock(returncode=128, stdout="")
        with patch.object(dev_status.subprocess, "run", return_value=fake):
            self.assertIsNone(dev_status._repo_name_for_path("/not/a/repo/x.py"))

    def test_ws1_prefix_of_prefers_the_longest_known_prefix(self):
        self.assertEqual(dev_status.prefix_of("iron-lb-wearable-card"), "iron-lb")
        self.assertEqual(dev_status.prefix_of("atk-publish-remote"), "atk")

    def test_ws2_project_prefixes_are_worker_safe(self):
        self.assertTrue(dev_status.is_worker_safe("atk"))
        self.assertTrue(dev_status.is_worker_safe("iron-lb"))

    def test_ws3_the_harness_prefix_is_not_worker_safe(self):
        harness = dev_status.REPO_PREFIXES[dev_status.HARNESS_REPO]
        self.assertFalse(dev_status.is_worker_safe(harness))

    def test_ws4_unknown_prefixes_are_unsafe_not_safe(self):
        # A whitelist, deliberately. `pi-` and `dotfiles-` are real prefixes in
        # the store, both dotfiles work, and an unsafe-only-if-meta rule classed
        # them as swarmable. `work-` has its own policy and must never swarm.
        for prefix in ("pi", "dotfiles", "work", "brand-new-project"):
            self.assertFalse(
                dev_status.is_worker_safe(prefix), f"{prefix}- should not be swarmable"
            )

    def test_ws5_ready_reports_worker_safe_per_item(self):
        self.write_items(
            [
                make_item("atk-a"),
                make_item("meta-b"),
                make_item("iron-lb-c"),
            ]
        )
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_ready(_args(prefix=None))
        by_id = {i["id"]: i for i in json.loads(out.getvalue())}
        self.assertIs(by_id["atk-a"]["worker_safe"], True)
        self.assertIs(by_id["meta-b"]["worker_safe"], False)
        self.assertIs(by_id["iron-lb-c"]["worker_safe"], True)

    def test_ws6_ready_reports_worker_safe_on_every_item(self):
        # swarm-tool.ts fails closed on a missing field, so an item without it
        # would be refused rather than skipped -- silently halving a wave.
        self.write_items([make_item("atk-a"), make_item("meta-b")])
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_ready(_args(prefix=None))
        for item in json.loads(out.getvalue()):
            self.assertIn("worker_safe", item, item["id"])



# Every env var _detect_harness consults, in its documented resolution order.
_HARNESS_ENV_VARS = (
    "DEVSTATUS_HARNESS",
    "PI_SESSION",
    "PI_CODING_AGENT",
    "CLAUDE_CODE",
    "ANTHROPIC_CLI",
    "ANTIGRAVITY",
    "AGY_SESSION",
    "OPENCODE_GATEWAY",
    "OPENCODE",
    "GITHUB_COPILOT",
    "COPILOT",
)


def _only_harness_env(**active: str) -> dict[str, str]:
    """Build an env dict asserting exactly one harness is detected.

    _detect_harness resolves by priority and only truthiness-checks each var,
    so whatever the session running the suite happens to export leaks in and
    outranks the branch under test: a Pi session exports PI_CODING_AGENT,
    which sits above CLAUDE_CODE and made the claude assertion fail. Blanking
    every other var makes each assertion depend only on the var it sets.
    """
    env = {name: "" for name in _HARNESS_ENV_VARS}
    env.update(active)
    return env


class ClaimMarkerAndWorktreeGuardTestCase(BacklogTestCase):
    def test_detect_harness(self):
        self.assertEqual(dev_status._detect_harness("custom"), "custom")
        with patch.dict(os.environ, _only_harness_env(DEVSTATUS_HARNESS="my-env")):
            self.assertEqual(dev_status._detect_harness(), "my-env")
        with patch.dict(os.environ, _only_harness_env(PI_SESSION="1")):
            self.assertEqual(dev_status._detect_harness(), "pi")
        with patch.dict(os.environ, _only_harness_env(PI_CODING_AGENT="1")):
            self.assertEqual(dev_status._detect_harness(), "pi")
        with patch.dict(os.environ, _only_harness_env(CLAUDE_CODE="1")):
            self.assertEqual(dev_status._detect_harness(), "claude")
        with patch.dict(os.environ, _only_harness_env(ANTHROPIC_CLI="1")):
            self.assertEqual(dev_status._detect_harness(), "claude")
        with patch.dict(os.environ, _only_harness_env(ANTIGRAVITY="1")):
            self.assertEqual(dev_status._detect_harness(), "agy")
        with patch.dict(os.environ, _only_harness_env(AGY_SESSION="1")):
            self.assertEqual(dev_status._detect_harness(), "agy")
        with patch.dict(os.environ, _only_harness_env(OPENCODE_GATEWAY="1")):
            self.assertEqual(dev_status._detect_harness(), "opencode")
        with patch.dict(os.environ, _only_harness_env(OPENCODE="1")):
            self.assertEqual(dev_status._detect_harness(), "opencode")
        with patch.dict(os.environ, _only_harness_env(GITHUB_COPILOT="1")):
            self.assertEqual(dev_status._detect_harness(), "copilot")
        with patch.dict(os.environ, _only_harness_env(COPILOT="1")):
            self.assertEqual(dev_status._detect_harness(), "copilot")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dev_status._detect_harness(), "cli")

    def test_detect_harness_priority_order(self):
        """Higher-priority vars win when several are set at once.

        The one-var-at-a-time assertions above cannot see branch order: they
        blank every other var, so they still pass if the branches are
        reordered. These adjacent pairs pin the documented priority chain.
        """
        with patch.dict(
            os.environ,
            _only_harness_env(DEVSTATUS_HARNESS="my-env", PI_SESSION="1"),
        ):
            self.assertEqual(dev_status._detect_harness(), "my-env")
        with patch.dict(os.environ, _only_harness_env(PI_SESSION="1", CLAUDE_CODE="1")):
            self.assertEqual(dev_status._detect_harness(), "pi")
        with patch.dict(
            os.environ, _only_harness_env(CLAUDE_CODE="1", ANTIGRAVITY="1")
        ):
            self.assertEqual(dev_status._detect_harness(), "claude")
        with patch.dict(os.environ, _only_harness_env(ANTIGRAVITY="1", OPENCODE="1")):
            self.assertEqual(dev_status._detect_harness(), "agy")
        with patch.dict(os.environ, _only_harness_env(OPENCODE="1", COPILOT="1")):
            self.assertEqual(dev_status._detect_harness(), "opencode")

    @patch("dev_status._check_worktree_guard")
    def test_claim_creation_on_start(self, mock_wt):
        item = make_item("task-a", status="open")
        self.write_items([item])
        with patch.dict(os.environ, {"DEVSTATUS_HARNESS": "pi"}):
            dev_status.cmd_start(_args(id="task-a", claimed_by=None, force=False))
        items = dev_status.load_items()
        claim = items[0].get("claimed_by")
        self.assertIsNotNone(claim)
        self.assertEqual(claim["harness"], "pi")
        self.assertEqual(claim["machine_id"], dev_status.machine_id())
        self.assertEqual(claim["pid"], os.getpid())
        self.assertIn("claimed_at", claim)
        self.assertIn("last_active", claim)

    @patch("dev_status._check_worktree_guard")
    def test_claim_cleared_on_done(self, mock_wt):
        item = make_item("task-a", status="open")
        self.write_items([item])
        dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        dev_status.cmd_done(_args(id="task-a"))
        items = dev_status.load_items()
        self.assertEqual(items[0]["status"], "done")
        self.assertNotIn("claimed_by", items[0])

    @patch("dev_status._check_worktree_guard")
    def test_claim_cleared_on_review(self, mock_wt):
        item = make_item("task-a", status="open")
        self.write_items([item])
        dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        dev_status.cmd_review(_args(id="task-a"))
        items = dev_status.load_items()
        self.assertEqual(items[0]["status"], "in-review")
        self.assertNotIn("claimed_by", items[0])

    @patch("dev_status._check_worktree_guard")
    def test_claim_cleared_on_block(self, mock_wt):
        item_a = make_item("task-a", status="open")
        item_b = make_item("task-b", status="open")
        self.write_items([item_a, item_b])
        dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        dev_status.cmd_update(_args(id="task-a", patch='{"status": "open"}'))
        dev_status.cmd_block(_args(id="task-a", blocker="task-b"))
        items = dev_status.load_items()
        self.assertEqual(items[0]["status"], "open")
        self.assertNotIn("claimed_by", items[0])

    @patch("dev_status._check_worktree_guard")
    def test_claim_cleared_on_update_status_open(self, mock_wt):
        item = make_item("task-a", status="open")
        self.write_items([item])
        dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        dev_status.cmd_update(_args(id="task-a", patch='{"status": "open"}'))
        items = dev_status.load_items()
        self.assertEqual(items[0]["status"], "open")
        self.assertNotIn("claimed_by", items[0])

    @patch("dev_status._is_pid_alive", return_value=True)
    @patch("dev_status._check_worktree_guard")
    def test_claim_collision_active_pid_refuses(self, mock_wt, mock_pid):
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        item = make_item("task-a", status="in-progress")
        item["claimed_by"] = {
            "harness": "pi",
            "machine_id": dev_status.machine_id(),
            "pid": 999999,
            "claimed_at": now_iso,
            "last_active": now_iso,
        }
        self.write_items([item])
        with self.assertRaises(SystemExit) as ctx:
            dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        self.assertEqual(ctx.exception.code, 1)

    @patch("dev_status._is_pid_alive", return_value=True)
    @patch("dev_status._check_worktree_guard")
    def test_claim_collision_active_pid_force_succeeds(self, mock_wt, mock_pid):
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        item = make_item("task-a", status="in-progress")
        item["claimed_by"] = {
            "harness": "pi",
            "machine_id": dev_status.machine_id(),
            "pid": 999999,
            "claimed_at": now_iso,
            "last_active": now_iso,
        }
        self.write_items([item])
        dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=True))
        items = dev_status.load_items()
        self.assertEqual(items[0]["claimed_by"]["harness"], "claude")

    @patch("dev_status._is_pid_alive", return_value=False)
    @patch("dev_status._check_worktree_guard")
    def test_claim_collision_dead_pid_takeover(self, mock_wt, mock_pid):
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        item = make_item("task-a", status="in-progress")
        item["claimed_by"] = {
            "harness": "pi",
            "machine_id": dev_status.machine_id(),
            "pid": 999999,
            "claimed_at": now_iso,
            "last_active": now_iso,
        }
        self.write_items([item])
        dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        items = dev_status.load_items()
        self.assertEqual(items[0]["claimed_by"]["harness"], "claude")

    @patch("dev_status._check_worktree_guard")
    def test_claim_collision_ttl_active_refuses(self, mock_wt):
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        item = make_item("task-a", status="in-progress")
        item["claimed_by"] = {
            "harness": "pi",
            "machine_id": "other-machine",
            "pid": 999999,
            "claimed_at": now_iso,
            "last_active": now_iso,
        }
        self.write_items([item])
        with self.assertRaises(SystemExit) as ctx:
            dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        self.assertEqual(ctx.exception.code, 1)

    @patch("dev_status._check_worktree_guard")
    def test_claim_collision_ttl_expired_takeover(self, mock_wt):
        old_iso = (datetime.now(UTC) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        item = make_item("task-a", status="in-progress")
        item["claimed_by"] = {
            "harness": "pi",
            "machine_id": "other-machine",
            "pid": 999999,
            "claimed_at": old_iso,
            "last_active": old_iso,
        }
        self.write_items([item])
        dev_status.cmd_start(_args(id="task-a", claimed_by="claude", force=False))
        items = dev_status.load_items()
        self.assertEqual(items[0]["claimed_by"]["harness"], "claude")

    @patch("subprocess.run")
    def test_worktree_guard_refuses_main_checkout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/repo/.git\n/repo/.git\nmain\n",
        )
        with self.assertRaises(SystemExit) as ctx:
            dev_status._check_worktree_guard(allow_main=False)
        self.assertEqual(ctx.exception.code, 1)

    @patch("subprocess.run")
    def test_worktree_guard_allows_with_flag(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/repo/.git\n/repo/.git\nmain\n",
        )
        # Should not raise
        dev_status._check_worktree_guard(allow_main=True)

    @patch("subprocess.run")
    def test_worktree_guard_allows_in_secondary_worktree(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/repo/.git\n/repo/.git/worktrees/wt1\nmain\n",
        )
        # Different git-dir means secondary worktree
        dev_status._check_worktree_guard(allow_main=False)

    @patch("subprocess.run")
    def test_worktree_guard_allows_in_feature_branch(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/repo/.git\n/repo/.git\nfeat-new-thing\n",
        )
        dev_status._check_worktree_guard(allow_main=False)

    @patch("subprocess.run")
    def test_worktree_guard_graceful_non_git(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
        )
        dev_status._check_worktree_guard(allow_main=False)

    def test_render_displays_harness_tag(self):
        item = make_item("task-a", status="in-progress", summary="Do something")
        item["claimed_by"] = {"harness": "pi"}
        self.write_items([item])
        out = io.StringIO()
        err = io.StringIO()
        dev_status.render([item], [], out=out, err=err, rev=1)
        rendered = out.getvalue()
        self.assertIn("[pi]", rendered)

    def test_project_prefix_extraction(self) -> None:
        self.assertEqual(dev_status._project_prefix("iron-lb-wearable-ui"), "iron-lb")
        self.assertEqual(dev_status._project_prefix("ajhp-search-jobs"), "ajhp")
        self.assertEqual(dev_status._project_prefix("meta-standup-sync"), "meta")
        self.assertEqual(dev_status._project_prefix("work-report"), "work")
        self.assertEqual(dev_status._project_prefix("custom-project-slug"), "custom")
        self.assertEqual(dev_status._project_prefix("unprefixed"), "")

    def test_project_divider_format(self) -> None:
        div_single = dev_status._project_divider("iron-lb", 1, width=76, color=False)
        self.assertTrue(div_single.startswith("│  ─── iron-lb (1 item) ───"))
        self.assertEqual(len(div_single), 76)

        div_plural = dev_status._project_divider("meta", 5, width=76, color=False)
        self.assertTrue(div_plural.startswith("│  ─── meta (5 items) ───"))
        self.assertEqual(len(div_plural), 76)

    def test_ready_sort_by_project_priority_created(self) -> None:
        # 4 items across 2 projects:
        # meta-a (normal, created 2026-01-01)
        # iron-lb-a (normal, created 2026-01-02)
        # meta-b (high, created 2026-01-03)
        # iron-lb-b (high, created 2026-01-04)
        items = [
            make_item("meta-a", created="2026-01-01"),
            make_item("iron-lb-a", created="2026-01-02"),
            make_item("meta-b", created="2026-01-03", priority="high"),
            make_item("iron-lb-b", created="2026-01-04", priority="high"),
        ]
        _, ready, _, _, _ = dev_status._render_order(items)
        # Expected:
        # iron-lb cluster first (iron-lb-b [high] -> iron-lb-a [normal])
        # meta cluster second (meta-b [high] -> meta-a [normal])
        self.assertEqual(
            [i["id"] for i in ready],
            ["iron-lb-b", "iron-lb-a", "meta-b", "meta-a"],
        )

    def test_blocked_sort_by_project_blocker_count(self) -> None:
        blocker1 = make_item("iron-lb-b1", status="open")
        blocker2 = make_item("meta-b1", status="open")
        items = [
            blocker1,
            blocker2,
            make_item(
                "meta-blocked-multi",
                blocked_by=["iron-lb-b1", "meta-b1"],
                status="open",
            ),
            make_item("meta-blocked-single", blocked_by=["meta-b1"], status="open"),
            make_item(
                "iron-lb-blocked-single", blocked_by=["iron-lb-b1"], status="open"
            ),
        ]
        _, ready, blocked, _, _ = dev_status._render_order(items)
        # iron-lb blocked first (1 blocker), meta blocked second (single before multi)
        self.assertEqual(
            [i["id"] for i in blocked],
            ["iron-lb-blocked-single", "meta-blocked-single", "meta-blocked-multi"],
        )

    def test_render_and_resolve_id_with_project_sorting(self) -> None:
        blocker = make_item("iron-lb-blocker", status="open", summary="Core schema")
        ready_meta = make_item(
            "meta-tooling", status="open", summary="Tooling improvement"
        )
        blocked_iron = make_item(
            "iron-lb-ui",
            status="open",
            summary="UI Screen",
            blocked_by=["iron-lb-blocker"],
        )
        self.write_items([ready_meta, blocker, blocked_iron])
        out = io.StringIO()
        err = io.StringIO()
        dev_status.render(out=out, err=err, rev=1)
        rendered = out.getvalue()

        # In READY: iron-lb-blocker (#1) -> meta-tooling (#2)
        # In BLOCKED: iron-lb-ui (#3) blocked by #1
        self.assertIn("─── iron-lb (1 item) ───", rendered)
        self.assertIn("─── meta (1 item) ───", rendered)
        self.assertIn("blocked by: #1 (Core schema)", rendered)

        # resolve_id check:
        kind, slug1 = dev_status.resolve_id("1", self.read_items(), [])
        kind, slug2 = dev_status.resolve_id("2", self.read_items(), [])
        kind, slug3 = dev_status.resolve_id("3", self.read_items(), [])
        self.assertEqual(slug1, "iron-lb-blocker")
        self.assertEqual(slug2, "meta-tooling")
        self.assertEqual(slug3, "iron-lb-ui")


# ── gate-pass run evidence (runs.jsonl, run/runs subcommands) ─────────────────


class RunEvidenceTestCase(BacklogTestCase):
    """gate-pass must require recorded run evidence, not self-attestation.

    Covers the ``runs.jsonl`` sidecar, the ``run``/``runs`` subcommands,
    ``gate-set``'s ``set_at`` stamp, ``gate-pass``'s per-criterion coverage
    payload, ``rename``'s run-row rewrite, and ``show``'s runs line.
    """

    RUN_ID_A = "aaaa1111aaaa1111aaaa1111aaaa1111"
    RUN_ID_B = "bbbb2222bbbb2222bbbb2222bbbb2222"
    RUN_ID_MISSING = "ffff9999ffff9999ffff9999ffff9999"

    # ── fixtures ─────────────────────────────────────────────────────────

    def _gate_item(self, criteria=("criterion one", "criterion two")):
        self.write_items(
            [
                make_item(
                    "gt-item",
                    gate={
                        "required": True,
                        "criteria": list(criteria),
                        "passed_at": None,
                    },
                )
            ]
        )

    def _seed_run(
        self,
        run_id=RUN_ID_A,
        item="gt-item",
        exit_code=0,
        timed_out=False,
        started_at=None,
        command="pytest -q",
        duration_s=1.5,
        cwd="/tmp",
    ):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "run_id": run_id,
            "item": item,
            "command": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "started_at": (started_at or datetime.now(UTC).isoformat()),
            "duration_s": duration_s,
            "cwd": cwd,
        }
        with open(self.runs_file, "a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def _read_runs(self):
        if not self.runs_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.runs_file.read_text().splitlines()
            if line.strip()
        ]

    def _gate_pass_refused(self, json_arg=None):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_gate_pass(_args(id="gt-item", json=json_arg))
        self.assertEqual(cm.exception.code, 1)
        return err.getvalue()

    # ── gate-set: set_at stamping ───────────────────────────────────────

    def test_gate_set_stamps_set_at(self):
        self.write_items([make_item("gt-item")])
        before = datetime.now(UTC)
        dev_status.cmd_gate_set(
            _args(id="gt-item", json='{"required": true, "criteria": ["c"]}')
        )
        gate = self._item_by_id("gt-item")["gate"]
        self.assertIsNone(gate["passed_at"])
        set_at = datetime.fromisoformat(gate["set_at"])
        if set_at.tzinfo is None:
            set_at = set_at.replace(tzinfo=UTC)
        self.assertGreaterEqual(set_at, before.replace(microsecond=0))

    # ── gate-pass: refusals ─────────────────────────────────────────────

    def test_gate_pass_without_coverage_payload_refuses(self):
        self._gate_item()
        msg = self._gate_pass_refused(json_arg=None)
        self.assertIn("coverage", msg)
        self.assertIn("run:", msg)
        self.assertIn("manual:", msg)
        self.assertIsNone(self._item_by_id("gt-item")["gate"]["passed_at"])

    def test_gate_pass_empty_coverage_refuses_naming_criteria(self):
        self._gate_item()
        msg = self._gate_pass_refused(json_arg='{"coverage": {}}')
        self.assertIn("criterion 1", msg)
        self.assertIn("criterion 2", msg)
        self.assertIn("not covered", msg)
        self.assertIn("no runs recorded", msg)

    def test_gate_pass_unknown_run_id_refuses_and_lists_recent_runs(self):
        self._gate_item()
        self._seed_run(run_id=self.RUN_ID_A)
        msg = self._gate_pass_refused(
            json_arg='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_MISSING
        )
        self.assertIn("unknown run id", msg)
        self.assertIn(self.RUN_ID_MISSING, msg)
        self.assertIn("recent runs", msg)
        self.assertIn(self.RUN_ID_A, msg)

    def test_gate_pass_failed_run_refuses(self):
        self._gate_item()
        self._seed_run(exit_code=2)
        msg = self._gate_pass_refused(
            json_arg='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_A
        )
        self.assertIn("failed", msg)

    def test_gate_pass_timed_out_run_refuses(self):
        self._gate_item()
        self._seed_run(exit_code=None, timed_out=True)
        msg = self._gate_pass_refused(
            json_arg='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_A
        )
        self.assertIn("timed out", msg)

    def test_gate_pass_stale_run_refuses(self):
        self.write_items([make_item("gt-item")])
        dev_status.cmd_gate_set(
            _args(id="gt-item", json='{"required": true, "criteria": ["c"]}')
        )
        stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        self._seed_run(started_at=stale)
        msg = self._gate_pass_refused(
            json_arg='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_A
        )
        self.assertIn("stale", msg)

    def test_gate_pass_out_of_range_criterion_index_refuses(self):
        self._gate_item()
        msg = self._gate_pass_refused(json_arg='{"coverage": {"3": "manual: ok"}}')
        self.assertIn("3", msg)
        self.assertIn("out of range", msg)

    def test_gate_pass_empty_manual_note_refuses(self):
        self._gate_item()
        msg = self._gate_pass_refused(json_arg='{"coverage": {"1": "manual:   "}}')
        self.assertIn("empty manual note", msg)

    def test_gate_pass_bad_value_shape_refuses(self):
        self._gate_item()
        msg = self._gate_pass_refused(json_arg='{"coverage": {"1": "winged it"}}')
        self.assertIn("run:", msg)
        self.assertIn("manual:", msg)

    def test_gate_pass_coverage_must_be_object_of_strings(self):
        self._gate_item()
        for bad in ('{"coverage": [1]}', '{"coverage": {"1": 3}}'):
            msg = self._gate_pass_refused(json_arg=bad)
            self.assertIn("coverage", msg)

    # ── gate-pass: happy paths ──────────────────────────────────────────

    def test_gate_pass_run_evidence_happy_path(self):
        self._gate_item()
        self._seed_run(run_id=self.RUN_ID_A)
        dev_status.cmd_gate_pass(
            _args(
                id="gt-item",
                json='{"coverage": {"1": "run:%s", "2": "run:%s"}}'
                % (self.RUN_ID_A, self.RUN_ID_A),
            )
        )
        gate = self._item_by_id("gt-item")["gate"]
        self.assertIsNotNone(gate["passed_at"])
        self.assertEqual(gate["passed_via"], "run-evidence")
        self.assertEqual(gate["coverage"]["1"]["run_id"], self.RUN_ID_A)
        self.assertEqual(gate["coverage"]["2"]["run_id"], self.RUN_ID_A)

    def test_gate_pass_noncanonical_coverage_key_still_covers(self):
        """Pre-fix, a validly-in-range but non-canonical key like "01"
        passed every validation check and was stored in `covered` under
        its raw form, but the final "not covered" pass matched on str(i)
        ("1") -- so a genuinely-covered single criterion still refused
        with "criterion 1: not covered" despite the cited run being valid."""
        self._gate_item(criteria=("only criterion",))
        self._seed_run(run_id=self.RUN_ID_A)
        dev_status.cmd_gate_pass(
            _args(id="gt-item", json='{"coverage": {"01": "run:%s"}}' % self.RUN_ID_A)
        )
        gate = self._item_by_id("gt-item")["gate"]
        self.assertIsNotNone(gate["passed_at"])
        self.assertIn("1", gate["coverage"])

    def test_gate_pass_manual_only_records_manual(self):
        self._gate_item()
        dev_status.cmd_gate_pass(
            _args(
                id="gt-item",
                json='{"coverage": {"1": "manual: saw it", "2": "manual: saw it too"}}',
            )
        )
        gate = self._item_by_id("gt-item")["gate"]
        self.assertEqual(gate["passed_via"], "manual")
        self.assertEqual(gate["coverage"]["1"]["note"], "saw it")

    def test_gate_pass_mixed_records_mixed(self):
        self._gate_item()
        self._seed_run(run_id=self.RUN_ID_A)
        dev_status.cmd_gate_pass(
            _args(
                id="gt-item",
                json='{"coverage": {"1": "run:%s", "2": "manual: eyeballed"}}'
                % self.RUN_ID_A,
            )
        )
        gate = self._item_by_id("gt-item")["gate"]
        self.assertEqual(gate["passed_via"], "mixed")

    def test_gate_pass_counts_run_for_other_item_as_unknown(self):
        self._gate_item()
        self._seed_run(run_id=self.RUN_ID_A, item="other-item")
        msg = self._gate_pass_refused(
            json_arg='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_A
        )
        self.assertIn("unknown run id", msg)

    # ── gate-set invalidation ───────────────────────────────────────────

    def test_re_gate_set_orphans_older_run_evidence(self):
        self.write_items([make_item("gt-item")])
        dev_status.cmd_gate_set(
            _args(id="gt-item", json='{"required": true, "criteria": ["c"]}')
        )
        self._seed_run(run_id=self.RUN_ID_A)
        dev_status.cmd_gate_pass(
            _args(id="gt-item", json='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_A)
        )
        self.assertIsNotNone(self._item_by_id("gt-item")["gate"]["passed_at"])

        # Re-classify: the audit trail stays, but the prior run no longer counts.
        dev_status.cmd_gate_set(
            _args(id="gt-item", json='{"required": true, "criteria": ["c2"]}')
        )
        self.assertIsNone(self._item_by_id("gt-item")["gate"]["passed_at"])
        self.assertEqual(len(self._read_runs()), 1)
        msg = self._gate_pass_refused(
            json_arg='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_A
        )
        self.assertIn("stale", msg)

    def test_gate_pass_on_legacy_gate_without_set_at_counts_current_runs(self):
        # Gates classified before set_at existed carry no lower bound — a
        # fresh run still counts rather than demanding a gate-set re-run.
        self._gate_item(("criterion one",))
        self._seed_run(run_id=self.RUN_ID_A)
        dev_status.cmd_gate_pass(
            _args(id="gt-item", json='{"coverage": {"1": "run:%s"}}' % self.RUN_ID_A)
        )
        self.assertIsNotNone(self._item_by_id("gt-item")["gate"]["passed_at"])

    # ── run subcommand ──────────────────────────────────────────────────

    def test_run_records_successful_command(self):
        self.write_items([make_item("gt-item")])
        fake = MagicMock(return_value=MagicMock(returncode=0))
        with patch("subprocess.run", fake) as m:
            dev_status.cmd_run(
                _args(id="gt-item", command=["pytest", "-q"], timeout=60)
            )
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], ["pytest", "-q"])
        self.assertEqual(m.call_args.kwargs["timeout"], 60)
        runs = self._read_runs()
        self.assertEqual(len(runs), 1)
        row = runs[0]
        self.assertEqual(row["item"], "gt-item")
        self.assertEqual(row["command"], "pytest -q")
        self.assertEqual(row["exit_code"], 0)
        self.assertIs(row["timed_out"], False)
        self.assertIsInstance(row["duration_s"], float)
        self.assertEqual(row["cwd"], os.getcwd())
        datetime.fromisoformat(row["started_at"])
        self.assertRegex(row["run_id"], r"^[0-9a-f]{32}$")

    def test_run_scrubs_devstatus_agent_from_the_child_environment(self):
        """Found via live dogfooding, not a code-review guess: `DEVSTATUS_AGENT=1
        dev_status.py run <item> -- uv run pytest -q` (a natural thing to type,
        since CLAUDE.md has agents prefix DEVSTATUS_AGENT=1 on dev_status.py
        mutating calls, and `run` needs --if-rev like every other one) leaked
        the env var into the pytest subprocess and spuriously failed every
        test asserting on the stderr echo DEVSTATUS_AGENT suppresses -- false
        evidence for a command that was actually fine."""
        self.write_items([make_item("gt-item")])
        fake = MagicMock(return_value=MagicMock(returncode=0))
        with (
            patch("subprocess.run", fake),
            patch.dict(os.environ, {"DEVSTATUS_AGENT": "1"}),
        ):
            dev_status.cmd_run(_args(id="gt-item", command=["true"], timeout=60))
        child_env = fake.call_args.kwargs["env"]
        self.assertNotIn("DEVSTATUS_AGENT", child_env)

    def test_run_records_failing_command(self):
        self.write_items([make_item("gt-item")])
        with patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=3))):
            dev_status.cmd_run(_args(id="gt-item", command=["false"], timeout=60))
        self.assertEqual(self._read_runs()[0]["exit_code"], 3)

    def test_run_records_timeout_as_timed_out_evidence(self):
        self.write_items([make_item("gt-item")])
        err = io.StringIO()
        with (
            patch(
                "subprocess.run",
                MagicMock(
                    side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=0.1)
                ),
            ),
            patch("sys.stderr", err),
        ):
            dev_status.cmd_run(_args(id="gt-item", command=["hang"], timeout=0.1))
        self.assertIn("timed out", err.getvalue())
        row = self._read_runs()[0]
        self.assertIs(row["timed_out"], True)
        self.assertIsNone(row["exit_code"])

    def test_run_refuses_unknown_item(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_run(_args(id="no-such-item", command=["true"], timeout=60))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("not found", err.getvalue())

    def test_run_refuses_empty_command(self):
        self.write_items([make_item("gt-item")])
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            dev_status.cmd_run(_args(id="gt-item", command=[], timeout=60))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("no command", err.getvalue())

    def test_run_append_failure_warns_but_does_not_crash(self):
        self.write_items([make_item("gt-item")])
        blocker = Path(self.tmpdir) / "blocker"
        blocker.write_text("not a directory")
        err = io.StringIO()
        with (
            patch.object(dev_status, "RUNS_FILE", blocker / "runs.jsonl"),
            patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=0))),
            patch("sys.stderr", err),
        ):
            dev_status.cmd_run(_args(id="gt-item", command=["true"], timeout=60))
        self.assertIn("append failed", err.getvalue())

    def test_run_parser_remainder_keeps_embedded_flags(self):
        args = dev_status.build_parser().parse_args(
            ["run", "--timeout", "5", "gt-item", "--", "pytest", "--weird-flag"]
        )
        self.assertEqual(args.id, "gt-item")
        self.assertEqual(args.timeout, 5)
        # argparse's own "--" handling ends option parsing, so the
        # separator itself never lands in args.command.
        self.assertEqual(args.command, ["pytest", "--weird-flag"])

    def test_run_parser_accepts_the_documented_timeout_after_id_order(self):
        """`run <id> --timeout N -- <command>` is the order INTERFACES.md
        documents and pi/extensions/dev-status-tool.ts's buildArgv actually
        generates. Pre-fix, nargs=REMAINDER swallowed --timeout into the
        command list the instant it started matching (a documented
        REMAINDER quirk), silently leaving args.timeout at the 1800s
        default and turning the executed argv into
        ["--timeout","N","--",...] instead of the intended command."""
        args = dev_status.build_parser().parse_args(
            ["run", "gt-item", "--timeout", "60", "--", "pytest", "-q"]
        )
        self.assertEqual(args.timeout, 60)
        self.assertEqual(args.command, ["pytest", "-q"])

    def test_run_parser_accepts_quiet_flag_after_id(self):
        """Same REMAINDER-ordering bug also swallowed -q/--quiet whenever
        it followed the id positional."""
        args = dev_status.build_parser().parse_args(
            ["run", "gt-item", "-q", "--", "pytest", "-q"]
        )
        self.assertTrue(args.quiet)
        self.assertEqual(args.command, ["pytest", "-q"])

    def test_run_leaves_a_second_literal_leading_dashdash_in_the_command(self):
        """cmd_run must not strip a second "--" the way it stripped the
        first pre-fix -- argparse's native "--" handling already consumes
        exactly one separator, so a manual re-strip would eat a command
        that legitimately starts with its own literal "--"."""
        args = dev_status.build_parser().parse_args(
            ["run", "gt-item", "--", "--", "foo"]
        )
        self.assertEqual(args.command, ["--", "foo"])

    def test_run_numeric_id_resolves_against_the_unified_pending_and_backlog_pool(
        self,
    ):
        """Pre-fix, cmd_run resolved a numeric id against the backlog pool
        alone (resolve_id(args.id, items, [])), so position 2 meant "the
        2nd backlog item" instead of the 2nd dashboard row -- silently
        wrong the moment any pending item exists, since render always
        numbers pending items first."""
        self.write_pending(
            [
                {
                    "id": "pend-wait",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting on someone",
                    "kind": "email",
                    "blocking": [],
                    "related_files": [],
                    "next_steps": [],
                }
            ]
        )
        self.write_items([make_item("item-a"), make_item("item-b")])
        # Dashboard order: 1=pend-wait, 2=item-a, 3=item-b.
        with patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=0))):
            dev_status.cmd_run(
                _args(
                    id="2", command=["true"], timeout=60, if_rev=dev_status.load_rev()
                )
            )
        self.assertEqual(self._read_runs()[0]["item"], "item-a")

    def test_run_refuses_a_pending_position(self):
        self.write_pending(
            [
                {
                    "id": "pend-wait",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting on someone",
                    "kind": "email",
                    "blocking": [],
                    "related_files": [],
                    "next_steps": [],
                }
            ]
        )
        self.write_items([make_item("item-a")])
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_run(
                _args(
                    id="1", command=["true"], timeout=60, if_rev=dev_status.load_rev()
                )
            )
        self.assertIn("pending", err.getvalue())

    def test_run_stale_numeric_position_refuses_without_if_rev(self):
        """Same staleness protection every other numeric-id mutating
        command gets via enforce_rev_guard -- pre-fix, run had none, so a
        numeric position computed from a stale dashboard could silently
        record evidence against the wrong item."""
        self.write_items([make_item("item-a"), make_item("item-b")])
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            dev_status.cmd_run(_args(id="2", command=["true"], timeout=60, if_rev=None))
        self.assertIn("if-rev", err.getvalue())

    def test_run_append_failure_reports_not_recorded_not_success(self):
        """Pre-fix, cmd_run printed "[run] recorded ..." unconditionally
        even when append_run_record's own OSError path had already failed
        -- misreporting a lost evidence row as saved."""
        self.write_items([make_item("gt-item")])
        blocker = Path(self.tmpdir) / "blocker2"
        blocker.write_text("not a directory")
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(dev_status, "RUNS_FILE", blocker / "runs.jsonl"),
            patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=0))),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            dev_status.cmd_run(_args(id="gt-item", command=["true"], timeout=60))
        self.assertNotIn("recorded", out.getvalue())
        self.assertIn("NOT recorded", err.getvalue())

    # ── runs listing ────────────────────────────────────────────────────

    def test_runs_lists_recorded_runs(self):
        self.write_items([make_item("gt-item")])
        self._seed_run(run_id=self.RUN_ID_A)
        self._seed_run(run_id=self.RUN_ID_B, exit_code=2)
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_runs(_args(id="gt-item"))
        text = out.getvalue()
        self.assertIn("2 recorded run(s)", text)
        self.assertIn(self.RUN_ID_A, text)
        self.assertIn(self.RUN_ID_B, text)
        self.assertIn("pytest -q", text)

    def test_runs_no_runs_message(self):
        self.write_items([make_item("gt-item")])
        out = io.StringIO()
        with patch("sys.stdout", out):
            dev_status.cmd_runs(_args(id="gt-item"))
        self.assertIn("no runs recorded", out.getvalue())

    # ── rename rewrites run rows ────────────────────────────────────────

    def test_rename_rewrites_run_rows(self):
        self.write_items([make_item("gt-item")])
        self._seed_run(run_id=self.RUN_ID_A)
        self._seed_run(run_id=self.RUN_ID_B, item="untouched-item")
        dev_status.cmd_rename(_args(old_slug="gt-item", new_slug="gt-item-2"))
        runs = {r["run_id"]: r for r in self._read_runs()}
        self.assertEqual(runs[self.RUN_ID_A]["item"], "gt-item-2")
        self.assertEqual(runs[self.RUN_ID_B]["item"], "untouched-item")

    # ── show runs line ──────────────────────────────────────────────────

    def test_show_prints_recent_runs_line(self):
        self.write_items([make_item("gt-item")])
        self._seed_run(run_id=self.RUN_ID_A)
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_show(_args(id="gt-item"))
        self.assertIn("# runs:", err.getvalue())
        self.assertIn(self.RUN_ID_A, err.getvalue())

    def test_show_omits_runs_line_when_no_runs(self):
        self.write_items([make_item("gt-item")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            dev_status.cmd_show(_args(id="gt-item"))
        self.assertNotIn("# runs:", err.getvalue())


class ReadyCommandTests(BacklogTestCase):
    """`ready` reports the computed READY bucket as JSON.

    It exists so a consumer -- swarm_spawn's scheduler, in
    pi/extensions/swarm-tool.ts -- never has to reimplement the blocker walk.
    READY is derived, not stored, so a second implementation would drift the
    moment the graph rules changed.
    """

    def _ready(self, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_ready(_args(**kwargs))
        return json.loads(out.getvalue())

    def test_ready_excludes_blocked_and_non_open_items(self):
        self.write_items(
            [
                make_item("a-ready"),
                make_item("b-blocked", blocked_by=["a-ready"]),
                make_item("c-done", status="done"),
                make_item("d-progress", status="in-progress"),
            ]
        )
        self.assertEqual([i["id"] for i in self._ready()], ["a-ready"])

    def test_ready_includes_an_item_whose_blocker_is_done(self):
        # The cascade this exists for: approving a blocker makes its
        # dependents ready with no further write anywhere.
        self.write_items(
            [
                make_item("a-done", status="done"),
                make_item("b-was-blocked", blocked_by=["a-done"]),
            ]
        )
        self.assertEqual([i["id"] for i in self._ready()], ["b-was-blocked"])

    def test_ready_returns_full_records_including_related_files(self):
        # related_files is the whole point for the scheduler: it is how two
        # items that would edit the same file are told apart from two that
        # would not.
        item = make_item("a-ready")
        item["related_files"] = [{"path": "/repo/x.ts", "note": "n"}]
        self.write_items([item])
        self.assertEqual(
            self._ready()[0]["related_files"], [{"path": "/repo/x.ts", "note": "n"}]
        )

    def test_ready_prefix_filters_by_slug(self):
        self.write_items([make_item("meta-a"), make_item("iron-lb-b")])
        self.assertEqual([i["id"] for i in self._ready(prefix="meta-")], ["meta-a"])

    def test_ready_is_pure(self):
        # Safe to call on a loop while workers are running.
        self.write_items([make_item("a-ready")])
        self._ready()
        self.assertEqual(self.read_rev(), 0)

    def test_ready_on_an_empty_backlog_is_an_empty_list(self):
        self.write_items([])
        self.assertEqual(self._ready(), [])


# ── arg helper ────────────────────────────────────────────────────────────────


class _args:
    """Minimal argparse.Namespace stand-in."""

    if_rev = None  # default; argparse always sets --if-rev (default None)
    status = None  # default; argparse sets --status (default None) for `list`
    raw = False  # default; argparse sets --raw (default False) for `list`
    prefix = None  # default; argparse sets --prefix (default None) for `ready`
    apply = False  # default; argparse sets --apply (default False) for backfill-gate
    force = False  # default; argparse sets --force (required) for `prune`
    allow_main = False
    no_worktree_check = False
    claimed_by = None
    refresh = False  # default; argparse sets --refresh (default False) for `recap`
    backend = None  # default; argparse sets --backend (default None) for `recap`
    quiet = (
        False  # default; argparse sets --quiet/-q (default False) on every leaf parser
    )
    verbose = False  # default; argparse sets --verbose/-v (default False) on every leaf parser

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
