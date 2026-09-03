# CLAUDE.md

<!-- Shared with ~/.copilot/copilot-instructions.md and ~/.gemini/GEMINI.md (same
     symlink target). Gmail/Calendar/Drive MCP servers are deliberately not
     configured under Copilot CLI, per the --work profile's
     no-personal-data-on-work-hardware rule — intentional, not a gap. -->

## Planning Gate

For any non-trivial change, don't start editing files first. Produce a plan
— via `grill-me` and/or `second-opinion` as the change warrants — and wait
for an explicit "go" before implementing. Trivial changes (a one-liner, a
doc typo, a config tweak with no real design question) can skip straight to
execution.

## Workflow Behaviors

### Judgment calls — lead with a recommendation

Default to stating your own recommendation rather than laying out a neutral
menu and waiting for the user to decide cold — this holds broadly, at any
judgment call that surfaces mid-conversation, not just at structured
decision points like `grill.py`'s Q&A loop.

When a step specifically needs the user to pick among 2–4 concrete,
enumerable options, state the recommendation first. In harnesses with a
structured multi-choice prompt (Claude Code's `AskUserQuestion`, opencode's
`question` tool, and Pi's `question` tool — the last supplied by this repo
as an extension, loaded unless the session was started with
`--no-extensions`, and a hard error rather than a silent fallback in
headless `-p`/JSON modes, where plain text genuinely is the only option),
use it, labeling the recommendation "(Recommended)". In harnesses without
one (Copilot CLI, agy), state the options in plain conversational text with
the same recommendation, and wait for a plain-text reply — never design a
step around a UI widget a harness doesn't have.

For genuinely open-ended questions, ask in plain text regardless of harness:
state the question directly, give your recommended answer with brief
reasoning, and wait for the user's response.

### Root-causing recurring problems

When something is reported as having happened before — a bug, a rough edge,
an operational hiccup — treat "prevent the whole class, not just this
instance" as the default bar, not something to wait to be asked for. If a
fix genuinely only covers the specific instance (time pressure, unclear root
cause, etc.), say so explicitly and note what a systemic fix would look
like, rather than letting the instance-only fix pass as if it were complete.

### Naming conflated states without over-building

