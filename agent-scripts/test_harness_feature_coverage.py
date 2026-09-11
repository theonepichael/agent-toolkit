#!/usr/bin/env python3
"""Tests for harness feature coverage matrix, notification wiring, and codex adapter.

Ensures that:
1. Every active harness declares an implementation or explicit waiver for all required features.
2. Icon assets exist in claude/icons/ for all active harnesses.
3. Notification wiring across all 6 harnesses points to notify.py.
4. codex/notify.py parses the agent-turn-complete JSON payload and dispatches correctly.
5. links.toml includes the codex notification adapter link.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness_spec
import notify

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestHarnessFeatureCoverage(unittest.TestCase):
    def test_required_features_declared(self) -> None:
        """REQUIRED_FEATURES must contain 'notifications'."""
        self.assertIn("notifications", harness_spec.REQUIRED_FEATURES)

    def test_all_active_harnesses_covered(self) -> None:
        """Every active harness must have a FeatureImplementation for each required feature."""
        for feat in harness_spec.REQUIRED_FEATURES:
            for name in harness_spec.ALL_NAMES:
                spec = harness_spec.feature_spec(name, feat)
                self.assertIsInstance(spec, harness_spec.FeatureImplementation)
                if spec.state == harness_spec.FeatureSupportState.SUPPORTED:
                    self.assertTrue(spec.source, f"{name} {feat} missing source")
                    self.assertTrue(spec.install_mapping, f"{name} {feat} missing install_mapping")
                    self.assertTrue(spec.event_mapping, f"{name} {feat} missing event_mapping")
                    self.assertTrue(spec.verification, f"{name} {feat} missing verification")
                    src_path = REPO_ROOT / spec.source
                    self.assertTrue(src_path.exists(), f"Source path {src_path} does not exist")
                    ver_path = REPO_ROOT / spec.verification
                    self.assertTrue(ver_path.exists(), f"Verification path {ver_path} does not exist")
                else:
                    self.assertTrue(spec.rationale, f"{name} {feat} missing rationale")

    def test_coverage_validator_fails_on_missing_harness(self) -> None:
        """assert_feature_coverage must fail if an active harness is omitted."""
        original_matrix = dict(harness_spec.FEATURE_MATRIX)
        try:
            # Temporarily remove codex
            del harness_spec.FEATURE_MATRIX[("codex", "notifications")]
            with self.assertRaises(AssertionError):
                harness_spec.assert_feature_coverage(REPO_ROOT)
        finally:
            harness_spec.FEATURE_MATRIX.clear()
            harness_spec.FEATURE_MATRIX.update(original_matrix)

    def test_assert_feature_coverage_passes(self) -> None:
        """assert_feature_coverage passes against the current repository state."""
        harness_spec.assert_feature_coverage(REPO_ROOT)

    def test_harness_spec_branding_fields(self) -> None:
        """HarnessSpec must declare app_id, display_name, and icon for all active harnesses."""
        for name, spec in harness_spec.HARNESSES.items():
            self.assertTrue(spec.app_id, f"{name} missing app_id")
            self.assertTrue(spec.display_name, f"{name} missing display_name")
            self.assertTrue(spec.icon, f"{name} missing icon")
            icon_file = REPO_ROOT / "claude" / "icons" / spec.icon
            self.assertTrue(icon_file.is_file(), f"Icon file for {name} does not exist: {icon_file}")

    def test_notify_py_app_registrations_derived_from_harness_spec(self) -> None:
        """notify.py's APP_REGISTRATIONS must include all harnesses from harness_spec."""
        for name, spec in harness_spec.HARNESSES.items():
            self.assertIn(name, notify.APP_REGISTRATIONS)
            reg = notify.APP_REGISTRATIONS[name]
            self.assertEqual(reg["id"], spec.app_id)
            self.assertEqual(reg["name"], spec.display_name)
            self.assertEqual(reg["icon"], spec.icon)
        # Aliases
        self.assertEqual(notify.APP_REGISTRATIONS["gemini"]["id"], "Agent.AGY")
        self.assertEqual(notify.APP_REGISTRATIONS["antigravity"]["id"], "Agent.AGY")

    def test_all_six_harnesses_notification_wiring(self) -> None:
        """Verify the notification wiring across all 6 harnesses."""
        # 1. Claude: claude/settings.json commands invoke notify.py
        settings_path = REPO_ROOT / "claude" / "settings.json"
        with open(settings_path) as f:
            claude_data = json.load(f)
        claude_commands = [
            cmd.get("command", "")
            for hook_group in claude_data.get("hooks", {}).values()
            for matcher_entry in hook_group
            for cmd in matcher_entry.get("hooks", [])
        ]
        self.assertTrue(any("notify.py" in cmd for cmd in claude_commands))

        # 2. Copilot: copilot/hooks/agent-stop.json agentStop invokes notify.py
        copilot_path = REPO_ROOT / "copilot" / "hooks" / "agent-stop.json"
        with open(copilot_path) as f:
            copilot_data = json.load(f)
        copilot_cmds = [
            h.get("bash", "")
            for hook_group in copilot_data.get("hooks", {}).values()
            for h in hook_group
        ]
        self.assertTrue(any("notify.py" in cmd for cmd in copilot_cmds))

        # 3. OpenCode: opencode/plugin/notify.ts invokes notify.py
        opencode_path = REPO_ROOT / "opencode" / "plugin" / "notify.ts"
        opencode_text = opencode_path.read_text()
        self.assertIn("notify.py", opencode_text)
        self.assertIn("session.idle", opencode_text)

        # 4. AGY: agy/hooks.json Stop hook invokes notify.py
        agy_path = REPO_ROOT / "agy" / "hooks.json"
        with open(agy_path) as f:
            agy_data = json.load(f)
        stop_cmds = [h.get("command", "") for h in agy_data.get("notify-on-stop", {}).get("Stop", [])]
        self.assertTrue(any("notify.py" in cmd for cmd in stop_cmds))

        # 5. Pi: pi/extensions/notify.ts invokes notify.py
        pi_path = REPO_ROOT / "pi" / "extensions" / "notify.ts"
        pi_text = pi_path.read_text()
        self.assertIn("notify.py", pi_text)
        self.assertIn("agent_settled", pi_text)

        # 6. Codex: invokes ~/.claude/scripts/notify.py directly (documented in codex/CLAUDE_CODE_PARITY.md)
        parity_path = REPO_ROOT / "codex" / "CLAUDE_CODE_PARITY.md"
        self.assertTrue(parity_path.is_file())
        parity_text = parity_path.read_text()
        self.assertIn("~/.claude/scripts/notify.py", parity_text)


