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

Run standalone: python3 test_dev_status_mutation.py  (from test/)
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
from pytest_shim import pytest
import test_layouts  # noqa: E402
import agent_toolkit_paths  # noqa: E402
import dev_status  # noqa: E402  (path insert above)
import dev_status_mutation
import dev_status_read
import dev_status_storage
import worktree_provenance


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
        # A sandbox HOME: every store path resolves under it at each use.
        test_layouts.activate_sandbox_home(Path(self.tmpdir), self.addCleanup)
        self.data_dir = agent_toolkit_paths.path_for("work-items")
        self.items_file = self.data_dir / "items.json"
        self.pending_file = self.data_dir / "pending_items.json"
        self.meta_file = self.data_dir / "_meta.json"
        self.lock_file = self.data_dir / ".backlog.lock"
        self.journal_file = self.data_dir / "journal.jsonl"
        self.runs_file = self.data_dir / "runs.jsonl"
        self.machine_id_file = self.data_dir / "_machine_id"
        self.recap_cache_file = self.data_dir / "recap-cache.json"
        self.recap_regen_lock_file = self.data_dir / "recap-regen.lock"
        self.out_of_scope_dir = agent_toolkit_paths.path_for("out-of-scope")
        self.out_of_scope_index_file = self.out_of_scope_dir / "index.json"
        self.out_of_scope_lock_file = self.out_of_scope_dir / ".out-of-scope.lock"
        # Never let a test spawn a real process against fabricated data
        # (mirrors BacklogFixture's rationale in test_dev_status.py).
        self._popen_patch = patch("subprocess.Popen")
        self.mock_popen = self._popen_patch.start()
        self.mock_popen.return_value.communicate.return_value = ("", "")
        self.mock_popen.return_value.poll.return_value = 0
        self.mock_popen.return_value.returncode = 0

    def tearDown(self):
        self._popen_patch.stop()
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
    def setUp(self):
        super().setUp()
        # run's checkout resolution and HEAD probe are git calls; these
        # characterization tests mock subprocess.run wholesale instead.
        for name, value in (
            ("resolve_run_checkout", worktree_provenance.RunCheckout()),
            ("read_head", ("f" * 40, None)),
        ):
            stubbed = patch.object(worktree_provenance, name, return_value=value)
            stubbed.start()
            self.addCleanup(stubbed.stop)

    """Tier 1: today's compact output line and exit code, per command."""

    def _compact(self, out):
        lines = [line for line in out.splitlines() if line.startswith("[")]
        self.assertEqual(len(lines), 1, f"expected one compact line, got: {out!r}")
        return lines[0]

    _COMPACT_RE = re.compile(
        r"^\[(?P<cmd>[^\]]+)\] "
        r"slug=(?P<slug>\S+) "
        r"status=(?P<status>\S+) "
        r"rev=(?P<rev>\d+)"
        r"(?: ref=\"(?P<ref>[^\"]*)\")? "
        r'detail="(?P<detail>(?:[^"]|\\")*)\"\Z'
    )

    def _parse_compact(self, line):
        m = self._COMPACT_RE.fullmatch(line)
        self.assertIsNotNone(m, f"unparseable compact line: {line!r}")
        return m.groupdict()

    def _assert_compact(self, line, cmd, slug, status, rev=None, ref=None, detail=None):
        fields = self._parse_compact(line)
        self.assertEqual(fields["cmd"], cmd)
        self.assertEqual(fields["slug"], slug)
        self.assertEqual(fields["status"], status)
        if rev is not None:
            self.assertEqual(fields["rev"], str(rev))
        if ref is None:
            self.assertIsNone(fields["ref"])
        else:
            self.assertEqual(fields["ref"], ref)
        if detail is not None:
            self.assertEqual(fields["detail"], detail)

    def test_add_compact_line(self):
        out, _ = self.run_cmd(
            dev_status.cmd_add,
            _Args(json='{"id": "my-feature", "summary": "Test feature"}'),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self._assert_compact(line, "add", "my-feature", "open", rev, detail="Test feature")
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
        self._assert_compact(
            line,
            "update",
            "item-one",
            "in-progress",
            rev,
            detail="updated context: Summary of item-one",
        )

    def test_update_numeric_without_if_rev_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="1", patch='{"context": "x"}'),
        )
        self.assertEqual(code, 1)
        # Deliberate wording test: asserts this exact diagnostic message.
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
        # Deliberate wording test: asserts this exact diagnostic message.
        self.assertIn("[update] stale rev: --if-rev 99 given, current is 0", err)
        self.assertEqual(self.read_rev(), before)

    def test_update_immutable_field_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"id": "other-slug"}'),
        )
        self.assertEqual(code, 1)
        # Deliberate wording test: asserts this exact diagnostic message.
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
        self._assert_compact(
            line, "start", "item-one", "in-progress", rev, detail="Summary of item-one"
        )
        self.assertIn("claimed_by", self._item_by_id("item-one"))

    def test_reopen_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        out, _ = self.run_cmd(dev_status.cmd_reopen, _Args(id="item-one"))
        line = self._compact(out)
        rev = self.read_rev()
        self._assert_compact(
            line, "reopen", "item-one", "open", rev, detail="Summary of item-one"
        )

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
        self._assert_compact(
            line, "done", "item-one", "done", rev, detail="Summary of item-one"
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
        # Deliberate wording test: asserts this exact diagnostic message.
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
        self._assert_compact(
            line, "review", "item-one", "in-review", rev, detail="Summary of item-one"
        )

    def test_approve_compact_line_and_hash_drift(self):
        self.write_items([make_item("item-one", status="in-progress")])
        self.run_cmd(dev_status.cmd_review, _Args(id="item-one"))
        out, _ = self.run_cmd(dev_status.cmd_approve, _Args(id="item-one"))
        line = self._compact(out)
        rev = self.read_rev()
        self._assert_compact(
            line, "approve", "item-one", "done", rev, detail="Summary of item-one"
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
        # Deliberate wording test: asserts this exact diagnostic message.
        self.assertIn("content changed since it was submitted for review", err)

    def test_reject_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        self.run_cmd(dev_status.cmd_review, _Args(id="item-one"))
        out, _ = self.run_cmd(
            dev_status.cmd_reject, _Args(id="item-one", feedback="needs work")
        )
        line = self._compact(out)
        rev = self.read_rev()
        self._assert_compact(
            line, "reject", "item-one", "in-progress", rev, detail="Summary of item-one"
        )
        self.assertEqual(self._item_by_id("item-one")["review_feedback"], "needs work")

    def test_reject_empty_feedback_refuses(self):
        self.write_items([make_item("item-one", status="in-review")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_reject, _Args(id="item-one", feedback="   ")
        )
        self.assertEqual(code, 1)
        # Deliberate wording test: asserts this exact diagnostic message.
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
        self._assert_compact(
            line, "block", "item-one", "open", rev, detail="blocked by two-slug"
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
        # Deliberate wording test: asserts this exact diagnostic message.
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
        self._assert_compact(
            line, "unblock", "item-one", "open", rev, detail="unblocked from two-slug"
        )

    def test_unblock_missing_blocker_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_unblock, _Args(id="item-one", blocker="two-slug")
        )
        self.assertEqual(code, 1)
        # Deliberate wording test: asserts this exact diagnostic message.
        self.assertEqual(err.strip(), "[unblock] item-one is not blocked by two-slug")

    def test_gate_set_compact_line(self):
        self.write_items([make_item("item-one", status="in-progress")])
        out, _ = self.run_cmd(
            dev_status.cmd_gate_set,
            _Args(id="item-one", json='{"required": true, "criteria": ["passes tests"]}'),
        )
        line = self._compact(out)
        rev = self.read_rev()
        self._assert_compact(
            line,
            "gate-set",
            "item-one",
            "in-progress",
            rev,
            detail="gate set (1 criteria, required=true)",
        )

    def test_gate_set_empty_required_criteria_refuses(self):
        self.write_items([make_item("item-one")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_gate_set,
            _Args(id="item-one", json='{"required": true, "criteria": []}'),
        )
        self.assertEqual(code, 1)
        # Deliberate wording test: asserts this exact diagnostic message.
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
        self._assert_compact(
            line,
            "gate-pass",
            "item-one",
            "in-progress",
            rev,
            detail="gate passed via manual",
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
        self._assert_compact(
            line,
            "rename",
            "renamed-one",
            "open",
            rev,
            ref="item-one",
            detail="renamed from item-one",
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
        self._assert_compact(
            line, "remove", "item-one", "removed", rev, detail="Summary of item-one"
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
        self._assert_compact(
            line,
            "pending add",
            "wait-reply",
            "waiting_for_reply",
            rev,
            detail="Waiting on reply",
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
        self._assert_compact(
            line,
            "pending update",
            "wait-reply",
            "reply_received",
            rev,
            detail="Waiting on reply",
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
        # Deliberate wording test: asserts this exact diagnostic message.
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



class NextStepsTypeTestCase(MutationFixture):
    """next_steps has one normalization on add/update and rename walks lists.

    Regression tests for the split str/list handling: add used to
    ``str()``-repr a list into a blob, update used to ``cast`` it verbatim,
    and rename's backlog branch used to ``in``-test the whole field so a
    list-valued next_steps was silently never rewritten (the adjacent
    pending branch already handled it per element).
    """

    def test_add_with_list_stores_a_list(self):
        self.run_cmd(
            dev_status.cmd_add,
            _Args(
                json='{"id": "my-feature", "summary": "S", '
                '"next_steps": ["step a", "step b"]}'
            ),
        )
        self.assertEqual(
            self._item_by_id("my-feature")["next_steps"], ["step a", "step b"]
        )

    def test_add_with_str_keeps_a_str(self):
        self.run_cmd(
            dev_status.cmd_add,
            _Args(
                json='{"id": "my-feature", "summary": "S", '
                '"next_steps": "do the thing"}'
            ),
        )
        self.assertEqual(
            self._item_by_id("my-feature")["next_steps"], "do the thing"
        )

    def test_add_with_empty_list_stores_empty_list(self):
        self.run_cmd(
            dev_status.cmd_add,
            _Args(json='{"id": "my-feature", "summary": "S", "next_steps": []}'),
        )
        self.assertEqual(self._item_by_id("my-feature")["next_steps"], [])

    def test_add_list_with_non_str_element_refuses(self):
        err, code = self.run_cmd_exits(
            dev_status.cmd_add,
            _Args(
                json='{"id": "my-feature", "summary": "S", '
                '"next_steps": ["a", 5]}'
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("next_steps", err)

    def test_add_bare_non_str_non_list_value_refuses(self):
        err, code = self.run_cmd_exits(
            dev_status.cmd_add,
            _Args(
                json='{"id": "my-feature", "summary": "S", "next_steps": 5}'
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("next_steps", err)

    def test_update_with_list_round_trips(self):
        self.write_items([make_item("item-one", next_steps="was a string")])
        self.run_cmd(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"next_steps": ["step a", "step b"]}'),
        )
        self.assertEqual(
            self._item_by_id("item-one")["next_steps"], ["step a", "step b"]
        )

    def test_update_with_str_keeps_str(self):
        self.write_items([make_item("item-one", next_steps=["old", "steps"])])
        self.run_cmd(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"next_steps": "now a string"}'),
        )
        self.assertEqual(self._item_by_id("item-one")["next_steps"], "now a string")

    def test_update_absent_next_steps_leaves_field_unchanged(self):
        self.write_items([make_item("item-one", next_steps=["keep", "me"])])
        self.run_cmd(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"context": "touched"}'),
        )
        self.assertEqual(self._item_by_id("item-one")["next_steps"], ["keep", "me"])

    def test_update_with_null_still_refused(self):
        self.write_items([make_item("item-one", next_steps="x")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"next_steps": null}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot be null", err)

    def test_update_list_with_non_str_element_refuses(self):
        self.write_items([make_item("item-one", next_steps="x")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="item-one", patch='{"next_steps": ["a", 5]}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("next_steps", err)

    def test_rename_rewrites_slug_inside_list_next_steps(self):
        self.write_items(
            [
                make_item(
                    "item-one",
                    next_steps=["work on item-one first", "unrelated"],
                ),
                make_item("two-slug"),
            ]
        )
        self.run_cmd(
            dev_status.cmd_rename,
            _Args(old_slug="item-one", new_slug="renamed-one"),
        )
        self.assertEqual(
            self._item_by_id("renamed-one")["next_steps"],
            ["work on renamed-one first", "unrelated"],
        )

    def test_rename_still_rewrites_str_valued_next_steps(self):
        self.write_items(
            [
                make_item("item-one", next_steps="see item-one later"),
                make_item("two-slug"),
            ]
        )
        self.run_cmd(
            dev_status.cmd_rename,
            _Args(old_slug="item-one", new_slug="renamed-one"),
        )
        self.assertEqual(
            self._item_by_id("renamed-one")["next_steps"], "see renamed-one later"
        )


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

    def test_update_item_sets_and_clears_integration_branch(self):
        self.write_items([make_item("item-1")])
        with patch.object(worktree_provenance, "branch_name_problem", return_value=None):
            res = dev_status_mutation.update_item(
                "item-1",
                dev_status_mutation.ItemUpdateRequest(integration_branch="release-1"),
            )
        self.assertEqual(res.item["integration_branch"], "release-1")
        self.assertEqual(self._item_by_id("item-1")["integration_branch"], "release-1")
        res = dev_status_mutation.update_item(
            "item-1", dev_status_mutation.ItemUpdateRequest(integration_branch=None)
        )
        self.assertNotIn("integration_branch", self._item_by_id("item-1"))
        self.assertIn("integration_branch", res.detail)

    def test_update_item_rejects_invalid_integration_branch_without_write(self):
        self.write_items([make_item("item-1")])
        rev_before = self.read_rev()
        with patch.object(
            worktree_provenance, "branch_name_problem", return_value="bad name"
        ):
            with self.assertRaises(dev_status_mutation.ValidationError) as cm:
                dev_status_mutation.update_item(
                    "item-1",
                    dev_status_mutation.ItemUpdateRequest(integration_branch="a..b"),
                )
        self.assertIn("integration_branch", str(cm.exception.message))
        self.assertEqual(self.read_rev(), rev_before)
        self.assertNotIn("integration_branch", self._item_by_id("item-1"))

    def test_add_item_stores_no_worker_fields(self):
        res = dev_status_mutation.add_item(
            dev_status_mutation.NewItemRequest(
                id="item-nw",
                summary="No worker",
                no_worker=True,
                no_worker_reason="cross-repo",
            )
        )
        self.assertIs(res.item["no_worker"], True)
        self.assertEqual(res.item["no_worker_reason"], "cross-repo")

    def test_add_item_rejects_non_bool_no_worker(self):
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation.add_item(
                dev_status_mutation.NewItemRequest(
                    id="item-bad", summary="x", no_worker="yes"
                )
            )

    def test_update_item_sets_and_clears_no_worker(self):
        self.write_items([make_item("item-1")])
        res = dev_status_mutation.update_item(
            "item-1",
            dev_status_mutation.ItemUpdateRequest(no_worker=True, no_worker_reason="r"),
        )
        self.assertIs(res.item["no_worker"], True)
        self.assertEqual(res.item["no_worker_reason"], "r")
        # Clearing the flag also clears the reason.
        res = dev_status_mutation.update_item(
            "item-1", dev_status_mutation.ItemUpdateRequest(no_worker=None)
        )
        self.assertNotIn("no_worker", self._item_by_id("item-1"))
        self.assertNotIn("no_worker_reason", self._item_by_id("item-1"))

    def test_update_item_rejects_non_bool_no_worker(self):
        self.write_items([make_item("item-1")])
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation.update_item(
                "item-1", dev_status_mutation.ItemUpdateRequest(no_worker="yes")
            )

    def test_mutation_transaction_add_item_stores_no_worker(self):
        with dev_status_mutation.mutation_transaction() as tx:
            tx.add_item(
                dev_status_mutation.NewItemRequest(
                    id="batch-nw",
                    summary="Batch",
                    no_worker=True,
                    no_worker_reason="r",
                )
            )
        self.assertIs(self._item_by_id("batch-nw")["no_worker"], True)

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

    def test_done_refuses_unmerged_worktree_and_journals_no_notice(self):
        """The retired post-success notice must not fire for a refused done."""
        self.write_items(
            [
                make_item(
                    "item-guard",
                    status="in-progress",
                    related_files=[{"path": "/repo/f.py", "note": ""}],
                )
            ]
        )

        with patch.object(
            worktree_provenance,
            "inspect_item_worktrees",
            return_value=[
                worktree_provenance.WorktreeInspection(
                    target="/repo-wt",
                    source="worktree",
                    dirty=True,
                    dirty_sample=" M f.py",
                )
            ],
        ):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError):
                dev_status_mutation.done_item("item-guard")

        self.assertEqual(self.journal_lines(), [])

    def test_approve_refuses_unmerged_worktree_and_journals_no_notice(self):
        self.write_items([make_item("item-guard", status="in-progress")])
        dev_status_mutation.review_item("item-guard")

        with patch.object(
            worktree_provenance,
            "inspect_item_worktrees",
            return_value=[
                worktree_provenance.WorktreeInspection(
                    target="/repo-wt",
                    source="worktree",
                    dirty=False,
                    head_ancestor_of_default=False,
                    default_ref="refs/heads/main",
                )
            ],
        ):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError):
                dev_status_mutation.approve_item("item-guard")

        self.assertEqual(
            [e["cmd"] for e in self.journal_lines()], ["review"]
        )

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


# ── Lifecycle-transition guard: update back-door closed + reopen_item ────────


def _recent_claim(harness="other", pid=999999):
    """A foreign claim stamped now (unanchored pid==owner_pid -> TTL-governed)."""
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "harness": harness,
        "machine_id": None,  # filled per-test with the current machine id
        "pid": pid,
        "owner_pid": pid,
        "claimed_at": now_iso,
        "last_active": now_iso,
    }


class UpdateStatusBackdoorTestCase(MutationFixture):
    """`update` must never move lifecycle status or claims (the audited hole).

    Regression: a sandboxed run moved an in-progress item straight to done
    with `update '{"status": "done"}'`, bypassing gate, review-cycle,
    claim-collision, and worktree checks entirely.
    """

    def test_cli_update_status_patch_refused(self):
        self.write_items([make_item("task-a", status="in-progress")])
        rev_before = self.read_rev()
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="task-a", patch='{"status": "done"}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("status", err)
        self.assertIn("reopen", err)
        self.assertIn("start", err)
        self.assertIn("review", err)
        self.assertEqual(self._item_by_id("task-a")["status"], "in-progress")
        self.assertEqual(self.read_rev(), rev_before)

    def test_cli_update_status_open_patch_refused(self):
        self.write_items([make_item("task-a", status="in-progress")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="task-a", patch='{"status": "open"}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("reopen", err)
        self.assertEqual(self._item_by_id("task-a")["status"], "in-progress")

    def test_cli_update_claimed_by_patch_refused(self):
        self.write_items([make_item("task-a", status="in-progress")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update,
            _Args(id="task-a", patch='{"claimed_by": {"harness": "forged"}}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("claimed_by", err)
        self.assertIn("no manual patch equivalent", err)
        self.assertNotIn("forged", json.dumps(self._item_by_id("task-a")))

    def test_service_update_request_rejects_status(self):
        # A Python caller is now a construction-time programmer error, not a
        # silent write: the typed request has no status/claimed_by fields.
        with self.assertRaises(TypeError):
            dev_status_mutation.ItemUpdateRequest(status="done")
        with self.assertRaises(TypeError):
            dev_status_mutation.ItemUpdateRequest(claimed_by={"harness": "x"})


class ReopenItemTestCase(MutationFixture):
    """`reopen_item`: the only sanctioned ->open edge."""

    def _seed_claim(self, item, **over):
        claim = _recent_claim()
        claim["machine_id"] = dev_status.machine_id()
        claim.update(over)
        item["claimed_by"] = claim
        return item

    def test_reopen_from_in_progress_releases_claim(self):
        # No claim seeded: reopen's core transition + journal shape.
        item = make_item("task-a", status="in-progress")
        self.write_items([item])
        res = dev_status_mutation.reopen_item("task-a")
        self.assertEqual(res.cmd, "reopen")
        self.assertEqual(res.status, "open")
        stored = self._item_by_id("task-a")
        self.assertEqual(stored["status"], "open")
        self.assertNotIn("claimed_by", stored)
        journal = self.journal_lines()
        self.assertEqual(journal[-1]["cmd"], "reopen")
        self.assertEqual(journal[-1]["from_status"], "in-progress")
        self.assertEqual(journal[-1]["to_status"], "open")

    def test_reopen_preserves_review_feedback_and_hash(self):
        # reject preserves review_feedback; approve retains the hash as an
        # audit trail (test_40b). reopen must not destroy either.
        item = make_item(
            "task-a",
            status="in-progress",
            review_feedback="fix the thing",
            review_content_hash="abc123",
        )
        self.write_items([item])
        dev_status_mutation.reopen_item("task-a")
        stored = self._item_by_id("task-a")
        self.assertEqual(stored["review_feedback"], "fix the thing")
        self.assertEqual(stored["review_content_hash"], "abc123")

    def test_reopen_from_done_clears_completed_at_keeps_hash(self):
        item = make_item(
            "task-a", status="done", review_content_hash="audit-hash"
        )
        item["completed_at"] = "2026-01-01"
        self.write_items([item])
        res = dev_status_mutation.reopen_item("task-a")
        self.assertEqual(res.status, "open")
        stored = self._item_by_id("task-a")
        self.assertNotIn("completed_at", stored)
        self.assertEqual(stored["review_content_hash"], "audit-hash")

    def test_reopen_repairs_malformed_completed_at_on_in_progress(self):
        # _apply_status_transition pops completed_at only when the old status
        # is done; reopen must pop it unconditionally to repair stragglers.
        item = make_item("task-a", status="in-progress")
        item["completed_at"] = "2026-01-01"
        self.write_items([item])
        dev_status_mutation.reopen_item("task-a")
        self.assertNotIn("completed_at", self._item_by_id("task-a"))

    def test_reopen_from_open_refuses_without_write(self):
        self.write_items([make_item("task-a", status="open")])
        rev_before = self.read_rev()
        with self.assertRaises(dev_status_mutation.InvalidItemStateError):
            dev_status_mutation.reopen_item("task-a")
        self.assertEqual(self.read_rev(), rev_before)
        self.assertEqual(self.journal_lines(), [])

    def test_reopen_from_in_review_points_to_reject(self):
        self.write_items([make_item("task-a", status="in-review")])
        with self.assertRaises(dev_status_mutation.InvalidItemStateError) as ctx:
            dev_status_mutation.reopen_item("task-a")
        self.assertIn("reject", str(ctx.exception))

    def test_foreign_live_claim_requires_force(self):
        item = self._seed_claim(make_item("task-a", status="in-progress"))
        self.write_items([item])
        with self.assertRaises(dev_status_mutation.ClaimCollisionError) as ctx:
            dev_status_mutation.reopen_item("task-a")
        msg = str(ctx.exception)
        self.assertIn("[reopen]", msg)
        self.assertIn("release", msg)
        # A bare [start]->[reopen] label swap would keep this wrong wording.
        self.assertNotIn("take over", msg)
        self.assertEqual(self._item_by_id("task-a")["status"], "in-progress")

    def test_force_release_journals_diagnostic(self):
        item = self._seed_claim(make_item("task-a", status="in-progress"))
        self.write_items([item])
        res = dev_status_mutation.reopen_item("task-a", force=True)
        self.assertEqual(res.status, "open")
        theft = [
            j for j in self.journal_lines() if j["cmd"] == "claim-theft"
        ]
        self.assertEqual(len(theft), 1)
        self.assertIn("released", theft[0]["detail"])

    def test_expired_claim_releases_without_force(self):
        item = self._seed_claim(
            make_item("task-a", status="in-progress"),
            last_active="2020-01-01T00:00:00Z",
        )
        self.write_items([item])
        res = dev_status_mutation.reopen_item("task-a")
        self.assertEqual(res.status, "open")
        self.assertTrue(
            any("Releasing" in n for n in res.notices), res.notices
        )

    def test_done_with_foreign_live_claim_still_guarded(self):
        # Release guard keys on claim presence, not status: a malformed done
        # item must not silently discard a live claim.
        item = self._seed_claim(make_item("task-a", status="done"))
        self.write_items([item])
        with self.assertRaises(dev_status_mutation.ClaimCollisionError):
            dev_status_mutation.reopen_item("task-a")
        res = dev_status_mutation.reopen_item("task-a", force=True)
        self.assertEqual(res.status, "open")

    def test_numeric_id_requires_if_rev(self):
        self.write_items([make_item("task-a", status="in-progress")])
        with self.assertRaises(dev_status_mutation.RevisionConflictError):
            dev_status_mutation.reopen_item("1")

    def test_start_collision_wording_unchanged(self):
        # Regression guard for parameterizing _check_claim_collision: start's
        # refusal strings must keep the exact [start]/take-over vocabulary.
        item = self._seed_claim(make_item("task-a", status="in-progress"))
        self.write_items([item])
        with self.assertRaises(dev_status_mutation.ClaimCollisionError) as ctx:
            dev_status_mutation.start_item("task-a")
        msg = str(ctx.exception)
        self.assertIn("[start]", msg)
        self.assertIn("take over", msg)

    def test_reopen_invalidates_passed_gate(self):
        gate = {
            "required": True,
            "criteria": ["c1"],
            "passed_at": "2026-01-01",
            "set_at": "2025-12-31T00:00:00+00:00",
            "passed_via": "manual",
            "coverage": {"1": {"kind": "manual", "note": "ok"}},
        }
        item = make_item("task-a", status="done", gate=gate)
        item["completed_at"] = "2026-01-01"
        self.write_items([item])
        dev_status_mutation.reopen_item("task-a")
        stored = self._item_by_id("task-a")
        self.assertIsNone(stored["gate"]["passed_at"])
        self.assertNotIn("passed_via", stored["gate"])
        self.assertNotIn("coverage", stored["gate"])
        self.assertEqual(stored["gate"]["criteria"], ["c1"])
        self.assertTrue(stored["gate"]["required"])

    def test_reopen_rearms_stale_run_evidence(self):
        # After reopen, a run recorded before the reopen must no longer pass
        # gate-pass (set_at refreshed -> started_at < set_at is stale).
        gate = {
            "required": True,
            "criteria": ["c1"],
            "passed_at": "2026-01-01",
            "set_at": "2025-12-31T00:00:00+00:00",
            "coverage": {"1": {"kind": "run", "run_id": "r-old"}},
        }
        item = make_item("task-a", status="done", gate=gate)
        item["completed_at"] = "2026-01-01"
        self.write_items([item])
        self.runs_file.write_text(
            json.dumps(
                {
                    "run_id": "r-old",
                    "item": "task-a",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "timed_out": False,
                    "started_at": "2026-01-01T12:00:00+00:00",
                }
            )
            + "\n"
        )
        dev_status_mutation.reopen_item("task-a")
        with self.assertRaises(dev_status_mutation.ValidationError) as ctx:
            dev_status_mutation.pass_gate(
                "task-a", dev_status_mutation.GatePassRequest(coverage={"1": "run:r-old"})
            )
        self.assertIn("stale", str(ctx.exception))


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

class CommittedWorkGuardTestCase(MutationFixture):
    """review/approve/done refuse when the item's identifiable local work is
    uncommitted or unmerged. The inspector is mocked at the subprocess
    boundary here — this class owns the policy, validation precedence,
    atomicity, and lock-order contracts; real-git inspection behavior lives
    in test_worktree_provenance.py."""

    DIRTY = [
        worktree_provenance.WorktreeInspection(
            target="/repo-wt",
            source="worktree",
            dirty=True,
            dirty_sample=" M f.py",
        )
    ]
    UNMERGED = [
        worktree_provenance.WorktreeInspection(
            target="/repo-wt",
            source="worktree",
            dirty=False,
            head_ancestor_of_default=False,
            default_ref="refs/heads/main",
        )
    ]
    CLEAN = [
        worktree_provenance.WorktreeInspection(
            target="/repo-wt",
            source="worktree",
            dirty=False,
            head_ancestor_of_default=True,
            default_ref="refs/heads/main",
        )
    ]
    PROBLEM = [
        worktree_provenance.WorktreeInspection(
            target="/repo-wt",
            source="worktree",
            problem="git status failed: index.lock exists",
        )
    ]

    def _guard(self, inspections):
        return patch.object(
            worktree_provenance,
            "inspect_item_worktrees",
            return_value=inspections,
        )

    def _committed_item(self, slug="item-guard"):
        self.write_items(
            [
                make_item(
                    slug,
                    status="in-progress",
                    related_files=[{"path": "/repo/f.py", "note": ""}],
                )
            ]
        )

    # ── refusal behavior and atomicity ─────────────────────────────────

    def test_done_refusal_names_target_and_escape_hatches(self):
        self._committed_item()
        with self._guard(self.DIRTY):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.done_item("item-guard")
        msg = str(cm.exception.message)
        self.assertIn("/repo-wt", msg)
        self.assertIn("uncommitted", msg)
        self.assertIn(" M f.py", msg)
        self.assertIn("delete", msg.lower())

    def test_review_refusal_names_unmerged_branch_and_target(self):
        self._committed_item()
        with self._guard(self.UNMERGED):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.review_item("item-guard")
        msg = str(cm.exception.message)
        self.assertIn("refs/heads/main", msg)
        self.assertIn("merge", msg.lower())

    def test_approve_refusal_catches_re_dirtied_worktree(self):
        """Re-dirtying between review and approve is caught on approve."""
        self._committed_item()
        with self._guard(self.CLEAN):
            dev_status_mutation.review_item("item-guard")
        with self._guard(self.DIRTY):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError):
                dev_status_mutation.approve_item("item-guard")
        stored = self._item_by_id("item-guard")
        self.assertEqual(stored["status"], "in-review")
        self.assertIn("review_content_hash", stored)

    def test_inspection_problem_refuses_fail_closed(self):
        self._committed_item()
        with self._guard(self.PROBLEM):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.done_item("item-guard")
        self.assertIn("git status failed", str(cm.exception.message))

    @pytest.mark.regression(
        "committed-work-prepass-read-fails-closed",
        "AssertionError: InvalidItemStateError not raised",
    )
    def test_transient_prepass_store_read_refuses_transitions(self) -> None:
        commands = {
            "done": dev_status_mutation.done_item,
            "review": dev_status_mutation.review_item,
            "approve": dev_status_mutation.approve_item,
        }
        for loader_name in ("load_items", "load_pending"):
            for command, transition in commands.items():
                with self.subTest(loader=loader_name, command=command):
                    self._committed_item()
                    if command == "approve":
                        with self._guard(self.CLEAN):
                            dev_status_mutation.review_item("item-guard")
                    items_before = self.read_items()
                    rev_before = self.read_rev()
                    journal_before = self.journal_lines()
                    real_loader = getattr(dev_status_storage, loader_name)
                    calls = 0

                    def fail_once(path: Path) -> object:
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            raise OSError("transient prepass read failure")
                        return real_loader(path)

                    inspector = MagicMock(return_value=self.CLEAN)
                    with (
                        patch.object(
                            dev_status_storage, loader_name, side_effect=fail_once
                        ),
                        patch.object(
                            worktree_provenance,
                            "inspect_item_worktrees",
                            inspector,
                        ),
                        self.assertRaises(
                            dev_status_mutation.InvalidItemStateError
                        ) as cm,
                    ):
                        transition("item-guard")
                    self.assertIn("committed-work", str(cm.exception.message))
                    self.assertEqual(calls, 2)
                    inspector.assert_not_called()
                    self.assertEqual(self.read_items(), items_before)
                    self.assertEqual(self.read_rev(), rev_before)
                    self.assertEqual(self.journal_lines(), journal_before)

    def test_refusal_leaves_store_untouched(self):
        self._committed_item()
        rev_before = self.read_rev()
        with self._guard(self.DIRTY):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError):
                dev_status_mutation.done_item("item-guard")
        self.assertEqual(self.read_rev(), rev_before)
        self.assertEqual(self._item_by_id("item-guard")["status"], "in-progress")
        self.assertEqual(self.journal_lines(), [])

    # ── allow paths ────────────────────────────────────────────────────

    def test_clean_merged_work_completes(self):
        self._committed_item()
        with self._guard(self.CLEAN):
            result = dev_status_mutation.done_item("item-guard")
        self.assertEqual(result.status, "done")

    def test_no_attributable_work_completes(self):
        self._committed_item()
        with self._guard([]):
            result = dev_status_mutation.done_item("item-guard")
        self.assertEqual(result.status, "done")

    def test_successful_done_journals_no_unmerged_event(self):
        self._committed_item()
        with self._guard(self.CLEAN):
            dev_status_mutation.done_item("item-guard")
        self.assertNotIn(
            "unmerged-completion", [e["cmd"] for e in self.journal_lines()]
        )

    def test_repeat_review_rechecks_work_each_time(self):
        self._committed_item()
        with self._guard(self.CLEAN):
            dev_status_mutation.review_item("item-guard")
        with self._guard(self.DIRTY):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError):
                dev_status_mutation.review_item("item-guard")
        self.assertEqual(self._item_by_id("item-guard")["status"], "in-review")

    # ── declared integration branch ────────────────────────────────────

    def _integration_item(self, slug="item-guard", branch="release-1"):
        item = make_item(
            slug,
            status="in-progress",
            related_files=[{"path": "/repo/f.py", "note": ""}],
        )
        item["integration_branch"] = branch
        self.write_items([item])

    def test_inspector_receives_declared_integration_branch(self):
        self._integration_item()
        inspector = MagicMock(return_value=[])
        with patch.object(worktree_provenance, "inspect_item_worktrees", inspector):
            dev_status_mutation.review_item("item-guard")
        self.assertEqual(inspector.call_args.kwargs["target_branch"], "release-1")

    def test_inspector_receives_no_target_when_undeclared(self):
        self._committed_item()
        inspector = MagicMock(return_value=[])
        with patch.object(worktree_provenance, "inspect_item_worktrees", inspector):
            dev_status_mutation.review_item("item-guard")
        self.assertIsNone(inspector.call_args.kwargs["target_branch"])

    def test_refusal_names_declared_branch_not_default(self):
        self._integration_item()
        unmerged = [
            worktree_provenance.WorktreeInspection(
                target="/repo-wt",
                source="worktree",
                head_ancestor_of_default=False,
                default_ref="refs/heads/release-1",
            )
        ]
        with self._guard(unmerged):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.review_item("item-guard")
        msg = str(cm.exception.message)
        self.assertIn("merge the branch into release-1", msg)
        self.assertNotIn("local default branch", msg)

    def _racing_inspector(self, new_branch):
        """An inspector that rewrites the item's integration_branch while
        the unlocked prepass runs — a concurrent update landing between
        the prepass and the lock."""

        def inspect(**kwargs):
            items = self.read_items()
            for it in items:
                if new_branch is None:
                    it.pop("integration_branch", None)
                else:
                    it["integration_branch"] = new_branch
            self.write_items(items)
            return []

        return patch.object(
            worktree_provenance, "inspect_item_worktrees", side_effect=inspect
        )

    def test_integration_branch_changed_during_review_prepass_refuses(self):
        self._integration_item()
        with self._racing_inspector(None):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.review_item("item-guard")
        self.assertIn("integration_branch changed", str(cm.exception.message))
        self.assertEqual(self._item_by_id("item-guard")["status"], "in-progress")

    def test_integration_branch_changed_during_approve_prepass_refuses(self):
        self._integration_item()
        with self._guard([]):
            dev_status_mutation.review_item("item-guard")
        with self._racing_inspector("release-2"):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.approve_item("item-guard")
        self.assertIn("integration_branch changed", str(cm.exception.message))
        self.assertEqual(self._item_by_id("item-guard")["status"], "in-review")

    def test_integration_branch_changed_during_done_prepass_refuses(self):
        self._committed_item()
        with self._racing_inspector("release-1"):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.done_item("item-guard")
        self.assertIn("integration_branch changed", str(cm.exception.message))
        self.assertEqual(self._item_by_id("item-guard")["status"], "in-progress")

    # ── validation precedence: cheap checks before git I/O ─────────────

    def test_status_validation_precedes_inspection(self):
        self.write_items([make_item("item-order", status="in-progress")])
        dev_status_mutation.review_item("item-order")
        inspector = MagicMock(return_value=[])
        with patch.object(
            worktree_provenance, "inspect_item_worktrees", inspector
        ):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as cm:
                dev_status_mutation.done_item("item-order")
        self.assertIn("in-review", str(cm.exception.message))
        inspector.assert_not_called()

    def test_gate_validation_precedes_inspection(self):
        self._committed_item("item-gate")
        set_req = dev_status_mutation.GateSetRequest(
            required=True, criteria=("evidence",)
        )
        dev_status_mutation.set_gate("item-gate", set_req)
        inspector = MagicMock(return_value=[])
        with patch.object(
            worktree_provenance, "inspect_item_worktrees", inspector
        ):
            with self.assertRaises(dev_status_mutation.GateUnmetError):
                dev_status_mutation.done_item("item-gate")
        inspector.assert_not_called()

    def test_missing_item_refuses_without_inspection(self):
        inspector = MagicMock(return_value=[])
        with patch.object(
            worktree_provenance, "inspect_item_worktrees", inspector
        ):
            with self.assertRaises(dev_status_mutation.NotFoundError):
                dev_status_mutation.done_item("missing-slug")
        inspector.assert_not_called()

    def test_inspection_runs_before_the_storage_lock(self):
        """Git I/O must never run under the storage lock: the pre-pass
        inspects before lock acquisition, the locked section only consumes
        the verdict."""
        self._committed_item()
        events = []

        @contextmanager
        def spy_lock(*args, **kwargs):
            events.append("lock-enter")
            yield
            events.append("lock-exit")

        def spy_inspect(**kwargs):
            events.append("inspect")
            return []

        with (
            patch.object(dev_status_storage, "backlog_lock", spy_lock),
            patch.object(
                worktree_provenance,
                "inspect_item_worktrees",
                side_effect=spy_inspect,
            ),
        ):
            dev_status_mutation.done_item("item-guard")

        self.assertLess(events.index("inspect"), events.index("lock-enter"))


def make_obe_item(slug, *, superseded_by=None, at="2026-01-02", **kwargs):
    """Build one stored-shape item closed as OBE."""
    item = make_item(slug, status="obe", **kwargs)
    item["obe"] = {"reason": f"{slug} is moot", "at": at, "by": "claude"}
    if superseded_by is not None:
        item["obe"]["superseded_by"] = superseded_by
    return item


class ObeItemTestCase(MutationFixture):
    """`obe_item`: close an open item whose need went away."""

    def test_closes_open_item_with_record_and_journal(self):
        self.write_items([make_item("task-a")])
        res = dev_status_mutation.obe_item("task-a", reason="  shipped upstream  ")
        self.assertEqual((res.cmd, res.status), ("obe", "obe"))
        stored = self._item_by_id("task-a")
        self.assertEqual(stored["status"], "obe")
        self.assertEqual(stored["obe"]["reason"], "shipped upstream")
        self.assertEqual(stored["obe"]["at"], dev_status_mutation.today())
        self.assertNotIn("superseded_by", stored["obe"])
        self.assertNotIn("completed_at", stored)
        self.assertEqual(self.read_rev(), 1)
        entry = self.journal_lines()[-1]
        self.assertEqual(
            {k: entry[k] for k in ("cmd", "slug", "from_status", "to_status", "reason")},
            {
                "cmd": "obe",
                "slug": "task-a",
                "from_status": "open",
                "to_status": "obe",
                "reason": "shipped upstream",
            },
        )
        self.assertEqual(entry["summary"], "Summary of task-a")

    def test_stale_completed_at_is_not_carried_into_obe(self):
        # A malformed open record with a leftover done stamp must not produce
        # an OBE record that validate rejects (obe and completed_at together).
        item = make_item("task-a")
        item["completed_at"] = "2026-01-01"
        self.write_items([item])
        dev_status_mutation.obe_item("task-a", reason="moot")
        stored = self._item_by_id("task-a")
        self.assertNotIn("completed_at", stored)
        self.assertEqual(dev_status._validate_item_lifecycle([stored]), [])

    def test_superseded_by_is_stored_stripped(self):
        self.write_items([make_item("task-a")])
        dev_status_mutation.obe_item(
            "task-a", reason="done elsewhere", superseded_by=" other-item "
        )
        self.assertEqual(
            self._item_by_id("task-a")["obe"]["superseded_by"], "other-item"
        )

    def test_blank_reason_or_superseded_by_refused_without_write(self):
        self.write_items([make_item("task-a")])
        for kwargs in ({"reason": "  "}, {"reason": "ok", "superseded_by": " "}):
            with self.subTest(**kwargs):
                with self.assertRaises(dev_status_mutation.ValidationError):
                    dev_status_mutation.obe_item("task-a", **kwargs)
        self.assertEqual(self._item_by_id("task-a")["status"], "open")
        self.assertEqual(self.read_rev(), 0)

    def test_non_open_statuses_refused_with_pointer(self):
        cases = {
            "in-progress": "reopen",
            "in-review": "reject",
            "done": "reopen",
            "obe": "already",
            "bogus": "bogus",
        }
        for status, pointer in cases.items():
            with self.subTest(status=status):
                item = (
                    make_obe_item("task-a")
                    if status == "obe"
                    else make_item("task-a", status=status)
                )
                self.write_items([item])
                with self.assertRaises(
                    dev_status_mutation.InvalidItemStateError
                ) as ctx:
                    dev_status_mutation.obe_item("task-a", reason="moot")
                self.assertIn(pointer, str(ctx.exception))
                self.assertEqual(self._item_by_id("task-a")["status"], status)

    def test_open_dependents_refuse_until_each_is_resolved(self):
        self.write_items(
            [
                make_item("task-a"),
                make_item("dep-open", blocked_by=["task-a"]),
                make_item("dep-wip", status="in-progress", blocked_by=["task-a"]),
                make_item("dep-done", status="done", blocked_by=["task-a"]),
            ]
        )
        with self.assertRaises(dev_status_mutation.InvalidItemStateError) as ctx:
            dev_status_mutation.obe_item("task-a", reason="moot")
        msg = str(ctx.exception)
        self.assertIn("dep-open", msg)
        self.assertIn("dep-wip", msg)
        self.assertNotIn("dep-done", msg)
        self.assertEqual(self._item_by_id("task-a")["status"], "open")

        dev_status_mutation.unblock_item("dep-open", "task-a")
        dev_status_mutation.unblock_item("dep-wip", "task-a")
        res = dev_status_mutation.obe_item("task-a", reason="moot")
        self.assertEqual(res.status, "obe")

    def test_obe_blocker_counts_as_resolved(self):
        # The one way an open item can still point at an OBE blocker: a done
        # dependent (not refused by obe) that is later reopened. Its blocker's
        # need is gone, so it is READY — on the dashboard and the read facade.
        self.write_items(
            [
                make_item("task-a"),
                make_item("dep-done", status="done", blocked_by=["task-a"]),
            ]
        )
        dev_status_mutation.obe_item("task-a", reason="moot")
        dev_status_mutation.reopen_item("dep-done")
        items = self.read_items()
        index = dev_status_mutation.build_index(items)
        self.assertEqual(
            dev_status_mutation.effective_blockers(index["dep-done"], index), []
        )
        ready = dev_status_read.BacklogSnapshot(tuple(items)).ready_items()
        self.assertEqual([i["id"] for i in ready], ["dep-done"])

    def test_new_dependencies_on_obe_item_refused(self):
        self.write_items([make_obe_item("task-a"), make_item("task-b")])
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation.block_item("task-b", "task-a")
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation.add_item(
                dev_status_mutation.NewItemRequest(
                    id="task-c", summary="c", blocked_by=("task-a",)
                )
            )
        with self.assertRaises(dev_status_mutation.ValidationError):
            with dev_status_mutation.mutation_transaction() as tx:
                tx.add_item(
                    dev_status_mutation.NewItemRequest(
                        id="task-d", summary="d", blocked_by=("task-a",)
                    )
                )
        self.assertEqual(
            sorted(i["id"] for i in self.read_items()), ["task-a", "task-b"]
        )
        self.assertEqual(self._item_by_id("task-b")["blocked_by"], [])

    def test_live_foreign_claim_requires_force(self):
        claim = _recent_claim()
        claim["machine_id"] = dev_status.machine_id()
        self.write_items([make_item("task-a", claimed_by=claim)])
        with self.assertRaises(dev_status_mutation.ClaimCollisionError) as ctx:
            dev_status_mutation.obe_item("task-a", reason="moot")
        self.assertIn("release", str(ctx.exception))
        self.assertEqual(self._item_by_id("task-a")["status"], "open")
        dev_status_mutation.obe_item("task-a", reason="moot", force=True)
        stored = self._item_by_id("task-a")
        self.assertEqual(stored["status"], "obe")
        self.assertNotIn("claimed_by", stored)

    def test_unlanded_worktree_work_refused(self):
        self.write_items(
            [make_item("task-a", related_files=[{"path": "/repo/f.py", "note": ""}])]
        )
        dirty = [
            worktree_provenance.WorktreeInspection(
                target="/repo-wt", source="worktree", dirty=True, dirty_sample=" M f.py"
            )
        ]
        with patch.object(
            worktree_provenance, "inspect_item_worktrees", return_value=dirty
        ):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError) as ctx:
                dev_status_mutation.obe_item("task-a", reason="moot")
        self.assertIn("/repo-wt", str(ctx.exception))
        self.assertEqual(self._item_by_id("task-a")["status"], "open")

    def test_numeric_id_requires_if_rev(self):
        self.write_items([make_item("task-a")])
        with self.assertRaises(dev_status_mutation.RevisionConflictError):
            dev_status_mutation.obe_item("1", reason="moot")

    def test_item_reopened_during_worktree_check_is_refused(self):
        # The unlocked pre-pass skips the worktree inspection for a non-open
        # item. If the item is reopened before the lock is taken, the locked
        # section must not close it on that uninspected verdict.
        self.write_items([make_item("task-a", status="in-progress")])
        real_prepass = dev_status_mutation._prepass_committed_work

        def prepass_then_reopen(cmd, slug_or_id, paths):
            verdict = real_prepass(cmd, slug_or_id, paths)
            self.write_items([make_item("task-a")])
            return verdict

        with patch.object(
            dev_status_mutation, "_prepass_committed_work", prepass_then_reopen
        ):
            with self.assertRaises(dev_status_mutation.InvalidItemStateError):
                dev_status_mutation.obe_item("task-a", reason="moot")
        self.assertEqual(self._item_by_id("task-a")["status"], "open")

    def test_reopen_clears_obe_record(self):
        self.write_items([make_obe_item("task-a")])
        dev_status_mutation.reopen_item("task-a")
        stored = self._item_by_id("task-a")
        self.assertEqual(stored["status"], "open")
        self.assertNotIn("obe", stored)

    def test_start_and_done_refuse_obe_item(self):
        self.write_items([make_obe_item("task-a")])
        for func in (dev_status_mutation.start_item, dev_status_mutation.done_item):
            with self.subTest(func=func.__name__):
                with self.assertRaises(
                    dev_status_mutation.InvalidItemStateError
                ) as ctx:
                    func("task-a")
                self.assertIn("reopen", str(ctx.exception))
        stored = self._item_by_id("task-a")
        self.assertEqual(stored["status"], "obe")
        self.assertNotIn("completed_at", stored)

    def test_rename_rewrites_obe_references(self):
        item = make_obe_item("task-a", superseded_by="old-name")
        item["obe"]["reason"] = "delivered by old-name"
        self.write_items([make_item("old-name", status="done"), item])
        dev_status_mutation.rename_item("old-name", "new-name")
        obe = self._item_by_id("task-a")["obe"]
        self.assertEqual(obe["superseded_by"], "new-name")
        self.assertEqual(obe["reason"], "delivered by new-name")


class ObeCliTestCase(MutationFixture):
    """The `obe` command and the read/maintenance commands that see OBE items."""

    def _list(self, status=None):
        args = _Args(status=status, raw=False)
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            dev_status.cmd_list(args)
        return out.getvalue()

    def test_compact_confirmation_line(self):
        self.write_items([make_item("task-a")])
        out, _ = self.run_cmd(
            dev_status.cmd_obe,
            _Args(id="task-a", reason="moot", reason_file=None, superseded_by=None, force=False),
        )
        lines = [line for line in out.splitlines() if line.startswith("[")]
        self.assertEqual(len(lines), 1, out)
        self.assertTrue(lines[0].startswith("[obe] slug=task-a status=obe rev=1 "))

    def test_reason_file_is_read(self):
        reason_file = Path(self.tmpdir) / "reason.txt"
        reason_file.write_text("it's superseded\n")
        self.write_items([make_item("task-a")])
        self.run_cmd(
            dev_status.cmd_obe,
            _Args(
                id="task-a",
                reason=None,
                reason_file=str(reason_file),
                superseded_by=None,
                force=False,
            ),
        )
        self.assertEqual(self._item_by_id("task-a")["obe"]["reason"], "it's superseded")

    def test_empty_reason_file_exits_under_obe_label(self):
        reason_file = Path(self.tmpdir) / "reason.txt"
        reason_file.write_text("  \n")
        self.write_items([make_item("task-a")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_obe,
            _Args(
                id="task-a",
                reason=None,
                reason_file=str(reason_file),
                superseded_by=None,
                force=False,
            ),
        )
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("[obe] "), err)
        self.assertEqual(self._item_by_id("task-a")["status"], "open")

    def test_parser_requires_exactly_one_reason_form(self):
        parser = dev_status.build_parser()
        with patch("sys.stderr", io.StringIO()):
            for argv in (
                ["obe", "task-a"],
                ["obe", "task-a", "--reason", "x", "--reason-file", "f"],
            ):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        args = parser.parse_args(["obe", "task-a", "--reason", "x"])
        self.assertEqual((args.reason, args.reason_file), ("x", None))
        self.assertEqual(parser.parse_args(["list", "--status", "obe"]).status, "obe")

    def test_update_refuses_obe_patch(self):
        self.write_items([make_obe_item("task-a")])
        err, code = self.run_cmd_exits(
            dev_status.cmd_update, _Args(id="task-a", patch='{"obe": {}}')
        )
        self.assertEqual(code, 1)
        self.assertIn("'obe'", err)
        self.assertIn("reason", self._item_by_id("task-a")["obe"])

    def test_list_status_obe_shows_unnumbered_rows(self):
        self.write_items([make_item("task-a"), make_obe_item("task-b")])
        out = self._list(status="obe")
        rows = [line for line in out.splitlines() if "task-b" in line]
        self.assertEqual(len(rows), 1, out)
        self.assertEqual(rows[0].split()[1], "-")
        self.assertNotIn("task-a", out)

    def test_obe_only_backlog_lists_without_crashing(self):
        self.write_items([make_obe_item("task-b")])
        self.assertEqual(self._list().strip(), "(no matching items)")
        self.assertIn("task-b", self._list(status="obe"))

    def test_prune_uses_obe_close_date(self):
        old_date = (datetime.now(UTC) - timedelta(days=20)).date().isoformat()
        self.write_items(
            [
                make_obe_item("obe-old", at=old_date, updated="2099-01-01"),
                make_obe_item("obe-new", at=dev_status_mutation.today()),
            ]
        )
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            dev_status.cmd_prune(_Args(force=True))
        self.assertEqual([i["id"] for i in self.read_items()], ["obe-new"])

    def test_cited_by_treats_obe_as_closed(self):
        cited = "/repo/plan.md"
        files = [{"path": cited, "note": ""}]
        for status, expected_exit in (("obe", None), ("open", 1)):
            with self.subTest(status=status):
                item = (
                    make_obe_item("task-a", related_files=files)
                    if status == "obe"
                    else make_item("task-a", related_files=files)
                )
                self.write_items([item])
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    if expected_exit is None:
                        dev_status.cmd_cited_by(_Args(paths=[cited]))
                    else:
                        with self.assertRaises(SystemExit) as raised:
                            dev_status.cmd_cited_by(_Args(paths=[cited]))
                        self.assertEqual(raised.exception.code, expected_exit)
                self.assertIn(f"task-a {status}", out.getvalue())

    def test_validate_flags_broken_obe_records(self):
        no_record = make_item("no-record", status="obe")
        stray = make_item("stray-record")
        stray["obe"] = {"reason": "x", "at": "2026-01-01", "by": "claude"}
        blank = make_obe_item("blank-reason")
        blank["obe"]["reason"] = " "
        with_done_stamp = make_obe_item("with-stamp")
        with_done_stamp["completed_at"] = "2026-01-01"
        good = make_obe_item("good-one", superseded_by="x")
        problems = dev_status._validate_item_lifecycle(
            [no_record, stray, blank, with_done_stamp, good, make_item("x", status="nope")]
        )
        flagged = {p.split(":")[0] for p in problems}
        self.assertEqual(
            flagged,
            {
                "item no-record",
                "item stray-record",
                "item blank-reason",
                "item with-stamp",
                "item x",
            },
        )


class ArchiveItemTestCase(MutationFixture):
    def setUp(self):
        super().setUp()
        self.archive_file = self.data_dir / "items-archive.json"

    def write_archive(self, items):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.archive_file.write_text(
            json.dumps({"schema_version": 2, "items": items}, indent=2)
        )

    def read_archive(self):
        return dev_status_storage.load_archive(self.archive_file)

    def test_archive_moves_eligible_items_and_bumps_rev(self):
        today_str = datetime.now(UTC).date().isoformat()
        item1 = make_item("item-old-done", status="done")
        item1["completed_at"] = "2020-01-01"
        item2 = make_item("item-recent-done", status="done")
        item2["completed_at"] = today_str
        item3 = make_item("item-open", status="open")
        self.write_items([item1, item2, item3])
        initial_rev = dev_status_storage.load_rev(self.meta_file)

        dev_status.cmd_archive(_Args(older_than=14, dry_run=False, quiet=True, compact=False))

        # Check live store
        surviving = self.read_items()
        self.assertEqual([i["id"] for i in surviving], ["item-recent-done", "item-open"])

        # Check archive store
        archived = self.read_archive()
        self.assertEqual([i["id"] for i in archived], ["item-old-done"])

        # Check rev bump
        new_rev = dev_status_storage.load_rev(self.meta_file)
        self.assertEqual(new_rev, initial_rev + 1)

        # Check journal entry
        journal_lines = self.journal_file.read_text().strip().splitlines()
        last_entry = json.loads(journal_lines[-1])
        self.assertEqual(last_entry["cmd"], "archive")
        self.assertEqual(last_entry["rev"], new_rev)

    def test_archive_dry_run_leaves_files_untouched(self):
        item1 = make_item("item-old-done", status="done")
        item1["completed_at"] = "2020-01-01"
        self.write_items([item1])
        initial_rev = dev_status_storage.load_rev(self.meta_file)

        dev_status.cmd_archive(_Args(older_than=14, dry_run=True, quiet=True, compact=False))

        self.assertEqual([i["id"] for i in self.read_items()], ["item-old-done"])
        self.assertFalse(self.archive_file.exists())
        self.assertEqual(dev_status_storage.load_rev(self.meta_file), initial_rev)

    def test_archive_reopen_restores_from_archive(self):
        self.write_items([make_item("item-open", status="open")])
        item_archived = make_item("item-archived", status="done")
        item_archived["completed_at"] = "2020-01-01"
        self.write_archive([item_archived])
        initial_rev = dev_status_storage.load_rev(self.meta_file)

        res = dev_status_mutation.reopen_item("item-archived")
        self.assertEqual(res.slug, "item-archived")
        self.assertEqual(res.status, "open")

        # Item should be in live store now, not in archive
        live = self.read_items()
        self.assertIn("item-archived", [i["id"] for i in live])
        archived = self.read_archive()
        self.assertNotIn("item-archived", [i["id"] for i in archived])
        self.assertEqual(dev_status_storage.load_rev(self.meta_file), initial_rev + 1)

    def test_archive_add_rejects_duplicate_archived_slug(self):
        self.write_items([])
        self.write_archive([make_item("item-archived", status="done")])

        req = dev_status_mutation.NewItemRequest(
            id="item-archived",
            summary="attempt duplicate",
        )
        with self.assertRaises(dev_status_mutation.DuplicateSlugError):
            dev_status_mutation.add_item(req)

    def test_show_finds_archived_item(self):
        self.write_items([])
        self.write_archive([make_item("item-archived", status="done", summary="Old archive item")])

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            dev_status.cmd_show(_Args(id="item-archived"))

        self.assertIn("[archived]", err.getvalue())
        data = json.loads(out.getvalue())
        self.assertEqual(data["id"], "item-archived")
        self.assertEqual(data["summary"], "Old archive item")

    def test_archive_purges_inbound_blocked_by_refs_so_ready_work_remains_ready(self):
        item_old = make_item("item-old-done", status="done")
        item_old["completed_at"] = "2020-01-01"
        item_dependent = make_item("item-dependent", status="open")
        item_dependent["blocked_by"] = ["item-old-done"]
        self.write_items([item_old, item_dependent])

        dev_status.cmd_archive(_Args(older_than=14, dry_run=False, quiet=True, compact=False))

        surviving = self.read_items()
        self.assertEqual([i["id"] for i in surviving], ["item-dependent"])
        # Blocker reference must be purged so effective_blockers doesn't treat missing item as unresolved
        self.assertEqual(surviving[0].get("blocked_by"), [])
        index = dev_status_mutation.build_index(surviving)
        self.assertEqual(dev_status_mutation.effective_blockers(surviving[0], index), [])

    def test_reopen_failure_does_not_delete_from_archive(self):
        item_archived = make_item("item-archived", status="done")
        item_archived["completed_at"] = "2020-01-01"
        now_iso = datetime.now(UTC).isoformat()
        # Foreign claim within TTL that will cause ClaimCollisionError without force=True
        item_archived["claimed_by"] = {
            "harness": "claude",
            "machine_id": "other-machine",
            "pid": 999999,
            "owner_pid": 999998,
            "claimed_at": now_iso,
            "last_active": now_iso,
        }
        self.write_items([])
        self.write_archive([item_archived])

        with self.assertRaises(dev_status_mutation.ClaimCollisionError):
            dev_status_mutation.reopen_item("item-archived", force=False)

        # Archive must still hold the item
        archived = self.read_archive()
        self.assertEqual([i["id"] for i in archived], ["item-archived"])
        # Live items must remain empty
        self.assertEqual(self.read_items(), [])

    def test_archive_bumps_rev_before_writing_stores(self):
        item_old = make_item("item-old-done", status="done")
        item_old["completed_at"] = "2020-01-01"
        self.write_items([item_old])

        call_order = []
        orig_bump = dev_status_storage.bump_rev
        orig_save_items = dev_status.save_items
        orig_save_archive = dev_status.save_archive

        def tracked_bump(*a, **kw):
            call_order.append("bump_rev")
            return orig_bump(*a, **kw)

        def tracked_save_items(*a, **kw):
            call_order.append("save_items")
            return orig_save_items(*a, **kw)

        def tracked_save_archive(*a, **kw):
            call_order.append("save_archive")
            return orig_save_archive(*a, **kw)

        with (
            patch.object(dev_status_storage, "bump_rev", side_effect=tracked_bump),
            patch.object(dev_status, "save_items", side_effect=tracked_save_items),
            patch.object(dev_status, "save_archive", side_effect=tracked_save_archive),
        ):
            dev_status.cmd_archive(_Args(older_than=14, dry_run=False, quiet=True, compact=False))

        self.assertIn("bump_rev", call_order)
        self.assertIn("save_items", call_order)
        self.assertIn("save_archive", call_order)
        self.assertLess(
            call_order.index("bump_rev"),
            call_order.index("save_items"),
            "bump_rev must happen before save_items",
        )
        self.assertLess(
            call_order.index("bump_rev"),
            call_order.index("save_archive"),
            "bump_rev must happen before save_archive",
        )

    def test_add_pending_item_rejects_duplicate_archived_slug(self):
        self.write_items([])
        self.write_archive([make_item("item-archived", status="done")])

        req = dev_status_mutation.PendingAddRequest(
            id="item-archived",
            description="attempt pending duplicate",
            kind="approval",
        )
        with self.assertRaises(dev_status_mutation.DuplicateSlugError):
            dev_status_mutation.add_pending_item(req)

    def test_reopen_item_strips_archived_blockers_so_item_is_not_blocked(self):
        item_base = make_item("item-base", status="done")
        item_base["completed_at"] = "2020-01-01"
        item_child = make_item("item-child", status="done")
        item_child["completed_at"] = "2020-01-01"
        item_child["blocked_by"] = ["item-base"]

        self.write_items([])
        self.write_archive([item_base, item_child])

        res = dev_status_mutation.reopen_item("item-child")
        self.assertEqual(res.status, "open")

        # Child is in live items
        live = self.read_items()
        child_live = [i for i in live if i["id"] == "item-child"][0]
        # Satisfied archived blocker must be stripped so effective_blockers doesn't block child
        self.assertEqual(child_live.get("blocked_by"), [])
        index = dev_status_mutation.build_index(live)
        self.assertEqual(dev_status_mutation.effective_blockers(child_live, index), [])

    def test_rename_rewrites_archived_references(self):
        item_child = make_item("item-child", status="done")
        item_child["completed_at"] = "2020-01-01"
        item_child["blocked_by"] = ["item-base"]
        item_child["summary"] = "Waiting on item-base for completion"
        self.write_archive([item_child])

        item_base = make_item("item-base", status="open")
        self.write_items([item_base])

        dev_status_mutation.rename_item("item-base", "item-renamed")

        # Archive item must have updated references
        archived = self.read_archive()
        child_archived = [a for a in archived if a["id"] == "item-child"][0]
        self.assertEqual(child_archived["blocked_by"], ["item-renamed"])
        self.assertIn("item-renamed", child_archived["summary"])



class ArchiveClosedItemsTestCase(ArchiveItemTestCase):
    """archive covers every closed item, is crash-repairable, and keeps readers whole."""

    def _archive(self, **kwargs):
        args = _Args(older_than=14, dry_run=False, quiet=True, compact=False)
        args.__dict__.update(kwargs)
        dev_status.cmd_archive(args)

    def test_obe_items_archive_on_their_obe_date(self):
        old_obe = make_obe_item("obe-old", at="2020-01-01", updated="2099-01-01")
        new_obe = make_obe_item("obe-new", at=datetime.now(UTC).date().isoformat())
        self.write_items([old_obe, new_obe])
        self._archive()
        self.assertEqual([i["id"] for i in self.read_archive()], ["obe-old"])
        self.assertEqual([i["id"] for i in self.read_items()], ["obe-new"])

    def test_older_than_rejects_bad_values_as_usage_errors(self):
        parser = dev_status.build_parser()
        with patch("sys.stderr", io.StringIO()):
            for raw in ("0", "14x", "14ddd", "-3"):
                with self.subTest(raw=raw), self.assertRaises(SystemExit) as raised:
                    parser.parse_args(["archive", "--older-than", raw])
                self.assertEqual(raised.exception.code, 2)
        for raw in ("14", "14d"):
            self.assertEqual(parser.parse_args(["archive", "--older-than", raw]).older_than, 14)
        self.assertEqual(parser.parse_args(["archive"]).older_than, 14)

    def test_dry_run_takes_no_lock_and_writes_nothing(self):
        old = make_item("item-old-done", status="done")
        old["completed_at"] = "2020-01-01"
        self.write_items([old])
        out = io.StringIO()
        with patch("sys.stdout", out):
            self._archive(dry_run=True)
        self.assertIn("item-old-done", out.getvalue())
        for path in (
            self.lock_file,
            self.machine_id_file,
            self.journal_file,
            self.archive_file,
            self.pending_file,
            self.meta_file,
        ):
            self.assertFalse(path.exists(), path)

    def test_stale_archive_copy_of_live_item_is_dropped_with_nothing_new(self):
        self.write_items([make_item("task-a")])
        stale = make_item("task-a", status="done")
        stale["completed_at"] = "2020-01-01"
        kept = make_item("task-b", status="done")
        kept["completed_at"] = "2020-01-01"
        self.write_archive([stale, kept])
        self._archive()
        self.assertEqual([i["id"] for i in self.read_archive()], ["task-b"])
        self.assertEqual(self.read_items()[0]["status"], "open")
        self.assertEqual(self.read_rev(), 1)

    def test_pending_store_untouched_when_no_blocking_ref_moves(self):
        old = make_item("item-old-done", status="done")
        old["completed_at"] = "2020-01-01"
        self.write_items([old])
        self._archive()
        self.assertEqual([i["id"] for i in self.read_archive()], ["item-old-done"])
        self.assertFalse(self.pending_file.exists())

    def test_crash_at_pending_write_is_repaired_by_a_retry(self):
        old = make_item("item-old-done", status="done")
        old["completed_at"] = "2020-01-01"
        self.write_items([old])
        self.write_pending(
            [
                {
                    "id": "pend-a",
                    "created": "2026-01-01",
                    "updated": "2026-01-01",
                    "status": "waiting_for_reply",
                    "description": "waiting",
                    "kind": "email",
                    "source_ref": {},
                    "context": "",
                    "next_steps": [],
                    "blocking": ["item-old-done"],
                    "outcome": None,
                }
            ]
        )
        # The pending write is the one the live-store write must not precede: a
        # crash between them used to leave a dangling blocking ref no retry fixed.
        with patch.object(dev_status, "save_pending", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._archive()
        self._archive()
        self.assertEqual(self.read_items(), [])
        self.assertEqual([i["id"] for i in self.read_archive()], ["item-old-done"])
        self.assertEqual(self.read_pending()[0]["blocking"], [])

    def test_reopen_from_archive_keeps_only_refs_to_live_records(self):
        self.write_items([make_item("live-x")])
        self.write_pending(
            [
                {
                    "id": "pend-y",
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
        archived = make_item(
            "task-a",
            status="done",
            blocked_by=["live-x", "pend-y", "gone-z", "archived-w"],
        )
        other = make_item("archived-w", status="done")
        self.write_archive([archived, other])
        dev_status_mutation.reopen_item("task-a")
        self.assertEqual(self._item_by_id("task-a")["blocked_by"], ["live-x", "pend-y"])

    def test_rename_bumps_updated_on_rewritten_archive_rows(self):
        self.write_items([make_item("old-name")])
        row = make_item("task-a", status="done", blocked_by=["old-name"])
        untouched = make_item("task-b", status="done")
        self.write_archive([row, untouched])
        dev_status_mutation.rename_item("old-name", "new-name")
        by_id = {i["id"]: i for i in self.read_archive()}
        self.assertEqual(by_id["task-a"]["blocked_by"], ["new-name"])
        self.assertEqual(by_id["task-a"]["updated"], dev_status_mutation.today())
        self.assertEqual(by_id["task-b"]["updated"], "2026-01-01")

    def test_rename_rewrites_archived_obe_references(self):
        self.write_items([make_item("old-name")])
        row = make_obe_item("task-a", superseded_by="old-name")
        row["obe"]["reason"] = "delivered by old-name"
        self.write_archive([row])
        dev_status_mutation.rename_item("old-name", "new-name")
        obe = self.read_archive()[0]["obe"]
        self.assertEqual(obe["superseded_by"], "new-name")
        self.assertEqual(obe["reason"], "delivered by new-name")

    def test_slug_check_accepts_an_explicit_archived_slug(self):
        self.write_items([])
        self.write_archive([make_item("task-a", status="done")])
        with patch.object(dev_status_mutation, "_git_show_toplevel", return_value=None):
            with self.assertRaises(dev_status_mutation.ValidationError) as ctx:
                dev_status_mutation.slug_check("task-a", repo=self.tmpdir)
        # Resolution passed; it failed later, on the (stubbed-out) repository.
        self.assertIn("not a git repository", str(ctx.exception))

    def test_prune_still_refreshes_the_recap(self):
        self.write_items([make_item("task-a")])
        with patch.object(dev_status, "_maybe_dispatch_recap_regen") as dispatch:
            with patch("sys.stdout", io.StringIO()):
                dev_status.cmd_prune(_Args(force=True))
        dispatch.assert_called_once()

    def test_cited_by_reports_archived_citations(self):
        cited = "/repo/plan.md"
        row = make_item("task-a", status="done", related_files=[{"path": cited, "note": ""}])
        self.write_items([])
        self.write_archive([row])
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            dev_status.cmd_cited_by(_Args(paths=[cited]))
        self.assertIn("task-a done", out.getvalue())

    def test_reader_never_misses_an_item_archived_between_its_two_reads(self):
        # `archive` writes the archive store before it drops the item from the
        # live store, so a lock-free reader that reads live first then archive
        # always finds the item in at least one of them.
        old = make_item("item-old-done", status="done")
        old["completed_at"] = "2020-01-01"
        self.write_items([old])
        moved = []

        def archive_once():
            if not moved:
                moved.append(True)
                self.write_archive([old])
                self.write_items([])

        real_items = dev_status_storage.load_items
        real_archive = dev_status_storage.load_archive

        def items_then_archive_run(*a, **kw):
            result = real_items(*a, **kw)
            archive_once()
            return result

        def archive_then_archive_run(*a, **kw):
            result = real_archive(*a, **kw)
            archive_once()
            return result

        with (
            patch.object(dev_status_storage, "load_items", items_then_archive_run),
            patch.object(dev_status_storage, "load_archive", archive_then_archive_run),
        ):
            ids = [i["id"] for i in dev_status_storage.load_all_items()]
        self.assertEqual(ids, ["item-old-done"])

    def test_worker_tab_close_sees_archived_items(self):
        import herdr_delegate

        self.write_items([make_item("task-live")])
        self.write_archive([make_item("task-archived", status="done")])
        ids = {i["id"] for i in herdr_delegate.load_backlog_items()}
        self.assertEqual(ids, {"task-live", "task-archived"})



class PendingDateFieldsMutationTestCase(MutationFixture):
    """``sent_at``/``due`` through the library seam: set, clear, refuse."""

    def _add(self, **extra):
        return dev_status_mutation.add_pending_item(
            dev_status_mutation.PendingAddRequest(
                id="pend-x", description="ask", kind="email", **extra
            )
        )

    def _update(self, **fields):
        return dev_status_mutation.update_pending_item(
            "pend-x", dev_status_mutation.PendingUpdateRequest(**fields)
        )

    def test_add_with_both_fields(self):
        self._add(sent_at="2026-11-17", due="2026-12-04")
        item = self.read_pending()[0]
        self.assertEqual((item["sent_at"], item["due"]), ("2026-11-17", "2026-12-04"))

    def test_null_clear_deletes_key_and_journals_field(self):
        self._add(sent_at="2026-11-17")
        self._update(sent_at=None)
        self.assertNotIn("sent_at", self.read_pending()[0])
        self.assertEqual(self.journal_lines()[-1]["fields"], ["sent_at"])

    def test_clearing_absent_key_is_an_ordinary_update(self):
        self._add()
        rev = self.read_rev()
        self._update(due=None)
        self.assertNotIn("due", self.read_pending()[0])
        self.assertEqual(self.read_rev(), rev + 1)
        self.assertEqual(self.journal_lines()[-1]["fields"], ["due"])

    def test_each_malformed_form_refused_on_add(self):
        for bad in ("20261120", "2026-W47-5", "2026-11-20\n", "2026-02-30", 20261120):
            with self.subTest(bad=bad):
                with self.assertRaises(dev_status_mutation.ValidationError):
                    self._add(due=bad)
                self.assertEqual(self.read_pending(), [])

    def test_update_set_stores_and_journals_both_fields(self):
        self._add()
        self._update(sent_at="2026-11-17", due="2026-12-04")
        item = self.read_pending()[0]
        self.assertEqual((item["sent_at"], item["due"]), ("2026-11-17", "2026-12-04"))
        self.assertEqual(self.journal_lines()[-1]["fields"], ["due", "sent_at"])

    def test_add_refuses_unset_sentinel_before_any_write(self):
        # UNSET means "leave unchanged" on update; add has nothing to leave,
        # so it must be refused up front, not crash mid-write after the rev bump.
        rev = self.read_rev()
        with self.assertRaises(dev_status_mutation.ValidationError):
            self._add(sent_at=dev_status_mutation.UNSET)
        self.assertEqual((self.read_pending(), self.read_rev()), ([], rev))

    def test_mixed_patch_with_bad_date_writes_nothing(self):
        self._add()
        before = (self.read_pending(), self.read_rev(), len(self.journal_lines()))
        with self.assertRaises(dev_status_mutation.ValidationError):
            self._update(context="new context", sent_at="2026-11-17", due="nope")
        after = (self.read_pending(), self.read_rev(), len(self.journal_lines()))
        self.assertEqual(before, after)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
