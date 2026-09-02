"""Unit tests for outlook_email.py."""

import io
import json
import unittest
from datetime import date
from unittest.mock import patch

import outlook_email
from standup_adapters import (
    EmailAdapter,
    Message,
    NotConfiguredError,
    OutlookEmailAdapter,
)


class TestOutlookEmailCLI(unittest.TestCase):
    def test_module_has_required_functions(self) -> None:
        self.assertTrue(callable(outlook_email.search_emails))
        self.assertTrue(callable(outlook_email.create_draft))
        self.assertTrue(callable(outlook_email.get_email))
        self.assertTrue(callable(outlook_email.get_recent_correspondence))

    def test_b64_encoding(self) -> None:
        self.assertEqual(outlook_email._b64("hello"), "aGVsbG8=")
        self.assertEqual(outlook_email._b64("it's a test"), "aXQncyBhIHRlc3Q=")

    def test_find_powershell_raises_when_missing(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch("os.path.exists", return_value=False),
        ):
            with self.assertRaises(outlook_email.OutlookError):
                outlook_email.find_powershell()

    def test_search_emails_parses_json(self) -> None:
        mock_output = {
            "status": "success",
            "count": 1,
            "emails": [
                {
                    "entry_id": "123",
                    "subject": "Test Subject",
                    "sender_name": "Alice",
                    "sender_email": "alice@example.com",
                    "received_time": "2026-09-02T10:00:00",
                    "body_preview": "Hello world",
                }
            ],
        }
        with patch.object(
            outlook_email, "run_powershell_json", return_value=mock_output
        ):
            result = outlook_email.search_emails(
                query="Test", since=date(2026, 9, 1), limit=5
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["subject"], "Test Subject")
            self.assertEqual(result[0]["sender_email"], "alice@example.com")

    def test_create_draft_saves_and_displays(self) -> None:
        mock_output = {
            "status": "success",
            "entry_id": "draft-123",
            "subject": "Draft Subject",
        }
        with patch.object(
            outlook_email, "run_powershell_json", return_value=mock_output
        ) as mock_run:
            result = outlook_email.create_draft(
                to="bob@example.com",
                subject="Draft Subject",
                body="Draft body content",
                cc="carol@example.com",
                display=True,
            )
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["entry_id"], "draft-123")
            mock_run.assert_called_once()

    def test_get_email_by_id(self) -> None:
        mock_output = {
            "status": "success",
            "entry_id": "123",
            "subject": "Detailed Subject",
            "sender_name": "Bob",
            "sender_email": "bob@example.com",
            "received_time": "2026-09-02T12:00:00",
            "body": "Full body content here",
        }
        with patch.object(
            outlook_email, "run_powershell_json", return_value=mock_output
        ):
            result = outlook_email.get_email("123")
            self.assertEqual(result["subject"], "Detailed Subject")
            self.assertEqual(result["body"], "Full body content here")

    def test_get_recent_correspondence(self) -> None:
        mock_output = {
            "status": "success",
            "count": 2,
            "emails": [
                {
                    "entry_id": "1",
                    "subject": "Update",
                    "sender_name": "Alice",
                    "sender_email": "alice@example.com",
                    "received_time": "2026-09-02T09:00:00",
                    "body_preview": "Update snippet",
                },
                {
                    "entry_id": "2",
                    "subject": "Question",
                    "sender_name": "Charlie",
                    "sender_email": "charlie@example.com",
                    "received_time": "2026-09-02T11:00:00",
                    "body_preview": "Question snippet",
                },
            ],
        }
        with patch.object(
            outlook_email, "run_powershell_json", return_value=mock_output
        ):
            emails = outlook_email.get_recent_correspondence(since=date(2026, 9, 1))
            self.assertEqual(len(emails), 2)

    def test_main_search_json(self) -> None:
        mock_output = [
            {
                "entry_id": "123",
                "subject": "Search Result",
                "sender_name": "Dave",
                "sender_email": "dave@example.com",
                "received_time": "2026-09-02T14:00:00",
                "body_preview": "Snippet",
            }
        ]
        with (
            patch("outlook_email.search_emails", return_value=mock_output),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            exit_code = outlook_email.main(["search", "-q", "Search", "--json"])
            self.assertEqual(exit_code, 0)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["subject"], "Search Result")

    def test_main_draft_json(self) -> None:
        mock_res = {
            "status": "success",
            "entry_id": "draft-abc",
            "subject": "Hello",
        }
        with (
            patch("outlook_email.create_draft", return_value=mock_res),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            exit_code = outlook_email.main(
                [
                    "draft",
                    "--to",
                    "user@example.com",
                    "--subject",
                    "Hello",
                    "--body",
                    "World",
                    "--no-display",
                    "--json",
                ]
            )
            self.assertEqual(exit_code, 0)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(data["status"], "success")

    def test_main_get_json(self) -> None:
        mock_res = {
            "entry_id": "item-1",
            "subject": "Item 1",
            "sender_name": "Eve",
            "sender_email": "eve@example.com",
            "received_time": "2026-09-02T15:00:00",
            "body": "Full body text",
        }
        with (
            patch("outlook_email.get_email", return_value=mock_res),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            exit_code = outlook_email.main(["get", "--id", "item-1", "--json"])
            self.assertEqual(exit_code, 0)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(data["subject"], "Item 1")

    def test_main_recent_json(self) -> None:
        mock_output = [
            {
                "entry_id": "r1",
                "subject": "Recent 1",
                "sender_name": "Frank",
                "sender_email": "frank@example.com",
                "received_time": "2026-09-02T16:00:00",
            }
        ]
        with (
            patch("outlook_email.get_recent_correspondence", return_value=mock_output),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            exit_code = outlook_email.main(
                ["recent", "--since", "2026-09-01", "--json"]
            )
            self.assertEqual(exit_code, 0)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(len(data), 1)

    def test_main_error_json(self) -> None:
        with (
            patch(
                "outlook_email.search_emails",
                side_effect=outlook_email.OutlookError("COM failed"),
            ),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            exit_code = outlook_email.main(["search", "-q", "Fail", "--json"])
            self.assertEqual(exit_code, 1)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(data["status"], "error")
            self.assertIn("COM failed", data["message"])


class TestOutlookEmailAdapter(unittest.TestCase):
    def test_implements_email_adapter_protocol(self) -> None:
        adapter = OutlookEmailAdapter()
        self.assertTrue(isinstance(adapter, EmailAdapter))

    def test_get_correspondence_maps_to_messages(self) -> None:
        adapter = OutlookEmailAdapter()
        sample_emails = [
            {
                "entry_id": "12345",
                "subject": "Standup topic",
                "sender_name": "Boss",
                "sender_email": "boss@company.com",
                "received_time": "2026-09-02T08:30:00",
                "body_preview": "Please review this item",
            }
        ]
        with patch(
            "outlook_email.get_recent_correspondence", return_value=sample_emails
        ):
            messages = adapter.get_correspondence(since=date(2026, 9, 2))
            self.assertEqual(len(messages), 1)
            msg = messages[0]
            self.assertIsInstance(msg, Message)
            self.assertEqual(msg.author, "Boss <boss@company.com>")
            self.assertEqual(msg.text, "Standup topic: Please review this item")
            self.assertEqual(msg.permalink, "outlook:12345")
            self.assertEqual(msg.timestamp, "2026-09-02T08:30:00")
            self.assertEqual(msg.channel_or_thread, "Standup topic")

    def test_get_correspondence_raises_not_configured_on_error(self) -> None:
        adapter = OutlookEmailAdapter()
        with patch(
            "outlook_email.get_recent_correspondence",
            side_effect=outlook_email.OutlookError("COM unavailable"),
        ), self.assertRaises(NotConfiguredError):
            adapter.get_correspondence(since=date(2026, 9, 2))

    def test_get_thread_updates_filters_by_pending_item(self) -> None:
        adapter = OutlookEmailAdapter()
        sample_emails = [
            {
                "entry_id": "999",
                "subject": "Re: Access request for project",
                "sender_name": "Admin",
                "sender_email": "admin@company.com",
                "received_time": "2026-09-02T13:00:00",
                "body_preview": "Approved your access request",
            }
        ]
        pending_items = [
            {
                "id": "work-access-req",
                "description": "Waiting on access request approval",
                "source_ref": {"subject": "Access request for project"},
            }
        ]
        with patch("outlook_email.search_emails", return_value=sample_emails):
            updates = adapter.get_thread_updates(pending_items)
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0].author, "Admin <admin@company.com>")
            self.assertEqual(
                updates[0].channel_or_thread, "Re: Access request for project"
            )

    def test_get_thread_updates_raises_not_configured_on_error(self) -> None:
        adapter = OutlookEmailAdapter()
        pending_items = [
            {
                "id": "work-item",
                "source_ref": {"subject": "Some task"},
            }
        ]
        with patch(
            "outlook_email.search_emails",
            side_effect=outlook_email.OutlookError("COM failed"),
        ), self.assertRaises(NotConfiguredError):
            adapter.get_thread_updates(pending_items)


if __name__ == "__main__":
    unittest.main()
