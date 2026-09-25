#!/usr/bin/env python3
"""Tests for gen_hooks.py and declarative hook registry in harness_spec.py.

Run with: python3 test/test_gen_hooks.py or uv run pytest test/test_gen_hooks.py
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import pytest
import test_bootstrap  # noqa: E402
import gen_hooks as gh  # noqa: E402
import harness_spec  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class HarnessSpecHookManifestTests(unittest.TestCase):
    def test_lifecycle_hooks_manifest_defined(self) -> None:
        """harness_spec must define LIFECYCLE_HOOKS covering required events."""
        self.assertTrue(hasattr(harness_spec, "LIFECYCLE_HOOKS"))
        manifest = harness_spec.LIFECYCLE_HOOKS
        self.assertIn("PreToolUse", manifest)
        self.assertIn("PostToolUse", manifest)
        self.assertIn("SessionStart", manifest)
        self.assertIn("Stop", manifest)

    def test_agy_does_not_declare_session_start(self) -> None:
        """AGY has has_session_start_hook=False; SessionStart must not map to PreInvocation."""
        manifest = harness_spec.LIFECYCLE_HOOKS
        session_start = manifest.get("SessionStart", {})
        self.assertNotIn("agy", session_start)
        self.assertFalse(harness_spec.spec("agy").has_session_start_hook)

    def test_copilot_session_start_uses_sessionstart_checks(self) -> None:
        """Copilot sessionStart must invoke sessionstart_checks.py with timeoutSec 45."""
        manifest = harness_spec.LIFECYCLE_HOOKS
        copilot_ss = manifest["SessionStart"].get("copilot")
        self.assertIsNotNone(copilot_ss)
        self.assertIn("sessionstart_checks.py", copilot_ss["bash"])
        self.assertEqual(copilot_ss.get("timeoutSec"), 45)


class CompileHooksTests(unittest.TestCase):
    def test_compile_hooks_returns_all_expected_targets(self) -> None:
        """compile_hooks must emit outputs for copilot, agy, and claude settings."""
        outputs = gh.compile_hooks(REPO_ROOT)
        expected_rel_paths = {
            Path("copilot/hooks/pre-tool-use.json"),
            Path("copilot/hooks/post-tool-use.json"),
            Path("copilot/hooks/session-start.json"),
            Path("copilot/hooks/agent-stop.json"),
            Path("agy/hooks.json"),
            Path("claude/settings.json"),
            Path("claude/settings.work.json"),
        }
        output_rel_paths = {p.relative_to(REPO_ROOT) for p in outputs}
        self.assertEqual(output_rel_paths, expected_rel_paths)

    def test_claude_settings_preserves_non_hook_fields(self) -> None:
        """Compiling claude settings must preserve permissions, model, theme, etc."""
        outputs = gh.compile_hooks(REPO_ROOT)
        settings_path = REPO_ROOT / "claude" / "settings.json"
        original = json.loads(settings_path.read_text(encoding="utf-8"))
        compiled = json.loads(outputs[settings_path])

        self.assertEqual(compiled.get("permissions"), original.get("permissions"))
        self.assertEqual(compiled.get("model"), original.get("model"))
        self.assertEqual(compiled.get("tui"), original.get("tui"))
        self.assertEqual(compiled.get("voice"), original.get("voice"))
        self.assertIn("hooks", compiled)
        self.assertIn("PreToolUse", compiled["hooks"])
        self.assertIn("PostToolUse", compiled["hooks"])
        self.assertIn("SessionStart", compiled["hooks"])

    def test_copilot_schema_shape(self) -> None:
        """Copilot hooks must have version 1, camelCase hook keys, and timeoutSec."""
        outputs = gh.compile_hooks(REPO_ROOT)
        for rel_path, hook_key in [
            (Path("copilot/hooks/pre-tool-use.json"), "preToolUse"),
            (Path("copilot/hooks/post-tool-use.json"), "postToolUse"),
            (Path("copilot/hooks/session-start.json"), "sessionStart"),
            (Path("copilot/hooks/agent-stop.json"), "agentStop"),
        ]:
            data = json.loads(outputs[REPO_ROOT / rel_path])
            self.assertEqual(data.get("version"), 1)
            self.assertIn("hooks", data)
            self.assertIn(hook_key, data["hooks"])
            for entry in data["hooks"][hook_key]:
                self.assertIn("type", entry)
                self.assertIn("bash", entry)
                self.assertIn("timeoutSec", entry)

    def test_agy_schema_shape(self) -> None:
        """AGY hooks must have named top-level blocks and valid timeout/command."""
        outputs = gh.compile_hooks(REPO_ROOT)
        data = json.loads(outputs[REPO_ROOT / "agy/hooks.json"])
        self.assertIn("worktree-guard", data)
        self.assertIn("ruff-format-on-edit", data)
        self.assertIn("notify-on-stop", data)
        self.assertIn("herdr", data)


@pytest.mark.allow_real_subprocess
class CliEndToEndTests(unittest.TestCase):
    def test_check_flag_exits_0_on_clean_tree(self) -> None:
        """python3 agent-scripts/gen_hooks.py --check exits 0 when committed content matches."""
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "agent-scripts/gen_hooks.py"), "--check", "--repo-root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"gen_hooks.py --check failed: {res.stderr}\n{res.stdout}")

    def test_check_flag_exits_1_on_stale_file(self) -> None:
        """python3 agent-scripts/gen_hooks.py --check exits 1 when a target file is stale."""
        target = REPO_ROOT / "copilot/hooks/pre-tool-use.json"
        original = target.read_text(encoding="utf-8")
        try:
            target.write_text('{"stale": true}\n', encoding="utf-8")
            res = subprocess.run(
                [sys.executable, str(REPO_ROOT / "agent-scripts/gen_hooks.py"), "--check", "--repo-root", str(REPO_ROOT)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("copilot/hooks/pre-tool-use.json", res.stderr)
        finally:
            target.write_text(original, encoding="utf-8")

    def test_stdout_flag_emits_json(self) -> None:
        """python3 agent-scripts/gen_hooks.py --stdout prints compiled targets."""
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "agent-scripts/gen_hooks.py"), "--stdout", "--repo-root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("copilot/hooks/pre-tool-use.json", res.stdout)
        self.assertIn("agy/hooks.json", res.stdout)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main(verbosity=2)
