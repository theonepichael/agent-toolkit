#!/usr/bin/env python3
"""Tests for contract-derived acceptance, verifier tokens, and contract-touch.

Covers dev_status_mutation's acceptance service (set_acceptance, the
gate-set/completion enforcement, worked_by + lifecycle invalidation, the
verifier-token file lifecycle, and contract_touch) plus the CLI layer's
token-path printing and redaction guarantees.

Tests never touch the real ~/.agent-toolkit store: every case runs against a
temporary data dir (via test_layouts.activate_sandbox_home) or an explicit
``items_path``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
from pytest_shim import pytest
import test_layouts  # noqa: E402
import agent_toolkit_paths  # noqa: E402
import dev_status  # noqa: E402  (path insert above)
import dev_status_mutation as mut
import dev_status_storage as storage


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
    gate=None,
    claimed_by=None,
    acceptance=None,
    worked_by=None,
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
    if gate is not None:
        item["gate"] = gate
    if claimed_by is not None:
        item["claimed_by"] = claimed_by
    if acceptance is not None:
        item["acceptance"] = acceptance
    if worked_by is not None:
        item["worked_by"] = worked_by
    return item


def acceptance_payload(criteria=None, independent_verifier=False, refs=None):
    return {
        "criteria": criteria
        or [
            {"text": "acceptance criterion one", "refs": refs or []},
            {"text": "acceptance criterion two", "refs": []},
        ],
        "independent_verifier": independent_verifier,
    }


class _Args:
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
    operator_override = False
    rotate_token = False
    verifier_token_file = None
    repo = None
    commit = None
    range = None

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class AcceptanceFixture(unittest.TestCase):
    """Temp store with dev_status path globals patched to a sandbox HOME."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        test_layouts.activate_sandbox_home(Path(self.tmpdir), self.addCleanup)
        self.data_dir = agent_toolkit_paths.path_for("work-items")
        self.items_file = self.data_dir / "items.json"
        self.pending_file = self.data_dir / "pending_items.json"
        self.meta_file = self.data_dir / "_meta.json"
        self.lock_file = self.data_dir / ".backlog.lock"
        self.journal_file = self.data_dir / "journal.jsonl"
        self.runs_file = self.data_dir / "runs.jsonl"
        self.machine_id_file = self.data_dir / "_machine_id"
        self.verifier_dir = self.data_dir / "verifier"
        # Never let a test spawn a real process against fabricated data.
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

    def read_items(self):
        return storage.load_items(self.items_file)

    def read_rev(self):
        return storage.load_rev(self.meta_file)

    def item(self, slug):
        return {i["id"]: i for i in self.read_items()}[slug]

    def token_path(self, slug):
        return self.verifier_dir / f"{slug}.token"

    def token_new_path(self, slug):
        return self.verifier_dir / f"{slug}.token.new"

    def token_text(self, slug):
        return (self.token_path(slug)).read_text().strip()

    def _service_acceptance_set(self, slug, payload, if_rev=None, **kw):
        return mut.set_acceptance(
            slug,
            mut.AcceptanceSetRequest(
                acceptance=payload,
                operator_override=kw.get("operator_override", False),
                rotate_token=kw.get("rotate_token", False),
            ),
            if_rev=if_rev,
            items_path=self.items_file,
        )

    def run_cli(self, func, args):
        """Run one cmd_* handler in compact mode; return (stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict("os.environ", {"DEVSTATUS_AGENT": "1"}),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            func(args)
        return out.getvalue(), err.getvalue()

    def run_cli_exits(self, func, args):
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


# ── acceptance-set ────────────────────────────────────────────────────────────


class AcceptanceSetTestCase(AcceptanceFixture):
    def test_set_on_open_never_worked_item(self):
        self.write_items([make_item("atk-a")])
        res = self._service_acceptance_set(
            "atk-a", acceptance_payload(), if_rev=self.read_rev()
        )
        acc = res.item["acceptance"]
        self.assertEqual(
            [c["text"] for c in acc["criteria"]],
            ["acceptance criterion one", "acceptance criterion two"],
        )
        self.assertFalse(acc["independent_verifier"])
        self.assertEqual(acc["history"], [])
        # implies a required gate covering the acceptance
        self.assertTrue(res.item["gate"]["required"])
        self.assertEqual(
            res.item["gate"]["criteria"],
            ["acceptance criterion one", "acceptance criterion two"],
        )

    def test_requires_if_rev_even_for_slug(self):
        self.write_items([make_item("atk-a")])
        with self.assertRaises(mut.RevisionConflictError):
            self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=None)
        with self.assertRaises(mut.RevisionConflictError):
            self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=999)

    def test_frozen_after_start(self):
        self.write_items([make_item("atk-a")])
        self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=self.read_rev())
        rev = self.read_rev()
        mut.start_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        with self.assertRaises(mut.InvalidItemStateError) as ctx:
            self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=rev)
        self.assertIn("frozen", ctx.exception.message)

    def test_frozen_after_reopen(self):
        self.write_items([make_item("atk-a")])
        self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=self.read_rev())
        rev = self.read_rev()
        mut.start_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        mut.reopen_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        # worked_by survives reopen, so acceptance stays frozen
        with self.assertRaises(mut.InvalidItemStateError):
            self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=rev)

    def test_operator_override_lifts_freeze_and_records_history(self):
        self.write_items([make_item("atk-a")])
        self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=self.read_rev())
        rev = self.read_rev()
        mut.start_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        new_payload = acceptance_payload(
            criteria=[{"text": "changed", "refs": []}]
        )
        res = self._service_acceptance_set(
            "atk-a", new_payload, if_rev=rev, operator_override=True
        )
        acc = res.item["acceptance"]
        self.assertEqual(acc["history"][-1]["kind"], "override")
        self.assertEqual(acc["history"][-1]["previous"]["criteria"][0]["text"],
                         "acceptance criterion one")
        # gate invalidated and rebuilt from the new acceptance
        self.assertIsNone(res.item["gate"]["passed_at"])
        self.assertEqual(res.item["gate"]["criteria"], ["changed"])

    def test_identical_content_is_noop(self):
        self.write_items([make_item("atk-a")])
        payload = acceptance_payload()
        self._service_acceptance_set("atk-a", payload, if_rev=self.read_rev())
        rev = self.read_rev()
        res = self._service_acceptance_set("atk-a", payload, if_rev=rev)
        self.assertEqual(res.rev, rev)
        self.assertEqual(res.item["acceptance"]["history"], [])

    def test_ref_change_is_not_noop(self):
        self.write_items([make_item("atk-a")])
        p1 = acceptance_payload(refs=[{"repo": "/r", "path": "old.md", "clause": "s1"}])
        self._service_acceptance_set("atk-a", p1, if_rev=self.read_rev())
        rev = self.read_rev()
        p2 = acceptance_payload(refs=[{"repo": "/r", "path": "new.md", "clause": "s1"}])
        res = self._service_acceptance_set("atk-a", p2, if_rev=rev)
        self.assertNotEqual(res.rev, rev)
        self.assertEqual(
            res.item["acceptance"]["criteria"][0]["refs"][0]["path"], "new.md"
        )

    def test_update_cannot_write_acceptance(self):
        self.write_items([make_item("atk-a")])
        err, code = self.run_cli_exits(
            dev_status.cmd_update,
            _Args(id="atk-a", patch='{"acceptance": {"criteria": []}}'),
        )
        self.assertEqual(code, 1)
        self.assertIn("acceptance", err)

    def test_add_with_acceptance_validates_and_implies_gate(self):
        req = mut.NewItemRequest(
            id="atk-added", summary="s", acceptance=acceptance_payload()
        )
        res = mut.add_item(req, items_path=self.items_file)
        self.assertTrue(res.item["gate"]["required"])
        self.assertEqual(
            res.item["gate"]["criteria"],
            ["acceptance criterion one", "acceptance criterion two"],
        )

    def test_add_with_bad_acceptance_refuses(self):
        req = mut.NewItemRequest(
            id="atk-bad", summary="s", acceptance={"criteria": []}
        )
        with self.assertRaises(mut.ValidationError):
            mut.add_item(req, items_path=self.items_file)


# ── gate-set + completion enforcement ─────────────────────────────────────────


class GateAcceptanceEnforcementTestCase(AcceptanceFixture):
    def _acceptance_item(self, slug="atk-a"):
        self.write_items([make_item(slug)])
        self._service_acceptance_set(slug, acceptance_payload(), if_rev=self.read_rev())
        return slug

    def test_gate_set_refuses_required_false(self):
        slug = self._acceptance_item()
        rev = self.read_rev()
        with self.assertRaises(mut.ValidationError) as ctx:
            mut.set_gate(
                slug, mut.GateSetRequest(required=False, criteria=()),
                if_rev=rev, items_path=self.items_file,
            )
        self.assertIn("unrequired gate", ctx.exception.message)

    def test_gate_set_refuses_missing_acceptance_text(self):
        slug = self._acceptance_item()
        rev = self.read_rev()
        with self.assertRaises(mut.ValidationError) as ctx:
            mut.set_gate(
                slug,
                mut.GateSetRequest(required=True, criteria=("acceptance criterion one",)),
                if_rev=rev, items_path=self.items_file,
            )
        self.assertIn("missing", ctx.exception.message)

    def test_gate_set_accepts_superset(self):
        slug = self._acceptance_item()
        rev = self.read_rev()
        res = mut.set_gate(
            slug,
            mut.GateSetRequest(
                required=True,
                criteria=("acceptance criterion one", "acceptance criterion two", "extra"),
            ),
            if_rev=rev, items_path=self.items_file,
        )
        self.assertEqual(
            res.item["gate"]["criteria"],
            ["acceptance criterion one", "acceptance criterion two", "extra"],
        )

    def test_done_refuses_unpassed_acceptance_gate(self):
        slug = self._acceptance_item()
        rev = self.read_rev()
        mut.start_item(slug, if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        with self.assertRaises(mut.GateUnmetError):
            mut.done_item(slug, if_rev=rev, items_path=self.items_file)

    def test_done_refuses_gate_missing_acceptance_text(self):
        slug = self._acceptance_item()
        rev = self.read_rev()
        mut.start_item(slug, if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        # Craft a gate that is passed but does not cover one acceptance text
        # (bypassing gate-set enforcement via direct store write).
        items = self.read_items()
        items[0]["gate"] = {
            "required": True,
            "criteria": ["acceptance criterion one"],
            "passed_at": "2026-01-02",
        }
        self.write_items(items)
        with self.assertRaises(mut.GateUnmetError):
            mut.done_item(slug, if_rev=self.read_rev(), items_path=self.items_file)

    def test_legacy_item_without_acceptance_unchanged(self):
        self.write_items(
            [make_item("atk-legacy", status="in-progress",
                       gate={"required": True, "criteria": ["x"], "passed_at": "2026-01-01"})]
        )
        # a legacy passed gate still completes (today's behaviour)
        res = mut.done_item("atk-legacy", if_rev=self.read_rev(), items_path=self.items_file)
        self.assertEqual(res.status, "done")


# ── worked_by + lifecycle invalidation ────────────────────────────────────────


class LifecycleInvalidationTestCase(AcceptanceFixture):
    def _pass_gate(self, slug, rev, criteria=None):
        criteria = criteria or {str(i + 1): f"manual:{i}" for i in range(2)}
        return mut.pass_gate(
            slug, mut.GatePassRequest(coverage=criteria), if_rev=rev,
            items_path=self.items_file,
        )

    def _acceptance_item_with_passed_gate(self):
        self.write_items([make_item("atk-a")])
        self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=self.read_rev())
        rev = self.read_rev()
        self._pass_gate("atk-a", rev)
        return self.read_rev()

    def test_start_invalidates_passed_gate(self):
        rev = self._acceptance_item_with_passed_gate()
        item = self.item("atk-a")
        self.assertIsNotNone(item["gate"]["passed_at"])
        res = mut.start_item("atk-a", if_rev=rev, items_path=self.items_file)
        gate = res.item["gate"]
        self.assertIsNone(gate["passed_at"])
        self.assertNotIn("passed_via", gate)
        self.assertNotIn("coverage", gate)
        self.assertNotIn("verified_by", gate)
        self.assertEqual(len(res.item["worked_by"]), 1)

    def test_reject_invalidates_passed_gate(self):
        # start -> review -> reject; reject re-enters in-progress
        self.write_items([make_item("atk-a")])
        self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=self.read_rev())
        rev = self.read_rev()
        self._pass_gate("atk-a", rev)
        rev = self.read_rev()
        mut.start_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        # re-pass the gate so reject has something to invalidate
        self._pass_gate("atk-a", rev)
        rev = self.read_rev()
        mut.review_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        res = mut.reject_item("atk-a", "nope", if_rev=rev, items_path=self.items_file)
        self.assertIsNone(res.item["gate"]["passed_at"])
        self.assertNotIn("verified_by", res.item["gate"])

    def test_reopen_clears_verified_by(self):
        self.write_items([make_item("atk-a")])
        self._service_acceptance_set(
            "atk-a", acceptance_payload(independent_verifier=True), if_rev=self.read_rev()
        )
        rev = self.read_rev()
        token = self.token_text("atk-a")
        self._service_acceptance_set is not None  # silence lint
        # start then pass with a verifier token, then reopen
        mut.start_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        res = mut.pass_gate(
            "atk-a", mut.GatePassRequest(coverage={"1": "manual:a", "2": "manual:b"}),
            if_rev=rev, items_path=self.items_file, verifier_token_file=str(self.token_path("atk-a")),
        )
        self.assertIn("verified_by", res.item["gate"])
        rev = self.read_rev()
        res = mut.reopen_item("atk-a", if_rev=rev, items_path=self.items_file)
        self.assertNotIn("verified_by", res.item["gate"])

    def test_legacy_start_keeps_passed_gate(self):
        self.write_items(
            [make_item("atk-legacy",
                       gate={"required": True, "criteria": ["x"], "passed_at": "2026-01-01"})]
        )
        res = mut.start_item("atk-legacy", if_rev=self.read_rev(), items_path=self.items_file)
        self.assertEqual(res.item["gate"]["passed_at"], "2026-01-01")

    def test_done_then_reopen_freezes_acceptance(self):
        # A completion followed by reopen (never passing through start) must
        # not unfreeze acceptance: done records worked_by too.
        self.write_items([make_item("atk-a")])
        self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=self.read_rev())
        rev = self.read_rev()
        mut.pass_gate(
            "atk-a",
            mut.GatePassRequest(coverage={"1": "manual:a", "2": "manual:b"}),
            if_rev=rev, items_path=self.items_file,
        )
        rev = self.read_rev()
        mut.done_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        mut.reopen_item("atk-a", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        with self.assertRaises(mut.InvalidItemStateError):
            self._service_acceptance_set("atk-a", acceptance_payload(), if_rev=rev)


# ── verifier token lifecycle ──────────────────────────────────────────────────


class VerifierTokenTestCase(AcceptanceFixture):
    def _verifier_item(self, slug="atk-v"):
        self.write_items([make_item(slug)])
        self._service_acceptance_set(
            slug, acceptance_payload(independent_verifier=True), if_rev=self.read_rev()
        )
        return slug

    def test_token_file_is_0600_and_hash_only_on_item(self):
        slug = self._verifier_item()
        self.assertTrue(self.token_path(slug).exists())
        self.assertEqual(oct(self.token_path(slug).stat().st_mode & 0o777), "0o600")
        acc = self.item(slug)["acceptance"]
        self.assertIn("verifier_token_sha256", acc)
        # the plaintext token is never in the item
        self.assertNotIn("verifier_token", str(acc).replace("verifier_token_sha256", ""))

    def test_gate_pass_refuses_without_token_and_accepts_current(self):
        slug = self._verifier_item()
        rev = self.read_rev()
        mut.start_item(slug, if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        coverage = {"1": "manual:a", "2": "manual:b"}
        with self.assertRaises(mut.ValidationError) as ctx:
            mut.pass_gate(slug, mut.GatePassRequest(coverage=coverage),
                          if_rev=rev, items_path=self.items_file)
        self.assertIn("independent verifier required", ctx.exception.message)
        res = mut.pass_gate(
            slug, mut.GatePassRequest(coverage=coverage), if_rev=rev,
            items_path=self.items_file, verifier_token_file=str(self.token_path(slug)),
        )
        self.assertEqual(res.item["gate"]["verified_by"]["via"], "token")

    def test_wrong_item_token_refused(self):
        self.write_items([make_item("atk-v"), make_item("atk-v2")])
        self._service_acceptance_set(
            "atk-v", acceptance_payload(independent_verifier=True), if_rev=self.read_rev()
        )
        rev = self.read_rev()
        self._service_acceptance_set(
            "atk-v2", acceptance_payload(independent_verifier=True), if_rev=rev
        )
        rev = self.read_rev()
        mut.start_item("atk-v", if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        coverage = {"1": "manual:a", "2": "manual:b"}
        with self.assertRaises(mut.ValidationError):
            mut.pass_gate(
                "atk-v", mut.GatePassRequest(coverage=coverage), if_rev=rev,
                items_path=self.items_file,
                verifier_token_file=str(self.token_path("atk-v2")),
            )

    def test_verifier_check_agrees_with_gate_pass(self):
        slug = self._verifier_item()
        self.assertEqual(mut.verifier_check(slug, str(self.token_path(slug)), items_path=self.items_file), "valid")
        # write a wrong token into a scratch file
        wrong = self.verifier_dir / "wrong.token"
        wrong.write_text("not-the-token")
        self.assertEqual(mut.verifier_check(slug, str(wrong), items_path=self.items_file), "invalid")

    def test_rotate_token_invalidates_old_and_accepts_new(self):
        slug = self._verifier_item()
        old_token = self.token_text(slug)
        rev = self.read_rev()
        res = self._service_acceptance_set(
            slug, acceptance_payload(independent_verifier=True),
            if_rev=rev, rotate_token=True,
        )
        self.assertEqual(res.item["acceptance"]["history"][-1]["kind"], "rotate")
        new_token = self.token_text(slug)
        self.assertNotEqual(old_token, new_token)
        # old token now invalid, new token valid
        old_file = self.verifier_dir / "old.token"
        old_file.write_text(old_token)
        self.assertEqual(mut.verifier_check(slug, str(old_file), items_path=self.items_file), "invalid")
        self.assertEqual(mut.verifier_check(slug, str(self.token_path(slug)), items_path=self.items_file), "valid")

    def test_atomic_publication_recovery(self):
        slug = self._verifier_item()
        token = self.token_text(slug)
        # simulate a crash: .token gone, .token.new present with matching hash
        self.token_path(slug).unlink()
        self.token_new_path(slug).write_text(token)
        self.token_new_path(slug).chmod(0o600)
        # a read path recovers it
        self.assertEqual(mut.verifier_check(slug, str(self.token_path(slug)), items_path=self.items_file), "valid")
        self.assertTrue(self.token_path(slug).exists())
        self.assertFalse(self.token_new_path(slug).exists())

    def test_rename_moves_token_file(self):
        slug = self._verifier_item("atk-v")
        old_token = self.token_text(slug)
        rev = self.read_rev()
        mut.rename_item(slug, "atk-renamed", if_rev=rev, items_path=self.items_file)
        self.assertFalse(self.token_path("atk-v").exists())
        self.assertEqual(self.token_path("atk-renamed").read_text().strip(), old_token)

    def test_remove_deletes_token_file(self):
        slug = self._verifier_item("atk-v")
        rev = self.read_rev()
        mut.remove_item(slug, if_rev=rev, items_path=self.items_file)
        self.assertFalse(self.token_path(slug).exists())
        self.assertFalse(self.token_new_path(slug).exists())

    def test_turning_off_verifier_removes_token_after_save(self):
        slug = self._verifier_item("atk-v")
        self.assertTrue(self.token_path(slug).exists())
        rev = self.read_rev()
        res = self._service_acceptance_set(
            slug, acceptance_payload(independent_verifier=False), if_rev=rev
        )
        self.assertNotIn("verifier_token_sha256", res.item["acceptance"])
        self.assertFalse(self.token_path(slug).exists())
        self.assertFalse(self.token_new_path(slug).exists())

    def test_turning_off_verifier_does_not_delete_token_before_save(self):
        slug = self._verifier_item("atk-v")
        token = self.token_text(slug)
        rev = self.read_rev()
        with patch.object(mut, "_save_items", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._service_acceptance_set(
                    slug, acceptance_payload(independent_verifier=False), if_rev=rev
                )
        # The old token survives the failed save (deletion happens post-save).
        self.assertEqual(self.token_path(slug).read_text().strip(), token)

    def test_token_never_in_cli_output(self):
        slug = self._verifier_item("atk-v")
        token = self.token_text(slug)
        rev = self.read_rev()
        # gate-pass error path
        mut.start_item(slug, if_rev=rev, items_path=self.items_file)
        rev = self.read_rev()
        err_text, _code = self.run_cli_exits(
            dev_status.cmd_gate_pass,
            _Args(id=slug, json='{"coverage": {"1": "manual:a", "2": "manual:b"}}', if_rev=rev),
        )
        self.assertNotIn(token, err_text)
        # acceptance-set path (token generated) — print never leaks the token
        self.write_items([make_item("atk-new")])
        rev = self.read_rev()
        out, err = self.run_cli(
            dev_status.cmd_acceptance_set,
            _Args(id="atk-new",
                  json=json.dumps(acceptance_payload(independent_verifier=True)),
                  if_rev=rev),
        )
        new_token = self.token_text("atk-new")
        self.assertNotIn(new_token, out + err)
        # verifier-check prints only valid/invalid
        out, err = self.run_cli(
            dev_status.cmd_verifier_check,
            _Args(id="atk-new", verifier_token_file=str(self.token_path("atk-new"))),
        )
        self.assertEqual(out.strip(), "valid")
        self.assertNotIn(new_token, out + err)


# ── contract-touch ────────────────────────────────────────────────────────────


class ContractTouchTestCase(AcceptanceFixture):
    def _contract_item(self):
        self.write_items(
            [make_item(
                "atk-c",
                acceptance={
                    "criteria": [
                        {"text": "c1", "refs": [
                            {"repo": "/main/repo", "path": "templates/land.md.tmpl", "clause": "s1"},
                            {"repo": "/main/repo", "path": "docs/a b.md", "clause": "s2"},
                        ]},
                        {"text": "c2", "refs": []},
                    ],
                    "independent_verifier": False,
                },
            )]
        )
        return "atk-c"

    def test_staged_mode_matches_ref_paths(self):
        slug = self._contract_item()
        with patch.object(mut, "_git_name_status", return_value=[("M", None, "templates/land.md.tmpl")]), \
             patch.object(mut, "_main_checkout_root", return_value="/main/repo"):
            matches = mut.contract_touch(slug, repo="/main/repo", items_path=self.items_file)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["path"], "templates/land.md.tmpl")
        self.assertEqual(matches[0]["clause"], "s1")

    def test_nul_and_rename_handling(self):
        slug = self._contract_item()
        with patch.object(mut, "_git_name_status",
                          return_value=[("R100", "templates/land.md.tmpl", "templates/land2.md.tmpl")]), \
             patch.object(mut, "_main_checkout_root", return_value="/main/repo"):
            matches = mut.contract_touch(slug, repo="/main/repo", items_path=self.items_file)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["renamed"], True)
        self.assertEqual(matches[0]["path"], "templates/land.md.tmpl")

    def test_range_mode_passes_range_spec(self):
        slug = self._contract_item()
        captured = {}
        def fake(repo_dir, range_spec):
            captured["range"] = range_spec
            return []
        with patch.object(mut, "_git_name_status_range", side_effect=fake), \
             patch.object(mut, "_main_checkout_root", return_value="/main/repo"):
            mut.contract_touch(slug, repo="/main/repo", range_spec="main..HEAD", items_path=self.items_file)
        self.assertEqual(captured["range"], "main..HEAD")

    def test_wrong_repo_is_not_matched(self):
        slug = self._contract_item()
        with patch.object(mut, "_git_name_status", return_value=[("M", None, "templates/land.md.tmpl")]), \
             patch.object(mut, "_main_checkout_root", return_value="/other/repo"):
            matches = mut.contract_touch(slug, repo="/other/repo", items_path=self.items_file)
        self.assertEqual(matches, [])

    def test_multiple_clauses_same_file_both_reported(self):
        self.write_items(
            [
                make_item(
                    "atk-multi",
                    acceptance={
                        "criteria": [
                            {
                                "text": "c1",
                                "refs": [
                                    {"repo": "/main/repo", "path": "contract.md", "clause": "s1"},
                                    {"repo": "/main/repo", "path": "contract.md", "clause": "s2"},
                                ],
                            }
                        ],
                        "independent_verifier": False,
                    },
                )
            ]
        )
        with patch.object(mut, "_git_name_status", return_value=[("M", None, "contract.md")]), \
             patch.object(mut, "_main_checkout_root", return_value="/main/repo"):
            matches = mut.contract_touch("atk-multi", repo="/main/repo", items_path=self.items_file)
        self.assertEqual(len(matches), 2)
        self.assertEqual({m["clause"] for m in matches}, {"s1", "s2"})

    def test_commit_mode_uses_commit_name_status(self):
        slug = self._contract_item()
        captured = {}
        def fake(repo_dir, commit):
            captured["commit"] = commit
            return []
        with patch.object(mut, "_git_commit_name_status", side_effect=fake), \
             patch.object(mut, "_main_checkout_root", return_value="/main/repo"):
            mut.contract_touch(slug, repo="/main/repo", commit="abc123", items_path=self.items_file)
        self.assertEqual(captured["commit"], "abc123")

    def test_no_refs_returns_empty(self):
        self.write_items([make_item("atk-norefs")])
        self.assertEqual(mut.contract_touch("atk-norefs", repo="/x", items_path=self.items_file), [])

    def test_git_failure_raises(self):
        slug = self._contract_item()
        with patch.object(mut, "_git_name_status", return_value=None), \
             patch.object(mut, "_main_checkout_root", return_value="/main/repo"):
            with self.assertRaises(mut.ValidationError):
                mut.contract_touch(slug, repo="/main/repo", items_path=self.items_file)

    def test_unresolvable_repo_root_raises(self):
        slug = self._contract_item()
        with patch.object(mut, "_git_name_status", return_value=[]), \
             patch.object(mut, "_main_checkout_root", return_value=None):
            with self.assertRaises(mut.ValidationError):
                mut.contract_touch(slug, repo="/main/repo", items_path=self.items_file)


# ── real git integration for _git_name_status ─────────────────────────────────


class GitNameStatusTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, text=True, check=False,
        )

    def _commit(self, msg):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg)

    @pytest.mark.allow_real_subprocess
    def test_name_status_parses_nul_names_rename_copy(self):
        (self.repo / "a b.md").write_text("x")
        self._commit("add")
        self._git("mv", "a b.md", "c d.md")
        (self.repo / "copy.md").write_text("y")
        self._git("add", "copy.md")
        self._commit("rename + add")
        entries = mut._git_name_status(str(self.repo), ["HEAD^", "HEAD"])
        kinds = {e[0] for e in entries}
        # rename (R...) and added (A) entries
        self.assertTrue(any(e[0].startswith("R") for e in entries))
        self.assertIn(("A", None, "copy.md"), entries)
        rename = next(e for e in entries if e[0].startswith("R"))
        self.assertEqual(rename[1], "a b.md")
        self.assertEqual(rename[2], "c d.md")

    @pytest.mark.allow_real_subprocess
    def test_range_catches_change_then_revert(self):
        (self.repo / "contract.md").write_text("v1")
        self._commit("add contract")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "contract.md").write_text("changed")
        self._commit("change contract")
        (self.repo / "contract.md").write_text("v1")
        self._commit("revert contract")
        entries = mut._git_name_status_range(str(self.repo), f"{base}..HEAD")
        paths = {p for (_s, old, new) in entries for p in (old, new) if p}
        self.assertIn("contract.md", paths)

    @pytest.mark.allow_real_subprocess
    def test_root_commit_diff_includes_added_file(self):
        (self.repo / "contract.md").write_text("v1")
        self._commit("root commit")
        root = self._git("rev-parse", "HEAD").stdout.strip()
        entries = mut._git_commit_name_status(str(self.repo), root)
        self.assertIn(("A", None, "contract.md"), entries)

    @pytest.mark.allow_real_subprocess
    def test_merge_commit_first_parent_diff(self):
        main_branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        (self.repo / "contract.md").write_text("v1")
        self._commit("base")
        self._git("checkout", "-q", "-b", "feature")
        (self.repo / "contract.md").write_text("changed")
        self._commit("change contract")
        self._git("checkout", "-q", main_branch)
        self._git("merge", "-q", "--no-ff", "feature", "-m", "merge feature")
        merge = self._git("rev-parse", "HEAD").stdout.strip()
        entries = mut._git_commit_name_status(str(self.repo), merge)
        self.assertIsNotNone(entries)
        paths = {p for (_s, old, new) in entries for p in (old, new) if p}
        self.assertIn("contract.md", paths)


class AcceptanceSetIfRevHelpTest(unittest.TestCase):
    """acceptance-set refuses a slug call without --if-rev, so its help says so."""

    def _subparser_help(self, name: str) -> str:
        import argparse

        import dev_status_impl

        parser = dev_status_impl.build_parser()
        sub = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        return " ".join(sub.choices[name].format_help().split())

    def test_acceptance_set_help_requires_if_rev_for_slugs(self):
        text = self._subparser_help("acceptance-set")
        self.assertIn("required even for a slug", text)
        self.assertNotIn("required when <id> is numeric", text)

    def test_other_commands_keep_numeric_only_wording(self):
        text = self._subparser_help("start")
        self.assertIn("is numeric", text)
