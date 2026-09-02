# agy (Antigravity CLI) → Claude Code parity notes

Goal: make agy (Google's Antigravity CLI, the May 2026 Go rewrite of Gemini
CLI, Gemini-backed) feel like Claude Code for this workflow — same shared
instructions, same skills, same backlog/pending-items/git conventions.
Compiled 2026-07-28 by reading agy's own bundled customization docs
(`~/.gemini/antigravity-cli/builtin/skills/agy-customizations/`) and the
official migration docs at antigravity.google/docs/cli/gcli-migration.
Revalidated 2026-08-13 against the installed agy 1.1.12 (see §3 and §4 —
slash invocation of skills now exists; the §3 invocation claim from the
1.1.8 era was rewritten).

---

## 1. Confirmed facts (read the docs before changing any of this)

- **Global rules file (CLAUDE.md-equivalent): `~/.gemini/GEMINI.md`.**
  Official docs: "The agent automatically consults and enforces your global
  constraints located at `~/.gemini/GEMINI.md`." No frontmatter support —
  always active, unlike skills' progressive disclosure. `install.sh` symlinks
  `claude/global-instructions.md` straight to this path, same pattern as
  `~/.copilot/copilot-instructions.md`.
- **Global skills path: `~/.gemini/antigravity-cli/skills/<name>/SKILL.md`.**
  This is the *current* path — it moved from a *legacy* `~/.gemini/skills/`
  path. Before this port, all 5 skills existed as untracked, stale copies at
  the legacy path (dated 2026-07-28, already out of sync with the current
  claude/copilot content) — those are superseded by this port and should be
  removed once the new symlinks are confirmed working (not automated here;
  `install.sh`'s `symlink` helper only touches its own destination paths).
- **Skill frontmatter**: only `name` + `description` required, confirmed via
  agy's own `agy-customizations` skill docs. No `allowed-tools` equivalent
  observed — agy's skill files here carry no such field, unlike Claude
  Code's granular list or Copilot's blanket `allowed-tools: shell`.
- **Reference-material subdirectory**: agy's own docs specify `references/`
  (not `ref/`, which is this repo's `claude`/`copilot` convention) as the
  supported subdirectory for bulky per-skill documentation.
- **Non-interactive print mode**: `agy -p '<prompt>'` (confirmed via
  `agy --help` on the installed 1.1.12 build).
- **Workspace (project-scoped) rules and skills** are a separate mechanism
  from the above — `AGENTS.md`/`GEMINI.md` files discovered by walking from
  cwd up to the repo root, and `.agents/skills/` for project-local skills.
  Not wired by this repo (mirrors this repo's stance on Claude Code project
  `CLAUDE.md` files — personal global tooling, not per-project).

## 2. Skills — ported (`agy/skills/`)

5 of 7 (`dashboard`, `grill-me`, `make-skill`, `second-opinion`, `standup`)
ported from the current `claude/commands/`/`copilot/skills/` content as of
2026-07-28, not from the stale legacy-path copies that predated this doc.
`standup` and `second-opinion` needed no agy-specific rewording beyond
dropping `allowed-tools` — their bodies were already harness-neutral after
the same-day consolidation pass that introduced "the shared instructions
file" phrasing. `dashboard` and `make-skill` got small agy-specific
adjustments (verified `-p` flag, `references/` vs `ref/`, Trigger section
reflecting agy's progressive-disclosure model instead of Claude Code's
model-invoked/user-invoked split or Copilot's pure description-match).
`spec` was added later, 2026-08-12, following the same no-`allowed-tools`,
plain-text-question conventions as `standup`/`second-opinion`, and is now
covered by the 2026-08-13 revalidation in §3 (a `/spec` slash probe on
1.1.12 produced the skill's structured spec output).

**Known gap: `grill-me` is missing its "clear-and-go" step.** Claude Code's,
Copilot's, and opencode's `grill-me` all have a step 5 in "End of session"
that offers to mark the plan pending-execution (`grill.py
mark-pending-execution`) and describes how it gets picked back up next
session. `agy/skills/grill-me/SKILL.md` stops at step 4 — this step was
dropped, not adapted, during the port. Since agy has no `SessionStart` hook
(confirmed in §3 below), the fix is the same shape as opencode's version:
keep the offer, but describe manual resume (tell the user to run
`grill.py pending-plan --consume` themselves at the start of the next
session) instead of claiming an auto-surfacing hook that doesn't exist here.
Not yet ported — flagging as an open item rather than silently leaving it
missing.

`backlog-item` ported 2026-08-13 to `agy/skills/backlog-item/SKILL.md`,
closing the last skill-count gap with `claude/commands`, `copilot/skills`,
and `opencode/command` (all 7 now). The open question this port had been
waiting on — whether agy's model-decision-only skill activation (no
`skill` subcommand, unlike opencode's native `skill` tool) can support
`backlog-item`'s mid-run delegation to `grill-me`/`second-opinion` — was
resolved empirically before porting: a throwaway probe skill
(`_delegation-probe`, deleted after use) instructed the model to "activate
the dashboard skill... run its command for real," and `agy -p "run the
delegation probe"` genuinely executed `dev_status.py render`, matching live
backlog state rather than describing intent. Delegation works because the
model, told to activate a referenced skill, reads and follows that skill's
own SKILL.md body directly via its normal tool access — there's no discrete
tool call involved, just prose reference (the same pattern `grill-me` and
`spec`'s own SKILL.md files already use). The probe only validated a
trivial single-command delegation, though — `backlog-item`'s delegation
into `grill-me`/`second-opinion`'s much longer, stateful Q&A loops is
mitigated with explicit suspend-and-return framing (a printed checkpoint
marker, a `next_steps` return pointer persisted via `dev_status.py` so it
survives context compaction, and an absolute-path re-read of the return
step) rather than assumed to generalize untested. Full design record:
`~/.claude/data/grill/meta-backlog-item-port-agy-spec.md` and its
`-critique-notes.md` companion (3 rounds of second-opinion critique).

## 3. Verification results (probed 2026-07-28, agy 1.1.8; revalidated 2026-08-13, agy 1.1.12)

Resolved against `agy --help`, `agy help`, `agy agent`, agy's own bundled
customization docs (`~/.gemini/antigravity-cli/builtin/skills/agy-customizations/
docs/{skills,hooks,rules}.md`), and a **live end-to-end invocation probe**
(`agy --dangerously-skip-permissions -p "<activate dashboard; run
dev_status.py render; show stdout verbatim>"`), which produced the dashboard
verbatim — empirically confirming agy loads skills from the new
`~/.gemini/antigravity-cli/skills/` path in normal use. The legacy
`~/.gemini/skills/` directory (previously moved aside as
`~/.gemini/skills.stale-bak-20260728`) has been **permanently deleted**.

2026-08-13 revalidation on agy 1.1.12: `agy --version` → 1.1.12;
`agy --help` still lists only `agent`(s)/`changelog`/`help`/`install`/
`models`/`plugin`(s)/`update` subcommands (still no `skill` subcommand);
`agy models` listed the current model set; `agy -p` returned a successful
text response; and — new in this version — **slash invocation of skills
works**: `agy -p "/dashboard"` expanded the dashboard skill and rendered
the dashboard, and `agy -p "/spec ..."` produced the spec skill's
structured output. The `--help` surface now carries
`--disable-slash-commands` ("Disable slash command and skill expansion in
print mode"), i.e. slash/skill expansion is on by default. All 6 ported
skills (including `spec`) are present at
`~/.gemini/antigravity-cli/skills/<name>/SKILL.md`.

- **Slash invocation EXISTS as of 1.1.12 — the 1.1.8-era claim below is
  superseded.** Original 1.1.8 finding: no explicit user-typed skill
  invocation (no slash-command or dedicated subcommand); `skills.md`
  described only model-decision activation. On 1.1.12, typed
  `/<skill-name>` prompts expand and run the skill in print mode (probed
  with `/dashboard` and `/spec`; expansion can be disabled with
  `--disable-slash-commands`). There is still no `skill` subcommand and no
  interactive slash-command menu confirmed — treat the typed-slash path as
  prompt expansion, not a Claude-Code-style command palette. Model-decision
  activation also still works, so the ported skills' description-first
  Triggers remain correct as written.
- **No SessionStart hook event type.** `hooks.md` lists exactly five events:
  `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`.
  There is no `SessionStart` (or `OnSessionStart`/`Startup`) event. The
  nearest, `PreInvocation`, fires before **every** model invocation, not once
  per session, so it can't cleanly drive a one-shot dashboard auto-render
  without a self-clearing guard (e.g. a flag file the hook checks then
  unlinks) — and that fires on the *first* invocation only by side effect,
  not by design. **Conclusion: not worth wiring** an auto-render hook for
  agy; `dashboard`'s agy version correctly makes no SessionStart claim.
- **`PostToolUse` IS wired and confirmed live (2026-08-13), but its real
  discovery path and payload both diverge from `hooks.md`.** Both findings
  came from empirical probing (a diagnostic hook dumping raw stdin), not
  from the docs alone, after several doc-plausible locations silently did
  nothing:
  - **Discovery path**: a standalone `hooks.json` is only picked up from
    `~/.gemini/config/hooks.json` — a *different* root than skills
    (`~/.gemini/antigravity-cli/skills/`, §1 above). Tried and confirmed
    **not** discovered: `~/.gemini/antigravity-cli/hooks.json` (silent
    no-op), a full `plugins/<name>/{plugin.json,hooks.json}` bundle under
    that same `antigravity-cli/plugins/` root (`agy plugin validate`
    accepts the shape and reports `hooks: 1 processed`, but `agy plugin
    enable <name>` then fails with `"plugin ... not found or invalid"` —
    validate accepts any path you hand it, it doesn't confirm the path is
    an actual scanned root), and a project-relative `.agents/hooks.json`
    at the repo root (this repo doesn't use `agy --project`/`--new-project`
    registration, which project-relative discovery may require and which
    wasn't tested further once the global path worked).
  - **Payload is richer than `hooks.md`'s documented example.** The doc's
    `PostToolUse` input example shows only `{stepIdx, error, ...common
    fields}` — no tool-call information at all. The real payload includes
    a full `toolCall: {name, args}`, same shape as the doc's own
    `PreToolUse` example. Confirmed tool names and their file-path arg key
    (both `TargetFile`, conveniently): `write_to_file` (new file) and
    `replace_file_content` (existing file edit) — probed by capturing raw
    stdin from real `agy --dangerously-skip-permissions -p "..."` edit and
    create calls. Extraction: `.toolCall.args.TargetFile`.
  - **Output contract confirmed as documented**: an empty `{}` on stdout.
    A hook that exits without emitting valid JSON was not tested for
    failure behavior — always emit `{}` regardless of which branch a
    handler script takes, don't rely on early `exit 0`.
  - Wired at `agy/hooks.json` → `~/.gemini/config/hooks.json`:
    - `PostToolUse`: runs `ruff format` + `ruff check --fix` on any `.py` file
      `write_to_file`/`replace_file_content` touches, inside a uv/ruff project
      (walks up for `pyproject.toml`) — ported from the same Claude Code/Copilot
      PostToolUse mechanism. Verified end-to-end: introduced a real
      formatting violation, ran a live `agy -p` edit, confirmed the hook
      auto-fixed it before the next tool call saw the file.
    - `Stop`: runs `notify-on-stop` invoking `~/.claude/scripts/notify.py --harness AGY`
      to dispatch desktop toast notifications when agy stops.
- **MCP config format differs**: `~/.gemini/config/mcp_config.json` with a
  `serverUrl` key, replacing inline `~/.gemini/settings.json` declarations
  from the Gemini CLI era. Not touched by this port; this repo doesn't
  manage MCP server config for any harness today. (Unchanged from prior.)
- **No structured multi-choice prompt widget.** Nothing in `--help`
  (`--json-schema` constrains *print-mode output shape*, not interactive
  user prompts), `agy help`, the agent/plugin subcommands, or the
  customization docs exposes an AskUserQuestion-style multi-choice surface.
  **Confirmed: agy is in the plain-text-question tier along with Copilot
  CLI** (not the Claude Code tier), and not opencode either — opencode has
  its own structured `question` tool (confirmed in its own command/skill
  files), so it's not a peer here despite earlier drafts of this doc lumping
  it in. The ported skills' existing treatment (plain-text question,
  recommendation first) is correct and needs no change.

## 4. Install state on this machine (versioning note)

Locally installed `agy` is version 1.1.12 (`agy --version`, 2026-08-13).
`~/.antigravity/` does **not** exist on this machine — confirms the install
is still on the legacy `~/.gemini/` layout, not a fully-migrated
`~/.antigravity/` one that some third-party sources describe for newer
installs. `agy inspect`, which a couple of blog posts (not official docs)
claimed as a diagnostic command, does **not** exist in this build's
`--help` output — don't trust secondhand descriptions of agy's CLI surface
without confirming against the actual installed binary's
`--help`/`--version`. Re-verify all of the above if/when this machine's
`agy` migrates to the `~/.antigravity/` layout.

## Pre-tool guard (`PreToolUse`)

`agy/hooks.json` gains a `worktree-guard` hook set calling
`claude/scripts/guard_rails.py --harness agy`, matching
`write_to_file|replace_file_content`.

**Verified live 2026-09-01**: the write was denied and agy surfaced the
reason to the model verbatim — it quoted the full text back, including the
suggested `git worktree add` command.

Contract as bundled in agy's own `hooks.md` and confirmed in the probe:
stdin carries `{"toolCall": {"name", "args"}, "stepIdx"}`, and the hook
answers on stdout with `{"decision": "allow"|"deny"|"ask"|"force_ask",
"reason": "..."}`. Unlike Copilot, agy passes `reason` straight through to
the model, so the guard's remediation advice actually lands.
