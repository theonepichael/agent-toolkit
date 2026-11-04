#!/usr/bin/env python3
"""Tests for herdr_delegate.py's foreman `watch` and `answer` subcommands and
the `launch --handoff` flag.

Everything runs in process: herdr is faked at the module's herdr() boundary and
`time.sleep` is patched out, so nothing reaches a real herdr socket or mutates
a real worktree. The watch/answer paths exercise scripted pane sequences, never
a live worker.
"""

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
import herdr_delegate  # noqa: E402


PICKER_DIR = Path(__file__).resolve().parent / "fixtures" / "picker"


def _picker_screen(name: str) -> str:
    return (PICKER_DIR / f"{name}.txt").read_text(encoding="utf-8")


def _edit_screen(name: str) -> str:
    return (PICKER_DIR / f"{name}.txt").read_text(encoding="utf-8")


class WatchFakeHerdr:
    """Fake herdr for `watch`: scripted `agent list` responses, last one repeats."""

    def __init__(self, list_script: list[list[dict[str, object]]]) -> None:
        self.list_script = list(list_script)
        self.last: list[dict[str, object]] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> dict[str, object]:
        self.calls.append(list(argv))
        if argv[0] == "agent" and argv[1] == "list":
            if self.list_script:
                self.last = self.list_script.pop(0)
            return {"result": {"agents": list(self.last)}}
        return {"result": {}}


def _agent(name: str, status: str, seq: int) -> dict[str, object]:
    return {"name": name, "agent_status": status, "state_change_seq": seq}


