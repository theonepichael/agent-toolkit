#!/usr/bin/env python3
"""gen_skills.py — regenerate the dashboard/grill-me/backlog-item/make-skill/
spec/standup/to-tickets skill copies from one template per skill, plus a
shared per-harness capability table. dashboard/grill-me/backlog-item/
make-skill cover all 5 harnesses (claude, copilot, opencode, agy, pi);
spec/standup/to-tickets cover only claude/opencode/pi — see
`SKILL_HARNESSES` below and AGENTS.md's "Harness maintenance tiers" section
for why copilot/agy stop getting new generated skills.

The first 4 skills used to live as hand-forked copies, one per harness, with
no mechanism keeping them in sync (see `meta-pi-skill-content-mismatch`'s
backlog record for the drift this caused: Pi's copies were reused from
agy's, describing agy's constraints — no structured multi-choice widget, no
SessionStart hook — that are factually wrong for Pi, which has both).
spec/standup/to-tickets had the same drift for Pi specifically
(`meta-pi-residual-skill-drift`). This script replaces those copies with
generated output: one body template per skill
(`templates/{dashboard,grill_me,backlog_item,make_skill,spec,standup,to_tickets}.md.tmpl`)
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
    python3 claude/scripts/gen_skills.py            rewrite every copy
    python3 claude/scripts/gen_skills.py --check     exit 1 if any copy is stale
    python3 claude/scripts/gen_skills.py --stdout    print the rendered copies,
                                                       write nothing

Flags: --check, --stdout, --repo-root <path>, --quiet/-q, --verbose/-v.
Env vars: none.
Files read: <repo>/templates/{dashboard,grill_me,backlog_item,make_skill,spec,standup,to_tickets}.md.tmpl.
Files written: the 29 (skill, harness) copies named in OUTPUT_PATHS —
4 skills x 5 harnesses (20) plus 3 skills x 3 harnesses (9), per
`SKILL_HARNESSES` (skipped by --check and --stdout).
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
    "grill-me",
    "backlog-item",
    "make-skill",
    "spec",
    "standup",
    "to-tickets",
)
HARNESSES = ("claude", "copilot", "opencode", "agy", "pi")

# Per skill, which harnesses get a generated copy. Every skill defaults to
# the full HARNESSES tuple except spec/standup/to-tickets, which cover only
# claude/opencode/pi -- per AGENTS.md's "Harness maintenance tiers": copilot
# and agy are best-effort and not proactively extended with new generated
# skills (meta-pi-residual-skill-drift's scope decision).
_ACTIVE_TIER = ("claude", "opencode", "pi")
SKILL_HARNESSES: dict[str, tuple[str, ...]] = {
    "dashboard": HARNESSES,
    "grill-me": HARNESSES,
    "backlog-item": HARNESSES,
    "make-skill": HARNESSES,
    "spec": _ACTIVE_TIER,
    "standup": _ACTIVE_TIER,
    "to-tickets": _ACTIVE_TIER,
}

TEMPLATE_PATHS: dict[str, str] = {
    "dashboard": "templates/dashboard.md.tmpl",
    "grill-me": "templates/grill_me.md.tmpl",
    "backlog-item": "templates/backlog_item.md.tmpl",
    "make-skill": "templates/make_skill.md.tmpl",
    "spec": "templates/spec.md.tmpl",
    "standup": "templates/standup.md.tmpl",
    "to-tickets": "templates/to_tickets.md.tmpl",
}

OUTPUT_PATHS: dict[tuple[str, str], str] = {
    ("dashboard", "claude"): "claude/commands/dashboard.md",
    ("dashboard", "copilot"): "copilot/skills/dashboard/SKILL.md",
    ("dashboard", "opencode"): "opencode/command/dashboard.md",
    ("dashboard", "agy"): "agy/skills/dashboard/SKILL.md",
    ("dashboard", "pi"): "pi/skills/dashboard/SKILL.md",
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
    ("make-skill", "pi"): "pi/skills/make-skill/SKILL.md",
    ("spec", "claude"): "claude/commands/spec.md",
    ("spec", "opencode"): "opencode/command/spec.md",
    ("spec", "pi"): "pi/skills/spec/SKILL.md",
    ("standup", "claude"): "claude/commands/standup.md",
    ("standup", "opencode"): "opencode/command/standup.md",
    ("standup", "pi"): "pi/skills/standup/SKILL.md",
    ("to-tickets", "claude"): "claude/commands/to-tickets.md",
    ("to-tickets", "opencode"): "opencode/command/to-tickets.md",
    ("to-tickets", "pi"): "pi/skills/to-tickets/SKILL.md",
}

# A line that is nothing but one `{{TOKEN}}` -- see render_body.
WHOLE_LINE_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


def do_not_edit_marker(skill: str) -> str:
    """Return this skill's marker, naming its own template file by name.

    Deliberately not a literal import of `gen_second_opinion.DO_NOT_EDIT_MARKER`
    (that constant names `gen_second_opinion.py` and a single template, both
    wrong here): this script drives 4 templates, and a developer editing the
    wrong one of the 4 is a real failure mode a generic marker wouldn't
    prevent, so each marker names its own `.tmpl` path specifically.
    """
    return (
        f"<!-- generated by claude/scripts/gen_skills.py — do not edit; "
        f"edit {TEMPLATE_PATHS[skill]} for shared wording or gen_skills.py's "
        "CAPABILITY_TABLE / *_PARAMS tables for harness-specific wording, "
        "then regenerate -->"
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
    return f"{frontmatter}\n{do_not_edit_marker(skill)}\n\n{body}"


def render_all(
    repo_root: Path, skill_params: dict[str, dict[str, dict]]
) -> dict[str, str]:
    """Render every (skill, harness) pair, keyed by its repo-relative output path."""
    rendered: dict[str, str] = {}
    for skill in SKILLS:
        template_text = (repo_root / TEMPLATE_PATHS[skill]).read_text(encoding="utf-8")
        for harness in SKILL_HARNESSES[skill]:
            relpath = OUTPUT_PATHS[(skill, harness)]
            params = skill_params[skill][harness]
            rendered[relpath] = render_one(skill, harness, template_text, params)
    return rendered


# ── cli ──────────────────────────────────────────────────────────────────────


def default_repo_root() -> Path:
    """Return the repo root inferred from this script's real location."""
    return Path(__file__).resolve().parents[2]


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
            "`python3 claude/scripts/gen_skills.py`",
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
