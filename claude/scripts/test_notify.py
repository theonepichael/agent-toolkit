#!/usr/bin/env python3
"""Tests for notify.py. Run with: python3 test_notify.py"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import notify


class NotifyTestCase(unittest.TestCase):
    def test_build_parser_defaults(self) -> None:
        parser = notify.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.title, "Agent Notification")
        self.assertIsNone(args.message)
        self.assertIsNone(args.positional_message)
        self.assertIsNone(args.harness)
        self.assertEqual(args.urgency, "normal")
        self.assertEqual(args.type, "completed")

    def test_build_parser_custom_args(self) -> None:
        parser = notify.build_parser()
        args = parser.parse_args([
            "Positional message",
            "--title", "Custom Title",
            "--harness", "Claude",
            "--icon", "/tmp/custom.png",
            "--urgency", "critical",
            "--type", "error",
            "-q",
        ])
        self.assertEqual(args.title, "Custom Title")
        self.assertEqual(args.positional_message, "Positional message")
        self.assertEqual(args.harness, "Claude")
        self.assertEqual(args.icon, "/tmp/custom.png")
        self.assertEqual(args.urgency, "critical")
        self.assertEqual(args.type, "error")
        self.assertTrue(args.quiet)

    def test_escape_powershell_string(self) -> None:
        raw = "Claude's Task: don't fail"
        escaped = notify._escape_powershell_string(raw)
        self.assertEqual(escaped, "Claude''s Task: don''t fail")

    def test_escape_xml(self) -> None:
        raw = 'Fish & Chips <"tasty">'
        escaped = notify._escape_xml(raw)
        self.assertEqual(escaped, "Fish &amp; Chips &lt;&quot;tasty&quot;&gt;")

    def test_get_harness_icon(self) -> None:
        claude_icon = notify.get_harness_icon("Claude")
        self.assertIsNotNone(claude_icon)
        self.assertTrue(claude_icon.name.endswith("claude.png"))

        agy_icon = notify.get_harness_icon("AGY")
        self.assertIsNotNone(agy_icon)

        pi_icon = notify.get_harness_icon("Pi")
        self.assertIsNotNone(pi_icon)

        copilot_icon = notify.get_harness_icon("Copilot")
        self.assertIsNotNone(copilot_icon)

        opencode_icon = notify.get_harness_icon("OpenCode")
        self.assertIsNotNone(opencode_icon)

    @patch("shutil.which", return_value="/usr/bin/powershell.exe")
    @patch("subprocess.run")
    def test_send_wsl_toast_invokes_powershell(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        success = notify.send_wsl_toast("Test Title", "Test Message")
        self.assertTrue(success)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "/usr/bin/powershell.exe")
        self.assertIn("-NoProfile", cmd)
        self.assertIn("-NonInteractive", cmd)
        self.assertIn("-InputFormat", cmd)
        self.assertIn("| Out-Null", cmd[6])
        self.assertIn("Test Title", cmd[6])
        self.assertIn("Test Message", cmd[6])

    @patch("shutil.which", return_value=None)
    @patch("os.path.exists", return_value=False)
    def test_send_wsl_toast_missing_powershell(self, mock_exists: MagicMock, mock_which: MagicMock) -> None:
        success = notify.send_wsl_toast("Test Title", "Test Message")
        self.assertFalse(success)

    @patch("subprocess.run")
    def test_send_macos_notification(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        success = notify.send_macos_notification('Title with "quotes"', 'Msg with "quotes"')
        self.assertTrue(success)
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        self.assertEqual(args[0][0], "osascript")
        self.assertIn('\\"quotes\\"', args[0][2])

    @patch("shutil.which", return_value="/usr/bin/notify-send")
    @patch("subprocess.run")
    def test_send_linux_notification(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        success = notify.send_linux_notification("Linux Title", "Linux Message", urgency="low")
        self.assertTrue(success)
        mock_run.assert_called_once_with(
            ["notify-send", "-u", "low", "Linux Title", "Linux Message"],
            capture_output=True,
            timeout=3,
            check=False,
        )

    @patch("notify.is_wsl", return_value=True)
    @patch("notify.send_terminal_osc")
    @patch("notify.send_wsl_toast")
    def test_dispatch_notification_wsl(
        self,
        mock_wsl_toast: MagicMock,
        mock_osc: MagicMock,
        mock_is_wsl: MagicMock,
    ) -> None:
        notify.dispatch_notification(
            title="Task Done",
            message="All tests pass",
            harness="Pi",
        )
        mock_osc.assert_called_once_with("Pi · Task Done", "All tests pass", verbose=False)
        self.assertTrue(mock_wsl_toast.called)


if __name__ == "__main__":
    unittest.main()
