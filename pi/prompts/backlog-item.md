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
conversation. This is a judgment call over enumerable options, so ask with
the `question` tool and state your recommendation first, exactly as steps 10
and 11 do. Pi ships no built-in question/select tool — `docs/usage.md` lists
only `read`, `bash`, `powershell`, `edit`, `write`, `grep`, `find`, `ls` —
but `question-tool.ts` in this repo supplies one, and it is loaded unless
the session was started with `-ne`. Falling back to plain text is correct
only in a session where that tool is genuinely absent:

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
herdr's pi integration reports as agent state `blocked` — asking in plain
text instead ends the turn like normal completion does, leaving this gate
indistinguishable from the agent simply finishing, to anything watching
over herdr's socket API (`--swarm` mode's relay, in particular).

`herdr-blocked-bridge.ts` is what raises that state, not the question tool
itself: it listens to pi's own `ui_prompt_start`/`ui_prompt_end` events, so
every blocking prompt reports `blocked` without each call site having to
remember to emit anything. The consequence worth knowing: a session started
with `-ne`/`--no-extensions` has no bridge and no herdr integration, so
nothing it does will ever report `blocked`.

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
tab, each running its own `/backlog-item --auto <slug>`. Requires
`HERDR_ENV=1` (this session must itself be running inside a herdr-managed
pane); if it isn't, say so and stop rather than falling back to `--auto`
silently. Full design: `~/.claude/data/grill/2026-09-01-pi-side-agent-swarm-orchestratio-plan.md`.

Queue selection is delegated to `swarm_spawn`. Pass it a `prefix` scoping
the run (`meta-` for tooling work, `iron-lb-` for that project, and so on)
rather than a fixed list of slugs: it re-reads the READY set from
`dev_status.py` on every call, so an item unblocked by a worker that just
finished is picked up on the next spawn without you naming it. A `prefix` is
required when you do not pass `items` — selecting from the whole READY queue
unscoped would pull unrelated projects into one run. `--swarm` never takes a
single-item target (see the invocation note above).

Uses the `swarm_spawn`, `swarm_poll`, and `swarm_resolve_blocked` tools
(`pi/extensions/swarm-tool.ts`) — never hand-compose `herdr` bash commands
for this; the tools own argv safety (a human's relay answer is never
shell-interpolated), state persistence across a crash/restart, and the
concurrency-cap accounting.

