#!/usr/bin/env python3
"""test_analyze_sessions.py — unit tests for analyze_sessions.py."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import analyze_sessions


class TestAnalyzeSessionsAdapters(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pi_adapter(self) -> None:
        pi_dir = self.root / ".pi" / "agent" / "sessions" / "--home-user--"
        pi_dir.mkdir(parents=True)
        session_file = pi_dir / "2026-08-30T05-00-00-000Z_sess123.jsonl"
        lines = [
            {
                "type": "session",
                "id": "sess123",
                "cwd": "/home/user/project",
                "timestamp": "2026-08-30T05:00:00.000Z",
            },
            {"type": "model_change", "modelId": "claude-3-7-sonnet"},
            {
                "type": "message",
                "timestamp": "2026-08-30T05:00:01.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello pi agent!"}],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-08-30T05:00:05.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello user, I can help."}],
                    "usage": {
                        "input": 1000,
                        "output": 200,
                        "cacheRead": 500,
                        "cacheWrite": 100,
                        "cost": {"total": 0.0055},
                    },
                },
            },
        ]
        with session_file.open("w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")

        records, _skipped = analyze_sessions.load_pi_records(
            base_dir=self.root / ".pi" / "agent" / "sessions"
        )
        self.assertEqual(len(records), 2)
        u_rec, a_rec = records[0], records[1]
        self.assertEqual(u_rec.role, "user")
        self.assertEqual(u_rec.text, "Hello pi agent!")
        self.assertEqual(u_rec.session_id, "sess123")
        self.assertEqual(u_rec.cwd, "/home/user/project")
        self.assertFalse(u_rec.is_subagent)

        self.assertEqual(a_rec.role, "assistant")
        self.assertEqual(a_rec.text, "Hello user, I can help.")
        self.assertEqual(a_rec.model, "claude-3-7-sonnet")
        self.assertEqual(a_rec.input_tokens, 1000)
        self.assertEqual(a_rec.output_tokens, 200)
        self.assertEqual(a_rec.cache_read_tokens, 500)
        self.assertEqual(a_rec.cache_write_tokens, 100)
        self.assertEqual(a_rec.cost_usd, 0.0055)
        self.assertEqual(a_rec.cost_origin, "native")

    def test_claude_adapter(self) -> None:
        claude_dir = self.root / ".claude" / "projects" / "test-proj"
        claude_dir.mkdir(parents=True)
        session_file = claude_dir / "c123.jsonl"
        lines = [
            {
                "type": "user",
                "sessionId": "c123",
                "cwd": "/home/user/claudeproj",
                "timestamp": "2026-08-31T12:00:00.000Z",
                "isSidechain": False,
                "message": {"role": "user", "content": "Refactor the module"},
            },
            {
                "type": "assistant",
                "sessionId": "c123",
                "cwd": "/home/user/claudeproj",
                "timestamp": "2026-08-31T12:00:05.000Z",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "model": "claude-3-5-sonnet",
                    "content": [{"type": "text", "text": "Refactoring complete."}],
                    "usage": {
                        "input_tokens": 10000,
                        "output_tokens": 1000,
                        "cache_read_input_tokens": 5000,
                        "cache_creation_input_tokens": 2000,
                    },
                },
            },
            {
                "type": "user",
                "sessionId": "c123",
                "cwd": "/home/user/claudeproj",
                "timestamp": "2026-08-31T12:01:00.000Z",
                "isSidechain": True,
                "message": {"role": "user", "content": "Subagent prompt"},
            },
        ]
        with session_file.open("w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")

        records, _skipped = analyze_sessions.load_claude_records(
            base_dir=self.root / ".claude" / "projects"
        )
        self.assertEqual(len(records), 3)
        u_rec, a_rec, sub_rec = records[0], records[1], records[2]

        self.assertEqual(u_rec.role, "user")
        self.assertEqual(u_rec.text, "Refactor the module")
        self.assertEqual(u_rec.cwd, "/home/user/claudeproj")
        self.assertFalse(u_rec.is_subagent)

        self.assertEqual(a_rec.role, "assistant")
        self.assertEqual(a_rec.model, "claude-3-5-sonnet")
        self.assertEqual(a_rec.input_tokens, 10000)
        self.assertEqual(a_rec.output_tokens, 1000)
        self.assertEqual(a_rec.cache_read_tokens, 5000)
        self.assertEqual(a_rec.cache_write_tokens, 2000)
        self.assertAlmostEqual(a_rec.cost_usd or 0, 0.054, places=5)
        self.assertEqual(a_rec.cost_origin, "derived")

        self.assertTrue(sub_rec.is_subagent)

    def test_opencode_adapter(self) -> None:
        db_path = self.root / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER)"
        )
        cursor.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)"
        )
        cursor.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
        )

        cursor.execute(
            "INSERT INTO session VALUES ('ses_1', NULL, '/home/user/opencodeproj', 1784831400000, 1784831450000)"
        )
        cursor.execute(
            "INSERT INTO message VALUES ('msg_1', 'ses_1', 1784831401000, ?)",
            (json.dumps({"role": "user"}),),
        )
        cursor.execute(
            "INSERT INTO part VALUES ('part_1', 'msg_1', 'ses_1', 1784831401000, ?)",
            (json.dumps({"type": "text", "text": "Fix the bug"}),),
        )

        cursor.execute(
            "INSERT INTO message VALUES ('msg_2', 'ses_1', 1784831405000, ?)",
            (
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "zai-org/GLM-5.2",
                        "cost": 0.0042,
                        "tokens": {
                            "input": 5000,
                            "output": 100,
                            "cache": {"read": 200, "write": 0},
                        },
                    }
                ),
            ),
        )
        cursor.execute(
            "INSERT INTO part VALUES ('part_2', 'msg_2', 'ses_1', 1784831405000, ?)",
            (json.dumps({"type": "text", "text": "Bug has been fixed."}),),
        )
        conn.commit()
        conn.close()

        records, _skipped = analyze_sessions.load_opencode_records(db_path=db_path)
        self.assertEqual(len(records), 2)
        u_rec, a_rec = records[0], records[1]
        self.assertEqual(u_rec.role, "user")
        self.assertEqual(u_rec.text, "Fix the bug")
        self.assertEqual(u_rec.cwd, "/home/user/opencodeproj")
        self.assertFalse(u_rec.is_subagent)

        self.assertEqual(a_rec.role, "assistant")
        self.assertEqual(a_rec.text, "Bug has been fixed.")
        self.assertEqual(a_rec.model, "zai-org/GLM-5.2")
        self.assertEqual(a_rec.input_tokens, 5000)
        self.assertEqual(a_rec.output_tokens, 100)
        self.assertEqual(a_rec.cache_read_tokens, 200)
        self.assertEqual(a_rec.cost_usd, 0.0042)
        self.assertEqual(a_rec.cost_origin, "native")

    def test_copilot_adapter(self) -> None:
        db_path = self.root / "session-store.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, branch TEXT, created_at TEXT, updated_at TEXT)"
        )
        cursor.execute(
            "CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT)"
        )
        cursor.execute(
            "CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, session_id TEXT, turn_index INTEGER, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER)"
        )

        cursor.execute(
            "INSERT INTO sessions VALUES ('cop_sess1', '/home/user/copilotproj', 'myrepo', 'main', '2026-08-31 10:00:00', '2026-08-31 10:05:00')"
        )
        cursor.execute(
            "INSERT INTO turns VALUES (1, 'cop_sess1', 1, 'Analyze performance', 'Here is the analysis', '2026-08-31 10:00:05')"
        )
        cursor.execute(
            "INSERT INTO assistant_usage_events VALUES (1, 'cop_sess1', 1, 'copilot-model-v1', 2000, 300, 100, 0)"
        )
        conn.commit()
        conn.close()

        records, _skipped = analyze_sessions.load_copilot_records(db_path=db_path)
        self.assertEqual(len(records), 2)
        u_rec, a_rec = records[0], records[1]
        self.assertEqual(u_rec.role, "user")
        self.assertEqual(u_rec.text, "Analyze performance")
        self.assertEqual(u_rec.cwd, "/home/user/copilotproj")

        self.assertEqual(a_rec.role, "assistant")
        self.assertEqual(a_rec.text, "Here is the analysis")
        self.assertEqual(a_rec.model, "copilot-model-v1")
        self.assertEqual(a_rec.input_tokens, 2000)
        self.assertEqual(a_rec.output_tokens, 300)
        self.assertEqual(a_rec.cache_read_tokens, 100)
        self.assertIsNone(a_rec.cost_usd)
        self.assertEqual(a_rec.cost_origin, "unavailable")

    def test_agy_adapter(self) -> None:
        brain_dir = (
            self.root
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / "uuid-1234"
            / ".system_generated"
            / "logs"
        )
        brain_dir.mkdir(parents=True)
        transcript = brain_dir / "transcript_full.jsonl"
        lines = [
            {
                "type": "USER_INPUT",
                "content": "Build UI component",
                "created_at": "2026-08-27T00:00:00Z",
            },
            {
                "type": "PLANNER_RESPONSE",
                "content": "Component created successfully.",
                "created_at": "2026-08-27T00:01:00Z",
            },
        ]
        with transcript.open("w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")

        records, _skipped = analyze_sessions.load_agy_records(
            base_dir=self.root / ".gemini" / "antigravity-cli" / "brain"
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].role, "user")
        self.assertEqual(records[0].text, "Build UI component")
        self.assertEqual(records[1].role, "assistant")
        self.assertEqual(records[1].text, "Component created successfully.")

        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        # Test load_all_records excludes agy when harness='all'
        all_records = analyze_sessions.load_all_records(
            harness="all",
            pi_dir=empty_dir,
            claude_dir=empty_dir,
            opencode_db=empty_dir / "none.db",
            copilot_db=empty_dir / "none.db",
            agy_dir=self.root / ".gemini" / "antigravity-cli" / "brain",
        )
        self.assertEqual(len(all_records), 0)

        # Test load_all_records includes agy when explicit
        agy_records = analyze_sessions.load_all_records(
            harness="agy",
            agy_dir=self.root / ".gemini" / "antigravity-cli" / "brain",
        )
        self.assertEqual(len(agy_records), 2)


class TestCalculateClaudeCost(unittest.TestCase):
    """Current rates verified against platform.claude.com/docs/en/about-claude/pricing
    (fetched 2026-09-01). Each case pins a real, currently-observed model id string,
    not a hypothetical -- these are the ids that actually show up in session data."""

    def test_sonnet_5_uses_its_own_rate_not_the_stale_generic_sonnet_fallback(
        self,
    ) -> None:
        # $2/$10 in/out is Sonnet 5's permanent rate (the scheduled Sept 1 2026
        # hike to $3/$15 was cancelled) -- distinct from claude-sonnet-4's $3/$15.
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-sonnet-5", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 2.0)

    def test_opus_5_uses_its_own_rate_not_the_stale_retired_opus_fallback(self) -> None:
        # Opus 5 is $5/$25 -- the old generic "claude-opus" key used to carry
        # the retired Opus 4/4.1 rate ($15/$75), a 3x overcount.
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-opus-5", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 5.0)

    def test_retired_opus_4_1_keeps_its_own_higher_rate(self) -> None:
        # A real historical model id: claude-opus-4-1-20250805. Must still
        # resolve to the retired $15/$75 rate, not fall through to the
        # current-generation Opus default now that the generic fallback below
        # points at $5/$25.
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-opus-4-1-20250805", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 15.0)

    def test_haiku_4_5_uses_its_own_rate_not_the_stale_haiku_3_5_fallback(self) -> None:
        # A real historical model id: claude-haiku-4-5-20251001. Haiku 4.5 is
        # $1/$5 -- the old generic "claude-haiku" key carried Haiku 3.5's rate
        # ($0.80/$4), which used to silently match this id too.
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-haiku-4-5-20251001", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 1.0)

    def test_haiku_3_5_keeps_its_own_lower_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-3-5-haiku-20241022", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 0.80)

    def test_fable_5_1_is_no_longer_unavailable(self) -> None:
        # Previously matched no key at all -- every Fable session silently
        # dropped out of cost rollups (cost_origin stayed "unavailable").
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-fable-5-1", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 10.0)

    def test_fable_5_1_cache_read_uses_its_special_0_025x_multiplier(self) -> None:
        # Fable 5.1 and Mythos 5.1 are the only models with a non-standard
        # cache-hit multiplier (0.025x base input, vs. 0.1x everywhere else).
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-fable-5-1", 0, 0, 1_000_000, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 0.25)

    def test_fable_5_cache_read_uses_the_standard_0_1x_multiplier(self) -> None:
        # Fable 5 (no ".1") predates the special cache multiplier -- distinct
        # from Fable 5.1 despite sharing a "claude-fable-5" prefix.
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-fable-5", 0, 0, 1_000_000, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost, 1.0)

    def test_unrecognized_model_stays_unavailable(self) -> None:
        cost, origin = analyze_sessions.calculate_claude_cost(
            "gpt-4o", 1_000_000, 0, 0, 0
        )
        self.assertIsNone(cost)
        self.assertEqual(origin, "unavailable")

    def test_none_model_stays_unavailable(self) -> None:
        cost, origin = analyze_sessions.calculate_claude_cost(None, 1_000_000, 0, 0, 0)
        self.assertIsNone(cost)
        self.assertEqual(origin, "unavailable")


class TestAnalyzeSessionsCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            analyze_sessions.SessionRecord(
                harness="pi",
                session_id="pi_1",
                cwd="/home/user/repoA",
                timestamp="2026-08-30T10:00:00Z",
                role="user",
                text="Write tests for cost calculation",
                model=None,
            ),
            analyze_sessions.SessionRecord(
                harness="pi",
                session_id="pi_1",
                cwd="/home/user/repoA",
                timestamp="2026-08-30T10:00:05Z",
                role="assistant",
                text="Tests written.",
                model="claude-3-7-sonnet",
                input_tokens=1000,
                output_tokens=100,
                cache_read_tokens=200,
                cost_usd=0.005,
                cost_origin="native",
            ),
            analyze_sessions.SessionRecord(
                harness="claude",
                session_id="cl_1",
                cwd="/home/user/repoB",
                timestamp="2026-08-31T11:00:00Z",
                role="user",
                text="Optimize queries",
                model=None,
            ),
            analyze_sessions.SessionRecord(
                harness="claude",
                session_id="cl_1",
                cwd="/home/user/repoB",
                timestamp="2026-08-31T11:00:10Z",
                role="assistant",
                text="Queries optimized.",
                model="claude-3-5-sonnet",
                input_tokens=5000,
                output_tokens=500,
                cache_read_tokens=1000,
                cost_usd=0.0228,
                cost_origin="derived",
            ),
            analyze_sessions.SessionRecord(
                harness="copilot",
                session_id="cp_1",
                cwd="/home/user/repoA",
                timestamp="2026-08-31T12:00:00Z",
                role="user",
                text="Copilot turn prompt",
            ),
            analyze_sessions.SessionRecord(
                harness="copilot",
                session_id="cp_1",
                cwd="/home/user/repoA",
                timestamp="2026-08-31T12:00:05Z",
                role="assistant",
                text="Copilot turn response",
                model="copilot-v1",
                input_tokens=1000,
                output_tokens=100,
                cost_usd=None,
                cost_origin="unavailable",
            ),
            analyze_sessions.SessionRecord(
                harness="claude",
                session_id="cl_sub",
                cwd="/home/user/repoB",
                timestamp="2026-08-31T13:00:00Z",
                role="user",
                text="Subagent user prompt",
                is_subagent=True,
            ),
            analyze_sessions.SessionRecord(
                harness="claude",
                session_id="cl_sub",
                cwd="/home/user/repoB",
                timestamp="2026-08-31T13:00:05Z",
                role="assistant",
                text="Subagent response",
                model="claude-3-5-haiku",
                input_tokens=1000,
                output_tokens=100,
                cost_usd=0.0012,
                cost_origin="derived",
                is_subagent=True,
            ),
        ]

    def test_cmd_cost_json(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["cost", "--by", "harness", "--json"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_cost(args, self.records, file=buf)
        self.assertEqual(res, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data), 3)
        harnesses = {row["group"]: row for row in data}
        self.assertIn("pi", harnesses)
        self.assertIn("claude", harnesses)
        self.assertIn("copilot", harnesses)
        self.assertEqual(harnesses["pi"]["cost_origin"], "native")
        self.assertEqual(harnesses["claude"]["cost_origin"], "derived")
        self.assertEqual(harnesses["copilot"]["cost_origin"], "unavailable")

    def test_cmd_cost_table_groupings(self) -> None:
        parser = analyze_sessions.build_parser()
        for by in ["total", "day", "project", "model", "session"]:
            args = parser.parse_args(["cost", "--by", by])
            buf = io.StringIO()
            res = analyze_sessions.cmd_cost(args, self.records, file=buf)
            self.assertEqual(res, 0)
            out = buf.getvalue()
            self.assertIn("Cost USD", out)

    def test_cmd_prompts(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["prompts"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_prompts(args, self.records, file=buf)
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn("Write tests for cost calculation", out)
        self.assertIn("Optimize queries", out)
        self.assertIn("Copilot turn prompt", out)
        self.assertNotIn("Tests written.", out)

    def test_cmd_prompts_grep_and_limit(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["prompts", "--grep", "queries", "--limit", "1"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_prompts(args, self.records, file=buf)
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn("Optimize queries", out)
        self.assertNotIn("Copilot", out)

    def test_cmd_prompts_jsonl(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["prompts", "--format", "jsonl"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_prompts(args, self.records, file=buf)
        self.assertEqual(res, 0)
        lines = [json.loads(line) for line in buf.getvalue().strip().split("\n")]
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0]["prompt"], "Write tests for cost calculation")

    def test_cmd_search_substring(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["search", "optimized"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_search(args, self.records, file=buf)
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn("Queries optimized.", out)
        self.assertIn("[claude]", out)

    def test_cmd_search_regex(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["search", r"optimiz\w+", "--regex"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_search(args, self.records, file=buf)
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn("Queries optimized.", out)

    def test_cmd_search_json(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["search", "queries", "--json"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_search(args, self.records, file=buf)
        self.assertEqual(res, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["harness"], "claude")

    def test_cmd_search_copilot_notice(self) -> None:
        parser = analyze_sessions.build_parser()
        args = parser.parse_args(["search", "Copilot turn"])
        buf = io.StringIO()
        res = analyze_sessions.cmd_search(args, self.records, file=buf)
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn("(turn-level digest — no intra-turn context)", out)

    def test_date_boundary_parsing(self) -> None:
        now = datetime.now(UTC)
        dt_today = analyze_sessions.parse_date_boundary("today")
        self.assertIsNotNone(dt_today)
        self.assertEqual(dt_today.day, now.day)

        dt_7d = analyze_sessions.parse_date_boundary("7d")
        self.assertIsNotNone(dt_7d)
        diff = now - dt_7d
        self.assertAlmostEqual(diff.total_seconds(), 7 * 86400, delta=60)

        dt_iso = analyze_sessions.parse_date_boundary("2026-08-31")
        self.assertIsNotNone(dt_iso)
        self.assertEqual(dt_iso.year, 2026)
        self.assertEqual(dt_iso.month, 8)
        self.assertEqual(dt_iso.day, 31)

        self.assertIsNone(analyze_sessions.parse_date_boundary("invalid-date"))


class TestSessionQueryService(unittest.TestCase):
    """Tests for the query_sessions() read-only query/report service.

    Spec: ~/.claude/data/grill/atk-analyze-sessions-query-service-spec.md.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        # pi fixture: two sessions, one with a claude-model assistant record
        pi_dir = self.root / "pi" / "sess"
        pi_dir.mkdir(parents=True)
        self._write_jsonl(
            pi_dir / "pi_a.jsonl",
            [
                {
                    "type": "session",
                    "id": "pi_1",
                    "cwd": "/home/user/repoA",
                    "timestamp": "2026-08-30T05:00:00.000Z",
                },
                {
                    "type": "message",
                    "timestamp": "2026-08-30T05:00:01.000Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello pi agent!"}],
                    },
                },
                {
                    "type": "message",
                    "timestamp": "2026-08-30T05:00:05.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello user."}],
                        "usage": {
                            "input": 1000,
                            "output": 200,
                            "cacheRead": 500,
                            "cacheWrite": 100,
                            "cost": {"total": 0.0055},
                        },
                    },
                },
            ],
        )
        self._write_jsonl(
            pi_dir / "pi_b.jsonl",
            [
                {
                    "type": "session",
                    "id": "pi_2",
                    "cwd": "/home/user/repoB",
                    "timestamp": "2026-09-01T05:00:00.000Z",
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-01T05:00:01.000Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Second prompt"}],
                    },
                },
            ],
        )
        # claude fixture
        claude_dir = self.root / "claude" / "proj"
        claude_dir.mkdir(parents=True)
        self._write_jsonl(
            claude_dir / "c1.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": "c_1",
                    "cwd": "/home/user/repoC",
                    "timestamp": "2026-08-31T12:00:00.000Z",
                    "isSidechain": False,
                    "message": {"role": "user", "content": "Optimize queries"},
                },
                {
                    "type": "assistant",
                    "sessionId": "c_1",
                    "cwd": "/home/user/repoC",
                    "timestamp": "2026-08-31T12:00:05.000Z",
                    "isSidechain": False,
                    "message": {
                        "role": "assistant",
                        "model": "claude-3-5-sonnet",
                        "content": [{"type": "text", "text": "Queries optimized."}],
                        "usage": {
                            "input_tokens": 10000,
                            "output_tokens": 1000,
                            "cache_read_input_tokens": 5000,
                            "cache_creation_input_tokens": 2000,
                        },
                    },
                },
            ],
        )
        self.roots = {
            "pi": self.root / "pi" / "sess",
            "claude": self.root / "claude" / "proj",
            "opencode": self.root / "none-opencode.db",
            "copilot": self.root / "none-copilot.db",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")

    def _query(self, **kwargs: object) -> analyze_sessions.SessionQueryResult:
        q = analyze_sessions.SessionQuery(**kwargs)
        return analyze_sessions.query_sessions(q, roots=self.roots)

    def test_harness_filtering_and_agy_exclusion(self) -> None:
        result = self._query()
        self.assertEqual(
            {r.harness for r in result.records}, {"pi", "claude"}
        )  # harnesses=None excludes agy
        result = self._query(harnesses=frozenset({"pi"}))
        self.assertEqual({r.harness for r in result.records}, {"pi"})
        self.assertEqual(len(result.records), 3)

    def test_agy_opt_in(self) -> None:
        agy_root = self.root / "agy-brain"
        logs = agy_root / "uuid-1" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        self._write_jsonl(
            logs / "transcript_full.jsonl",
            [
                {
                    "type": "USER_INPUT",
                    "content": "Build UI",
                    "created_at": "2026-08-27T00:00:00Z",
                },
            ],
        )
        roots = dict(self.roots)
        roots["agy"] = agy_root
        q = analyze_sessions.SessionQuery(harnesses=frozenset({"agy"}))
        result = analyze_sessions.query_sessions(q, roots=roots)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].text, "Build UI")

    def test_since_until_inclusive_boundaries(self) -> None:
        since = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)  # exactly at record
        result = self._query(harnesses=frozenset({"claude"}), since=since)
        self.assertEqual(len(result.records), 2)  # boundary record included
        until = datetime(2026, 8, 30, 5, 0, 1, tzinfo=UTC)
        result = self._query(harnesses=frozenset({"pi"}), until=until)
        # pi records are at 05:00:01 (user) and 05:00:05 (assistant); the
        # 05:00:01 boundary record is included, the later one excluded.
        self.assertEqual(len(result.records), 1)  # boundary record included
        self.assertEqual(result.records[0].role, "user")

    def test_structural_filters(self) -> None:
        result = self._query(cwd="repoA")
        self.assertEqual({r.session_id for r in result.records}, {"pi_1"})
        result = self._query(model="sonnet")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].session_id, "c_1")
        result = self._query(session_id="pi_2")
        self.assertEqual({r.session_id for r in result.records}, {"pi_2"})
        result = self._query(role="user")
        self.assertEqual({r.role for r in result.records}, {"user"})

    def test_subagent_filtering(self) -> None:
        claude_dir = self.root / "claude" / "proj"
        self._write_jsonl(
            claude_dir / "c2.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": "c_sub",
                    "cwd": "/home/user/repoC",
                    "timestamp": "2026-08-31T13:00:00.000Z",
                    "isSidechain": True,
                    "message": {"role": "user", "content": "Sub prompt"},
                },
            ],
        )
        result = self._query()
        self.assertNotIn("c_sub", {r.session_id for r in result.records})
        result = self._query(include_subagents=True)
        self.assertIn("c_sub", {r.session_id for r in result.records})

    def test_text_substring_case_insensitive(self) -> None:
        result = self._query(text="HELLO")
        self.assertEqual(len(result.records), 2)  # matches both hello prompts

    def test_text_regex_case_insensitive(self) -> None:
        result = self._query(text=r"optimiz\w+", text_regex=True)
        self.assertEqual(len(result.records), 2)  # prompt + response both match
        self.assertEqual({r.role for r in result.records}, {"user", "assistant"})

    def test_text_regex_anchored_to_assistant(self) -> None:
        result = self._query(text=r"optimiz\w+\.", text_regex=True)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].role, "assistant")

    def test_invalid_regex_raises_re_error(self) -> None:
        with self.assertRaises(re.error):
            self._query(text="(", text_regex=True)

    def test_limit_keep_first_and_last(self) -> None:
        result = self._query(limit=2, keep="first")
        self.assertEqual(
            [r.timestamp for r in result.records],
            ["2026-08-30T05:00:01Z", "2026-08-30T05:00:05Z"],
        )
        result = self._query(limit=2, keep="last")
        self.assertEqual(
            [r.timestamp for r in result.records],
            ["2026-08-31T12:00:05Z", "2026-09-01T05:00:01Z"],
        )
        # returned list is always ascending
        ts = [r.timestamp for r in result.records]
        self.assertEqual(ts, sorted(ts))

    def test_limit_nonpositive_is_noop(self) -> None:
        result = self._query(limit=0)
        self.assertEqual(len(result.records), 5)
        result = self._query(limit=-3)
        self.assertEqual(len(result.records), 5)

    def test_sort_tie_break_deterministic(self) -> None:
        claude_dir = self.root / "claude" / "proj"
        self._write_jsonl(
            claude_dir / "c3.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": "c_tie",
                    "cwd": "/x",
                    "timestamp": "2026-08-30T05:00:01.000Z",
                    "message": {"role": "user", "content": "tie claude"},
                },
            ],
        )
        result = self._query()
        tie = [r for r in result.records if r.timestamp == "2026-08-30T05:00:01Z"]
        self.assertEqual([r.harness for r in tie], ["claude", "pi"])

    def test_skipped_record_malformed_line(self) -> None:
        pi_dir = self.root / "pi" / "sess"
        with (pi_dir / "pi_bad.jsonl").open("w", encoding="utf-8") as f:
            f.write("{not json}\n")
            f.write(json.dumps({
                "type": "message",
                "timestamp": "2026-09-02T00:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "survivor"}],
                },
            }) + "\n")
            f.write("{still not json}\n")
        result = self._query()
        decodes = [
            s for s in result.skipped_records if s.kind == "decode"
        ]
        self.assertEqual(len(decodes), 2)
        self.assertEqual(decodes[0].line_number, 1)
        self.assertEqual(decodes[0].harness, "pi")
        self.assertIn("pi_bad.jsonl", str(decodes[0].path))
        # good line still loaded
        self.assertIn(
            "survivor", [r.text for r in result.records if r.harness == "pi"]
        )

    def test_decode_cap_overflow_message_exact(self) -> None:
        pi_dir = self.root / "pi" / "sess"
        with (pi_dir / "pi_cap.jsonl").open("w", encoding="utf-8") as f:
            for _ in range(15):
                f.write("bad\n")
        result = self._query(harnesses=frozenset({"pi"}))
        decodes = [s for s in result.skipped_records if s.kind == "decode"]
        self.assertEqual(len(decodes), 11)  # 10 line-level + 1 overflow
        self.assertEqual(
            decodes[-1].reason, "5 additional invalid JSON lines"
        )
        self.assertIsNone(decodes[-1].line_number)

    def test_unreadable_file_io_skip(self) -> None:
        pi_dir = self.root / "pi" / "sess"
        locked = pi_dir / "pi_locked.jsonl"
        locked.write_text("{}\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            with contextlib.suppress(OSError):
                locked.read_text()
            if locked.stat().st_mode & 0o400:
                self.skipTest("running as root — chmod unreadable ineffective")
            result = self._query(harnesses=frozenset({"pi"}))
            io_skips = [s for s in result.skipped_records if s.kind == "io"]
            self.assertEqual(len(io_skips), 1)
        finally:
            locked.chmod(0o644)

    def test_corrupt_sqlite_db_db_skip(self) -> None:
        db = self.root / "corrupt.db"
        db.write_bytes(b"this is not a sqlite database" * 100)
        roots = dict(self.roots)
        roots["opencode"] = db
        q = analyze_sessions.SessionQuery()
        result = analyze_sessions.query_sessions(q, roots=roots)
        db_skips = [s for s in result.skipped_records if s.kind == "db"]
        self.assertEqual(len(db_skips), 1)
        self.assertEqual(db_skips[0].harness, "opencode")
        # other harnesses unaffected
        self.assertTrue(result.records)

    def test_absent_path_benign_no_diagnostic(self) -> None:
        roots = dict(self.roots)
        roots["pi"] = self.root / "does-not-exist"
        q = analyze_sessions.SessionQuery()
        result = analyze_sessions.query_sessions(q, roots=roots)
        self.assertNotIn("pi", {r.harness for r in result.records})
        self.assertEqual(result.skipped_records, [])

    def test_explicit_roots_missing_key_raises(self) -> None:
        q = analyze_sessions.SessionQuery(harnesses=frozenset({"pi"}))
        with self.assertRaises(ValueError):
            analyze_sessions.query_sessions(q, roots={"claude": self.root})
        # roots=None never raises for missing keys
        result = analyze_sessions.query_sessions(q, roots=None)
        self.assertIsInstance(result, analyze_sessions.SessionQueryResult)

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._query(since=datetime(2026, 8, 30))
        with self.assertRaises(ValueError):
            self._query(until=datetime(2026, 8, 30))

    def test_unknown_role_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._query(role="USER")

    def test_empty_result_zero_summary_shape(self) -> None:
        roots = {"pi": self.root / "nope"}
        q = analyze_sessions.SessionQuery(harnesses=frozenset({"pi"}))
        result = analyze_sessions.query_sessions(q, roots=roots)
        self.assertEqual(result.records, [])

    def test_load_all_records_shim_parity(self) -> None:
        legacy = analyze_sessions.load_all_records(
            harness="all",
            pi_dir=self.roots["pi"],
            claude_dir=self.roots["claude"],
            opencode_db=self.roots["opencode"],
            copilot_db=self.roots["copilot"],
        )
        result = self._query(include_subagents=True)
        self.assertIsInstance(legacy, list)
        self.assertEqual(
            [(r.harness, r.session_id, r.timestamp) for r in legacy],
            [(r.harness, r.session_id, r.timestamp) for r in result.records],
        )

    def test_aggregate_cost_parity_with_old_inline(self) -> None:
        records = [
            analyze_sessions.SessionRecord(
                harness="pi", session_id="p1", cwd=None,
                timestamp="2026-08-30T10:00:00Z", role="assistant",
                text="x", model="claude-3-5-sonnet",
                input_tokens=1000, output_tokens=100,
                cache_read_tokens=200, cost_usd=0.005, cost_origin="native",
            ),
            analyze_sessions.SessionRecord(
                harness="pi", session_id="p1", cwd=None,
                timestamp="2026-08-30T10:00:05Z", role="user",
                text="y", model=None,
            ),
            analyze_sessions.SessionRecord(
                harness="claude", session_id="c1", cwd=None,
                timestamp="2026-08-31T10:00:00Z", role="assistant",
                text="z", model="claude-opus-5",
                input_tokens=5000, output_tokens=500,
                cache_write_tokens=1000, cost_usd=0.0228, cost_origin="derived",
            ),
            analyze_sessions.SessionRecord(
                harness="copilot", session_id="c1", cwd=None,
                timestamp="2026-08-31T11:00:00Z", role="assistant",
                text="w", model="copilot-v1", cost_origin="unavailable",
            ),
        ]
        cs = analyze_sessions.aggregate_cost(records)
        # expected from today's inlined aggregation logic
        self.assertEqual(cs.sessions, 2)  # {p1, c1} — cross-harness id dedupe preserved
        self.assertEqual(cs.messages, 4)
        self.assertEqual(cs.input_tokens, 6000)
        self.assertEqual(cs.output_tokens, 600)
        self.assertEqual(cs.cache_read_tokens, 200)
        self.assertEqual(cs.cache_write_tokens, 1000)
        self.assertEqual(cs.total_tokens, 7800)
        self.assertAlmostEqual(cs.cost_usd or 0, 0.0278, places=6)
        # origins: cost-bearing records are pi (native) + claude (derived);
        # the copilot record has no tokens and no cost, so it is not
        # cost-bearing and doesn't make the origin "partial".
        self.assertEqual(cs.cost_origin, "native+derived")
        empty = analyze_sessions.aggregate_cost([])
        self.assertIsNone(empty.cost_usd)
        self.assertEqual(empty.cost_origin, "unavailable")
        self.assertEqual(empty.sessions, 0)

    def test_cli_cost_json_fixture(self) -> None:
        argv = ["cost", "--by", "harness", "--json"]
        with mock.patch.object(
            analyze_sessions, "_resolve_cli_roots", return_value=self.roots
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = analyze_sessions.main(argv)
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        harnesses = {row["group"]: row for row in data}
        self.assertEqual(set(harnesses), {"pi", "claude"})
        self.assertEqual(harnesses["pi"]["cost_origin"], "native")
        self.assertEqual(harnesses["claude"]["cost_origin"], "derived")

    def test_cli_prompts_jsonl_and_search(self) -> None:
        with mock.patch.object(
            analyze_sessions, "_resolve_cli_roots", return_value=self.roots
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = analyze_sessions.main(["prompts", "--format", "jsonl"])
            self.assertEqual(rc, 0)
            lines = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0]["prompt"], "Hello pi agent!")
            # pi_2 (2026-09-01) is the latest prompt
            self.assertEqual(lines[-1]["prompt"], "Second prompt")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = analyze_sessions.main(["search", "optimized", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["harness"], "claude")

    def test_cli_invalid_regex_exits_1_with_message(self) -> None:
        for argv, bad in (
            (["prompts", "--grep", "("], "("),
            (["search", "(", "--regex"], "("),
        ):
            with mock.patch.object(
                analyze_sessions, "_resolve_cli_roots", return_value=self.roots
            ):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = analyze_sessions.main(argv)
                self.assertEqual(rc, 1)
                self.assertIn(f"Error: invalid regex '{bad}':", err.getvalue())

    def test_cli_verbose_reports_skips_on_stderr(self) -> None:
        pi_dir = self.root / "pi" / "sess"
        with (pi_dir / "pi_vbad.jsonl").open("w", encoding="utf-8") as f:
            f.write("{nope}\n")
        with mock.patch.object(
            analyze_sessions, "_resolve_cli_roots", return_value=self.roots
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = analyze_sessions.main(["cost", "--by", "total", "-v"])
            self.assertEqual(rc, 0)
            self.assertIn("skipped pi", err.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = analyze_sessions.main(["cost", "--by", "total"])
            self.assertEqual(rc, 0)
            self.assertEqual(err.getvalue(), "")


class TestCliRootResolution(unittest.TestCase):
    def test_resolve_cli_roots_defaults_none(self) -> None:
        self.assertIsNone(analyze_sessions._resolve_cli_roots())


if __name__ == "__main__":
    unittest.main()
