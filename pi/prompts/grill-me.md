---
description: "Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions 'grill me'."
argument-hint: [--verify | --auto] [topic or plan to grill on]
---

All session state lives in JSON under `~/.claude/data/grill/`, mutated only through
the `grill` tool — never via `grill.py` in bash, and never by writing or editing
session files by hand. The plan document is a separate markdown artifact
**you author yourself**, informed by the recorded decision points.

The tool's actions map one-to-one onto the session lifecycle. `new` starts a
session from a `payload` of `{topic}` and prints its slug. `ask` registers an open
decision point from `{id, question, reasoning?, depends_on?}`, and `decide`
resolves one from `{id, decision, question?, source?, depends_on?}`. `revise`
amends a decision by `decisionId`, resetting its verdict; `verdict` records a
verification result from `{result, evidence}`; `rm` removes a decision point.
`plan` records the path of the artifact you already wrote. For reading:
`next` gives the resume point, `frontier` the currently-askable batch, `render`
the status table, `show` the raw JSON, and `list` every session.

All actions default to the most recent session; pass `session` with a slug or
unique substring otherwise. Mutations echo what they did — check it matches
your intent.

**Pin the session.** The moment you have a slug — from `new`'s stdout, or from
`list`/`next` when resuming — pass `--session <slug>` on every subsequent
`ask`/`decide`/`revise`/`verdict` call for the rest of the conversation. Never rely
on the default-most-recent resolution past that first call: a concurrent grill
session elsewhere (another terminal, another agent — e.g. Claude Code running in
parallel) can become "most recent" between calls and silently redirect a bare
call at the wrong session's decisions. This has actually happened — a `decide`
meant for one session landed on a different one that happened to be more
recently touched.

## Default mode

If $ARGUMENTS is empty (and contains neither `--verify` nor `--auto`), grill the plan under discussion when the conversation makes it obvious; otherwise ask the user what to grill before proceeding. `--verify` and `--auto` each run their own section below instead of this Q&A loop.

**Pre-step: orient before asking.**
Check what codebase context is already in scope from prior exploration. Do a targeted read/grep only for files directly relevant to the topic that haven't been read yet. Don't re-crawl what's already known. Never ask a question the codebase already answers.

Also check the vitals store for already-settled facts on this topic before asking about it: read `~/.claude/data/grill/vitals/_global.json` (and `~/.claude/data/grill/vitals/<backlog-slug>.json` too, if this session is tied to a backlog item and that file exists) and skim `text`/`reasoning` fields for anything relevant — no keyword filter, judge relevance directly. Treat a matching record as a settled fact: don't re-ask a question it already answers, and cite it (`source_slug`/`source_decision_id`) if it informs a new decision. Skip this check entirely if the vitals directory doesn't exist yet — nothing has been promoted.

Then use the `grill` tool's `list` action to find an existing session matching the topic. If one matches, resume it — action `next` picks up at the first open question; tell the user you're resuming. Only use action `new` when nothing matches or the user says "start over".

**Q&A loop:**

1. Identify the top-level decisions and unknowns. Register each one immediately with `ask` (id + question) so nothing is lost if the session is cut short. When one decision genuinely can't be answered before another is settled, register that dependency explicitly via `ask`'s `depends_on` field (a list of the ids it waits on) — this is what `frontier` (step 2) uses to compute the safe-to-ask batch, instead of you having to judge ordering yourself. New decision points surfaced by later answers get `ask`ed as they appear, with `depends_on` set the same way whenever they depend on something still open.

2. Use the `grill` tool's `frontier` action to get the batch of currently-askable questions — every open decision whose `depends_on` ids are all resolved (decided, or never registered) — and ask all of them within the same turn rather than revealing one, waiting, then revealing the next. For each question in the round: when the plausible answers are enumerable (2–4 real options) or the question is genuinely open-ended, state it in plain text with your recommended answer and brief reasoning — Pi's built-in tools are `read`, `bash`, `powershell`, `edit`, `write`, `grep`, `find`, `ls` (`docs/usage.md`), with no built-in structured question/select tool among them, so don't design this step around one. A question whose answer depends on another still-open question won't appear until a later round — `frontier` handles that, you don't need to track it yourself. Wait for the user's response to the whole round before opening the next one.

