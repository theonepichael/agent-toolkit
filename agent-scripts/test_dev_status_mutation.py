#!/usr/bin/env python3
"""Tests for the dev_status mutation service (candidate 12).

Tier 1 (MutationCliCharacterizationTestCase) was written BEFORE the
extraction and runs against the pre-refactor command handlers: one test
per ``cmd_*`` mutation asserting today's compact structured-output line
(``DEVSTATUS_AGENT=1``) and exit code against a temporary backlog store,
so the extraction to ``dev_status_mutation.py`` is proven
behavior-preserving command by command. These tests invoke the CLI layer
exactly as argparse does — they must never import the service module.

Tiers 2 and 3 (MutationServiceTestCase, LockContentionTestCase,
CrashSafetyTestCase) cover the extracted service: typed requests and
errors, lock contention semantics, and the store's verified
crash-safety ordering (rev bump before write before journal). They are
meaningful only once ``dev_status_mutation`` exists.

Run standalone: python3 test_dev_status_mutation.py  (from agent-scripts/)
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import dev_status  # noqa: E402  (path insert above)
import dev_status_mutation
import dev_status_storage


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
    claimed_by=None,
):
    """Build one stored-shape backlog item for tests."""
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
    if claimed_by is not None:
        item["claimed_by"] = claimed_by
    return item


class _Args:
    """Minimal argparse.Namespace stand-in (mirrors test_dev_status._args)."""

    if_rev = None
    command = None
    timeout = None
    cwd = None
    allow_main = False
    no_worktree_check = False
    claimed_by = None
    quiet = False
    verbose = False
    compact = False
    full = False

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MutationFixture(unittest.TestCase):
    """Temp backlog store with all dev_status path globals patched."""

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
        # Never let a test spawn a real process against fabricated data
        # (mirrors BacklogFixture's rationale in test_dev_status.py).
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

    # ── store helpers ──────────────────────────────────────────────────────

    def write_items(self, items):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.items_file.write_text(
            json.dumps({"schema_version": 2, "items": items}, indent=2)
        )

    def write_pending(self, pending_items):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text(
            json.dumps({"schema_version": 1, "items": pending_items}, indent=2)
        )

    def read_items(self):
        return dev_status.load_items()

    def read_pending(self):
        return dev_status.load_pending()

    def read_rev(self):
        return dev_status.load_rev()

    def journal_lines(self):
        if not self.journal_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.journal_file.read_text().splitlines()
            if line.strip()
        ]

    def _item_by_id(self, slug):
        return {i["id"]: i for i in self.read_items()}[slug]

    # ── invocation helpers ─────────────────────────────────────────────────

    def run_cmd(self, func, args):
        """Run one cmd_* handler in compact mode; return (stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict("sys.modules", {}),
            patch.dict("os.environ", {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            func(args)
        return out.getvalue(), err.getvalue()

    def run_cmd_exits(self, func, args):
        """Run one cmd_* handler expecting SystemExit(1); return (stderr, code)."""
        err = io.StringIO()
        with (
            patch.dict("os.environ", {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", err),
        ):
            with self.assertRaises(SystemExit) as raised:
                func(args)
        return err.getvalue(), raised.exception.code


class MutationCliCharacterizationTestCase(MutationFixture):
    """Tier 1: today's compact output line and exit code, per command."""

    def _compact(self, out):
        lines = [line for line in out.splitlines() if line.startswith("[")]
        self.assertEqual(len(lines), 1, f"expected one compact line, got: {out!r}")
        return lines[0]

    def test_add_compact_line(self):
        out, _ = self.run_cmd(
            dev_status.cmd_add,
            _Args(json='{"id": "my-feature", "summary": "Test feature"}'),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[add] slug=my-feature status=open rev={rev} detail="Test feature"',
        )
        self.assertEqual(self._item_by_id("my-feature")["status"], "open")

    def test_add_missing_id_refuses_with_suggestion(self):
        err, code = self.run_cmd_exits(
            dev_status.cmd_add,
            _Args(json='{"summary": "Fix the broken widget"}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("'id' is required — suggested slug:", err)

    def test_add_duplicate_refuses(self):
        self.write_items([make_item("my-feature")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_add,
            _Args(json='{"id": "my-feature", "summary": "Dup"}'),
        )
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "[add] duplicate slug: my-feature")

    def test_update_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        out, _ = self.run_cmd(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"context": "New context"}'),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[update] slug=item-one status=in-progress rev={rev} '
            f'detail="updated context: Summary of item-one"',
        )

    def test_update_numeric_without_if_rev_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="1", patch='{"context": "x"}'),
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "[update] numeric id '1' requires --if-rev <N> to guard against a "
            "stale position — refusing (no write).",
            err,
        )
        self.assertIn("[update] current rev is 0.", err)

    def test_update_stale_if_rev_refuses_no_write(self):
        self.write_items([make_item("item-one")])
        before = self.read_rev()
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="1", if_rev=99, patch='{"context": "x"}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("[update] stale rev: --if-rev 99 given, current is 0", err)
        self.assertEqual(self.read_rev(), before)

    def test_update_immutable_field_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"id": "other-slug"}'),
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            err.strip(), "[update] cannot modify immutable field(s): id"
        )

    def test_update_blocked_by_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"blocked_by": ["two-slug"]}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot modify 'blocked_by' directly", err)

    def test_start_compact_line(self):
        self.write_items([make_item("item-one")])
        out, _ = self.run_cmd(dev_status.cmd_start, _Args(id="item-one"))
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[start] slug=item-one status=in-progress rev={rev} '
            f'detail="Summary of item-one"',
        )
        self.assertIn("claimed_by", self._item_by_id("item-one"))

    def test_start_numeric_ref_in_compact_line(self):
        self.write_items([make_item("item-one")])
        out, _ = self.run_cmd(dev_status.cmd_start, _Args(id="1", if_rev=0))
        line = self._compact(out)
        self.assertIn('slug=item-one status=in-progress rev=', line)
        self.assertIn('ref="1"', line)

    def test_start_in_review_refuses(self):
        self.write_items([make_item("item-one", status="in-review")])
        err, code = self.run_cmd_exits(dev_status.cmd_start, _Args(id="item-one"))
        self.assertEqual(code, 1)
        self.assertIn("[start] item-one is in-review --", err)

    def test_done_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        out, _ = self.run_cmd(dev_status.cmd_done, _Args(id="item-one"))
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[done] slug=item-one status=done rev={rev} '
            f'detail="Summary of item-one"',
        )
        self.assertEqual(self._item_by_id("item-one")["status"], "done")

    def test_done_unmet_gate_refuses(self):
        self.write_items(
            [
                make_item(
                    "item-one",
                    status="in-progress",
                    gate={"required": True, "criteria": ["passes tests"], "passed_at": None},
                )
            ]
        )
        before = self.read_rev()
        err, code = self.run_cmd_exits(dev_status.cmd_done, _Args(id="item-one"))
        self.assertEqual(code, 1)
        self.assertIn("[done] item-one has an unmet gate (1 criterion/criteria", err)
        # Refusal raises before bumping rev or writing items.
        self.assertEqual(self.read_rev(), before)
        # Nothing was written: the item is still in-progress.
        self.assertEqual(self._item_by_id("item-one")["status"], "in-progress")

    def test_review_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        out, _ = self.run_cmd(dev_status.cmd_review, _Args(id="item-one"))
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[review] slug=item-one status=in-review rev={rev} '
            f'detail="Summary of item-one"',
        )

    def test_approve_compact_line_and_hash_drift(self):
        self.write_items([make_item("item-one", status="in-progress")])
        self.run_cmd(dev_status.cmd_review, _Args(id="item-one"))
        out, _ = self.run_cmd(dev_status.cmd_approve, _Args(id="item-one"))
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[approve] slug=item-one status=done rev={rev} '
            f'detail="Summary of item-one"',
        )

        # hash drift refusal: mutate content after review, approve must refuse
        self.write_items([make_item("item-two", status="in-progress")])
        self.run_cmd(dev_status.cmd_review, _Args(id="item-two"))
        items = self.read_items()
        by_id = {i["id"]: i for i in items}
        by_id["item-two"]["context"] = "changed after review"
        self.write_items(items)
        err, code = self.run_cmd_exits(dev_status.cmd_approve, _Args(id="item-two"))
        self.assertEqual(code, 1)
        self.assertIn("content changed since it was submitted for review", err)

    def test_reject_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        self.run_cmd(dev_status.cmd_review, _Args(id="item-one"))
        out, _ = self.run_cmd(
            dev_status.cmd_reject, _Args(id="item-one", feedback="needs work")
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[reject] slug=item-one status=in-progress rev={rev} '
            f'detail="Summary of item-one"',
        )
        self.assertEqual(self._item_by_id("item-one")["review_feedback"], "needs work")

    def test_reject_empty_feedback_refuses(self):
        self.write_items([make_item("item-one", status="in-review")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_reject, _Args(id="item-one", feedback="   ")
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            err.strip(), "[reject] feedback is required and cannot be empty"
        )

    def test_block_compact_line(self):
        self.write_items([make_item("item-one"), make_item("two-slug")])
        out, _ = self.run_cmd(
            dev_status.cmd_block, _Args(id="item-one", blocker="two-slug")
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[block] slug=item-one status=open rev={rev} '
            f'detail="blocked by two-slug"',
        )

    def test_block_cycle_refuses(self):
        self.write_items(
            [
                make_item("item-one", blocked_by=["two-slug"]),
                make_item("two-slug", blocked_by=[]),
            ]
        )
        err, code = self.run_cmd_exits(
            dev_status.cmd_block, _Args(id="two-slug", blocker="item-one")
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            err.strip(),
            "[block] would create a cycle: item-one already depends on two-slug",
        )

    def test_unblock_compact_line(self):
        self.write_items(
            [
                make_item("item-one", blocked_by=["two-slug"]),
                make_item("two-slug"),
            ]
        )
        out, _ = self.run_cmd(
            dev_status.cmd_unblock, _Args(id="item-one", blocker="two-slug")
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[unblock] slug=item-one status=open rev={rev} '
            f'detail="unblocked from two-slug"',
        )

    def test_unblock_missing_blocker_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_unblock, _Args(id="item-one", blocker="two-slug")
        )
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "[unblock] item-one is not blocked by two-slug")

    def test_gate_set_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        out, _ = self.run_cmd(
            dev_status.cmd_gate_set,
            _Args(id="item-one", json='{"required": true, "criteria": ["passes tests"]}'),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[gate-set] slug=item-one status=in-progress rev={rev} '
            f'detail="gate set (1 criteria, required=true)"',
        )

    def test_gate_set_empty_required_criteria_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_gate_set,
            _Args(id="item-one", json='{"required": true, "criteria": []}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("criteria' cannot be empty when required=true", err)

    def test_gate_pass_compact_line_via_manual(self):
        self.write_items(
            [
                make_item(
                    "item-one",
                    status="in-progress",
                    gate={
                        "required": True,
                        "criteria": ["passes tests"],
                        "passed_at": None,
                        "set_at": "2026-01-01T00:00:00+00:00",
                    },
                )
            ]
        )
        out, _ = self.run_cmd(
            dev_status.cmd_gate_pass,
            _Args(id="item-one", json='{"coverage": {"1": "manual:eye-balled it"}}'),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[gate-pass] slug=item-one status=in-progress rev={rev} '
            f'detail="gate passed via manual"',
        )

    def test_gate_pass_bare_coverage_refuses(self):
        self.write_items(
            [
                make_item(
                    "item-one",
                    gate={"required": True, "criteria": ["a"], "passed_at": None},
                )
            ]
        )
        err, code = self.run_cmd_exits(
            dev_status.cmd_gate_pass, _Args(id="item-one", json="{}")
        )
        self.assertEqual(code, 1)
        self.assertIn("coverage payload is required", err)

    def test_rename_compact_line_rewrites_references(self):
        self.write_items(
            [
                make_item("item-one", blocked_by=[], context="see item-one later"),
                make_item("two-slug", blocked_by=["item-one"]),
            ]
        )
        self.write_runs_row("item-one", "abc123")
        out, _ = self.run_cmd(
            dev_status.cmd_rename, _Args(old_slug="item-one", new_slug="renamed-one")
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[rename] slug=renamed-one status=open rev={rev} '
            f'ref="item-one" detail="renamed from item-one"',
        )
        self.assertEqual(self._item_by_id("renamed-one")["context"], "see renamed-one later")
        self.assertEqual(self._item_by_id("two-slug")["blocked_by"], ["renamed-one"])
        runs = dev_status.load_runs("renamed-one")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], "abc123")

    def test_remove_compact_line_purges_references(self):
        self.write_items(
            [
                make_item("item-one", status="in-progress"),
                make_item("two-slug", blocked_by=["item-one"]),
            ]
        )
        self.write_pending(
            [
                {
                    "id": "pend-one",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting",
                    "kind": "email",
                    "source_ref": {},
                    "context": "",
                    "next_steps": [],
                    "blocking": ["item-one"],
                    "outcome": None,
                }
            ]
        )
        out, _ = self.run_cmd(dev_status.cmd_remove, _Args(id="item-one"))
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[remove] slug=item-one status=removed rev={rev} '
            f'detail="Summary of item-one"',
        )
        self.assertEqual([i["id"] for i in self.read_items()], ["two-slug"])
        self.assertEqual(self._item_by_id("two-slug")["blocked_by"], [])
        self.assertEqual(self.read_pending()[0]["blocking"], [])

    def test_run_records_evidence_and_compact_line(self):
        self.write_items([make_item("item-one")])
        proc = MagicMock()
        proc.returncode = 0
        out = io.StringIO()
        with (
            patch.dict("os.environ", {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stdout", out),
            patch("sys.stderr", io.StringIO()),
            patch("subprocess.run", return_value=proc) as run_mock,
        ):
            dev_status.cmd_run(_Args(id="item-one", command=["pytest", "-q"]))
        line = [line for line in out.getvalue().splitlines() if line.startswith("[")][0]
        self.assertTrue(line.startswith("[run] recorded "), line)
        self.assertIn(" for item-one: exit 0 after ", line)
        run_mock.assert_called_once()
        argv = run_mock.call_args.args[0]
        self.assertEqual(argv, ["pytest", "-q"])
        runs = dev_status.load_runs("item-one")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["exit_code"], 0)
        self.assertFalse(runs[0]["timed_out"])

    def test_run_empty_command_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(dev_status.cmd_run, _Args(id="item-one", command=[]))
        self.assertEqual(code, 1)
        self.assertIn("[run] no command given", err)

    def test_run_timed_out_still_records(self):
        self.write_items([make_item("item-one")])
        err = io.StringIO()
        with (
            patch.dict("os.environ", {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", err),
            patch("subprocess.run", side_effect=dev_status.subprocess.TimeoutExpired("cmd", 5)),
        ):
            dev_status.cmd_run(_Args(id="item-one", command=["sleep", "10"], timeout=5))
        self.assertIn("[run] command timed out after 5s", err.getvalue())
        runs = dev_status.load_runs("item-one")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["timed_out"])
        self.assertIsNone(runs[0]["exit_code"])

    def test_pending_add_compact_line(self):
        self.write_items([make_item("item-one")])
        out, _ = self.run_cmd(
            dev_status.cmd_pending_add,
            _Args(
                json=(
                    '{"id": "wait-reply", "description": "Waiting on reply", '
                    '"kind": "email", "blocking": ["item-one"]}'
                )
            ),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[pending add] slug=wait-reply status=waiting_for_reply rev={rev} '
            f'detail="Waiting on reply"',
        )
        self.assertEqual(self.read_pending()[0]["id"], "wait-reply")

    def test_pending_add_duplicate_refuses(self):
        self.write_pending(
            [
                {
                    "id": "wait-reply",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "x",
                    "kind": "email",
                    "source_ref": {},
                    "context": "",
                    "next_steps": [],
                    "blocking": [],
                    "outcome": None,
                }
            ]
        )
        err, code = self.run_cmd_exits(
            dev_status.cmd_pending_add,
            _Args(json='{"id": "wait-reply", "description": "y", "kind": "chat"}'),
        )
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "[pending add] duplicate id: wait-reply")

    def test_pending_update_compact_line_and_transition(self):
        self.write_pending(
            [
                {
                    "id": "wait-reply",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "Waiting on reply",
                    "kind": "email",
                    "source_ref": {},
                    "context": "",
                    "next_steps": [],
                    "blocking": [],
                    "outcome": None,
                }
            ]
        )
        out, _ = self.run_cmd(
            dev_status.cmd_pending_update,
            _Args(id="wait-reply", patch='{"status": "reply_received"}'),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self.assertEqual(
            line,
            f'[pending update] slug=wait-reply status=reply_received rev={rev} '
            f'detail="Waiting on reply"',
        )
        self.assertEqual(self.read_pending()[0]["status"], "reply_received")

    def test_pending_update_unknown_field_refuses(self):
        self.write_pending(
            [
                {
                    "id": "wait-reply",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "x",
                    "kind": "email",
                    "source_ref": {},
                    "context": "",
                    "next_steps": [],
                    "blocking": [],
                    "outcome": None,
                }
            ]
        )
        err, code = self.run_cmd_exits(
            dev_status.cmd_pending_update,
            _Args(id="wait-reply", patch='{"created": "2026-02-02"}'),
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            err.strip(), "[pending update] cannot update field(s): created"
        )

    def test_journal_events_per_command(self):
        self.write_items([make_item("item-one", status="in-progress")])
        self.run_cmd(dev_status.cmd_done, _Args(id="item-one"))
        events = self.journal_lines()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["cmd"], "done")
        self.assertEqual(events[0]["kind"], "backlog")
        self.assertEqual(events[0]["from_status"], "in-progress")
        self.assertEqual(events[0]["to_status"], "done")

    # ── helpers ─────────────────────────────────────────────────────────────

    def write_runs_row(self, slug, run_id):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "run_id": run_id,
            "item": slug,
            "command": "pytest -q",
            "exit_code": 0,
            "timed_out": False,
            "started_at": "2026-01-01T00:00:00+00:00",
            "duration_s": 1.0,
            "cwd": "/tmp",
        }
        with open(self.runs_file, "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")