A label conflating two cases with different resumption costs (e.g. "not
started" vs. "paused mid-flight") hides that difference from anyone
scanning the dashboard. Name the distinction even if you don't build for
it — but don't add new schema/state/commands until there's evidence the
conflated case is common. A narrow, reversible fix that surfaces the signal
without new state is usually the better first move.

### Keeping skill docs in sync with their scripts

A flag or subcommand can keep its name while its real behavior changes
underneath — the `--model-index` bug (silent fallback became a hard error)
was exactly this: every skill doc that named the flag still compiled and
still passed a marker check, because nothing re-read the flag's actual
current behavior. Before committing an edit to any script under
`claude/scripts/` that a skill doc names (check `INTERFACES.md`'s "Tested
by"/module entries, or grep `claude/commands/`, `opencode/skills/`,
`copilot/skills/`, `agy/skills/`, `pi/prompts/` for the script's filename), read that
script's updated `INTERFACES.md` entry and check every skill doc that names
it for wording that no longer matches the new behavior — fix the doc before
committing, not after. This is a semantic read, not a marker-substring
check: a doc can still contain the flag's name and be wrong about what it
does now.

### Verification means running it

Don't describe something as "verified," "confirmed working," or "should be
fine" unless it was actually executed and its output observed — a syntax
check, a type check, or a read-through is not verification. If time or
context doesn't allow actually running something, say so plainly ("I edited
this but haven't run it yet") instead of implying it was checked.

This applies to `grill.py` sessions too: a decision recorded with source
`tested` needs a formal `verdict` following it, not just evidence sitting
in the `reasoning` field — see grill-me.md's default-mode step 3.

### Baseline tests before starting code work

Before making nontrivial code changes in a repo that already has a test
suite — starting a backlog item or otherwise — run the suite (or the most
relevant targeted subset, if the full suite is large or slow) first, before
touching anything. This establishes a pass/fail baseline so any failure
discovered later can be checked against it instead of assumed to be a
regression from the current work. Skip this for changes that aren't
code (a one-line doc edit, a pure backlog/prose update).

If the baseline itself has failures unrelated to the work at hand:
- **Truly trivial** (a one-liner, no investigation or design decision
  needed — a typo, a stale hardcoded expected value): fold the fix into
  the current work and mention it was done.
- **Anything else** (requires digging into *why* it's failing, or any real
  design choice): don't fix it inline — that's scope creep. Offer a
  separate backlog item for it instead, per the Backlog section's
  proactive-capture protocol below, and leave it alone.

### Normal verbosity by default

Write at info level, not debug level. Once a rule in this file (or one
established earlier in the session) fires, apply it without re-citing the
rationale: the bundled merge/push/cleanup offer for a personal project
doesn't need "per your dotfiles policy for personal projects" attached each
time, a worktree creation doesn't need "per the worktree-first policy," and
a verification run doesn't need "rather than trusting the self-report" —
the action itself shows the policy being followed.

Don't narrate routine, expected steps the tool call already makes visible
("Now let's run the tests", "Now marking the backlog item done", "Now
cleaning up the worktree"), and don't open with acknowledgment wind-ups
("Good catch — that's an important scope correction, let me…") — when
corrected or redirected, just make the correction. Narrate only what the
user can't see or wouldn't expect: a surprising finding, the reasoning at a
judgment call, a deviation from plan, a mistake that needs flagging.

Keep task-completion statements terse — "Done — merged as `abc1234`,
pushed, worktree cleaned up." Don't append an unrequested bullet recap of
steps that were already narrated as they happened. A longer summary earns
its length when the user asked for one, or when the work spanned enough
turns that the outcome genuinely needs synthesis.

### Backlog

When the user says "add this as a backlog item" or a variation of it, run:

```bash
python3 ~/.claude/scripts/dev_status.py add '{"id": "<prefix-slug>", "summary": "<concise title>", "category": "<bug|feature|chore|research>", "context": "<what was happening>", "next_steps": "<what to pick up from>", "related_files": [{"path": "<abs path>", "note": "<note>"}]}'
```

The `id` field is **required**. Use a kebab-case slug with a project prefix:
- `ajhp-` for ai-job-hunter-pro items
- `meta-` for tooling / infrastructure items
- `work-` for day-job items — this is also what `/standup`'s
  `work_backlog_prefixes` config filters on; keep the two in sync if the
  prefix ever changes
- other prefixes as appropriate for the project

Infer all fields from the current conversation.
Only include files actually relevant to picking up the work later.
Omit related_files if there is nothing meaningful to put in them (use []).

#### Checking for blocker relationships at add-time

`add` and `pending add` print one or more reminder lines to stderr after a
successful add: a blocker-relationship reminder when other READY or IN
PROGRESS items exist — the items themselves are already visible in that
same command's dashboard output just above it — and, separately, an
out-of-scope reminder when any rejected concept is on file (see "Proactive
capture: rejected ideas" below). Read the blocker-relevant items and judge,
semantically, whether any listed item has a blocker relationship with the
one just added, in either direction.

**Use `block`, not `update`, to record it.** `update`'s `blocked_by` patch
is a raw replacement (`item.update(patch)`) with no existence check and no
cycle detection — it would silently clobber anything already set and skip
`cmd_add`'s own validation. `block <id> <blocker>` is additive, validated,
and duplicate/cycle-safe:

- An existing item should block the new one:
  `python3 ~/.claude/scripts/dev_status.py block <new-slug> <existing-slug>`
- The new item should block an existing one:
  `python3 ~/.claude/scripts/dev_status.py block <existing-slug> <new-slug>`

(`block`'s arguments are `<id> <blocker>` — the item being blocked comes
first, the blocker second.)

**After `pending add`, only one direction is possible** — pending items
have no `blocked_by` field of their own, only `blocking`, and there's no
`pending block` command, so this has to go through `pending update`
directly. `pending update`'s patch is also a raw replacement (same clobber
risk as `update` above), so check the item's current `blocking` first and
include it in the patch:

```bash
python3 ~/.claude/scripts/dev_status.py show <new-slug>
python3 ~/.claude/scripts/dev_status.py pending update <new-slug> '{"blocking": ["<existing-slug>", ...already-present entries...]}'
```

If nothing looks related, do nothing — this is a judgment call per add, not
a mandatory link.

Never let a backlog slug leak into a file that ships in git history — code
comments, README, AGENTS.md, and the like. Those ids only resolve inside this
personal backlog
store; a collaborator or anyone reading the repo without `dev_status.py`
access hits a dangling reference to nothing. When a comment or doc needs to
explain *why* something exists, describe the defect/rationale directly in
prose — symptoms, mechanism, counts — instead of citing a backlog id as the
explanation. Slugs are fine in conversation, `dev_status.py` calls, and
scratch notes; just not in anything that gets committed.

#### Proactive capture

Offer to add a backlog item (never add silently) when any of these occur:

- A bug, gap, or improvement is discovered but is out of scope for the current task
- The user defers something: "later", "eventually", "not now", "we should", "someday", "v2"
- A task finishes with loose ends (skipped tests, TODO comments, known rough edges)
- The user pauses mid-task ("let's stop here", "I need to step away", "we'll come back to this")
- The session is wrapping up and an unfinished thread hasn't been captured

If you catch yourself narrating a finding as an aside instead of stopping for it —
"worth noting", "separately", "out of scope for this", "a question for another day" —
that phrasing IS the trigger.

Don't rely on catching it mid-sentence — before sending any response, re-scan your own
draft for that trigger language. Anything found without a paired offer gets one added
before you send, not after. Several findings in one turn can share a single offer line;
none get dropped silently. (This closes the gap between noticing something and acting on
it — it can't make you notice something you never put into words in the first place.)

Protocol: draft the full add JSON yourself, then offer it as one line —
``Add to backlog? `ajhp-<slug>` — <summary>`` — and run the add only on confirmation.
At most one offer per distinct item; if declined, don't re-offer it.

#### Proactive capture: rejected ideas

Offer to record a rejection (never record silently) whenever a `grill-me`,
`second-opinion`, or planning discussion reaches an explicit "skip this
concept"/"not doing this" verdict — the agent's own verdict or the user's,
and even when the user only declines to object rather than stating it
outright. This mirrors the Proactive capture rule above: the trigger is the
verdict itself, not a phrase the user has to say — in practice, a user
rarely announces "this is out of scope"; the agent reaches that verdict
during planning and the user just doesn't object.

Protocol: draft a concept slug, a short reason (written to a scratch file
per this file's Shell Command Safety section — never inlined into a
single-quoted shell string), and, if applicable, which backlog item the
concept came from. Offer it as one line — ``Record as out-of-scope?
`<concept-slug>` — <one-line reason>`` — and run `out-of-scope add` only on
confirmation. At most one offer per distinct concept; if declined, don't
re-offer it.

```bash
python3 ~/.claude/scripts/dev_status.py out-of-scope add <concept-slug> --reason-file <path> [--related-item <backlog-slug>]
python3 ~/.claude/scripts/dev_status.py out-of-scope list
python3 ~/.claude/scripts/dev_status.py out-of-scope show <concept-slug>
python3 ~/.claude/scripts/dev_status.py out-of-scope link <concept-slug> <backlog-slug>
python3 ~/.claude/scripts/dev_status.py out-of-scope unlink <concept-slug> <backlog-slug>
python3 ~/.claude/scripts/dev_status.py out-of-scope remove <concept-slug>
```

`out-of-scope` is unrelated to `reject <id> <feedback>` below — `reject`
sends an in-review backlog item back for rework; `out-of-scope` records an
idea declined outright, independent of any single item's review cycle.

To update, start, or complete an item — pass the integer directly to the script.
**Do not look up the slug in your context; the script resolves numbers internally.**

```bash
DEVSTATUS_AGENT=1 python3 ~/.claude/scripts/dev_status.py start <slug|N>
DEVSTATUS_AGENT=1 python3 ~/.claude/scripts/dev_status.py done <slug|N>
DEVSTATUS_AGENT=1 python3 ~/.claude/scripts/dev_status.py update <slug|N> '{"field": "value"}'
python3 ~/.claude/scripts/dev_status.py show <slug|N>
```

Once work is ready for review, submit it and let the review/approve/reject
cycle replace a direct `done`:

```bash
DEVSTATUS_AGENT=1 python3 ~/.claude/scripts/dev_status.py review <slug|N>
DEVSTATUS_AGENT=1 python3 ~/.claude/scripts/dev_status.py approve <slug|N>
DEVSTATUS_AGENT=1 python3 ~/.claude/scripts/dev_status.py reject <slug|N> "<feedback>"
```

When passing a numeric position (not a slug) to `start`/`done`/`update`/`block`/
`unblock`/`pending update`/`review`/`approve`/`reject`/`run`/`gate-set`/
`gate-pass`/`remove`/`rename` (rename's numeric position is its `old_slug`
argument), fetch the current rev first —
the `item-map:` line of `render` (or `# rev=N` of `list`/`show`) output — in the
same tool-call step immediately before the mutating call, and pass it as
`--if-rev <N>`. This pre-mutation `render` must run **without**
`DEVSTATUS_AGENT` (the mutating call itself still sets it) — the item-map is
the whole point of this step, and this call's output was never meant to be
shown to the user anyway. The script refuses (no write) if `--if-rev` is
missing or stale on a numeric call, so this is guidance for the fast path,
not the safety net — a numeric call that omits it fails loudly with a fresh
render printed for retry, it never silently mutates the wrong item.
Slug-based calls are exempt and need nothing extra.

When work is ready, submit it with `review`; once a reviewer approves it (or
you're working solo and are confident it's ready), use `approve` to mark it done.
`done` alone now refuses on an in-review item — go through the review cycle
(`approve` to complete, `reject <feedback>` to send back) rather than patching
`status` directly, which is also refused for in-review items.

An item whose implementation plan had judgment-call steps may also carry a
`gate` (set via `gate-set` when the plan was classified — see backlog-item.md
step 5). Both `done` and `approve` refuse when `gate.required` is true and
`gate.passed_at` isn't set — `show <id>` to see the unmet criteria, verify each
against the actual work (never reflexively), then `gate-pass <id>` with a
coverage payload citing evidence per criterion (`run <id> -- <command>`
records an executed command; `"manual:<note>"` covers genuinely-manual
checks) and retry. A bare `gate-pass` without coverage is refused.
`gate` can't be set via a raw `update` patch — always `gate-set`/`gate-pass`.

`start`/`done`/`update`/`review`/`approve`/`reject` already render the full dashboard as part of their own
stdout — after running one, display that stdout to the user instead of just
narrating a one-line confirmation. Run these with `DEVSTATUS_AGENT=1` (see
the example invocations above) so stdout is clean; dashboard.md's
misresolution-check protocol still applies (verify against the mutated
item's line in the displayed dashboard, since the old stderr echo is
suppressed under this env var).

If the item's work touched a real project repo (not this dotfiles repo) and
left actual file changes, offer to commit — and if the repo has a remote,
offer to push too — once the work is verified and ready. Offer, never commit
or push silently, same as every other git action in this file. Scope the
offered commit to the files this item actually touched, not a blanket
`git add -A` — especially relevant if the repo has other uncommitted changes
sitting alongside this item's work.

#### Plans and deliverables get a path on record

Whenever work produces a durable plan or deliverable artifact — a `grill.py`
plan, a plan reviewed via `/second-opinion`, a written design doc, or
similar — a backlog item must end up with `related_files` pointing at that
artifact's path:

- **No tracking item exists yet** — this is a proactive-capture trigger (see
  above): offer one, seeded with `related_files` pointing at the artifact
  path from the start.
- **An item already exists** — if `related_files` doesn't yet reference the
  artifact, update it to add the path.

The path goes in `related_files`; never the artifact's full content inlined
into `context`/`next_steps` or any other prose field meant for short
descriptions — those fields describe the work, they don't hold it.

#### Reading an item before starting work

`start` only renders the dashboard (one-line summaries). It does NOT surface the
item's actionable detail — `context`, `next_steps`, and `related_files` are what
you actually pick the work up from. So before beginning work on an item the user
names ("work on 4", "let's pick up the truncation item", etc.), run `show` on it
first and read the full record:

```bash
python3 ~/.claude/scripts/dev_status.py show <slug|N>
```

Then, with that context in hand, actually act on it — e.g. open the listed
`related_files`, re-read the cited code, and ground the next step in the stored
`next_steps`. Do not start writing or editing a task item from the dashboard's
one-line summary alone. The `start` call can happen in the same batch as the
`show`, or immediately after — the point is to have the full record loaded
before any work begins, not merely to have marked it in-progress.

If `show` returns no `context`/`next_steps`/`related_files` (empty fields), say
so and ask the user to fill them in before proceeding — don't fabricate a plan
from the summary title.

When writing to stored fields (`summary`, `context`, `next_steps`, `related_files[].note`)
and prose cross-references, use slugs for any item references — never raw hex IDs.

#### Starting work on a backlog item

Follow the Git section's worktree-first policy below: create a fresh
worktree scoped to the item's slug before touching the repo under
`related_files`, rather than branching in the main checkout.

`dev_status.py start` now enforces part of this itself, as a backstop: it
refuses to run from a main/master checkout of a git repository (exit 1,
with a message telling you to create a worktree first), unless you pass
`--allow-main`/`--no-worktree-check`. It also stamps a `claimed_by` marker
(harness, machine id, PID, timestamps) on the item and refuses `start` if
another live session already holds an active claim — pass `--force`/`-f`
to take over. A dead process or an expired claim (idle past
`DEVSTATUS_CLAIM_TTL_SECONDS`, default 2 hours) is taken over automatically,
no `--force` needed. `--claimed-by <harness>` overrides the auto-detected
harness name if it's ever wrong. The claim clears itself when the item
leaves `in-progress`. The dashboard's IN PROGRESS section shows the
claiming harness in brackets, e.g. `[claude]`.

#### Cross-machine sync

The backlog/pending store is per-machine by default. If the user wants it
reconciled with another machine's store, use
`python3 ~/.claude/scripts/dev_status_sync.py sync` (add `--dry-run` to
preview, or `status` to check divergence without merging) — a desktop-
initiated bidirectional merge over SSH. This is a manual, occasional
operation, not part of the normal add/update/done loop above. `sync` also
transfers the `~/.claude/data/grill/` artifact files referenced by items'
`related_files` (specs, grill plans, critique notes) via `rsync`, so those
references don't dangle on the other machine; pass `--no-artifacts` to skip,
and `--rsync-io-timeout` to bound each rsync call.

### Pending Items

When helping the user send an email/message that expects a reply, or take an
action that depends on someone else's response (e.g. requesting API/access
approval), offer explicitly — never add silently: "want me to track this as
a pending item?"

```bash
python3 ~/.claude/scripts/dev_status.py pending add '{"id": "<slug>", "description": "<what you are waiting on>", "kind": "<email|chat|approval>", "source_ref": {...}, "context": "<why>", "next_steps": ["..."]}'
```

Status moves one step at a time — `waiting_for_reply` → `reply_received` →
`resolved` — never jump straight to `resolved`. A reply arriving means it
needs a look, not that it's closed:

```bash
python3 ~/.claude/scripts/dev_status.py pending update <slug|N> '{"status": "reply_received"}'
python3 ~/.claude/scripts/dev_status.py pending update <slug|N> '{"status": "resolved", "outcome": "<what happened>"}'
```

Same proactive-capture discipline as the backlog section above: if you catch
yourself narrating a send-and-wait action as an aside instead of asking,
that phrasing IS the trigger — offer before you send, not after — and
re-scan your own draft response before sending it, the same pre-send check
used for backlog capture.

## Git

- Use conventional commits: `type(scope): description` — types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`, `perf`, `ci`
- Never commit directly to `main`/`master`, in any repo. Before starting new
  work — this dotfiles repo included, regardless of whether the checkout is
  currently clean or dirty, and even in solo sessions with no concurrent
  activity — create a fresh worktree for it rather than branching in the
  existing checkout:

  ```bash
  git -C <repo> worktree add ../<repo-name>-<slug> -b <slug>
  ```

  A fresh worktree has no installed dependencies — package managers install
  into an untracked local folder (`node_modules`, `.venv`, `target`, etc.),
  and worktrees don't share it. Install dependencies right after creating
  the worktree, before running tests, lint, or a dev/build command. Check
  the repo's manifest/lock file to find the right install command for its
  ecosystem.

  This sidesteps concurrent-session collisions by construction — repos
  routinely get worked from more than one tool in parallel against the same
  checkout. On a machine where `install.py` has run, a pre-tool guard
  enforces it: every harness refuses a write into a repo's main checkout
  while a backlog item for that repo is in progress, and warns when a
  worktree's base is behind `origin/main`. Only the user can switch it off,
  by setting `GUARD_RAILS_OFF=1` in the environment the harness is launched
  from — an agent's own `export` never reaches the hook. Elsewhere, this
  rule is on you.

  If you're already mid-task in a worktree this session created, keep
  working there; don't spin up a second one for the same task. Mention the
  worktree path when the work is done — it needs a manual merge or PR back
  into the main branch. Avoid repo-wide operations (a full reformat, a
  rename-everywhere refactor) that could conflict with whatever else is
  active in the repo.
- Committing itself always needs its own explicit confirmation — never
  commit, in any repo or worktree, without asking first and getting a yes,
  no exceptions for being mid-pipeline or in auto-mode.
- For the user's own personal projects only (this dotfiles repo, personal
  side projects under their own accounts — never a day-job/work repo, a
  `work-`-prefixed backlog item, a work-profile machine, or anything
  ambiguous): once a commit is in and the work is tested/verified, the
  follow-on sequence — merge to main locally, push to the remote, clean up
  (remove the worktree, delete the merged branch) — is what the user almost
  always wants next, so offer it as one bundled question ("merge to main,
  push, and clean up the worktree?") instead of asking separately at each
  step. For anything work-related, or when it's unclear which category a
  repo falls into, default to the safer path: keep merge and push as
  separate, individually-confirmed asks — never bundle.

## Test Hygiene

Never let tests touch real user state. Mock `subprocess.Popen`/
`subprocess.run` calls and any writes to `~/.claude/`, `~/.config/`, or
production data directories. When fixing a bug, add a regression test
proven to fail against the pre-fix code (run it red, then green) — a test
that can't be made to fail first isn't verifying anything.

## Shell Command Safety

- Before an `Edit`, re-read the exact target lines rather than retyping
  them from memory — prose files in this repo contain unicode dashes and
  smart quotes that look identical to ASCII but break literal string
  matching, causing avoidable failed-edit retries.
- Never put text that may contain an apostrophe (backlog titles/summaries,
  commit bodies, freeform notes) inside a single-quoted shell string — an
  apostrophe there terminates the quote early and breaks the command. Write
  the text to a heredoc or a temp file instead, and pass that to the
  command (`--body-file`, `< file`, or reading it back into the JSON payload
  before calling `dev_status.py`).
- Never pipe a long-running or destructive command (`install.sh --rollback`,
  a migration, anything that mutates real state) through `head`/`less`/`tail`
  to skim the output. A downstream reader closing early can SIGPIPE the
  writer mid-run and abort it partway through mutating state — this has
  already happened and cost live symlinks. Redirect to a file and read the
  file instead.
- On a machine running watchcommit (personal, non-work), wrap any command
  you run yourself that deliberately leaves a watched repo (`~/dotfiles`)
  in a broken or temporary state on purpose — a test/demo script proving a
  staleness check works, a deliberate mid-refactor pause, anything the user
  hasn't reviewed yet — in `wc-guard <command>` (`scripts/wc-guard`,
  installed to `~/.local/bin/wc-guard`). Without it, watchcommit's 90s poll
  can auto-commit and auto-push the broken state to `main` before anyone
  reviews it, with an LLM-written commit message that makes the breakage
  look intentional — this already happened once (commit `cd0bad8`, fixed
  forward in `60401d2`). A plain edit you intend to keep doesn't need the
  wrapper; this is specifically for state that is deliberately, temporarily
  wrong.

## Scripts

- Always use `#!/usr/bin/env <lang>` shebangs (e.g. `#!/usr/bin/env python3`, `#!/usr/bin/env zsh`)

## Python

### Tooling

Always use `uv`. Never use `pip`, `poetry`, or `virtualenv` directly.

```bash
uv sync                          # install / sync deps from pyproject.toml
uv add <package>                 # add a dependency
uv run python <script>           # run a script in the project venv
uv run pytest                    # run tests
```

Format and lint with `ruff`:

```bash
uv run ruff format .             # auto-format
uv run ruff check .              # lint
uv run ruff check --fix .        # lint + auto-fix
```

### Coding Standards

- **Python version**: 3.12+
- **Type hints**: required on all function signatures (args and return type)
- **Formatter**: `ruff format` (88-char line length)
- **Linter**: `ruff check` — fix all warnings before committing
- **Imports**: stdlib → third-party → local, one blank line between groups
- **No `Any`** unless genuinely unavoidable; prefer `object` or a Union
- **Prefer `pathlib.Path`** over `os.path` string manipulation
