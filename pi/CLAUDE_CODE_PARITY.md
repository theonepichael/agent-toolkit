# Pi → Claude Code parity notes

Goal: make Pi (`earendil-works/pi`, npm `@earendil-works/pi-coding-agent`,
formerly `badlogic/pi-mono`) feel like Claude Code for this workflow — same
shared instructions, same skills, same backlog/pending-items/git
conventions — as a 5th harness alongside Claude Code, Copilot, opencode,
and agy. Compiled 2026-08-30 from the docs bundled with the installed
package (`~/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/docs/`,
version 0.84.4 at time of writing) and from live probes against that same
install, not from search-engine summaries.

---

## 1. Confirmed facts (read the docs before changing any of this)

- **Global instructions file: `~/.pi/agent/AGENTS.md`, with `CLAUDE.md` read
  as a fallback name** (`docs/usage.md`'s Context Files section: "Pi loads
  `AGENTS.md` or `CLAUDE.md` at startup from: `~/.pi/agent/AGENTS.md` for
  global instructions..."). `links.toml` symlinks `claude/global-instructions.md`
  to this path (as `AGENTS.md`, not `CLAUDE.md` — do not shadow `AGENTS.md` with a
  same-directory `CLAUDE.md`; when both are considered for a directory the
  doc names `AGENTS.md` first). No frontmatter support — always active,
  same as every other harness's shared-instructions file.
- **Prompt templates are the command layer** (`docs/prompt-templates.md`):
  Markdown files, filename becomes the command name (`review.md` → `/review`),
  frontmatter `description` (optional — first non-empty line is the
  fallback) and `argument-hint` (optional). Placeholder syntax (`$1`, `$@`/
  `$ARGUMENTS`, `${1:-default}`, `${@:N}`, `${@:N:L}`) matches opencode's
  exactly. Global discovery root: `~/.pi/agent/prompts/*.md`, non-recursive.
  This repo's `pi/prompts/*.md` are ported from `opencode/command/*.md` —
  the two harnesses' command-file conventions are close enough that porting
  is close to a straight copy, only differing where a body explicitly
  invoked an opencode-only mechanism (see §4).
- **Skills**: Pi implements the [Agent Skills
  standard](https://agentskills.io/specification) (`docs/skills.md`), and
  explicitly supports pointing at *another* harness's skill directory via
  the `skills` setting — its own docs give `~/.claude/skills` as the
  worked example. **All 8 skills are generated into `pi/skills/`** (7 by
  `gen_skills.py`, `second-opinion` by `gen_second_opinion.py`) and found
  purely through Pi's default scan of `~/.pi/agent/skills/` — symlinked
  from `pi/skills/` — since `pi/settings.json` carries no `skills` key (see
  §3 for why an explicit `pi/skills` entry there is wrong, not just
  redundant). Skills register as `/skill:name` commands
  (`enableSkillCommands`, default `true`) and are also model-invoked from
  an `<available_skills>` XML block in the system prompt.
- **Extensions**: TypeScript modules, auto-discovered from
  `~/.pi/agent/extensions/*.ts` (global) or `.pi/extensions/*.ts`
  (project-local, only after project trust) — no `settings.json` entry
  needed once symlinked into the discovery root, unlike skills (see §3).
  Loaded via [jiti](https://github.com/unjs/jiti), so plain `.ts` works with
  no compile step. Full event list in `docs/extensions.md`.
- **No built-in permission system, no sub-agents, no plan mode**
  (`docs/usage.md`'s "Design Principles": "it intentionally does not
  include built-in MCP, sub-agents, permission popups, plan mode, to-dos,
  or background bash. You can build or install those workflows as
  extensions or packages"). This is the single biggest behavioral gap from
  opencode/Claude Code and shapes every prompt file that used to lean on
  opencode's `adversary` agent or its `permission.bash` config — see §4
  and §5.
- **Built-in tools**: `read`, `bash`, `powershell` (Windows), `edit`,
  `write`, `grep`, `find`, `ls` (`docs/usage.md`). No question/select tool
  among them — this repo now supplies one as an extension instead
  (`question-tool.ts`, §5), so an interactive Pi session does get a
  structured multi-choice prompt. The plain-text-with-a-recommendation
  convention Copilot/agy use (CLAUDE.md's harnesses-without-a-widget rule)
  is still the fallback, and still the only option in headless `-p`/JSON
  modes, where the tool refuses to run.
- **Headless invocation**: `pi -p [--no-session] [--provider <name>]
  [--model <pattern>] "<prompt>"` (`docs/usage.md`'s CLI Reference).
  `--list-models [search]` checks whether a model id actually resolves
  before handing off to it (used by `backlog-item.md`'s model-handoff
  branch instead of guessing).

## 2. Prompts — ported (`pi/prompts/`)

All 8 files from `opencode/command/*.md` (`dashboard`, `grill-me`,
`make-skill`, `second-opinion`, `spec`, `standup`, `backlog-item`,
`to-tickets`) ported 2026-08-30. `second-opinion.md` is machine-generated
by `claude/scripts/gen_second_opinion.py` from `templates/second_opinion.md.tmpl`
— never hand-edit it; edit the template or `gen_second_opinion.py`'s
`HARNESS_TABLE` entry for `pi/prompts/second-opinion.md` and regenerate.
`dashboard.md`, `standup.md`, and `to-tickets.md` ported with no
opencode-specific rewording — their bodies were already harness-neutral.
`grill-me.md`, `spec.md`, `backlog-item.md`, and `make-skill.md` needed real
rewrites wherever the opencode original invoked a mechanism Pi doesn't
have:

- Any reference to opencode's native Task-tool subagent spawn (grill-me's
  `--auto` critique path, spec's audit-pass step) became a plain
  `second_opinion.py` critique-loop call — Pi has no sub-agent mechanism at
  all (§1), so there's no "prefer the native path, fall back to
  second_opinion.py" framing the way opencode's version has; it's the only
  path.
- Any reference to opencode's native skill tool (`skill({ name: "..." })`)
  became `/skill:name` — Pi's real, documented skill-invocation command
  (`docs/skills.md`'s "Skill Commands"), not a guess.
- `make-skill.md` was rewritten from scratch (not a straight port) since
  opencode's version is about authoring *opencode* skills at
  `opencode/skills/<name>/SKILL.md` — Pi has no such directory of its own
  (§3), so the Pi version documents authoring at `agy/skills/<name>/SKILL.md`
  instead, the file Pi actually reads.
- `backlog-item.md`'s "different/cheaper model" handoff branch uses Pi's
  confirmed `--list-models` and `pi -p --provider/--model` invocation
  (§1) instead of opencode's `opencode models` / `opencode run --auto -m`.
  No `--auto`-equivalent flag is needed for headless tool execution — Pi
  has no built-in permission-prompt system to bypass in the first place
  (§1); the only gate is whatever `permission-gate.ts` (§5) itself denies
  or blocks.

## 3. Skills — 8 generated skills in `pi/skills/`, found via Pi's default scan

`pi/skills/` holds all 8 generated skills (`backlog-item`, `dashboard`,
`grill-me`, `make-skill`, `spec`, `standup`, `to-tickets` via `gen_skills.py`,
and `second-opinion` via `gen_second_opinion.py`). `pi/settings.json`
(copy-once like `claude/settings.json` — Pi rewrites parts of this file
live via `/settings`, `/model` Ctrl+S, etc., same detach risk that file's
comment warns about) carries **no** `skills` key:

```json
{
  "lastChangelogVersion": "0.84.4"
}
```

All 8 skills are found purely through Pi's default, always-on scan of
`~/.pi/agent/skills/`, which `links.toml` (§ below) symlinks straight to
`pi/skills/`. An earlier version of this file explicitly listed
`"skills": ["/home/yanil/dotfiles/pi/skills", "/home/yanil/dotfiles/agy/skills"]`
— that was wrong, not just redundant: Pi's `skills` config is *additive* to
the default scan, not a replacement for it (`docs/skills.md`), so listing
`pi/skills` a second time made every one of the 8 skills resolve at two
different absolute paths (the symlink target from the default scan, plus
the explicit array entry) and collide with itself — a repo-wide collision
warning on every skill. Dropping the `skills` key entirely is the fix;
never reintroduce an explicit `pi/skills` entry. The `agy/skills` fallback
for a hypothetical future agy-only skill is gone as a side effect; if one
is ever needed, it takes its own dedicated entry (`"skills": ["agy/skills"]`,
never paired with `pi/skills`) so the same double-discovery bug doesn't
recur.

`links.toml` manages `pi/skills` with a `dir = true` row that automatically
symlinks all subdirectories under `pi/skills/` into `~/.pi/agent/skills/`.

## 4. Delegation mechanics — `/skill:name`

Documented in `docs/skills.md`'s "Skill Commands" section: every discovered
skill registers as a `/skill:<name>` command (`/skill:grill-me`,
`/skill:second-opinion`, `/skill:spec`), and arguments after the command are
appended to the skill content as `User: <args>`. The model can also load a
skill on its own once the topic matches the skill's `description` in the
`<available_skills>` system-prompt block ("How Skills Work"), without a
user typing the slash form at all. `pi/prompts/backlog-item.md`,
`grill-me.md`, and `spec.md` all delegate this way now — see §2.

## 5. Extensions (`pi/extensions/`)

Thirteen extensions. Their verification states genuinely differ, so this
section records each one's rather than asserting a single claim over all of
them:

- **Exercised live in a real Pi session**, with the tool call read back out
  of the session JSONL rather than inferred from the reply text —
  `guard-rails`, `permission-gate`, `ruff-format-on-edit`,
  `dev-status-tool`, `notify`, and the five script wrappers below
  (`grill`, `second-opinion`, `standup`, `to-tickets`,
  `vitals-promotion`). Provider `opencode-go`, model `kimi-k2.6`.
- **`question-tool`**: both paths exercised — the headless refusal, and the
  TUI picker driven through a pty, which returned a real arrow-key
  selection of the *second* option (a default-accepting bug would have
  returned the first).
- **Unit-tested only, no live run**: `custom-footer` and
  `philosophy-header`. Both are cosmetic and TUI-only. `philosophy-header`
  ran live for a day before adoption, but as the pre-refactor file, not the
  version committed here.

A note that cost real debugging time and applies to every tool below: a
fenced ` ```bash ` fallback block defeats a "prefer the tool" instruction
no matter how clear the surrounding prose is. See `dev-status-tool.ts`'s
subsection for the regression that established this. Every prompt template
converted to a tool since has had its fenced command block removed, not
reworded.

### `guard-rails.ts`

Enforces safety and workflow guard rails:
- **`rm -rf` confirmation gate**: blocks recursive force deletions (`rm -rf`, `rm -fr`, `rm -r -f`, `--recursive --force`) unless confirmed via `ctx.ui.confirm()`. In headless `-p` mode (`!ctx.hasUI`), blocks outright.
- **`sudo` confirmation gate**: blocks privileged commands unless confirmed via `ctx.ui.confirm()`. In headless `-p` mode, blocks outright.
- **`git commit` main/master branch protection (worktree policy)**: resolves the target git working directory and branch. If attempting `git commit` directly on `main` or `master`, blocks with a guidance message enforcing feature branches or worktrees. Allowed on feature branches.
- **`write`/`edit` protected path protection**: blocks modifications to `.env` (and `.env.*`), `.git` internal files, and `node_modules`.
- **Toggle commands**: `/guard-rails-disable` and `/guard-rails-enable` for session-scoped override.

**Verified live**:
1. `rm -rf /tmp/test_guard_file.txt` in headless `-p` mode was blocked and preserved on disk.
2. `sudo id` in headless `-p` mode was blocked.
3. `git commit -m 'test commit'` on branch `main` in a test repo was blocked with house policy guidance. Switching to `feat-test` allowed the commit to succeed.
4. `write` tool calls to `/tmp/.env`, `/tmp/.git/config`, and `/tmp/node_modules/pkg/index.js` were all blocked by guard-rails, while safe file writes succeeded.

### `permission-gate.ts`

Replicates `opencode.jsonc`'s `permission.bash` allowlist — the same rule
set, re-expressed for Pi's `tool_call` event instead of a declarative
config Pi has no equivalent of (§1: no built-in permission system at all).
Only bash is gated, matching `opencode.jsonc`'s actual current scope (no
deny entries exist there today, only allow + a `"*": "ask"` default);
`opencode.jsonc`'s separate `external_directory` permission type has no
direct Pi analog and is out of scope.

On the "ask" tier (anything not on the allowlist), the extension checks
`ctx.hasUI` (`docs/extensions.md`'s ExtensionContext: `true` in TUI and RPC
modes, `false` in print mode `-p` and JSON mode) — `true` prompts via
`ctx.ui.confirm()`, `false` blocks outright since there's nothing to
confirm through. This corrects an assumption from planning: RPC mode
*does* have `ctx.hasUI === true` (it has its own extension-UI protocol,
`docs/rpc.md`), unlike `-p`/JSON mode — checking `ctx.hasUI` directly,
rather than hardcoding a list of "headless" mode names, is what makes this
correct regardless.

**Verified live**: an allowlisted command (`echo hello-from-allowlist`)
ran and printed its output normally; a non-allowlisted command
(`touch not-allowed-marker.txt`) run in headless `-p` mode was blocked —
the model's own turn reported "The command was blocked: **Blocked by
permission-gate (no UI to confirm through in this mode): touch
not-allowed-marker.txt**" and no file was created.

**Deliberate divergence from `opencode.jsonc` (added once `dev-status-tool.ts`
shipped, below):** `python3 ~/.claude/scripts/dev_status.py *` is *not*
allowlisted here, unlike every other harness's permission config. Leaving
it allowlisted would let the model silently bypass the native `dev_status`
tool and shell out instead; dropping it to the `"*": "ask"` default makes
that bash path need confirmation (or fail outright headless), while the
tool's own internal `pi.exec` calls are unaffected — they never go through
the `tool_call` event this gate hooks at all, since they're plain
subprocess calls from already-running extension code, not a model-invoked
`bash` tool call. Verified live: a direct bash call to `dev_status.py
render` in headless mode was blocked with the same message shown above;
the `dev_status` tool's own `render` action, called in the same session
with both extensions loaded, succeeded with zero blocking.

### `ruff-format-on-edit.ts`

Runs `uv run ruff format` + `uv run ruff check --fix` on any `.py` file the
`write`/`edit` tools touch, inside a uv/ruff project (detected by walking
up for `pyproject.toml`) — same mechanism as the Claude Code/Copilot/agy
hooks, and the same corrected event mechanics `opencode/plugin/ruff-format-on-edit.ts`
already found for opencode's analogous hook, independently re-confirmed
here for Pi's own event shapes (`docs/extensions.md`'s Tool Events):

- `tool_execution_end` does **not** carry the file path — only
  `{ toolCallId, toolName, result, isError }`.
- The path is only available on **`tool_execution_start`**, which carries
  `{ toolCallId, toolName, args }` — for `write` and `edit`, `args.path`.
- Implementation: stash `args.path` in a `Map` keyed by `toolCallId` on
  `tool_execution_start`; on the matching `tool_execution_end`
  (`isError === false`), look up the path, run ruff, delete the map entry.

**Verified live**: a scratch uv/ruff project with `def   add(a,b):` /
`return a+b` in `sample.py`; a real Pi edit adding one unrelated comment
line above the `def`; the file came back as `def add(a, b):` /
`return a + b` — ruff format ran automatically after the edit, not just
on the specific lines the model touched.

### `dev-status-tool.ts`

Registers `dev_status` — a single `pi.registerTool()` custom tool wrapping
every `claude/scripts/dev_status.py` subcommand with a typed schema
(`StringEnum` action + optional identity/patch/flag fields), so Pi's
prompt templates can call it directly instead of composing a raw bash
string. Full design history (3 rounds of `/second-opinion` critique, two
real bugs caught and fixed) is in
`~/.claude/data/grill/pi-tool-dev-status-spec.md` and its companion
critique-notes file — not restated here, only the load-bearing corrections
that shape the code:

- **Mutating actions refuse a numeric `slug`/`secondarySlug` outright.**
  Two earlier designs (auto-fetch the current rev; auto-resolve the
  position to a slug via `show`) both silently risked mutating whatever
  item currently occupies a drifted position instead of what the model
  actually meant. The shipped design forces two explicit, model-visible
  tool calls instead: `show` to resolve a position to its real `id`, then
  the mutating call by that slug. Read-only actions still take numeric
  positions directly (`dev_status.py` already handles `<slug|N>` natively
  for reads, and nothing gets mutated).
- **`pi.exec`'s real `ExecOptions` has no `env` field** — confirmed
  against the installed package's own `dist/core/exec.d.ts`
  (`{signal?, timeout?, cwd?}` only), not assumed from the docs' prose
  example. `DEVSTATUS_AGENT=1` is set via the `env` coreutil as the
  actual command (`pi.exec("env", ["DEVSTATUS_AGENT=1", "python3", ...])`),
  not via a nonexistent `options.env`.
- **`StringEnum` imports from `@earendil-works/pi-ai`**, not
  `@earendil-works/pi-coding-agent` — confirmed against both packages'
  real, installed type definitions.
- stderr is appended to the tool's returned `content` whenever non-empty,
  on both success and failure — `content` is what reaches the model,
  `details` doesn't (confirmed against `docs/extensions.md`'s own inline
  comment on the Tool Definition example). In practice this rarely fires:
  `dev_status.py`'s own agent-facing reminders (e.g. `add`'s
  blocker-relationship prompt) are gated by `_agent_quiet()`, true
  whenever `DEVSTATUS_AGENT=1` — which this tool always sets — confirmed
  by reading `dev_status.py` directly and by running a real `add` under
  that flag and observing empty stderr. The forwarding stays in as a
  backstop against anything not similarly gated, not because a real
  reminder was ever caught by it.
- **2026-08-31, `start` gained `force`/`allowMain`/`claimedBy`.**
  `dev_status.py start` (66aa8aa) added `--force`/`-f`,
  `--allow-main`/`--no-worktree-check`, and `--claimed-by <harness>` — a
  claim-collision refusal and a main/master worktree guard, both new. This
  tool's `start` action initially shipped with none of the three wired
  (`force` existed on the `Field` union already, but only for `prune`),
  so Pi had no typed-tool way to clear either refusal and no raw-bash
  fallback either (`permission-gate.ts` blocks it). Backlog item
  `meta-pi-dev-status-tool-start-flags` closed the gap: `force`/
  `allowMain`/`claimedBy` added to `start`'s `ACTION_FIELDS` (allowed,
  not required — `start` still works with just `slug`) and `buildArgv`,
  following the same conditional-spread pattern already used by
  `list`/`recap`/`backfill_gate`.

**Verified live**: extension loads cleanly (`pi -e
dev-status-tool.ts "..."` with no error); `render` returns the real
dashboard; `show` resolves both a real slug and a real numeric position;
a mutating action (`start`) with a numeric `slug` was refused with the
exact guidance error and left the store untouched; a `patch` payload
containing an apostrophe (`Pi's skill discovery test`) round-tripped into
`dev_status.py` correctly — the specific hazard this tool exists to
eliminate; a missing required field and a field valid only for a
different action were both refused before `pi.exec` ran.

**A fenced ` ```bash ` fallback block actively defeats a "prefer the tool"
instruction — confirmed by a real regression, not theory.** After
`permission-gate.ts` moved `dev_status.py` off its bash allowlist (see its
own subsection above) specifically so a direct bash call would need
confirmation and push usage toward this tool, `pi/prompts/dashboard.md`'s real `/dashboard` template —
run for real through `--prompt-template`, not a hand-picked test prompt —
still fell through to the bash fallback and got blocked, even though the
tool instruction came first and was unambiguous prose. The fenced code
block a few lines down was more salient to the model than the prose
around it. Fix: drop the fence entirely for a fallback path that should
rarely fire — state it in plain prose, and add an explicit "check your
actual tool list before assuming this" qualifier. Re-verified clean across
3 consecutive real `/dashboard` runs after the fix, plus a spot check of
`backlog-item.md`'s step 1 and `standup.md`'s `pending_add` path. Applies
generally: any Pi prompt template with a "primary: tool, fallback: bash"
shape should keep the fallback out of a code fence, not just this repo's
3 dev_status-calling files.

### The script-wrapper tools

`grill-tool.ts`, `second-opinion-tool.ts`, `standup-tool.ts`,
`to-tickets-tool.ts` and `vitals-promotion-tool.ts` all follow
`dev-status-tool.ts`'s pattern: one `pi.registerTool()` per script, a
typed `StringEnum` action plus optional fields, validation in exported
pure functions so it is unit-testable, and `promptGuidelines` telling the
model never to reach for the script via bash. Each has its Pi prompt
template converted to call the tool, a `links.toml` entry, and bun tests
over its `assertFields`/`buildArgv` helpers. Only what is distinctive is
recorded here.

- **`grill-tool.ts`** — all 14 subcommands, the largest wrapper. Unlike
  `dev_status.py`, `grill.py` addresses sessions by slug or unique
  substring only, never a numeric position, so the numeric-identity
  refusal that dominates `dev-status-tool.ts` has no analogue and
  `session` passes through as-is. The validation mirrors constraints the
  script already enforces so a bad call fails with a useful message rather
  than an argparse error: `new` needs `payload.topic`, `ask`/`decide` need
  `payload.id`, and a `VERIFIED` or `DISPUTED` verdict needs `evidence`,
  matching `grill.py`'s own `EVIDENCE_REQUIRED`.
- **`second-opinion-tool.ts`** — `detect` and `review`. `modelIndex` is
  compared against `undefined`, not truthiness: index 0 is round 1 of the
  rotation, and a truthiness test would silently drop it and fall back to
  the single-model override instead of the pool. The multi-round loop,
  plan revision and convergence judgment stay in the prompt template; the
  script is single-round by design and the tool does not model them.
- **`standup-tool.ts`** — `fetch` only. The tool validates `date`'s shape
  before the script runs: `standup.py` takes it as a bare string, and a
  wrong shape does not error, it lands the window on the wrong day and the
  standup silently covers the wrong period. Pending-item writes are
  deliberately absent — those go through `dev_status`.
- **`to-tickets-tool.ts`** — `run` only, path in, slugs out. The argv shape
  gains little; what it removes is the shell. Batch files and the ticket
  text behind them routinely contain apostrophes, and `to-tickets.md` had
  to carry a quoting rule to stop an inline single-quoted command breaking
  on them. `pi.exec` takes argv directly, so no shell parses the path.
- **`vitals-promotion-tool.ts`** — the script is flags-only, with no
  subcommands. Modelled as two actions (`run`, `needs_review_summary`)
  rather than a bare flag bag, so `apply` cannot be offered on the
  summary-only path where the script would silently ignore it.

**Verified live** (2026-08-30, tool calls read from session JSONL):
`grill` `{action:"list"}` returned the real session list; `second_opinion`
`{action:"detect"}` returned the four available backends; `standup`
`{action:"fetch"}` returned real data and `--date` correctly shifted the
window; `to_tickets` `{action:"run", batchFile:…}` created two throwaway
tickets with their `blocked_by` edge intact, since removed. `vitals_promotion`
is the sharpest evidence that the schema reads correctly to a model rather
than only to its author: asked for a dry run, the model sent
`{action:"run"}` with `apply` omitted — exactly the intended encoding.

Also verified: with `grill.py` and `second_opinion.py` off
`permission-gate.ts`'s bash allowlist, a direct instruction to run
`python3 ~/.claude/scripts/grill.py list` as bash was refused by the model,
which rerouted through the `grill` tool on its own.

### `question-tool.ts`

Registers `question` — the AskUserQuestion equivalent Pi has no built-in
for (§1). Takes 1–4 questions, each with a short `header`, an optional
`multiSelect`, and 2–4 `{label, description}` options, and returns the
user's choices. The TUI half is adapted from Pi's own shipped example
(`examples/extensions/question.ts`: arrow-key option list, inline
free-text editor, Escape to cancel); the schema shape, validation,
multi-select, and the multi-question loop are this repo's.

- **Headless is a hard error, never a silent default.** `ctx.hasUI` is
  false in `-p` and JSON modes (§1, and the same check `permission-gate.ts`
  and `guard-rails.ts` make), so there is nothing to prompt through.
  Answering on the user's behalf would be worse than failing, so `execute`
  throws with the fallback spelled out: state the options and the
  recommendation in plain text. RPC mode has `hasUI === true` but no TUI,
  so it routes to `ctx.ui.select()` instead of `ctx.ui.custom()` — which
  `docs/extensions.md` documents as TUI-only — with multi-select degrading
  to a single choice there.
- **Cancellation is distinguishable from an answer** in both the returned
  text and `details.cancelled`, and the returned text explicitly forbids
  inferring a choice or falling back to the recommended option. A cancel
  partway through a multi-question run reports what was already answered,
  labelled as incomplete, rather than passing it off as the full result.
- **The recommendation-first house rule is enforced, not just requested.**
  CLAUDE.md's "Judgment calls — lead with a recommendation" says the
  recommended option goes first and is labelled `(Recommended)`;
  `assertQuestions` refuses a call that puts the marker anywhere but
  `options[0]`, or on more than one option. Duplicate option labels are
  refused too — the answer comes back by label, so identical labels are
  unresolvable — as are duplicate question headers.

**Verified**: unit tests over the exported pure helpers
(`pi/test/question-tool.test.ts`) plus typecheck and lint. The interactive
`ui.custom` path is not unit-testable and has not been driven live in a
real TUI session — unlike the four extensions above, this one's UI is
verified by adaptation from Pi's shipped example, not by observation.

### `custom-footer.ts` and `philosophy-header.ts`

The two cosmetic extensions. Both are TUI-only and both are **unit-tested
but never run live** — stated plainly because the rest of this section's
claims are live-verified and flattening the difference would make the
section less useful, not more.

- **`custom-footer.ts`** — replaces the footer with the git branch, the
  active model, and a pending-item count, behind `/custom-footer`. It is
  the one extension with no test file at all; `refreshPendingCount` parses
  `dev_status.py pending list` JSONL and swallows malformed lines, which is
  real logic worth covering.
- **`philosophy-header.ts`** — replaces Pi's startup header with a wordmark
  and one of six taglines quoted from this repo's own `STYLE.md` and
  `claude/global-instructions.md`, picked once per session; `/builtin-header` restores
  the stock one. It imports only a type, so no `pi.exec`, no filesystem, no
  network, and `ctx.mode !== "tui"` makes it inert in print, RPC and JSON
  modes.

- **`notify.ts`** — listens to `agent_settled` (when Pi returns to idle)
  and `ui_prompt_start` (when Pi prompts the user for interactive choices),
  dispatching cross-platform desktop toasts with the official `pi.dev`
  geometric logo via `~/.claude/scripts/notify.py`.

`philosophy-header.ts` is also the cautionary tale for this whole
directory. It was authored straight into `~/.pi/agent/extensions/` and
never tracked — Pi loads every `.ts` there, so it ran in every TUI session
from a file that was invisible to review, absent on every other machine,
and one directory rebuild from gone. `custom-footer.ts` failed the same way
from the other direction: it was committed to the repo but hand-symlinked
from the worktree that authored it, so removing that worktree left a
dangling link and it silently stopped loading. `test_pi_extension_links.py`
now asserts repo-to-`links.toml` parity in both directions, which catches
the second failure. It cannot catch the first — a file that exists only in
the installed directory is still undetected, tracked as its own item.

## 6. second-opinion / dev_status recap backend

`llm_backends.py`'s `BACKEND_PRIORITY` is `["agy", "pi", "opencode",
"copilot"]` — `pi` sits right after `agy`, ahead of `opencode`/`copilot`,
per explicit user preference during this port (not derived from any
technical ranking). `run_pi()` invokes `pi -p --no-session --provider
opencode-go --model <id> "<prompt>"` — `opencode-go` is hardcoded as the
provider since it's the only one confirmed authenticated on this machine
(`pi auth check --provider opencode-go --json` -> `ready`) and the one
every configured model-pool entry resolves through. `SECOND_OPINION_PI_MODEL`
/ `SECOND_OPINION_PI_MODEL_POOL` / `SECOND_OPINION_PI_TIMEOUT_SECONDS`
follow the same contract as the other three backends.

Unlike opencode's `adversary` agent (`"permission": "deny"`, forcing a
swapped-in model to return only prose), `run_pi()` passes no
tool-restriction flag — no equivalent restricted-permission invocation for
Pi has been built or verified. `_raise_on_emitted_tool_call()` is the only
backstop against a model leaking an attempted tool call as text; it cannot
catch Pi actually taking a real tool action instead of returning a
critique. Revisit if this ever becomes a real problem in practice —
`--tools`/`--no-tools`/`--exclude-tools` (§1) are the documented levers,
just not wired into `run_pi()` yet.

## 7. Config file location reference

| File | Global | Project |
|---|---|---|
| Main settings | `~/.pi/agent/settings.json` | `.pi/settings.json` |
| Instructions | `~/.pi/agent/AGENTS.md` (or `CLAUDE.md`) | `AGENTS.md` (or `CLAUDE.md`), walking up from cwd |
| Prompt templates | `~/.pi/agent/prompts/*.md` | `.pi/prompts/*.md` |
| Skills | `~/.pi/agent/skills/`, `~/.agents/skills/`, plus `settings.json`'s `skills` array | `.pi/skills/`, `.agents/skills/` |
| Extensions | `~/.pi/agent/extensions/*.ts` | `.pi/extensions/*.ts` |
| Sessions | `~/.pi/agent/sessions/` | — |
| Trust decisions | `~/.pi/agent/trust.json` | — |

Global and project settings merge (project overrides global on nested
keys), same pattern as Claude Code's `~/.claude/settings.json` +
`.claude/settings.json`.

## 8. Out of scope for this port

- **Keybindings / TUI customization** — no `pi/keybindings.json` equivalent
  of opencode's `tui.json` exists in this repo; nothing in the original
  plan named a specific rebind worth doing, and none has come up since.
- **A dashboard-on-`session_start` extension** — Pi supports the
  `session_start` event (§1, `docs/extensions.md`), so this is technically
  buildable, but it's a real engineering task, not config parity, and
  wasn't part of this port's scope (mirrors opencode's own deferred
  hooks→plugin dashboard port, `opencode/CLAUDE_CODE_PARITY.md` §5). Revisit
  as its own backlog item if resuming a `grill-me`/`backlog-item`
  mark-pending-execution flow manually (§2) becomes a real friction point.
- **A restricted-permission invocation for `run_pi()`** — see §6.

## Sources

- `~/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/docs/` —
  the full doc set bundled with the installed npm package, version 0.84.4:
  `usage.md`, `prompt-templates.md`, `skills.md`, `extensions.md`,
  `settings.md`, `security.md` in particular.
- `pi --help` (installed 0.84.4) — top-level flag reference, cross-checked
  against `docs/usage.md`'s CLI Reference.
- Live probes against the installed 0.84.4 build, provider `opencode-go`,
  model `kimi-k2.6`: skill discovery, `permission-gate.ts`'s allow/block
  paths, `ruff-format-on-edit.ts`'s auto-fix path — all in this doc's own
  sections above, not asserted separately.

## Pre-tool guard: worktree rules delegated to the shared script

`pi/extensions/guard-rails.ts` keeps its own `rm -rf`, `sudo`,
protected-path and `git commit` guards — those need `ctx.ui.confirm()`,
which a subprocess cannot do — and gains a write/edit branch that calls
`claude/scripts/guard_rails.py`, the single source of truth for the
main-checkout and stale-base rules across all five harnesses.

A `deny` becomes `{block: true, reason}`; a `warn` becomes
`ctx.ui.notify(reason, "warning")`. Failure to run the script fails open.

**Verified live 2026-09-01**, with a control: the write was blocked with the
guard active, and the same prompt under `GUARD_RAILS_OFF=1` wrote the file
successfully — so the block was the guard, not an unrelated failure. That
control also confirms the escape hatch works end to end in a real harness.