# ── Tier 2: Service Unit Tests ───────────────────────────────────────────────


class MutationServiceTestCase(MutationFixture):
    """Unit tests for dev_status_mutation service functions, requests, and errors."""

    def test_add_item_success(self):
        req = dev_status_mutation.NewItemRequest(
            id="item-new",
            summary="Brand new item",
            category="chore",
            context="Some context",
            next_steps="Next steps",
            priority="high",
        )
        res = dev_status_mutation.add_item(req)
        self.assertIsInstance(res, dev_status_mutation.MutationResult)
        self.assertEqual(res.cmd, "add")
        self.assertEqual(res.slug, "item-new")
        self.assertEqual(res.status, "open")
        self.assertEqual(res.rev, 1)
        self.assertEqual(res.detail, "Brand new item")
        self.assertEqual(res.item["category"], "chore")
        self.assertEqual(res.item["priority"], "high")

    def test_update_item_success(self):
        self.write_items([make_item("item-1", summary="Old summary", status="open")])
        req = dev_status_mutation.ItemUpdateRequest(summary="Updated summary", priority="low")
        res = dev_status_mutation.update_item("item-1", req)
        self.assertEqual(res.cmd, "update")
        self.assertEqual(res.slug, "item-1")
        self.assertEqual(res.item["summary"], "Updated summary")
        self.assertEqual(res.item["priority"], "low")

    def test_start_and_done_item_success(self):
        self.write_items([make_item("item-1", status="open")])
        res_start = dev_status_mutation.start_item("item-1")
        self.assertEqual(res_start.cmd, "start")
        self.assertEqual(res_start.status, "in-progress")

        res_done = dev_status_mutation.done_item("item-1")
        self.assertEqual(res_done.cmd, "done")
        self.assertEqual(res_done.status, "done")

    def test_review_and_approve_cycle(self):
        self.write_items([make_item("item-rev", status="in-progress")])
        res_rev = dev_status_mutation.review_item("item-rev")
        self.assertEqual(res_rev.status, "in-review")
        self.assertIn("review_content_hash", res_rev.item)

        res_app = dev_status_mutation.approve_item("item-rev")
        self.assertEqual(res_app.status, "done")

    def test_reject_item_success(self):
        self.write_items([make_item("item-rej", status="in-progress")])
        dev_status_mutation.review_item("item-rej")
        res_rej = dev_status_mutation.reject_item("item-rej", "needs more tests")
        self.assertEqual(res_rej.status, "in-progress")
        self.assertEqual(res_rej.item.get("review_feedback"), "needs more tests")

    def test_gate_set_and_pass(self):
        self.write_items([make_item("item-gt", status="in-progress")])
        set_req = dev_status_mutation.GateSetRequest(required=True, criteria=("test pass",))
        res_set = dev_status_mutation.set_gate("item-gt", set_req)
        self.assertEqual(res_set.item["gate"]["required"], True)

        pass_req = dev_status_mutation.GatePassRequest(coverage={"1": "manual: verified"})
        res_pass = dev_status_mutation.pass_gate("item-gt", pass_req)
        self.assertIsNotNone(res_pass.item["gate"]["passed_at"])
        self.assertEqual(res_pass.item["gate"]["passed_via"], "manual")

    def test_rename_item_success(self):
        self.write_items([make_item("old-slug")])
        res = dev_status_mutation.rename_item("old-slug", "new-slug")
        self.assertEqual(res.slug, "new-slug")
        self.assertEqual(res.ref, "old-slug")

    def test_block_and_unblock(self):
        self.write_items([make_item("item-a"), make_item("item-b")])
        res_blk = dev_status_mutation.block_item("item-a", "item-b")
        self.assertIn("item-b", res_blk.item["blocked_by"])

        res_unblk = dev_status_mutation.unblock_item("item-a", "item-b")
        self.assertNotIn("item-b", res_unblk.item["blocked_by"])

    def test_pending_lifecycle(self):
        add_req = dev_status_mutation.PendingAddRequest(
            id="pend-1",
            description="Waiting for reply",
            kind="email",
        )
        res_add = dev_status_mutation.add_pending_item(add_req)
        self.assertEqual(res_add.slug, "pend-1")
        self.assertEqual(res_add.status, "waiting_for_reply")

        up_req = dev_status_mutation.PendingUpdateRequest(status="reply_received")
        res_up = dev_status_mutation.update_pending_item("pend-1", up_req)
        self.assertEqual(res_up.status, "reply_received")

    def test_remove_item_success(self):
        self.write_items([make_item("item-del")])
        res = dev_status_mutation.remove_item("item-del")
        self.assertEqual(res.status, "removed")
        self.assertFalse(any(i["id"] == "item-del" for i in res.items))

    def test_revision_conflict_error(self):
        self.write_items([make_item("item-rev-err")])
        with self.assertRaises(dev_status_mutation.RevisionConflictError) as cm:
            dev_status_mutation.done_item("1", if_rev=999)
        err = cm.exception
        self.assertEqual(err.exit_code, 1)
        self.assertIn("stale rev", err.message)
        self.assertIsNotNone(err.items)
        self.assertIsNotNone(err.pending_items)

    def test_not_found_error(self):
        self.write_items([])
        with self.assertRaises(dev_status_mutation.NotFoundError) as cm:
            dev_status_mutation.done_item("non-existent")
        self.assertIn("not found", cm.exception.message)

    def test_claim_collision_error(self):
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        item = make_item("task-claimed", status="in-progress")
        item["claimed_by"] = {
            "harness": "pi",
            "machine_id": dev_status.machine_id(),
            "pid": 888888,
            "owner_pid": 888888,
            "claimed_at": now_iso,
            "last_active": now_iso,
        }
        self.write_items([item])
        with patch.object(dev_status_mutation, "_is_pid_alive", return_value=True):
            with self.assertRaises(dev_status_mutation.ClaimCollisionError) as cm:
                dev_status_mutation.start_item("task-claimed")
            self.assertIn("claimed by pi", cm.exception.message)

    def test_cycle_error_subclasses_validation_error(self):
        self.write_items([
            make_item("a", blocked_by=["b"]),
            make_item("b", blocked_by=["c"]),
            make_item("c"),
        ])
        with self.assertRaises(dev_status_mutation.CycleError) as cm:
            dev_status_mutation.block_item("c", "a")
        self.assertIsInstance(cm.exception, dev_status_mutation.ValidationError)
        self.assertIn("would create a cycle", cm.exception.message)

    def test_invalid_item_state_error(self):
        self.write_items([make_item("not-in-review", status="open")])
        with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
            dev_status_mutation.approve_item("not-in-review")
        self.assertIn("not in-review", cm.exception.message)

    def test_gate_unmet_error(self):
        self.write_items([
            make_item(
                "gated-item",
                status="in-progress",
                gate={"required": True, "criteria": ["must pass"], "passed_at": None},
            )
        ])
        with self.assertRaises(dev_status_mutation.GateUnmetError) as cm:
            dev_status_mutation.done_item("gated-item")
        self.assertIn("has an unmet gate", cm.exception.message)

    def test_explicit_items_path_no_global_patching(self):
        with tempfile.TemporaryDirectory() as td:
            custom_items = Path(td) / "custom_items.json"
            req = dev_status_mutation.NewItemRequest(id="custom-item", summary="Test")
            res = dev_status_mutation.add_item(req, items_path=custom_items)
            self.assertTrue(custom_items.exists())
            self.assertEqual(res.slug, "custom-item")

    def test_mutation_transaction_batch_and_live_index(self):
        with dev_status_mutation.mutation_transaction() as tx:
            self.assertEqual(len(tx.index()), 0)
            tx.add_item(dev_status_mutation.NewItemRequest(id="batch-1", summary="First"))
            self.assertIn("batch-1", tx.index())
            tx.add_item(dev_status_mutation.NewItemRequest(id="batch-2", summary="Second"))
            self.assertIn("batch-2", tx.index())

        items = dev_status_storage.load_items(self.items_file)
        slugs = [i["id"] for i in items]
        self.assertIn("batch-1", slugs)
        self.assertIn("batch-2", slugs)

    def test_mutation_transaction_validation_failure_leaves_store_intact(self):
        self.write_items([make_item("existing-item")])
        rev_before = self.read_rev()
        with self.assertRaises(dev_status_mutation.DuplicateSlugError):
            with dev_status_mutation.mutation_transaction() as tx:
                tx.add_item(dev_status_mutation.NewItemRequest(id="existing-item", summary="Duplicate"))

        self.assertEqual(self.read_rev(), rev_before)
        items = dev_status_storage.load_items(self.items_file)
        self.assertEqual([i["id"] for i in items], ["existing-item"])


