---
name: spec
description: Turn a vague coding task into a structured specification (objective, context, inputs, output format, constraints, evaluation criteria, edge cases, verification steps) before generation begins. Use when the user wants to formalize a task, write a spec, or invokes /spec.
---
If the invoking context names a task (e.g. another command or skill delegated to you with an item's context/next_steps), spec that task. Otherwise spec the task under discussion in the conversation; if neither applies, ask the user what to spec.

## 1. Draft the eight fields

Fill in what you can from the named task and context already in scope (files read, prior discussion) — don't ask about anything inferable from the codebase.

- **Objective** — one sentence: what exists when this is done.
- **Context** — what the agent needs to know: existing code, conventions, prior decisions.
- **Inputs** — data, files, tools, assumptions in bounds.
- **Output format** — the literal shape of the deliverable: file structure, schema, API.
- **Constraints** — what to avoid: new deps, paid APIs, style rules.
- **Evaluation criteria** — how correctness gets judged.
- **Edge cases** — what could go wrong or fall through.
- **Verification steps** — tests/checks that must pass before this counts as done.

## 2. Fill gaps — ask, don't guess

For each field you can't confidently fill, ask one at a time. When the plausible answers are enumerable (2–4 real options), use the `question` tool with your recommendation as the first option, labeled "(Recommended)". When the question is genuinely open-ended, state it directly, give your recommended answer with brief reasoning, and wait for the response. Skip fields already unambiguous from context — a trivial task doesn't need all eight interrogated.

## 3. Escalate real decisions — don't resolve them here

A missing fact gets asked directly (step 2). A genuinely open branch — multiple viable designs, unclear tradeoffs, a decision that cascades into others — gets handed to the `grill-me` skill: invoke it for that specific decision, with the blocked field's question as topic. Let it own its full protocol — Q&A, `--verify`, executor-readiness — don't hand-roll `grill.py` calls here. Decline grill-me's own clear-and-go offer — drafting isn't done yet, so grill-me resolving the branch doesn't get to be the last word.

Grill-me resolving the branch is a precursor, not a replacement: once its session concludes with the decision settled, resume here at step 1 with that field now answered — grill-me settles the open branch, this step never redoes that work, it only drafts the field with the now-known answer. Never interrogate architecture inline in a spec. Cite grill-me's `plan_path` from this spec's own Context field as the decision record behind that field — the spec, not grill-me's plan, is the artifact a caller (e.g. `backlog-item`) records.

## 4. Save and confirm

Write the spec to `~/.claude/data/grill/<topic-slug>-spec.md` (the same central location grill-me/second-opinion use for plan artifacts — never a per-session scratchpad). Never `mkdir -p` that directory first — `grill.py` and `second_opinion.py` each create it on every invocation, so just write the file. Show it to the user. Apply the shared instructions file's "plans and deliverables get a path on record" backlog convention if this is tracked work.

Then ask, via the `question` tool: "Start generation against this spec now?" — `Yes (recommended)` / `No, stop here`. If a delegating caller declined this offer up front (e.g. it owns the handoff decision itself), skip the ask and stop after saving — report the spec path back to the caller.

## 5. Generation

If yes: implement directly in this session, using the spec as the source of truth for scope. The spec is complete — nothing it didn't say yes to gets improvised.

## 6. Validation and audit

After generation, check the result against every **Evaluation criteria** and **Edge cases** line explicitly, and actually run the **Verification steps** — execute them, don't just describe them. If anything fails, revise and re-check. Cap this at 3 rounds (same cap as the `second-opinion` skill's convergence loop); if verification steps are still failing after round 3, stop and state plainly, distinct from a passing finish: "Stopped after 3 rounds — still failing: `<specific step>`." Then ask how to proceed rather than looping further.

Once verification passes (or is stopped-and-reported), ask via the `question` tool: "Run an audit pass for specification gaming?" — `Yes (recommended unless this is trivial)` / `No, done`. A yes checks whether the result satisfies the letter while missing the Objective, via adversarial critique — prefer the native path since you're already running inside opencode: spawn the `adversary` agent (Task tool, no subprocess) with the spec's Objective, the result, and a prompt that argues the result games the spec rather than satisfies it. Fall back to the `second-opinion` skill's `second_opinion.py review` loop only if `adversary` is erroring or unavailable.
