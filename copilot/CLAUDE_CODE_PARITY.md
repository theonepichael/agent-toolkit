# GitHub Copilot CLI → Claude Code parity notes

Goal: make the GitHub Copilot CLI harness feel like Claude Code for this
workflow — same shared instructions, same skills, same
backlog/pending-items/git conventions, plus the one Copilot-only affordance
Claude Code also has but agy/opencode lack: a real `SessionStart` hook.
Compiled 2026-07-28 from the official Copilot CLI docs at
`docs.github.com/copilot/how-tos/copilot-cli/...` (specifically the *Adding
agent skills*, *Using hooks*, *Adding custom instructions*, and *Invoking
custom agents* pages), cross-checked against the installed CLI's own
`--help`/`help commands`/`help config`/`skill --help`/`skill list` and a
live end-to-end invocation probe.

---

## 1. Confirmed facts (docs + installed CLI cross-checked)

All claims in this section are corroborated by both the official docs and
the locally-installed CLI surface, except where noted.

- **Global rules file (CLAUDE.md-equivalent): `~/.copilot/copilot-instructions.md`.**
  Official docs (*Adding custom instructions*): "`$HOME/.copilot/copilot-instructions.md`
  — User-level instructions that apply across repositories." `links.toml`
  symlinks `claude/global-instructions.md` straight to this path. Always
  active, no frontmatter.
  - The same docs page also auto-discovers `AGENTS.md`, `CLAUDE.md`, and
    `GEMINI.md` "in the standard locations" (repo root → cwd walk), so
    workspace-level rule files in any of those three names work natively —
    no port needed. There is additionally `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`
    (env var) for extra load roots, and `/instructions` (interactive slash
    command) to view/toggle discovered files live. Neither is wired by this
    repo (mirrors the repo's stance on per-project `CLAUDE.md` everywhere
    else — personal global tooling, not per-project).
  - The custom-instructions docs also describe path-specific modular
    files (`~/.copilot/instructions/**/*.instructions.md` and
    `.github/instructions/**/*.instructions.md`) with an `applyTo` glob in
    frontmatter. Not used by this repo today; noting for awareness.

- **Personal skills path: `~/.copilot/skills/<name>/SKILL.md`.** Official
  docs (*Adding agent skills*): "For **personal skills**, shared across
  projects, create a `~/.copilot/skills` or `~/.agents/skills` directory in
  your local home directory." Each skill lives in its own subdirectory
  (lowercase, hyphenated), and the skill file **must** be named `SKILL.md`.
  The `copilot/skills/*` entries in `links.toml` symlink each of the 7
  `SKILL.md` files into this path; `install.py`'s symlink handling does
  `mkdir -p "$(dirname "$dst")"` and so creates the per-skill parent dir
  automatically — no separate directory-wiring step, no gap in the install
  flow. `COPILOT_HOME` overrides
  `~/.copilot` for both instructions and hooks (not set on this machine).

- **Skill frontmatter**: `name` + `description` (both required), plus an
  optional `license` field. Permissions are governed separately by an
  **`allowed-tools`** field (e.g. `allowed-tools: shell`) that pre-approves
  tools the skill can use without per-call confirmation. Per the docs'
  security warning, `shell`/`bash` should only be pre-approved for fully
  trusted skills — this repo's skills use `allowed-tools: shell`, which is
  appropriate because all 7 are self-authored and reviewed. This is one
  field richer than agy (which has no `allowed-tools` equivalent) and
  matches Claude Code's per-skill tool-grant model in spirit, though
  Copilot's value vocabulary is coarser (a tool category like `shell`, not
  a granular list).

- **Skill invocation model: BOTH model-decision-activated AND user-typed.**
  Per the *Adding agent skills* page ("Using agent skills"): "When
  performing tasks, Copilot will decide when to use your skills based on
  your prompt and the skill's description." **And** separately: "To tell
  Copilot to use a specific skill, include the skill name in your prompt,
  preceded by a forward slash. For example, if you have a skill named
  'frontend-design' you could use a prompt such as: `Use the /frontend-design
  skill to ...`." So `/dashboard` (or `/grill-me`, `/standup`, etc.) **is**
  a valid user-typed invocation gesture — same muscle memory as Claude
  Code's slash commands. See §3 for the prior misreading and the
  reconciliation.

