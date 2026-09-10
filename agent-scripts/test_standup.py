#!/usr/bin/env python3
"""Tests for standup.py. Run with: python3 test_standup.py"""

import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import standup
from standup_adapters import CalEvent, Item, Message, NotConfiguredError


class FakeIssueTrackerAdapter:
    def get_assigned_items(self) -> list[Item]:
        return [
            Item(
                id="ISSUE-2",
                title="Fresh issue",
                url="https://example.test/2",
                status="open",
                updated_at="2026-09-10T09:00:00",
            ),
            Item(
                id="ISSUE-1",
                title="Older issue",
                url="https://example.test/1",
                status="open",
                updated_at="2026-09-09T09:00:00",
            ),
        ]


class FakeChatAdapter:
    def get_relevant_messages(self, since: date) -> list[Message]:
        return [
            Message(
                author="Dev",
                text=f"Since {since.isoformat()}",
                permalink="chat://msg",
                timestamp="2026-09-10T10:00:00",
                channel_or_thread="#team",
            )
        ]

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]:
        return [
            Message(
                author="Lead",
                text=str(pending_items[0]["description"]),
                permalink="chat://thread",
                timestamp="2026-09-10T11:00:00",
                channel_or_thread="#team",
            )
        ]


class FakeEmailAdapter:
    def get_correspondence(self, since: date) -> list[Message]:
        return [
            Message(
                author="Peer",
                text=f"Email since {since.isoformat()}",
                permalink="outlook:item",
                timestamp="2026-09-10T12:00:00",
                channel_or_thread="Subject",
            )
        ]

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]:
        return [
            Message(
                author="Peer",
                text=str(pending_items[0]["description"]),
                permalink="outlook:thread",
                timestamp="2026-09-10T13:00:00",
                channel_or_thread="Subject",
            )
        ]


class FakeCalendarAdapter:
    def get_calendar_events(self, days: list[date]) -> list[CalEvent]:
        return [
            CalEvent(title="Standup", start=f"{days[-1].isoformat()}T09:30:00", is_recurring=False),
            CalEvent(title="Daily sync", start=f"{days[-1].isoformat()}T10:00:00", is_recurring=True),
        ]


class FailingChatAdapter:
    def get_relevant_messages(self, since: date) -> list[Message]:
        raise NotConfiguredError("chat unavailable")

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]:
        raise NotConfiguredError("chat unavailable")


class FetchStandupServiceTests(unittest.TestCase):
    def test_fetch_standup_uses_typed_sources_and_caller_supplied_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            standup_dir = root / "standup"
            standup_dir.mkdir()
            (standup_dir / "2026-09-08.md").write_text("Yesterday", encoding="utf-8")
            backlog_file = root / "items.json"
            backlog_file.write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": "work-active", "status": "in-progress"},
                            {"id": "work-review", "status": "in-review"},
                            {
                                "id": "work-done",
                                "status": "done",
                                "updated": "2026-09-10T08:00:00",
                            },
                            {"id": "meta-ignore", "status": "in-progress"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            pending_file = root / "pending.json"
            pending_file.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "chat-wait",
                                "status": "waiting_for_reply",
                                "kind": "chat",
                                "description": "Chat reply",
                            },
                            {
                                "id": "email-wait",
                                "status": "waiting_for_reply",
                                "kind": "email",
                                "description": "Email reply",
                            },
                            {
                                "id": "resolved",
                                "status": "resolved",
                                "kind": "email",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = standup.fetch_standup(
                standup.StandupConfig(
                    git_repos=(),
                    work_backlog_prefixes=("work-",),
                    commit_days=1,
                    recent_done_days=10000,
                ),
                standup.StandupSources(
                    issue_tracker=FakeIssueTrackerAdapter(),
                    chat=FakeChatAdapter(),
                    email=FakeEmailAdapter(),
                    calendar=FakeCalendarAdapter(),
                ),
                reference_date=date(2026, 9, 10),
                paths=standup.StandupPaths(
                    standup_data_dir=standup_dir,
                    backlog_file=backlog_file,
                    pending_file=pending_file,
                ),
            )

        self.assertEqual(report.date, date(2026, 9, 10))
        self.assertEqual(report.since, date(2026, 9, 9))
        self.assertEqual([i["id"] for i in report.backlog_in_progress], ["work-active"])
        self.assertEqual([i["id"] for i in report.backlog_in_review], ["work-review"])
        self.assertEqual([i["id"] for i in report.backlog_recent_done], ["work-done"])
        self.assertEqual([i.id for i in report.assigned_items], ["ISSUE-2", "ISSUE-1"])
        self.assertEqual(report.messages[0].permalink, "chat://msg")
        self.assertEqual(report.chat_thread_updates[0].text, "Chat reply")
        self.assertEqual(report.email_thread_updates[0].text, "Email reply")
        self.assertEqual([event.title for event in report.calendar_events], ["Standup"])
        self.assertEqual(report.previous_standup, {"date": "2026-09-08", "content": "Yesterday"})
        self.assertEqual(report.skipped_sources, [])

    def test_fetch_standup_records_adapter_skip_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = standup.fetch_standup(
                standup.StandupConfig(work_backlog_prefixes=()),
                standup.StandupSources(chat=FailingChatAdapter()),
                reference_date=date(2026, 9, 10),
                paths=standup.StandupPaths(
                    standup_data_dir=root / "standup",
                    backlog_file=root / "missing-items.json",
                    pending_file=root / "missing-pending.json",
                ),
            )

        self.assertEqual(
            [asdict(skip) for skip in report.skipped_sources],
            [
                {
                    "source": "backlog",
                    "reason": "work_backlog_prefixes not configured in config.json — backlog source skipped",
                },
                {"source": "chat", "reason": "chat unavailable"},
            ],
        )


class ParserVerbosityTests(unittest.TestCase):
    def test_flags_parse_after_every_leaf_subcommand(self) -> None:
        # A leaf added later without an entry here silently loses coverage.
        cases = {
            "fetch": ("cmd_fetch", []),
        }
        for cmd, (target, extra) in cases.items():
            argv = ["standup.py", cmd, *extra, "-q"]
            with (
                patch.object(standup, target) as mock_cmd,
                patch.object(sys, "argv", argv),
            ):
                standup.main()
            self.assertTrue(mock_cmd.call_args.args[0].quiet)


if __name__ == "__main__":
    unittest.main(verbosity=1)
