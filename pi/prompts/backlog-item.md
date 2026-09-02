---
description: "Runs a dev_status.py backlog item end-to-end: resolve, worktree, spec (escalating to grill-me only for a genuinely open design branch), second-opinion critique, execution handoff, TDD implement, verify, commit/merge/push gates, review+approve. Use when the user says 'work on backlog item 4', 'pick up <slug>', 'let's do the next backlog item', or otherwise names a specific item to work end-to-end. Add --auto (optionally with a slug) for an unattended single-item or full-READY-batch run — commit and merge/push gates still stop live, per item. Add --swarm[=N] to fan the full-READY-batch run out across N (default 3) concurrent recursive pi workers via herdr, instead of running the queue one item at a time -- requires HERDR_ENV=1."
argument-hint: [--auto] [--swarm[=N]] [slug|N]
---

Work the named item to done, one step at a time. `$ARGUMENTS` holds the
invocation: strip a leading `--auto` or `--swarm`/`--swarm=N` token if
present (note which one was given) — what remains is the target item, a
slug or an integer N. If `--auto` was given, skip straight to the `--auto
mode` section at the end of this file instead of running the numbered steps
live. If `--swarm`/`--swarm=N` was given, skip straight to the `--swarm[=N]
mode` section instead — it does not take a single-item target; `--swarm
<slug>` is a usage error, ask the user whether they meant `--auto <slug>`.
Otherwise, if the remaining target is empty, ask the user which item — never
guess. Every
user-approval gate below (`## 10`, `## 11`) stops and waits for the user —
never collapse two gates into one approval. Distinct from those: the item's
own `gate` field in `dev_status.py` (step 5, step 12) is a judgment-step
verification checkpoint, not a user-approval stop — same word, different
mechanism, don't conflate them.

## 1. Resolve
Call the `dev_status` tool with `action: "show", slug: "$ARGUMENTS"` (works
whether `$ARGUMENTS` is a real slug or a numeric position — `show` is
read-only). Its response's `id` field is this item's real slug — use that
resolved slug for every remaining step below, never the raw `$ARGUMENTS`
again (every mutating `dev_status` action refuses a numeric slug outright).
Read the full record — never start from the dashboard's one-line summary
(CLAUDE.md). Empty context/next_steps/related_files: stop and ask the user
to fill them in; don't fabricate a plan from the title. related_files
already names a grill plan (`~/.claude/data/grill/<slug>-plan.md`) or a
spec (`~/.claude/data/grill/<slug>-spec.md`)? Planning and critique (steps
5–6) are already done — skip to step 8. Worktree already has implemented,
uncommitted changes (e.g. handed back from an external executor)? Skip
straight to step 9.

## 2. Start
If not already in-progress: call the tool with `action: "start", slug:
"<resolved slug>"`.

## 3. Branch
related_files names exactly one project repo → worktree it per CLAUDE.md's
Git section: `git -C <repo> worktree add ../<repo-name>-<slug> -b <slug>`.
Reuse a worktree this session already made for this item instead of a
second one. Multiple repos, or none: ask which repo — never guess.

