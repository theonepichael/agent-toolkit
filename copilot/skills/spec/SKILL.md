---
name: spec
description: Turn a vague coding task into a structured specification (objective, context, inputs, output format, constraints, evaluation criteria, edge cases, verification steps) before generation begins. Use when the user wants to formalize a task, write a spec, or invokes /spec.
allowed-tools: shell
---
If the user didn't name a specific task (and isn't asking to formalize something already under discussion), ask what to spec before proceeding. Otherwise spec the named task, or the task under discussion in the conversation.

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

For each field you can't confidently fill, ask one at a time, applying the shared instructions file's convention for asking the user to choose. Skip fields already unambiguous from context — a trivial task doesn't need all eight interrogated.

## 3. Escalate real decisions — don't resolve them here

A missing fact gets asked directly (step 2). A genuinely open branch — multiple viable designs, unclear tradeoffs, a decision that cascades into others — gets handed to the `grill-me` skill: use it for that specific decision, with the blocked field's question as topic. Let it run its full protocol — Q&A, plan recording, the `--verify` offer if any decision was defaulted/assumed. Only when it reaches its own end-of-session clear-and-go offer: do not ask the user that question and do not run `mark-pending-execution` yet — drafting isn't done here yet, so grill-me resolving the branch doesn't get to be the last word.

Grill-me resolving the branch is a precursor, not a replacement: once its session concludes with the decision settled, resume here at step 1 with that field now answered — grill-me settles the open branch, this step never redoes that work, it only drafts the field with the now-known answer. Never interrogate architecture inline in a spec. Cite grill-me's plan path from this spec's own Context field as the decision record behind that field — the spec, not grill-me's plan, is the artifact a caller (e.g. `backlog-item`) records.

## 4. Save and confirm

Write the spec to `~/.claude/data/grill/<topic-slug>-spec.md` (the same central location grill-me/second-opinion use for plan artifacts — never a per-session scratchpad). Never `mkdir -p` that directory first — `grill.py` and `second_opinion.py` each create it on every invocation, so just write the file. Show it to the user. Apply the shared instructions file's "plans and deliverables get a path on record" backlog convention if this is tracked work.

Then ask, in plain text with a recommendation: "Start generation against this spec now?" — recommend yes. If a delegating caller declined this offer up front (e.g. it owns the handoff decision itself), skip the ask and stop after saving — report the spec path back to the caller.

## 5. Generation

If yes: implement directly in this session, using the spec as the source of truth for scope. The spec is complete — nothing it didn't say yes to gets improvised.

## 6. Validation and audit

After generation, check the result against every **Evaluation criteria** and **Edge cases** line explicitly, and actually run the **Verification steps** — execute them, don't just describe them. If anything fails, revise and re-check. Cap this at 3 rounds (same cap as the `second-opinion` skill's convergence loop); if verification steps are still failing after round 3, stop and state plainly, distinct from a passing finish: "Stopped after 3 rounds — still failing: `<specific step>`." Then ask how to proceed rather than looping further.

Once verification passes (or is stopped-and-reported), ask, in plain text with a recommendation: "Run an audit pass for specification gaming?" — recommend yes unless this is trivial. A yes reuses the `second-opinion` skill's adversarial critique loop against the result and the spec's Objective — does it satisfy the letter while missing the intent? — rather than self-grading.