# ── Tier 2: Lock Contention Tests ─────────────────────────────────────────────


class LockContentionTestCase(MutationFixture):
    """Assert bounded synchronization and non-deadlocking lock behavior."""

    def test_service_blocks_on_contended_lock(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        started = threading.Event()
        finished = threading.Event()
        result_holder = []

        def worker():
            started.set()
            req = dev_status_mutation.NewItemRequest(id="item-blocked", summary="Blocked")
            res = dev_status_mutation.add_item(req)
            result_holder.append(res)
            finished.set()

        t = threading.Thread(target=worker)
        t.start()

        started.wait(timeout=2.0)
        # Worker must be blocked waiting on the lock
        self.assertFalse(finished.is_set())

        # Release the lock
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

        # Worker should finish now
        finished.wait(timeout=5.0)
        t.join(timeout=2.0)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(result_holder), 1)
        self.assertEqual(result_holder[0].slug, "item-blocked")

    def test_transaction_add_item_does_not_deadlock(self):
        # Nested operations in mutation_transaction must not re-acquire or deadlock
        with dev_status_mutation.mutation_transaction() as tx:
            tx.add_item(dev_status_mutation.NewItemRequest(id="item-no-deadlock-1", summary="1"))
            tx.add_item(dev_status_mutation.NewItemRequest(id="item-no-deadlock-2", summary="2"))


