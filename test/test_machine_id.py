#!/usr/bin/env python3
"""Tests for dev_status_storage.machine_id(): stable, validated, persist-or-fail.

Run with: python3 test/test_machine_id.py (or under pytest).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import pytest
except ImportError:  # direct `python3 test/test_machine_id.py` without pytest installed

    class _NoMark:
        def __getattr__(self, name: str) -> object:
            def mark(*args: object, **kwargs: object) -> object:
                if len(args) == 1 and callable(args[0]) and not kwargs:
                    return args[0]
                return lambda f: f

            return mark

    class pytest:  # type: ignore[no-redef]
        mark = _NoMark()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import dev_status_storage  # noqa: E402

AGENT_SCRIPTS = str(Path(__file__).resolve().parent.parent / "agent-scripts")


class MachineIdFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.data_dir = self.tmp / "backlog"
        self.mid_file = self.data_dir / "_machine_id"

    def tearDown(self) -> None:
        for p in [self.data_dir, self.tmp]:
            if p.exists():
                p.chmod(0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self) -> str:
        return dev_status_storage.machine_id(self.mid_file, self.data_dir)


class TestValidExisting(MachineIdFixture):
    def test_valid_id_is_returned_unchanged(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("0ee2ec8d")
        self.assertEqual(self.call(), "0ee2ec8d")

    def test_trailing_newline_is_accepted(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("0ee2ec8d\n")
        self.assertEqual(self.call(), "0ee2ec8d")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_valid_id_is_read_from_a_read_only_directory(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("0ee2ec8d")
        self.data_dir.chmod(0o555)
        self.assertEqual(self.call(), "0ee2ec8d")
        self.assertEqual(self.call(), "0ee2ec8d")


class TestInvalidExisting(MachineIdFixture):
    def assert_rejected(self, content: bytes) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_bytes(content)
        with self.assertRaises(dev_status_storage.MachineIdError) as ctx:
            self.call()
        self.assertEqual(ctx.exception.path, self.mid_file)
        self.assertIn(str(self.mid_file), str(ctx.exception))
        self.assertIn("machine-id --repair", str(ctx.exception))
        # never overwritten
        self.assertEqual(self.mid_file.read_bytes(), content)

    def test_empty(self) -> None:
        self.assert_rejected(b"")

    def test_garbage(self) -> None:
        self.assert_rejected(b"not an id")

    def test_uppercase(self) -> None:
        self.assert_rejected(b"0EE2EC8D")

    def test_wrong_length(self) -> None:
        self.assert_rejected(b"0ee2ec8d00")

    def test_not_utf8(self) -> None:
        self.assert_rejected(b"\xff\xfe\x00\x01")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("0ee2ec8d")
        self.mid_file.chmod(0o000)
        try:
            with self.assertRaises(dev_status_storage.MachineIdError):
                self.call()
        finally:
            self.mid_file.chmod(0o644)


class TestCreation(MachineIdFixture):
    def test_missing_file_is_created_and_then_stable(self) -> None:
        first = self.call()
        self.assertRegex(first, r"^[0-9a-f]{8}$")
        self.assertEqual(self.mid_file.read_text(), first)
        self.assertEqual(self.call(), first)

    @pytest.mark.regression(
        "machine-id-invents-a-new-id-when-unwritable",
        "AttributeError: module 'dev_status_storage' has no attribute 'MachineIdError'",
    )
    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_directory_fails_instead_of_inventing_ids(self) -> None:
        # Regression: this used to return a fresh random id on every call.
        self.data_dir.mkdir(parents=True)
        self.data_dir.chmod(0o555)
        with self.assertRaises(dev_status_storage.MachineIdError) as ctx:
            self.call()
        self.assertIn("cannot create machine id", str(ctx.exception))
        with self.assertRaises(dev_status_storage.MachineIdError):
            self.call()
        self.assertFalse(self.mid_file.exists())

    def test_publish_race_rereads_the_winner(self) -> None:
        # Another process publishes between our temp write and our link.
        real_link = os.link

        def racing_link(src: str, dst: str) -> None:
            Path(dst).write_text("abcdef01")
            real_link(src, dst)  # raises FileExistsError

        with patch.object(dev_status_storage.os, "link", racing_link):
            self.assertEqual(self.call(), "abcdef01")
        self.assertEqual(self.mid_file.read_text(), "abcdef01")
        leftovers = [p.name for p in self.data_dir.iterdir() if p.name.startswith(".machine_id_tmp_")]
        self.assertEqual(leftovers, [])

    def test_final_file_never_exists_before_it_is_complete(self) -> None:
        seen: list[bool] = []
        real_link = os.link

        def watching_link(src: str, dst: str) -> None:
            seen.append(Path(dst).exists())
            real_link(src, dst)

        with patch.object(dev_status_storage.os, "link", watching_link):
            self.call()
        self.assertEqual(seen, [False])


class TestConcurrentProcesses(MachineIdFixture):
    @pytest.mark.regression(
        "machine-id-concurrent-first-calls-create-two-ids",
        "AssertionError: 2 != 1 : {'9e984800', '63b327e5'}",
    )
    @pytest.mark.allow_real_subprocess  # spawns 8 python children racing on a tmp id file
    def test_concurrent_first_calls_agree_on_one_id(self) -> None:
        script = self.tmp / "call.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import sys, time
                sys.path.insert(0, {AGENT_SCRIPTS!r})
                import dev_status_storage
                from pathlib import Path
                go = Path(sys.argv[3])
                while not go.exists():
                    time.sleep(0.001)
                print(dev_status_storage.machine_id(Path(sys.argv[1]), Path(sys.argv[2])))
                """
            )
        )
        go = self.tmp / "go"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        procs = [
            subprocess.Popen(
                [sys.executable, str(script), str(self.mid_file), str(self.data_dir), str(go)],
                stdout=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(8)
        ]
        go.touch()
        ids = {p.communicate(timeout=30)[0].strip() for p in procs}
        self.assertEqual(len(ids), 1, ids)
        self.assertEqual(ids, {self.mid_file.read_text()})


class TestCrashDuringPublish(MachineIdFixture):
    @pytest.mark.allow_real_subprocess  # SIGKILLs a python child at a checkpoint (fault harness)
    def test_kill_before_publish_leaves_no_invalid_id_file(self) -> None:
        import test_fault_injection as fi

        home = self.tmp / "home"
        home.mkdir()
        script = self.tmp / "create.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, {AGENT_SCRIPTS!r})
                import dev_status_storage
                from pathlib import Path
                dev_status_storage.machine_id(Path(sys.argv[1]), Path(sys.argv[2]))
                """
            )
        )
        fi.run_killed_at(
            "machine-id.publish",
            script,
            [str(self.mid_file), str(self.data_dir)],
            home=home,
            declared=["machine-id.publish"],
            extra_env={"PYTHONPATH": AGENT_SCRIPTS},
        )
        self.assertFalse(self.mid_file.exists())
        created = self.call()
        self.assertRegex(created, r"^[0-9a-f]{8}$")


class TestOperationIdentity(MachineIdFixture):
    """Identity is resolved once per operation, before any write."""

    def setUp(self) -> None:
        super().setUp()
        import dev_status_mutation

        self.mut = dev_status_mutation
        self.data_dir.mkdir(parents=True)
        self.items = self.data_dir / "items.json"

    def add(self, slug: str) -> None:
        self.mut.add_item(
            self.mut.NewItemRequest(id=slug, summary=f"summary {slug}"),
            items_path=self.items,
        )

    def store_bytes(self) -> dict[str, bytes]:
        return {
            p.name: p.read_bytes()
            for p in self.data_dir.iterdir()
            if p.is_file() and not p.name.endswith(".lock")
        }

    def test_add_with_broken_id_writes_nothing(self) -> None:
        self.mid_file.write_text("0ee2ec8d")
        self.add("first-item")
        self.mid_file.write_text("garbage")
        before = self.store_bytes()
        with self.assertRaises(dev_status_storage.MachineIdError):
            self.add("second-item")
        self.assertEqual(self.store_bytes(), before)

    def test_lock_is_usable_after_an_identity_failure(self) -> None:
        self.mid_file.write_text("garbage")
        with self.assertRaises(dev_status_storage.MachineIdError):
            self.add("never-created")
        self.assertEqual(dev_status_storage._backlog_lock_count, 0)
        self.assertEqual(dev_status_storage._backlog_lock_fd, -1)
        self.mid_file.write_text("0ee2ec8d")
        self.add("after-repair")
        self.assertIn("after-repair", self.items.read_text())

    def test_explicit_store_identity_wins_over_a_broken_default(self) -> None:
        default_file = self.tmp / "default" / "_machine_id"
        default_file.parent.mkdir()
        default_file.write_text("garbage")
        self.mid_file.write_text("0ee2ec8d")
        with patch.object(dev_status_storage, "MACHINE_ID_FILE", default_file):
            self.add("explicit-store")
            self.mut.start_item("explicit-store", items_path=self.items)
        journal = (self.data_dir / "journal.jsonl").read_text().splitlines()
        machines = {json.loads(line).get("machine") for line in journal}
        self.assertEqual(machines, {"0ee2ec8d"})
        items = json.loads(self.items.read_text())
        claim = next(i for i in items["items"] if i["id"] == "explicit-store")["claimed_by"]
        self.assertEqual(claim["machine_id"], "0ee2ec8d")

    def test_snapshot_read_works_with_a_broken_id(self) -> None:
        self.mid_file.write_text("garbage")
        with dev_status_storage.backlog_lock(self.data_dir, self.data_dir / ".backlog.lock", require_identity=False):
            self.assertIsNone(dev_status_storage.operation_machine_id())

    def test_nested_operation_on_another_store_is_refused(self) -> None:
        self.mid_file.write_text("0ee2ec8d")
        other = self.tmp / "other"
        other.mkdir()
        (other / "_machine_id").write_text("abcdef01")
        with dev_status_storage.backlog_lock(self.data_dir, self.data_dir / ".backlog.lock"):
            with self.assertRaises(dev_status_storage.BacklogLockStoreMismatch):
                with dev_status_storage.backlog_lock(other, other / ".backlog.lock"):
                    pass
        self.assertEqual(dev_status_storage._backlog_lock_count, 0)

    def test_lock_wait_diagnostic_needs_identity_and_never_raises(self) -> None:
        import dev_status_impl  # journal paths resolve through impl when loaded

        journal = self.data_dir / "journal.jsonl"
        meta = self.data_dir / "_meta.json"
        with patch.object(
            dev_status_storage, "_BACKLOG_LOCK_WAIT_JOURNAL_THRESHOLD_SECONDS", -1
        ), patch.object(dev_status_storage, "JOURNAL_FILE", journal), patch.object(
            dev_status_storage, "META_FILE", meta
        ), patch.object(dev_status_impl, "JOURNAL_FILE", journal), patch.object(
            dev_status_impl, "META_FILE", meta
        ):
            self.mid_file.write_text("garbage")
            with dev_status_storage.backlog_lock(self.data_dir, self.data_dir / ".backlog.lock", require_identity=False):
                pass
            self.assertFalse(journal.exists())
            self.mid_file.write_text("0ee2ec8d")
            with dev_status_storage.backlog_lock(self.data_dir, self.data_dir / ".backlog.lock"):
                pass
        kinds = [json.loads(line)["cmd"] for line in journal.read_text().splitlines()]
        self.assertEqual(kinds, ["lock-wait"])

    def test_each_operation_rereads_the_id(self) -> None:
        # A long-lived process picks up a repair at its next operation.
        self.mid_file.write_text("0ee2ec8d")
        with dev_status_storage.backlog_lock(self.data_dir, self.data_dir / ".backlog.lock"):
            self.assertEqual(dev_status_storage.operation_machine_id(), "0ee2ec8d")
            self.mid_file.write_text("abcdef01")  # repaired by another process
            self.assertEqual(dev_status_storage.operation_machine_id(), "0ee2ec8d")
        with dev_status_storage.backlog_lock(self.data_dir, self.data_dir / ".backlog.lock"):
            self.assertEqual(dev_status_storage.operation_machine_id(), "abcdef01")


class TestRepair(MachineIdFixture):
    def repair(self) -> "dev_status_storage.MachineIdRepair":
        return dev_status_storage.repair_machine_id(self.mid_file, self.data_dir)

    def backups(self) -> list[Path]:
        return sorted(self.data_dir.glob("_machine_id.bad-*"))

    def test_valid_id_is_left_alone(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("0ee2ec8d")
        result = self.repair()
        self.assertEqual((result.action, result.machine_id), ("unchanged", "0ee2ec8d"))
        self.assertEqual(self.backups(), [])

    def test_missing_id_is_created(self) -> None:
        result = self.repair()
        self.assertEqual(result.action, "created")
        self.assertEqual(self.mid_file.read_text(), result.machine_id)

    def test_invalid_id_is_backed_up_and_replaced(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_bytes(b"\xff\xfe garbage")
        result = self.repair()
        self.assertEqual(result.action, "replaced")
        self.assertRegex(result.machine_id, r"^[0-9a-f]{8}$")
        self.assertEqual(self.mid_file.read_text(), result.machine_id)
        self.assertEqual(result.old_content, b"\xff\xfe garbage")
        [backup] = self.backups()
        self.assertEqual(backup.read_bytes(), b"\xff\xfe garbage")
        self.assertEqual(result.backup, backup)
        self.assertEqual(self.call(), result.machine_id)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_id_is_refused_and_untouched(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("0ee2ec8d")
        self.mid_file.chmod(0o000)
        try:
            with self.assertRaises(dev_status_storage.MachineIdError) as ctx:
                self.repair()
            self.assertIn("never replaced", str(ctx.exception))
        finally:
            self.mid_file.chmod(0o644)
        self.assertEqual(self.mid_file.read_text(), "0ee2ec8d")
        self.assertEqual(self.backups(), [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_directory_is_a_clear_error(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("garbage")
        self.data_dir.chmod(0o555)
        with self.assertRaises(dev_status_storage.MachineIdError) as ctx:
            self.repair()
        self.assertIn("writable", str(ctx.exception))

    @pytest.mark.allow_real_subprocess  # two python children repairing at once
    def test_concurrent_repairs_make_one_id_and_one_backup(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.mid_file.write_text("garbage")
        script = self.tmp / "repair.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import sys, time
                sys.path.insert(0, {AGENT_SCRIPTS!r})
                import dev_status_storage
                from pathlib import Path
                go = Path(sys.argv[3])
                while not go.exists():
                    time.sleep(0.001)
                r = dev_status_storage.repair_machine_id(Path(sys.argv[1]), Path(sys.argv[2]))
                print(r.action, r.machine_id)
                """
            )
        )
        go = self.tmp / "go"
        procs = [
            subprocess.Popen(
                [sys.executable, str(script), str(self.mid_file), str(self.data_dir), str(go)],
                stdout=subprocess.PIPE,
                text=True,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            )
            for _ in range(4)
        ]
        go.touch()
        outs = sorted(p.communicate(timeout=30)[0].split()[0] for p in procs)
        self.assertEqual(outs, ["replaced", "unchanged", "unchanged", "unchanged"])
        self.assertEqual(len(self.backups()), 1)


