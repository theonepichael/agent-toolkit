# opencode → Claude Code parity notes

Goal: make opencode (running GLM-5.2) feel like Claude Code — same muscle
memory for keybinds, same instruction-file behavior, same slash commands,
same session-resume workflow. Compiled 2026-07-24 from opencode's official
docs (opencode.ai/docs/*).

This is a planning/reference doc, not applied config yet. Once decisions
below are made, the actual config files should live alongside this one
(`opencode/agent/`, `opencode/command/`, `opencode/plugin/`, `opencode/tui.json`,
`opencode/opencode.json`) mirroring the existing `claude/` and `copilot/`
directories in this repo, and get symlinked into `~/.config/opencode/` by
`install.sh` the same way `claude/settings.json` etc. are wired up.

---

## 1. Already matches — no work needed

| Claude Code behavior | opencode equivalent | Notes |
|---|---|---|
| `CLAUDE.md` project/global instructions | **Project:** `AGENTS.md` only, from cwd up to the project root — a project `CLAUDE.md` is never loaded. **Global:** reads `~/.claude/CLAUDE.md` automatically. | Our existing global `~/.claude/CLAUDE.md` is already picked up — nothing to port. See the note below on a wrong correction made and reverted. |
| `Esc` interrupts current turn | `session_interrupt: escape` (default) | Exact match |
| `Shift+Tab` cycles Default→AcceptEdits→Plan→Auto | `Tab` / `Shift+Tab` → `agent_cycle` / `agent_cycle_reverse`, cycles Build ↔ Plan | Same gesture, but only 2 states by default vs Claude Code's 4 — see §3 |
| `Ctrl+G` opens external editor | `editor_open: <leader>e` (default) | Same feature, different key — rebind if wanted, see §4 |
| `Ctrl+C` / `Ctrl+D` exit | `app_exit: ctrl+c,ctrl+d,<leader>q` (default) | Exact match |
| `Ctrl+V` paste (incl. images) | `input_paste: ctrl+v` (default) | Works with opencode-vision (installed separately as a plugin) |
| `claude --continue` / `--resume` | `opencode run --continue` / `-c`, `--session`/`-s <id>` | Same concept, different flag names |
| Subagent naming (Explore, general-purpose) | Built-in subagents: `general`, `explore`, `scout` | opencode's `explore` is close to Claude Code's `Explore` agent already |

### Correction, 2026-08-30 — itself wrong, reverted 2026-08-31

On 2026-08-30 this section was rewritten to claim opencode had dropped its
`~/.claude/CLAUDE.md` fallback and was therefore loading no instructions at
all, and a `links.toml` entry was added pointing `claude/CLAUDE.md` at
`~/.config/opencode/AGENTS.md`. **That correction was wrong and has been
reverted.**

The evidence behind it was a fixture repo holding an `AGENTS.md` and a
`CLAUDE.md` at both a root and a subdirectory, each with a distinct token.
Only the `AGENTS.md` tokens came back. That is a real result — but it tests
**project-level discovery only**, because every file in the fixture sat
inside the project. It says nothing about the global fallback, which reads a
path in the home directory the fixture never touched. The conclusion "no
`CLAUDE.md` fallback of any kind" did not follow from it.

Bisected properly on 2026-08-31, from a cwd (`/tmp`) with no `AGENTS.md`
anywhere up the tree:

- shadow `HOME` with `.config`, `.local`, `.cache`, `.opencode` symlinked
  but **no** `.claude` → opencode could not see the global instructions.
- same shadow `HOME` with `.claude` symlinked back in → it could.

The live `~/.config/opencode/opencode.jsonc` has no `instructions` key and
`opencode debug config` reports `instructions: null`, ruling out
config-driven injection as the source.

So both halves are true at once, and neither implies the other:
**project-level discovery is `AGENTS.md`-only; global instructions still
come from `~/.claude/CLAUDE.md`.** The added link was redundant — opencode
already had that content — so it has been removed and this section restored.

---

## 2. Slash commands — DONE (6 repo-tracked as of 2026-08-03)

**Status (2026-08-03): all 6 commands are tracked in the repo.** All 6
(`dashboard`, `grill-me`, `make-skill`, `second-opinion`, `standup`,
`backlog-item`) live at `opencode/command/<name>.md` and are symlinked into
`~/.config/opencode/commands/<name>.md` by install.sh — so `git pull` +
`install.sh --harness=opencode` reproduces the full command set on a fresh
machine. This closes the drift class the earlier version of this section
flagged: as of 2026-07-24 only `dashboard.md` and `grill-me.md` were in the
repo, with the other 3 (`make-skill`, `second-opinion`, `standup`) sitting as
untracked manual copies in `~/.config/opencode/commands/` that a fresh
`install.sh` run would silently drop. The 3 missing files have now been
pulled into the repo and wired into install.sh alongside the existing two.

**Update (2026-08-12): 7th command added.** `spec.md` was added at
`opencode/command/spec.md` (plus a delegated `opencode/skills/spec/SKILL.md`
copy, same split as `grill-me`/`second-opinion` below) and symlinked via
`links.toml` — the actual current linking mechanism; "install.sh" above is
this doc's original, now-imprecise, shorthand for it.
`backlog-item` was ported from Claude Code on 2026-08-03; because it
delegates planning/critique to `grill-me`/`second-opinion` at runtime, the
port also introduced this repo's first two opencode **skills** (see the
Commands-vs-skills subsection below).

### Format (informed by the official Commands doc, `opencode.ai/docs/commands/`)

opencode's custom-command format is nearly identical to Claude Code's, with
the schema differences below. The live `.md` files in
`~/.config/opencode/commands/` are the canonical source for this repo's
port — every frontmatter decision was originally a deliberate adaptation
pass from `claude/commands/`, not a blind copy.

- **Location**: `~/.config/opencode/commands/` (global) or
  `.opencode/commands/` (project) — vs Claude Code's `~/.claude/commands/`
  / `.claude/commands/`. (`COPILOT_HOME` / `OPENCODE_HOME` overrides the
  global root, but is not set on this machine.)
- **Filename = command name**: `test.md` → `/test`. There is **no `name`
  frontmatter field** in opencode commands (the filename plays that role),
  which is why the live files dropped Claude Code's `name:` field.
- **Frontmatter schema** (per the official doc's "Options" section):
  - `description` (optional) — shown in the TUI when typing the command.
    Not strictly required by the specifier, but every repo command carries
    one since model-decision and user-discovery both depend on it.
  - `agent` (optional) — override which agent executes the command. None of
    the 5 repo commands use this; they all inherit the current agent.
  - `model` (optional) — override the model. Unused here.
  - `subtask` (optional bool) — force subagent invocation. Unused here.
  - `template` is marked "required" in the doc — but that's for the
    `opencode.json` JSON form, where the template is a string field. In the
    markdown-file form, **the file body is the template**; no `template:`
    frontmatter key is needed.
  - **No `argument-hint`, no `allowed-tools`** — neither exists in
    opencode's command frontmatter. Tool-access control lives in the agent's
    `permission` block instead (see §3). Both fields were dropped from the
    Claude Code originals during the manual adaptation pass.
- **Placeholders in the template body** (per the official "Prompt config"
  section, identical to Claude Code):
  - `$ARGUMENTS` — all args as one string. Also `$1`, `$2`, … for positional
    access (`/create-file config.json src "..."` → `$1`=config.json etc.).
  - `` !`shell cmd` `` — bash output injection; the command runs in the
    project root and its stdout becomes part of the prompt. `dashboard.md`
    and `standup.md` don't use this (they invoke scripts via the agent
    shell tool, not as inline command output) — `standup.md` does describe
    shell-invocation patterns in its body, which the agent runs, not opencode's
    parser.
  - `@filepath` — file content inclusion. Unused by the 5 repo commands.
- **Built-in command override**: a custom command with the same name as a
  built-in (`/init`, `/undo`, `/redo`, `/share`, `/help`) **overrides** it.
  None of the 5 repo commands collide with built-ins.
- **Custom commands can also live inline in `opencode.json`** under a
  `"command"` key. This repo uses the markdown-file form exclusively —
  keeping each command in its own file is easier to author, lint, and review
  than a single growing JSON object.

### Commands vs skills (different layers — both supported, different use)

opencode exposes two distinct extension surfaces that the official docs keep
separate (see `opencode.ai/docs/commands/` vs `opencode.ai/docs/skills/`):

- **Commands** (used by this repo): user-typed `/x` invocation, file body IS
  the prompt template. The 7 commands in this repo are deliberately
  user-typed — `/dashboard`, `/grill-me`, `/standup`, `/second-opinion`,
  `/make-skill`, `/backlog-item`, `/spec` — because the user explicitly
  initiates each one in a session the same way they would in Claude Code.
- **Skills** (used by this repo as of 2026-08-03, for `backlog-item`'s
  delegation): model-invoked via the **native `skill` tool** — agents see
  the available-skills list (name + description in a `<available_skills>`
  block) and load a skill with `skill({ name: "..." })` when relevant.
  opencode's `skill` tool only loads `SKILL.md` files — commands are
  invisible to it — so `backlog-item`'s runtime delegation to
  grill-me/second-opinion/spec (which Claude Code does through its own Skill
  tool) needs real skills here. This repo tracks three,
  `opencode/skills/grill-me/SKILL.md`,
  `opencode/skills/second-opinion/SKILL.md`, and
  `opencode/skills/spec/SKILL.md` (added 2026-08-12, once `backlog-item`
  started delegating to `spec` too), symlinked into
  `~/.config/opencode/skills/<name>/SKILL.md` via `links.toml`. They are
  model-invoked copies of the same protocols the user-typed `/grill-me`,
  `/second-opinion`, and `/spec` commands carry — deliberate full
  duplication, one copy per layer, each adapted to how its layer is
  invoked. Skills are
  auto-discovered from `~/.config/opencode/skills/<name>/SKILL.md`,
  `.opencode/skills/<name>/`, and **also from the Claude-Code-compat
  paths** `~/.claude/skills/<name>/` and `~/.agents/skills/<name>/` (per
  `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` env-toggle in §1). Frontmatter:
  `name` (required, lowercase-with-hyphens, 1-64 chars, must match the
  directory name), `description` (required, 1-1024 chars), plus optional
  `license` / `compatibility` / `metadata` (string→string map); unknown
  frontmatter fields are ignored. `permission.skill` (with `*` wildcard
  patterns) gates access per-agent. The `make-skill` command in this repo
  documents the skill-authoring rubric for opencode; **note that
  `make-skill`'s step-5 claim "no symlinks needed — opencode auto-discovers
  skills in configured paths" is accurate as far as discovery goes** (a
  `SKILL.md` dropped into `~/.config/opencode/skills/<name>/` is
  immediately discoverable, no enabling config needed), but a skill just
  dropped there won't reproduce across machines without repo+symlink
  wiring analogous to commands — the same drift class this section was
  written to close, and the reason the two `backlog-item` skills are
  repo-tracked + wired rather than dropped in place.

### History of the staged pull-into-repo (kept for reference)

- **2026-07-23**: original manual port — all 5 commands placed directly at
  `~/.config/opencode/commands/` as plain (non-symlinked) `.md` files,
  adapted from the Claude Code originals.
- **2026-07-24**: Claude Code's command was renamed `status.md` →
  `dashboard.md` (collided with Claude Code's built-in `/status`). Rather
  than require a manual fix on whatever machine has opencode configured,
  `dashboard.md` was pulled into the repo properly at
  `opencode/command/dashboard.md` and wired into install.sh. The stale,
  un-symlinked `~/.config/opencode/commands/status.md` from the old manual
  port is harmless leftover clutter on any pre-rename machine — delete it
  whenever, or leave it.
- **2026-07-25**: `grill-me.md` similarly pulled into the repo at
  `opencode/command/grill-me.md` + wired into install.sh.
- **2026-07-28**: the remaining 3 — `make-skill.md`,
  `second-opinion.md`, `standup.md` — pulled into the repo at
  `opencode/command/<name>.md` and wired into install.sh. All 5 commands
  are now repo-tracked.
- **2026-08-03 (this commit)**: `backlog-item.md` ported from Claude Code
  at `opencode/command/backlog-item.md` and wired into install.sh (6th
  command). Its runtime delegation to grill-me/second-opinion goes through
  opencode's native `skill` tool, so this also adds the repo's first two
  opencode skills — `opencode/skills/{grill-me,second-opinion}/SKILL.md` —
  likewise wired into install.sh. All 6 commands are now repo-tracked.

---

## 3. Extra permission-mode states (Claude Code's 4-way cycle) — DECIDED: skip

**Decision (2026-07-24): not doing this.** Build/Plan's 2-state Tab cycle is
fine as-is — no need to replicate Claude Code's 4-mode Default/AcceptEdits/
Plan/Auto cycle. Section kept for reference in case this gets revisited.

Claude Code's Shift+Tab cycles four named modes: **Default → AcceptEdits →
Plan → Auto**. opencode ships only Build (full access) and Plan (edit/bash →
`ask`) as primary agents.

**Plan**: define additional primary agents in `opencode.json` (or as markdown
files in `~/.config/opencode/agents/`) with `mode: primary` and different
`permission` blocks:

```json
{
  "agent": {
    "auto": {
      "mode": "primary",
      "description": "Auto-accept edits, still ask before bash",
      "permission": { "edit": "allow", "bash": "ask" }
    },
    "yolo": {
      "mode": "primary",
      "description": "Bypass permissions entirely",
      "permission": { "*": "allow" }
    }
  }
}
```

`agent_cycle` (Tab) should then cycle through all `mode: primary` agents —
**needs verification once configured**: does Tab cycle built-ins + custom in
a stable, predictable order? Not confirmed from docs alone.

Permission value reference: `"allow"` / `"ask"` / `"deny"`, wildcard `*` for
global default, per-tool override (`read`, `edit`, `bash`, `grep`, `glob`,
`webfetch`, `websearch`, `external_directory`, `task`, `skill`). Bash also
supports pattern rules: `{"bash": {"*": "ask", "git *": "allow", "rm *": "deny"}}`.

CLI-level equivalent of "bypass everything for this run": `opencode run --auto`
(auto-approves anything not explicitly `deny`d — different semantics from
setting everything to `allow`, since explicit `deny` rules still apply).

---

## 4. Keybind conflicts to resolve

Full default keybind list pulled from `opencode.ai/docs/keybinds`. Verified
against Claude Code's actual shortcuts (corrected from an earlier draft of
this doc, which had `Ctrl+R`/`Ctrl+O` swapped):

- **Claude Code `Ctrl+O`** = transcript/log viewer (verbose tool output,
  thinking tokens). **Clean 1:1 in opencode** — `session_toggle_generic_tool_output`
  is unbound by default. **Done (2026-07-24)**: bound to `ctrl+o` in
  `opencode/tui.json`, symlinked into `~/.config/opencode/tui.json` via
  `install.sh`:
  ```json
  { "$schema": "https://opencode.ai/tui.json", "keybinds": { "session_toggle_generic_tool_output": "ctrl+o" } }
  ```

- **Claude Code `Ctrl+R`** = fuzzy search through past prompt history
  (`Ctrl+S` cycles scope: session/project/all). opencode's `Ctrl+R` is
  `session_rename` by default — genuinely different feature, and **no clean
  opencode equivalent for history *search* exists** (`history_previous`/
  `history_next` on Up/Down only step through recent entries, no fuzzy
  search; confirmed open feature request `anomalyco/opencode#5062`, not
  implemented in core).

  **Decision (2026-07-24): gap accepted, not pursuing.** Considered
  `opencode-history-search` (npm plugin) as a substitute, but it's an
  agent-invoked tool ("search my history for X" in chat), not an inline
  `Ctrl+R`-style popup — different enough interaction model that it's not
  worth installing just to approximate the muscle memory. Revisit if
  opencode ships native history search later.

- **Claude Code `Ctrl+G`** = open external editor. Already matches
  functionally: opencode's `editor_open` is bound to `<leader>e`. Same
  feature, different key — rebind `editor_open` to `ctrl+g` if the muscle
  memory matters more than the leader-key consistency.

- **Leader key** — **decided (2026-07-24): keep default `ctrl+x`, no
  remap needed.** No collision with tmux prefix (`C-a`, confirmed via
  `~/dotfiles/tmux/`). User wants the leader shortcuts as-is — nothing to
  configure, they already work out of the box. Reference list of what it
  gates (new
  session `<leader>n`, model list `<leader>m`, agent list `<leader>a`,
  sidebar toggle `<leader>b`, session export `<leader>x`, compact
  `<leader>c`, undo/redo `<leader>u`/`<leader>r`, etc.) — Claude Code has no
  leader-key paradigm at all, so this is pure upside: opencode-only
  shortcuts on top of everything else.

Config file: `tui.json` (global: `~/.config/opencode/tui.json`, project:
`.opencode/tui.json` — mirrors `opencode.json`'s global/project split).
Syntax: `"action": "key"`, comma-separated for multiple bindings, array
form also accepted, `"action": "none"` or `false` to disable a bind.

Other keybinds worth knowing about (not necessarily changes, just
awareness — full list in this doc's source research, available on request):
- `messages_undo` / `messages_redo`: `<leader>u` / `<leader>r` — opencode's
  loose analog to Claude Code's double-Esc rewind-to-edit-previous-message,
  though the trigger gesture differs
- `model_cycle_recent`: `f2` — quick model switch
- `session_child_cycle` / `session_child_first`: navigate subagent/session
  tree — no direct Claude Code equivalent (Claude Code doesn't expose this)

---

## 5. Hooks → plugin system (bigger lift, separate decision)

Claude Code hooks (`settings.json` — `SessionStart`, `PreToolUse`,
`PostToolUse`, etc., declarative JSON shelling out to scripts) have **no
declarative equivalent** in opencode. The analog is a TypeScript **plugin**
system:

- **Location**: `~/.config/opencode/plugin/*.ts` (global) or
  `.opencode/plugin/*.ts` (project) — or an npm package referenced in config
- **Shape**: an async function receiving `PluginInput` (SDK client, project
  info, project dir, git worktree root, server URL, a Bun shell for running
  commands), returning a hooks object
- **25+ lifecycle events** documented, including `chat.message`,
  `chat.params`, `permission.ask`, `tool.execute.before` /
  `tool.execute.after` — the opencode-vision plugin we already installed
  uses `experimental.chat.messages.transform` as one example of this system
- Two config surfaces exist: hand-written `.ts` files, or npm packages
  declared in `opencode.json`

**Not scoped in this doc**: whether to actually port the SessionStart
backlog-dashboard hook (`dev_status.py` dashboard render) into an opencode
plugin. That's a real engineering task (TypeScript, not JSON config) and
should be its own decision/backlog item once the keybind/command/agent
parity work above is done and evaluated.

**`tool.execute.after`: ported (2026-08-13) and confirmed live, with a
richer real payload than the SDK's own type comments suggest.** Ported the
Claude Code/Copilot/agy ruff-format-on-edit hook as
`opencode/plugin/ruff-format-on-edit.ts` → `~/.config/opencode/plugin/`
(global, singular `plugin/` — confirmed correct against a stray web source
that claimed plural `plugins/`; `@opencode-ai/plugin`'s own installed
`index.d.ts` was pulled from the npm tarball and checked directly rather
than trusted from docs prose). The hook's exact signature, from that same
`.d.ts`:

```ts
"tool.execute.after"?: (input: {
    tool: string;
    sessionID: string;
    callID: string;
    args: any;
}, output: {
    title: string;
    output: string;
    metadata: any;
}) => Promise<void>;
```

`args` is typed `any` — the SDK doesn't publish built-in tool arg shapes,
so the actual field names came from a diagnostic plugin dumping raw
`input`/`output` to a file, then a real `opencode run --auto` edit and a
real create. Confirmed: the edit tool is named `"edit"`, the create tool
`"write"`, and **both** put the target path at `input.args.filePath`
(edit also carries `oldString`/`newString`; write carries the new file's
content). Runs `ruff format` + `ruff check --fix` via the plugin's `$`
(Bun shell) on any `.py` `filePath`, inside a uv/ruff project (walks up
for `pyproject.toml`). Verified end-to-end: introduced a real formatting
violation, ran a live `opencode run --auto` edit, confirmed the hook
auto-fixed it before the next tool call saw the file.

---

## 6. Config file location reference

| File | Global | Project |
|---|---|---|
| Main config | `~/.config/opencode/opencode.json` | `opencode.json` in repo root |
| Keybinds/TUI | `~/.config/opencode/tui.json` | `.opencode/tui.json` |
| Commands | `~/.config/opencode/commands/` | `.opencode/commands/` |
| Agents | `~/.config/opencode/agents/` (or inline in `opencode.json`) | inline in project `opencode.json` |
| Plugins | `~/.config/opencode/plugin/` | `.opencode/plugin/` |
| Instructions | `~/.config/opencode/AGENTS.md` (or legacy `~/.claude/CLAUDE.md`) | `AGENTS.md` (or legacy `CLAUDE.md`) |

Global and project configs **merge** (project overrides global on
conflicting keys), same pattern as Claude Code's `~/.claude/settings.json` +
`.claude/settings.json`.

---

## 7. Open decisions (need user input before implementing)

1. ~~Which extra permission-mode agents to define beyond Build/Plan~~ —
   **decided: none, 2-state Build/Plan cycle is fine as-is (see §3)**
2. ~~Bind `session_toggle_generic_tool_output` → `ctrl+o` (no conflict,
   low-risk) — yes/no?~~ **Done (2026-07-24): yes — applied in
   opencode/tui.json, wired through install.sh** (see §4)
3. ~~Remap the leader key off `ctrl+x`~~ — **decided: no, keep default**
   (no tmux collision, user wants the shortcuts as-is)
4. ~~Scope of command porting~~ — **decided/done: all 6 commands already
   ported, incl. backlog-item (see §2)**
5. ~~Whether to pursue the hooks→plugin port at all right now, or leave
   opencode without dashboard/backlog integration for the time being.~~
   **Decided (2026-07-24): defer the port** (real TypeScript lift, not
   config parity work) — captured as its own backlog item so it survives
   independently of this doc. Revisit when there's a real need or when the
   opencode plugin API stabilizes further.
6. ~~Whether `opencode.jsonc` (the bash permission allowlist, `~/.config/
   opencode/opencode.jsonc`) should be tracked in this repo~~ — **Decided
   (2026-07-24): yes.** It existed only as an untracked local file on the
   machine it was set up on, so pulling this repo to a fresh machine would
   have silently dropped back to per-command permission prompts for
   everything. Pulled into the repo at `opencode/opencode.jsonc` and wired
   into `install.sh` copy-once + drift-check, same pattern as
   `claude/settings.json` (not symlinked — opencode likely rewrites this
   file in place as permissions get approved live, same detach risk
   `settings.json`'s comment warns about).
7. ~~Whether `AGENTS.md` needs a repo-tracked equivalent for the backlog
   "show before start" guidance~~ — **Decided (2026-07-24): no.** Per §1,
   opencode already reads `~/.claude/CLAUDE.md` directly as a legacy
   fallback when no `AGENTS.md` exists — the guidance is already live
   without any extra file. Confirmed no local `AGENTS.md` shadows it on
   this machine.

## 8. Plugins (`opencode/plugin/`)

- **`ruff-format-on-edit.ts`** — auto-formats Python files on `tool.execute.after` for `edit` and `write`.
- **`notify.ts`** — listens to `session.idle` event and dispatches cross-platform desktop toasts with the official OpenCode logo via `~/.claude/scripts/notify.py`.

## Sources

- https://opencode.ai/docs/keybinds/
- https://opencode.ai/docs/agents/
- https://opencode.ai/docs/permissions/
- https://opencode.ai/docs/config/
- https://opencode.ai/docs/rules/
- https://opencode.ai/docs/commands/
- https://opencode.ai/docs/cli/
- https://opencode.ai/docs/plugins/ — plugin file shape, global location; does **not** document `tool.execute.after`'s field-level schema or built-in tool arg shapes (`args` is untyped in its own examples)
- `@opencode-ai/plugin` npm package, `dist/index.d.ts` (version pinned to the installed `opencode` CLI's own version, pulled straight from the registry tarball rather than trusted from docs prose) — ground truth for `tool.execute.after`'s exact signature (`input.tool`/`sessionID`/`callID`/`args`, `output.title`/`output`/`metadata`)
- Live probe (2026-08-13): a diagnostic `tool.execute.after` plugin dumping raw `input`/`output` to a file, wired at `~/.config/opencode/plugin/zz-probe.ts`, run against a real `opencode run --auto` edit and a real create → confirmed built-in tool names (`edit`, `write`) and their shared file-path arg key (`args.filePath`), none of which the SDK types or docs page publish

## Pre-tool guard (`tool.execute.before`)

`opencode/plugin/guard-rails.ts` delegates to
`claude/scripts/guard_rails.py`, the same script all five harnesses call.

**Verified live 2026-09-01**, in a throwaway repo with a throwaway backlog
store (`GUARD_RAILS_STORE`): the write was denied and the reason reached the
model verbatim, which quoted it back in full.

Payload shape, confirmed by a diagnostic hook rather than from docs:

    input  = {tool, sessionID, callID}
    output = {args: {filePath, content}}

so the arguments are on the **second** parameter for `.before`, unlike
`.after` (see the ruff plugin's own note). A plugin blocks by **throwing an
Error**; the thrown message is what the model sees.

**opencode has no warn channel.** `tool.execute.before` can throw or return,
with nothing in between. The stale-worktree-base warning is therefore *not*
surfaced on this harness — a `warn` verdict degrades to a silent allow. This
is a real gap, not an oversight: Claude Code has `additionalContext`, Pi has
`ctx.ui.notify`, agy can return `decision: allow` with a `reason`, and
opencode has no equivalent.

**Bash-family payload, confirmed live 2026-09-01** the same way (a
throwaway `tool.execute.before` plugin dumping raw `input`/`output`, run
against a real `opencode run --auto` bash call):

    input  = {tool: "bash", sessionID, callID}
    output = {args: {command}}

No `cwd` field, same as the write-family payload — the guard-rails plugin
uses `process.cwd()` for the bash-family call.