## 4. Baseline
Run that repo's test suite (or the most relevant targeted subset) in the
fresh worktree before touching anything (CLAUDE.md's "Baseline tests before
starting code work").

## 5. Spec or plan
Load the `spec` skill via `/skill:spec` with the item's context/next_steps
as the task. Let it draft and save the spec end-to-end (its steps 1–4) —
including its own internal escalation to `grill-me` if a field's design is
genuinely open; `spec`'s step 3 owns that handoff and the resume-after
entirely, there is nothing to orchestrate here. Decline spec's own step 4
generation offer — step 7 below owns the handoff decision.

Once spec records its artifact path, add it to the item's related_files if
missing (CLAUDE.md's "Plans and deliverables get a path on record"). If
`spec` delegated into `grill-me` along the way, that session's `plan_path`
is already cited from the spec's Context field — don't also record it as a
second, competing artifact.

Once spec is saved, add its path to the item's related_files if missing
(CLAUDE.md's "Plans and deliverables get a path on record").

### Architecture capture (new modules only)

If the artifact's related_files point into a module with no
`docs/architecture/{module-slug}.md` yet in the target repo — a genuinely
new module, not routine work in an existing one; judgment call, ask if
unclear — run one more pass before continuing: draft a 300–600 word doc
covering the module's boundary/responsibility (one paragraph), key
interfaces it exposes or consumes, explicit non-goals, and known
unknowns/deferred decisions. Save it to `docs/architecture/{module-slug}.md`
in the target repo (project documentation, not `~/.claude/data/grill/`) and
reference it from the spec/plan. A later item touching the same module cites
the existing doc instead of repeating this pass — check for it first.

### Gate classification

Once the spec/plan is recorded (and after architecture capture, if it ran),
classify its concrete implementation steps: **mechanical** (a rote,
unambiguous transformation — no interpretation needed) or **judgment**
(interprets a requirement, makes a design choice, or has ambiguous
acceptance criteria). If any step is judgment, set the item's gate before
continuing:

Call the tool with `action: "gate_set", slug: "<resolved slug>", patch:
{"required": true, "criteria": ["<short imperative criterion per judgment
step>", "..."]}`.

If every step is mechanical, leave the gate unset (inert by default) —
don't call `gate-set` for a step breakdown with no judgment calls in it.

## 6. Critique
If step 5 set a gate (judgment steps present), run the `second-opinion`
skill (`/skill:second-opinion`) against the resulting plan or spec file
unconditionally, no ask — critique it before committing to an executor. If
step 5 left the gate unset (all steps mechanical), skip this step; a
critique adds nothing to a rote transformation.

## 7. Handoff
Decide who implements the plan — ask if it isn't already obvious from the
conversation, in plain conversational text with a stated recommendation (Pi
has no built-in question/select tool — `docs/usage.md` lists only `read`,
`bash`, `powershell`, `edit`, `write`, `grep`, `find`, `ls` among its
built-ins — so state the options, recommend one, then stop and wait for an
actual reply):

- **Same session, now.** Trivial/small item → go to step 8 immediately.
- **Fresh Pi session.** Use the `grill` tool's `mark_pending_execution`
  action with `backlogSlug` set to this item's slug (the plan's session,
  with this item's slug — not its own resolved-topic slug) and tell the user to start a fresh session
  and type `/backlog-item <slug|N>`. Pi supports a `session_start`
  extension event (`docs/extensions.md`), but this repo doesn't ship an
  extension hooked to it to auto-surface the marked plan, so the typed
  command IS the resume path — step 1 sees the plan in related_files and
  skips to step 8.
- **Different/cheaper model, or another harness.** Confirm the model
  actually exists first — `pi --list-models <search>` (documented in
  `docs/usage.md`), not a guess at the id. Then use the `delegate` tool
  with `cwd` set to the worktree, a `prompt` along the lines of "Implement
  <plan path> exactly as written — TDD, run the full suite, then STOP
  without committing and report the diff", and the `harness`/`model` you
  settled on. Never hand-compose an `opencode run` / `agy -p` / `pi -p`
  bash command: `delegate` keeps the child's transcript out of this
  session's context and gets the per-harness invocation right, including
  two forms that fail silently by hand (opencode's `-p` is `--password`,
  and a bare `agy -p` swallows the following flag as its prompt).
  Set `autoApprove: true` for opencode or agy — without it opencode makes
  no progress headless. Pi needs no such flag: it has no built-in
  permission-prompt system (`docs/usage.md`'s Design Principles), so tool
  calls execute freely in `-p` mode, gated only by whatever this repo's
  `permission-gate.ts` denies (see `pi/CLAUDE_CODE_PARITY.md`).
  opencode is for personal projects only — never a work-related item. An
  external executor never gets the commit gate. Once it reports back,
  review — that resumes at step 9.

## 8. Red, green
TDD in the worktree: a failing test that proves the gap the plan names,
then the minimal implementation.

## 9. Verify
Run the full suite (and lint, if present) in the worktree and show the
output — "should work" is not verification (CLAUDE.md).

## 10. Gate: commit
Show the full diff. Stop — use the `question` tool for explicit commit
approval, recommended option first (e.g. "Yes, commit (Recommended)" / "No,
don't commit"), per CLAUDE.md's judgment-call convention. No exceptions for
being mid-pipeline, and no exception for code an external executor wrote
(CLAUDE.md). Use `question`, not plain text: its interactive prompt is what
herdr's pi integration reports as agent state `blocked`
(`question-tool.ts` emits `herdr:blocked` around it) — asking in plain text
instead ends the turn like normal completion does, leaving this gate
indistinguishable from the agent simply finishing, to anything watching
over herdr's socket API (`--swarm` mode's relay, in particular).

## 11. Gate: commit-then-land
On approval, commit (conventional format) — this gate is never bundled with
what follows. Personal project (this repo, a personal side project — never
a `work-`-prefixed item or a work repo): offer the follow-on sequence as one
bundled question via the `question` tool (CLAUDE.md's Git section) — "merge
to main, push, and clean up the worktree?" — then merge locally, push,
`git worktree remove`, `git branch -d` on that single approval. Work-related
or ambiguous: ask separately for merge and for push via the `question`
tool — never bundle. Same reason as step 10: `question`, not plain text, so
this gate registers as `blocked`, not indistinguishable from done.

**`git worktree remove` fails with "Directory not empty"?** A dev server
(or other long-running process) launched against this worktree during step
9 — e.g. via the `run` skill's smoke-check pattern — can outlive the port
kill that pattern documents: killing the port's listener doesn't always
reap a wrapper's child process (a `bun run dev` parent whose `node .../next
dev` child keeps the worktree as its cwd). `git worktree remove` then fails
partway, and can strip the worktree's git-admin metadata (it drops out of
`git worktree list`) while leaving the directory on disk — a second
`remove` won't find it. Find what's still holding it open with `lsof +D
<worktree-path>`, `kill` those exact PIDs (never a broad `pkill -f` — it
can match unrelated processes, including the agent's own), then remove the
orphaned directory directly (`rm -rf <worktree-path>`) and retry
`git branch -d`.

## 12. Close
Call the tool with `action: "review", slug: "<resolved slug>"`, then
`action: "approve", slug: "<resolved slug>"` — never a bare `done` on an
in-review item. If `approve` refuses citing an unmet gate, actually check
each criterion from `show`'s record against the diff — don't pass it
reflexively — then cover every criterion with evidence (`action: "run"`
executes and records a command; `action: "gate_pass"` takes a `patch`
`{"coverage": {"<N>": "run:<run_id>" or "manual:<note>"}}` and refuses
until each criterion cites a recorded run or a manual note) and retry
`approve`. Display the full dashboard text these return; don't just
narrate a one-line confirmation.

If the `dev_status` tool is genuinely unavailable at any of the steps
above, fall back to the equivalent bash `dev_status.py` command named in
this repo's other harness prompts (`show`/`start`/`gate-set`/`review`/
`approve`/`gate-pass <slug|N> [--if-rev <N>]`) — in that fallback path
only, a numeric id needs a fresh, non-quiet `render` immediately before
each mutating call to read the current rev for `--if-rev` (CLAUDE.md's
Backlog section).

---

## `--auto` mode

Runs the per-item procedure above end to end with minimal live input — the
user has explicitly asked for unattended execution. Steps 10 (commit) and 11
(merge/push/cleanup) always stay live, per item, no exception: CLAUDE.md's
commit-approval rule holds even mid-pipeline. Steps not called out below run
exactly as written above.

**Invocation.** A slug/N present after stripping `--auto` runs just that
item under this mode. No slug batch-processes every READY item, in
dashboard order; any IN PROGRESS item is resumed first via the existing
step 1–2 logic; BLOCKED items are skipped by construction (never READY).
The queue is fixed at the start of the run — items added to READY mid-run
aren't picked up until a later invocation. Loop the modified per-item
procedure below across the queue.

1. **Step 3 (Branch)** — repo ambiguity (multiple named, or none) can't be
   guessed. Skip the item, queue an end-of-run digest entry noting why, and
   continue the batch.
2. **Step 4 (Baseline)** — a truly trivial failing baseline (a one-liner,
   no investigation needed) still folds in per CLAUDE.md's existing
   exception. Anything needing real investigation: draft the backlog-item
   `add` JSON for it, queue it in the digest (never add silently), skip
   implementing on top of a broken baseline, and move to the next item.
3. **Step 5 (Spec or plan)** — when loading the `spec` skill
   (`/skill:spec`), state explicitly in the task text that this
   backlog-item run is `--auto`: if spec's own step 3 escalates into
   `grill-me` for a genuinely open design branch, tell it to run that inner
   session as `grill-me --auto` too, rather than stopping for live Q&A.
4. **Step 6 (Critique)** — no ask either way, per the updated step 6 rule:
   runs unconditionally when a gate was set, skipped when the item is
   all-mechanical.
5. **Step 7 (Handoff)** — always resolves to "same session, now," no ask —
   no dispatch to a fresh session or a different model.
6. **Steps 8–9 (Red/green, Verify)** — on failure, retry up to 2 times
   before giving up. Still failing: skip the item, queue a digest entry
   describing the failure, and continue the batch.
7. **Proactive-capture protocols** — every "offer, never do silently"
   trigger from CLAUDE.md that would otherwise fire mid-run (baseline-
   failure backlog offers, proactive backlog capture, pending-item
   tracking, rejected-idea capture) queues into the digest instead.
8. **Steps 10–11** — unchanged, always live, per item, exactly as written
   above.
9. **Step 12 (Close)** — unchanged; `review`/`approve`/`gate-pass` is
   already agent-performed self-verification against stored gate criteria,
   not a user-facing ask.

**End of run.** When the queue is exhausted (or the single item completes),
show a dashboard-style summary of every item processed — done, skipped
(with reason), or failed after retries — then walk the accumulated digest
in one pass, asking in plain text for each queued item exactly as its
originating CLAUDE.md protocol specifies (a backlog `add`, a
`pending add`, an `out-of-scope add`), stating a recommendation first and
confirming or declining each in turn.

---

## `--swarm[=N]` mode

Runs the READY queue concurrently instead of one item at a time — `N`
recursive pi workers (default 3, from `--swarm=N`), each in its own herdr
pane, each running its own `/backlog-item --auto <slug>`. Requires
`HERDR_ENV=1` (this session must itself be running inside a herdr-managed
pane); if it isn't, say so and stop rather than falling back to `--auto`
silently. Full design: `~/.claude/data/grill/2026-09-01-pi-side-agent-swarm-orchestratio-plan.md`.

Queue selection is identical to `--auto`'s no-slug batch mode: every READY
item, in dashboard order, fixed at the start of the run. `--swarm` never
takes a single-item target (see the invocation note above).

Uses the `swarm_spawn`, `swarm_poll`, and `swarm_resolve_blocked` tools
(`pi/extensions/swarm-tool.ts`) — never hand-compose `herdr` bash commands
for this; the tools own argv safety (a human's relay answer is never
shell-interpolated), state persistence across a crash/restart, and the
concurrency/pane-cap accounting.

1. Pick a `runId` for this invocation (e.g. a short timestamp-based slug)
   and call `swarm_spawn` with the full READY queue and the concurrency —
   it spawns up to the cap, reporting any items skipped (cap) or failed to
   spawn. Each worker is sent `/permission-gate-disable` before its item, so
   `permission-gate.ts`'s bash confirmation can't strand it: a worker runs
   in a TUI pane, so the gate's `ctx.ui.confirm` would wait on a human
   nobody has told to look, while `agent_status` stays `working` and
   `swarm_poll` reads it as progress. `guard-rails.ts` deliberately stays
   armed — workers still cannot write into a repo's main checkout.

   The gate is confirmed by the worker's own acknowledgement file, not by
   reading its terminal: `swarm_spawn` mints a per-worker token, passes it
   to the command, and polls for the file the worker writes under
   `permission-gate.ts`'s own ack directory. A terminal read cannot answer
   this — herdr's `recent` source is a bounded window of rendered rows, so a
   redraw or a stale notice from an earlier command produces a false
   confirmation or a false failure. Two spawn failures follow from it, and
   they mean different things:

   - **`permission_gate_not_disabled`** — the prompt was delivered but no
     acknowledgement arrived within the deadline. Report the item in the
     digest, leave it READY, and do not re-spawn it within the same call.
   - **`agent_prompt_failed`** — the prompt could not be delivered at all.
     For one worker, report it and carry on with the rest of the batch. If
     **every** worker in the batch fails this way, that is a herdr-level
     fault: stop the run and report, rather than spawning into the same
     fault repeatedly.

   Each failure names its reason in the tool's own result text, which is what
   the two rules above key off. The worker pane's captured output is recorded
   alongside it but is not in that text — read it back from the run's record
   when diagnosing, rather than expecting it inline. Every failure after a
   pane exists closes that pane, so a failed round leaves no orphan panes to
   subdivide the layout for the next one.

   Worker panes are carved from the orchestrator's own pane in equal shares,
   so N workers each get roughly a (N+1)th of it rather than the half-of-a-half
   that repeated splitting used to produce. When even shares would leave panes
   too cramped to read a diff in, the batch is trimmed to what fits and the
   remainder is reported as `pane_too_narrow`, naming the pane size and the
   split count. That is a property of the geometry, not of the item: re-spawning
   it into the same pane will fail identically, so report it, leave the item
   READY, and either widen the pane or run the swarm from a full-width tab
   before trying again.
2. Loop: call `swarm_poll`. It blocks until at least one worker settles and
   returns every event that settled in that window (usually one,
   occasionally more — process all of them before polling again):
   - **`blocked`** — a worker hit an approval gate (almost always step 10
     commit or step 11 merge/push, but treat the quoted prompt as whatever
     it actually says, never assumed to be a diff or a yes/no). `swarm_poll`
     reports that prompt verbatim in its own result text; show it to the
     user unchanged, alongside the item's slug, and ask
     for their answer **via the `question` tool, not plain text**, stating
     your own recommendation first per CLAUDE.md's judgment-call convention
     when one is warranted (e.g. recommending approval when the diff looks
     clean). Mirror the worker's own listed options where it has them, and
     keep a free-text escape so an answer that matches nothing is still
     possible. Same reason as steps 10 and 11: `question` emits
     `herdr:blocked`, so this orchestrator's own pane registers as `blocked`
     while it waits on the human. Asking in plain text ends the turn, which
     leaves the orchestrator reporting `idle` — indistinguishable from a
     finished run to anything watching over herdr's socket, including the
     user, who then has to hunt through panes to discover a relay is even
     pending (confirmed live, 2026-09-02: a relayed commit gate sat unseen
     behind an `idle` orchestrator). But this is still a
     live commit/merge approval, not a mechanical judgment call: never
     answer on the user's behalf, no exceptions, exactly as step 10/11 above
     require outside swarm mode. Once they answer, call
     `swarm_resolve_blocked` with that exact text — never a summary or
     paraphrase; it matches the text against the worker's currently listed
     option labels and navigates to the match. Every outcome it reports
     leads with its own marker word — `resolved:`, `needs_manual:` or
     `relay_failed:` — and names the agent, slug and pane, so branch on that
     word rather than on any field you expect to find in a result object. On
     `needs_manual:` (the answer matched no listed option, or matched more
     than one), relay it back to the user verbatim — the exact option labels
     it listed and the suggestion to attach directly
     (`herdr agent attach <id>`) — rather than guessing or retrying with a
     rephrased answer yourself. Handle one blocked event fully (through to
     calling `swarm_resolve_blocked`, or surfacing `needs_manual:`) before
     moving to the next event in the same batch; never stack multiple relay
     questions into one message.
   - **`finished`** / **`timed_out`** — record the outcome for the
     end-of-run digest (approved / flagged / timed out); the tool has
     already closed that worker's pane and freed its slot. `swarm_poll`
     itself spawns the next READY item into a new pane when there's queue
     left and the cap has headroom — no separate `swarm_spawn` call needed
     mid-run.
3. Repeat step 2 until `swarm_poll` reports no active workers and the
   digest accounts for the whole queue. "No active workers" on its own is
   not the end of the run: when workers are still parked awaiting a relay,
   `swarm_poll` names them and the answer each is waiting on is still owed —
   resolve those with `swarm_resolve_blocked` before treating the queue as
   drained, or the run ends with a live worker holding an open pane.
4. **End of run** — same shape as `--auto`'s: a dashboard-style summary of
   every item (done, flagged, timed out), then walk any accumulated
   proactive-capture digest entries exactly as `--auto`'s own end-of-run
   step does.

Steps 10 and 11's live-approval requirement is never bypassed in this mode
— it is *how* the blocked-event relay above works, not an exception to it.
