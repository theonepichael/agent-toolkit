#!/usr/bin/env python3
"""test_worker_run_metrics.py — unit tests for worker_run_metrics.py."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import analyze_sessions  # noqa: E402
import worker_run_metrics  # noqa: E402
from pytest_shim import pytest  # noqa: E402


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_len(field: int, payload: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(payload)) + payload


def _request_id_blob(req_id: str) -> bytes:
    return _pb_len(1, _pb_len(4, _pb_len(11, req_id.encode("utf-8"))))


def _agy_db(db_path: Path, request_ids: list[str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        for idx, req_id in enumerate(request_ids):
            conn.execute(
                "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
                (idx, _request_id_blob(req_id)),
            )
        conn.commit()
    finally:
        conn.close()


def _pi_session_lines(
    session_id: str,
    cwd: str,
    model: str,
    *,
    write_path: str | None = None,
    costs: list[float],
) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = [
        {
            "type": "session",
            "id": session_id,
            "cwd": cwd,
            "timestamp": "2026-01-01T00:00:00Z",
        },
        {"type": "model_change", "modelId": model},
        {
            "type": "message",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        },
    ]
    for i, cost in enumerate(costs):
        content: list[dict[str, object]] = [
            {"type": "text", "text": f"step {i}"}
        ]
        if write_path and i == 0:
            content.append(
                {
                    "type": "toolCall",
                    "name": "write",
                    "arguments": {"path": write_path},
                }
            )
        lines.append(
            {
                "type": "message",
                "timestamp": f"2026-01-01T00:0{i + 2}:00Z",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "usage": {"input": 100, "output": 50, "cost": {"total": cost}},
                },
            }
        )
    return lines


class TestWorkerRunMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = test_bootstrap.install_sandbox_home()

    def _launch(
        self,
        slug: str,
        *,
        ts: str,
        model: str | None = None,
        worker_session: str | None = None,
        kind: str = "worker",
    ) -> dict[str, object]:
        rec: dict[str, object] = {"kind": kind, "slug": slug, "ts": ts}
        if model is not None:
            rec["model"] = model
        if worker_session is not None:
            rec["worker_session"] = worker_session
        return rec

    def _run(self, argv: list[str]) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = worker_run_metrics.main(argv)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    # ── pure aggregation tests ────────────────────────────────────────────

    def test_landed_pi_item(self) -> None:
        session = "/sandbox/.pi/agent/sessions/x/abc.jsonl"
        items = {
            "slug-a": {
                "id": "slug-a",
                "status": "done",
                "completed_at": "2026-01-01T00:10:00Z",
            }
        }
        launches = [
            self._launch("slug-a", ts="2026-01-01T00:00:00Z", worker_session=session)
        ]
        # patch the session reader to a fake, read-only-free measurement
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: worker_run_metrics.SessionInfo(
            harness="pi",
            session_model="deepseek-v4-pro",
            minutes_to_first_write=5.0,
            spend_to_first_write=0.05,
            first_write_complete=True,
            total_spend=0.20,
            total_spend_complete=True,
            readable=True,
        )
        try:
            rows = worker_run_metrics.compute_report(launches, items, {}, [])
        finally:
            worker_run_metrics._pi_session_info = original

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["harness"], "pi")
        self.assertEqual(row["model"], "deepseek-v4-pro")
        self.assertEqual(row["launched"], 1)
        self.assertEqual(row["landed"], 1)
        self.assertEqual(row["sent_back"], 0)
        self.assertEqual(row["unjoined"], 0)
        self.assertEqual(row["first_write"]["median_minutes"], 5.0)
        self.assertEqual(row["first_write"]["median_spend"], 0.05)
        self.assertEqual(row["first_write"]["spend_unit"], "USD")
        self.assertAlmostEqual(row["spend_per_landed"]["value"], 0.20)
        self.assertEqual(row["spend_per_landed"]["unit"], "USD")
        self.assertTrue(row["spend_per_landed"]["complete"])
        self.assertEqual(row["spend_per_landed"]["covered"], 1)
        self.assertEqual(row["spend_per_landed"]["of"], 1)

    def test_unreadable_journal_renders_unmeasured(self) -> None:
        session = "/sandbox/.pi/agent/sessions/x/abc.jsonl"
        items = {"slug-b": {"id": "slug-b", "status": "done"}}
        done_ts = {"slug-b": "2026-01-01T00:10:00Z"}
        launches = [
            self._launch("slug-b", ts="2026-01-01T00:00:00Z", worker_session=session)
        ]
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: worker_run_metrics.SessionInfo(
            harness="pi",
            session_model="m",
            minutes_to_first_write=None,
            spend_to_first_write=None,
            first_write_complete=True,
            total_spend=None,
            total_spend_complete=True,
            readable=True,
        )
        try:
            rows = worker_run_metrics.compute_report(launches, items, done_ts, None)
        finally:
            worker_run_metrics._pi_session_info = original
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["sent_back"])  # unreadable journal -> unmeasured

    @pytest.mark.regression(
        "sendback-counted-per-receiving-model",
        "compute_report() takes 3 positional arguments but 4 were given",
    )
    def test_sendback_counted_per_receiving_model(self) -> None:
        session_a = "/sandbox/.pi/agent/sessions/x/a.jsonl"
        session_b = "/sandbox/.pi/agent/sessions/x/b.jsonl"
        items = {"slug-sb": {"id": "slug-sb", "status": "done"}}
        done_ts = {"slug-sb": "2026-01-01T00:10:00Z"}
        launches = [
            self._launch("slug-sb", ts="2026-01-01T00:00:00Z", worker_session=session_a),
            self._launch("slug-sb", ts="2026-01-01T00:08:00Z", worker_session=session_b),
        ]
        # model A receives the send-back at 00:05, before B's launch at 00:08.
        sendbacks = [("slug-sb", "2026-01-01T00:05:00Z")]
        info = {
            session_a: worker_run_metrics.SessionInfo(
                harness="pi", session_model="model-a", minutes_to_first_write=None,
                spend_to_first_write=None, first_write_complete=True,
                total_spend=None, total_spend_complete=True, readable=True,
            ),
            session_b: worker_run_metrics.SessionInfo(
                harness="pi", session_model="model-b", minutes_to_first_write=None,
                spend_to_first_write=None, first_write_complete=True,
                total_spend=None, total_spend_complete=True, readable=True,
            ),
        }
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: info[str(p)]
        try:
            rows = worker_run_metrics.compute_report(launches, items, done_ts, sendbacks)
        finally:
            worker_run_metrics._pi_session_info = original
        by_model = {r["model"]: r for r in rows}
        self.assertEqual(by_model["model-a"]["sent_back"], 1)
        self.assertEqual(by_model["model-b"]["sent_back"], 0)

    def test_sendback_unattributed_when_no_launch_at_or_before(self) -> None:
        session = "/sandbox/.pi/agent/sessions/x/a.jsonl"
        items = {"slug-u": {"id": "slug-u", "status": "done"}}
        done_ts = {"slug-u": "2026-01-01T00:10:00Z"}
        launches = [
            self._launch("slug-u", ts="2026-01-01T00:05:00Z", worker_session=session)
        ]
        sendbacks = [("slug-u", "2026-01-01T00:00:00Z")]  # before every launch
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: worker_run_metrics.SessionInfo(
            harness="pi", session_model="m", minutes_to_first_write=None,
            spend_to_first_write=None, first_write_complete=True,
            total_spend=None, total_spend_complete=True, readable=True,
        )
        try:
            rows = worker_run_metrics.compute_report(launches, items, done_ts, sendbacks)
        finally:
            worker_run_metrics._pi_session_info = original
        self.assertEqual(rows[0]["sent_back"], 0)

    def test_sendback_invalid_timestamp_unattributed(self) -> None:
        session = "/sandbox/.pi/agent/sessions/x/a.jsonl"
        items = {"slug-v": {"id": "slug-v", "status": "done"}}
        done_ts = {"slug-v": "2026-01-01T00:10:00Z"}
        launches = [
            self._launch("slug-v", ts="2026-01-01T00:05:00Z", worker_session=session)
        ]
        sendbacks = [("slug-v", "bad timestamp")]
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: worker_run_metrics.SessionInfo(
            harness="pi", session_model="m", minutes_to_first_write=None,
            spend_to_first_write=None, first_write_complete=True,
            total_spend=None, total_spend_complete=True, readable=True,
        )
        try:
            rows = worker_run_metrics.compute_report(launches, items, done_ts, sendbacks)
        finally:
            worker_run_metrics._pi_session_info = original
        self.assertEqual(rows[0]["sent_back"], 0)

    def test_sendback_events_only_counts_sendback_cmd(self) -> None:
        journal = self.sandbox / ".agent-toolkit" / "data" / "backlog" / "journal.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            {"cmd": "reject", "kind": "backlog", "slug": "r", "ts": "2026-01-01T00:01:00Z"},
            {"cmd": "sendback", "kind": "backlog", "slug": "s", "ts": "2026-01-01T00:02:00Z"},
            {"cmd": "approve", "kind": "backlog", "slug": "s", "ts": "2026-01-01T00:03:00Z"},
        ]
        journal.write_text(
            "\n".join(json.dumps(x) for x in lines) + "\nnot json\n"
        )
        events = worker_run_metrics._sendback_events(journal)
        self.assertEqual(events, [("s", "2026-01-01T00:02:00Z")])

    def test_sendback_events_unreadable_returns_none(self) -> None:
        journal = self.sandbox / ".agent-toolkit" / "data" / "backlog" / "missing.jsonl"
        self.assertIsNone(worker_run_metrics._sendback_events(journal))

    def test_unjoinable_launch_record(self) -> None:
        items = {"slug-c": {"id": "slug-c", "status": "open"}}
        launches = [
            self._launch("slug-c", ts="2026-01-01T00:00:00Z", worker_session=None)
        ]
        rows = worker_run_metrics.compute_report(launches, items, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["harness"], "unknown")
        self.assertEqual(rows[0]["launched"], 1)
        self.assertEqual(rows[0]["landed"], 0)
        self.assertEqual(rows[0]["unjoined"], 1)
        self.assertEqual(rows[0]["spend_per_landed"]["unit"], "unmeasured")

    def test_slug_absent_from_items_is_unjoined(self) -> None:
        session = "/sandbox/.pi/agent/sessions/x/abc.jsonl"
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: worker_run_metrics.SessionInfo(
            harness="pi",
            session_model="m",
            minutes_to_first_write=None,
            spend_to_first_write=None,
            first_write_complete=True,
            total_spend=None,
            total_spend_complete=True,
            readable=True,
        )
        try:
            rows = worker_run_metrics.compute_report(
                [self._launch("gone", ts="2026-01-01T00:00:00Z", worker_session=session)],
                {},
                {},
            )
        finally:
            worker_run_metrics._pi_session_info = original
        self.assertEqual(rows[0]["unjoined"], 1)
        self.assertEqual(rows[0]["landed"], 0)

    def test_relaunch_two_models_lands_in_final_group(self) -> None:
        s_a = "/sandbox/.pi/agent/sessions/a.jsonl"
        s_b = "/sandbox/.pi/agent/sessions/b.jsonl"
        items = {
            "slug-e": {"id": "slug-e", "status": "done", "completed_at": "2026-01-01T00:20:00Z"}
        }
        launches = [
            self._launch("slug-e", ts="2026-01-01T00:00:00Z", model="model-A", worker_session=s_a),
            self._launch("slug-e", ts="2026-01-01T00:10:00Z", model="model-B", worker_session=s_b),
        ]
        def fake(path: Path) -> worker_run_metrics.SessionInfo:
            spend = 0.10 if str(path).endswith("a.jsonl") else 0.30
            return worker_run_metrics.SessionInfo(
                harness="pi",
                session_model=None,
                minutes_to_first_write=None,
                spend_to_first_write=None,
                first_write_complete=True,
                total_spend=spend,
                total_spend_complete=True,
                readable=True,
            )
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = fake
        try:
            rows = worker_run_metrics.compute_report(launches, items, {})
        finally:
            worker_run_metrics._pi_session_info = original
        by_model = {r["model"]: r for r in rows}
        self.assertEqual(by_model["model-A"]["launched"], 1)
        self.assertEqual(by_model["model-A"]["landed"], 0)
        self.assertIsNone(by_model["model-A"]["spend_per_landed"]["value"])
        self.assertEqual(by_model["model-B"]["launched"], 1)
        self.assertEqual(by_model["model-B"]["landed"], 1)
        self.assertAlmostEqual(by_model["model-B"]["spend_per_landed"]["value"], 0.30)

    # ── end-to-end sandbox tests ──────────────────────────────────────────

    def _write_file(self, rel: str, text: str) -> None:
        path = self.sandbox / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_end_to_end_sandbox_resolution(self) -> None:
        session_dir = self.sandbox / ".pi" / "agent" / "sessions" / "x"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / "abc.jsonl"
        session_path.write_text(
            "\n".join(
                json.dumps(line)
                for line in _pi_session_lines(
                    "abc",
                    str(self.sandbox / "repo"),
                    "deepseek-v4-pro",
                    write_path=str(self.sandbox / "repo" / "f.py"),
                    costs=[0.05, 0.05],
                )
            )
            + "\n"
        )
        self._write_file(
            ".agent-toolkit/data/launch_log.jsonl",
            json.dumps(
                {
                    "kind": "worker",
                    "slug": "slug-f",
                    "ts": "2026-01-01T00:00:00Z",
                    "model": None,
                    "worker_session": str(session_path),
                }
            )
            + "\n",
        )
        self._write_file(
            ".agent-toolkit/data/backlog/items.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "items": [
                        {
                            "id": "slug-f",
                            "status": "done",
                            "completed_at": "2026-01-01T00:10:00Z",
                        }
                    ],
                }
            ),
        )
        out = json.loads(self._run(["--json"]))
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["harness"], "pi")
        self.assertEqual(row["model"], "deepseek-v4-pro")
        self.assertEqual(row["launched"], 1)
        self.assertEqual(row["landed"], 1)
        self.assertIsNotNone(row["first_write"]["median_minutes"])
        self.assertEqual(row["spend_per_landed"]["unit"], "USD")
        self.assertAlmostEqual(row["spend_per_landed"]["value"], 0.10)

    # ── analyze_sessions supporting change ────────────────────────────────

    def test_strict_read_only_never_creates_db(self) -> None:
        db = self.sandbox / ".gemini" / "antigravity-cli" / "conversations" / "nope.db"
        self.assertFalse(db.exists())
        result = analyze_sessions.read_agy_session_calls(db, strict_read_only=True)
        self.assertTrue(result.unreadable)
        self.assertIsNone(result.calls)
        self.assertFalse(db.exists())  # never created

    def test_strict_read_only_no_write_on_corrupt_db(self) -> None:
        db = self.sandbox / ".gemini" / "antigravity-cli" / "conversations" / "corrupt.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"not a sqlite database")
        before = db.read_bytes()
        result = analyze_sessions.read_agy_session_calls(db, strict_read_only=True)
        self.assertTrue(result.unreadable)
        self.assertEqual(db.read_bytes(), before)  # unchanged, no fallback write

    def test_agy_measured_calls_end_to_end(self) -> None:
        conv = "11111111-2222-3333-4444-555555555555"
        _agy_db(
            self.sandbox / ".gemini" / "antigravity-cli" / "conversations" / f"{conv}.db",
            ["req-1", "req-2"],
        )
        self._write_file(
            ".agent-toolkit/data/launch_log.jsonl",
            json.dumps(
                {
                    "kind": "worker",
                    "slug": "slug-agy",
                    "ts": "2026-01-01T00:00:00Z",
                    "worker_session": conv,
                }
            )
            + "\n",
        )
        self._write_file(
            ".agent-toolkit/data/backlog/items.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "items": [
                        {"id": "slug-agy", "status": "done", "completed_at": "2026-01-01T00:10:00Z"}
                    ],
                }
            ),
        )
        rows = json.loads(self._run(["--json"]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["harness"], "agy")
        self.assertEqual(rows[0]["model"], "unknown")
        self.assertEqual(rows[0]["spend_per_landed"]["unit"], "calls")
        self.assertAlmostEqual(rows[0]["spend_per_landed"]["value"], 2.0)
        self.assertTrue(rows[0]["spend_per_landed"]["complete"])

    def test_agy_uuid_and_db_path_alias_to_one_session(self) -> None:
        conv = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        db_path = self.sandbox / ".gemini" / "antigravity-cli" / "conversations" / f"{conv}.db"
        _agy_db(db_path, ["req-1"])
        items = {"slug-alias": {"id": "slug-alias", "status": "done", "completed_at": "2026-01-01T00:10:00Z"}}
        launches = [
            self._launch("slug-alias", ts="2026-01-01T00:00:00Z", worker_session=conv),
            self._launch("slug-alias", ts="2026-01-01T00:01:00Z", worker_session=str(db_path)),
        ]
        rows = worker_run_metrics.compute_report(launches, items, {})
        self.assertEqual(len(rows), 1)
        # one session, counted once -> spend_per_landed is 1 call, not 2
        self.assertAlmostEqual(rows[0]["spend_per_landed"]["value"], 1.0)

    # ── review-finding regressions ───────────────────────────────────────

    def test_missing_session_marks_spend_incomplete(self) -> None:
        s_ok = "/sandbox/.pi/agent/sessions/ok.jsonl"
        s_missing = "/sandbox/.pi/agent/sessions/missing.jsonl"
        items = {
            "slug-m": {"id": "slug-m", "status": "done", "completed_at": "2026-01-01T00:20:00Z"}
        }
        launches = [
            self._launch("slug-m", ts="2026-01-01T00:00:00Z", model="m", worker_session=s_ok),
            self._launch("slug-m", ts="2026-01-01T00:10:00Z", model="m", worker_session=s_missing),
        ]

        def fake(path: Path) -> worker_run_metrics.SessionInfo:
            if str(path).endswith("ok.jsonl"):
                return worker_run_metrics.SessionInfo(
                    "pi", "m", None, None, True, 1.0, True, True
                )
            return worker_run_metrics.SessionInfo(
                "pi", "m", None, None, True, None, True, False
            )

        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = fake
        try:
            rows = worker_run_metrics.compute_report(launches, items, {})
        finally:
            worker_run_metrics._pi_session_info = original
        row = rows[0]
        self.assertEqual(row["unjoined"], 1)
        self.assertAlmostEqual(row["spend_per_landed"]["value"], 1.0)
        self.assertEqual(row["spend_per_landed"]["covered"], 1)
        self.assertEqual(row["spend_per_landed"]["of"], 1)
        self.assertFalse(row["spend_per_landed"]["complete"])

    def test_external_db_path_measured(self) -> None:
        db = self.sandbox / "external" / "conversation.db"
        _agy_db(db, ["req-1", "req-2"])
        items = {
            "slug-x": {"id": "slug-x", "status": "done", "completed_at": "2026-01-01T00:10:00Z"}
        }
        launches = [
            self._launch("slug-x", ts="2026-01-01T00:00:00Z", worker_session=str(db))
        ]
        rows = worker_run_metrics.compute_report(launches, items, {})
        self.assertEqual(rows[0]["harness"], "agy")
        self.assertEqual(rows[0]["spend_per_landed"]["unit"], "calls")
        self.assertAlmostEqual(rows[0]["spend_per_landed"]["value"], 2.0)

    def test_zero_spend_is_measured_not_unmeasured(self) -> None:
        session = "/sandbox/.pi/agent/sessions/zero.jsonl"
        items = {
            "slug-z": {"id": "slug-z", "status": "done", "completed_at": "2026-01-01T00:10:00Z"}
        }
        launches = [
            self._launch("slug-z", ts="2026-01-01T00:00:00Z", worker_session=session)
        ]
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: worker_run_metrics.SessionInfo(
            "pi", "m", None, None, True, 0.0, True, True
        )
        try:
            rows = worker_run_metrics.compute_report(launches, items, {})
        finally:
            worker_run_metrics._pi_session_info = original
        self.assertEqual(rows[0]["spend_per_landed"]["value"], 0.0)
        self.assertTrue(rows[0]["spend_per_landed"]["complete"])

    def test_partial_session_cost_keeps_known_floor(self) -> None:
        session = "/sandbox/.pi/agent/sessions/partial.jsonl"
        items = {
            "slug-p": {"id": "slug-p", "status": "done", "completed_at": "2026-01-01T00:10:00Z"}
        }
        launches = [
            self._launch("slug-p", ts="2026-01-01T00:00:00Z", worker_session=session)
        ]
        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = lambda p: worker_run_metrics.SessionInfo(
            "pi", "m", None, None, True, 0.50, False, True
        )
        try:
            rows = worker_run_metrics.compute_report(launches, items, {})
        finally:
            worker_run_metrics._pi_session_info = original
        row = rows[0]
        # known partial spend is summed (a floor), but flagged incomplete
        self.assertAlmostEqual(row["spend_per_landed"]["value"], 0.50)
        self.assertFalse(row["spend_per_landed"]["complete"])

    def test_post_completion_launch_excluded_from_spend(self) -> None:
        s1 = "/sandbox/.pi/agent/sessions/s1.jsonl"
        s2 = "/sandbox/.pi/agent/sessions/s2.jsonl"
        items = {
            "slug-l": {"id": "slug-l", "status": "done", "completed_at": "2026-01-01T00:10:00Z"}
        }
        launches = [
            self._launch("slug-l", ts="2026-01-01T00:00:00Z", model="m", worker_session=s1),
            self._launch("slug-l", ts="2026-01-01T00:20:00Z", model="m", worker_session=s2),
        ]

        def fake(path: Path) -> worker_run_metrics.SessionInfo:
            spend = 1.0 if str(path).endswith("s1.jsonl") else 9.0
            return worker_run_metrics.SessionInfo(
                "pi", "m", None, None, True, spend, True, True
            )

        original = worker_run_metrics._pi_session_info
        worker_run_metrics._pi_session_info = fake
        try:
            rows = worker_run_metrics.compute_report(launches, items, {})
        finally:
            worker_run_metrics._pi_session_info = original
        row = rows[0]
        self.assertEqual(row["landed"], 1)
        # only the pre-completion session's spend counts
        self.assertAlmostEqual(row["spend_per_landed"]["value"], 1.0)

    def test_write_without_cost_marks_first_write_incomplete(self) -> None:
        session_dir = self.sandbox / ".pi" / "agent" / "sessions" / "y"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / "nocost.jsonl"
        cwd = str(self.sandbox / "repo")
        lines = [
            {"type": "session", "id": "nocost", "cwd": cwd, "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "model_change", "modelId": "m"},
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
            },
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "write",
                            "arguments": {"path": f"{cwd}/f.py"},
                        }
                    ],
                    "usage": {"input": 100, "output": 50},
                },
            },
        ]
        session_path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n"
        )
        info = worker_run_metrics._pi_session_info(session_path)
        self.assertTrue(info.readable)
        self.assertIsNotNone(info.minutes_to_first_write)
        self.assertIsNone(info.spend_to_first_write)
        self.assertFalse(info.first_write_complete)

    def test_first_write_outside_cwd_is_counted(self) -> None:
        # A worker writes to a sibling path / the grill dir, not under its
        # launch cwd. The first write must still be the earliest write
        # anywhere (matching `milestones --repo /`), not the first under cwd.
        session_dir = self.sandbox / ".pi" / "agent" / "sessions" / "w"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / "outside.jsonl"
        cwd = str(self.sandbox / "repo" / "worktree")
        outside = str(self.sandbox / "elsewhere" / "f.py")
        lines = [
            {"type": "session", "id": "outside", "cwd": cwd, "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "model_change", "modelId": "m"},
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
            },
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "name": "write", "arguments": {"path": outside}}
                    ],
                    "usage": {"input": 100, "output": 50, "cost": {"total": 0.05}},
                },
            },
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:10Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "write",
                            "arguments": {"path": f"{cwd}/g.py"},
                        }
                    ],
                    "usage": {"input": 100, "output": 50, "cost": {"total": 0.05}},
                },
            },
        ]
        session_path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n"
        )
        info = worker_run_metrics._pi_session_info(session_path)
        self.assertTrue(info.readable)
        # first write at 2s (the outside write), not the 10s in-cwd write
        self.assertAlmostEqual(info.minutes_to_first_write, 2 / 60, places=4)

    def test_pi_model_label_provider_and_level(self) -> None:
        session_dir = self.sandbox / ".pi" / "agent" / "sessions" / "m"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / "m.jsonl"
        session_path.write_text(
            "\n".join(
                json.dumps(line)
                for line in [
                    {"type": "model_change", "provider": "opencode-go", "modelId": "mimo-v2.6-pro"},
                    {"type": "thinking_level_change", "thinkingLevel": "high"},
                ]
            )
            + "\n"
        )
        self.assertEqual(
            worker_run_metrics._pi_model_label(session_path),
            "opencode-go/mimo-v2.6-pro:high",
        )


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
