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
    structured_choice: str = ""
    instructions_ref: str = "the shared instructions file's"
    instructions_ref_bare: str = "the shared instructions file"
    has_session_start_hook: bool = False
    skill_src_pattern: str = ""
    skill_dest_pattern: str = ""
    skill_ref_dir: str = "ref"
    probe_command: str = ""
    commit_scope: str = ""

    def process_basename(self) -> str:
        """Return process basename for claim detection, falling back to name."""
        return self.process_name or self.name

    def skill_output_path(self, skill: str) -> str:
        """Return repo-relative output path for a skill."""
        return self.skill_src_pattern.replace("<name>", skill)

    def capability_facts(self) -> dict[str, str | bool]:
        """Return dict of capability facts for skill template rendering."""
        return {
            "structured_choice": self.structured_choice,
            "instructions_ref": self.instructions_ref,
            "instructions_ref_bare": self.instructions_ref_bare,
            "has_session_start_hook": self.has_session_start_hook,
            "skill_src_pattern": self.skill_src_pattern,
            "skill_dest_pattern": self.skill_dest_pattern,
            "skill_ref_dir": self.skill_ref_dir,
            "probe_command": self.probe_command,
            "commit_scope": self.commit_scope,
        }


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
        structured_choice="AskUserQuestion",
        instructions_ref="CLAUDE.md's",
        instructions_ref_bare="CLAUDE.md",
        has_session_start_hook=True,
        skill_src_pattern="claude/commands/<name>.md",
        skill_dest_pattern="~/.claude/commands/<name>.md",
        skill_ref_dir="ref",
        probe_command="claude -p",
        commit_scope="claude",
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
        structured_choice="",
        instructions_ref="the shared instructions file's",
        instructions_ref_bare="the shared instructions file",
        has_session_start_hook=True,
        skill_src_pattern="copilot/skills/<name>/SKILL.md",
        skill_dest_pattern="~/.copilot/skills/<name>/SKILL.md",
        skill_ref_dir="ref",
        probe_command="copilot -p",
        commit_scope="copilot",
    ),
    "opencode": HarnessSpec(
        name="opencode",
        cli="opencode",
        install_hint="curl -fsSL https://opencode.ai/install | bash",
        expected_filenames=frozenset({"AGENTS.md"}),
        fallback_paths=("~/.opencode/bin/opencode",),
        load_bearing=True,
        probe_expected_root=frozenset({"FIXTURE_TOKEN_AGENTS_ROOT"}),
        structured_choice="the `question` tool",
        instructions_ref="the shared instructions file's",
        instructions_ref_bare="the shared instructions file",
        has_session_start_hook=False,
        skill_src_pattern="opencode/command/<name>.md",
        skill_dest_pattern="~/.config/opencode/commands/<name>.md",
        skill_ref_dir="ref",
        probe_command="opencode -p",
        commit_scope="opencode",
    ),
    "agy": HarnessSpec(
        name="agy",
        cli="agy",
        install_hint="internal workstation installation",
        expected_filenames=frozenset(),
        fallback_paths=("~/.local/bin/agy",),
        load_bearing=False,
        probe_expected_root=frozenset(),
        structured_choice="",
        instructions_ref="the shared instructions file's",
        instructions_ref_bare="the shared instructions file",
        has_session_start_hook=False,
        skill_src_pattern="agy/skills/<name>/SKILL.md",
        skill_dest_pattern="~/.gemini/antigravity-cli/skills/<name>/SKILL.md",
        skill_ref_dir="references",
        probe_command="agy -p",
        commit_scope="agy",
    ),
    "pi": HarnessSpec(
        name="pi",
        cli="pi",
        install_hint="npm install -g @mariozechner/pi-cli",
        expected_filenames=frozenset({"AGENTS.md"}),
        fallback_paths=("~/.npm-global/bin/pi",),
        load_bearing=False,
        probe_expected_root=frozenset({"FIXTURE_TOKEN_AGENTS_ROOT"}),
        structured_choice="the `question` tool",
        instructions_ref="the shared instructions file's",
        instructions_ref_bare="the shared instructions file",
        has_session_start_hook=False,
        skill_src_pattern="pi/skills/<name>/SKILL.md",
        skill_dest_pattern="~/.pi/agent/skills/<name>/SKILL.md",
        skill_ref_dir="references",
        probe_command="pi -p",
        commit_scope="pi",
    ),
    "codex": HarnessSpec(
        name="codex",
        cli="codex",
        install_hint="npm install -g @openai/codex",
        expected_filenames=frozenset({"AGENTS.md"}),
        fallback_paths=("~/.npm-global/bin/codex",),
        load_bearing=False,
        probe_expected_root=frozenset({"FIXTURE_TOKEN_AGENTS_ROOT"}),
        structured_choice="",
        instructions_ref="the shared instructions file's",
        instructions_ref_bare="the shared instructions file",
        has_session_start_hook=False,
        skill_src_pattern="codex/skills/<name>/SKILL.md",
        skill_dest_pattern="~/.codex/skills/<name>/SKILL.md",
        skill_ref_dir="ref",
        probe_command="codex exec",
        commit_scope="codex",
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
