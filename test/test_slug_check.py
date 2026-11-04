#!/usr/bin/env python3
"""Tests for dev_status's ``slug-check`` — the commit-gate slug-leak scan.

Every case runs against a sandboxed HOME with a throwaway backlog store and
a throwaway git repository under the test's own tmpdir. Real git runs only
via ``subprocess.run`` against those fixture repos (marked
``allow_real_subprocess`` per test/AGENTS.md); nothing touches real user
state.

Run standalone: python3 test_slug_check.py  (from test/)
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
from pytest_shim import pytest  # noqa: E402
import test_layouts  # noqa: E402
import agent_toolkit_paths  # noqa: E402
import dev_status  # noqa: E402  (path insert above)
import dev_status_mutation  # noqa: E402  (path insert above)


class SlugCheckFixture(unittest.TestCase):
    """Sandbox HOME + throwaway store + throwaway git repo at <tmpdir>/repo."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        test_layouts.activate_sandbox_home(Path(self.tmpdir), self.addCleanup)
        self.data_dir = agent_toolkit_paths.path_for("work-items")
        self.items_file = self.data_dir / "items.json"
        self.pending_file = self.data_dir / "pending_items.json"
        self.overrides_file = self.data_dir / "slug_overrides.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repo = Path(self.tmpdir) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    # ── helpers ───────────────────────────────────────────────────────────

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def write_items(self, *slugs):
        self.items_file.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "items": [
                        {
                            "id": s,
                            "status": "open",
                            "created": "2026-01-01",
                            "updated": "2026-01-01",
                            "summary": f"Summary of {s}",
                            "category": "feature",
                            "blocked_by": [],
                            "related_files": [],
                            "context": "",
                            "next_steps": "",
                        }
                        for s in slugs
                    ],
                },
                indent=2,
            )
        )

    def write_pending(self, *slugs):
        self.pending_file.write_text(
            json.dumps(
                {"schema_version": 1, "items": [{"id": s} for s in slugs]},
                indent=2,
            )
        )

    def stage(self, relpath: str, text: str) -> None:
        target = self.repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self._git("add", relpath)

    def stage_bytes(self, relpath: str, data: bytes) -> None:
        target = self.repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._git("add", relpath)

    def run_main(self, *argv):
        """Run the CLI exactly as argparse sees it; return (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            patch("sys.argv", ["dev_status", *argv]),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            try:
                dev_status.main()
            except SystemExit as exc:
                code = 0 if exc.code is None else exc.code
        return code, out.getvalue(), err.getvalue()

    def slug_check(self, *extra):
        return self.run_main("slug-check", "--repo", str(self.repo), *extra)

    def override_rows(self):
        if not self.overrides_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.overrides_file.read_text().splitlines()
            if line.strip()
        ]


# Real git against a fixture repo in tmpdir below: no network, no real user
# state, no writes outside the test tmpdir/sandbox HOME.
@pytest.mark.allow_real_subprocess
class LeakRefusalTestCase(SlugCheckFixture):
    @pytest.mark.regression(
        "land-gate-slug-leak-refused",
        "AssertionError: 2 != 1",
    )
    def test_leak_refused_with_file_line(self):
        self.write_items("atk-fake-leak")
        self.stage("report.md", "see atk-fake-leak for details\n")
        code, out, err = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("report.md:1: atk-fake-leak", out)

    def test_item_slug_matched_with_slug_flag(self):
        self.write_items("atk-alpha", "atk-beta")
        self.stage("notes.md", "about atk-alpha\n")
        code, out, _ = self.slug_check("--slug", "atk-alpha")
        self.assertEqual(code, 1)
        self.assertIn("notes.md:1: atk-alpha", out)

    def test_other_store_id_matched_even_without_slug(self):
        self.write_items("atk-alpha", "atk-beta")
        self.stage("notes.md", "about atk-beta\n")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("notes.md:1: atk-beta", out)

    def test_numeric_slug_resolves_and_never_matches_digits(self):
        self.write_items("atk-one")
        self.stage("count.md", "there are 1 items\n")
        code, out, _ = self.slug_check("--slug", "1")
        self.assertEqual(code, 0)

    def test_numeric_slug_still_matches_resolved_slug(self):
        self.write_items("atk-one")
        self.stage("notes.md", "about atk-one\n")
        code, out, _ = self.slug_check("--slug", "1")
        self.assertEqual(code, 1)
        self.assertIn("notes.md:1: atk-one", out)

    def test_two_ids_one_line_are_two_findings(self):
        self.write_items("atk-a1", "atk-b2")
        self.stage("both.md", "x atk-a1 y atk-b2\n")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("both.md:1: atk-a1", out)
        self.assertIn("both.md:1: atk-b2", out)

    def test_deleted_lines_not_scanned(self):
        self.write_items("atk-del")
        self.stage("old.md", "about atk-del\n")
        self._git("commit", "-q", "-m", "seed")
        (self.repo / "old.md").write_text("clean now\n")
        self._git("add", "old.md")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)

    def test_mode_only_change_scans_nothing(self):
        self.write_items("atk-mode")
        self.stage("tool.sh", "# about atk-mode\n")
        self._git("commit", "-q", "-m", "seed")
        (self.repo / "tool.sh").chmod(0o755)
        self._git("add", "tool.sh")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)

    def test_diff_suppressed_file_still_scanned(self):
        self.write_items("atk-attr")
        self.stage(".gitattributes", "leak.bin -diff\n")
        self.stage("leak.bin", "text with atk-attr inside\n")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("leak.bin", out)
        self.assertIn("atk-attr", out)

    def test_leak_outside_supplied_subdir_still_found(self):
        self.write_items("atk-sub")
        self.stage("README.md", "about atk-sub\n")
        sub = self.repo / "sub"
        sub.mkdir()
        code, out, _ = self.run_main(
            "slug-check", "--repo", str(sub)
        )
        self.assertEqual(code, 1)
        self.assertIn("README.md:1: atk-sub", out)

    def test_clean_tree_reports_hash_and_exits_zero(self):
        self.write_items("atk-clean")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)
        self.assertIn("sha256=", out)

    def test_summary_hash_tracks_scanned_bytes(self):
        self.write_items("atk-hash")
        self.stage("a.md", "one\n")
        code1, out1, _ = self.slug_check()
        self.stage("a.md", "two\n")
        code2, out2, _ = self.slug_check()
        self.assertEqual((code1, code2), (0, 0))
        hash1 = out1.split("sha256=", 1)[1].strip()
        hash2 = out2.split("sha256=", 1)[1].strip()
        self.assertNotEqual(hash1, hash2)


@pytest.mark.allow_real_subprocess
class OverrideTestCase(SlugCheckFixture):
    def test_allow_per_finding_two_ids_one_line(self):
        self.write_items("atk-a1", "atk-b2")
        self.stage("both.md", "x atk-a1 y atk-b2\n")
        code, out, _ = self.slug_check(
            "--allow", "both.md:1:atk-a1", "fixture string"
        )
        self.assertEqual(code, 1)
        self.assertNotIn("both.md:1:atk-a1", out.split("overridden:", 1)[0])
        self.assertIn("both.md:1: atk-b2", out)
        code, out, _ = self.slug_check(
            "--allow", "both.md:1:atk-a1", "fixture string",
            "--allow", "both.md:1:atk-b2", "also fixture",
        )
        self.assertEqual(code, 0)
        rows = self.override_rows()
        # The refused first run recorded its own accepted override too —
        # every accepted override is recorded, refused run or not.
        pairs = {(r["id"], r["reason"]) for r in rows}
        self.assertEqual(
            pairs, {("atk-a1", "fixture string"), ("atk-b2", "also fixture")}
        )

    def test_allow_echoed_with_reason(self):
        self.write_items("atk-echo")
        self.stage("e.md", "mentions atk-echo\n")
        code, out, _ = self.slug_check(
            "--allow", "e.md:1:atk-echo", "not a reference"
        )
        self.assertEqual(code, 0)
        self.assertIn(
            'overridden: e.md:1:atk-echo reason="not a reference"', out
        )

    def test_override_row_fields(self):
        self.write_items("atk-row")
        self.stage("r.md", "mentions atk-row\n")
        self.slug_check("--slug", "atk-row", "--allow", "r.md:1:atk-row", "why")
        rows = self.override_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "atk-row")
        self.assertEqual(row["slug"], "atk-row")
        self.assertEqual(row["path"], "r.md")
        self.assertEqual(row["line"], 1)
        self.assertEqual(row["reason"], "why")
        self.assertIn("text_sha256", row)

    def test_allow_without_matching_finding_refused(self):
        self.write_items("atk-x")
        self.stage("f.md", "clean\n")
        code, _, err = self.slug_check(
            "--allow", "f.md:1:atk-x", "stale"
        )
        self.assertEqual(code, 2)

    def test_allow_empty_reason_refused(self):
        self.write_items("atk-x")
        self.stage("f.md", "mentions atk-x\n")
        code, _, _ = self.slug_check("--allow", "f.md:1:atk-x", "")
        self.assertEqual(code, 2)

    def test_sidecar_append_failure_makes_override_ineffective(self):
        self.write_items("atk-x")
        self.stage("f.md", "mentions atk-x\n")
        self.overrides_file.mkdir()
        code, _, _ = self.slug_check("--allow", "f.md:1:atk-x", "why")
        self.assertEqual(code, 2)

    def test_no_blanket_bypass_exists(self):
        self.write_items("atk-x")
        self.stage("f.md", "mentions atk-x\n")
        code, _, err = self.run_main(
            "slug-check", "--repo", str(self.repo), "--allow-all"
        )
        self.assertEqual(code, 2)


@pytest.mark.allow_real_subprocess
class StoreGuardTestCase(SlugCheckFixture):
    def test_missing_items_store_is_error(self):
        self.stage("f.md", "anything\n")
        code, _, err = self.slug_check()
        self.assertEqual(code, 2)

    def test_malformed_store_missing_items_key_is_error(self):
        self.items_file.write_text(json.dumps({"schema_version": 2}))
        self.stage("f.md", "anything\n")
        code, _, _ = self.slug_check()
        self.assertEqual(code, 2)

    def test_malformed_store_items_not_list_is_error(self):
        self.items_file.write_text(
            json.dumps({"schema_version": 2, "items": {"a": 1}})
        )
        self.stage("f.md", "anything\n")
        code, _, _ = self.slug_check()
        self.assertEqual(code, 2)

    def test_malformed_store_entry_without_string_id_is_error(self):
        self.items_file.write_text(
            json.dumps({"schema_version": 2, "items": [{"id": 5}]})
        )
        self.stage("f.md", "anything\n")
        code, _, _ = self.slug_check()
        self.assertEqual(code, 2)

    def test_empty_store_is_clean_but_git_errors_still_reported(self):
        self.write_items()
        not_a_repo = Path(self.tmpdir) / "bare-dir"
        not_a_repo.mkdir()
        code, _, _ = self.run_main(
            "slug-check", "--repo", str(not_a_repo)
        )
        self.assertEqual(code, 2)

    def test_pending_slug_is_rejected(self):
        self.write_items("atk-real")
        self.write_pending("atk-pend")
        self.stage("f.md", "mentions atk-real\n")
        code, _, _ = self.slug_check("--slug", "atk-pend")
        self.assertEqual(code, 2)

    def test_unknown_slug_is_error(self):
        self.write_items("atk-real")
        self.stage("f.md", "clean\n")
        code, _, _ = self.slug_check("--slug", "atk-nope")
        self.assertEqual(code, 2)


@pytest.mark.allow_real_subprocess
class PatchParserEdgeCaseTestCase(SlugCheckFixture):
    def test_deletion_only_hunk_is_not_an_error(self):
        self.write_items("atk-del2")
        self.stage("two.md", "safe\nabout atk-del2\n")
        self._git("commit", "-q", "-m", "seed")
        (self.repo / "two.md").write_text("safe\n")
        self._git("add", "two.md")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)

    def test_missing_final_newline_still_scanned(self):
        self.write_items("atk-nonl")
        self.stage_bytes("nn.md", b"mentions atk-nonl")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("nn.md:1: atk-nonl", out)

    def test_binary_deletion_is_not_an_error(self):
        self.write_items("atk-bin")
        self.stage_bytes("image.bin", b"\x89PNG\x00data\n")
        self._git("commit", "-q", "-m", "seed")
        self._git("rm", "-q", "image.bin")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)

    def test_form_feed_content_cannot_hide_a_slug(self):
        self.write_items("atk-feed")
        self.stage_bytes("ff.md", b"clean\x0c--- atk-feed\n")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("atk-feed", out)

    def test_binary_filename_with_embedded_prefix_scanned(self):
        self.write_items("atk-weird")
        (self.repo / "asset and b").mkdir()
        self.stage_bytes("asset and b/data.bin", b"\x00atk-weird\n")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("asset and b/data.bin", out)
        self.assertIn("atk-weird", out)

    def test_submodule_change_is_not_an_error(self):
        self.write_items("atk-submod")
        self._git("config", "diff.submodule", "log")
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            "160000,0123456789012345678901234567890123456789,mysub",
        )
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)


@pytest.mark.allow_real_subprocess
class SnapshotBehaviorTestCase(SlugCheckFixture):
    """Behavior of the --raw/--numstat snapshot scan (no C-quoted parsing)."""

    def test_diff_suppressed_modified_file_still_scanned(self):
        # A *modified* text file marked -diff is opaque to git, so its whole
        # blob is scanned (not just added lines) -- a pathname-based
        # attribute a path-free blob diff would lose.
        self.write_items("atk-diffmod")
        self.stage("tracked.txt", "line one\nunchanged atk-diffmod line\n")
        self._git("commit", "-q", "-m", "seed")
        self.stage(".gitattributes", "tracked.txt -diff\n")
        self.stage(
            "tracked.txt",
            "line one\nunchanged atk-diffmod line\nnew clean line\n",
        )
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("tracked.txt:2: atk-diffmod", out)

    def test_pure_rename_treated_as_add(self):
        # --no-renames turns a rename into delete+add, so the new path is
        # scanned as a new file (conservative: a pre-existing id is flagged,
        # overridable via --allow, never silently passed).
        self.write_items("atk-renamed")
        self.stage("oldname.md", "mentions atk-renamed\n")
        self._git("commit", "-q", "-m", "seed")
        self._git("mv", "oldname.md", "newname.md")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("newname.md:1: atk-renamed", out)

    def test_mode_only_change_to_binary_file_not_flagged(self):
        # old_oid == new_oid (identical content) is skipped before binary
        # classification, so a chmod of a binary file with an existing id is
        # not flagged.
        self.write_items("atk-binmode")
        self.stage_bytes("tool.bin", b"\x00atk-binmode\n")
        self._git("commit", "-q", "-m", "seed")
        (self.repo / "tool.bin").chmod(0o755)
        self._git("add", "tool.bin")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)

    def test_new_empty_file_clean(self):
        self.write_items("atk-empty")
        self.stage_bytes("empty.bin", b"")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 0)

    def test_hash_repeatable(self):
        self.write_items("atk-rep")
        self.stage("a.md", "same content\n")
        code1, out1, _ = self.slug_check()
        code2, out2, _ = self.slug_check()
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(
            out1.split("sha256=", 1)[1].strip(),
            out2.split("sha256=", 1)[1].strip(),
        )

    def test_gitlink_replaced_by_file_scanned(self):
        # A tracked submodule replaced by a text file is a typechange whose
        # old side is a commit OID; the new file's content must be scanned
        # rather than failing to diff a commit against a blob.
        self.write_items("atk-gitlink")
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            "160000,0123456789012345678901234567890123456789,vendor",
        )
        self._git("commit", "-q", "-m", "seed")
        self.stage("vendor", "mentions atk-gitlink\n")
        code, out, _ = self.slug_check()
        self.assertEqual(code, 1)
        self.assertIn("vendor:1: atk-gitlink", out)


class SnapshotParserUnitTestCase(unittest.TestCase):
    """Fail-closed parsing of the combined --raw --numstat snapshot."""

    _ZERO = b"0" * 40
    _HEX = b"a" * 40

    def test_snapshot_parses_raw_and_numstat(self):
        output = (
            b":100644 100644 " + self._ZERO + b" " + self._HEX + b" A\0f.txt\0"
            b"1\t0\tf.txt\0"
        )
        entries, binary = dev_status_mutation._parse_slug_check_snapshot(output)
        self.assertEqual(
            entries,
            [("100644", "100644", self._ZERO.decode(), self._HEX.decode(), "A", "f.txt")],
        )
        self.assertEqual(binary, {"f.txt": False})

    def test_snapshot_truncated_raw_raises(self):
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation._parse_slug_check_snapshot(b":100644 100644 00000000")

    def test_snapshot_missing_path_raises(self):
        raw = b":100644 100644 " + self._ZERO + b" " + self._HEX + b" A\0"
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation._parse_slug_check_snapshot(raw)

    def test_snapshot_numstat_binary(self):
        output = (
            b":000000 100644 " + self._ZERO + b" " + self._HEX + b" A\0f.bin\0"
            b"-\t-\tf.bin\0"
        )
        _, binary = dev_status_mutation._parse_slug_check_snapshot(output)
        self.assertEqual(binary, {"f.bin": True})

    def test_snapshot_numstat_malformed_raises(self):
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation._parse_slug_check_snapshot(b"1\tf.txt\0")

    def test_snapshot_numstat_duplicate_path_raises(self):
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation._parse_slug_check_snapshot(b"1\t0\tf.txt\0" * 2)

    def test_blob_text_diff_added_lines(self):
        diff = (
            b"diff --git a/x b/x\nindex 0..1 100644\n--- a/x\n+++ b/x\n"
            b"@@ -1 +1,2 @@\n a\n+b\n"
        )
        self.assertEqual(dev_status_mutation._parse_blob_text_diff(diff), [(2, "b")])

    def test_blob_text_diff_binary_marker_returns_none(self):
        diff = (
            b"diff --git a/x b/x\nindex 0..1 100644\n"
            b"Binary files a/x and b/x differ\n"
        )
        self.assertIsNone(dev_status_mutation._parse_blob_text_diff(diff))

    def test_blob_text_diff_incomplete_hunk_raises(self):
        diff = b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n"
        with self.assertRaises(dev_status_mutation.ValidationError):
            dev_status_mutation._parse_blob_text_diff(diff)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