- **SessionStart hook: supported and live.** Official docs (*Using hooks*,
  "Creating a user-level hook"): hook files live at `~/.copilot/hooks/`
  (or `$COPILOT_HOME/hooks/`) as `NAME.json` with `{version:1, hooks:{
  <event>: [...]}}` schema; supported events are `sessionStart`,
  `sessionEnd`, `userPromptSubmitted`, `preToolUse`, `postToolUse`,
  `errorOccurred`, `agentStop`. Each handler has `type: "command"`, a
  shell (`bash` for Linux/macOS, optional `powershell` for Windows), a
  `cwd`, optional `env`, and a `timeoutSec` (default 30). `copilot help
  config` corroborates (`hooks` keyed by event name, same schema as
  `.github/hooks/*.json`; `disableAllHooks` global toggle).
   This repo wires hooks under `copilot/hooks/` (symlinked to
   `~/.copilot/hooks/` via `links.toml` gated `platform = "mac"` / `"linux"`):
   - `session-start.json` running `dev_status.py render` + `dotfiles_sync_check.py`
     on session start.
   - `post-tool-use.json` for tool tracking.
   - `agent-stop.json` on `agentStop` running `~/.claude/scripts/notify.py --harness Copilot`
     for desktop toast notifications with GitHub Copilot branding.
   Note that `dashboard`'s Copilot version correctly flags this in its
   description, so the SKILL is not re-run unprompted by the user's "status"
   phrasing once the hook has surfaced the backlog.

- **Non-interactive print mode**: `copilot -p "<prompt>"` (alias
  `--prompt`) with `--allow-all-tools` (required for non-interactive mode)
  / `--allow-all`/`--yolo` for permission scope. Confirmed via
  `copilot --help`. Used for the live invocation probe in §3.

