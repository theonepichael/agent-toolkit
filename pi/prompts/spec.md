---
description: "Turn a vague coding task into a structured specification (objective, context, inputs, output format, constraints, evaluation criteria, edge cases, verification steps) before generation begins. Use when the user wants to formalize a task, write a spec, or invokes /spec."
argument-hint: [task description]
---
If $ARGUMENTS is empty, use the task under discussion in the conversation; if neither exists, ask what to spec.

## 1. Draft the eight fields

Fill in what you can from $ARGUMENTS and context already in scope (files read, prior discussion) — don't ask about anything inferable from the codebase.

- **Objective** — one sentence: what exists when this is done.
- **Context** — what the agent needs to know: existing code, conventions, prior decisions.
- **Inputs** — data, files, tools, assumptions in bounds.
- **Output format** — the literal shape of the deliverable: file structure, schema, API.
- **Constraints** — what to avoid: new deps, paid APIs, style rules.
- **Evaluation criteria** — how correctness gets judged.
- **Edge cases** — what could go wrong or fall through.
- **Verification steps** — tests/checks that must pass before this counts as done.

## 2. Fill gaps — ask, don't guess

For each field you can't confidently fill, ask one at a time. Use the `question` tool for an enumerable choice (2–4 real options), your recommendation as the first option labeled "(Recommended)" — Pi ships no built-in question/select tool (only `read`, `bash`, `powershell`, `edit`, `write`, `grep`, `find`, `ls` — `docs/usage.md`), but `question-tool.ts` in this repo supplies one, loaded unless the session was started with `-ne`; it is a hard error (not a silent fallback) in headless `-p`/JSON modes, since there's no UI to prompt through there, so state the choice in plain text with your recommendation instead when running headless. When the question is genuinely open-ended, state it directly, give your recommended answer with brief reasoning, and wait for the response. Skip fields already unambiguous from context — a trivial task doesn't need all eight interrogated.

## 3. Escalate real decisions — don't resolve them here

A missing fact gets asked directly (step 2). A genuinely open branch — multiple viable designs, unclear tradeoffs, a decision that cascades into others — gets handed to `grill-me`: load it via `/skill:grill-me` (Pi registers every discovered skill as a `/skill:<name>` command, confirmed in `docs/skills.md`; the agent can also load it on its own once the topic matches the skill's description in the `<available_skills>` block, per the same doc's "How Skills Work") for that specific decision, with the blocked field's question as topic. Let it own its full protocol — Q&A, `--verify`, executor-readiness — don't hand-roll `grill.py` calls here. Decline grill-me's own clear-and-go offer — drafting isn't done yet, so grill-me resolving the branch doesn't get to be the last word.

Grill-me resolving the branch is a precursor, not a replacement: once its session concludes with the decision settled, resume here at step 1 with that field now answered — grill-me settles the open branch, this step never redoes that work, it only drafts the field with the now-known answer. Never interrogate architecture inline in a spec. Cite grill-me's `plan_path` from this spec's own Context field as the decision record behind that field — the spec, not grill-me's plan, is the artifact a caller (e.g. `backlog-item`) records.

## 4. Save and confirm

Write the spec to `~/.claude/data/grill/<topic-slug>-spec.md` (the same central location grill-me/second-opinion use for plan artifacts — never a per-session scratchpad). Never `mkdir -p` that directory first — `grill.py` and `second_opinion.py` each create it on every invocation, so just write the file. Show it to the user. Apply the shared instructions file's "plans and deliverables get a path on record" backlog convention if this is tracked work.

Then ask, in plain text with a recommendation: "Start generation against this spec now?" — recommend yes.

## 5. Generation

If yes: implement directly in this session, using the spec as the source of truth for scope. The spec is complete — nothing it didn't say yes to gets improvised.

## 6. Validation and audit

After generation, check the result against every **Evaluation criteria** and **Edge cases** line explicitly, and actually run the **Verification steps** — execute them, don't just describe them. If anything fails, revise and re-check. Cap this at 3 rounds (same cap as `/second-opinion`'s convergence loop); if verification steps are still failing after round 3, stop and state plainly, distinct from a passing finish: "Stopped after 3 rounds — still failing: `<specific step>`." Then ask the user how to proceed rather than looping further.

Once verification passes (or is stopped-and-reported), ask in plain text with a recommendation: "Run an audit pass for specification gaming?" — recommend yes unless this is trivial. A yes reuses `/second-opinion`'s `second_opinion.py review` loop against the spec's Objective and the result — does it satisfy the letter while missing the intent? Pi's own design principles rule out built-in sub-agents (`docs/usage.md`'s "Design Principles": "it intentionally does not include built-in MCP, sub-agents, permission popups, plan mode, to-dos, or background bash"), so there is no native path to spawn an adversarial critique agent of its own the way opencode's `adversary` agent does — this always goes through the shared `second_opinion.py` critique loop instead.

## 7. Plumbing (house convention)

1. File lives at `~/dotfiles/pi/prompts/spec.md`.
2. Add a `[[link]]` entry (`src = "pi/prompts/spec.md"`, `dest = "~/.pi/agent/prompts/spec.md"`, `harness = "pi"`) in `links.toml` next to the existing ones.
3. Conventional commit, scope `pi`: `feat`.
