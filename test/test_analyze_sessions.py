#!/usr/bin/env python3
"""test_analyze_sessions.py — unit tests for analyze_sessions.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import analyze_sessions  # noqa: E402


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

    def test_copilot_adapter_derived_cost(self) -> None:
        db_path = self.root / "session-store-derived.db"
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
            "INSERT INTO sessions VALUES ('cop_sess2', '/home/user/proj', 'myrepo', 'main', '2026-08-31 10:00:00', '2026-08-31 10:05:00')"
        )
        cursor.execute(
            "INSERT INTO turns VALUES (1, 'cop_sess2', 1, 'Hello', 'Hi there', '2026-08-31 10:00:05')"
        )
        cursor.execute(
            "INSERT INTO assistant_usage_events VALUES (1, 'cop_sess2', 1, 'gpt-4o', 1000000, 100000, 500000, 0)"
        )
        conn.commit()
        conn.close()

        records, _skipped = analyze_sessions.load_copilot_records(db_path=db_path)
        self.assertEqual(len(records), 2)
        a_rec = records[1]
        self.assertEqual(a_rec.role, "assistant")
        self.assertEqual(a_rec.model, "gpt-4o")
        # 1M * 2.50 + 0.1M * 10.0 + 0.5M * 1.25 = 2.50 + 1.00 + 0.625 = 4.125
        self.assertAlmostEqual(a_rec.cost_usd or 0, 4.125)
        self.assertEqual(a_rec.cost_origin, "derived")

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

    def test_codex_adapter(self) -> None:
        codex_dir = (
            self.root
            / ".codex"
            / "sessions"
            / "2026"
            / "09"
            / "22"
        )
        codex_dir.mkdir(parents=True)
        session_file = (
            codex_dir
            / "rollout-2026-09-22T14-37-24-01a0ca68-8b84-7371-958f-f50b1431b709.jsonl"
        )
        lines = [
            {
                "timestamp": "2026-09-22T14:37:24.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "01a0ca68-8b84-7371-958f-f50b1431b709",
                    "cwd": "/home/user/codexproj",
                    "source": "cli",
                },
            },
            {
                "timestamp": "2026-09-22T14:37:24.100Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn_1",
                    "model": "gpt-5.6-terra",
                    "cwd": "/home/user/codexproj",
                },
            },
            {
                "timestamp": "2026-09-22T14:37:24.200Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "system prompt"}],
                },
            },
            {
                "timestamp": "2026-09-22T14:37:25.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Run tests and refactor"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn_1",
                    },
                },
            },
            {
                "timestamp": "2026-09-22T14:37:26.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I will examine the codebase."}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn_1",
                    },
                },
            },
            {
                "timestamp": "2026-09-22T14:37:27.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                },
            },
            {
                "timestamp": "2026-09-22T14:37:27.100Z",
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn_1",
                    "usage": {
                        "input_tokens": 10000,
                        "cached_input_tokens": 4000,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 200,
                        "total_tokens": 10200,
                    },
                },
            },
            {
                "timestamp": "2026-09-22T14:37:30.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Tests passed and refactored."}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn_1",
                    },
                },
            },
            {
                "timestamp": "2026-09-22T14:37:30.100Z",
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn_1",
                    "usage": {
                        "input_tokens": 15000,
                        "cached_input_tokens": 5000,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 800,
                        "total_tokens": 15800,
                    },
                },
            },
        ]
        with session_file.open("w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")

        records, skipped = analyze_sessions.load_codex_records(
            base_dir=self.root / ".codex" / "sessions"
        )
        self.assertEqual(skipped, [])
        self.assertEqual(len(records), 3)

        u_rec = records[0]
        self.assertEqual(u_rec.role, "user")
        self.assertEqual(u_rec.text, "Run tests and refactor")
        self.assertEqual(u_rec.session_id, "01a0ca68-8b84-7371-958f-f50b1431b709")
        self.assertEqual(u_rec.cwd, "/home/user/codexproj")
        self.assertFalse(u_rec.is_subagent)
        self.assertEqual(u_rec.input_tokens, 0)
        self.assertEqual(u_rec.output_tokens, 0)

        a1_rec = records[1]
        self.assertEqual(a1_rec.role, "assistant")
        self.assertEqual(a1_rec.text, "I will examine the codebase.")
        self.assertEqual(a1_rec.model, "gpt-5.6-terra")
        self.assertEqual(a1_rec.input_tokens, 0)
        self.assertEqual(a1_rec.output_tokens, 0)

        a2_rec = records[2]
        self.assertEqual(a2_rec.role, "assistant")
        self.assertEqual(a2_rec.text, "Tests passed and refactored.")
        self.assertEqual(a2_rec.model, "gpt-5.6-terra")
        self.assertEqual(a2_rec.input_tokens, 16000)
        self.assertEqual(a2_rec.cache_read_tokens, 9000)
        self.assertEqual(a2_rec.output_tokens, 1000)
        self.assertEqual(a2_rec.cache_write_tokens, 0)
        self.assertEqual(a2_rec.total_tokens, 26000)
        self.assertEqual(a2_rec.cost_origin, "derived")
        expected_cost = (16000 * 2.0 + 1000 * 12.0 + 9000 * 0.20) / 1_000_000.0
        self.assertAlmostEqual(a2_rec.cost_usd or 0, expected_cost, places=6)

    def test_codex_adapter_subagent_and_inline_usage(self) -> None:
        codex_dir = self.root / ".codex" / "sessions"
        codex_dir.mkdir(parents=True, exist_ok=True)
        sub_file = codex_dir / "subagent_rollout_1.jsonl"
        lines = [
            {
                "type": "session_meta",
                "payload": {"id": "sub_1", "cwd": "/work", "source": "subagent"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Subagent response"}],
                    "usage": {
                        "input_tokens": 5000,
                        "cached_input_tokens": 1000,
                        "output_tokens": 300,
                        "cost": 0.015,
                    },
                },
            },
        ]
        with sub_file.open("w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")

        records, _ = analyze_sessions.load_codex_records(base_dir=codex_dir)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertTrue(rec.is_subagent)
        self.assertEqual(rec.input_tokens, 4000)
        self.assertEqual(rec.cache_read_tokens, 1000)
        self.assertEqual(rec.output_tokens, 300)
        self.assertEqual(rec.cost_usd, 0.015)
        self.assertEqual(rec.cost_origin, "native")


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

    def test_claude_opus_5_5_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_claude_cost(
            "claude-opus-5-5-20260922", 1_000_000, 100_000, 500_000, 200_000
        )
        self.assertEqual(origin, "derived")
        # 1M * 4.0 + 0.1M * 20.0 + 0.5M * 0.20 + 0.2M * 5.0 = 4.0 + 2.0 + 0.10 + 1.0 = 7.10
        self.assertAlmostEqual(cost or 0, 7.10)

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


class TestCalculateOpenAICost(unittest.TestCase):
    def test_gpt_6_astra_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "gpt-6-astra", 1_000_000, 100_000, 500_000, 0
        )
        self.assertEqual(origin, "derived")
        # 1M * 10 + 0.1M * 50 + 0.5M * 1.0 = 10 + 5 + 0.5 = 15.5
        self.assertAlmostEqual(cost or 0, 15.5)

    def test_gpt_5_6_terra_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "gpt-5.6-terra", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost or 0, 2.0)

    def test_gpt_6_sol_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "gpt-6-sol", 1_000_000, 0, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost or 0, 2.0)

    def test_gpt_5_6_luna_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "gpt-5.6-luna", 1_000_000, 1_000_000, 0, 0
        )
        self.assertEqual(origin, "derived")
        # 1M * 0.20 + 1M * 1.20 = 1.40
        self.assertAlmostEqual(cost or 0, 1.40)

    def test_o3_mini_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "o3-mini", 1_000_000, 1_000_000, 0, 0
        )
        self.assertEqual(origin, "derived")
        self.assertAlmostEqual(cost or 0, 5.50)

    def test_o3_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "o3", 1_000_000, 100_000, 500_000, 0
        )
        self.assertEqual(origin, "derived")
        # 1M * 2.0 + 0.1M * 8.0 + 0.5M * 0.50 = 2.0 + 0.8 + 0.25 = 3.05
        self.assertAlmostEqual(cost or 0, 3.05)

    def test_gpt_5_mini_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "gpt-5-mini", 1_000_000, 1_000_000, 1_000_000, 0
        )
        self.assertEqual(origin, "derived")
        # 1M * 0.25 + 1M * 2.0 + 1M * 0.025 = 0.25 + 2.0 + 0.025 = 2.275
        self.assertAlmostEqual(cost or 0, 2.275)

    def test_gpt_4_5_rate(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "gpt-4.5-preview", 1_000_000, 100_000, 500_000, 0
        )
        self.assertEqual(origin, "derived")
        # 1M * 75 + 0.1M * 150 + 0.5M * 37.5 = 75 + 15 + 18.75 = 108.75
        self.assertAlmostEqual(cost or 0, 108.75)

    def test_unrecognized_model_stays_unavailable(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(
            "claude-opus-5", 1_000_000, 0, 0, 0
        )
        self.assertIsNone(cost)
        self.assertEqual(origin, "unavailable")

    def test_none_model_stays_unavailable(self) -> None:
        cost, origin = analyze_sessions.calculate_openai_cost(None, 1_000_000, 0, 0, 0)
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
            "codex": self.root / "none-codex",
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

    def test_codex_query(self) -> None:
        codex_root = self.root / "codex-sessions"
        sess_dir = codex_root / "2026" / "09" / "22"
        sess_dir.mkdir(parents=True)
        self._write_jsonl(
            sess_dir / "rollout-codex1.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {"id": "codex_1", "cwd": "/work"},
                },
                {
                    "type": "turn_context",
                    "payload": {"model": "gpt-6-sol", "cwd": "/work"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Codex prompt"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Codex answer"}],
                    },
                },
            ],
        )
        roots = dict(self.roots)
        roots["codex"] = codex_root
        q = analyze_sessions.SessionQuery(harnesses=frozenset({"codex"}))
        result = analyze_sessions.query_sessions(q, roots=roots)
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].text, "Codex prompt")
        self.assertEqual(result.records[0].harness, "codex")
        self.assertEqual(result.records[1].text, "Codex answer")
        self.assertEqual(result.records[1].model, "gpt-6-sol")

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
        # chmod 0o000 correctly clears the read bit even as root -- it's the
        # *read itself* that root bypasses (CAP_DAC_OVERRIDE), so checking
        # the resulting stat mode never detects root and this skip must
        # check the effective uid directly instead.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root — chmod unreadable ineffective")
        pi_dir = self.root / "pi" / "sess"
        locked = pi_dir / "pi_locked.jsonl"
        locked.write_text("{}\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
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
            codex_dir=self.roots["codex"],
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




def _pi_msg(role: str, ts: str | None, cost: object = None, **extra: object) -> dict:
    message: dict[str, object] = {"role": role, "content": []}
    if cost is not None:
        message["usage"] = {"input": 1, "output": 1, "cost": cost}
    rec: dict[str, object] = {"type": "message", "message": message, **extra}
    if ts is not None:
        rec["timestamp"] = ts
    return rec


def _write_jsonl(path: Path, records: list[object], tail: str = "\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(r if isinstance(r, str) else json.dumps(r) for r in records)
    path.write_text(body + tail if records else "", encoding="utf-8")
    return path


class TestPiUsageCost(unittest.TestCase):
    def test_accepts_number_and_total_dict(self) -> None:
        self.assertEqual(analyze_sessions._pi_usage_cost({"cost": 0.25}), 0.25)
        self.assertEqual(analyze_sessions._pi_usage_cost({"cost": {"total": 2}}), 2.0)
        self.assertEqual(analyze_sessions._pi_usage_cost({"cost": 0}), 0.0)

    def test_rejects_everything_else(self) -> None:
        for usage in (
            None,
            [],
            {},
            {"cost": "0.5"},
            {"cost": True},
            {"cost": {"total": False}},
            {"cost": -0.1},
            {"cost": {"total": -1}},
            {"cost": float("nan")},
            {"cost": float("inf")},
            {"cost": {"total": "1"}},
            {"cost": 10**400},
        ):
            with self.subTest(usage=usage):
                self.assertIsNone(analyze_sessions._pi_usage_cost(usage))


class TestPiSessionCost(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sums_assistant_costs_and_ignores_other_records(self) -> None:
        p = _write_jsonl(
            self.root / "s.jsonl",
            [
                {"type": "session", "id": "x"},
                {"type": "model_change", "modelId": "m"},
                _pi_msg("user", "2026-10-15T10:00:00Z"),
                _pi_msg("toolResult", "2026-10-15T10:00:01Z"),
                _pi_msg("assistant", "2026-10-15T10:00:02Z", {"total": 0.5}),
                _pi_msg("assistant", "2026-10-15T10:00:03Z", 0.25),
            ],
        )
        got = analyze_sessions.pi_session_cost(p)
        self.assertEqual(got, analyze_sessions.PiCost(0.75, True, ()))

    def test_torn_unterminated_tail_is_not_a_problem(self) -> None:
        p = _write_jsonl(
            self.root / "s.jsonl",
            [_pi_msg("assistant", "2026-10-15T10:00:02Z", 1.0), '{"type": "mess'],
            tail="",
        )
        self.assertEqual(
            analyze_sessions.pi_session_cost(p), analyze_sessions.PiCost(1.0, True, ())
        )

    def test_interior_corrupt_line_is_a_problem(self) -> None:
        p = _write_jsonl(
            self.root / "s.jsonl",
            ['{"type": "mess', _pi_msg("assistant", "2026-10-15T10:00:02Z", 1.0)],
        )
        got = analyze_sessions.pi_session_cost(p)
        self.assertFalse(got.complete)
        self.assertEqual(got.usd, 1.0)
        self.assertEqual(len(got.problems), 1)

    def test_assistant_without_cost_and_non_dict_usage_are_problems(self) -> None:
        bad_usage = _pi_msg("assistant", "2026-10-15T10:00:02Z")
        bad_usage["message"]["usage"] = "nope"
        p = _write_jsonl(
            self.root / "s.jsonl",
            [
                _pi_msg("assistant", "2026-10-15T10:00:01Z"),
                bad_usage,
                {"type": "message", "message": "not-a-dict"},
                _pi_msg("assistant", "2026-10-15T10:00:03Z", -1),
                _pi_msg("assistant", "2026-10-15T10:00:04Z", True),
            ],
        )
        got = analyze_sessions.pi_session_cost(p)
        self.assertFalse(got.complete)
        self.assertEqual(len(got.problems), 5)
        self.assertEqual(got.usd, 0.0)

    def test_non_object_json_line_is_a_problem(self) -> None:
        p = _write_jsonl(self.root / "s.jsonl", ["[1, 2]"])
        self.assertFalse(analyze_sessions.pi_session_cost(p).complete)

    def test_unreadable_file_is_incomplete_and_never_raises(self) -> None:
        got = analyze_sessions.pi_session_cost(self.root / "missing.jsonl")
        self.assertFalse(got.complete)
        self.assertEqual(got.usd, 0.0)
        self.assertTrue(got.problems)

    def test_overflowing_sum_is_json_safe(self) -> None:
        p = _write_jsonl(
            self.root / "s.jsonl",
            [
                _pi_msg("assistant", "2026-10-15T10:00:01Z", 1.7e308),
                _pi_msg("assistant", "2026-10-15T10:00:02Z", 1.7e308),
            ],
        )
        got = analyze_sessions.pi_session_cost(p)
        self.assertIsNone(got.usd)
        self.assertFalse(got.complete)
        self.assertIn("overflow", got.problems)


class TestPiCostSince(unittest.TestCase):
    SINCE = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _touch(self, p: Path, when: datetime) -> None:
        os.utime(p, (when.timestamp(), when.timestamp()))

    def test_missing_base_dir_is_complete_zero(self) -> None:
        self.assertEqual(
            analyze_sessions.pi_cost_since(self.SINCE, self.base),
            analyze_sessions.PiCost(0.0, True, ()),
        )

    def test_window_counts_only_in_window_assistant_messages(self) -> None:
        a = _write_jsonl(
            self.base / "proj-a" / "a.jsonl",
            [
                _pi_msg("assistant", "2026-10-15T03:59:59Z", 5.0),
                _pi_msg("assistant", "2026-10-15T04:00:00Z", 1.0),
            ],
        )
        b = _write_jsonl(
            self.base / "proj-b" / "subagent" / "b.jsonl",
            [_pi_msg("assistant", "2026-10-15T09:00:00+00:00", 0.5)],
        )
        for p in (a, b):
            self._touch(p, datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        got = analyze_sessions.pi_cost_since(self.SINCE, self.base)
        self.assertEqual(got, analyze_sessions.PiCost(1.5, True, ()))

    def test_file_older_than_window_is_not_opened(self) -> None:
        old = _write_jsonl(
            self.base / "p" / "old.jsonl",
            [_pi_msg("assistant", "2026-10-15T09:00:00Z", 9.0)],
        )
        self._touch(old, datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
        with mock.patch.object(
            analyze_sessions, "_scan_pi_file", wraps=analyze_sessions._scan_pi_file
        ) as spy:
            got = analyze_sessions.pi_cost_since(self.SINCE, self.base)
        spy.assert_not_called()
        self.assertEqual(got, analyze_sessions.PiCost(0.0, True, ()))

    def test_undated_cost_bearing_message_is_a_problem_not_counted(self) -> None:
        p = _write_jsonl(
            self.base / "p" / "u.jsonl",
            [
                _pi_msg("assistant", None, 3.0),
                _pi_msg("assistant", "garbage", 3.0),
                _pi_msg("assistant", 10**30, 3.0),
                _pi_msg("assistant", "2026-10-15T09:00:00Z", 1.0),
            ],
        )
        self._touch(p, datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        got = analyze_sessions.pi_cost_since(self.SINCE, self.base)
        self.assertEqual(got.usd, 1.0)
        self.assertFalse(got.complete)
        self.assertEqual(len(got.problems), 3)
        self.assertTrue(all(pr.startswith("u.jsonl") for pr in got.problems))

    def test_unreadable_subdirectory_is_a_problem(self) -> None:
        good = _write_jsonl(
            self.base / "ok" / "g.jsonl",
            [_pi_msg("assistant", "2026-10-15T09:00:00Z", 1.0)],
        )
        self._touch(good, datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        locked = self.base / "locked"
        locked.mkdir()

        real_walk = os.walk

        def walk(top: object, onerror: object = None, **kw: object) -> object:
            for item in real_walk(top, onerror=onerror, **kw):
                yield item
            onerror(PermissionError(13, "Permission denied", str(locked)))

        with mock.patch.object(analyze_sessions.os, "walk", walk):
            got = analyze_sessions.pi_cost_since(self.SINCE, self.base)
        self.assertEqual(got.usd, 1.0)
        self.assertFalse(got.complete)
        self.assertEqual(len(got.problems), 1)

    def test_vanished_entry_is_silently_skipped(self) -> None:
        p = _write_jsonl(
            self.base / "p" / "v.jsonl",
            [_pi_msg("assistant", "2026-10-15T09:00:00Z", 1.0)],
        )
        self._touch(p, datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        real_walk = os.walk

        def walk(top: object, onerror: object = None, **kw: object) -> object:
            for dirpath, dirs, files in real_walk(top, onerror=onerror, **kw):
                yield dirpath, dirs, [*files, "gone.jsonl"]
            onerror(FileNotFoundError(2, "No such file", "x"))

        with mock.patch.object(analyze_sessions.os, "walk", walk):
            got = analyze_sessions.pi_cost_since(self.SINCE, self.base)
        self.assertEqual(got, analyze_sessions.PiCost(1.0, True, ()))


class TestPiCostSinceWindowFirst(unittest.TestCase):
    def test_out_of_window_message_without_cost_is_not_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "sessions"
            p = _write_jsonl(
                base / "p" / "resumed.jsonl",
                [
                    _pi_msg("assistant", "2026-10-14T12:00:00Z"),
                    _pi_msg("assistant", "2026-10-15T09:00:00Z", 1.0),
                ],
            )
            stamp = datetime(2026, 10, 15, 12, 0, tzinfo=UTC).timestamp()
            os.utime(p, (stamp, stamp))
            got = analyze_sessions.pi_cost_since(TestPiCostSince.SINCE, base)
        self.assertEqual(got, analyze_sessions.PiCost(1.0, True, ()))


class TestPiCostSinceBaseDirErrors(unittest.TestCase):
    def test_untraversable_base_dir_is_incomplete_not_a_crash(self) -> None:
        base = Path(tempfile.mkdtemp()) / "sessions"
        with mock.patch.object(
            Path, "stat", side_effect=PermissionError(13, "Permission denied")
        ):
            got = analyze_sessions.pi_cost_since(TestPiCostSince.SINCE, base)
        self.assertFalse(got.complete)
        self.assertEqual(got.usd, 0.0)


# ── launch-prompt slug extraction (cost --by item) ───────────────────────────


def _expanded_template(template_name: str, invocation: str) -> str:
    """Expand the repo's real backlog-item template with $ARGUMENTS.

    Built from the real template so an anchor change breaks these tests
    loudly instead of silently mis-attributing every worker session.
    """
    template = (
        Path(__file__).resolve().parent.parent / "templates" / template_name
    )
    return template.read_text(encoding="utf-8").replace("$ARGUMENTS", invocation)


def _generated_skill(relpath: str) -> str:
    """A real generated skill copy (the expansion a claude-style harness logs)."""
    return (Path(__file__).resolve().parent.parent / relpath).read_text(
        encoding="utf-8"
    )


class TestLaunchSlugFromPrompt(unittest.TestCase):
    def test_raw_invocation_with_auto_and_handoff(self) -> None:
        self.assertEqual(
            analyze_sessions.launch_slug_from_prompt(
                "/backlog-item --auto atk-foo --handoff"
            ),
            "atk-foo",
        )

    def test_raw_invocation_plain(self) -> None:
        self.assertEqual(
            analyze_sessions.launch_slug_from_prompt("/backlog-item atk-foo"),
            "atk-foo",
        )

    def test_codex_dollar_form(self) -> None:
        self.assertEqual(
            analyze_sessions.launch_slug_from_prompt(
                "$backlog-item --auto atk-foo --handoff"
            ),
            "atk-foo",
        )

    def test_expanded_pi_template(self) -> None:
        text = _expanded_template(
            "backlog_item_pi_native.md.tmpl", "--auto atk-foo --handoff"
        )
        self.assertEqual(analyze_sessions.launch_slug_from_prompt(text), "atk-foo")

    def test_claude_style_expansion_is_inert(self) -> None:
        # The claude/copilot-style skill embeds no invocation and only
        # backticked placeholder examples; without a typed line in the
        # message there is nothing to attribute.
        text = _generated_skill("copilot/skills/backlog-item/SKILL.md")
        self.assertIsNone(analyze_sessions.launch_slug_from_prompt(text))

    def test_claude_style_expansion_with_appended_typed_line(self) -> None:
        # Harnesses that log the typed line alongside the expansion.
        text = (
            _generated_skill("copilot/skills/backlog-item/SKILL.md")
            + "\n/backlog-item --auto atk-foo --handoff"
        )
        self.assertEqual(analyze_sessions.launch_slug_from_prompt(text), "atk-foo")

    def test_expanded_template_with_numeric_target(self) -> None:
        text = _expanded_template("backlog_item_pi_native.md.tmpl", "--auto 4")
        self.assertEqual(analyze_sessions.launch_slug_from_prompt(text), "4")

    def test_template_placeholder_mentions_are_never_parsed(self) -> None:
        text = "running `/backlog-item --auto <slug>` and `/backlog-item <slug|N>`"
        self.assertIsNone(analyze_sessions.launch_slug_from_prompt(text))

    def test_queue_invocations_have_no_item_target(self) -> None:
        for invocation in (
            "/backlog-item --swarm=3 --prefix atk",
            "/backlog-item --swarm --prefix atk",
            "/backlog-item --serial --prefix atk",
        ):
            with self.subTest(invocation=invocation):
                self.assertIsNone(
                    analyze_sessions.launch_slug_from_prompt(invocation)
                )

    def test_resume_tail_does_not_replace_the_target(self) -> None:
        self.assertEqual(
            analyze_sessions.launch_slug_from_prompt(
                "/backlog-item --auto atk-foo --handoff resume run-1 --prefix atk"
            ),
            "atk-foo",
        )

    def test_no_invocation_yields_none(self) -> None:
        self.assertIsNone(
            analyze_sessions.launch_slug_from_prompt("just a normal prompt")
        )

    def test_expanded_template_without_target_is_unattributed(self) -> None:
        text = _expanded_template("backlog_item_pi_native.md.tmpl", "")
        self.assertIsNone(analyze_sessions.launch_slug_from_prompt(text))


class TestLoadLaunchSlugs(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_pi_session(self, name: str, prompt: str | None) -> None:
        d = self.root / "pi"
        d.mkdir(parents=True, exist_ok=True)
        lines: list[dict[str, object]] = [
            {"type": "session", "id": name, "cwd": "/repo", "timestamp": "2026-11-03T12:00:00Z"}
        ]
        if prompt is not None:
            lines.append(
                {
                    "type": "message",
                    "timestamp": "2026-11-03T12:00:01Z",
                    "message": {"role": "user", "content": prompt},
                }
            )
        lines.append(
            {
                "type": "message",
                "timestamp": "2026-11-03T12:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input": 1, "output": 1, "cost": {"total": 0.5}},
                },
            }
        )
        with (d / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def test_pi_sessions_map_to_their_slugs(self) -> None:
        self._write_pi_session(
            "s1", _expanded_template("backlog_item_pi_native.md.tmpl", "--auto atk-foo --handoff")
        )
        self._write_pi_session("s2", "hello there")
        got = analyze_sessions.load_launch_slugs({"pi": self.root / "pi"})
        self.assertEqual(got, {("pi", "s1"): "atk-foo"})

    def test_first_user_message_is_the_launch_prompt(self) -> None:
        # A later /backlog-item invocation must not re-attribute the session.
        d = self.root / "pi"
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            {"type": "session", "id": "s1", "cwd": "/repo", "timestamp": "2026-11-03T12:00:00Z"},
            {"type": "message", "timestamp": "2026-11-03T12:00:01Z",
             "message": {"role": "user", "content": "fix the bug"}},
            {"type": "message", "timestamp": "2026-11-03T12:05:00Z",
             "message": {"role": "user", "content": "/backlog-item atk-late"}},
        ]
        with (d / "s1.jsonl").open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        got = analyze_sessions.load_launch_slugs({"pi": self.root / "pi"})
        self.assertEqual(got, {})

    def test_claude_opencode_copilot_agy_codex_attribution(self) -> None:
        # claude: expansion with the typed invocation line appended
        cd = self.root / "claude" / "proj"
        cd.mkdir(parents=True)
        lines = [
            {"type": "user", "sessionId": "c1", "cwd": "/repo",
             "timestamp": "2026-11-03T12:00:00Z", "isSidechain": False,
             "message": {"role": "user",
                         "content": _generated_skill("copilot/skills/backlog-item/SKILL.md")
                                    + "\n/backlog-item --auto atk-foo"}},
        ]
        with (cd / "c1.jsonl").open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        # opencode: first user message parts
        db_path = self.root / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER)")
        cur.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
        cur.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        cur.execute("INSERT INTO session VALUES ('o1', NULL, '/repo', 1, 2)")
        cur.execute("INSERT INTO message VALUES ('m1', 'o1', 1, ?)", (json.dumps({"role": "user"}),))
        cur.execute("INSERT INTO part VALUES ('p1', 'm1', 'o1', 1, ?)",
                    (json.dumps({"type": "text", "text": "/backlog-item --auto atk-foo"}),))
        conn.commit()
        conn.close()
        # copilot: first turn's user_message
        cp_path = self.root / "session-store.db"
        conn = sqlite3.connect(str(cp_path))
        cur = conn.cursor()
        cur.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, branch TEXT, created_at TEXT, updated_at TEXT)")
        cur.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT)")
        cur.execute("INSERT INTO sessions VALUES ('cp1', '/repo', 'r', 'main', '2026-11-03 12:00:00', '2026-11-03 12:05:00')")
        cur.execute("INSERT INTO turns VALUES (1, 'cp1', 1, '/backlog-item --auto atk-foo', 'ok', '2026-11-03 12:00:05')")
        conn.commit()
        conn.close()
        # agy: first USER_INPUT content
        ad = self.root / "agy" / "sess-a" / ".system_generated" / "logs"
        ad.mkdir(parents=True)
        with (ad / "transcript_full.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "USER_INPUT", "content": "/backlog-item --auto atk-foo", "created_at": "2026-11-03T12:00:00Z"}) + "\n")
        # codex: first user response_item
        cdx = self.root / "codex"
        cdx.mkdir(parents=True)
        with (cdx / "rollout-1.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-11-03T12:00:00Z", "type": "session_meta",
                                "payload": {"id": "x1", "cwd": "/repo"}}) + "\n")
            f.write(json.dumps({"timestamp": "2026-11-03T12:00:01Z", "type": "response_item",
                                "payload": {"type": "message", "role": "user",
                                            "content": [{"type": "input_text", "text": "$backlog-item --auto atk-foo"}]}}) + "\n")

        got = analyze_sessions.load_launch_slugs(
            {
                "claude": self.root / "claude",
                "opencode": db_path,
                "copilot": cp_path,
                "agy": self.root / "agy",
                "codex": cdx,
            }
        )
        self.assertEqual(got[("claude", "c1")], "atk-foo")
        self.assertEqual(got[("opencode", "o1")], "atk-foo")
        self.assertEqual(got[("copilot", "cp1")], "atk-foo")
        self.assertEqual(got[("agy", "sess-a")], "atk-foo")
        self.assertEqual(got[("codex", "x1")], "atk-foo")


class TestCostByItem(unittest.TestCase):
    def _rec(self, harness: str, sid: str, cost: float, ts: str) -> analyze_sessions.SessionRecord:
        return analyze_sessions.SessionRecord(
            harness=harness,
            session_id=sid,
            cwd="/repo",
            timestamp=ts,
            role="assistant",
            text="work",
            cost_usd=cost,
            cost_origin="native",
        )

    def test_groups_by_slug_and_sums_across_relaunches(self) -> None:
        records = [
            self._rec("pi", "r1", 0.10, "2026-11-03T12:00:00Z"),
            self._rec("pi", "r2", 0.20, "2026-11-03T13:00:00Z"),
            self._rec("pi", "other", 0.05, "2026-11-03T14:00:00Z"),
        ]
        slugs = {("pi", "r1"): "atk-foo", ("pi", "r2"): "atk-foo"}
        args = type("Args", (), {"by": "item", "json": True, "quiet": False, "no_subagents": False})()
        buf = io.StringIO()
        rc = analyze_sessions.cmd_cost(args, records, launch_slugs=slugs, file=buf)
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        by_group = {r["group"]: r for r in rows}
        self.assertAlmostEqual(by_group["atk-foo"]["cost_usd"], 0.3, places=4)
        self.assertEqual(by_group["atk-foo"]["sessions"], 2)
        self.assertAlmostEqual(by_group["unattributed"]["cost_usd"], 0.05, places=4)

    def test_no_map_means_everything_unattributed(self) -> None:
        records = [self._rec("pi", "r1", 0.10, "2026-11-03T12:00:00Z")]
        args = type("Args", (), {"by": "item", "json": True, "quiet": False, "no_subagents": False})()
        buf = io.StringIO()
        analyze_sessions.cmd_cost(args, records, launch_slugs=None, file=buf)
        rows = json.loads(buf.getvalue())
        self.assertEqual(rows[0]["group"], "unattributed")


# ── milestones (time and spend to first file write) ───────────────────────


class TestMilestonesPi(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_pi_session(
        self, name: str, events: list[dict[str, object]], cwd: str | None = None
    ) -> None:
        d = self.root / "pi"
        d.mkdir(parents=True, exist_ok=True)
        head: list[dict[str, object]] = [
            {
                "type": "session",
                "id": name,
                "timestamp": "2026-11-03T12:00:00Z",
                **({"cwd": cwd} if cwd is not None else {}),
            }
        ]
        with (d / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for line in head + events:
                f.write(json.dumps(line) + "\n")

    @staticmethod
    def _msg(
        ts: str,
        *,
        cost: float | None = 0.0,
        tool: tuple[str, str] | None = None,
        tokens: bool = True,
    ) -> dict[str, object]:
        content: list[dict[str, object]] = [{"type": "text", "text": "doing"}]
        if tool is not None:
            name, path = tool
            content.append(
                {"type": "toolCall", "name": name, "arguments": {"path": path}}
            )
        usage: dict[str, object] = {}
        if tokens:
            usage = {"input": 10, "output": 5}
        if cost is not None:
            usage["cost"] = {"total": cost}
        msg: dict[str, object] = {"role": "assistant", "content": content}
        if usage:
            msg["usage"] = usage
        return {"type": "message", "timestamp": ts, "message": msg}

    def test_first_write_time_spend_and_totals(self) -> None:
        self._write_pi_session(
            "s1",
            [
                self._msg("2026-11-03T12:05:00Z", cost=0.10),
                self._msg("2026-11-03T12:10:00Z", cost=0.07, tool=("write", "src/a.py")),
                self._msg("2026-11-03T12:20:00Z", cost=0.05, tool=("edit", "src/b.py")),
            ],
            cwd=str(self.repo),
        )
        rows, _skipped = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "ok")
        self.assertAlmostEqual(row.minutes_to_first_write or -1, 10.0, places=3)
        self.assertAlmostEqual(row.spend_to_first_write or -1, 0.17, places=4)
        self.assertAlmostEqual(row.total_spend or -1, 0.22, places=4)
        self.assertAlmostEqual(row.total_minutes or -1, 20.0, places=3)
        self.assertEqual(row.first_write_path, "src/a.py")
        self.assertEqual(row.first_write_cost_origin, "native")
        self.assertEqual(row.total_cost_origin, "native")

    def test_reads_never_count_as_writes(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:05:00Z", tool=("read", "src/a.py"))],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        self.assertEqual(rows[0].status, "no-write")
        self.assertIsNone(rows[0].minutes_to_first_write)
        self.assertIsNotNone(rows[0].total_spend)
        self.assertAlmostEqual(rows[0].total_spend, 0.0, places=4)

    def test_writes_outside_the_repo_do_not_count(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:05:00Z", tool=("write", "/elsewhere/x.py"))],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        self.assertEqual(rows[0].status, "no-write")

    def test_relative_paths_resolve_against_the_session_cwd(self) -> None:
        self._write_pi_session(
            "s1",
            [
                self._msg("2026-11-03T12:05:00Z", tool=("write", "../sibling/x.py")),
                self._msg("2026-11-03T12:06:00Z", tool=("write", "src/in.py")),
            ],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        # ../sibling/x.py escapes the repo; src/in.py is the first in-repo write.
        self.assertAlmostEqual(rows[0].minutes_to_first_write or -1, 6.0, places=3)
        self.assertEqual(rows[0].first_write_path, "src/in.py")

    def test_sibling_prefix_is_not_inside_the_repo(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:05:00Z", tool=("write", str(self.root / "repo-other" / "x.py")))],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        self.assertEqual(rows[0].status, "no-write")

    def test_no_containment_root_is_unmeasurable_never_zero(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:05:00Z", tool=("write", "anywhere.py"))],
            cwd=None,
        )
        rows, _ = analyze_sessions.session_milestones("pi", self.root / "pi")
        self.assertEqual(rows[0].status, "no-root")
        self.assertIsNone(rows[0].minutes_to_first_write)
        self.assertIsNone(rows[0].spend_to_first_write)

    def test_session_cwd_is_the_fallback_root(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:05:00Z", tool=("write", "src/a.py"))],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones("pi", self.root / "pi")
        self.assertEqual(rows[0].status, "ok")

    def test_model_filters_on_the_starting_model(self) -> None:
        self._write_pi_session(
            "s1",
            [
                {"type": "model_change", "timestamp": "2026-11-03T12:00:01Z", "modelId": "mimo-1"},
                self._msg("2026-11-03T12:05:00Z"),
                {"type": "model_change", "timestamp": "2026-11-03T12:06:00Z", "modelId": "mimo-2"},
                self._msg("2026-11-03T12:07:00Z", tool=("write", "src/a.py")),
            ],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        self.assertEqual(rows[0].model, "mimo-1")
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo), model="mimo-2"
        )
        self.assertEqual(rows, [])
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo), model="mimo-1"
        )
        self.assertEqual(len(rows), 1)

    def test_missing_costs_are_marked_not_hidden(self) -> None:
        self._write_pi_session(
            "s1",
            [
                self._msg("2026-11-03T12:05:00Z", cost=0.10),
                self._msg("2026-11-03T12:06:00Z", cost=None),  # charge-bearing, no cost
                self._msg("2026-11-03T12:10:00Z", cost=None, tool=("write", "src/a.py")),
            ],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        row = rows[0]
        self.assertEqual(row.first_write_cost_origin, "partial")
        self.assertEqual(row.total_cost_origin, "partial")
        self.assertAlmostEqual(row.spend_to_first_write or -1, 0.10, places=4)

    def test_unavailable_cost_window_is_blank_not_zero(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:10:00Z", cost=None, tool=("write", "src/a.py"))],
            cwd=str(self.repo),
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        self.assertEqual(rows[0].spend_to_first_write, None)
        self.assertEqual(rows[0].first_write_cost_origin, "unavailable")

    def test_since_and_until_select_whole_sessions(self) -> None:
        self._write_pi_session(
            "early",
            [self._msg("2026-11-03T12:10:00Z", tool=("write", "src/a.py"))],
            cwd=str(self.repo),
        )
        since = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo), since=since
        )
        self.assertEqual(rows, [])

    # ── single-session first-write helper (watch's planning note reuses it) ─

    def _sess_path(self, name: str) -> Path:
        return self.root / "pi" / f"{name}.jsonl"

    def test_pi_session_first_write_returns_first_raw_write_path(self) -> None:
        self._write_pi_session(
            "s1",
            [
                self._msg("2026-11-03T12:05:00Z", cost=0.10),
                self._msg("2026-11-03T12:10:00Z", cost=0.07, tool=("write", "src/a.py")),
                self._msg("2026-11-03T12:20:00Z", cost=0.05, tool=("edit", "src/b.py")),
            ],
            cwd=str(self.repo),
        )
        self.assertEqual(
            analyze_sessions.pi_session_first_write(self._sess_path("s1")), "src/a.py"
        )

    def test_pi_session_first_write_no_write_returns_none(self) -> None:
        self._write_pi_session("s1", [self._msg("2026-11-03T12:05:00Z", cost=0.10)])
        self.assertIsNone(
            analyze_sessions.pi_session_first_write(self._sess_path("s1"))
        )

    def test_pi_session_first_write_counts_writes_outside_cwd(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:10:00Z", tool=("write", "/tmp/scratch.md"))],
            cwd=str(self.repo),
        )
        # Watch's note counts any write; the milestone CLI (containment) does not.
        self.assertEqual(
            analyze_sessions.pi_session_first_write(self._sess_path("s1")),
            "/tmp/scratch.md",
        )
        rows, _ = analyze_sessions.session_milestones(
            "pi", self.root / "pi", repo=str(self.repo)
        )
        self.assertEqual(rows[0].status, "no-write")

    def test_pi_session_first_write_without_cwd_still_returns_path(self) -> None:
        self._write_pi_session(
            "s1", [self._msg("2026-11-03T12:10:00Z", tool=("write", "src/a.py"))]
        )
        self.assertEqual(
            analyze_sessions.pi_session_first_write(self._sess_path("s1")), "src/a.py"
        )

    def test_pi_session_first_write_never_raises_on_torn_or_bad_fields(self) -> None:
        d = self.root / "pi"
        d.mkdir(parents=True)
        # Torn, unterminated, malformed final line after a valid cost line.
        (d / "torn.jsonl").write_text(
            json.dumps(
                {
                    "type": "message",
                    "timestamp": "2026-11-03T12:00:00Z",
                    "message": {"role": "assistant", "usage": {"cost": {"total": 0.5}}},
                }
            )
            + "\n"
            + '{"type": "mess',
            encoding="utf-8",
        )
        self.assertIsNone(analyze_sessions.pi_session_first_write(d / "torn.jsonl"))
        # Decoded-but-malformed field: out-of-range epoch timestamp.
        (d / "badts.jsonl").write_text(
            json.dumps(
                {
                    "type": "message",
                    "timestamp": 1e300,
                    "message": {"role": "assistant", "usage": "nope"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertIsNone(analyze_sessions.pi_session_first_write(d / "badts.jsonl"))

    def test_pi_session_first_write_sees_earlier_write_after_bad_record(self) -> None:
        self._write_pi_session(
            "s1",
            [self._msg("2026-11-03T12:05:00Z", tool=("write", "src/a.py"))],
            cwd=str(self.repo),
        )
        # A decoded-but-malformed record after the write must not erase it.
        with (self.root / "pi" / "s1.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": 1e300,
                        "message": {"role": "assistant", "usage": "nope"},
                    }
                )
                + "\n"
            )
        self.assertEqual(
            analyze_sessions.pi_session_first_write(self._sess_path("s1")), "src/a.py"
        )


class TestMilestonesClaudeAndOpencode(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_claude_tool_use_write_counts_and_model_comes_from_first_model_bearing_record(self) -> None:
        d = self.root / "claude" / "proj"
        d.mkdir(parents=True)
        lines = [
            {"type": "user", "sessionId": "c1", "cwd": str(self.repo),
             "timestamp": "2026-11-03T12:00:00Z", "isSidechain": False,
             "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "sessionId": "c1", "cwd": str(self.repo),
             "timestamp": "2026-11-03T12:02:00Z", "isSidechain": False,
             "message": {"role": "assistant", "model": "claude-sonnet-5",
                         "content": [{"type": "tool_use", "name": "Write",
                                     "input": {"file_path": str(self.repo / "src" / "b.py")}}],
                         "usage": {"input_tokens": 1000, "output_tokens": 100,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        ]
        with (d / "c1.jsonl").open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        rows, _ = analyze_sessions.session_milestones(
            "claude", self.root / "claude", repo=str(self.repo)
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "ok")
        self.assertEqual(row.model, "claude-sonnet-5")
        self.assertAlmostEqual(row.minutes_to_first_write or -1, 2.0, places=3)
        self.assertAlmostEqual(row.spend_to_first_write or -1, 0.003, places=4)
        self.assertEqual(row.first_write_cost_origin, "derived")

    def test_claude_sidechain_writes_never_mark_the_parent(self) -> None:
        d = self.root / "claude" / "proj"
        d.mkdir(parents=True)
        lines = [
            {"type": "assistant", "sessionId": "c1", "cwd": str(self.repo),
             "timestamp": "2026-11-03T12:02:00Z", "isSidechain": True,
             "message": {"role": "assistant", "model": "claude-sonnet-5",
                         "content": [{"type": "tool_use", "name": "Edit",
                                     "input": {"file_path": str(self.repo / "src" / "b.py")}}],
                         "usage": {"input_tokens": 100, "output_tokens": 10}}},
        ]
        with (d / "c1.jsonl").open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        rows, _ = analyze_sessions.session_milestones(
            "claude", self.root / "claude", repo=str(self.repo)
        )
        self.assertEqual(rows[0].status, "no-write")

    def test_unknown_model_when_no_record_carries_one(self) -> None:
        d = self.root / "claude" / "proj"
        d.mkdir(parents=True)
        lines = [
            {"type": "user", "sessionId": "c1", "cwd": str(self.repo),
             "timestamp": "2026-11-03T12:00:00Z", "isSidechain": False,
             "message": {"role": "user", "content": "go"}},
        ]
        with (d / "c1.jsonl").open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        rows, _ = analyze_sessions.session_milestones(
            "claude", self.root / "claude", repo=str(self.repo)
        )
        self.assertEqual(rows[0].model, "unknown")

    def test_opencode_tool_part_edit_counts(self) -> None:
        db_path = self.root / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER)")
        cur.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
        cur.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        cur.execute("INSERT INTO session VALUES ('o1', NULL, ?, 1784831400000, 1784831450000)", (str(self.repo),))
        cur.execute("INSERT INTO message VALUES ('m1', 'o1', 1784831401000, ?)", (json.dumps({"role": "user"}),))
        cur.execute("INSERT INTO part VALUES ('p1', 'm1', 'o1', 1784831401000, ?)",
                    (json.dumps({"type": "text", "text": "go"}),))
        cur.execute("INSERT INTO message VALUES ('m2', 'o1', 1784831460000, ?)",
                    (json.dumps({"role": "assistant", "modelID": "glm-5.2", "cost": 0.01,
                                 "tokens": {"input": 10, "output": 5}}),))
        cur.execute("INSERT INTO part VALUES ('p2', 'm2', 'o1', 1784831460000, ?)",
                    (json.dumps({"type": "tool", "tool": "edit",
                                 "state": {"input": {"filePath": str(self.repo / "src" / "c.py")}}}),))
        conn.commit()
        conn.close()
        rows, _ = analyze_sessions.session_milestones(
            "opencode", db_path, repo=str(self.repo)
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "ok")
        self.assertAlmostEqual(row.minutes_to_first_write or -1, 1.0, places=3)
        self.assertAlmostEqual(row.spend_to_first_write or -1, 0.01, places=4)
        self.assertEqual(row.model, "glm-5.2")


class TestMilestonesOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _args(self, **overrides: object) -> object:
        base: dict[str, object] = {
            "harness": "all",
            "since": None,
            "until": None,
            "cwd": None,
            "model": None,
            "session": None,
            "repo": None,
            "json": True,
            "quiet": False,
        }
        base.update(overrides)
        return type("Args", (), base)()

    def test_unsupported_harnesses_report_unsupported_never_zero(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        args = self._args()
        buf = io.StringIO()
        rc = analyze_sessions.cmd_milestones(
            args,
            roots={
                "pi": empty,
                "claude": empty,
                "opencode": empty / "o.db",
                "copilot": empty / "c.db",
                "codex": empty,
            },
            file=buf,
        )
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        unsupported = {r["harness"] for r in rows if r.get("status") == "unsupported"}
        self.assertEqual(unsupported, {"codex", "copilot"})
        for r in rows:
            if r.get("status") == "unsupported":
                self.assertIsNone(r.get("minutes_to_first_write"))
                self.assertIsNone(r.get("spend_to_first_write"))

    def test_unsupported_row_when_only_that_harness_is_selected(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        buf = io.StringIO()
        analyze_sessions.cmd_milestones(
            self._args(harness="codex"),
            roots={"codex": empty},
            file=buf,
        )
        rows = json.loads(buf.getvalue())
        self.assertEqual(rows[0]["harness"], "codex")
        self.assertEqual(rows[0]["status"], "unsupported")


class TestAgySessionCalls(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _make_req_proto(req_id: str) -> bytes:
        val = req_id.encode("utf-8")
        f11 = bytes([(11 << 3) | 2, len(val)]) + val
        f4 = bytes([(4 << 3) | 2, len(f11)]) + f11
        f1 = bytes([(1 << 3) | 2, len(f4)]) + f4
        return f1

    def test_extract_agy_request_id_valid(self) -> None:
        proto = self._make_req_proto("req-abc-123")
        req_id, err = analyze_sessions.extract_agy_request_id(proto)
        self.assertEqual(req_id, "req-abc-123")
        self.assertIsNone(err)

    def test_extract_agy_request_id_with_surrounding_fields(self) -> None:
        # Field 2 wire-type 0 (varint): tag = (2 << 3) | 0 = 0x10, val = 42
        prefix = bytes([0x10, 42])
        # Field 3 wire-type 2 (length-delimited): tag = (3 << 3) | 2 = 0x1A, len = 3, val = "xyz"
        suffix = bytes([0x1A, 3]) + b"xyz"
        proto = prefix + self._make_req_proto("req-xyz-789") + suffix
        req_id, err = analyze_sessions.extract_agy_request_id(proto)
        self.assertEqual(req_id, "req-xyz-789")
        self.assertIsNone(err)

    def test_extract_agy_request_id_malformed(self) -> None:
        req_id, err = analyze_sessions.extract_agy_request_id(b"")
        self.assertIsNone(req_id)
        self.assertIn("expected 1 field 1", err or "")

        req_id, err = analyze_sessions.extract_agy_request_id(b"\x0a\x05incomplete")
        self.assertIsNone(req_id)
        self.assertIn("corrupt", err or "")

        # Varint overflow (high bit never cleared)
        req_id, err = analyze_sessions.extract_agy_request_id(b"\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80")
        self.assertIsNone(req_id)
        self.assertIn("corrupt", err or "")

        # Invalid utf-8 in field 11
        val = b"\xff\xfe\xfd"
        f11 = bytes([(11 << 3) | 2, len(val)]) + val
        f4 = bytes([(4 << 3) | 2, len(f11)]) + f11
        f1 = bytes([(1 << 3) | 2, len(f4)]) + f4
        req_id, err = analyze_sessions.extract_agy_request_id(f1)
        self.assertIsNone(req_id)
        self.assertIn("non-utf8", err or "")

    def test_extract_agy_request_id_missing_subfields(self) -> None:
        # Field 1 exists, but submessage has no field 4
        f1_empty = bytes([(1 << 3) | 2, 2, 0x10, 42])
        req_id, err = analyze_sessions.extract_agy_request_id(f1_empty)
        self.assertIsNone(req_id)
        self.assertIn("field 4", err or "")

        # Field 1 and field 4 exist, but field 4 has no field 11
        f4_no_f11 = bytes([(4 << 3) | 2, 2, 0x10, 42])
        f1_with_f4 = bytes([(1 << 3) | 2, len(f4_no_f11)]) + f4_no_f11
        req_id, err = analyze_sessions.extract_agy_request_id(f1_with_f4)
        self.assertIsNone(req_id)
        self.assertIn("field 11", err or "")

    def test_read_agy_session_calls_valid(self) -> None:
        db_path = self.root / "valid.db"
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
        p1 = self._make_req_proto("req-1")
        p2 = self._make_req_proto("req-2")
        cur.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (1, p1))
        cur.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (2, p2))
        cur.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (3, p1))
        con.commit()
        con.close()

        calls = analyze_sessions.read_agy_session_calls(db_path)
        self.assertEqual(calls.calls, 2)
        self.assertTrue(calls.complete)
        self.assertFalse(calls.unreadable)
        self.assertEqual(calls.request_ids, frozenset({"req-1", "req-2"}))
        self.assertEqual(calls.problems, ())

    def test_read_agy_session_calls_missing_table(self) -> None:
        db_path = self.root / "no_table.db"
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("CREATE TABLE other_table (idx INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

        calls = analyze_sessions.read_agy_session_calls(db_path)
        self.assertIsNone(calls.calls)
        self.assertFalse(calls.complete)
        self.assertFalse(calls.unreadable)
        self.assertIn("gen_metadata table missing", calls.problems[0])

    def test_read_agy_session_calls_empty_table(self) -> None:
        db_path = self.root / "empty.db"
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
        con.commit()
        con.close()

        calls = analyze_sessions.read_agy_session_calls(db_path)
        self.assertEqual(calls.calls, 0)
        self.assertFalse(calls.complete)
        self.assertFalse(calls.unreadable)
        self.assertIn("no metadata rows yet", calls.problems[0])

    def test_read_agy_session_calls_unparseable_rows(self) -> None:
        db_path = self.root / "corrupt.db"
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
        p1 = self._make_req_proto("req-1")
        cur.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (1, p1))
        cur.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (2, b"not a valid proto"))
        cur.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (3, b"\x00\x01\x02"))
        con.commit()
        con.close()

        calls = analyze_sessions.read_agy_session_calls(db_path)
        self.assertEqual(calls.calls, 1)
        self.assertFalse(calls.complete)
        self.assertFalse(calls.unreadable)
        self.assertEqual(len(calls.problems), 2)
        self.assertIn("row 2:", calls.problems[0])

    def test_read_agy_session_calls_unreadable(self) -> None:
        missing = self.root / "does_not_exist.db"
        calls = analyze_sessions.read_agy_session_calls(missing)
        self.assertIsNone(calls.calls)
        self.assertFalse(calls.complete)
        self.assertTrue(calls.unreadable)

    def test_agy_session_calls_wrapper(self) -> None:
        db_path = self.root / "wrap.db"
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
        cur.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (1, self._make_req_proto("req-w")))
        con.commit()
        con.close()

        calls = analyze_sessions.agy_session_calls(db_path)
        self.assertEqual(calls.calls, 1)
        self.assertTrue(calls.complete)



if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