# ── Tier 3: Crash-Safety Characterization Tests ───────────────────────────────


class CrashSafetyTestCase(MutationFixture):
    """Assert verified crash-safety invariants: bump -> save -> journal."""

    def test_save_failure_leaves_rev_bumped_and_no_journal_gap(self):
        self.write_items([make_item("item-crash")])
        rev_before = self.read_rev()

        with patch.object(dev_status_storage, "save_items", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                dev_status_mutation.done_item("item-crash")

        # On-disk rev was already bumped before save_items
        rev_after_crash = self.read_rev()
        self.assertEqual(rev_after_crash, rev_before + 1)
        # Items file was NOT updated
        item_on_disk = self._item_by_id("item-crash")
        self.assertEqual(item_on_disk["status"], "open")
        # No journal event was written
        self.assertEqual(len(self.journal_lines()), 0)

        # Next successful mutation observes rev_after_crash + 1 with NO gap beyond 1
        res = dev_status_mutation.done_item("item-crash")
        self.assertEqual(res.rev, rev_after_crash + 1)
        self.assertEqual(self.read_rev(), rev_after_crash + 1)

    def test_journal_append_failure_is_non_fatal(self):
        self.write_items([make_item("item-jfail")])
        with patch.object(dev_status_storage, "append_journal_event", side_effect=OSError("journal disk full")):
            # Mutation still succeeds despite journal failure
            res = dev_status_mutation.done_item("item-jfail")
            self.assertEqual(res.status, "done")
        self.assertEqual(self._item_by_id("item-jfail")["status"], "done")

    def test_call_order_bump_then_save_then_journal(self):
        self.write_items([make_item("item-order")])
        calls = []

        real_bump = dev_status_storage.bump_rev
        real_save = dev_status_storage.save_items
        real_journal = dev_status_storage.append_journal_event

        def mock_bump(*args, **kwargs):
            calls.append("bump")
            return real_bump(*args, **kwargs)

        def mock_save(*args, **kwargs):
            calls.append("save")
            return real_save(*args, **kwargs)

        def mock_journal(*args, **kwargs):
            calls.append("journal")
            return real_journal(*args, **kwargs)

        with (
            patch.object(dev_status_storage, "bump_rev", side_effect=mock_bump),
            patch.object(dev_status_storage, "save_items", side_effect=mock_save),
            patch.object(dev_status_storage, "append_journal_event", side_effect=mock_journal),
        ):
            dev_status_mutation.done_item("item-order")

        self.assertEqual(calls, ["bump", "save", "journal"])


if __name__ == "__main__":
    unittest.main()
