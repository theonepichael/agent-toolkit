#!/usr/bin/env python3
"""Declarative harness specification registry.

Single source of truth for per-harness metadata across generators, interface
renderers, installers, link inspection, and discovery probes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessSpec:
    """Specification and discovery facts for a single AI agent harness."""

    name: str
    cli: str
    install_hint: str
    process_name: str = ""
    expected_filenames: frozenset[str] = frozenset()
    fallback_paths: tuple[str, ...] = ()
    load_bearing: bool = False
    probe_expected_root: frozenset[str] = frozenset()

    def process_basename(self) -> str:
        """Return process basename for claim detection, falling back to name."""
        return self.process_name or self.name


# Authoring order is the canonical render order across all consumers.
HARNESSES: dict[str, HarnessSpec] = {
    "claude": HarnessSpec(
        name="claude",
        cli="claude",
        install_hint="npm install -g @anthropic-ai/claude-code",
        expected_filenames=frozenset({"CLAUDE.md"}),
        fallback_paths=("~/.local/bin/claude",),
        load_bearing=True,
        probe_expected_root=frozenset({"FIXTURE_TOKEN_CLAUDE_ROOT"}),
    ),
    "copilot": HarnessSpec(
        name="copilot",
        cli="copilot",
        install_hint="npm install -g @github/copilot",
        expected_filenames=frozenset({"CLAUDE.md", "GEMINI.md", "AGENTS.md"}),
        fallback_paths=("~/.npm-global/bin/copilot",),
        load_bearing=False,
        probe_expected_root=frozenset(
            {
                "FIXTURE_TOKEN_CLAUDE_ROOT",
                "FIXTURE_TOKEN_GEMINI_ROOT",
                "FIXTURE_TOKEN_AGENTS_ROOT",
            }
        ),
    ),
    "opencode": HarnessSpec(
        name="opencode",
        cli="opencode",
        install_hint="curl -fsSL https://opencode.ai/install | bash",
        expected_filenames=frozenset({"AGENTS.md"}),
        fallback_paths=("~/.opencode/bin/opencode",),
        load_bearing=True,
        probe_expected_root=frozenset({"FIXTURE_TOKEN_AGENTS_ROOT"}),
    ),
    "agy": HarnessSpec(
        name="agy",
        cli="agy",
        install_hint="internal workstation installation",
        expected_filenames=frozenset(),
        fallback_paths=("~/.local/bin/agy",),
        load_bearing=False,
        probe_expected_root=frozenset(),
    ),
    "pi": HarnessSpec(
        name="pi",
        cli="pi",
        install_hint="npm install -g @mariozechner/pi-cli",
        expected_filenames=frozenset({"AGENTS.md"}),
        fallback_paths=("~/.npm-global/bin/pi",),
        load_bearing=False,
        probe_expected_root=frozenset({"FIXTURE_TOKEN_AGENTS_ROOT"}),
    ),
    "codex": HarnessSpec(
        name="codex",
        cli="codex",
        install_hint="npm install -g @openai/codex",
        expected_filenames=frozenset({"AGENTS.md"}),
        fallback_paths=("~/.npm-global/bin/codex",),
        load_bearing=False,
        probe_expected_root=frozenset({"FIXTURE_TOKEN_AGENTS_ROOT"}),
    ),
}

ALL_NAMES: tuple[str, ...] = tuple(HARNESSES.keys())

HARNESS_BASENAMES: tuple[str, ...] = tuple(
    spec.process_basename() for spec in HARNESSES.values()
)

LOAD_BEARING_NAMES: tuple[str, ...] = tuple(
    name for name, spec in HARNESSES.items() if spec.load_bearing
)

# Probe targets order in harness_discovery_check
DISCOVERY_TARGETS: tuple[str, ...] = (
    "claude",
    "opencode",
    "pi",
    "copilot",
    "agy",
    "codex",
)


def spec(name: str) -> HarnessSpec:
    """Return the HarnessSpec for the given harness name."""
    return HARNESSES[name]


def binary(name: str) -> str:
    """Return the CLI binary name for the given harness."""
    return HARNESSES[name].cli


def install_hint(name: str) -> str:
    """Return the installation hint for the given harness."""
    return HARNESSES[name].install_hint


def fallback_paths(name: str) -> tuple[str, ...]:
    """Return the fallback probe paths for the given harness."""
    return HARNESSES[name].fallback_paths


def expected_filenames(name: str) -> frozenset[str]:
    """Return the expected instruction filenames loaded by the given harness."""
    return HARNESSES[name].expected_filenames


def probe_expected_root(name: str) -> frozenset[str]:
    """Return the fixture root tokens expected for the given harness."""
    return HARNESSES[name].probe_expected_root