REPO = Path(__file__).resolve().parent.parent
DEV_STATUS = str(REPO / "agent-scripts" / "dev_status.py")
RUNNER = str(REPO / "agent-scripts" / "to_tickets_runner.py")


@pytest.mark.allow_real_subprocess  # runs the real CLI against a throwaway HOME
class TestCliWithBrokenId(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.data = self.home / ".claude" / "data" / "backlog"
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            XDG_STATE_HOME=str(self.home / ".local" / "state"),
            PYTHONDONTWRITEBYTECODE="1",
            DEVSTATUS_AGENT="1",
        )
        self.env.pop("AGENT_TOOLKIT_HOME", None)
        self.env.pop("AGENT_TOOLKIT_TIMING", None)
        ok = self.cli("add", '{"id": "demo-item", "summary": "demo"}')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        (self.data / "_machine_id").write_text("garbage")

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def cli(self, *args: str, script: str = DEV_STATUS) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, script, *args],
            env=self.env,
            cwd=self.home,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def items_bytes(self) -> bytes:
        return (self.data / "items.json").read_bytes()

    def assert_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("machine-id --repair", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_is_refused_with_nothing_written(self) -> None:
        before = self.items_bytes()
        self.assert_refused(self.cli("add", '{"id": "second-item", "summary": "x"}'))
        self.assertEqual(self.items_bytes(), before)

    def test_start_is_refused(self) -> None:
        self.assert_refused(self.cli("start", "demo-item", "--allow-main"))

    def test_pending_add_is_refused(self) -> None:
        self.assert_refused(
            self.cli(
                "pending",
                "add",
                '{"id": "wait-thing", "description": "d", "kind": "email"}',
            )
        )

    def test_render_warns_on_stdout_and_succeeds(self) -> None:
        r = self.cli("render")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dead-claim sweep skipped", r.stdout)
        self.assertIn("[feat] demo", r.stdout)  # the dashboard still renders

    def test_list_raw_keeps_stdout_clean(self) -> None:
        r = self.cli("list", "--raw")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("sweep skipped", r.stdout)
        self.assertIn("sweep skipped", r.stderr)
        self.assertIn("demo-item", r.stdout)

    def test_show_stdout_is_still_json(self) -> None:
        r = self.cli("show", "demo-item")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["id"], "demo-item")
        self.assertIn("sweep skipped", r.stderr)

    def test_ready_stdout_is_still_json(self) -> None:
        r = self.cli("ready")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([i["id"] for i in json.loads(r.stdout)], ["demo-item"])

    def test_machine_id_repair_warns_about_foreign_claims(self) -> None:
        (self.data / "_machine_id").write_text("0ee2ec8d")
        started = self.cli("start", "demo-item", "--allow-main")
        self.assertEqual(started.returncode, 0, started.stderr)
        (self.data / "_machine_id").write_text("garbage")
        r = self.cli("machine-id", "--repair")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("replaced invalid id", r.stdout)
        self.assertIn("demo-item", r.stderr)

    def test_machine_id_without_repair_reports_the_problem(self) -> None:
        self.assert_refused(self.cli("machine-id"))

    def test_ticket_runner_creates_nothing(self) -> None:
        batch = self.home / "batch.json"
        batch.write_text(json.dumps([{"id": "batch-one", "summary": "b"}]))
        before = self.items_bytes()
        r = self.cli("run", str(batch), script=RUNNER)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.items_bytes(), before)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