- **Custom agents/subagents location: `~/.copilot/agents/` (personal) or
  `.github/agents/`, `.github-private/agents/` (repo/org/enterprise).**
  Per *Invoking custom agents*. Built-in custom agents are `Explore`,
  `Task`, `General-purpose`, `Code-review` (names that map roughly to
  opencode's `explore`/`general` built-ins and Claude Code's same-named
  tier). Invocable via `/agent`, via natural-language mention ("Use the
  refactoring agent to..."), or via `copilot --agent=<name> --prompt
  "..."`. Not directly relevant to this item (this repo doesn't ship
  custom agents) but documented for cross-harness parity awareness.

- **MCP defaults: `~/.copilot/mcp-config.json`** (or
  `$COPILOT_HOME/mcp-config.json`). Copilot CLI ships with the GitHub MCP
  server pre-configured (enabling PR/issue ops from the CLI). Not touched
  by this repo; this repo doesn't manage MCP server config for any
  harness today.

---

## 2. Skills — ported (`copilot/skills/`)

All 7 (`backlog-item`, `dashboard`, `grill-me`, `make-skill`,
`second-opinion`, `spec`, `standup`) live in the repo at
`copilot/skills/<name>/SKILL.md` and are symlinked into
`~/.copilot/skills/<name>/SKILL.md` by the `copilot/skills/*` entries in
`links.toml`. `backlog-item` orchestrates `grill-me`/`second-opinion`/`spec`
via mid-skill delegation (see the 2026-08-03 finding in section 3 below,
verified the same day `backlog-item` was added — `spec` was added later,
2026-08-12, and follows the same delegation shape). The other 5 (excluding
`spec`) were verified 2026-07-28. Re-checked 2026-08-13 on CLI 1.0.79:
`copilot skill list` shows all 7 under "Personal skills", a
`copilot -p "/dashboard" --allow-all-tools` slash probe rendered the
dashboard, and the sessionStart hook was confirmed dispatching (a marker
hook in the same `~/.copilot/hooks/` directory with the same schema fired,
and the real hook's `dev_status.py render` output appears in the session
log at startup). Earlier end-to-end probe
(`copilot -p "<activate dashboard; run dev_status.py render; show stdout
verbatim>" --allow-all-tools`) produced a visible `skill(dashboard)`
tool-call step in the run log followed by the dashboard verbatim.

The `make-skill` skill's own step-3 documents the scoped manual fix for
single-skill drift on a stale machine (the `ln -s <src> <dst>` one-liner),
so when a machine's live state lags the repo (e.g. a skill added after that
machine's last `install.sh` run), the procedure for catching it up without
re-running the full personal-profile pipeline is already self-documenting.

---

## 3. Verification results (probed 2026-07-28)

Resolved against the official Copilot CLI docs (linked inline below) and a
**live end-to-end invocation probe** (`copilot -p` → activate dashboard),
which produced a `skill(dashboard)` tool call followed by the rendered
backlog verbatim.

- **User-typed skill invocation EXISTS — `/dashboard` is a valid gesture.**
  Per *Adding agent skills* ("Using agent skills" section), Copilot
  supports BOTH model-decision activation ("Copilot will decide when to use
  your skills based on your prompt and the skill's description") AND an
  explicit user-typed **"skill name in your prompt, preceded by a forward
  slash"** — i.e. `/dashboard`, `/grill-me`, `/standup`, etc. work as
  invocation gestures, same as Claude Code's slash commands.
  This corrects an earlier draft of this doc which conflated the
  management-only `/skills` (plural) command with skill invocation; `/skills`
  is purely the management surface (`list`, `add`, `reload`, `info`,
  `remove`), while `/<skill-name>` in a prompt is the actual invocation.
  Crossed via the docs and corroborated by the live probe below (the prompt
  used a skill-name reference and Copilot invoked `skill(dashboard)` as a
  tool call). **Copilot CLI is therefore the closest peer in invocation UX
  to Claude Code among all the harnesses on this machine**: same `/x`
  gesture, same model-decision fallback when not slash-invoked. agy (no
  skill subcommand, no slash-invocation) and opencode (commands but not
  slash-invoked skills in the same way) are weaker here.
  Management surface in detail (`copilot skill --help` + docs:
  `/skills`/`/skills <sub>` interactive): `/skills list` (also `copilot
  skill list` non-interactively), `/skills reload` (after edits), `/skills
  info <name>` (incl. location), `/skills add <dir|url|file>` (also
  `copilot skill add`), `/skills remove <dir>` (also `copilot skill remove`
  for direct-added, not plugin-bundled, skills); `/skills` (bare) opens the
  interactive enable/disable toggle.

- **SessionStart hook: confirmed, and live.** Per *Using hooks*
  ("Creating a user-level hook") and corroborated by `copilot help config`:
  hook files at `~/.copilot/hooks/*.json`, schema `{"version":1,
  "hooks":{"<event>":[...]}}`, supported events `sessionStart` /
  `sessionEnd` / `userPromptSubmitted` / `preToolUse` / `postToolUse` /
   `errorOccurred` / `agentStop`. `copilot/hooks/session-start.json` is
   symlinked into `~/.copilot/hooks/session-start.json` (via `links.toml`,
   macOS/Linux only)
   and runs `dev_status.py render` + `dotfiles_sync_check.py` automatically
  at session open — the auto-render dashboard affordance agy and opencode
  can't replicate (agy's `hooks.md` lists only `PreToolUse`/`PostToolUse`/
  `PreInvocation`/`PostInvocation`/`Stop` — no `SessionStart`; opencode
  would need a TypeScript plugin for the same effect). `dashboard`'s
  Copilot version correctly notes the SessionStart hook covers auto-render,
  so the skill should not be re-run unprompted — Claude-Code-equivalent
  behavior preserved. Other Copilot hook events not wired here
  (`userPromptSubmitted`, `preToolUse`, etc.) are available if future
  items need them (e.g. permission gating, prompt logging); not used today.

- **`postToolUse` wired and confirmed live (2026-08-13); `toolArgs` is a
  JSON-encoded string, not a nested object as the official schema page
  implies.** `copilot/hooks/post-tool-use.json` ports the Claude
  Code/agy ruff-format-on-edit hook: matcher `create|edit`, runs `ruff
  format` + `ruff check --fix` on any `.py` file touched, inside a
  uv/ruff project. The docs page for this hook (*Post-tool use hook*,
  `copilot-sdk/use-hooks/`) shows `toolArgs` as a plain object in its
  example (`"toolArgs": {"path": "..."}`). A live probe (a diagnostic
  hook dumping raw stdin, then a real `copilot -p` edit and a real
  `copilot -p` file-create) captured the actual payload:
  `"toolArgs":"{\"path\":\"...\",\"old_str\":\"...\",\"new_str\":\"...\"}"`
  — `toolArgs` is a *string* holding JSON, so extraction needs a double
  parse: `.toolArgs | fromjson | .path`, not `.toolArgs.path`. Confirmed
  for both `edit` (`old_str`/`new_str` keys) and `create` (`file_text`
  key) — both use the same `path` key for the target file. Verified
  end-to-end: introduced a real formatting violation, ran a live
  `copilot -p` edit, confirmed the hook auto-fixed it before the next
  tool call saw the file.

- **No structured multi-choice prompt widget.** Nothing in
  `copilot --help`, the full slash-command survey (`copilot help commands`),
  `copilot help config`, the `skill` subcommand family, or any of the
  official customization docs (*Adding agent skills*, *Using hooks*,
  *Adding custom instructions*, *Invoking custom agents*) exposes an
  AskUserQuestion-style structured multi-choice prompt surface. **Confirmed:
  Copilot CLI is in the plain-text-question tier along with agy** — not
  opencode, which has its own structured `question` tool (confirmed in its
  own command/skill files) despite earlier drafts of this doc lumping it
  in — and not the Claude Code tier. The ported skills' existing treatment
  (plain-text question, recommendation first per CLAUDE.md's "Judgment
  calls — lead with a recommendation") is correct and needs no change.

  **Re-checked 2026-08-19 against a new CLI feature: still holds, with one
  untested caveat.** Copilot CLI shipped an `ask_user` tool in its 2026-01-21
  release (*GitHub Copilot CLI: Plan before you build, steer as you go*),
  which shows a numbered list of selectable options in the terminal — a real
  structured-choice surface, not just free text. This could have overturned
  the finding above. It doesn't, for the way skills invoke it:
  - The official *Adding agent skills* doc's `allowed-tools` field only
    documents `shell`/`bash` as tools a skill can request — no `ask_user`.
  - Live-tested via `copilot -p` (installed CLI 1.0.80): a prompt explicitly
    instructing the model to "use whatever structured question/choice tool
    you have available" produced a plain-text question with no tool call
    (`toolRequests: []`) in both a normal run (this repo's own
    `~/.copilot/copilot-instructions.md` loaded) and a `--no-custom-instructions`
    run — the latter's own unprompted words: "No structured choice/selection
    tool is available in this environment."
  - **Caveat, not yet tested:** `ask_user` is documented tied to `--plan`/
    `--mode plan`, a real interactive TUI mode this probe didn't drive (`-p`
    is non-interactive print mode only). If a live interactive `--plan`
    session is ever confirmed to expose `ask_user` to a running skill, this
    entry needs another pass — until then, `-p` mode (what `copilot -p`
    skill invocations from other tooling use) is confirmed to still have no
    structured-choice tool.

- **Mid-skill delegation works (2026-08-03 finding).** A throwaway
  `~/.copilot/skills/zz-probe-delegation/SKILL.md` whose body said "now use
  the dashboard skill" produced `skill(zz-probe-delegation)` →
  `skill(dashboard)` → the real `dev_status.py render` output, when run via
  `copilot -p "run the zz-probe-delegation skill" --allow-all-tools`
  (probe skill deleted after the run). This proves an instruction embedded
  in one skill's own body can trigger a second skill's activation mid-run —
  not just user-prompt-triggered activation, which was the only thing
  previously confirmed above. This is what makes `backlog-item`'s
  delegation to `grill-me`/`second-opinion` (section 2) viable as a ported
  skill rather than requiring the user to invoke each step by hand.

---

## 4. Install state / drift note

Live `~/.copilot/skills/` was found to be missing `dashboard/` entirely
during this verification (the other 4 skills were correctly symlinked at
the `SKILL.md` level). Root cause is drift-by-rename across machines,
**not a gap in install.sh**: the `status → dashboard` rename
(commit `68bf726`) changed the symlink target path in install.sh from
`~/.copilot/skills/status/SKILL.md` to
`~/.copilot/skills/dashboard/SKILL.md`, so a machine last provisioned
before that commit has neither the new dashboard symlink nor its parent
dir. The `symlink()` helper (in the pre-Python install.sh at the time; now
install.py) makes the destination's parent dir as needed,
so a fresh `install.sh --harness=copilot` run
creates the dir + symlink cleanly; no installer fix is warranted. The
scoped manual fix `mkdir -p ~/.copilot/skills/dashboard && ln -s
~/dotfiles/copilot/skills/dashboard/SKILL.md
~/.copilot/skills/dashboard/SKILL.md` was applied to this machine and
verified via `copilot skill list` (dashboard now listed) plus the live
invocation probe in §3. An orphaned `~/.copilot/skills/status/` from the
pre-rename provisioning, if present on a stale machine, is harmless
leftover — delete whenever; install.sh is additive-only and won't clean
it up automatically.

---

## Sources

Official Copilot CLI docs (`docs.github.com/copilot/how-tos/copilot-cli/`):
- *Adding agent skills for GitHub Copilot CLI* — skills schema, invocation model (both auto-decision and `/<skill>`), management surface, `allowed-tools` security note
- *Using hooks with GitHub Copilot CLI* — hook schema, full event list incl. `sessionStart`, user-level hook location
- *Adding custom instructions for GitHub Copilot CLI* — rules-file discovery (incl. `AGENTS.md`/`CLAUDE.md`/`GEMINI.md` walk), `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, path-specific modular files
- *Invoking custom agents* — built-in custom agents (`Explore`/`Task`/`General-purpose`/`Code-review`), custom-agent file locations, three invocation modes (`/agent`, natural-language mention, `--agent`)
- *Post-tool use hook* (`docs.github.com/en/copilot/how-tos/copilot-sdk/use-hooks/post-tool-use`) — `postToolUse` input/output field reference (`timestamp`, `workingDirectory`, `toolName`, `toolArgs`, `toolResult`); example shows `toolArgs` as a plain object, which the live probe below found does not match the real payload shape
- *GitHub Copilot hooks reference* (`docs.github.com/en/copilot/reference/hooks-reference`) — built-in tool name list (`ask_user`/`bash`/`create`/`edit`/`glob`/`grep`/`powershell`/`task`/`view`/`web_fetch`), full `postToolUse` hook JSON schema incl. `matcher` (regex, anchored `^(?:PATTERN)$`)

Installed CLI surface (cross-checked against the above):
- `copilot --help` — print mode flags, `--agent`, `--yolo`/`--allow-all`
- `copilot help commands` — full interactive slash-command survey, including the management-only `/skills` family
- `copilot help config` — `hooks` config schema, `disableAllHooks` toggle
- `copilot skill --help` / `copilot skill list` — non-interactive skill management + live skill discovery
- Live probe: `copilot -p "<activate dashboard; run dev_status.py render; show stdout verbatim>" --allow-all-tools` → produced `skill(dashboard)` tool call + dashboard verbatim
- Live probe (2026-08-13): a diagnostic `postToolUse` hook dumping raw stdin, wired at `~/.copilot/hooks/zz-probe.json`, fired against a real `copilot -p` edit and a real `copilot -p` create → captured payload showed `toolArgs` as a **JSON-encoded string**, not a nested object as the *Post-tool use hook* doc's example implies; correct extraction is `.toolArgs | fromjson | .path`, confirmed for both `edit` (`old_str`/`new_str` keys) and `create` (`file_text` key)

## Pre-tool guard (`preToolUse`) — mechanism established by probe

GitHub's docs say `preToolUse` "can approve or deny tool executions" but
never document *how*. Probed live 2026-09-01, three runs:

1. JSON `{"decision":"deny"}` on stdout **with exit 2** -> blocked.
2. The same JSON on stdout **with exit 0** -> **not blocked**; the write went
   through.
3. A reason on **stderr** with exit 2 -> blocked, but the reason was not
   shown.

**Copilot honours the exit code only.** `copilot/hooks/pre-tool-use.json`
therefore relies on `guard_rails.py --harness copilot` exiting 2, and the
JSON verdict it also prints is inert today, kept only in case a later
release starts reading it.

**The block carries no reason.** Whatever the hook writes to stdout or
stderr, the model sees exactly `Denied by preToolUse hook: hook exited with
code 2` — confirmed in run 3. So on Copilot the guard stops the write but
cannot say why, and cannot suggest the worktree remedy. Every other harness
surfaces the reason in full.
