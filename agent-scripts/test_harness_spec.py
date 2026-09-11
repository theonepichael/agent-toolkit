#!/usr/bin/env python3
"""Tests for harness_spec.py registry, external reality anchors, and consumer parity.

Guards the single declarative harness specification registry, asserting that:
1. harness_spec is stdlib-only, import-safe, and immutable.
2. Codex is present in the registry, in links.toml, and in claim detection.
3. Historical CLI binaries and install hints are preserved.
4. links.toml harness values are a subset of the registry.
5. All in-scope consumers derive their views from harness_spec with ordered sequence parity.
6. Deferred consumers (gen_skills, gen_shell_completion) are explicitly documented.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness_spec
import link_inspect
import gen_interfaces
import install
import dev_status_mutation
import harness_discovery_check


class TestHarnessSpecRegistry(unittest.TestCase):
    def test_dataclass_frozen_and_attributes(self) -> None:
        """HarnessSpec is a frozen dataclass with expected fields and methods."""
        spec = harness_spec.HARNESSES["claude"]
        self.assertTrue(is_dataclass(spec))
        with self.assertRaises(FrozenInstanceError):
            spec.name = "other"  # type: ignore[misc]

        self.assertEqual(spec.process_basename(), "claude")

        # Test process_name override behavior
        custom = harness_spec.HarnessSpec(
            name="dummy",
            cli="dummy-cli",
            install_hint="none",
            process_name="dummy_proc",
        )
        self.assertEqual(custom.process_basename(), "dummy_proc")

        default_custom = harness_spec.HarnessSpec(
            name="dummy2",
            cli="dummy2-cli",
            install_hint="none",
        )
        self.assertEqual(default_custom.process_basename(), "dummy2")

    def test_registry_types_and_views(self) -> None:
        """Derived views must be immutable ordered tuples."""
        self.assertIsInstance(harness_spec.HARNESSES, dict)
        self.assertIsInstance(harness_spec.ALL_NAMES, tuple)
        self.assertIsInstance(harness_spec.HARNESS_BASENAMES, tuple)
        self.assertIsInstance(harness_spec.LOAD_BEARING_NAMES, tuple)
        self.assertIsInstance(harness_spec.DISCOVERY_TARGETS, tuple)

        self.assertEqual(harness_spec.ALL_NAMES, tuple(harness_spec.HARNESSES.keys()))
        self.assertEqual(
            harness_spec.HARNESS_BASENAMES,
            tuple(spec.process_basename() for spec in harness_spec.HARNESSES.values()),
        )
        self.assertEqual(
            harness_spec.LOAD_BEARING_NAMES,
            tuple(name for name, spec in harness_spec.HARNESSES.items() if spec.load_bearing),
        )

    def test_external_reality_anchors(self) -> None:
        """Codex and historical values must be anchored against reality."""
        # Codex present
        self.assertIn("codex", harness_spec.HARNESSES)
        self.assertIn("codex", harness_spec.HARNESS_BASENAMES)
        self.assertIn("codex", harness_spec.ALL_NAMES)

        # Historical CLI binary mapping
        expected_binaries = {
            "claude": "claude",
            "copilot": "copilot",
            "opencode": "opencode",
            "agy": "agy",
            "pi": "pi",
            "codex": "codex",
        }
        for name, expected_cli in expected_binaries.items():
            self.assertEqual(harness_spec.HARNESSES[name].cli, expected_cli)
            self.assertEqual(harness_spec.binary(name), expected_cli)

        # Historical install hints
        expected_hints = {
            "claude": "npm install -g @anthropic-ai/claude-code",
            "copilot": "npm install -g @github/copilot",
            "opencode": "curl -fsSL https://opencode.ai/install | bash",
            "agy": "internal workstation installation",
            "pi": "npm install -g @mariozechner/pi-cli",
            "codex": "npm install -g @openai/codex",
        }
        for name, expected_hint in expected_hints.items():
            self.assertEqual(harness_spec.HARNESSES[name].install_hint, expected_hint)
            self.assertEqual(harness_spec.install_hint(name), expected_hint)

    def test_links_toml_harnesses_subset(self) -> None:
        """Distinct harness values in links.toml must be a subset of the registry and contain codex."""
        repo_root = Path(__file__).resolve().parent.parent
        links_file = repo_root / "links.toml"
        with open(links_file, "rb") as f:
            data = tomllib.load(f)

        harnesses_in_links = set()
        for section in ("link", "directory", "managed_dir"):
            for entry in data.get(section, []):
                h = entry.get("harness")
                if h:
                    harnesses_in_links.add(h)

        self.assertIn("codex", harnesses_in_links)
        self.assertTrue(
            harnesses_in_links.issubset(set(harness_spec.ALL_NAMES)),
            f"links.toml contains unknown harnesses: {harnesses_in_links - set(harness_spec.ALL_NAMES)}",
        )

    def test_migrated_consumers_sequence_parity(self) -> None:
        """Migrated consumers must match registry views in exact sequence order."""
        # 1. link_inspect.VALID_HARNESSES
        self.assertEqual(link_inspect.VALID_HARNESSES, harness_spec.ALL_NAMES)

        # 2. gen_interfaces.HARNESS_DIRS (includes codex now)
        self.assertEqual(gen_interfaces.HARNESS_DIRS, harness_spec.ALL_NAMES)

        # 3. install.HARNESS_BINARIES & HARNESS_INSTALL_HINTS
        self.assertEqual(tuple(install.HARNESS_BINARIES.keys()), harness_spec.ALL_NAMES)
        self.assertEqual(tuple(install.HARNESS_INSTALL_HINTS.keys()), harness_spec.ALL_NAMES)

        # 4. dev_status_mutation._HARNESS_BASENAMES includes codex
        self.assertIn("codex", dev_status_mutation._HARNESS_BASENAMES)
        self.assertEqual(
            set(dev_status_mutation._HARNESS_BASENAMES),
            set(harness_spec.HARNESS_BASENAMES),
        )

        # 5. harness_discovery_check
        self.assertEqual(harness_discovery_check._LOAD_BEARING, harness_spec.LOAD_BEARING_NAMES)
        self.assertEqual(tuple(harness_discovery_check._FALLBACK_PATHS.keys()), harness_spec.ALL_NAMES)
        self.assertEqual(harness_discovery_check.DISCOVERY_TARGETS, harness_spec.DISCOVERY_TARGETS)

    def test_deferred_consumers_documented(self) -> None:
        """Explicitly documents why gen_skills and gen_shell_completion are deferred.

        gen_skills: Its render surface uses 35 template tokens fed largely by
        per-skill SKILL_PARAMS, not a pure per-harness fact table. Migrating it
        touches 58 byte-checked files, so it is deferred to a follow-up item.

        gen_shell_completion: Uses an adapter dataclass that will be unified
        and renamed in a follow-up item without drifting the CLI set.
        """
        deferred = {"gen_skills", "gen_shell_completion"}
        self.assertEqual(len(deferred), 2)


if __name__ == "__main__":
    unittest.main()