3. When the user answers a round (they may answer several questions in one message), handle each answered question the same way:
   - If the answer is consistent and resolves the question, record it — `decide` with source `user`.
   - If the answer is vague, push back and ask them to be specific.
   - If the answer introduces inconsistencies or new risks, name them and keep drilling.
   - If the user defers ("whatever you think", "you decide"), record your recommendation via `decide` with source `defaulted`.
   - If you can settle the question yourself by running actual code (a scoped script, existing tests, a REPL check) rather than asking, do that first and record it — `decide` with source `tested`, noting what you ran and observed in the reasoning field. Cite the specific file, function, constant, or line checked — not just that a check happened. This is distinct from `user` (the person didn't state it) and from `--verify` mode (which re-checks decisions after the fact) — it's confirming a decision inline, during the Q&A loop itself. Once `decide` is recorded, immediately follow with action `verdict` on the same `decisionId`, payload `{"result": "VERIFIED", "evidence": "..."}`, using the same observation just gathered — don't leave the check recorded only in reasoning; the evidence string should restate what was actually run or checked, not just repeat the decision.

4. A branch is resolved when the answer generates no new questions. Newly surfaced questions get `ask`ed immediately (step 1) and join the next round's frontier once their own dependencies clear. Keep opening rounds until every open question is decided. Two distinct early exits — the user's words pick which:
   - **Pause** ("let's stop here", "I need to step away", "we'll come back to this", or a forced interruption cutting the session short) — stop without deciding anything. Open questions stay open for a later resume; no plan is written. Offer a backlog item with session slug in `context`, `next_steps` pointing at the `grill` tool's `next` action.
   - **Wrap up** ("wrap it up", "just finish it", "that's enough") — `decide` each remaining open question with your best-guess answer and source `assumed`, then conclude normally.

**End of session** (fully decided or wrapped up — not on pause):

1. Author the plan as a markdown document — a real plan someone could execute, not a decision log. The recorded decision points (the `grill` tool's `show` action) inform it. Plans always live centrally at `~/.claude/data/grill/<slug>-plan.md` — never in project repos; this is personal tooling, not team-facing docs. Never `mkdir -p` that directory first — `grill.py` and `second_opinion.py` each create it on every invocation, so just write the file.
2. Call the `vitals_promotion` tool with action `run` and `apply: true` — never `vitals_promotion.py` via bash (mechanical, no other fields needed — this re-runs the full classify/promote/supersede pass over every session, not just this one, so it also catches drift from sessions closed since the last run). Show the printed report (promoted/superseded/needs-review counts) to the user in plain text; if `promoted_count` or `superseded_count` is nonzero, this session's activity changed the vitals store the next session's pre-step will read.
3. Record it: the `grill` tool, action `plan`, with the artifact's `path`.
4. Show the user the plan and the `grill` tool's `render` output (decision table, any open questions, verification state).
5. Check whether any decided item has no recorded verdict, regardless of source. Tally the no-verdict set by source — `defaulted`/`assumed` (nobody confirmed these) vs `user` (a human stated it, but the claim itself was never checked) vs `tested` (rare here, since step 3 already auto-verdicts these inline) — and offer with the breakdown visible, in plain text with a recommendation: "N decision(s) have no recorded verdict — X defaulted/assumed, Y user-stated — want me to run --verify on them before we call this done?" Proceed into `--verify` mode only on a yes; otherwise the session ends here as-is.
6. Once verification (if any) is settled, always offer clear-and-go, in plain text with a recommendation: "Clear context and start executing this plan now?" On yes: use the `grill` tool's `mark_pending_execution` action (defaults to this session), then tell the user in plain text: "Marked — start a fresh session whenever you're ready and ask me to pick the plan back up." Pi supports a `session_start` extension event (`docs/extensions.md`), but no extension hooked to it ships in this repo (only the permission-gate and ruff-format extensions are ported — see `pi/CLAUDE_CODE_PARITY.md`), so resume is manual: when a session opens with the user asking to resume/execute the marked plan, use the `grill` tool's `pending_plan` action with `consume: true` and act on the printed instructions (resume if the user says go/continue, otherwise leave the cleared flag alone). On no, nothing else happens, no state change.

---

## `--verify` mode

Run this after a default-mode session. It does not ask new questions — it tests each decision against reality.

1. Load state with the `grill` tool's `render` action (most recent session by default, `session` otherwise). Do not ask the user to re-state decisions. Decisions still open must be decided in a default-mode pass first.

2. For each decided row — `defaulted` and `assumed` first, since no human confirmed those — design and run a non-destructive experiment to test whether the decision holds: run relevant tests, write and execute a scoped throwaway script, invoke existing CLI/dev tools. Observe actual behavior. (Bash isn't pre-approved here; permission prompts for experiments are expected.) `tested` rows are lowest priority — they already carry inline run-time evidence — but are still fair game for a spot re-check.

3. Record each result with the `grill` tool, action `verdict`, payload `{"result": ..., "evidence": ...}`:
   - **VERIFIED** — experiment confirms the decision holds. Evidence: what was run, what was observed.
   - **DISPUTED** — experiment contradicts it. Evidence: exactly what was found and why it conflicts.
   - **UNVERIFIABLE** — no experiment can test this yet (e.g. code doesn't exist). State why.

4. For any DISPUTED decision: re-grill the user, record the new answer with action `revise` on that `decisionId` (this resets the verdict), then re-run the experiment and record a fresh verdict. Repeat until nothing is DISPUTED.

5. If any decisions changed, update the plan artifact to match, then show the final `render` output.

---

## `--auto` mode

For grilling a topic (or a batch of backlog items) with no live user Q&A — the
user has explicitly asked for autonomous resolution, e.g. "grill the rest of the
backlog on your own." Every decision in this mode is unconfirmed by a human, so
it trades interactivity for adversarial rigor instead of just guessing.

1. Same pre-step and `ask`-registration as default mode — identify and register
   every decision point up front.

2. For each open question, instead of asking the user: form your own leading
   answer, then run the `second-opinion` skill's iteration loop (same
   convergence rule and round cap — reuse that backend and loop rather than
   inventing a separate critique mechanism) against a short write-up of the
   question, your answer, and enough surrounding context for an outside
   model to attack it credibly. Pi's own design principles rule out
   built-in sub-agents (`docs/usage.md`'s "Design Principles": "it
   intentionally does not include built-in MCP, sub-agents, permission
   popups, plan mode, to-dos, or background bash") — routing every
   `--auto` round through `second_opinion.py` is the only path here, not a
   fallback from a preferred native one. "Adversarial" here means exactly what
   `second_opinion.py`'s own `CRITIQUE_PROMPT` asks for regardless — find
   problems rather than summarize or agree, name what's underspecified or
   assumed without justification, disagree explicitly where warranted, and
   propose a simpler approach if one exists; a critique that just restates or
   praises your answer isn't adversarial and doesn't count as a round.
   Revise your answer if the critique lands a real objection, and repeat
   until a round surfaces nothing new or you hit a round cap (3 is a
   reasonable default). Record the surviving answer with `decide` and source
   `assumed` — summarize the critique exchange (what was challenged, what
   survived, what changed) in `reasoning`, since that's the only record of
   how the decision was actually stress-tested.

3. Batch topics (a backlog list) run this per-item, each as its own session.

4. End-of-session is otherwise identical to default mode's — author the plan,
   record it, show it — except step 5's `--verify` offer is not optional here:
   every decision in an `--auto` session is `assumed` by construction, so always
   run `--verify` immediately afterward rather than asking first, and say so
   plainly in the plan's header (topic, "resolved via adversarial critique — no
   live user Q&A", and that verify then ran against it).

5. If this session was started with `--backlog-slug` (the batch-backlog-items
   case — a freestanding topic session with no item behind it skips this step
   entirely): once `--verify` is settled, hand off to the `spec` skill via
   `/skill:spec` with this session's resolved decisions and plan.md
   as input, declining spec's own step 4 generation offer. `spec.md` becomes
   the artifact this item's `related_files` records; cite this session's
   `plan_path` from spec's Context field as the decision record behind it —
   spec is final, this plan is the precursor, the same relationship default
   mode's escalation case has (`spec.md` step 3).
