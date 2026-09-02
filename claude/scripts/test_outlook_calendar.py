"""Unit tests for outlook_calendar.py."""

from datetime import date
import io
import json
import unittest
from unittest.mock import patch

import outlook_calendar
from standup_adapters import CalEvent, CalendarAdapter, NotConfiguredError, OutlookCalendarAdapter


class TestOutlookCalendarCLI(unittest.TestCase):
    def test_module_has_required_functions(self) -> None:
        self.assertTrue(callable(outlook_calendar.get_calendar_events_range))
        self.assertTrue(callable(outlook_calendar.get_appointment))

    def test_b64_encoding(self) -> None:
        self.assertEqual(outlook_calendar._b64("meeting"), "bWVldGluZw==")

    def test_get_calendar_events_range(self) -> None:
        mock_output = {
            "status": "success",
            "count": 2,
            "events": [
                {
                    "entry_id": "cal-1",
                    "title": "Daily Standup",
                    "start": "2026-09-02T09:30:00",
                    "end": "2026-09-02T09:45:00",
                    "is_recurring": True,
                    "location": "Teams",
                },
                {
                    "entry_id": "cal-2",
                    "title": "1:1 with Manager",
                    "start": "2026-09-02T14:00:00",
                    "end": "2026-09-02T14:30:00",
                    "is_recurring": False,
                    "location": "Room 101",
                },
            ],
        }
        with patch.object(
            outlook_calendar, "run_powershell_json", return_value=mock_output
        ):
            events = outlook_calendar.get_calendar_events_range(
                start_date=date(2026, 9, 2), end_date=date(2026, 9, 2)
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["title"], "Daily Standup")
            self.assertTrue(events[0]["is_recurring"])

    def test_get_appointment_by_id(self) -> None:
        mock_output = {
            "status": "success",
            "entry_id": "cal-1",
            "title": "Sprint Planning",
            "start": "2026-09-02T11:00:00",
            "end": "2026-09-02T12:00:00",
            "is_recurring": True,
            "location": "Virtual",
            "body": "Planning agenda",
        }
        with patch.object(
            outlook_calendar, "run_powershell_json", return_value=mock_output
        ):
            item = outlook_calendar.get_appointment("cal-1")
            self.assertEqual(item["title"], "Sprint Planning")
            self.assertEqual(item["body"], "Planning agenda")

    def test_main_list_json(self) -> None:
        mock_events = [
            {
                "entry_id": "e1",
                "title": "Sprint Review",
                "start": "2026-09-02T15:00:00",
                "end": "2026-09-02T16:00:00",
                "is_recurring": False,
                "location": "Online",
            }
        ]
        with patch(
            "outlook_calendar.get_calendar_events_range", return_value=mock_events
        ), patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            exit_code = outlook_calendar.main(["list", "--since", "2026-09-02", "--json"])
            self.assertEqual(exit_code, 0)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["title"], "Sprint Review")

    def test_main_get_json(self) -> None:
        mock_item = {
            "entry_id": "e1",
            "title": "Retro",
            "start": "2026-09-02T16:00:00",
            "end": "2026-09-02T17:00:00",
            "is_recurring": True,
            "location": "",
            "body": "What went well",
        }
        with patch(
            "outlook_calendar.get_appointment", return_value=mock_item
        ), patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            exit_code = outlook_calendar.main(["get", "--id", "e1", "--json"])
            self.assertEqual(exit_code, 0)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(data["title"], "Retro")

    def test_main_error_handling(self) -> None:
        with patch(
            "outlook_calendar.get_calendar_events_range",
            side_effect=outlook_calendar.OutlookCalendarError("COM error"),
        ), patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            exit_code = outlook_calendar.main(["list", "--json"])
            self.assertEqual(exit_code, 1)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(data["status"], "error")


class TestOutlookCalendarAdapter(unittest.TestCase):
    def test_implements_calendar_adapter_protocol(self) -> None:
        adapter = OutlookCalendarAdapter()
        self.assertTrue(isinstance(adapter, CalendarAdapter))

    def test_get_calendar_events_maps_to_cal_events(self) -> None:
        adapter = OutlookCalendarAdapter()
        mock_events = [
            {
                "entry_id": "cal-10",
                "title": "Project Demo",
                "start": "2026-09-02T13:00:00",
                "end": "2026-09-02T13:30:00",
                "is_recurring": False,
                "location": "Main Hall",
            },
            {
                "entry_id": "cal-11",
                "title": "Recurring Sync",
                "start": "2026-09-03T10:00:00",
                "end": "2026-09-03T10:30:00",
                "is_recurring": True,
                "location": "Zoom",
            },
        ]
        with patch(
            "outlook_calendar.get_calendar_events_range", return_value=mock_events
        ):
            events = adapter.get_calendar_events([date(2026, 9, 2), date(2026, 9, 3)])
            self.assertEqual(len(events), 2)
            self.assertIsInstance(events[0], CalEvent)
            self.assertEqual(events[0].title, "Project Demo")
            self.assertEqual(events[0].start, "2026-09-02T13:00:00")
            self.assertFalse(events[0].is_recurring)
            self.assertTrue(events[1].is_recurring)

    def test_get_calendar_events_raises_not_configured_on_error(self) -> None:
        adapter = OutlookCalendarAdapter()
        with patch(
            "outlook_calendar.get_calendar_events_range",
            side_effect=outlook_calendar.OutlookCalendarError("COM unavailable"),
        ):
            with self.assertRaises(NotConfiguredError):
                adapter.get_calendar_events([date(2026, 9, 2)])


if __name__ == "__main__":
    unittest.main()