class WatchTests(unittest.TestCase):
    def run_watch(self, argv: list[str], fake: Callable) -> tuple[int, str, str]:
        with (
            mock.patch.object(herdr_delegate, "herdr", fake),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            sys.argv = ["herdr_delegate.py", *argv]
            out, err = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    herdr_delegate.main()
            except SystemExit as e:
                return int(e.code or 0), out.getvalue(), err.getvalue()
            return 0, out.getvalue(), err.getvalue()

    def test_reports_an_already_blocked_worker_and_reruns_until_acked(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        blocked = [_agent("atk-w1", "blocked", 5)]
        code, out, _ = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"], WatchFakeHerdr([blocked])
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["attention"]["atk-w1"], {"status": "blocked", "seq": 5})

        # Rerun without acking: reports again.
        code, out, _ = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"], WatchFakeHerdr([blocked])
        )
        self.assertEqual(code, 0)
        self.assertIn("atk-w1", json.loads(out)["attention"])

    def test_acked_agent_is_not_rereported(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        code, out, _ = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1",
             "--ack", "atk-w1=5"],
            WatchFakeHerdr([[_agent("atk-w1", "blocked", 5)]]),
        )
        self.assertEqual(code, 124)  # acked, nothing to report -> timeout
        self.assertEqual(json.loads(out)["attention"], {})

    def test_gone_is_reported_until_ack_gone(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        # First run: the agent is present.
        self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"],
            WatchFakeHerdr([[_agent("atk-w1", "working", 3)]]),
        )
        # Second run: gone.
        code, out, _ = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"],
            WatchFakeHerdr([[]]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["gone"], ["atk-w1"])

        # Ack-gone stops the report.
        code, out, _ = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1",
             "--ack-gone", "atk-w1"],
            WatchFakeHerdr([[]]),
        )
        self.assertEqual(code, 124)
        self.assertEqual(json.loads(out)["gone"], [])

    def test_second_concurrent_watcher_exits_2(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        with herdr_delegate._watch_lock(state):
            code, _, err = self.run_watch(
                ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"],
                WatchFakeHerdr([[]]),
            )
        self.assertEqual(code, 2)
        self.assertIn("locked", err)

    def test_malformed_agent_record_exits_2_without_mutating_state(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        state.write_text('{"agents": {}, "membership": [], "pending_gone": []}')
        code, _, err = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"],
            WatchFakeHerdr([[{"name": "atk-w1", "agent_status": "blocked"}]]),
        )
        self.assertEqual(code, 2)
        self.assertIn("state_change_seq", err)
        # State untouched.
        self.assertEqual(
            state.read_text(), '{"agents": {}, "membership": [], "pending_gone": []}'
        )

    def test_corrupt_state_exits_2(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        state.write_text("{not json")
        code, _, err = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"],
            WatchFakeHerdr([[]]),
        )
        self.assertEqual(code, 2)
        self.assertIn("corrupt", err)

    def test_malformed_envelope_exits_2(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"

        def fake(argv: list[str]) -> dict[str, object]:
            return {"result": {"no_agents_here": []}}

        code, _, err = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"], fake
        )
        self.assertEqual(code, 2)
        self.assertIn("malformed", err)

    def test_strict_rejects_unknown_names(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        code, _, err = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1", "--strict"],
            WatchFakeHerdr([[]]),
        )
        self.assertEqual(code, 2)
        self.assertIn("atk-w1", err)

    def test_grace_window_is_not_reset_by_reruns(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        # First run: an idle agent within grace -> suppressed, but first_seen_at persisted.
        code, _, _ = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1", "--grace-s", "60"],
            WatchFakeHerdr([[_agent("atk-w1", "idle", 3)]]),
        )
        self.assertEqual(code, 124)
        persisted = json.loads(state.read_text())
        first_seen = persisted["agents"]["atk-w1"]["first_seen_at"]
        self.assertGreater(first_seen, 0)

    def test_relaunch_under_same_name_resets_grace_and_ever_working(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        # Old session: working, seq=5, pane="w1:p1", first_seen_at=1000.0 (past grace window).
        old_time = 1000.0
        state.write_text(
            json.dumps({
                "agents": {
                    "atk-w1": {
                        "acked_seq": 5,
                        "first_seen_at": old_time,
                        "ever_working": True,
                        "last_seq": 5,
                        "pane_id": "w1:p1",
                        "agent_session": {"agent": "pi", "kind": "path", "value": "/path/s1.jsonl"},
                    }
                },
                "membership": ["atk-w1"],
                "pending_gone": [],
            })
        )
        # Relaunch under same name: new pane, new session, seq=1, status="idle" (startup flicker).
        new_agent = {
            "name": "atk-w1",
            "agent_status": "idle",
            "state_change_seq": 1,
            "pane_id": "w1:p2",
            "agent_session": {"agent": "pi", "kind": "path", "value": "/path/s2.jsonl"},
        }
        with mock.patch("time.time", return_value=1200.0):
            code, out, _ = self.run_watch(
                ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1", "--grace-s", "60"],
                WatchFakeHerdr([[new_agent]]),
            )
        # Startup idle must be suppressed by grace (timeout 124, attention empty)
        self.assertEqual(code, 124)
        payload = json.loads(out)
        self.assertEqual(payload["attention"], {})
        # State must be reset: first_seen_at updated to now (1200.0), ever_working False, acked_seq 0
        persisted = json.loads(state.read_text())
        agent_state = persisted["agents"]["atk-w1"]
        self.assertEqual(agent_state["first_seen_at"], 1200.0)
        self.assertFalse(agent_state["ever_working"])
        self.assertEqual(agent_state["acked_seq"], 0)
        self.assertEqual(agent_state["pane_id"], "w1:p2")
        self.assertEqual(agent_state["agent_session"]["value"], "/path/s2.jsonl")
        self.assertEqual(agent_state["last_seq"], 1)

    def test_relaunch_detected_by_seq_lower_than_persisted(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        state.write_text(
            json.dumps({
                "agents": {
                    "atk-w1": {
                        "acked_seq": 5,
                        "first_seen_at": 1000.0,
                        "ever_working": True,
                        "last_seq": 5,
                    }
                },
                "membership": ["atk-w1"],
                "pending_gone": [],
            })
        )
        # Only seq is lower (1 < 5); pane_id and agent_session absent
        new_agent = _agent("atk-w1", "idle", 1)
        with mock.patch("time.time", return_value=1200.0):
            code, out, _ = self.run_watch(
                ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1", "--grace-s", "60"],
                WatchFakeHerdr([[new_agent]]),
            )
        self.assertEqual(code, 124)
        persisted = json.loads(state.read_text())
        agent_state = persisted["agents"]["atk-w1"]
        self.assertEqual(agent_state["first_seen_at"], 1200.0)
        self.assertFalse(agent_state["ever_working"])
        self.assertEqual(agent_state["acked_seq"], 0)

    def test_relaunch_detected_by_terminal_id_change(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        state.write_text(
            json.dumps({
                "agents": {
                    "atk-w1": {
                        "acked_seq": 0,
                        "first_seen_at": 1000.0,
                        "ever_working": True,
                        "last_seq": 5,
                        "terminal_id": "term-1",
                    }
                },
                "membership": ["atk-w1"],
                "pending_gone": [],
            })
        )
        new_agent = {
            "name": "atk-w1",
            "agent_status": "idle",
            "state_change_seq": 5,
            "terminal_id": "term-2",
        }
        with mock.patch("time.time", return_value=1200.0):
            code, out, _ = self.run_watch(
                ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1", "--grace-s", "60"],
                WatchFakeHerdr([[new_agent]]),
            )
        self.assertEqual(code, 124)
        persisted = json.loads(state.read_text())
        agent_state = persisted["agents"]["atk-w1"]
        self.assertEqual(agent_state["first_seen_at"], 1200.0)
        self.assertFalse(agent_state["ever_working"])
        self.assertEqual(agent_state["terminal_id"], "term-2")

    def test_relaunch_detected_by_pane_id_change(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        state.write_text(
            json.dumps({
                "agents": {
                    "atk-w1": {
                        "acked_seq": 0,
                        "first_seen_at": 1000.0,
                        "ever_working": True,
                        "last_seq": 5,
                        "pane_id": "w1:p1",
                    }
                },
                "membership": ["atk-w1"],
                "pending_gone": [],
            })
        )
        new_agent = {
            "name": "atk-w1",
            "agent_status": "idle",
            "state_change_seq": 5,
            "pane_id": "w1:p2",
        }
        with mock.patch("time.time", return_value=1200.0):
            code, out, _ = self.run_watch(
                ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1", "--grace-s", "60"],
                WatchFakeHerdr([[new_agent]]),
            )
        self.assertEqual(code, 124)
        persisted = json.loads(state.read_text())
        agent_state = persisted["agents"]["atk-w1"]
        self.assertEqual(agent_state["first_seen_at"], 1200.0)
        self.assertFalse(agent_state["ever_working"])
        self.assertEqual(agent_state["pane_id"], "w1:p2")

    def test_unchanged_session_never_resets_state(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        original_time = 1000.0
        state.write_text(
            json.dumps({
                "agents": {
                    "atk-w1": {
                        "acked_seq": 3,
                        "first_seen_at": original_time,
                        "ever_working": True,
                        "last_seq": 4,
                        "pane_id": "w1:p1",
                        "terminal_id": "term-1",
                        "agent_session": {"agent": "pi", "kind": "path", "value": "/path/s1.jsonl"},
                    }
                },
                "membership": ["atk-w1"],
                "pending_gone": [],
            })
        )
        same_agent = {
            "name": "atk-w1",
            "agent_status": "working",
            "state_change_seq": 4,
            "pane_id": "w1:p1",
            "terminal_id": "term-1",
            "agent_session": {"agent": "pi", "kind": "path", "value": "/path/s1.jsonl"},
        }
        with mock.patch("time.time", return_value=1200.0):
            code, out, _ = self.run_watch(
                ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1", "--grace-s", "60"],
                WatchFakeHerdr([[same_agent]]),
            )
        self.assertEqual(code, 124)
        persisted = json.loads(state.read_text())
        agent_state = persisted["agents"]["atk-w1"]
        self.assertEqual(agent_state["first_seen_at"], original_time)
        self.assertTrue(agent_state["ever_working"])
        self.assertEqual(agent_state["acked_seq"], 3)

    def test_work_then_block_between_polls_is_surfaced(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        code, out, _ = self.run_watch(
            ["watch", "atk-w1", "--state", str(state), "--timeout-s", "1"],
            WatchFakeHerdr([
                [_agent("atk-w1", "working", 3)],
                [_agent("atk-w1", "blocked", 4)],
            ]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["attention"]["atk-w1"], {"status": "blocked", "seq": 4})

    def test_atomic_write_leaves_previous_state_on_interrupted_write(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        good = {"agents": {}, "membership": [], "pending_gone": []}
        state.write_text(json.dumps(good))
        # A stray temp file (as an interrupted write would leave) must not be read.
        state.with_name("watch.json.tmp").write_text("{corrupt")
        herdr_delegate._load_watch_state(state)  # reads the real file, not the tmp
        self.assertEqual(json.loads(state.read_text()), good)

    def test_prefix_matched_late_arrival_is_surfaced(self) -> None:
        state = Path(tempfile.mkdtemp()) / "watch.json"
        code, out, _ = self.run_watch(
            ["watch", "atk-w1", "--prefix", "atk", "--state", str(state), "--timeout-s", "1"],
            WatchFakeHerdr([
                [_agent("atk-w1", "working", 1)],
                [_agent("atk-w1", "working", 1), _agent("atk-late", "blocked", 2)],
            ]),
        )
        self.assertEqual(code, 0)
        self.assertIn("atk-late", json.loads(out)["attention"])


def _rehighlight(screen: str, to_idx: int) -> str:
    """Move the picker's `>` marker to the option at ``to_idx`` (1-based)."""
    out: list[str] = []
    for line in screen.split("\n"):
        m = re.match(r"^(> |  )(\d+)\. ", line)
        if m:
            marker = "> " if int(m.group(2)) == to_idx else "  "
            out.append(marker + m.group(2) + ". " + line[m.end() :])
        else:
            out.append(line)
    return "\n".join(out)


def _edit_mode_screen(
    options: list[str], marked: int, text: str, width: int = 42
) -> str:
    """Build an edit-mode picker screen for a given option set."""
    rule = "─" * width
    erule = " " + "─" * (width - 2)
    lines = [rule, " Commit", " Commit these changes?", ""]
    for i, label in enumerate(options, 1):
        marker = "> " if i == marked else "  "
        suffix = " ✎" if i == marked else ""
        lines.append(f"{marker}{i}. {label}{suffix}")
        lines.append("     " + "desc")
    lines += ["", " Your answer:", erule]
    lines += [" " + t for t in (text.split("\n") if text else [""])]
    lines += [erule, "", " Enter to submit • Esc to go back", rule]
    return "\n".join(lines) + "\n"


class AnswerFakeHerdr:
    """Stateful fake: simulates arrow navigation, edit mode, text, and submit."""

    def __init__(
        self,
        options: list[str],
        selected: int = 1,
        pane: str = "w:p1",
        status: str = "blocked",
    ) -> None:
        self.options = options
        self.selected = selected
        self.pane = pane
        self.status = status
        self.edit_mode = False
        self.editor_text = ""
        self.cleared = False
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> dict[str, object]:
        self.calls.append(list(argv))
        if argv[0] == "agent" and argv[1] == "get":
            return {"result": {"agent": {"agent_status": self.status, "pane_id": self.pane}}}
        if argv[0] == "pane" and argv[1] == "read":
            return {"result": {"text": self._screen()}}
        if argv[0] == "pane" and argv[1] == "send-keys":
            for key in argv[3:]:
                self._key(key)
            return {"result": {}}
        if argv[0] == "pane" and argv[1] == "send-text":
            self.editor_text = argv[3]
            return {"result": {}}
        return {"result": {}}

    def _key(self, key: str) -> None:
        if key == "down":
            self.selected = min(len(self.options), self.selected + 1)
        elif key == "up":
            self.selected = max(1, self.selected - 1)
        elif key == "enter":
            if self.edit_mode:
                self.cleared = True
            elif self.options[self.selected - 1] == herdr_delegate.herdr_picker.OTHER_OPTION_LABEL:
                self.edit_mode = True
            else:
                self.cleared = True

    def _screen(self) -> str:
        if self.cleared:
            return ""
        if self.edit_mode:
            return _edit_mode_screen(
                self.options, self.selected, self.editor_text
            )
        return _rehighlight(_base_selection(self.options), self.selected)

    def sent_keys(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "pane" and c[1] == "send-keys"]

    def sent_text(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "pane" and c[1] == "send-text"]


def _base_selection(options: list[str]) -> str:
    """A selection-mode picker screen for the given options (selected=1)."""
    width = 42
    rule = "─" * width
    lines = [rule, " Commit", " Commit these changes?", ""]
    for i, label in enumerate(options, 1):
        marker = "> " if i == 1 else "  "
        lines.append(f"{marker}{i}. {label}")
        lines.append("     " + "desc")
    lines += ["", " ↑↓ navigate • Enter to select • Esc to cancel", rule]
    return "\n".join(lines) + "\n"


def _blocked_agent(pane: str) -> dict[str, object]:
    return {"agent_status": "blocked", "pane_id": pane}


class AnswerTests(unittest.TestCase):
    OPTIONS = ["Commit now (Recommended)", "Stop here", "Something else (type it)"]

    def run_answer(self, argv: list[str], fake: Callable) -> tuple[int, str, str]:
        with (
            mock.patch.object(herdr_delegate, "herdr", fake),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            sys.argv = ["herdr_delegate.py", *argv]
            out, err = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    herdr_delegate.main()
            except SystemExit as e:
                return int(e.code or 0), out.getvalue(), err.getvalue()
            return 0, out.getvalue(), err.getvalue()

    def test_option_selection_navigates_and_submits(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS, selected=1)
        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Stop here", "--settle-s", "0"],
            fake,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"], "ok")
        self.assertEqual(fake.sent_keys(), [
            ["pane", "send-keys", "w:p1", "down"],
            ["pane", "send-keys", "w:p1", "enter"],
        ])

    def test_refuses_on_non_blocked_status(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS, status="working")
        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Stop here"], fake
        )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["input_sent"], "none")

    def test_refuses_on_pane_mismatch(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS, pane="w:p9")
        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Stop here"], fake
        )
        self.assertEqual(code, 3)
        self.assertIn("pane", json.loads(out)["reason"])

    def test_refuses_on_ambiguous_match(self) -> None:
        opts = ["Merge now", "Merge later", "Something else (type it)"]
        fake = AnswerFakeHerdr(opts)
        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Merge"], fake
        )
        self.assertEqual(code, 3)
        self.assertIn("ambiguous", json.loads(out)["reason"])

    def test_refuses_on_out_of_range_index(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS)
        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--index", "99"], fake
        )
        self.assertEqual(code, 3)
        self.assertIn("out of range", json.loads(out)["reason"])

    def test_refuses_on_scrollback_only_picker(self) -> None:
        def fake(argv: list[str]) -> dict[str, object]:
            if argv[0] == "agent" and argv[1] == "get":
                return {"result": {"agent": {"agent_status": "blocked", "pane_id": "w:p1"}}}
            if argv[0] == "pane" and argv[1] == "read":
                return {"result": {"text": "no picker here"}}
            return {"result": {}}

        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Stop here"], fake
        )
        self.assertEqual(code, 3)

    def test_other_text_lands_the_text(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS, selected=1)
        code, out, _ = self.run_answer(
            [
                "answer", "atk-w1", "--expect-pane", "w:p1", "--other",
                "--text", "Foreman review: read /x and act on it", "--settle-s", "0",
            ],
            fake,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"], "ok")
        self.assertEqual(
            fake.sent_text(),
            [["pane", "send-text", "w:p1", "Foreman review: read /x and act on it"]],
        )
        keys = fake.sent_keys()
        self.assertIn(["pane", "send-keys", "w:p1", "down", "down"], keys)
        self.assertTrue(any(c[-1] == "enter" for c in keys))

    def test_rejects_text_outside_contract(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS)
        code, _, err = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--other", "--text", "bad  double  spaces"],
            fake,
        )
        self.assertEqual(code, 2)
        self.assertIn("single spaces", err)

    def test_rejects_text_with_leading_space(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS)
        code, _, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--other", "--text", " leading"],
            fake,
        )
        self.assertEqual(code, 2)

    def test_reports_submitted_unconfirmed_when_picker_persists(self) -> None:
        fake = AnswerFakeHerdr(self.OPTIONS, selected=1)

        def never_clear(argv: list[str]) -> dict[str, object]:
            fake.calls.append(list(argv))
            if argv[0] == "agent" and argv[1] == "get":
                return {"result": {"agent": {"agent_status": "blocked", "pane_id": "w:p1"}}}
            if argv[0] == "pane" and argv[1] == "read":
                return {"result": {"text": _rehighlight(_base_selection(self.OPTIONS), 2)}}
            if argv[0] == "pane" and argv[1] == "send-keys":
                return {"result": {}}
            return {"result": {}}

        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Stop here", "--settle-s", "0"],
            never_clear,
        )
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out)["result"], "submitted_unconfirmed")

    def test_other_already_in_edit_mode_sends_text_without_renavigating(self) -> None:
        # A previous attempt left the worker in edit mode; answer must send the
        # new text directly rather than re-entering edit mode and submitting old
        # editor text.
        fake = AnswerFakeHerdr(self.OPTIONS, selected=3)
        fake.edit_mode = True
        code, out, _ = self.run_answer(
            [
                "answer", "atk-w1", "--expect-pane", "w:p1", "--other",
                "--text", "Foreman review: read /x and act on it", "--settle-s", "0",
            ],
            fake,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"], "ok")
        self.assertEqual(
            fake.sent_text(),
            [["pane", "send-text", "w:p1", "Foreman review: read /x and act on it"]],
        )
        # No down/up navigation: it was already parked on the OTHER option.
        self.assertFalse(any(c[-1] in ("down", "up") for c in fake.sent_keys()))

    def test_verification_failure_after_navigation_reports_input_sent(self) -> None:
        # The worker resumes (leaves blocked) after the arrows are sent: the
        # failure must be exit 4 with input_sent "navigation", not exit 3/none.
        opts = self.OPTIONS
        gets = {"n": 0}
        reads = {"n": 0}

        def fake(argv: list[str]) -> dict[str, object]:
            if argv[0] == "agent" and argv[1] == "get":
                gets["n"] += 1
                status = "blocked" if gets["n"] < 3 else "working"
                return {"result": {"agent": {"agent_status": status, "pane_id": "w:p1"}}}
            if argv[0] == "pane" and argv[1] == "read":
                reads["n"] += 1
                text = (
                    _rehighlight(_base_selection(opts), 2)
                    if reads["n"] == 3
                    else _base_selection(opts)
                )
                return {"result": {"text": text}}
            if argv[0] == "pane" and argv[1] == "send-keys":
                return {"result": {}}
            return {"result": {}}

        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Stop here", "--settle-s", "0"],
            fake,
        )
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["input_sent"], "navigation")

    def test_herdr_failure_after_input_is_exit_4_not_2(self) -> None:
        # A herdr failure on the submit Enter, after navigation was already
        # sent, must be exit 4 with input_sent "navigation" -- never exit 2.
        opts = self.OPTIONS
        reads = {"n": 0}

        def fake(argv: list[str]) -> dict[str, object]:
            if argv[0] == "agent" and argv[1] == "get":
                return {"result": {"agent": {"agent_status": "blocked", "pane_id": "w:p1"}}}
            if argv[0] == "pane" and argv[1] == "read":
                reads["n"] += 1
                text = (
                    _rehighlight(_base_selection(opts), 2)
                    if reads["n"] >= 3
                    else _base_selection(opts)
                )
                return {"result": {"text": text}}
            if argv[0] == "pane" and argv[1] == "send-keys" and "enter" in argv:
                raise herdr_delegate.RefusedError("send-keys refused")
            if argv[0] == "pane" and argv[1] == "send-keys":
                return {"result": {}}
            return {"result": {}}

        code, out, _ = self.run_answer(
            ["answer", "atk-w1", "--expect-pane", "w:p1", "--option", "Stop here", "--settle-s", "0"],
            fake,
        )
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["input_sent"], "navigation")