1. Pick a `runId` for this invocation (e.g. a short timestamp-based slug)
   and call `swarm_spawn` with the run's `prefix` and the concurrency — it
   spawns up to the cap, reporting any items skipped (cap), deferred (file
   overlap) or failed to spawn.

   **Deferred is not skipped.** Two items whose `related_files` name the same
   file are never spawned into the same wave: each worker gets its own
   worktree, so the second to merge would conflict. A deferred item is still
   owed and becomes schedulable once the worker it collided with finishes; a
   skipped one was only held back by the concurrency cap and is coming next
   wave regardless. Both are named in the tool's result text. Each worker's tab is created with `PI_AGENT_UNATTENDED=1` in its
   environment, so both gate extensions settle themselves at pi's module
   load, before the worker can be handed anything. Nothing is negotiated
   over the wire and there is no acknowledgement to wait for.

   The two gates read that variable and reach deliberately different
   conclusions. `permission-gate.ts` starts disabled: its ask tier is
   everything outside a narrow allowlist, and a worker that cannot run tests
   or git is useless. `guard-rails.ts` stays fully armed and instead
   **blocks** its two interactive confirmations — `rm -rf` and `sudo` are
   refused with a reason the worker can read, rather than raising a dialog
   nobody will answer. Every other guard-rails rule, including the
   protected-path writes and the git-commit-on-main worktree policy, applies
   to a worker exactly as it would to you.

   So a worker that tries `rm -rf` gets a clean refusal it can report,
   instead of stalling forever while `agent_status` still reads `working`
   and `swarm_poll` reads that as progress. If an item genuinely needs one of
   those commands, that is a human's job, not a thing to route around.

   Two spawn failures are worth telling apart:

   - **`agent_not_ready`** — the tab was created but pi never became ready
     for input in it. The tab's captured output is on the failure reason and
     usually says why.
   - **`agent_prompt_stalled`** — the item could not be delivered to a worker
     that was ready.

   For one worker, either means: report it, leave the item READY, and carry
   on with the rest of the batch. If **every** worker in the batch fails the
   same way, that is a herdr-level fault: stop the run and report, rather
   than spawning into the same fault repeatedly.

   Each failure names its reason in the tool's own result text, which is what
   the rule above keys off. The worker pane's captured output is recorded
   alongside it but is not in that text — read it back from the run's record
   when diagnosing, rather than expecting it inline. Every failure after a
   tab exists closes that tab, so a failed round leaves no orphan workers
   behind.

   Each worker gets its own herdr tab, not a slice of the orchestrator's
   pane. A tab's root pane is the full terminal size however many tabs are
   open, so the batch is never trimmed for want of width and the concurrency
   you ask for is the concurrency you get. Nothing is reported as too narrow
   any more; a spawn failure now always names a herdr-level or worker-level
   fault rather than the geometry.
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
     possible. Same reason as steps 10 and 11: raising a `question` puts this
     orchestrator's own pane into herdr's `blocked` state while it waits on
     the human — `herdr-blocked-bridge.ts` reports it from pi's own
     `ui_prompt_start`, so it holds for any blocking prompt, not just this
     tool. Asking in plain text ends the turn, which leaves the orchestrator
     reporting `idle` — indistinguishable from a
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
   - **`still_working`** — a check-in, **not an outcome**. The worker's wait
     window elapsed, it was confirmed alive and still inside its working-time
     budget, and a fresh wait is already armed. Three things follow, and each
     is a separate way to get this wrong: it frees **no** concurrency slot,
     so it is never a cue to call `swarm_spawn`; its item stays in the active
     working set rather than the accounted-for one; and it is never a row in
     the end-of-run summary. The event names its check-in number and the
     working time so far against the budget (e.g. "check-in 7, 3h31m of a 4h
     budget") — relay that to the user when it is worth their attention, then
     simply poll again. An elapsed wait against a live worker used to close
     its tab and drop it; it now produces this and nothing else.
   - **`finished`** / **`timed_out`** / **`error`** — record the outcome for
     the end-of-run digest (approved / flagged / stopped on budget / failed);
     all three have already closed that worker's tab and freed its slot, and
     none is ever silently retried. A freed slot is the cue to call
     `swarm_spawn` again with the same `runId` and `prefix`: that re-reads
     READY, so a worker that just unblocked two items causes those items to
     be picked up without anyone naming them. The three are deliberately
     different and must not be reported as one:
     - `timed_out` means the worker exceeded its whole-item **working-time
       budget** and was stopped deliberately — not that a wait deadline
       elapsed, which is now merely a check-in. Its item is probably still
       `in-progress` with a live claim and its worktree survives on disk, so
       the event carries the worktree path and the recovery commands: relay
       that detail verbatim rather than reporting the worker as having
       misbehaved. The detail also says whether the worker's liveness was
       **confirmed** before it was stopped, or whether the liveness probe
       failed and the stop rests on the budget alone — never report the
       second as though it were the first.
     - `error` means the agent is positively gone (herdr reported
       `agent_not_found`), it crashed, or the wait itself failed, and carries
       the raw reason after the kind. A *transient* failure of the liveness
       check is not an error and never appears here: it re-arms, because
       killing a healthy worker on an inconclusive signal is the defect this
       tool was fixed for.

     **`swarm_poll` does not spawn anything.** It arms waits, drains events
     and closes finished workers' tabs; that is all. When an event frees a
     slot and the READY queue still has items, call `swarm_spawn` again
     yourself for the next batch. A run that skips this processes only the
     first N items of its queue and then stops, leaving the rest silently
     unaccounted for.

     A `swarm_poll` that returns saying it was **aborted** is not an outcome
     for the digest: its workers were left untouched and are still running,
     so poll again rather than treating the run as finished.
3. Repeat steps 1 and 2 — spawn, poll, spawn again — until `swarm_spawn`
   reports nothing left to spawn, `swarm_poll` reports no active workers, and
   the digest accounts for the whole queue. A spawn that returns zero
   spawned while naming deferred items is not the end of the run: poll the
   workers still running, then spawn again once one of them finishes. "No active workers" on its own is
   not the end of the run: when workers are still parked awaiting a relay,
   `swarm_poll` names them and the answer each is waiting on is still owed —
   resolve those with `swarm_resolve_blocked` before treating the queue as
   drained, or the run ends with a live worker holding an open tab.
4. **End of run** — same shape as `--auto`'s: a dashboard-style summary of
   every item (done, flagged, stopped on budget, failed), then walk any accumulated
   proactive-capture digest entries exactly as `--auto`'s own end-of-run
   step does.

Steps 10 and 11's live-approval requirement is never bypassed in this mode
— it is *how* the blocked-event relay above works, not an exception to it.