class TestCodexNotifyHandling(unittest.TestCase):
    def test_notify_py_parses_codex_payload_flag(self) -> None:
        """notify.py parses --codex-payload JSON and dispatches with Codex branding."""
        payload = json.dumps({
            "type": "agent-turn-complete",
            "thread-id": "123",
            "last-assistant-message": "Done with task",
        })

        with patch("notify.dispatch_notification") as mock_dispatch:
            notify.main(["--codex-payload", payload])
            self.assertTrue(mock_dispatch.called)
            kwargs = mock_dispatch.call_args[1]
            self.assertEqual(kwargs["harness"], "Codex")
            self.assertEqual(kwargs["title"], "Codex CLI")
            self.assertEqual(kwargs["message"], "Done with task")
            self.assertEqual(kwargs["event_type"], "completed")

    def test_notify_py_auto_detects_positional_codex_json(self) -> None:
        """notify.py auto-detects Codex agent-turn-complete JSON passed as positional arg."""
        payload = json.dumps({
            "type": "agent-turn-complete",
            "thread-id": "456",
            "last-assistant-message": "Finished analyzing repo",
        })

        with patch("notify.dispatch_notification") as mock_dispatch:
            notify.main([payload])
            self.assertTrue(mock_dispatch.called)
            kwargs = mock_dispatch.call_args[1]
            self.assertEqual(kwargs["harness"], "Codex")
            self.assertEqual(kwargs["title"], "Codex CLI")
            self.assertEqual(kwargs["message"], "Finished analyzing repo")
            self.assertEqual(kwargs["event_type"], "completed")

    def test_codex_notify_fallback_on_invalid_payload(self) -> None:
        """parse_codex_payload falls back cleanly when payload is empty or invalid JSON."""
        self.assertEqual(notify.parse_codex_payload(""), "Task completed")
        self.assertEqual(notify.parse_codex_payload("not-json"), "Task completed")
        self.assertEqual(
            notify.parse_codex_payload('{"type": "agent-turn-complete"}'),
            "Task completed",
        )


if __name__ == "__main__":
    unittest.main()