class HerdrEmptyStdoutTests(unittest.TestCase):
    """The real herdr() treats empty stdout + exit 0 on pane send-* as success."""

    def test_pane_send_keys_empty_stdout_is_success(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["herdr", "pane", "send-keys", "w:p1", "down"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(herdr_delegate.subprocess, "run", return_value=completed):
            result = herdr_delegate.herdr(["pane", "send-keys", "w:p1", "down"])
        self.assertEqual(result, {"result": {}})

    def test_pane_send_text_empty_stdout_is_success(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["herdr", "pane", "send-text", "w:p1", "x"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(herdr_delegate.subprocess, "run", return_value=completed):
            result = herdr_delegate.herdr(["pane", "send-text", "w:p1", "x"])
        self.assertEqual(result, {"result": {}})


class HandoffTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        sys.argv = ["herdr_delegate.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                herdr_delegate.main()
        except SystemExit as e:
            return int(e.code or 0), out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_handoff_rejects_swarm(self) -> None:
        code, _, err = self.run_main(
            ["launch", "--swarm", "3", "--prefix", "atk", "--handoff"]
        )
        self.assertEqual(code, 2)
        self.assertIn("--slug", err)

    def test_handoff_rejects_non_pi_kind(self) -> None:
        code, _, err = self.run_main(
            ["launch", "--slug", "atk-example", "--kind", "copilot", "--handoff"]
        )
        self.assertEqual(code, 2)
        self.assertIn("pi-only", err)

    def test_handoff_prompt_and_env(self) -> None:
        fake = AnswerFakeHerdr([], [])

        def fake_launch(argv: list[str]) -> dict[str, object]:
            if argv[0] == "tab" and argv[1] == "create":
                fake.calls.append(list(argv))
                return {"result": {"root_pane": {"pane_id": "w:pN"}, "tab": {"tab_id": "w:tN"}}}
            if argv[0] == "agent" and argv[1] == "start":
                return {"result": {}}
            if argv[0] == "agent" and argv[1] == "prompt":
                fake.calls.append(list(argv))
                return {"result": {}}
            if argv[0] == "pane" and argv[1] == "read":
                prompts = [
                    c[3] for c in fake.calls if c[0] == "agent" and c[1] == "prompt"
                ]
                return {"result": {"text": "\n".join(prompts)}}
            return {"result": {}}

        with (
            mock.patch.object(herdr_delegate, "herdr", fake_launch),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            code, out, _ = self.run_main(
                ["launch", "--slug", "atk-example", "--cwd", "/tmp", "--handoff"]
            )
        self.assertEqual(code, 0)
        create = next(c for c in fake.calls if c[0] == "tab" and c[1] == "create")
        self.assertIn("PI_AGENT_UNATTENDED=1", create)
        self.assertTrue(any(arg.startswith("PI_SWARM_CAPTURE_FILE=") for arg in create))
        prompt = next(c for c in fake.calls if c[0] == "agent" and c[1] == "prompt")
        self.assertEqual(prompt[3], "/backlog-item --auto atk-example --handoff")

    def test_handoff_accepts_agy_kind(self) -> None:
        fake = AnswerFakeHerdr([], [])

        def fake_launch(argv: list[str]) -> dict[str, object]:
            if argv[0] == "tab" and argv[1] == "create":
                fake.calls.append(list(argv))
                return {
                    "result": {
                        "root_pane": {"pane_id": "w:pN"},
                        "tab": {"tab_id": "w:tN"},
                    }
                }
            if argv[0] == "agent" and argv[1] == "start":
                return {"result": {}}
            if argv[0] == "agent" and argv[1] == "prompt":
                fake.calls.append(list(argv))
                return {"result": {}}
            if argv[0] == "pane" and argv[1] == "read":
                prompts = [
                    c[3] for c in fake.calls if c[0] == "agent" and c[1] == "prompt"
                ]
                return {"result": {"text": "\n".join(prompts)}}
            return {"result": {}}

        agy_dir = Path.home() / ".gemini" / "antigravity-cli"
        agy_dir.mkdir(parents=True, exist_ok=True)
        (agy_dir / "antigravity-oauth-token").write_text(
            json.dumps({"token": "fake"}), encoding="utf-8"
        )

        with (
            mock.patch.object(herdr_delegate, "herdr", fake_launch),
            mock.patch.object(
                herdr_delegate, "prepare_worker_worktree", return_value="/tmp/item"
            ),
            mock.patch.object(herdr_delegate.agy_workspace_trust, "prune_trusted"),
            mock.patch.object(herdr_delegate.agy_workspace_trust, "ensure_trusted"),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            code, out, _ = self.run_main(
                ["launch", "--slug", "atk-example", "--kind", "agy", "--handoff"]
            )
        self.assertEqual(code, 0)
        create = next(c for c in fake.calls if c[0] == "tab" and c[1] == "create")
        self.assertTrue(
            any(arg.startswith("PI_SWARM_CAPTURE_FILE=") for arg in create)
        )
        prompt = next(c for c in fake.calls if c[0] == "agent" and c[1] == "prompt")
        self.assertEqual(prompt[3], "/backlog-item --auto atk-example --handoff")

    def test_handoff_without_flag_omits_capture_env(self) -> None:
        argv = herdr_delegate.build_tab_argv(cwd="/tmp", label="x", kind="pi")
        self.assertNotIn("PI_SWARM_CAPTURE_FILE", " ".join(argv))

    def test_worker_prompt_handoff(self) -> None:
        self.assertEqual(
            herdr_delegate.worker_prompt("atk-example", handoff=True),
            "/backlog-item --auto atk-example --handoff",
        )
        self.assertEqual(
            herdr_delegate.worker_prompt("atk-example"),
            "/backlog-item --auto atk-example",
        )


class CloseFakeHerdr:
    """Fake herdr for `close`: scripted `agent get` / `tab get` reads (the last
    one repeats), and every `tab close` recorded instead of performed."""

    def __init__(
        self,
        agents: list[dict[str, object] | None],
        tabs: list[dict[str, object] | None],
    ) -> None:
        self.agents = list(agents)
        self.tabs = list(tabs)
        self.calls: list[list[str]] = []

    @staticmethod
    def _next(script: list[dict[str, object] | None]) -> dict[str, object] | None:
        return script.pop(0) if len(script) > 1 else script[0]

    def __call__(self, argv: list[str]) -> dict[str, object]:
        self.calls.append(list(argv))
        if argv[:2] == ["agent", "get"]:
            agent = self._next(self.agents)
            if agent is None:
                raise herdr_delegate.RefusedError(
                    "herdr agent get failed: agent_not_found", code="agent_not_found"
                )
            return {"result": {"agent": agent}}
        if argv[:2] == ["tab", "get"]:
            tab = self._next(self.tabs)
            if tab is None:
                raise herdr_delegate.RefusedError(
                    "herdr tab get failed: tab_not_found", code="tab_not_found"
                )
            return {"result": {"tab": tab}, "type": "tab_info"}
        return {"result": {}}

    def closes(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["tab", "close"]]


def _close_agent(
    status: str = "done", pane: str = "w:p7", tab: str | None = "w:t7"
) -> dict[str, object]:
    agent: dict[str, object] = {"agent_status": status, "pane_id": pane}
    if tab is not None:
        agent["tab_id"] = tab
    return agent


def _close_tab(label: str, pane_count: int = 1, tab: str = "w:t7") -> dict[str, object]:
    return {"tab_id": tab, "label": label, "pane_count": pane_count}


class CloseTests(unittest.TestCase):
    SLUG = "mvc2-m3-runtime-structs"

    def run_close(
        self,
        argv: list[str],
        fake: Callable,
        items: list[dict[str, object]] | list[list[dict[str, object]]],
    ) -> tuple[int, str, str]:
        """``items`` is one store snapshot, or a list of snapshots read in turn
        (the last repeats) so a test can change the item between reads."""
        snapshots = items if items and isinstance(items[0], list) else [items]
        reads = list(snapshots)

        def load() -> list[dict[str, object]]:
            return reads.pop(0) if len(reads) > 1 else reads[0]  # type: ignore[return-value]

        with (
            mock.patch.object(herdr_delegate, "herdr", fake),
            mock.patch.object(herdr_delegate, "load_backlog_items", load),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            sys.argv = ["herdr_delegate.py", *argv]
            out, err = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    herdr_delegate.main()
            except SystemExit as e:
                return int(e.code or 0), out.getvalue(), err.getvalue()
            return 0, out.getvalue(), err.getvalue()

    def done_items(self, slug: str | None = None) -> list[dict[str, object]]:
        return [{"id": slug or self.SLUG, "status": "done"}]

    def test_closes_a_done_workers_exclusive_tab(self) -> None:
        fake = CloseFakeHerdr([_close_agent()], [_close_tab(self.SLUG)])
        code, out, err = self.run_close(
            ["close", self.SLUG, "--expect-pane", "w:p7"], fake, self.done_items()
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.closes(), [["tab", "close", "w:t7"]])
        payload = json.loads(out)
        self.assertEqual(payload["closed_tab"], "w:t7")
        self.assertEqual(payload["item"], self.SLUG)
        self.assertEqual(payload["item_status"], "done")
        self.assertFalse(payload["forced"])

    def test_idle_agent_is_closable(self) -> None:
        fake = CloseFakeHerdr([_close_agent("idle")], [_close_tab(self.SLUG)])
        code, _, err = self.run_close(
            ["close", self.SLUG, "--expect-pane", "w:p7"], fake, self.done_items()
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.closes()), 1)

    def test_long_slug_matches_truncated_agent_name_and_full_label(self) -> None:
        slug = "atk-a-really-long-backlog-item-slug-past-32-chars"
        agent = herdr_delegate.agent_name_for(slug)
        self.assertNotEqual(agent, slug)
        fake = CloseFakeHerdr([_close_agent()], [_close_tab(slug)])
        code, out, err = self.run_close(
            ["close", agent, "--expect-pane", "w:p7"], fake, self.done_items(slug)
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["item"], slug)

    def assert_refused(
        self,
        fake: CloseFakeHerdr,
        items: list[dict[str, object]] | list[list[dict[str, object]]],
        argv: list[str] | None = None,
    ) -> str:
        code, _, err = self.run_close(
            argv or ["close", self.SLUG, "--expect-pane", "w:p7"], fake, items
        )
        self.assertEqual(code, 1)
        self.assertEqual(fake.closes(), [])
        return err

    def test_refuses_item_not_done(self) -> None:
        fake = CloseFakeHerdr([_close_agent()], [_close_tab(self.SLUG)])
        err = self.assert_refused(fake, [{"id": self.SLUG, "status": "in-review"}])
        self.assertIn("in-review", err)

    def test_refuses_working_blocked_and_unknown_status(self) -> None:
        for status in ("working", "blocked", "", "mystery"):
            with self.subTest(status=status):
                fake = CloseFakeHerdr([_close_agent(status)], [_close_tab(self.SLUG)])
                self.assert_refused(fake, self.done_items())

    def test_refuses_pane_mismatch_and_empty_expected_pane(self) -> None:
        fake = CloseFakeHerdr([_close_agent(pane="w:p9")], [_close_tab(self.SLUG)])
        self.assert_refused(fake, self.done_items())
        fake = CloseFakeHerdr([_close_agent(pane="")], [_close_tab(self.SLUG)])
        self.assert_refused(
            fake, self.done_items(), ["close", self.SLUG, "--expect-pane", ""]
        )

    def test_refuses_missing_agent_and_missing_tab_id(self) -> None:
        self.assert_refused(CloseFakeHerdr([None], [_close_tab(self.SLUG)]), self.done_items())
        self.assert_refused(
            CloseFakeHerdr([_close_agent(tab=None)], [_close_tab(self.SLUG)]),
            self.done_items(),
        )

    def test_refuses_no_match_and_truncation_collision(self) -> None:
        fake = CloseFakeHerdr([_close_agent()], [_close_tab(self.SLUG)])
        self.assert_refused(fake, [{"id": "mvc2-something-else", "status": "done"}])
        a = "atk-a-really-long-backlog-item-slug-AAA"
        b = "atk-a-really-long-backlog-item-slug-BBB"
        agent = herdr_delegate.agent_name_for(a)
        self.assertEqual(agent, herdr_delegate.agent_name_for(b))
        fake = CloseFakeHerdr([_close_agent()], [_close_tab(a)])
        self.assert_refused(
            fake,
            [{"id": a, "status": "done"}, {"id": b, "status": "done"}],
            ["close", agent, "--expect-pane", "w:p7"],
        )

    def test_refuses_shared_tab_or_foreign_label(self) -> None:
        self.assert_refused(
            CloseFakeHerdr([_close_agent()], [_close_tab(self.SLUG, pane_count=2)]),
            self.done_items(),
        )
        self.assert_refused(
            CloseFakeHerdr([_close_agent()], [_close_tab("swarm-mvc2")]),
            self.done_items(),
        )

    def test_revalidation_refuses_any_change_between_reads(self) -> None:
        cases = {
            "status": ([_close_agent(), _close_agent("working")], [_close_tab(self.SLUG)]),
            "tab id": ([_close_agent(), _close_agent(tab="w:t8")], [_close_tab(self.SLUG)]),
            "agent gone": ([_close_agent(), None], [_close_tab(self.SLUG)]),
            "pane count": ([_close_agent()], [_close_tab(self.SLUG), _close_tab(self.SLUG, 2)]),
            "label": ([_close_agent()], [_close_tab(self.SLUG), _close_tab("other")]),
            "tab gone": ([_close_agent()], [_close_tab(self.SLUG), None]),
        }
        for name, (agents, tabs) in cases.items():
            with self.subTest(change=name):
                self.assert_refused(CloseFakeHerdr(agents, tabs), self.done_items())
        with self.subTest(change="item reopened"):
            self.assert_refused(
                CloseFakeHerdr([_close_agent()], [_close_tab(self.SLUG)]),
                [self.done_items(), [{"id": self.SLUG, "status": "open"}]],
            )

    def test_force_bypasses_item_and_status_but_not_identity(self) -> None:
        fake = CloseFakeHerdr([_close_agent("working")], [_close_tab(self.SLUG)])
        code, out, err = self.run_close(
            ["close", self.SLUG, "--expect-pane", "w:p7", "--force"],
            fake,
            [{"id": self.SLUG, "status": "open"}],
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["forced"])
        self.assertEqual(json.loads(out)["item_status"], "open")
        # No resolvable item: item is null, label not checked, still closes.
        fake = CloseFakeHerdr([_close_agent()], [_close_tab("anything")])
        code, out, err = self.run_close(
            ["close", self.SLUG, "--expect-pane", "w:p7", "--force"], fake, []
        )
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)["item"])
        # A status change between reads is waived under --force; a tab change is not.
        fake = CloseFakeHerdr(
            [_close_agent("idle"), _close_agent("working")], [_close_tab(self.SLUG)]
        )
        code, _, err = self.run_close(
            ["close", self.SLUG, "--expect-pane", "w:p7", "--force"],
            fake,
            self.done_items(),
        )
        self.assertEqual(code, 0, err)
        self.assert_refused(
            CloseFakeHerdr(
                [_close_agent(), _close_agent(tab="w:t8")], [_close_tab(self.SLUG)]
            ),
            self.done_items(),
            ["close", self.SLUG, "--expect-pane", "w:p7", "--force"],
        )
        # Identity and exclusivity still hold under --force.
        self.assert_refused(
            CloseFakeHerdr([_close_agent(pane="w:p9")], [_close_tab(self.SLUG)]),
            self.done_items(),
            ["close", self.SLUG, "--expect-pane", "w:p7", "--force"],
        )
        self.assert_refused(
            CloseFakeHerdr([_close_agent()], [_close_tab(self.SLUG, pane_count=2)]),
            self.done_items(),
            ["close", self.SLUG, "--expect-pane", "w:p7", "--force"],
        )

    def test_parse_agent_get_without_tab_id_still_parses_for_answer(self) -> None:
        parsed = herdr_delegate.parse_agent_get(
            {"result": {"agent": {"agent_status": "blocked", "pane_id": "w:p1"}}}
        )
        self.assertEqual(parsed, {"status": "blocked", "pane": "w:p1", "tab": ""})


# A fixed "now": 2026-10-15 15:00 local; UTC keeps the day boundary simple.
COST_NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


def _pi_agent(
    name: str, status: str, seq: int, session: str | None
) -> dict[str, object]:
    rec = _agent(name, status, seq)
    if session is not None:
        rec["agent_session"] = {"agent": "pi", "kind": "path", "value": session}
    return rec


def _assistant(cost: object, ts: str = "2026-10-15T12:00:00Z") -> str:
    return json.dumps(
        {
            "type": "message",
            "timestamp": ts,
            "message": {"role": "assistant", "usage": {"cost": {"total": cost}}},
        }
    )


class WatchCostTests(unittest.TestCase):
    """`watch --cost-cap-*`: pi cost alerts with snapshot-bound acks."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "watch.json"
        self.sessions = Path.home() / ".pi" / "agent" / "sessions"
        self.now = COST_NOW
        env = mock.patch.dict(os.environ, {"TZ": "UTC"})
        env.start()
        time.tzset()
        self.addCleanup(env.stop)
        self.addCleanup(time.tzset)
        self.addCleanup(shutil.rmtree, self.sessions, True)

    def session(self, name: str, costs: list[float], *, tail: str = "\n") -> str:
        path = self.sessions / "--proj--" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(_assistant(c) for c in costs) + tail)
        stamp = self.now.timestamp()
        os.utime(path, (stamp, stamp))
        return str(path)

    def run_watch(
        self, argv: list[str], agents: list[dict[str, object]]
    ) -> tuple[int, dict[str, object], str]:
        fake = WatchFakeHerdr([agents])
        with (
            mock.patch.object(herdr_delegate, "herdr", fake),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.object(
                herdr_delegate.time, "time", lambda: self.now.timestamp()
            ),
            mock.patch.object(herdr_delegate, "_watch_now", lambda: self.now),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            sys.argv = [
                "herdr_delegate.py",
                "watch",
                *argv,
                "--state",
                str(self.state),
                "--timeout-s",
                "0",
            ]
            out, err = io.StringIO(), io.StringIO()
            code = 0
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    herdr_delegate.main()
            except SystemExit as e:
                code = int(e.code or 0)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else {}), err.getvalue()

    # ── per-agent caps ──────────────────────────────────────────────────────

    def test_agent_over_cap_alerts_and_ack_snoozes_one_step(self) -> None:
        s = self.session("w1", [2.0, 2.5])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        code, out, _ = self.run_watch(["atk-w1", "--cost-cap-agent", "4"], agents)
        self.assertEqual(code, 0)
        self.assertEqual(out["attention"], {})
        alert = out["cost"]["alerts"]["agent:atk-w1"]
        self.assertEqual((alert["reason"], alert["usd"]), ("over_cap", 4.5))
        self.assertEqual(out["cost"]["agents"]["atk-w1"]["usd"], 4.5)
        self.assertIn("not provider billing", out["cost"]["estimate"])

        # Level-triggered: still reported until acked.
        code, out2, _ = self.run_watch(["atk-w1", "--cost-cap-agent", "4"], agents)
        self.assertEqual(out2["cost"]["alerts"]["agent:atk-w1"]["ack"], alert["ack"])

        # Acked: quiet while spend stays below 4.5 + step.
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4", "--ack-cost", alert["ack"]], agents
        )
        self.assertEqual(code, 124)
        self.assertEqual(out["cost"]["alerts"], {})

        self.session("w1", [2.0, 2.5, 0.9])
        code, out, _ = self.run_watch(["atk-w1", "--cost-cap-agent", "4"], agents)
        self.assertEqual(code, 124)

        self.session("w1", [2.0, 2.5, 1.0])
        code, out, _ = self.run_watch(["atk-w1", "--cost-cap-agent", "4"], agents)
        self.assertEqual(code, 0)
        self.assertEqual(out["cost"]["alerts"]["agent:atk-w1"]["usd"], 5.5)

    def test_stale_ack_cannot_swallow_later_spend(self) -> None:
        s = self.session("w1", [4.0])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        _, out, _ = self.run_watch(["atk-w1", "--cost-cap-agent", "4"], agents)
        token = out["cost"]["alerts"]["agent:atk-w1"]["ack"]
        # Spend rose past token usd + step before the foreman acked.
        self.session("w1", [4.0, 1.5])
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4", "--ack-cost", token], agents
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["cost"]["alerts"]["agent:atk-w1"]["usd"], 5.5)

    def test_new_session_under_same_name_re_alerts(self) -> None:
        s1 = self.session("w1", [5.0])
        _, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"], [_pi_agent("atk-w1", "working", 3, s1)]
        )
        token = out["cost"]["alerts"]["agent:atk-w1"]["ack"]
        s2 = self.session("w1-restart", [5.0])
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4", "--ack-cost", token],
            [_pi_agent("atk-w1", "working", 4, s2)],
        )
        self.assertEqual(code, 0)
        self.assertIn("agent:atk-w1", out["cost"]["alerts"])

    def test_cost_exactly_at_cap_counts(self) -> None:
        s = self.session("w1", [4.0])
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"], [_pi_agent("atk-w1", "working", 3, s)]
        )
        self.assertEqual(out["cost"]["alerts"]["agent:atk-w1"]["reason"], "over_cap")

    # ── fail closed ─────────────────────────────────────────────────────────

    def test_agent_without_session_path_is_unmeasured(self) -> None:
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"], [_agent("atk-w1", "working", 3)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out["cost"]["alerts"]["agent:atk-w1"]["reason"], "unmeasured"
        )
        self.assertIsNone(out["cost"]["agents"]["atk-w1"]["session"])

    def test_interior_corruption_is_unmeasured_but_torn_tail_is_not(self) -> None:
        torn = self.session("torn", [1.0], tail='\n{"type": "mess')
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"], [_pi_agent("atk-w1", "working", 3, torn)]
        )
        self.assertEqual(code, 124)
        self.assertTrue(out["cost"]["agents"]["atk-w1"]["complete"])

        bad = self.sessions / "--proj--" / "bad.jsonl"
        bad.write_text("garbage\n" + _assistant(1.0) + "\n")
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"],
            [_pi_agent("atk-w1", "working", 3, str(bad))],
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out["cost"]["alerts"]["agent:atk-w1"]["reason"], "unmeasured"
        )

    def test_known_overspend_beats_incompleteness(self) -> None:
        bad = self.sessions / "--proj--" / "bad.jsonl"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("garbage\n" + _assistant(9.0) + "\n")
        _, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"],
            [_pi_agent("atk-w1", "working", 3, str(bad))],
        )
        alert = out["cost"]["alerts"]["agent:atk-w1"]
        self.assertEqual((alert["reason"], alert["usd"]), ("over_cap", 9.0))

    def test_acked_incomplete_over_cap_still_gets_reminders(self) -> None:
        bad = self.sessions / "--proj--" / "bad.jsonl"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("garbage\n" + _assistant(9.0) + "\n")
        agents = [_pi_agent("atk-w1", "working", 3, str(bad))]
        argv = ["atk-w1", "--cost-cap-agent", "4", "--cost-remind-s", "600"]
        _, out, _ = self.run_watch(argv, agents)
        token = out["cost"]["alerts"]["agent:atk-w1"]["ack"]
        self.assertEqual(self.run_watch([*argv, "--ack-cost", token], agents)[0], 124)
        self.now = COST_NOW + timedelta(seconds=600)
        code, out, _ = self.run_watch(argv, agents)
        self.assertEqual(code, 0)
        self.assertEqual(out["cost"]["alerts"]["agent:atk-w1"]["reason"], "over_cap")

    def test_acked_unmeasured_re_alerts_after_remind_or_on_spend(self) -> None:
        bad = self.sessions / "--proj--" / "bad.jsonl"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("garbage\n" + _assistant(0.5) + "\n")
        agents = [_pi_agent("atk-w1", "working", 3, str(bad))]
        argv = ["atk-w1", "--cost-cap-agent", "4", "--cost-remind-s", "600"]
        _, out, _ = self.run_watch(argv, agents)
        token = out["cost"]["alerts"]["agent:atk-w1"]["ack"]
        code, out, _ = self.run_watch([*argv, "--ack-cost", token], agents)
        self.assertEqual(code, 124)

        # Unobservable spend: the measurement never moves, the reminder fires.
        self.now = COST_NOW + timedelta(seconds=599)
        self.assertEqual(self.run_watch(argv, agents)[0], 124)
        self.now = COST_NOW + timedelta(seconds=600)
        code, out, _ = self.run_watch(argv, agents)
        self.assertEqual(code, 0)
        self.assertEqual(
            out["cost"]["alerts"]["agent:atk-w1"]["reason"], "unmeasured"
        )

        # Acking unmeasured never suppresses a later over_cap.
        self.now = COST_NOW
        bad.write_text("garbage\n" + _assistant(4.0) + "\n")
        _, out, _ = self.run_watch(argv, agents)
        self.assertEqual(out["cost"]["alerts"]["agent:atk-w1"]["reason"], "over_cap")

    def test_overflow_is_json_safe_and_ackable_by_reminder(self) -> None:
        s = self.session("w1", [1.7e308, 1.7e308])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        argv = ["atk-w1", "--cost-cap-agent", "4", "--cost-remind-s", "600"]
        code, out, _ = self.run_watch(argv, agents)  # json.loads would choke on Infinity
        alert = out["cost"]["alerts"]["agent:atk-w1"]
        self.assertEqual((alert["reason"], alert["usd"]), ("over_cap", None))
        code, out, _ = self.run_watch([*argv, "--ack-cost", alert["ack"]], agents)
        self.assertEqual(code, 124)
        self.now = COST_NOW + timedelta(seconds=600)
        self.assertEqual(self.run_watch(argv, agents)[0], 0)

    # ── daily total ─────────────────────────────────────────────────────────

    def test_total_counts_unwatched_sessions_and_rearms_next_day(self) -> None:
        mine = self.session("w1", [1.0])
        self.session("someone-else", [3.0])
        agents = [_pi_agent("atk-w1", "working", 3, mine)]
        code, out, _ = self.run_watch(["atk-w1", "--cost-cap-total", "4"], agents)
        self.assertEqual(code, 0)
        self.assertEqual(out["cost"]["total"]["usd"], 4.0)
        self.assertEqual(out["cost"]["agents"], {})  # no agent cap set
        alert = out["cost"]["alerts"]["total"]
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-total", "4", "--ack-cost", alert["ack"]], agents
        )
        self.assertEqual(code, 124)

        # Next day: yesterday's messages fall out of the window entirely.
        self.now = COST_NOW + timedelta(days=1)
        code, out, _ = self.run_watch(["atk-w1", "--cost-cap-total", "4"], agents)
        self.assertEqual(code, 124)
        self.assertEqual(out["cost"]["day"], "2026-10-16")
        self.assertEqual(out["cost"]["total"]["usd"], 0.0)

    def test_total_ack_does_not_carry_into_a_new_day(self) -> None:
        self.session("a", [5.0])
        agents = [_agent("atk-w1", "working", 3)]
        _, out, _ = self.run_watch(["atk-w1", "--cost-cap-total", "4"], agents)
        token = out["cost"]["alerts"]["total"]["ack"]
        self.run_watch(["atk-w1", "--cost-cap-total", "4", "--ack-cost", token], agents)
        self.now = COST_NOW + timedelta(days=1)
        path = self.sessions / "--proj--" / "a.jsonl"
        path.write_text(_assistant(5.0, ts="2026-10-16T12:00:00Z") + "\n")
        os.utime(path, (self.now.timestamp(), self.now.timestamp()))
        code, out, _ = self.run_watch(["atk-w1", "--cost-cap-total", "4"], agents)
        self.assertEqual(code, 0)
        self.assertEqual(out["cost"]["alerts"]["total"]["reason"], "over_cap")

    def test_incomplete_total_alerts(self) -> None:
        path = self.sessions / "--proj--" / "x.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage\n")
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-total", "4"], [_agent("atk-w1", "working", 3)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["cost"]["alerts"]["total"]["reason"], "incomplete")
        self.assertFalse(out["cost"]["total"]["complete"])

    def test_agent_named_total_does_not_collide(self) -> None:
        s = self.session("w1", [5.0])
        _, out, _ = self.run_watch(
            ["total", "--cost-cap-total", "4", "--cost-cap-agent", "4"],
            [_pi_agent("total", "working", 3, s)],
        )
        self.assertEqual(set(out["cost"]["alerts"]), {"total", "agent:total"})
        token = out["cost"]["alerts"]["agent:total"]["ack"]
        code, out, _ = self.run_watch(
            ["total", "--cost-cap-total", "4", "--cost-cap-agent", "4",
             "--ack-cost", token],
            [_pi_agent("total", "working", 3, s)],
        )
        self.assertEqual(set(out["cost"]["alerts"]), {"total"})

    def test_local_midnight_across_dst_transitions(self) -> None:
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            try:
                spring = herdr_delegate._local_midnight(
                    datetime(2026, 3, 8, 12, 0, tzinfo=UTC).astimezone()
                )
                fall = herdr_delegate._local_midnight(
                    datetime(2026, 11, 1, 18, 0, tzinfo=UTC).astimezone()
                )
            finally:
                os.environ["TZ"] = "UTC"
                time.tzset()
        # Midnight before each switch is still on the old offset.
        self.assertEqual(spring.astimezone(UTC), datetime(2026, 3, 8, 5, 0, tzinfo=UTC))
        self.assertEqual(fall.astimezone(UTC), datetime(2026, 11, 1, 4, 0, tzinfo=UTC))

    # ── compatibility and input validation ──────────────────────────────────

    def test_no_cap_flags_means_no_cost_key_and_no_new_state(self) -> None:
        s = self.session("w1", [50.0])
        code, out, _ = self.run_watch(["atk-w1"], [_pi_agent("atk-w1", "working", 3, s)])
        self.assertEqual(code, 124)
        self.assertNotIn("cost", out)
        self.assertNotIn("cost_acks", json.loads(self.state.read_text()))

    def test_settle_and_cost_alert_share_one_report(self) -> None:
        s = self.session("w1", [5.0])
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"], [_pi_agent("atk-w1", "blocked", 7, s)]
        )
        self.assertEqual(out["attention"]["atk-w1"]["seq"], 7)
        self.assertIn("agent:atk-w1", out["cost"]["alerts"])

    def test_cost_acks_round_trip_and_old_state_loads(self) -> None:
        self.state.write_text('{"agents": {}, "membership": [], "pending_gone": []}')
        s = self.session("w1", [5.0])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        _, out, _ = self.run_watch(["atk-w1", "--cost-cap-agent", "4"], agents)
        token = out["cost"]["alerts"]["agent:atk-w1"]["ack"]
        self.run_watch(["atk-w1", "--cost-cap-agent", "4", "--ack-cost", token], agents)
        saved = json.loads(self.state.read_text())
        self.assertIn("agent:atk-w1", saved["cost_acks"])
        loaded = herdr_delegate._load_watch_state(self.state)
        self.assertEqual(loaded["cost_acks"], saved["cost_acks"])
        # The ack persists across runs.
        self.assertEqual(self.run_watch(["atk-w1", "--cost-cap-agent", "4"], agents)[0], 124)

    def test_corrupt_cost_acks_state_is_refused(self) -> None:
        self.state.write_text(
            '{"agents": {}, "membership": [], "pending_gone": [], "cost_acks": []}'
        )
        code, _, err = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4"], [_agent("atk-w1", "working", 3)]
        )
        self.assertEqual(code, 2)
        self.assertIn("cost_acks", err)

    def test_bad_cli_values_are_refused(self) -> None:
        cases = [
            ["--cost-cap-agent", "nan"],
            ["--cost-cap-agent", "inf"],
            ["--cost-cap-total", "0"],
            ["--cost-cap-total", "-1"],
            ["--cost-cap-agent", "4", "--cost-step", "0"],
            ["--cost-cap-agent", "4", "--cost-remind-s", "nan"],
            ["--ack-cost", "cost:total:over_cap:5.0:-:-:2026-10-15"],
            ["--cost-cap-total", "4", "--ack-cost", "cost:agent:x:over_cap:5.0:-:-:d"],
            ["--cost-cap-total", "4", "--ack-cost", "nonsense"],
            ["--cost-cap-total", "4", "--ack-cost", "cost:total:bogus:5.0:-:-:d"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                code, _, _ = self.run_watch(
                    ["atk-w1", *extra], [_agent("atk-w1", "working", 3)]
                )
                self.assertEqual(code, 2)


def _agy_agent(
    name: str, status: str, seq: int, db_path: Path | str | None
) -> dict[str, object]:
    rec = _agent(name, status, seq)
    rec["agent"] = "agy"
    if db_path is not None:
        rec["agent_session"] = {
            "agent": "agy",
            "source": "herdr:antigravity_cli",
            "value": str(db_path),
        }
    return rec


def _make_req_proto(req_id: str) -> bytes:
    val = req_id.encode("utf-8")
    f11 = bytes([(11 << 3) | 2, len(val)]) + val
    f4 = bytes([(4 << 3) | 2, len(f11)]) + f11
    f1 = bytes([(1 << 3) | 2, len(f4)]) + f4
    return f1


class WatchAgyCallsTests(unittest.TestCase):
    """`watch --call-cap-*` and `--elapsed-cap-*`: agy call count and fallback elapsed caps."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "watch.json"
        self.dbs = self.tmp / "dbs"
        self.dbs.mkdir(parents=True, exist_ok=True)
        self.now = COST_NOW
        env = mock.patch.dict(os.environ, {"TZ": "UTC"})
        env.start()
        time.tzset()
        self.addCleanup(env.stop)
        self.addCleanup(time.tzset)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def db(self, name: str, req_ids: list[str | bytes]) -> Path:
        path = self.dbs / f"{name}.db"
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS gen_metadata (idx integer primary key, data blob)"
        )
        cur.execute("DELETE FROM gen_metadata")
        for idx, item in enumerate(req_ids, 1):
            blob = item if isinstance(item, bytes) else _make_req_proto(item)
            cur.execute(
                "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (idx, blob)
            )
        con.commit()
        con.close()
        return path

    def run_watch(
        self, argv: list[str], agents: list[dict[str, object]]
    ) -> tuple[int, dict[str, object], str]:
        fake = WatchFakeHerdr([agents])
        with (
            mock.patch.object(herdr_delegate, "herdr", fake),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.object(
                herdr_delegate.time, "time", lambda: self.now.timestamp()
            ),
            mock.patch.object(herdr_delegate, "_watch_now", lambda: self.now),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            sys.argv = [
                "herdr_delegate.py",
                "watch",
                *argv,
                "--state",
                str(self.state),
                "--timeout-s",
                "0",
            ]
            out, err = io.StringIO(), io.StringIO()
            code = 0
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    herdr_delegate.main()
            except SystemExit as e:
                code = int(e.code or 0)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else {}), err.getvalue()

    def test_agent_over_call_cap_alerts_and_ack_snoozes(self) -> None:
        db_path = self.db("w1", ["req-1", "req-2", "req-3", "req-4", "req-5"])
        agents = [_agy_agent("atk-w1", "working", 3, db_path)]
        code, out, _ = self.run_watch(["atk-w1", "--call-cap-agent", "4"], agents)
        self.assertEqual(code, 0)
        alert = out["calls"]["alerts"]["agent:atk-w1"]
        self.assertEqual(
            (alert["reason"], alert["calls"], alert["unit"]),
            ("over_cap", 5, "calls"),
        )
        token = alert["ack"]
        self.assertTrue(token.startswith("call:agent:atk-w1:over_cap:5:"))

        # Level-triggered: still reported until acked
        code, out2, _ = self.run_watch(["atk-w1", "--call-cap-agent", "4"], agents)
        self.assertEqual(out2["calls"]["alerts"]["agent:atk-w1"]["ack"], token)

        # Acked: quiet while call count stays at 5
        code, out, _ = self.run_watch(
            ["atk-w1", "--call-cap-agent", "4", "--ack-cost", token], agents
        )
        self.assertEqual(code, 124)
        self.assertEqual(out["calls"]["alerts"], {})

        # New call added (total 6): re-alerts!
        self.db("w1", ["req-1", "req-2", "req-3", "req-4", "req-5", "req-6"])
        code, out, _ = self.run_watch(
            ["atk-w1", "--call-cap-agent", "4", "--ack-cost", token], agents
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["calls"]["alerts"]["agent:atk-w1"]["calls"], 6)

    def test_agent_unmeasured_when_db_missing(self) -> None:
        missing_db = self.dbs / "missing.db"
        agents = [_agy_agent("atk-w1", "working", 3, missing_db)]
        code, out, _ = self.run_watch(["atk-w1", "--call-cap-agent", "4"], agents)
        self.assertEqual(code, 0)
        alert = out["calls"]["alerts"]["agent:atk-w1"]
        self.assertEqual((alert["reason"], alert["unit"]), ("unmeasured", "calls"))

    def test_agent_incomplete_when_db_corrupt_row(self) -> None:
        db_path = self.db("w1", ["req-1", b"not-a-proto"])
        agents = [_agy_agent("atk-w1", "working", 3, db_path)]
        code, out, _ = self.run_watch(["atk-w1", "--call-cap-agent", "4"], agents)
        self.assertEqual(code, 0)
        alert = out["calls"]["alerts"]["agent:atk-w1"]
        self.assertEqual((alert["reason"], alert["calls"]), ("incomplete", 1))

    def test_elapsed_cap_fallback_only_on_unreadable_db(self) -> None:
        missing_db = self.dbs / "missing.db"
        agents = [_agy_agent("atk-w1", "working", 3, missing_db)]

        # First run records first_seen_at in state
        self.run_watch(
            ["atk-w1", "--call-cap-agent", "4", "--elapsed-cap-agent", "10"], agents
        )

        # Advance time by 15 seconds
        self.now = self.now + timedelta(seconds=15)
        code, out, _ = self.run_watch(
            ["atk-w1", "--call-cap-agent", "4", "--elapsed-cap-agent", "10"], agents
        )
        self.assertEqual(code, 0)
        self.assertIn("elapsed", out)
        elapsed_alert = out["elapsed"]["alerts"]["agent:atk-w1"]
        self.assertEqual(
            (elapsed_alert["reason"], elapsed_alert["unit"]),
            ("over_cap", "seconds"),
        )
        self.assertGreaterEqual(elapsed_alert["seconds"], 10.0)

        # If DB is readable but incomplete, elapsed cap does NOT fire as fallback
        db_path = self.db("w2", ["req-1", b"corrupt"])
        agents2 = [_agy_agent("atk-w2", "working", 3, db_path)]
        self.run_watch(
            ["atk-w2", "--call-cap-agent", "4", "--elapsed-cap-agent", "10"], agents2
        )
        self.now = self.now + timedelta(seconds=15)
        code, out, _ = self.run_watch(
            ["atk-w2", "--call-cap-agent", "4", "--elapsed-cap-agent", "10"], agents2
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out["calls"]["alerts"]["agent:atk-w2"]["reason"], "incomplete"
        )
        self.assertEqual(out.get("elapsed", {}).get("alerts", {}), {})

    def test_total_call_cap_deduplicates_and_alerts(self) -> None:
        db1 = self.db("w1", ["req-1", "req-2"])
        db2 = self.db("w2", ["req-2", "req-3"])
        agents = [
            _agy_agent("atk-w1", "working", 3, db1),
            _agy_agent("atk-w2", "working", 3, db2),
        ]
        code, out, _ = self.run_watch(
            ["atk-w1", "atk-w2", "--call-cap-total", "3"], agents
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["calls"]["total"]["calls"], 3)
        self.assertIn("total", out["calls"]["alerts"])
        self.assertEqual(out["calls"]["alerts"]["total"]["reason"], "over_cap")

    def test_invalid_cli_arguments_refused(self) -> None:
        cases = [
            ["--call-cap-agent", "0"],
            ["--call-cap-agent", "-1"],
            ["--call-cap-agent", "abc"],
            ["--call-cap-total", "0"],
            ["--elapsed-cap-agent", "0"],
            ["--elapsed-cap-agent", "-5"],
            ["--elapsed-cap-total", "inf"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                code, _, _ = self.run_watch(
                    ["atk-w1", *extra], [_agy_agent("atk-w1", "working", 3, None)]
                )
                self.assertEqual(code, 2)


class WatchPlanningTests(unittest.TestCase):
    """`watch --planning-note-usd`: report-only planning checkpoint notes."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "watch.json"
        self.sessions = Path.home() / ".pi" / "agent" / "sessions"
        self.now = COST_NOW
        env = mock.patch.dict(os.environ, {"TZ": "UTC"})
        env.start()
        time.tzset()
        self.addCleanup(env.stop)
        self.addCleanup(time.tzset)
        self.addCleanup(shutil.rmtree, self.sessions, True)

    @staticmethod
    def _msg(cost: object, write: str | None = None) -> str:
        content: list[dict[str, object]] = [{"type": "text", "text": "doing"}]
        if write is not None:
            content.append(
                {"type": "toolCall", "name": "write", "arguments": {"path": write}}
            )
        return json.dumps(
            {
                "type": "message",
                "timestamp": "2026-10-15T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "usage": {"cost": {"total": cost}},
                },
            }
        )

    def session(self, name: str, events: list[tuple[float, str | None]]) -> str:
        path = self.sessions / "--proj--" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self._msg(c, w) for c, w in events) + "\n")
        stamp = self.now.timestamp()
        os.utime(path, (stamp, stamp))
        return str(path)

    def run_watch(
        self, argv: list[str], agents: list[dict[str, object]]
    ) -> tuple[int, dict[str, object], str]:
        fake = WatchFakeHerdr([agents])
        with (
            mock.patch.object(herdr_delegate, "herdr", fake),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.object(
                herdr_delegate.time, "time", lambda: self.now.timestamp()
            ),
            mock.patch.object(herdr_delegate, "_watch_now", lambda: self.now),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            sys.argv = [
                "herdr_delegate.py",
                "watch",
                *argv,
                "--state",
                str(self.state),
                "--timeout-s",
                "0",
            ]
            out, err = io.StringIO(), io.StringIO()
            code = 0
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    herdr_delegate.main()
            except SystemExit as e:
                code = int(e.code or 0)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else {}), err.getvalue()

    def test_note_fires_past_threshold_with_no_write(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 0)
        alert = out["planning"]["alerts"]["agent:atk-w1"]
        self.assertEqual(alert["usd"], 4.5)
        self.assertTrue(alert["ack"].startswith("planning:agent:atk-w1:no-write:"))
        self.assertEqual(out["planning"]["agents"]["atk-w1"]["first_write"], None)
        self.assertEqual(out["planning"]["threshold"], 4.0)

    def test_no_note_once_a_file_is_written(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, "src/a.py")])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 124)
        self.assertEqual(out["planning"]["alerts"], {})
        self.assertEqual(out["planning"]["agents"]["atk-w1"]["first_write"], "src/a.py")

    def test_no_note_under_threshold(self) -> None:
        s = self.session("w1", [(0.2, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 124)
        self.assertEqual(out["planning"]["alerts"], {})

    def test_level_triggered_until_acked_then_fires_once(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        _, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        token = out["planning"]["alerts"]["agent:atk-w1"]["ack"]

        # Unacknowledged: re-fires on every poll.
        code, out2, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 0)
        self.assertEqual(out2["planning"]["alerts"]["agent:atk-w1"]["ack"], token)

        # Acked: quiet for the same session.
        code, out, _ = self.run_watch(
            ["atk-w1", "--planning-note-usd", "4", "--ack-cost", token], agents
        )
        self.assertEqual(code, 124)
        self.assertEqual(out["planning"]["alerts"], {})

    def test_acked_spend_growth_does_not_refire(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        _, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        token = out["planning"]["alerts"]["agent:atk-w1"]["ack"]
        self.run_watch(
            ["atk-w1", "--planning-note-usd", "4", "--ack-cost", token], agents
        )
        # Spend rises past any cost-style step; the note stays acked.
        self.session("w1", [(2.0, None), (2.5, None), (10.0, None)])
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 124)
        self.assertEqual(out["planning"]["alerts"], {})

    def test_new_session_under_same_name_refires(self) -> None:
        s1 = self.session("w1", [(2.0, None), (2.5, None)])
        _, out, _ = self.run_watch(
            ["atk-w1", "--planning-note-usd", "4"], [_pi_agent("atk-w1", "working", 3, s1)]
        )
        token = out["planning"]["alerts"]["agent:atk-w1"]["ack"]
        s2 = self.session("w1-restart", [(2.0, None), (2.5, None)])
        code, out, _ = self.run_watch(
            ["atk-w1", "--planning-note-usd", "4", "--ack-cost", token],
            [_pi_agent("atk-w1", "working", 4, s2)],
        )
        self.assertEqual(code, 0)
        self.assertIn("agent:atk-w1", out["planning"]["alerts"])

    def test_planning_ack_does_not_revive_cost_alert(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        _, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4", "--planning-note-usd", "4"], agents
        )
        cost_token = out["cost"]["alerts"]["agent:atk-w1"]["ack"]
        plan_token = out["planning"]["alerts"]["agent:atk-w1"]["ack"]
        # Ack the planning note; the cost alert must remain independent.
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4", "--planning-note-usd", "4",
             "--ack-cost", plan_token],
            agents,
        )
        self.assertEqual(code, 0)  # cost alert still fires (unacked)
        self.assertEqual(out["cost"]["alerts"]["agent:atk-w1"]["ack"], cost_token)
        self.assertEqual(out["planning"]["alerts"], {})

    def test_cost_ack_does_not_clobber_planning_note(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        _, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4", "--planning-note-usd", "4"], agents
        )
        cost_token = out["cost"]["alerts"]["agent:atk-w1"]["ack"]
        plan_token = out["planning"]["alerts"]["agent:atk-w1"]["ack"]
        # Ack the cost alert; the planning note must remain independent.
        code, out, _ = self.run_watch(
            ["atk-w1", "--cost-cap-agent", "4", "--planning-note-usd", "4",
             "--ack-cost", cost_token],
            agents,
        )
        self.assertEqual(code, 0)  # planning note still fires (unacked)
        self.assertEqual(out["planning"]["alerts"]["agent:atk-w1"]["ack"], plan_token)
        self.assertEqual(out["cost"]["alerts"], {})

    def test_no_planning_key_without_flag(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        code, out, _ = self.run_watch(["atk-w1"], agents)
        self.assertEqual(code, 124)
        self.assertNotIn("planning", out)

    def test_non_pi_agent_never_fires(self) -> None:
        agents = [_agent("atk-w1", "working", 3)]
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 124)
        self.assertEqual(out["planning"]["alerts"], {})
        self.assertIsNone(out["planning"]["agents"]["atk-w1"]["usd"])

    def test_malformed_write_bearing_record_still_fires(self) -> None:
        path = self.sessions / "--proj--" / "w1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self._msg(4.5)
            + "\n"
            + '{"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "write", "arguments": {"path": "src/a.py"}}]',
        )  # torn tail would have been the first write; it is unreadable
        stamp = self.now.timestamp()
        os.utime(path, (stamp, stamp))
        agents = [_pi_agent("atk-w1", "working", 3, str(path))]
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 0)  # no write observed -> note fires
        self.assertIn("agent:atk-w1", out["planning"]["alerts"])

    def test_valid_unterminated_final_write_is_seen(self) -> None:
        path = self.sessions / "--proj--" / "w1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A valid but unterminated final line (no trailing newline) is parsed.
        path.write_bytes(
            (
                self._msg(4.5, "src/a.py") + "\n" + self._msg(0.1, "src/b.py")
            ).encode()
        )
        stamp = self.now.timestamp()
        os.utime(path, (stamp, stamp))
        agents = [_pi_agent("atk-w1", "working", 3, str(path))]
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 124)  # a write was observed
        self.assertEqual(
            out["planning"]["agents"]["atk-w1"]["first_write"], "src/a.py"
        )

    def test_no_write_reason_refused_for_cost_token(self) -> None:
        s = self.session("w1", [(5.0, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        code, _, err = self.run_watch(
            ["atk-w1", "--planning-note-usd", "4",
             "--ack-cost", "cost:agent:atk-w1:no-write:5.0:-:-:2026-10-15"],
            agents,
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown reason", err)

    def test_planning_ack_requires_flag(self) -> None:
        s = self.session("w1", [(5.0, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        code, _, err = self.run_watch(
            ["atk-w1", "--ack-cost", "planning:agent:atk-w1:no-write:5.0:-:-:2026-10-15"],
            agents,
        )
        self.assertEqual(code, 2)
        self.assertIn("needs --planning-note-usd", err)

    def test_planning_ack_round_trips_through_state(self) -> None:
        s = self.session("w1", [(2.0, None), (2.5, None)])
        agents = [_pi_agent("atk-w1", "working", 3, s)]
        _, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        token = out["planning"]["alerts"]["agent:atk-w1"]["ack"]
        self.run_watch(
            ["atk-w1", "--planning-note-usd", "4", "--ack-cost", token], agents
        )
        saved = json.loads(self.state.read_text())
        self.assertIn("planning:agent:atk-w1", saved["cost_acks"])
        self.assertNotIn("agent:atk-w1", saved["cost_acks"])
        # Reload the state and confirm the ack still silences.
        code, out, _ = self.run_watch(["atk-w1", "--planning-note-usd", "4"], agents)
        self.assertEqual(code, 124)
        self.assertEqual(out["planning"]["alerts"], {})


if __name__ == "__main__":
    unittest.main()
