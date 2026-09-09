#!/usr/bin/env python3
"""gen_skills.py — regenerate the dashboard/recap/grill-me/backlog-item/
make-skill/spec/standup/to-tickets/swarm skill copies from one template per
skill, plus a shared per-harness capability table. dashboard/recap/
grill-me/backlog-item/make-skill/spec/standup/to-tickets cover all 6
harnesses (claude, copilot, opencode, agy, pi, codex); swarm covers only
claude/copilot (user-directed; pi already owns the orchestration surface) —
see `SKILL_HARNESSES` below and AGENTS.md's "Harness maintenance tiers" section.

The first 4 skills used to live as hand-forked copies, one per harness, with
no mechanism keeping them in sync (see `meta-pi-skill-content-mismatch`'s
backlog record for the drift this caused: Pi's copies were reused from
agy's, describing agy's constraints — no structured multi-choice widget, no
SessionStart hook — that are factually wrong for Pi, which has both).
spec/standup/to-tickets had the same drift for Pi specifically
(`meta-pi-residual-skill-drift`). This script replaces those copies with
generated output: one body template per skill
(`templates/{dashboard,recap,grill_me,backlog_item,make_skill,spec,standup,to_tickets,swarm}.md.tmpl`)
plus the shared `CAPABILITY_TABLE` below, mirroring
`gen_second_opinion.py`'s generator/--check/--stdout shape for the
second-opinion skill (which this script does not touch — a separate,
already-working generator, decision recorded in the plan this script
implements).

Unlike `gen_second_opinion.py`'s single-template, prose-heavy body (where a
textwrap reflow pass is safe), these skills' bodies lean heavily on
numbered/bulleted lists with no blank line between items — reflowing those
by joining lines with `textwrap.fill` would merge list items into one
paragraph and corrupt the list. `render_body` here is therefore a plainer,
non-reflowing substitution: every line is substituted in place and emitted
verbatim, with the one exception (a whole-line `{{TOKEN}}`) allowed to carry
a multi-line value of its own, same as `gen_second_opinion.py`'s
`WHOLE_LINE_PLACEHOLDER` mechanism.

Usage:
    python3 agent-scripts/gen_skills.py            rewrite every copy
    python3 agent-scripts/gen_skills.py --check     exit 1 if any copy is stale
    python3 agent-scripts/gen_skills.py --stdout    print the rendered copies,
                                                       write nothing

Flags: --check, --stdout, --repo-root <path>, --quiet/-q, --verbose/-v.
Env vars: none.
Files read: <repo>/templates/{dashboard,recap,grill_me,backlog_item,make_skill,spec,standup,to_tickets,swarm}.md.tmpl.
Files written: the 58 (skill, harness) copies named in OUTPUT_PATHS —
8 skills x 6 harnesses (48), plus swarm x {claude, copilot} (2), plus a
second pi/prompts/*.md output for each of the 8 skills under the synthetic
"pi-prompt" harness (8), per `SKILL_HARNESSES` (skipped by --check and
--stdout).
Exit codes: 0 success; 1 --check found stale output; 2 bad usage.

Requires Python 3.12+.
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

import cli_common

SKILLS = (
    "dashboard",
    "recap",
    "grill-me",
    "backlog-item",
    "make-skill",
    "spec",
    "standup",
    "to-tickets",
    "swarm",
)
HARNESSES = ("claude", "copilot", "opencode", "agy", "pi", "codex")

# Per skill, which harnesses get a generated copy. All 6 harnesses are in
# _ACTIVE_TIER (full active parity, decided 2026-09-08). swarm deviates:
# claude/copilot only, user-directed: copilot was explicitly requested,
# opencode/agy/codex were not, and pi already owns the swarm orchestration
# surface (pi/prompts/backlog-item.md's --swarm[=N] section plus
# pi/extensions/swarm-tool.ts), so a pi copy would be exactly the
# "second copy is a second thing to drift" problem swarm.md's own closing
# section warns against.
_ACTIVE_TIER = HARNESSES
# The 8 gen_skills.py-managed skills each get a second pi output --
# pi/prompts/{name}.md, the "pi-prompt" synthetic harness (see
# TEMPLATE_PATH_OVERRIDES) -- alongside their pi/skills/{name}/SKILL.md.
# swarm doesn't: pi already owns the swarm orchestration surface natively
# (pi/prompts/backlog-item.md's --swarm[=N] section), so a swarm-specific
# pi/prompts output would be exactly the "second copy is a second thing to
# drift" problem this fix exists to close, not extend.
SKILL_HARNESSES: dict[str, tuple[str, ...]] = {
    "dashboard": HARNESSES + ("pi-prompt",),
    "recap": HARNESSES + ("pi-prompt",),
    "grill-me": HARNESSES + ("pi-prompt",),
    "backlog-item": HARNESSES + ("pi-prompt",),
    "make-skill": HARNESSES + ("pi-prompt",),
    "spec": _ACTIVE_TIER + ("pi-prompt",),
    "standup": _ACTIVE_TIER + ("pi-prompt",),
    "to-tickets": _ACTIVE_TIER + ("pi-prompt",),
    "swarm": ("claude", "copilot"),
}

TEMPLATE_PATHS: dict[str, str] = {
    "dashboard": "templates/dashboard.md.tmpl",
    "recap": "templates/recap.md.tmpl",
    "grill-me": "templates/grill_me.md.tmpl",
    "backlog-item": "templates/backlog_item.md.tmpl",
    "make-skill": "templates/make_skill.md.tmpl",
    "spec": "templates/spec.md.tmpl",
    "standup": "templates/standup.md.tmpl",
    "to-tickets": "templates/to_tickets.md.tmpl",
    "swarm": "templates/swarm.md.tmpl",
}

# Per-(skill, harness) template overrides. Pi has two output surfaces per
# skill: `pi/prompts/{name}.md` binds pi's literal `/name` slash command
# (prompt-template mechanism), and `pi/skills/{name}/SKILL.md` binds
# `/skill:name` or semantic match. 7 of the 8 skills integrate pi-native
# extension tools (`dev_status`, `grill`, `standup`, `question`, `delegate`,
# `swarm_resolve_blocked`) that the generic bash-oriented templates never
# reference. Both Pi surfaces (`pi` and `pi-prompt`) render from the dedicated
# native template for those 7 skills, unifying Pi's behavior across `/name`
# and `/skill:name`. `make-skill` is deliberately absent: it has no
# native-tool content, so both its outputs reuse TEMPLATE_PATHS["make-skill"]
# unchanged (same pattern gen_second_opinion.py uses for second-opinion).
TEMPLATE_PATH_OVERRIDES: dict[tuple[str, str], str] = {
    ("dashboard", "pi"): "templates/dashboard_pi_native.md.tmpl",
    ("recap", "pi"): "templates/recap_pi_native.md.tmpl",
    ("grill-me", "pi"): "templates/grill_me_pi_native.md.tmpl",
    ("backlog-item", "pi"): "templates/backlog_item_pi_native.md.tmpl",
    ("spec", "pi"): "templates/spec_pi_native.md.tmpl",
    ("standup", "pi"): "templates/standup_pi_native.md.tmpl",
    ("to-tickets", "pi"): "templates/to_tickets_pi_native.md.tmpl",
    ("dashboard", "pi-prompt"): "templates/dashboard_pi_native.md.tmpl",
    ("recap", "pi-prompt"): "templates/recap_pi_native.md.tmpl",
    ("grill-me", "pi-prompt"): "templates/grill_me_pi_native.md.tmpl",
    ("backlog-item", "pi-prompt"): "templates/backlog_item_pi_native.md.tmpl",
    ("spec", "pi-prompt"): "templates/spec_pi_native.md.tmpl",
    ("standup", "pi-prompt"): "templates/standup_pi_native.md.tmpl",
    ("to-tickets", "pi-prompt"): "templates/to_tickets_pi_native.md.tmpl",
}


def template_path_for(skill: str, harness: str) -> str:
    """Return the template path this (skill, harness) pair renders from."""
    return TEMPLATE_PATH_OVERRIDES.get((skill, harness), TEMPLATE_PATHS[skill])


OUTPUT_PATHS: dict[tuple[str, str], str] = {
    ("dashboard", "claude"): "claude/commands/dashboard.md",
    ("dashboard", "copilot"): "copilot/skills/dashboard/SKILL.md",
    ("dashboard", "opencode"): "opencode/command/dashboard.md",
    ("dashboard", "agy"): "agy/skills/dashboard/SKILL.md",
    ("dashboard", "pi"): "pi/skills/dashboard/SKILL.md",
    ("recap", "claude"): "claude/commands/recap.md",
    ("recap", "copilot"): "copilot/skills/recap/SKILL.md",
    ("recap", "opencode"): "opencode/command/recap.md",
    ("recap", "agy"): "agy/skills/recap/SKILL.md",
    ("recap", "pi"): "pi/skills/recap/SKILL.md",
    ("grill-me", "claude"): "claude/commands/grill-me.md",
    ("grill-me", "copilot"): "copilot/skills/grill-me/SKILL.md",
    ("grill-me", "opencode"): "opencode/command/grill-me.md",
    ("grill-me", "agy"): "agy/skills/grill-me/SKILL.md",
    ("grill-me", "pi"): "pi/skills/grill-me/SKILL.md",
    ("backlog-item", "claude"): "claude/commands/backlog-item.md",
    ("backlog-item", "copilot"): "copilot/skills/backlog-item/SKILL.md",
    ("backlog-item", "opencode"): "opencode/command/backlog-item.md",
    ("backlog-item", "agy"): "agy/skills/backlog-item/SKILL.md",
    ("backlog-item", "pi"): "pi/skills/backlog-item/SKILL.md",
    ("make-skill", "claude"): "claude/commands/make-skill.md",
    ("make-skill", "copilot"): "copilot/skills/make-skill/SKILL.md",
    ("make-skill", "opencode"): "opencode/command/make-skill.md",
    ("make-skill", "agy"): "agy/skills/make-skill/SKILL.md",
    ("dashboard", "codex"): "codex/skills/dashboard/SKILL.md",
    ("recap", "codex"): "codex/skills/recap/SKILL.md",
    ("grill-me", "codex"): "codex/skills/grill-me/SKILL.md",
    ("backlog-item", "codex"): "codex/skills/backlog-item/SKILL.md",
    ("make-skill", "codex"): "codex/skills/make-skill/SKILL.md",
    ("make-skill", "pi"): "pi/skills/make-skill/SKILL.md",
    ("spec", "claude"): "claude/commands/spec.md",
    ("spec", "copilot"): "copilot/skills/spec/SKILL.md",
    ("spec", "opencode"): "opencode/command/spec.md",
    ("spec", "agy"): "agy/skills/spec/SKILL.md",
    ("spec", "pi"): "pi/skills/spec/SKILL.md",
    ("spec", "codex"): "codex/skills/spec/SKILL.md",
    ("standup", "claude"): "claude/commands/standup.md",
    ("standup", "copilot"): "copilot/skills/standup/SKILL.md",
    ("standup", "opencode"): "opencode/command/standup.md",
    ("standup", "agy"): "agy/skills/standup/SKILL.md",
    ("standup", "pi"): "pi/skills/standup/SKILL.md",
    ("standup", "codex"): "codex/skills/standup/SKILL.md",
    ("to-tickets", "claude"): "claude/commands/to-tickets.md",
    ("to-tickets", "copilot"): "copilot/skills/to-tickets/SKILL.md",
    ("to-tickets", "opencode"): "opencode/command/to-tickets.md",
    ("to-tickets", "agy"): "agy/skills/to-tickets/SKILL.md",
    ("to-tickets", "pi"): "pi/skills/to-tickets/SKILL.md",
    ("to-tickets", "codex"): "codex/skills/to-tickets/SKILL.md",
    ("swarm", "claude"): "claude/commands/swarm.md",
    ("swarm", "copilot"): "copilot/skills/swarm/SKILL.md",
    ("dashboard", "pi-prompt"): "pi/prompts/dashboard.md",
    ("recap", "pi-prompt"): "pi/prompts/recap.md",
    ("grill-me", "pi-prompt"): "pi/prompts/grill-me.md",
    ("backlog-item", "pi-prompt"): "pi/prompts/backlog-item.md",
    ("make-skill", "pi-prompt"): "pi/prompts/make-skill.md",
    ("spec", "pi-prompt"): "pi/prompts/spec.md",
    ("standup", "pi-prompt"): "pi/prompts/standup.md",
    ("to-tickets", "pi-prompt"): "pi/prompts/to-tickets.md",
}

# A line that is nothing but one `{{TOKEN}}` -- see render_body.
WHOLE_LINE_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


def do_not_edit_marker(skill: str, harness: str) -> str:
    """Return this (skill, harness) pair's marker, naming its real template.

    Deliberately not a literal import of `gen_second_opinion.DO_NOT_EDIT_MARKER`
    (that constant names `gen_second_opinion.py` and a single template, both
    wrong here): this script drives many templates, and a developer editing
    the wrong one is a real failure mode a generic marker wouldn't prevent,
    so each marker names its own `.tmpl` path specifically -- via
    `template_path_for`, since a pi-prompt output can name a different
    template than its skill's other harnesses.
    """
    return (
        f"<!-- generated by agent-scripts/gen_skills.py — do not edit; "
        f"edit {template_path_for(skill, harness)} for shared wording or "
        "gen_skills.py's CAPABILITY_TABLE / *_PARAMS tables for "
        "harness-specific wording, then regenerate -->"
    )


# ── shared per-harness capability facts ────────────────────────────────────
#
# One dict, keyed by harness name, of facts referenced by more than one
# template's placeholders. Not every skill uses every fact (CLAUDE.md's
# plan §3) -- e.g. only grill-me and backlog-item need STRUCTURED_CHOICE.
#
# Sources: `copilot/CLAUDE_CODE_PARITY.md`, `opencode/CLAUDE_CODE_PARITY.md`,
# `agy/CLAUDE_CODE_PARITY.md`, `pi/CLAUDE_CODE_PARITY.md` (each harness's own
# confirmed-facts doc, re-checked while writing this table, not assumed from
# the pre-fix hand-forked copies -- those are exactly what was wrong).

CAPABILITY_TABLE: dict[str, dict[str, str | bool]] = {
    "codex": {
        # Verified against Codex CLI 0.153.4's official docs and --help
        # surface (2026-09-08): no AskUserQuestion-style structured
        # multi-choice widget exists; judgment calls go through plain text.
        "structured_choice": "",
        "instructions_ref": "the shared instructions file's",
        "instructions_ref_bare": "the shared instructions file",
        # Codex itself has SessionStart hook events, but this toolkit
        # provisions no codex hooks: every non-managed codex hook needs
        # per-definition trust review (/hooks, hash-tracked) before it runs,
        # a poor fit for provisioned files.
        "has_session_start_hook": False,
        "skill_src_pattern": "codex/skills/<name>/SKILL.md",
        # Codex's USER-scope skills dir, sibling to the bundled .system/ one
        # -- confirmed live via `codex debug prompt-input`'s skill-roots
        # table and Codex's own skill-installer skill (2026-09-08,
        # atk-codex-skill-root-fix); ~/.agents/skills/ is never scanned.
        # UPDATE 2026-09-08 (atk-codex-skill-copy-fix): this destination is
        # reached by install.py's sync_codex_skills() copying the file into
        # place, not a links.toml [[link]] symlink row like every other
        # harness here -- Codex's skill scanner does not follow symlinks for
        # USER-scope discovery (confirmed live the same way). The 5
        # generated skills are still self-contained, no ref/ dirs.
        "skill_dest_pattern": "~/.codex/skills/<name>/SKILL.md",
        "skill_ref_dir": "ref",
        "probe_command": "codex exec",
        "commit_scope": "codex",
    },
    "claude": {
        # Claude Code's structured multi-choice UI is AskUserQuestion.
        "structured_choice": "AskUserQuestion",
        "instructions_ref": "CLAUDE.md's",
        "instructions_ref_bare": "CLAUDE.md",
        # Claude Code hooks include a real SessionStart event, wired in
        # this repo (claude/settings.json).
        "has_session_start_hook": True,
        "skill_src_pattern": "claude/commands/<name>.md",
        "skill_dest_pattern": "~/.claude/commands/<name>.md",
        "skill_ref_dir": "ref",
        "probe_command": "claude -p",
        "commit_scope": "claude",
    },
    "copilot": {
        # Confirmed: no AskUserQuestion-style widget anywhere in Copilot
        # CLI's docs/help surface as of the 2026-08-19 re-check (parity
        # doc §1); `ask_user` exists only behind an untested `--plan`/TUI
        # mode, not the `-p` invocation these skills run under.
        "structured_choice": "",
        "instructions_ref": "the shared instructions file's",
        "instructions_ref_bare": "the shared instructions file",
        # Copilot's sessionStart hook is confirmed live (parity doc §1/§3):
        # `copilot/hooks/session-start.json` runs `dev_status.py render`
        # at session open.
        "has_session_start_hook": True,
        "skill_src_pattern": "copilot/skills/<name>/SKILL.md",
        "skill_dest_pattern": "~/.copilot/skills/<name>/SKILL.md",
        "skill_ref_dir": "ref",
        "probe_command": "copilot -p",
        "commit_scope": "copilot",
    },
    "opencode": {
        # opencode has its own structured `question` tool (parity doc §1,
        # corroborated by opencode's own command/skill files).
        "structured_choice": "the `question` tool",
        "instructions_ref": "the shared instructions file's",
        "instructions_ref_bare": "the shared instructions file",
        # No SessionStart-equivalent: Claude Code hooks have no
        # declarative equivalent in opencode, only a TypeScript plugin
        # system, and the dashboard-on-open port is explicitly deferred
        # (parity doc §5).
        "has_session_start_hook": False,
        "skill_src_pattern": "opencode/command/<name>.md",
        "skill_dest_pattern": "~/.config/opencode/commands/<name>.md",
        "skill_ref_dir": "ref",
        "probe_command": "opencode -p",
        "commit_scope": "opencode",
    },
    "agy": {
        # Confirmed: no AskUserQuestion-style widget in --help, agent/plugin
        # subcommands, or agy's own customization docs (parity doc §3).
        "structured_choice": "",
        "instructions_ref": "the shared instructions file's",
        "instructions_ref_bare": "the shared instructions file",
        # hooks.md lists exactly PreToolUse/PostToolUse/PreInvocation/
        # PostInvocation/Stop -- no SessionStart event exists (parity
        # doc §3).
        "has_session_start_hook": False,
        "skill_src_pattern": "agy/skills/<name>/SKILL.md",
        "skill_dest_pattern": "~/.gemini/antigravity-cli/skills/<name>/SKILL.md",
        "skill_ref_dir": "references",
        "probe_command": "agy -p",
        "commit_scope": "agy",
    },
    "pi": {
        # Pi has no built-in question/select tool (docs/usage.md's built-in
        # list is read/bash/powershell/edit/write/grep/find/ls), but this
        # repo ships one as an extension (`question-tool.ts`, pi/CLAUDE_CODE_PARITY.md
        # §5) -- unit-tested, recommendation-first enforced, a hard error
        # (not a silent fallback) in headless `-p`/JSON modes where there's
        # no UI to prompt through.
        "structured_choice": "the `question` tool",
        # Pi loads AGENTS.md with CLAUDE.md as a fallback name (pi/CLAUDE_CODE_PARITY.md
        # §1) -- same generic phrasing as every other non-Claude harness,
        # since neither name is Pi's own coinage.
        "instructions_ref": "the shared instructions file's",
        "instructions_ref_bare": "the shared instructions file",
        # `session_start` is a real extension event (docs/extensions.md),
        # but no extension hooked to it ships in this repo yet (pi/CLAUDE_CODE_PARITY.md
        # §8, "out of scope for this port") -- so, same as opencode/agy,
        # nothing auto-surfaces anything at session open today.
        "has_session_start_hook": False,
        "skill_src_pattern": "pi/skills/<name>/SKILL.md",
        "skill_dest_pattern": "~/.pi/agent/skills/<name>/SKILL.md",
        # Pi implements the Agent Skills standard (pi/CLAUDE_CODE_PARITY.md
        # §1), the same spec agy/skills/ already follows -- references/ is
        # that standard's subdirectory name, not claude/copilot's ref/.
        "skill_ref_dir": "references",
        "probe_command": "pi -p",
        "commit_scope": "pi",
    },
    # Synthetic harness for pi/prompts/{name}.md's second output surface --
    # not a real member of HARNESSES (see TEMPLATE_PATH_OVERRIDES above).
    # Same facts as "pi": it's still pi under the hood, just a different
    # invocation surface, so make-skill's shared-template rendering (the
    # only "pi-prompt" consumer that actually substitutes these tokens)
    # gets identical values to pi/skills/make-skill/SKILL.md's.
    "pi-prompt": {
        "structured_choice": "the `question` tool",
        "instructions_ref": "the shared instructions file's",
        "instructions_ref_bare": "the shared instructions file",
        "has_session_start_hook": False,
        "skill_src_pattern": "pi/skills/<name>/SKILL.md",
        "skill_dest_pattern": "~/.pi/agent/skills/<name>/SKILL.md",
        "skill_ref_dir": "references",
        "probe_command": "pi -p",
        "commit_scope": "pi",
    },
}


def capability_tokens(harness: str) -> dict[str, str]:
    """Map the shared capability facts to the `{{TOKEN}}` names templates use."""
    facts = CAPABILITY_TABLE[harness]
    return {
        "INSTRUCTIONS_REF": str(facts["instructions_ref"]),
        "INSTRUCTIONS_REF_BARE": str(facts["instructions_ref_bare"]),
        "SKILL_REF_DIR": str(facts["skill_ref_dir"]),
        "PROBE_COMMAND": str(facts["probe_command"]),
        "COMMIT_SCOPE": str(facts["commit_scope"]),
    }


# ── rendering ────────────────────────────────────────────────────────────────


def apply_placeholders(text: str, values: dict[str, str]) -> str:
    """Replace every `{{TOKEN}}` in ``text`` with its harness-specific value."""
    for token, value in values.items():
        text = text.replace("{{" + token + "}}", value)
    return text


def render_body(template_text: str, values: dict[str, str]) -> str:
    """Render one harness's body: substitute placeholders, no reflow.

    A whole-line `{{TOKEN}}` (the entire stripped line is one placeholder)
    is replaced with its value verbatim, so that value can be empty, span
    multiple lines, or carry its own markdown (a list, a code fence) without
    the substitution result being re-wrapped. Every other line is substituted
    in place and emitted exactly as the template wrote it -- deliberately no
    `textwrap.fill` pass (unlike `gen_second_opinion.py`'s `render_body`):
    these 4 skills' bodies are numbered/bulleted lists with no blank line
    between items, and joining list-item lines into one paragraph before
    refilling would merge them into broken prose.
    """
    lines = template_text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if WHOLE_LINE_PLACEHOLDER.fullmatch(stripped):
            out.append(apply_placeholders(stripped, values))
        else:
            out.append(apply_placeholders(line, values))
    return "\n".join(out).rstrip("\n") + "\n"


def render_one(skill: str, harness: str, template_text: str, params: dict) -> str:
    """Render one (skill, harness) pair's complete file."""
    values = {**capability_tokens(harness), **params}
    frontmatter = values.pop("FRONTMATTER")
    body = render_body(template_text, values)
    return f"{frontmatter}\n{do_not_edit_marker(skill, harness)}\n\n{body}"


def render_all(
    repo_root: Path, skill_params: dict[str, dict[str, dict]]
) -> dict[str, str]:
    """Render every (skill, harness) pair, keyed by its repo-relative output path."""
    rendered: dict[str, str] = {}
    for skill in SKILLS:
        for harness in SKILL_HARNESSES[skill]:
            template_text = (repo_root / template_path_for(skill, harness)).read_text(
                encoding="utf-8"
            )
            relpath = OUTPUT_PATHS[(skill, harness)]
            params = skill_params[skill][harness]
            rendered[relpath] = render_one(skill, harness, template_text, params)
    return rendered


# ── cli ──────────────────────────────────────────────────────────────────────


def default_repo_root() -> Path:
    """Return the repo root inferred from this script's real location."""
    return Path(__file__).resolve().parents[1]


def main() -> None:
    """Parse argv, then regenerate, check, or print the skill copies."""
    parser = argparse.ArgumentParser(
        prog="gen_skills",
        description="regenerate the dashboard/grill-me/backlog-item/make-skill "
        "copies from one template per skill",
    )
    cli_common.add_verbosity_args(parser)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 (with diffs on stderr) if any copy is stale",
    )
    parser.add_argument(
        "--stdout", action="store_true", help="print the rendered copies, write nothing"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        metavar="<path>",
        help="repository root (default: inferred from this script's path)",
    )
    args = parser.parse_args()

    repo_root = (args.repo_root or default_repo_root()).resolve()
    for skill, relpath in TEMPLATE_PATHS.items():
        if not (repo_root / relpath).is_file():
            print(f"[gen_skills] no {relpath} under {repo_root}", file=sys.stderr)
            sys.exit(2)
    valid_harnesses = set(HARNESSES) | {"pi-prompt"}
    for (override_skill, override_harness), relpath in TEMPLATE_PATH_OVERRIDES.items():
        if override_skill not in SKILLS:
            print(
                f"[gen_skills] TEMPLATE_PATH_OVERRIDES key names unknown skill "
                f"{override_skill!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        if override_harness not in valid_harnesses:
            print(
                f"[gen_skills] TEMPLATE_PATH_OVERRIDES key names unknown harness "
                f"{override_harness!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        if not (repo_root / relpath).is_file():
            print(f"[gen_skills] no {relpath} under {repo_root}", file=sys.stderr)
            sys.exit(2)

    # Imported here (not at module scope) so --stdout/--check/regen all work
    # even before skill_params.py exists during early development; kept
    # local also keeps the params tables (large, mostly-literal content) out
    # of this module's own diff noise.
    from gen_skills_params import SKILL_PARAMS

    rendered = render_all(repo_root, SKILL_PARAMS)

    if args.stdout:
        for relpath, text in rendered.items():
            sys.stdout.write(f"# ── {relpath} ──\n{text}\n")
        return

    if args.check:
        stale: list[str] = []
        for relpath, text in rendered.items():
            output = repo_root / relpath
            current = output.read_text(encoding="utf-8") if output.is_file() else ""
            if current != text:
                stale.append(relpath)
                diff = difflib.unified_diff(
                    current.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=f"{relpath} (on disk)",
                    tofile=f"{relpath} (generated)",
                )
                sys.stderr.writelines(diff)
        if not stale:
            cli_common.qprint(
                f"[gen_skills] all {len(rendered)} copies are up to date",
                quiet=args.quiet,
            )
            return
        print(
            f"[gen_skills] stale: {', '.join(stale)} — run "
            "`python3 agent-scripts/gen_skills.py`",
            file=sys.stderr,
        )
        sys.exit(1)

    for relpath, text in rendered.items():
        out_path = repo_root / relpath
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    cli_common.qprint(f"[gen_skills] wrote {len(rendered)} copies", quiet=args.quiet)


if __name__ == "__main__":
    main()
