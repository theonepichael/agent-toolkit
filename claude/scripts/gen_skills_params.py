"""gen_skills_params.py — per-(skill, harness) content tables for gen_skills.py.

Kept in its own module (not gen_skills.py itself) so that script's own diffs
stay readable — this file is almost entirely literal template-parameter
content, the generator logic lives in gen_skills.py. Every `FRONTMATTER` and
whole-block value below is either transcribed verbatim from the
hand-authored copy it replaces, or (for pi/skills — new with this script,
since pi/skills/ did not exist before) written fresh from
`pi/CLAUDE_CODE_PARITY.md`'s confirmed facts and `pi/prompts/*.md`'s
existing, already-verified Pi-specific wording, adapted from prompt-template
($ARGUMENTS-substitution) invocation mechanics to skill (`/skill:<name>`,
args appended as `User: <args>`) invocation mechanics where the two differ.

A capability fact already in `gen_skills.CAPABILITY_TABLE` is deliberately
NOT re-embedded as a `{{TOKEN}}` inside any string below: `render_body`'s
`apply_placeholders` does one sequential pass over its values dict, so a
`{{TOKEN}}` nested inside a later-processed value never gets a second pass
and would be left literally in the output. Every value below spells out the
harness-specific fact directly instead.

Requires Python 3.12+.
"""

DASHBOARD_PARAMS: dict[str, dict[str, str]] = {
    "claude": {
        "FRONTMATTER": """\
---
name: dashboard
description: "surfaces backlog and pending items as a dashboard. use when the user says 'dashboard', 'what's pending', 'show backlog', 'where we at', 'what am i working on', 'open items', or any variant of checking current work status. Renamed from /status to avoid colliding with Claude Code's built-in /status (plan usage/rate-limit view) — a naming collision with a built-in command can silently break custom command loading. (session start is covered by a SessionStart hook — do not run this again unprompted.)"
---""",
    },
    "copilot": {
        "FRONTMATTER": """\
---
name: dashboard
description: "surfaces backlog and pending items as a dashboard. use when the user says 'dashboard', 'what's pending', 'show backlog', 'where we at', 'what am i working on', 'open items', or any variant of checking current work status. Renamed from /status to avoid colliding with Claude Code's built-in /status. (session start is covered by a sessionStart hook — do not run this again unprompted.)"
allowed-tools: shell
---""",
    },
    "opencode": {
        "FRONTMATTER": """\
---
description: "surfaces backlog and pending items as a dashboard. use when the user says 'dashboard', 'what's pending', 'show backlog', 'where we at', 'what am i working on', 'open items', or any variant of checking current work status."
---""",
    },
    "agy": {
        "FRONTMATTER": """\
---
name: dashboard
description: "surfaces backlog and pending items as a dashboard. use when the user says 'dashboard', 'what's pending', 'show backlog', 'where we at', 'what am i working on', 'open items', or any variant of checking current work status."
---""",
    },
    "pi": {
        "FRONTMATTER": """\
---
name: dashboard
description: "surfaces backlog and pending items as a dashboard. use when the user says 'dashboard', 'what's pending', 'show backlog', 'where we at', 'what am i working on', 'open items', or any variant of checking current work status."
---""",
    },
}

GRILL_ME_PARAMS: dict[str, dict[str, str]] = {
    "claude": {
        "FRONTMATTER": """\
---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
argument-hint: [--verify | --auto] [topic or plan to grill on]
allowed-tools: [Read, Glob, Grep, Write, AskUserQuestion, "Bash(python3 ~/.claude/scripts/grill.py:*)", "Bash(python3 ~/.claude/scripts/second_opinion.py:*)"]
---""",
        "DEFAULT_MODE_OPENING": (
            "If $ARGUMENTS is empty (and contains neither `--verify` nor `--auto`), "
            "grill the plan under discussion when the conversation makes it obvious; "
            "otherwise ask the user what to grill before proceeding. `--verify` and "
            "`--auto` each run their own section below instead of this Q&A loop."
        ),
        "STEP2_ASK_MECHANISM": (
            "For a question with 2–4 enumerable options, use `AskUserQuestion` "
            "(batch up to 4 per call, applying CLAUDE.md's recommendation-first "
            "convention to each); for a genuinely open-ended question, state it in "
            "plain text with your recommended answer, listed alongside the others "
            "in the same message. If the frontier has more than 4 enumerable-choice "
            "questions, split across multiple `AskUserQuestion` calls, still within "
            "the same turn."
        ),
        "STEP5_VERDICT_OFFER": (
            'offer with the breakdown visible: "N decision(s) have no recorded '
            "verdict — X defaulted/assumed, Y user-stated — want me to run --verify "
            'on them before we call this done?"'
        ),
        "STEP6_CLEARGO": """\
Once verification (if any) is settled, always offer clear-and-go, via `AskUserQuestion` — "Clear context and start executing this plan now?" with options `Yes, clear and go (recommended)` / `No, leave it for later`:
   - **Yes** — run `grill.py mark-pending-execution` (defaults to this session), then tell the user in plain text: "Marked. Run /clear whenever you're ready — I'll pick the plan back up automatically." There's no tool to trigger `/clear` itself, so the user has to type it. The SessionStart hook's `grill.py pending-plan --consume` call surfaces the marked plan at the start of whatever conversation comes next — read its output when a session opens with a "Grill plan ready to execute" notice, and act on the printed instructions (resume if the user says go/continue, otherwise leave the cleared flag alone).
   - **No** — nothing else happens, no state change.""",
        "AUTO_CRITIQUE_MECHANISM": """\
For each open question, instead of asking the user: form your own leading
   answer, then run `/second-opinion`'s iteration loop (same convergence rule
   and round cap — reuse that backend and loop rather than inventing a
   separate critique mechanism) against a short write-up of the question,
   your answer, and enough surrounding context for an outside model to
   attack it credibly. "Adversarial" here means exactly what
   `second_opinion.py`'s own `CRITIQUE_PROMPT` already asks for — find
   problems rather than summarize or agree, name what's underspecified or
   assumed without justification, disagree explicitly where warranted, and
   propose a simpler approach if one exists; a critique that just restates
   or praises your answer isn't adversarial and doesn't count as a round.
   Record the surviving answer with `decide` and source `assumed` —
   summarize the critique exchange (what was challenged, what survived, what
   changed) in `reasoning`, since that's the only record of how the decision
   was actually stress-tested.""",
        "AUTO_SPEC_HANDOFF": """\
If this session was started with `--backlog-slug` (the batch-backlog-items
   case — a freestanding topic session with no item behind it skips this step
   entirely): once `--verify` is settled, hand off to the `spec` skill (Skill
   tool) with this session's resolved decisions and plan.md as input,
   declining spec's own step 4 generation offer. `spec.md` becomes the
   artifact this item's `related_files` records; cite this session's
   `plan_path` from spec's Context field as the decision record behind it —
   spec is final, this plan is the precursor, the same relationship default
   mode's escalation case has (`spec.md` step 3).""",
    },
    "copilot": {
        "FRONTMATTER": """\
---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
allowed-tools: shell
---""",
        "DEFAULT_MODE_OPENING": """\
If the user didn't name a specific topic (and isn't asking for verification or
autonomous resolution), grill the plan under discussion when the conversation
makes it obvious; otherwise ask the user what to grill before proceeding.
`--verify` and autonomous/"grill this on your own" runs each follow their own
section below instead of this Q&A loop.""",
        "STEP2_ASK_MECHANISM": (
            "List them together in the same message, numbered, each with your "
            "recommended answer, applying the shared instructions file's "
            "convention for asking the user to choose."
        ),
        "STEP5_VERDICT_OFFER": (
            'offer, in plain text, with the breakdown visible: "N decision(s) have '
            "no recorded verdict — X defaulted/assumed, Y user-stated — want me to "
            'run --verify on them before we call this done?"'
        ),
        "STEP6_CLEARGO": """\
Once verification (if any) is settled, always offer clear-and-go, in plain text — ask whether to clear context and start executing the plan now. On yes, run `grill.py mark-pending-execution` (defaults to this session), then tell the user: "Marked — start a fresh session whenever you're ready, and I'll pick the plan back up automatically." The SessionStart hook's `grill.py pending-plan --consume` call surfaces the marked plan at the start of whatever session comes next — read its output when a session opens with a "Grill plan ready to execute" notice, and act on the printed instructions (resume if the user says go/continue, otherwise leave the cleared flag alone). On no, nothing else happens, no state change.""",
        "AUTO_CRITIQUE_MECHANISM": """\
For each open question, instead of asking the user: form your own leading
   answer, then run the second-opinion skill's iteration loop (same
   convergence rule and round cap — reuse that backend and loop rather than
   inventing a separate critique mechanism) against a short write-up of the
   question, your answer, and enough surrounding context for an outside
   model to attack it credibly. "Adversarial" here means exactly what
   `second_opinion.py`'s own `CRITIQUE_PROMPT` already asks for — find
   problems rather than summarize or agree, name what's underspecified or
   assumed without justification, disagree explicitly where warranted, and
   propose a simpler approach if one exists; a critique that just restates
   or praises your answer isn't adversarial and doesn't count as a round.
   Record the surviving answer with `decide` and source `assumed` —
   summarize the critique exchange (what was challenged, what survived, what
   changed) in `reasoning`, since that's the only record of how the decision
   was actually stress-tested.""",
        "AUTO_SPEC_HANDOFF": """\
If this session was started with `--backlog-slug` (the batch-backlog-items
   case — a freestanding topic session with no item behind it skips this step
   entirely): once `--verify` is settled, hand off to the `spec` skill with
   this session's resolved decisions and plan.md as input, declining spec's
   own step 4 generation offer. `spec.md` becomes the artifact this item's
   `related_files` records; cite this session's `plan_path` from spec's
   Context field as the decision record behind it — spec is final, this plan
   is the precursor, the same relationship default mode's escalation case has
   (`spec.md` step 3).""",
    },
    "opencode": {
        "FRONTMATTER": """\
---
description: "Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions 'grill me'."
---""",
        "DEFAULT_MODE_OPENING": (
            "If $ARGUMENTS is empty (and contains neither `--verify` nor `--auto`), "
            "grill the plan under discussion when the conversation makes it obvious; "
            "otherwise ask the user what to grill before proceeding. `--verify` and "
            "`--auto` each run their own section below instead of this Q&A loop."
        ),
        "STEP2_ASK_MECHANISM": """\
For each question in the round: when the plausible answers are enumerable (2–4 real options), use the `question` tool with your recommendation as the first option, labeled "(Recommended)"; when genuinely open-ended, ask in plain text:
   - State the question directly.
   - Give your recommended answer with brief reasoning.""",
        "STEP5_VERDICT_OFFER": (
            "offer with the breakdown visible, via the `question` tool: "
            '"N decision(s) have no recorded verdict — X defaulted/assumed, Y '
            "user-stated — want me to run --verify on them before we call this "
            'done?"'
        ),
        "STEP6_CLEARGO": """\
Once verification (if any) is settled, always offer clear-and-go, via the `question` tool — "Clear context and start executing this plan now?" with options `Yes, clear and go (recommended)` / `No, leave it for later`:
   - **Yes** — run `grill.py mark-pending-execution` (defaults to this session), then tell the user in plain text: "Marked — start a fresh session whenever you're ready and ask me to pick the plan back up." opencode has no SessionStart hook to auto-surface the marked plan (Claude Code and Copilot do; opencode's hooks→plugin port is still deferred), so resume is manual: when a session opens with the user asking to resume/execute the marked plan, run `grill.py pending-plan --consume` and act on the printed instructions (resume if the user says go/continue, otherwise leave the cleared flag alone).
   - **No** — nothing else happens, no state change.""",
        "AUTO_CRITIQUE_MECHANISM": """\
For each open question, instead of asking the user: form your own leading
   answer, then critique it adversarially before recording it. Two paths exist
   — prefer the native one, since you're already running inside opencode:

   - **Primary — Task tool, native, no subprocess**: spawn the `adversary`
     agent directly (the model configured in `opencode.jsonc` under
     `agent.adversary`) with a prompt that argues against
     your answer. This is genuine cross-model critique, not the same model
     second-guessing itself, because `adversary` is deliberately configured
     with a model different from the primary session's default — a subagent
     spawned *without* a configured model would inherit the primary's model
     and be a weaker, same-model critique, which is why this always targets
     `adversary` by name rather than the generic `general` subagent.
   - **Alternative — `second_opinion.py review`**: use this instead (or in
     addition, for a third opinion) when you specifically want `agy`'s Gemini
     backend rather than `adversary`'s configured model, or if `adversary` is
     erroring. Don't route through `second_opinion.py`'s own `opencode`
     backend from inside opencode itself — that backend exists for Claude
     Code and Copilot, which have no other way to reach `adversary`; from
     opencode it would just shell out to `opencode run --agent adversary` as
     a subprocess of itself, redoing what the Task tool already does natively
     and more cheaply. Force `--backend agy` if you go this route.

   "Adversarial" here means exactly what `second_opinion.py`'s own
   `CRITIQUE_PROMPT` asks for regardless of which path you use — find
   problems rather than summarize or agree, name what's underspecified or
   assumed without justification, disagree explicitly where warranted, and
   propose a simpler approach if one exists; a critique that just restates or
   praises your answer isn't adversarial and doesn't count as a round.
   Revise your answer if the critique lands a real objection, and repeat
   until a round surfaces nothing new or you hit a round cap (3 is a
   reasonable default). Record the surviving answer with `decide` and source
   `assumed` — summarize the critique exchange (what was challenged, what
   survived, what changed, and which path produced it) in `reasoning`, since
   that's the only record of how the decision was actually stress-tested.""",
        "AUTO_SPEC_HANDOFF": """\
If this session was started with `--backlog-slug` (the batch-backlog-items
   case — a freestanding topic session with no item behind it skips this step
   entirely): once `--verify` is settled, hand off to the `spec` skill via
   opencode's native skill tool (`skill({ name: "spec" })`) with this
   session's resolved decisions and plan.md as input, declining spec's own
   step 4 generation offer. `spec.md` becomes the artifact this item's
   `related_files` records; cite this session's `plan_path` from spec's
   Context field as the decision record behind it — spec is final, this plan
   is the precursor, the same relationship default mode's escalation case has
   (`spec.md` step 3).""",
    },
    "agy": {
        "FRONTMATTER": """\
---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
---""",
        "DEFAULT_MODE_OPENING": """\
If the user didn't name a specific topic (and isn't asking for verification or
autonomous resolution), grill the plan under discussion when the conversation
makes it obvious; otherwise ask the user what to grill before proceeding.
`--verify` and autonomous/"grill this on your own" runs each follow their own
section below instead of this Q&A loop.""",
        "STEP2_ASK_MECHANISM": (
            "List them together in the same message, numbered, each with your "
            "recommended answer, applying the shared instructions file's "
            "convention for asking the user to choose."
        ),
        "STEP5_VERDICT_OFFER": (
            'offer, in plain text, with the breakdown visible: "N decision(s) have '
            "no recorded verdict — X defaulted/assumed, Y user-stated — want me to "
            'run --verify on them before we call this done?"'
        ),
        "STEP6_CLEARGO": """\
Once verification (if any) is settled, always offer clear-and-go, in plain text — ask whether to clear context and start executing the plan now. On yes, run `grill.py mark-pending-execution` (defaults to this session), then tell the user: "Marked — start a fresh session whenever you're ready and ask me to pick the plan back up." agy has no `SessionStart`-equivalent hook event (confirmed: `hooks.md` lists only `PreToolUse`/`PostToolUse`/`PreInvocation`/`PostInvocation`/`Stop`) to auto-surface the marked plan, so resume is manual: when a session opens with the user asking to resume/execute the marked plan, run `grill.py pending-plan --consume` and act on the printed instructions (resume if the user says go/continue, otherwise leave the cleared flag alone). On no, nothing else happens, no state change.""",
        "AUTO_CRITIQUE_MECHANISM": """\
For each open question, instead of asking the user: form your own leading
   answer, then run the second-opinion skill's iteration loop (same
   convergence rule and round cap — reuse that backend and loop rather than
   inventing a separate critique mechanism) against a short write-up of the
   question, your answer, and enough surrounding context for an outside
   model to attack it credibly. "Adversarial" here means exactly what
   `second_opinion.py`'s own `CRITIQUE_PROMPT` already asks for — find
   problems rather than summarize or agree, name what's underspecified or
   assumed without justification, disagree explicitly where warranted, and
   propose a simpler approach if one exists; a critique that just restates
   or praises your answer isn't adversarial and doesn't count as a round.
   Record the surviving answer with `decide` and source `assumed` —
   summarize the critique exchange (what was challenged, what survived, what
   changed) in `reasoning`, since that's the only record of how the decision
   was actually stress-tested.""",
        "AUTO_SPEC_HANDOFF": """\
If this session was started with `--backlog-slug` (the batch-backlog-items
   case — a freestanding topic session with no item behind it skips this step
   entirely): once `--verify` is settled, delegate to the spec skill with this
   session's resolved decisions and plan.md as input, declining spec's own
   step 4 generation offer.

   **Delegating into spec is a suspend-and-return, not a fire-and-forget
   reference** — agy has no discrete "Skill" tool call; the model activates a
   referenced skill by reading and following its SKILL.md body directly using
   normal tool access, and a long sub-conversation inside spec (and, inside
   that, potentially a nested grill-me delegation for an unrelated field) can
   push this session's own state out of effective attention. Before
   delegating:
   1. Print a literal checkpoint marker: `[CHECKPOINT: suspending grill-me
      --auto at step 5 for the spec skill; grill-me itself has nothing left
      to resume once spec confirms its save — this is the last step]`.
   2. Run spec's protocol to actual completion, including any inner grill-me
      delegation and spec's own end-of-session steps.
   3. On return, confirm spec recorded its artifact path and that the item's
      `related_files` cites it (CLAUDE.md's "Plans and deliverables get a
      path on record"). `spec.md` becomes the artifact this item's
      `related_files` records; spec's own Context field cites this session's
      `plan_path` as the decision record behind it — spec is final, this
      plan is the precursor, the same relationship default mode's escalation
      case has (`spec.md` step 3). End the grill-me session here.""",
    },
    "pi": {
        "FRONTMATTER": """\
---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
---""",
        "DEFAULT_MODE_OPENING": """\
If the user didn't name a specific topic (and isn't asking for verification or
autonomous resolution), grill the plan under discussion when the conversation
makes it obvious; otherwise ask the user what to grill before proceeding.
`--verify` and autonomous/"grill this on your own" runs each follow their own
section below instead of this Q&A loop.""",
        "STEP2_ASK_MECHANISM": """\
For each question in the round: when the plausible answers are enumerable (2–4 real options), use the `question` tool with your recommendation as the first option, labeled "(Recommended)" — batch up to 4 per call; when genuinely open-ended, include it in that same batch with 2–4 options anyway. The `question` tool is a hard error (not a silent fallback) in headless `-p`/JSON modes, since there's no UI to prompt through there — if you're running headless, state each question in plain text with your recommended answer instead.""",
        "STEP5_VERDICT_OFFER": (
            "offer with the breakdown visible, via the `question` tool: "
            '"N decision(s) have no recorded verdict — X defaulted/assumed, Y '
            "user-stated — want me to run --verify on them before we call this "
            'done?"'
        ),
        "STEP6_CLEARGO": """\
Once verification (if any) is settled, always offer clear-and-go, via the `question` tool — "Clear context and start executing this plan now?" with options `Yes, clear and go (recommended)` / `No, leave it for later`:
   - **Yes** — use the `grill` tool's `mark_pending_execution` action (defaults to this session), then tell the user in plain text: "Marked — start a fresh session whenever you're ready and ask me to pick the plan back up." Pi supports a `session_start` extension event (`docs/extensions.md`), but no extension hooked to it ships in this repo, so resume is manual: when a session opens with the user asking to resume/execute the marked plan, use the `grill` tool's `pending_plan` action with `consume: true` and act on the printed instructions (resume if the user says go/continue, otherwise leave the cleared flag alone).
   - **No** — nothing else happens, no state change.""",
        "AUTO_CRITIQUE_MECHANISM": """\
For each open question, instead of asking the user: form your own leading
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
   how the decision was actually stress-tested.""",
        "AUTO_SPEC_HANDOFF": """\
If this session was started with `--backlog-slug` (the batch-backlog-items
   case — a freestanding topic session with no item behind it skips this step
   entirely): once `--verify` is settled, hand off to the `spec` skill via
   `/skill:spec` with this session's resolved decisions and plan.md
   as input, declining spec's own step 4 generation offer. `spec.md` becomes
   the artifact this item's `related_files` records; cite this session's
   `plan_path` from spec's Context field as the decision record behind it —
   spec is final, this plan is the precursor, the same relationship default
   mode's escalation case has (`spec.md` step 3).""",
    },
}

BACKLOG_ITEM_PARAMS: dict[str, dict[str, str]] = {
    "claude": {
        "FRONTMATTER": """\
---
name: backlog-item
description: "Runs a dev_status.py backlog item end-to-end: resolve, worktree, spec (escalating to grill-me only for a genuinely open design branch), second-opinion critique, execution handoff, TDD implement, verify, commit/merge/push gates, review+approve. Use when the user says 'work on backlog item 4', 'pick up <slug>', 'let's do the next backlog item', or otherwise names a specific item to work end-to-end. Add --auto (optionally with a slug) for an unattended single-item or full-READY-batch run — commit and merge/push gates still stop live, per item."
argument-hint: [--auto] [slug|N]
---""",
        "OPENING_PARAGRAPH": """\
Work the named item to done, one step at a time. Every user-approval gate
below (`## 10`, `## 11`) stops and waits for the user — never collapse two
gates into one approval. Distinct from those: the item's own `gate` field in
`dev_status.py` (step 5, step 12) is a judgment-step verification checkpoint,
not a user-approval stop — same word, different mechanism, don't conflate
them.

Invoked with `--auto` (`/backlog-item --auto [slug|N]`)? Skip straight to
the `--auto mode` section at the end of this file instead of running the
numbered steps live.""",
        "STEP1_BODY": """\
`python3 ~/.claude/scripts/dev_status.py show <slug|N>`. Read the full
record — never start from the dashboard's one-line summary (CLAUDE.md).
Empty context/next_steps/related_files: stop and ask the user to fill them
in; don't fabricate a plan from the title. Numeric id: note the rendered rev
for `--if-rev` on the next mutating call. related_files already names a
grill plan (`~/.claude/data/grill/<slug>-plan.md`) or a spec
(`~/.claude/data/grill/<slug>-spec.md`)? Planning and critique (steps 5–6)
are already done — skip to step 8. Worktree already has
implemented, uncommitted changes (e.g. handed back from an external
executor)? Skip straight to step 9.""",
        "STEP2_BODY": (
            "If not already in-progress: `dev_status.py start <slug|N>` "
            "(`--if-rev <N>` for numeric ids). On a main/master checkout, "
            "`start` now refuses (worktree guard) — do step 3 first, then run "
            "`start` from inside the fresh worktree."
        ),
        "STEP5_BODY": """\
Delegate to the `spec` skill (Skill tool) with the item's context/next_steps
as the task. Let it draft and save the spec end-to-end (its steps 1–4) —
including its own internal escalation to `grill-me` if a field's design is
genuinely open; `/spec`'s step 3 owns that handoff and the resume-after
entirely, there is nothing to orchestrate here. Decline spec's own step 4
generation offer — step 7 below owns the handoff decision.

Once spec records its artifact path, add it to the item's related_files if
missing (CLAUDE.md's "Plans and deliverables get a path on record"). If
`/spec` delegated into `grill-me` along the way, that session's `plan_path`
is already cited from the spec's Context field — don't also record it as a
second, competing artifact.""",
        "STEP6_BODY": """\
If step 5 set a gate (judgment steps present), run the `second-opinion`
skill against the resulting plan or spec file unconditionally, no ask —
critique the plan before committing to an executor. If step 5 left the gate
unset (all steps mechanical), skip this step; a critique adds nothing to a
rote transformation.""",
        "STEP7_BODY": """\
Decide who implements the plan — ask if it isn't already obvious from the
conversation:
- **Same session, now.** Trivial/small item → go to step 8 immediately.
- **Cheaper Claude session.** Now run `grill.py mark-pending-execution
  --backlog-slug <slug>` (the plan's session, with this item's slug — not
  its own resolved-topic slug) and tell the user to resume with
  `/backlog-item <slug|N>` after `/clear`. The SessionStart hook's
  `pending-plan --consume` then prints that same `/backlog-item <slug>`
  line itself, pointing the fresh session at step 1's resume path instead
  of the plan file directly.
- **opencode/GLM-5.2 — personal projects only, never at work; this user
  does not use opencode in a work context under any circumstances.**
  Confirm the model actually exists in opencode's catalog (`opencode
  models`) before invoking — don't assume the version number is right,
  dictation has flubbed it before. From the worktree, hand off
  non-interactively: `opencode run --auto -m opencode-go/glm-5.2 "Implement
  <plan path> exactly as written — TDD, run the full suite, then STOP
  without committing and report the diff."` (`--auto` is required —
  without it, opencode auto-rejects its own tool-call permission requests
  in headless mode and silently makes no progress). GLM never gets the
  commit gate. Once it reports back, tell the user to point Claude at the
  worktree to review — that resumes at step 9.

For a work-related item, only the first two options are on the table —
don't offer the opencode/GLM route at all.""",
        "STEP8_BODY": (
            "TDD in the worktree: a failing test that proves the gap the plan "
            "names, then the minimal implementation."
        ),
        "STEP10_BODY": (
            "Show the full diff. Stop — AskUserQuestion for explicit commit "
            "approval. No exceptions for being mid-pipeline, and no exception for "
            "code an external executor wrote (CLAUDE.md)."
        ),
        "STEP11_BODY": """\
On approval, commit (conventional format) — this gate is never bundled with
what follows. Personal project (this repo, a personal side project — never
a `work-`-prefixed item or a work repo): offer the follow-on sequence as one
bundled question (CLAUDE.md's Git section) — "merge to main, push, and
clean up the worktree?" — then merge locally, push, `git worktree remove`,
`git branch -d` on that single approval. Work-related or ambiguous: ask
separately for merge and for push — never bundle.""",
        "STEP12_BODY": """\
`dev_status.py review <slug|N>` then `approve <slug|N>` — never a bare
`done` on an in-review item. If `approve` refuses citing an unmet gate,
actually check each criterion from `show <slug|N>` against the diff — don't
pass it reflexively — then cover every criterion with evidence:
`dev_status.py run <slug|N> -- <command>` executes and records a command,
and `gate-pass <slug|N> '{"coverage": {"<N>": "run:<run_id>" or
"manual:<note>"}}'` refuses until each criterion cites a recorded run or a
manual note. Then retry `approve`. Display the full dashboard stdout these
print; don't just narrate a one-line confirmation.""",
        "AUTO_INVOCATION": """\
**Invocation.** `--auto <slug|N>` runs just that item under this mode.
`--auto` with no slug batch-processes every READY item, in dashboard order;
any IN PROGRESS item is resumed first via the existing step 1–2 logic;
BLOCKED items are skipped by construction (never READY). The queue is fixed
at the start of the run — items added to READY mid-run aren't picked up
until a later invocation. Loop the modified per-item procedure below across
the queue.""",
        "AUTO_STEP5_SPEC": """\
**Step 5 (Spec or plan)** — when delegating to the `spec` skill (Skill
   tool), state explicitly in the task text that this backlog-item run is
   `--auto`: if spec's own step 3 escalates into `grill-me` for a
   genuinely open design branch, that inner session should also run
   `grill-me --auto` rather than stopping for live Q&A.""",
        "AUTO_END_BLOCK": """\
in one pass via `AskUserQuestion`, offering each queued item exactly as its
originating CLAUDE.md protocol specifies (a backlog `add`, a `pending add`,
an `out-of-scope add`), confirming or declining each in turn.""",
    },
    "copilot": {
        "FRONTMATTER": """\
---
name: backlog-item
description: "Runs a dev_status.py backlog item end-to-end: resolve, worktree, spec (escalating to grill-me only for a genuinely open design branch), second-opinion critique, execution handoff, TDD implement, verify, commit/merge/push gates, review+approve. Use when the user says 'work on backlog item 4', 'pick up <slug>', 'let's do the next backlog item', or otherwise names a specific item to work end-to-end. Add --auto (optionally with a slug) for an unattended single-item or full-READY-batch run — commit and merge/push gates still stop live, per item."
allowed-tools: shell
---""",
        "OPENING_PARAGRAPH": """\
The target item is whatever slug or integer N the user named in their
prompt (e.g. `/backlog-item 4`, "work on backlog item 4"). If the prompt
also names `--auto` (with or without a target item), skip straight to the
`--auto mode` section at the end of this file instead of running the
numbered steps live. Otherwise, if no target item was named, ask which item
— never guess.

Work the named item to done, one step at a time. Every user-approval gate
below (`## 10`, `## 11`) stops and waits for the user — never collapse two
gates into one approval. Distinct from those: the item's own `gate` field in
`dev_status.py` (step 5, step 12) is a judgment-step verification checkpoint,
not a user-approval stop — same word, different mechanism, don't conflate
them.""",
        "STEP1_BODY": """\
`python3 ~/.claude/scripts/dev_status.py show <slug|N>`. Read the full
record — never start from the dashboard's one-line summary (the shared
instructions file). Empty context/next_steps/related_files: stop and ask
the user to fill them in; don't fabricate a plan from the title. Numeric
id: note the rendered rev for `--if-rev` on the next mutating call.
related_files already names a grill plan (`~/.claude/data/grill/<slug>-plan.md`)
or a spec (`~/.claude/data/grill/<slug>-spec.md`)?
Planning and critique (steps 5–6) are already done — skip to step 8.
Worktree already has implemented, uncommitted changes (e.g. handed back
from an external executor)? Skip straight to step 9. Either skip: the
worktree lives at `$(dirname <repo>)/<repo-name>-<slug>`, where `<repo>` is
the absolute path from `related_files` — resolve that path explicitly and
work there, not the root checkout. Do not assume `cd ../<repo-name>-<slug>`
resolves correctly; a fresh Copilot session's ambient cwd is not guaranteed
to be the repo root.""",
        "STEP2_BODY": (
            "If not already in-progress: `dev_status.py start <slug|N>` "
            "(`--if-rev <N>` for numeric ids). On a main/master checkout, "
            "`start` now refuses (worktree guard) — do step 3 first, then run "
            "`start` from inside the fresh worktree."
        ),
        "STEP5_BODY": """\
Now use the `spec` skill with the item's context/next_steps as the task.
Let it run through drafting and saving end-to-end (its steps 1–4) —
including its own internal escalation to `grill-me` if a field's design is
genuinely open; `spec`'s step 3 owns that handoff and the resume-after
entirely, there is nothing to orchestrate here. Only when it reaches its
own step 4 generation offer: do not ask that question and do not implement
yet — move immediately to step 6 here instead.

Once spec is saved, update the backlog item's `related_files` to include
its path if not already present (per the shared instructions file's "Plans
and deliverables get a path on record" rule) — neither skill has knowledge
of `dev_status.py`, so nothing else performs this update, and step 1's
resume branch depends on it. If `spec` delegated into `grill-me` along the
way, that session's plan path is already cited from the spec's Context
field — don't also record it as a second, competing artifact.""",
        "STEP6_BODY": """\
If step 5 set a gate (judgment steps present), run the `second-opinion`
skill against the resulting plan or spec file unconditionally, no ask —
critique it before committing to an executor. If step 5 left the gate
unset (all steps mechanical), skip this step; a critique adds nothing to a
rote transformation.""",
        "STEP7_BODY": """\
Decide who implements the plan — ask if it isn't already obvious from the
conversation:
- **Same session, now.** Trivial/small item → go to step 8 immediately.
- **Cheaper Copilot session.** Now run `grill.py mark-pending-execution
  --backlog-slug <slug>`, using the resolved string slug from step 1,
  never the raw numeric `N` — `--backlog-slug` validates
  lowercase-kebab-case and aborts otherwise. Tell the user to start a
  fresh Copilot session — the SessionStart hook auto-prints `Resume via:
  /backlog-item <slug>`, no manual `/clear` needed (unlike Claude, where
  the user types `/clear` themselves).
- **A cheaper Copilot model, same machine.** Ask the user for the specific
  model id to run — don't parse `copilot help config` output and guess
  which entry is "the cheap one." State explicitly, before offering this
  option, that a cheaper-tier model doing unsupervised TDD is a materially
  weaker executor than the model running this session — step 9's review
  below is not optional, it's the actual safety net.

  Model check, two parts, neither skipped: (1) advisory —
  `PAGER=cat copilot help config` (Copilot has no `models` subcommand;
  this is the actual lookup), confirm the id appears under the `model:`
  key's bullet list, don't guess a near match if it's missing; (2)
  authoritative — the real invocation below, whose own exit code is the
  definitive signal per the exit-code handling further down.

  Capture the pre-handoff state first: `git -C <worktree> rev-parse
  HEAD`. Then, as one grouped command so a failing `cd` still lands in the
  log instead of leaking to this session's stderr: `(cd <worktree> &&
  timeout -k 30 1800 copilot --model <id> --allow-all-tools -p "Implement
  <absolute plan path> exactly as written — TDD, run the full suite, then
  STOP without committing or pushing.") > <logfile> 2>&1`, where
  `<worktree>` is `$(dirname <repo>)/<repo-name>-<slug>` (see step 1).
  Clear any pre-existing `<logfile>` first and create it with restrictive
  permissions (`umask 077` before the redirect, or `chmod 600` right
  after) — it can hold the child's full transcript on a work machine.
  1800s is a starting default; raise it for a repo whose own suite runs
  longer. This branch never gets the commit gate — enforced by the prompt
  above, verified below, not assumed.

  Exit-code handling once the subprocess returns:
  - **124**: `timeout` killed it (`-k 30` guarantees the process is
    actually gone even if it ignored the initial signal) — treat as a
    failure.
  - **Any other nonzero exit**: grep a bounded slice of the log (e.g.
    `tail -n 100`), never the whole file, for an explicit CLI rejection
    string ("unknown model", "invalid model", an auth error). Found →
    report it and stop. Not found → don't guess a cause (a failed `cd`, a
    crash, and an infra failure all look identical here) — report the
    exit code and log tail verbatim and stop.
  - **Zero exit**: compare `git -C <worktree> rev-parse HEAD` against the
    captured pre-handoff SHA before moving on. Moved → the child committed
    despite the prompt (`--allow-all-tools` auto-approves every tool call,
    including `git commit` — the prompt is the only enforcement) — treat
    like the step 9 rejection case below, using the pre-handoff SHA (not
    `HEAD`) as the reset target. Unchanged → proceed to step 9 as normal;
    the diff comes from `git -C <worktree> diff` directly, the log isn't
    needed on a clean run.

  Step 9 rejection/rework path — if review finds the diff unacceptable
  (or the committed-child case above fired): don't force it forward
  toward step 10. `git status` the worktree first, then either (a) fully
  discard via `git -C <worktree> reset --hard <pre-handoff SHA> && git -C
  <worktree> clean -fd` and retry with an adjusted prompt, or (b) fall
  back to the "Same session" branch and implement directly. Ask the user
  explicitly which, and whether to keep the child's partial diff as a
  starting point or wipe it first — don't pick automatically.""",
        "STEP8_BODY": """\
Work inside the worktree resolved in step 1 — `cd` there (or use `git -C`)
for every shell command in this step and the next three; do not run
TDD/test/diff/merge commands against the root checkout. TDD in the
worktree: a failing test that proves the gap the plan names, then the
minimal implementation.""",
        "STEP10_BODY": """\
Show the full diff (read from the worktree, not root). Stop — ask in
plain text for explicit commit approval, stating your recommendation
first. No exceptions for being mid-pipeline, and no exception for code an
external executor wrote (the shared instructions file).""",
        "STEP11_BODY": """\
On approval, commit (conventional format) — this gate is never bundled
with what follows. Personal project (this repo, a personal side project —
never a `work-`-prefixed item or a work repo): offer the follow-on
sequence as one bundled plain-text question (the shared instructions
file's Git section) — "merge to main, push, and clean up the worktree?" —
then merge locally, push, `git worktree remove`, `git branch -d` on that
single approval. Work-related or ambiguous: ask separately for merge and
for push — never bundle. Run merge, push, `git worktree remove`, and `git
branch -d` from the main checkout (`git -C <repo> merge <slug>` etc.), not
from inside the worktree being removed — a branch cannot merge into
itself.""",
        "STEP12_BODY": """\
`dev_status.py review <slug|N>` then `approve <slug|N>` — never a bare
`done` on an in-review item. If `approve` refuses citing an unmet gate,
actually check each criterion from `show <slug|N>` against the diff — don't
pass it reflexively — then cover every criterion with evidence:
`dev_status.py run <slug|N> -- <command>` executes and records a command,
and `gate-pass <slug|N> '{"coverage": {"<N>": "run:<run_id>" or
"manual:<note>"}}'` refuses until each criterion cites a recorded run or a
manual note. Then retry `approve`. Display the full dashboard stdout these
print; don't just narrate a one-line confirmation.""",
        "AUTO_INVOCATION": """\
**Invocation.** `--auto` with a slug/N runs just that item under this mode.
`--auto` alone batch-processes every READY item, in dashboard order; any IN
PROGRESS item is resumed first via the existing step 1–2 logic; BLOCKED
items are skipped by construction (never READY). The queue is fixed at the
start of the run — items added to READY mid-run aren't picked up until a
later invocation. Loop the modified per-item procedure below across the
queue.""",
        "AUTO_STEP5_SPEC": """\
**Step 5 (Spec or plan)** — when handing off to the `spec` skill, state
   explicitly in the task that this backlog-item run is `--auto`: if
   spec's own step 3 escalates into `grill-me` for a genuinely open design
   branch, tell it to run that inner session as `grill-me --auto` too,
   rather than stopping for live Q&A.""",
        "AUTO_END_BLOCK": """\
in one pass, asking in plain text for each queued item exactly as its
originating shared-instructions protocol specifies (a backlog `add`, a
`pending add`, an `out-of-scope add`), stating a recommendation first and
confirming or declining each in turn.""",
    },
    "opencode": {
        "FRONTMATTER": """\
---
description: "Runs a dev_status.py backlog item end-to-end: resolve, worktree, spec (escalating to grill-me only for a genuinely open design branch), second-opinion critique, execution handoff, TDD implement, verify, commit/merge/push gates, review+approve. Use when the user says 'work on backlog item 4', 'pick up <slug>', 'let's do the next backlog item', or otherwise names a specific item to work end-to-end. Add --auto (optionally with a slug) for an unattended single-item or full-READY-batch run — commit and merge/push gates still stop live, per item."
---""",
        "OPENING_PARAGRAPH": """\
Work the named item to done, one step at a time. `$ARGUMENTS` holds the
invocation: strip a leading `--auto` token if present (note that it was
given) — what remains is the target item, a slug or an integer N. If
`--auto` was given, skip straight to the `--auto mode` section at the end
of this file instead of running the numbered steps live. Otherwise, if the
remaining target is empty, ask the user which item — never guess. Every
user-approval gate below (`## 10`, `## 11`) stops and waits for the user —
never collapse two gates into one approval. Distinct from those: the item's
own `gate` field in `dev_status.py` (step 5, step 12) is a judgment-step
verification checkpoint, not a user-approval stop — same word, different
mechanism, don't conflate them.""",
        "STEP1_BODY": """\
`python3 ~/.claude/scripts/dev_status.py show $ARGUMENTS`. Read the full
record — never start from the dashboard's one-line summary (the shared
instructions file). Empty context/next_steps/related_files: stop and ask
the user to fill them in; don't fabricate a plan from the title. Numeric
id: note the rendered rev for `--if-rev` on the next mutating call.
related_files already names a grill plan (`~/.claude/data/grill/<slug>-plan.md`)
or a spec (`~/.claude/data/grill/<slug>-spec.md`)? Planning and critique
(steps 5–6) are already done — skip to step 8. Worktree already has
implemented, uncommitted changes (e.g. handed back from an external
executor)? Skip straight to step 9.""",
        "STEP2_BODY": (
            "If not already in-progress: `dev_status.py start $ARGUMENTS` "
            "(`--if-rev <N>` for numeric ids). On a main/master checkout, "
            "`start` now refuses (worktree guard) — do step 3 first, then run "
            "`start` from inside the fresh worktree."
        ),
        "STEP5_BODY": """\
Load the `spec` skill via opencode's native skill tool
(`skill({ name: "spec" })`) with the item's context/next_steps as the task.
Let it draft and save the spec end-to-end (its steps 1–4) — including its
own internal escalation to `grill-me` if a field's design is genuinely
open; `spec`'s step 3 owns that handoff and the resume-after entirely,
there is nothing to orchestrate here. Decline spec's own step 4 generation
offer — step 7 below owns the handoff decision.

Once spec records its artifact path, add it to the item's related_files if
missing (CLAUDE.md's "Plans and deliverables get a path on record"). If
`spec` delegated into `grill-me` along the way, that session's `plan_path`
is already cited from the spec's Context field — don't also record it as a
second, competing artifact.""",
        "STEP6_BODY": """\
If step 5 set a gate (judgment steps present), run the `second-opinion`
skill (same skill tool, `skill({ name: "second-opinion" })`) against the
resulting plan or spec file unconditionally, no ask — critique it before
committing to an executor. If step 5 left the gate unset (all steps
mechanical), skip this step; a critique adds nothing to a rote
transformation.""",
        "STEP7_BODY": """\
Decide who implements the plan — ask if it isn't already obvious from the
conversation:
- **Same session, now.** Trivial/small item → go to step 8 immediately.
- **Fresh opencode session.** Now run `grill.py mark-pending-execution
  --backlog-slug <slug>` (the plan's session, with this item's slug — not
  its own resolved-topic slug) and tell the user to start a fresh session
  and type `/backlog-item <slug|N>`. opencode has no SessionStart hook to
  auto-surface the marked plan (the hooks→plugin port is still deferred), so
  the typed command IS the resume path — step 1 sees the plan in
  related_files and skips to step 8.
- **Different/cheaper model.** Confirm the model actually exists in
  opencode's catalog (`opencode models`) before invoking — don't assume the
  version number is right, dictation has flubbed it before. From the
  worktree, hand off non-interactively: `opencode run --auto -m
  <provider/model> "Implement <plan path> exactly as written — TDD, run the
  full suite, then STOP without committing and report the diff."`
  (`--auto` is required — without it, opencode auto-rejects its own
  tool-call permission requests in headless mode and silently makes no
  progress). An external executor never gets the commit gate. Once it
  reports back, review — that resumes at step 9.""",
        "STEP8_BODY": (
            "TDD in the worktree: a failing test that proves the gap the plan "
            "names, then the minimal implementation."
        ),
        "STEP10_BODY": (
            "Show the full diff. Stop — use the `question` tool for explicit "
            "commit approval. No exceptions for being mid-pipeline, and no "
            "exception for code an external executor wrote (the shared "
            "instructions file)."
        ),
        "STEP11_BODY": """\
On approval, commit (conventional format) — this gate is never bundled with
what follows. Personal project (this repo, a personal side project — never
a `work-`-prefixed item or a work repo): offer the follow-on sequence as one
bundled question (the shared instructions file's Git section) — "merge to
main, push, and clean up the worktree?" — then merge locally, push, `git
worktree remove`, `git branch -d` on that single approval. Work-related or
ambiguous: ask separately for merge and for push — never bundle.""",
        "STEP12_BODY": """\
`dev_status.py review $ARGUMENTS` then `approve $ARGUMENTS` — never a bare
`done` on an in-review item. If `approve` refuses citing an unmet gate,
actually check each criterion from `show $ARGUMENTS` against the diff —
don't pass it reflexively — then cover every criterion with evidence:
`dev_status.py run $ARGUMENTS -- <command>` executes and records a command,
and `gate-pass $ARGUMENTS '{"coverage": {"<N>": "run:<run_id>" or
"manual:<note>"}}'` refuses until each criterion cites a recorded run or a
manual note. Then retry `approve`. Display the full dashboard stdout these
print; don't just narrate a one-line confirmation.""",
        "AUTO_INVOCATION": """\
**Invocation.** A slug/N present after stripping `--auto` runs just that
item under this mode. No slug batch-processes every READY item, in
dashboard order; any IN PROGRESS item is resumed first via the existing
step 1–2 logic; BLOCKED items are skipped by construction (never READY).
The queue is fixed at the start of the run — items added to READY mid-run
aren't picked up until a later invocation. Loop the modified per-item
procedure below across the queue.""",
        "AUTO_STEP5_SPEC": """\
**Step 5 (Spec or plan)** — when loading the `spec` skill
   (`skill({ name: "spec" })`), state explicitly in the task text that this
   backlog-item run is `--auto`: if spec's own step 3 escalates into
   `grill-me` for a genuinely open design branch, tell it to run that inner
   session as `grill-me --auto` too, rather than stopping for live Q&A.""",
        "AUTO_END_BLOCK": """\
in one pass via the `question` tool, offering each queued item exactly as
its originating CLAUDE.md protocol specifies (a backlog `add`, a
`pending add`, an `out-of-scope add`), confirming or declining each in turn.""",
    },
    "agy": {
        "FRONTMATTER": """\
---
name: backlog-item
description: "Runs a dev_status.py backlog item end-to-end: resolve, worktree, spec/plan, second-opinion critique, execution handoff, TDD implement, verify, commit/merge/push gates, review+approve. Use when the user says 'work on backlog item 4', 'pick up <slug>', 'let's do the next backlog item', or otherwise names a specific item to work end-to-end. Add --auto (optionally with a slug) for an unattended single-item or full-READY-batch run — commit and merge/push gates still stop live, per item."
---""",
        "OPENING_PARAGRAPH": """\
Work the named item to done, one step at a time. If the user's prompt names
`--auto` (with or without a target item), skip straight to the `--auto
mode` section at the end of this file instead of running the numbered
steps live. Otherwise, if the user didn't name a specific item (slug or N),
ask which one — never guess. Every user-approval gate below (`## 10`,
`## 11`) stops and waits for the user — never collapse two gates into one
approval. Distinct from those: the item's own `gate` field in
`dev_status.py` (step 5, step 12) is a judgment-step verification
checkpoint, not a user-approval stop — same word, different mechanism,
don't conflate them.""",
        "STEP1_BODY": """\
`python3 ~/.claude/scripts/dev_status.py show <slug|N>`. Read the full
record — never start from the dashboard's one-line summary (the shared
instructions file). Empty context/next_steps/related_files: stop and ask
the user to fill them in; don't fabricate a plan from the title. Numeric
id: note the rendered rev for `--if-rev` on the next mutating call.
related_files already names a grill plan (`~/.claude/data/grill/<slug>-plan.md`)
or a spec (`~/.claude/data/grill/<slug>-spec.md`)? Planning and critique
(steps 5–6) are already done — skip to step 8. Worktree already has
implemented, uncommitted changes (e.g. handed back from an external
executor)? Skip straight to step 9. `next_steps` starts with "Resume
backlog-item at step N" (a return pointer left by an earlier suspend, see
step 5–6)? That step N is where to resume, not step 1's normal dispatch.""",
        "STEP2_BODY": (
            "If not already in-progress: `dev_status.py start <slug|N>` "
            "(`--if-rev <N>` for numeric ids). On a main/master checkout, "
            "`start` now refuses (worktree guard) — do step 3 first, then run "
            "`start` from inside the fresh worktree."
        ),
        "STEP5_BODY": """\
Delegate to the spec skill with the item's context/next_steps as the task.
Let it draft and save the spec end-to-end (its steps 1–4) — including its
own internal escalation to grill-me if a field's design is genuinely open;
spec's step 3 owns that handoff and the resume-after entirely, including
its own suspend-and-return discipline for that inner delegation. There is
nothing to orchestrate here. Decline spec's own step 4 generation offer —
step 7 below owns the handoff decision.

**Delegating into spec is a suspend-and-return, not a fire-and-forget
reference** — agy has no discrete "Skill" tool call; the model activates a
referenced skill by reading and following its SKILL.md body directly using
normal tool access, which means a long sub-conversation inside spec (and,
inside that, potentially grill-me) can push this procedure's own state out
of effective attention. Before delegating:
1. Print a literal checkpoint marker: `[CHECKPOINT: suspending backlog-item
   at step 5 for the spec skill; resume at step 6 when it finishes]`.
2. Persist the same return pointer somewhere that outlives the chat
   transcript — this harness and agy both auto-compress context under
   length pressure, so the marker alone isn't enough: `dev_status.py
   update <slug> '{"next_steps": "Resume backlog-item at step 6 after the
   spec skill finishes - <original next_steps preserved/appended>"}'`.
3. Run spec's protocol to actual completion, including any inner grill-me
   delegation and spec's own end-of-session steps.
4. On return, read `~/.gemini/antigravity-cli/skills/backlog-item/SKILL.md`'s
   own step 6 text by its literal absolute path before acting — don't rely
   on recalling it from earlier in the conversation.

Once spec records its artifact path, add it to the item's related_files if
missing (the shared instructions file's "Plans and deliverables get a path
on record"). If spec delegated into grill-me along the way, that session's
`plan_path` is already cited from the spec's Context field — don't also
record it as a second, competing artifact.""",
        "STEP6_BODY": """\
If step 5 set a gate (judgment steps present), run the second-opinion
skill against the resulting plan or spec file unconditionally, no ask,
using the same suspend-and-return framing as step 5 (checkpoint marker,
persisted return pointer, absolute-path re-read on return) — critique the
plan before committing to an executor. If step 5 left the gate unset (all
steps mechanical), skip this step; a critique adds nothing to a rote
transformation.""",
        "STEP7_BODY": """\
Decide who implements the plan — ask if it isn't already obvious from the
conversation, in plain conversational text with a stated recommendation (agy
has no structured multi-choice widget — state the options, recommend one,
then stop and wait for an actual reply, don't assume the recommended option
was accepted):

- **Same session, now.** Trivial/small item → go to step 8 immediately.
- **Cheaper agy session.** Run `grill.py mark-pending-execution
  --backlog-slug <slug>` (the plan's session, with this item's slug — not
  its own resolved-topic slug), then tell the user to start a fresh agy
  session and type `/backlog-item <slug|N>` themselves. agy has no
  `SessionStart`-equivalent hook (confirmed: `hooks.md` lists only
  `PreToolUse`/`PostToolUse`/`PreInvocation`/`PostInvocation`/`Stop`) to
  auto-surface the marked plan, so the typed command IS the resume path —
  step 1 sees the plan in related_files and skips to step 8.
- **A cheaper agy model, same machine.** Personal projects only, never at
  work. Ask the user to name the specific model id to run (don't parse `agy
  models` stdout and guess which entry is "the cheap one" — that's brittle
  to catalog/format drift). State explicitly, before offering this option,
  that a flash-tier model doing unsupervised TDD (implement, run tests,
  debug, iterate) is a materially weaker executor than the model running
  this session — step 9's review below is not optional for this branch,
  it's the actual safety net. If chosen: this is a blocking subprocess call
  from this session's own Bash access (the same shape as opencode's `run -m
  <model>` handoff on other harnesses), not an out-of-band handoff like the
  cheaper-session option above — say so, so the user knows this session's
  own context/tokens pay for it. Redirect the child's output to a file
  rather than letting the full streaming transcript land in this session's
  context: `agy -p --model <id> "Implement <plan path> exactly as written —
  TDD, run the full suite, then STOP without committing and report the
  diff." > /tmp/<slug>-handoff.log 2>&1`, then read back only the final
  summary/diff from the log. This branch never gets the commit gate. Once
  it reports back, review the diff yourself — that resumes at step 9.

For a work-related item, only the first two options are on the table —
don't offer the cheaper-model branch at all.""",
        "STEP8_BODY": (
            "TDD in the worktree: a failing test that proves the gap the plan "
            "names, then the minimal implementation."
        ),
        "STEP10_BODY": """\
Show the full diff. Ask for explicit commit approval, then stop and yield
the turn. Do not run `git commit` under any circumstances until the user's
next message contains an explicit yes — stating the question is not the
same as getting an answer. No exceptions for being mid-pipeline, and no
exception for code an external executor wrote (the shared instructions
file).""",
        "STEP11_BODY": """\
On approval, commit (conventional format) — this gate is never bundled with
what follows. Personal project (this repo, a personal side project — never
a `work-`-prefixed item or a work repo): offer the follow-on sequence as one
bundled question (the shared instructions file's Git section) — "merge to
main, push, and clean up the worktree?" — then merge locally, push, `git
worktree remove`, `git branch -d` on that single approval. Work-related or
ambiguous: ask separately for merge and for push — never bundle.""",
        "STEP12_BODY": """\
`dev_status.py review <slug|N>` then `approve <slug|N>` — never a bare
`done` on an in-review item. If `approve` refuses citing an unmet gate,
actually check each criterion from `show <slug|N>` against the diff — don't
pass it reflexively — then cover every criterion with evidence:
`dev_status.py run <slug|N> -- <command>` executes and records a command,
and `gate-pass <slug|N> '{"coverage": {"<N>": "run:<run_id>" or
"manual:<note>"}}'` refuses until each criterion cites a recorded run or a
manual note. Then retry `approve`. Display the full dashboard stdout these
print; don't just narrate a one-line confirmation.""",
        "AUTO_INVOCATION": """\
**Invocation.** `--auto` with a slug/N runs just that item under this mode.
`--auto` alone batch-processes every READY item, in dashboard order; any IN
PROGRESS item is resumed first via the existing step 1–2 logic; BLOCKED
items are skipped by construction (never READY). The queue is fixed at the
start of the run — items added to READY mid-run aren't picked up until a
later invocation. Loop the modified per-item procedure below across the
queue. This mode does not remove the need for step 5/6's suspend-and-return
checkpoint discipline around delegating into `spec` (and, inside that,
potentially `grill-me`) — it still applies unchanged; the checkpoint marker
and the persisted `next_steps` pointer just also carry the auto-context note
from point 3 below.""",
        "AUTO_STEP5_SPEC": """\
**Step 5 (Spec or plan)** — the checkpoint marker and persisted
   `next_steps` pointer this step already requires before delegating into
   `spec` also state explicitly that this backlog-item run is `--auto`: if
   spec's own step 3 escalates into `grill-me` for a genuinely open design
   branch, that inner session should also run `grill-me --auto` rather than
   stopping for live Q&A.""",
        "AUTO_END_BLOCK": """\
in one pass, asking in plain conversational text for each queued item
exactly as its originating shared-instructions protocol specifies (a
backlog `add`, a `pending add`, an `out-of-scope add`), stating a
recommendation first, then stopping and waiting for an actual reply before
each next entry.""",
    },
    "pi": {
        "FRONTMATTER": """\
---
name: backlog-item
description: "Runs a dev_status.py backlog item end-to-end: resolve, worktree, spec (escalating to grill-me only for a genuinely open design branch), second-opinion critique, execution handoff, TDD implement, verify, commit/merge/push gates, review+approve. Use when the user says 'work on backlog item 4', 'pick up <slug>', 'let's do the next backlog item', or otherwise names a specific item to work end-to-end. Add --auto (optionally with a slug) for an unattended single-item or full-READY-batch run — commit and merge/push gates still stop live, per item. Also supports a --swarm[=N] mode (concurrent recursive pi workers via herdr) -- see pi/prompts/backlog-item.md, the file /backlog-item actually runs, for that procedure; this generated copy only points at it."
---""",
        "OPENING_PARAGRAPH": """\
Work the named item to done, one step at a time. If the invocation names
`--auto` (with or without a target item), skip straight to the `--auto
mode` section at the end of this file instead of running the numbered
steps live. Otherwise, if no target item (slug or N) was named, ask which
one — never guess. Every user-approval gate below (`## 10`, `## 11`) stops
and waits for the user — never collapse two gates into one approval.
Distinct from those: the item's own `gate` field in `dev_status.py` (step
5, step 12) is a judgment-step verification checkpoint, not a
user-approval stop — same word, different mechanism, don't conflate them.

There is also a `--swarm[=N]` mode -- `N` concurrent recursive pi workers
via herdr fanning out over the full READY queue, instead of one item at a
time. This generated copy doesn't carry that procedure (avoids a second copy
drifting out of sync); type `/backlog-item --swarm[=N]` directly, which runs
`pi/prompts/backlog-item.md` -- read that file for the full `--swarm[=N]
mode` section.""",
        "STEP1_BODY": """\
Call the `dev_status` tool with `action: "show", slug: "<slug|N>"` (works
whether the identifier is a real slug or a numeric position — `show` is
read-only). Its response's `id` field is this item's real slug — use that
resolved slug for every remaining step below (every mutating `dev_status`
action refuses a numeric slug outright). Read the full record — never start
from the dashboard's one-line summary (CLAUDE.md). Empty
context/next_steps/related_files: stop and ask the user to fill them in;
don't fabricate a plan from the title. related_files already names a grill
plan (`~/.claude/data/grill/<slug>-plan.md`) or a spec
(`~/.claude/data/grill/<slug>-spec.md`)? Planning and critique (steps 5–6)
are already done — skip to step 8. Worktree already has implemented,
uncommitted changes (e.g. handed back from an external executor)? Skip
straight to step 9. If the `dev_status` tool is genuinely unavailable (e.g.
the session was started with `--no-extensions`), fall back to
`python3 ~/.claude/scripts/dev_status.py show <slug|N>` for this and every
other step below — in that fallback path only, a numeric id needs a fresh,
non-quiet `render` immediately before each mutating call to read the
current rev for `--if-rev` (CLAUDE.md's Backlog section).""",
        "STEP2_BODY": (
            "If not already in-progress: call the `dev_status` tool with "
            '`action: "start", slug: "<resolved slug>"` (or the bash fallback '
            "named in step 1). On a main/master checkout, `start` now refuses "
            "(worktree guard), and the typed tool has no `--allow-main` "
            "escape hatch — do step 3 first, then run `start` from inside the "
            "fresh worktree."
        ),
        "STEP5_BODY": """\
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
second, competing artifact.""",
        "STEP6_BODY": """\
If step 5 set a gate (judgment steps present), run the `second-opinion`
skill (`/skill:second-opinion`) against the resulting plan or spec file
unconditionally, no ask — critique it before committing to an executor. If
step 5 left the gate unset (all steps mechanical), skip this step; a
critique adds nothing to a rote transformation.""",
        "STEP7_BODY": """\
Decide who implements the plan — ask if it isn't already obvious from the
conversation, in plain conversational text with a stated recommendation
(this repo's `question` extension tool is also available for an
interactive session — either way, state the options, recommend one, then
stop and wait for an actual reply; headless `-p`/JSON modes have no
structured-choice tool at all, so plain text is the only option there):

- **Same session, now.** Trivial/small item → go to step 8 immediately.
- **Fresh Pi session.** Use the `grill` tool's `mark_pending_execution`
  action with `backlogSlug` set to this item's slug (the plan's session,
  with this item's slug — not its own resolved-topic slug) and tell the
  user to start a fresh session and type `/backlog-item <slug|N>`. Pi
  supports a `session_start` extension event (`docs/extensions.md`), but
  this repo doesn't ship an extension hooked to it to auto-surface the
  marked plan, so the typed command IS the resume path — step 1 sees the
  plan in related_files and skips to step 8.
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
  and a bare `agy -p` swallows the following flag as its prompt). Set
  `autoApprove: true` for opencode or agy — without it opencode makes no
  progress headless. Pi needs no such flag: it has no built-in
  permission-prompt system (`docs/usage.md`'s Design Principles), so tool
  calls execute freely in `-p` mode, gated only by whatever this repo's
  `permission-gate.ts` denies (see `pi/CLAUDE_CODE_PARITY.md`). opencode is
  for personal projects only — never a work-related item. An external
  executor never gets the commit gate. Once it reports back, review — that
  resumes at step 9.""",
        "STEP8_BODY": (
            "TDD in the worktree: a failing test that proves the gap the plan "
            "names, then the minimal implementation."
        ),
        "STEP10_BODY": """\
Show the full diff. Stop — ask in plain text for explicit commit approval,
stating your recommendation first (this repo's `question` extension tool
is also available for an interactive session; either way, wait for an
actual reply). No exceptions for being mid-pipeline, and no exception for
code an external executor wrote (CLAUDE.md).""",
        "STEP11_BODY": """\
On approval, commit (conventional format) — this gate is never bundled with
what follows. Personal project (this repo, a personal side project — never
a `work-`-prefixed item or a work repo): offer the follow-on sequence as one
bundled question (CLAUDE.md's Git section) — "merge to main, push, and
clean up the worktree?" — then merge locally, push, `git worktree remove`,
`git branch -d` on that single approval. Work-related or ambiguous: ask
separately for merge and for push — never bundle.""",
        "STEP12_BODY": """\
Call the tool with `action: "review", slug: "<resolved slug>"`, then
`action: "approve", slug: "<resolved slug>"` — never a bare `done` on an
in-review item. If `approve` refuses citing an unmet gate, actually check
each criterion from `show`'s record against the diff — don't pass it
reflexively — then cover every criterion with evidence (`action: "run"`
executes and records a command; `action: "gate_pass"` takes a `patch`
`{"coverage": {"<N>": "run:<run_id>" or "manual:<note>"}}` and refuses
until each criterion cites a recorded run or a manual note) and retry
`approve`. Display the full dashboard text these return; don't just
narrate a one-line confirmation. (Bash fallback, per step 1: the
equivalent `dev_status.py` commands, same caveats.)""",
        "AUTO_INVOCATION": """\
**Invocation.** `--auto` with a slug/N runs just that item under this mode.
`--auto` alone batch-processes every READY item, in dashboard order; any IN
PROGRESS item is resumed first via the existing step 1–2 logic; BLOCKED
items are skipped by construction (never READY). The queue is fixed at the
start of the run — items added to READY mid-run aren't picked up until a
later invocation. Loop the modified per-item procedure below across the
queue.""",
        "AUTO_STEP5_SPEC": """\
**Step 5 (Spec or plan)** — when loading the `spec` skill
   (`/skill:spec`), state explicitly in the task text that this
   backlog-item run is `--auto`: if spec's own step 3 escalates into
   `grill-me` for a genuinely open design branch, tell it to run that inner
   session as `grill-me --auto` too, rather than stopping for live Q&A.""",
        "AUTO_END_BLOCK": """\
in one pass, asking in plain text for each queued item exactly as its
originating CLAUDE.md protocol specifies (a backlog `add`, a `pending add`,
an `out-of-scope add`), stating a recommendation first and confirming or
declining each in turn.""",
    },
}

MAKE_SKILL_PARAMS: dict[str, dict[str, str]] = {
    "claude": {
        "FRONTMATTER": """\
---
name: make-skill
description: "Author or revise a Claude Code skill (slash command) using a trigger/structure/steering/pruning rubric. Use when the user wants to create a new skill, improve or simplify an existing one, or complains a skill isn't triggering or isn't being followed."
---""",
        "TRIGGER_SECTION": """\
Ask (or infer and confirm): model-invoked, user-invoked, or both?

- **Model-invoked** costs context in every session and can silently not fire; it buys hands-off convenience. **User-invoked** is reliable and cheap but the user must remember it exists.
- The frontmatter `description` IS the model-invoke surface. Write it as: what the skill does + "use when" + the literal phrases the user actually says (steal them from real transcripts). For user-invoked-only skills, keep the description one terse line.""",
        "STRUCTURE_REF_NOTE": (
            "- Supporting material (schemas, long examples, lookup tables) does "
            "NOT go in the body. Put it in "
            "`~/dotfiles/claude/commands/ref/<skill>-<topic>.md` and point to it "
            'from the step that needs it: "Read `~/.claude/commands/ref/...` '
            'when X." Reference files need their own symlink lines (step 5).'
        ),
        "STEERING_WIDGET_NOTE": "",
        "VERIFY_PROBE": """\
Probe with headless runs: `claude -p '<a real trigger phrase>'` for model-invoke, `claude -p '/<name> <args>'` for behavior. Pass `--add-dir ~/dotfiles` when the probe must read a skill file — the headless sandbox won't follow the `~/.claude/commands` symlinks otherwise. Check the output (and reasoning, if visible) repeats your leading words back. If the agent skips a step, that step needs splitting or stronger steering — not more prose.""",
        "PLUMBING_STEPS": """\
1. File lives at `~/dotfiles/claude/commands/<name>.md`.
2. Add a `[[link]]` entry (`src = "claude/commands/<name>.md"`, `dest = "~/.claude/commands/<name>.md"`, `harness = "claude"`) in `links.toml` next to the existing ones (same for any ref files).
3. Create the live symlink now: `ln -s ~/dotfiles/claude/commands/<name>.md ~/.claude/commands/<name>.md`.
4. Conventional commit, scope `claude`: `feat` for a new skill, `refactor`/`docs` for revisions.""",
    },
    "copilot": {
        "FRONTMATTER": """\
---
name: make-skill
description: "Author or revise a Copilot CLI skill using a trigger/structure/steering/pruning rubric. Use when the user wants to create a new skill, improve or simplify an existing one, or complains a skill isn't triggering or isn't being followed."
allowed-tools: shell
---""",
        "TRIGGER_SECTION": """\
Copilot supports both model-decision activation (based on your prompt and
the skill's description) and explicit user-typed invocation — put the skill
name in the prompt preceded by a slash, e.g. `/dashboard`
(`copilot/CLAUDE_CODE_PARITY.md` §1/§3, confirmed live). Ask (or infer and
confirm) whether this skill needs strong trigger phrasing for the
model-decision path, a terse one-liner for a user-invoked-only skill, or
both — the same trade-off Claude Code's model-invoked/user-invoked split
names. The frontmatter `description` IS the model-invoke surface either
way: write it as what the skill does + "use when" + the literal phrases the
user actually says (steal them from real transcripts). A vague description
means the skill silently never fires on the model-decision path; that
failure mode is invisible until you go looking for it, so err toward
over-specifying trigger phrases.""",
        "STRUCTURE_REF_NOTE": (
            "- Supporting material (schemas, long examples, lookup tables) does "
            "NOT go in the body. Put it in "
            "`~/dotfiles/copilot/skills/<skill>/ref/<topic>.md` and point to it "
            "from the step that needs it. Reference files need their own symlink "
            "lines (step 5)."
        ),
        "STEERING_WIDGET_NOTE": (
            "- Copilot has no `AskUserQuestion`-style structured prompt "
            "(`copilot/CLAUDE_CODE_PARITY.md` §1: confirmed absent from `-p` "
            "invocations). Any step that needs a multi-choice decision from the "
            "user must be written as plain conversational text: state the "
            "question, give a recommendation, wait for a plain-text reply. Don't "
            "design a skill step around a UI widget Copilot doesn't have in this "
            "invocation mode."
        ),
        "VERIFY_PROBE": """\
Probe with `copilot -p '<a real trigger phrase>' --allow-all-tools`
(confirmed non-interactive print-mode flags, `copilot/CLAUDE_CODE_PARITY.md`
§1) for model-invoke, `copilot -p '/<name> <args>' --allow-all-tools` for a
direct user-typed invocation. Check the output (and reasoning, if visible)
repeats your leading words back. If the agent skips a step, that step needs
splitting or stronger steering — not more prose.""",
        "PLUMBING_STEPS": """\
1. File lives at `~/dotfiles/copilot/skills/<name>/SKILL.md` (same for any ref files, under `~/dotfiles/copilot/skills/<name>/ref/`).
2. Add a `[[link]]` entry (`src = "copilot/skills/<name>/SKILL.md"`, `dest = "~/.copilot/skills/<name>/SKILL.md"`, `harness = "copilot"`) in `links.toml` next to the existing ones (same for any ref files).
3. Create the live symlink now: `ln -s ~/dotfiles/copilot/skills/<name>/SKILL.md ~/.copilot/skills/<name>/SKILL.md`.
4. Conventional commit, scope `copilot`: `feat` for a new skill, `refactor`/`docs` for revisions.""",
    },
    "opencode": {
        "FRONTMATTER": """\
---
description: "Author or revise an opencode skill (SKILL.md) using a trigger/structure/steering/pruning rubric. Use when the user wants to create a new skill, improve or simplify an existing one, or complains a skill isn't triggering or isn't being followed."
---""",
        "TRIGGER_SECTION": """\
Ask (or infer and confirm): model-invoked, user-invoked, or both?

- **Model-invoked** costs context in every session and can silently not fire; it buys hands-off convenience. **User-invoked** is reliable and cheap but the user must remember it exists.
- The frontmatter `description` IS the model-invoke surface. Write it as: what the skill does + "use when" + the literal phrases the user actually says (steal them from real transcripts). For user-invoked-only skills, keep the description one terse line.""",
        "STRUCTURE_REF_NOTE": (
            "- Supporting material (schemas, long examples, lookup tables) does "
            "NOT go in the body. Put it in "
            "`~/.config/opencode/skills/ref/<skill>-<topic>.md` and point to it "
            'from the step that needs it: "Read `~/.config/opencode/skills/ref/...` '
            'when X."'
        ),
        "STEERING_WIDGET_NOTE": "",
        "VERIFY_PROBE": """\
Probe with headless runs: `opencode -p '<a real trigger phrase>'` for model-invoke, `opencode -p '/<name> <args>'` for behavior. Check the output (and reasoning, if visible) repeats your leading words back. If the agent skips a step, that step needs splitting or stronger steering — not more prose.""",
        "PLUMBING_STEPS": """\
1. Create the repo file at `~/dotfiles/opencode/skills/<name>/SKILL.md` with frontmatter (`name`, `description`) and the skill body.
2. Add a `[[link]]` entry (`src = "opencode/skills/<name>/SKILL.md"`, `dest = "~/.config/opencode/skills/<name>/SKILL.md"`, `harness = "opencode"`) in `links.toml` next to the existing ones.
3. Create the live symlink now: `ln -s ~/dotfiles/opencode/skills/<name>/SKILL.md ~/.config/opencode/skills/<name>/SKILL.md`.
   Discovery is automatic — opencode picks up any `SKILL.md` under `~/.config/opencode/skills/` with no enabling config — but a skill dropped straight into the live path without steps 1–2 won't reproduce on other machines. The repo file + `links.toml` entry is what makes it reproducible; discovery alone is not reproducibility.
4. Conventional commit, scope `skills`: `feat` for a new skill, `refactor`/`docs` for revisions.""",
    },
    "agy": {
        "FRONTMATTER": """\
---
name: make-skill
description: "Author or revise an agy (Antigravity CLI) skill using a trigger/structure/steering/pruning rubric. Use when the user wants to create a new skill, improve or simplify an existing one, or complains a skill isn't triggering or isn't being followed."
---""",
        "TRIGGER_SECTION": """\
agy skills use progressive disclosure: only `name` + `description` are ever
injected into context; the full body loads only once "the model (or the
user) explicitly decides to activate it" (agy's own customization docs). As
of agy 1.1.12, typed `/<skill-name>` prompts also expand and run a skill
directly (`agy/CLAUDE_CODE_PARITY.md` §3, confirmed live) — model-decision
activation still works too, so the frontmatter `description` remains the
model-invoke trigger surface either way. Write it as: what the skill does +
"use when" + the literal phrases the user actually says (steal them from
real transcripts).""",
        "STRUCTURE_REF_NOTE": (
            "- Supporting material (schemas, long examples, lookup tables) does "
            "NOT go in the body. agy's own docs specify a `references/` "
            "subdirectory for this (not `ref/` — matches agy's documented "
            "skill-folder convention, distinct from this repo's "
            "`claude`/`copilot` naming). Put it in "
            "`~/dotfiles/agy/skills/<skill>/references/<topic>.md` and point to "
            "it from the step that needs it. Reference files need their own "
            "symlink lines (step 5)."
        ),
        "STEERING_WIDGET_NOTE": (
            "- agy has no `AskUserQuestion`-style structured prompt "
            "(`agy/CLAUDE_CODE_PARITY.md` §3: confirmed absent from `--help`, "
            "docs, and probes). Any step that needs a multi-choice decision "
            "from the user must be written as plain conversational text: state "
            "the question, give a recommendation, wait for a plain-text reply. "
            "Don't design a skill step around a UI widget agy doesn't have."
        ),
        "VERIFY_PROBE": (
            "Probe with `agy -p '<a real trigger phrase>'` (confirmed "
            "non-interactive print-mode flag). Check the output (and reasoning, "
            "if visible) repeats your leading words back. If the agent skips a "
            "step, that step needs splitting or stronger steering — not more "
            "prose."
        ),
        "PLUMBING_STEPS": """\
1. File lives at `~/dotfiles/agy/skills/<name>/SKILL.md` (same for any reference files, under `~/dotfiles/agy/skills/<name>/references/`).
2. Add a `[[link]]` entry (`src = "agy/skills/<name>/SKILL.md"`, `dest = "~/.gemini/antigravity-cli/skills/<name>/SKILL.md"`, `harness = "agy"`) in `links.toml` next to the existing ones (same for any reference files).
3. Create the live symlink now: `ln -s ~/dotfiles/agy/skills/<name>/SKILL.md ~/.gemini/antigravity-cli/skills/<name>/SKILL.md`.
4. Conventional commit, scope `agy`: `feat` for a new skill, `refactor`/`docs` for revisions.""",
    },
    "pi": {
        "FRONTMATTER": """\
---
name: make-skill
description: "Author or revise a skill Pi can use, using a trigger/structure/steering/pruning rubric. Use when the user wants to create a new skill, improve or simplify an existing one, or complains a skill isn't triggering or isn't being followed."
---""",
        "TRIGGER_SECTION": """\
Ask (or infer and confirm): model-invoked, user-invoked, or both?

- **Model-invoked** costs context in every session and can silently not fire; it buys hands-off convenience. **User-invoked** is reliable and cheap but the user must remember it exists.
- The frontmatter `description` IS the model-invoke surface. Write it as: what the skill does + "use when" + the literal phrases the user actually says (steal them from real transcripts). For user-invoked-only skills, keep the description one terse line.""",
        "STRUCTURE_REF_NOTE": (
            "- Supporting material (schemas, long examples, lookup tables) does "
            "NOT go in the body. Pi implements the Agent Skills standard, whose "
            "documented subdirectory for this is `references/` (not `ref/` — "
            "this repo's claude/copilot convention). Put it in "
            "`~/dotfiles/pi/skills/<skill>/references/<topic>.md` and point to "
            "it from the step that needs it. Reference files need their own "
            "symlink lines (step 5)."
        ),
        "STEERING_WIDGET_NOTE": "",
        "VERIFY_PROBE": """\
Probe with `pi -p '<a real trigger phrase>'` for model-invoke, or
`pi -p '/skill:<name> <args>'` for a direct user-typed invocation (Pi
registers every discovered skill as a `/skill:<name>` command —
`docs/skills.md`'s "Skill Commands"). Check the output (and reasoning, if
visible) repeats your leading words back. If the agent skips a step, that
step needs splitting or stronger steering — not more prose.""",
        "PLUMBING_STEPS": """\
1. File lives at `~/dotfiles/pi/skills/<name>/SKILL.md` (same for any reference files, under `~/dotfiles/pi/skills/<name>/references/`). `pi/skills` is already wired into `links.toml` as one `dir = true` row and into `pi/settings.json`'s `skills` array — a new file under it needs no new `links.toml` row of its own, just the file.
2. If this skill should also be shared with agy (a skill agy itself should offer, not just Pi), author it at `agy/skills/<name>/SKILL.md` instead and follow agy's own plumbing steps — Pi still falls back to `agy/skills/` (`pi/settings.json`) for anything not under `pi/skills/`.
3. Conventional commit, scope `pi` (or `agy`, if authored there instead): `feat` for a new skill, `refactor`/`docs` for revisions.""",
    },
}

SPEC_PARAMS: dict[str, dict[str, str]] = {
    "claude": {
        "FRONTMATTER": """\
---
name: spec
description: "Turn a vague coding task into a structured specification (objective, context, inputs, output format, constraints, evaluation criteria, edge cases, verification steps) before generation begins. Use when the user wants to formalize a task, write a spec, or invokes /spec."
argument-hint: [task description]
allowed-tools: [Read, Glob, Grep, Write, AskUserQuestion, "Bash(python3 ~/.claude/scripts/second_opinion.py:*)"]
---""",
        "OPENING_LINE": (
            "If $ARGUMENTS is empty, use the task under discussion in the "
            "conversation; if neither exists, ask what to spec."
        ),
        "ARGS_TOKEN": "$ARGUMENTS",
        "STEP2_BODY": (
            "For each field you can't confidently fill, ask one at a time, "
            "applying CLAUDE.md's recommendation-first convention. Skip fields "
            "already unambiguous from context — a trivial task doesn't need "
            "all eight interrogated."
        ),
        "STEP3_ESCALATION": (
            "A missing fact gets asked directly (step 2). A genuinely open "
            "branch — multiple viable designs, unclear tradeoffs, a decision "
            "that cascades into others — gets handed to `grill-me` (Skill "
            "tool): invoke it for that specific decision, with the blocked "
            "field's question as topic. Let it own its full protocol — Q&A, "
            "`--verify`, executor-readiness — don't hand-roll `grill.py` calls "
            "here. Decline grill-me's own clear-and-go offer — drafting isn't "
            "done yet, so grill-me resolving the branch doesn't get to be the "
            "last word."
        ),
        "BACKLOG_ITEM_REF": "`/backlog-item`",
        "STEP4_ASK": (
            'ask, via AskUserQuestion: "Start generation against this spec '
            'now?" — `Yes (recommended)` / `No, stop here`.'
        ),
        "STEP6_AUDIT_OFFER": (
            "Once verification passes (or is stopped-and-reported), ask via "
            'AskUserQuestion: "Run an audit pass for specification gaming?" — '
            "`Yes (recommended unless this is trivial)` / `No, done`. A yes "
            "reuses `/second-opinion`'s adversarial critique loop against the "
            "result and the spec's Objective — does it satisfy the letter "
            "while missing the intent? — rather than self-grading."
        ),
        "PLUMBING_STEPS": """\
1. File lives at `~/dotfiles/claude/commands/spec.md`.
2. Add a `[[link]]` entry (`src = "claude/commands/spec.md"`, `dest = "~/.claude/commands/spec.md"`, `harness = "claude"`) in `links.toml` next to the existing ones.
3. Create the live symlink now: `ln -s ~/dotfiles/claude/commands/spec.md ~/.claude/commands/spec.md`.
4. Conventional commit, scope `claude`: `feat`.""",
    },
    "opencode": {
        "FRONTMATTER": """\
---
description: "Turn a vague coding task into a structured specification (objective, context, inputs, output format, constraints, evaluation criteria, edge cases, verification steps) before generation begins. Use when the user wants to formalize a task, write a spec, or invokes /spec."
---""",
        "OPENING_LINE": (
            "If $ARGUMENTS is empty, use the task under discussion in the "
            "conversation; if neither exists, ask what to spec."
        ),
        "ARGS_TOKEN": "$ARGUMENTS",
        "STEP2_BODY": (
            "For each field you can't confidently fill, ask one at a time. "
            "When the plausible answers are enumerable (2–4 real options), "
            "use the `question` tool with your recommendation as the first "
            'option, labeled "(Recommended)". When the question is genuinely '
            "open-ended, state it directly, give your recommended answer with "
            "brief reasoning, and wait for the response. Skip fields already "
            "unambiguous from context — a trivial task doesn't need all eight "
            "interrogated."
        ),
        "STEP3_ESCALATION": (
            "A missing fact gets asked directly (step 2). A genuinely open "
            "branch — multiple viable designs, unclear tradeoffs, a decision "
            "that cascades into others — gets handed to `grill-me`: load it "
            '(`skill({ name: "grill-me" })`) for that specific decision, with '
            "the blocked field's question as topic. Let it own its full "
            "protocol — Q&A, `--verify`, executor-readiness — don't hand-roll "
            "`grill.py` calls here. Decline grill-me's own clear-and-go offer "
            "— drafting isn't done yet, so grill-me resolving the branch "
            "doesn't get to be the last word."
        ),
        "BACKLOG_ITEM_REF": "`backlog-item`",
        "STEP4_ASK": (
            'ask, via the `question` tool: "Start generation against this '
            'spec now?" — `Yes (recommended)` / `No, stop here`.'
        ),
        "STEP6_AUDIT_OFFER": (
            "Once verification passes (or is stopped-and-reported), ask via "
            'the `question` tool: "Run an audit pass for specification '
            'gaming?" — `Yes (recommended unless this is trivial)` / `No, '
            "done`. A yes checks whether the result satisfies the letter "
            "while missing the Objective, via adversarial critique — prefer "
            "the native path since you're already running inside opencode: "
            "spawn the `adversary` agent (Task tool, no subprocess) with the "
            "spec's Objective, the result, and a prompt that argues the "
            "result games the spec rather than satisfies it. Fall back to "
            "`/second-opinion`'s `second_opinion.py review` loop only if "
            "`adversary` is erroring or unavailable."
        ),
        "PLUMBING_STEPS": """\
1. File lives at `~/dotfiles/opencode/command/spec.md`.
2. Add a `[[link]]` entry (`src = "opencode/command/spec.md"`, `dest = "~/.config/opencode/commands/spec.md"`, `harness = "opencode"`) in `links.toml` next to the existing ones.
3. Conventional commit, scope `opencode`: `feat`.""",
    },
    "pi": {
        "FRONTMATTER": """\
---
name: spec
description: "Turn a vague coding task into a structured specification (objective, context, inputs, output format, constraints, evaluation criteria, edge cases, verification steps) before generation begins. Use when the user wants to formalize a task, write a spec, or invokes /spec."
---""",
        "OPENING_LINE": (
            "If the user didn't name a specific task (and isn't asking to "
            "formalize something already under discussion), ask what to spec "
            "before proceeding. Otherwise spec the named task, or the task "
            "under discussion in the conversation."
        ),
        "ARGS_TOKEN": "the named task",
        "STEP2_BODY": (
            "For each field you can't confidently fill, ask one at a time. "
            "Use the `question` tool for an enumerable choice (2–4 real "
            "options), your recommendation as the first option labeled "
            '"(Recommended)" — it is a hard error (not a silent fallback) in '
            "headless `-p`/JSON modes, since there's no UI to prompt through "
            "there; state it in plain text with your recommendation instead "
            "when running headless. When the question is genuinely "
            "open-ended, state it directly, give your recommended answer with "
            "brief reasoning, and wait for the response. Skip fields already "
            "unambiguous from context — a trivial task doesn't need all eight "
            "interrogated."
        ),
        "STEP3_ESCALATION": (
            "A missing fact gets asked directly (step 2). A genuinely open "
            "branch — multiple viable designs, unclear tradeoffs, a decision "
            "that cascades into others — gets handed to `grill-me`: load it "
            "via `/skill:grill-me` (Pi registers every discovered skill as a "
            "`/skill:<name>` command, confirmed in `docs/skills.md`; the "
            "agent can also load it on its own once the topic matches the "
            "skill's description in the `<available_skills>` block, per the "
            'same doc\'s "How Skills Work") for that specific decision, with '
            "the blocked field's question as topic. Let it own its full "
            "protocol — Q&A, `--verify`, executor-readiness — don't hand-roll "
            "`grill.py` calls here. Decline grill-me's own clear-and-go offer "
            "— drafting isn't done yet, so grill-me resolving the branch "
            "doesn't get to be the last word."
        ),
        "BACKLOG_ITEM_REF": "`backlog-item`",
        "STEP4_ASK": (
            'ask, in plain text with a recommendation: "Start generation '
            'against this spec now?" — recommend yes.'
        ),
        "STEP6_AUDIT_OFFER": (
            "Once verification passes (or is stopped-and-reported), ask in "
            'plain text with a recommendation: "Run an audit pass for '
            'specification gaming?" — recommend yes unless this is trivial. '
            "A yes reuses `/second-opinion`'s `second_opinion.py review` loop "
            "against the spec's Objective and the result — does it satisfy "
            "the letter while missing the intent? Pi's own design principles "
            "rule out built-in sub-agents (`docs/usage.md`'s \"Design "
            'Principles": "it intentionally does not include built-in MCP, '
            "sub-agents, permission popups, plan mode, to-dos, or background "
            'bash"), so there is no native path to spawn an adversarial '
            "critique agent of its own the way opencode's `adversary` agent "
            "does — this always goes through the shared `second_opinion.py` "
            "critique loop instead."
        ),
        "PLUMBING_STEPS": """\
1. File lives at `~/dotfiles/pi/skills/spec/SKILL.md`. `pi/skills` is already wired into `links.toml` as one `dir = true` row and into `pi/settings.json`'s `skills` array — a new file under it needs no new `links.toml` row of its own, just the file.
2. Conventional commit, scope `pi`: `feat`.""",
    },
}

STANDUP_PARAMS: dict[str, dict[str, str]] = {
    "claude": {
        "FRONTMATTER": """\
---
name: standup
description: "Gather assigned work, chat signal, calendar events, pending replies, git commits, and backlog activity into a daily standup draft, saved to a dated file. Use when the user says 'standup', 'prep for standup', or wants their daily status pulled together."
allowed-tools: [Read, Write, Glob, Grep, "Bash(python3 ~/.claude/scripts/standup.py:*)", "Bash(python3 ~/.claude/scripts/dev_status.py:*)", "Bash(git log:*)"]
---""",
        "FETCH_MECHANISM": """\
```
python3 ~/.claude/scripts/standup.py fetch [--date YYYY-MM-DD]
```

`--date` overrides the reference date (defaults to today) — use it after a
gap longer than one working day (holiday, PTO) where the default
last-working-day boundary would land on the wrong day.""",
        "RECONCILE_BODY": """\
Apply CLAUDE.md's pending-item status-transition rule (one step at a time,
never jump straight to `resolved`). For each entry in `pending_items_open`,
check `chat_thread_updates` / `email_thread_updates` (and
`messages`/`email_correspondence` for anything those targeted fetches
missed) for a reply. Propose the transition to the user in chat first —
nothing gets written until they confirm, since "was this actually answered"
is a judgment call, not a pattern match. Only move an item to `resolved`
when the user confirms it's actually done, and record the `outcome`.

For anything in the fetched data that looks like a new item worth tracking
across days (an email/chat message still awaiting a reply, an access
request not yet approved) but isn't already in `pending_items_open`,
propose adding it per CLAUDE.md's pending-item protocol.

`<id>` can be the pending item's slug — `dev_status.py`'s cross-section
numbering (visible via `/dashboard`) also works, but its numbers shift as
items change, so prefer the slug here since `standup.py`'s `fetch` output
already gives you it directly.""",
    },
    "opencode": {
        "FRONTMATTER": """\
---
description: "Gather assigned work, chat signal, calendar events, pending replies, git commits, and backlog activity into a daily standup draft, saved to a dated file. Use when the user says 'standup', 'prep for standup', or wants their daily status pulled together."
---""",
        "FETCH_MECHANISM": """\
```
python3 ~/.claude/scripts/standup.py fetch [--date YYYY-MM-DD]
```

`--date` overrides the reference date (defaults to today) — use it after a
gap longer than one working day (holiday, PTO) where the default
last-working-day boundary would land on the wrong day.""",
        "RECONCILE_BODY": """\
Status moves one step at a time: `waiting_for_reply` → `reply_received` →
`resolved`. A reply landing doesn't mean the thing is closed out — it means
it needs a look. Don't jump straight to `resolved` on a hunch.

For each entry in `pending_items_open`, check `chat_thread_updates` /
`email_thread_updates` (and `messages`/`email_correspondence` for anything
those targeted fetches missed) for a reply. Propose the transition to the
user in chat first — nothing gets written until they confirm, since "was
this actually answered" is a judgment call, not a pattern match:

```
python3 ~/.claude/scripts/dev_status.py pending update <id> '{"status": "reply_received"}'
```

Only move an item to `resolved` when the user confirms it's actually done,
and record what happened:

```
python3 ~/.claude/scripts/dev_status.py pending update <id> '{"status": "resolved", "outcome": "what actually happened"}'
```

For anything in the fetched data that looks like a new item worth tracking
across days (an email/chat message still awaiting a reply, an access
request not yet approved) but isn't already in `pending_items_open`,
propose adding it:

```
python3 ~/.claude/scripts/dev_status.py pending add '{"id", "description", "kind", "source_ref": {...}, "context", "next_steps": [...]}'
```

`<id>` can be the pending item's slug — `dev_status.py`'s cross-section
numbering (visible via `/dashboard`) also works, but its numbers shift as
items change, so prefer the slug here since `standup.py`'s `fetch` output
already gives you it directly.

`kind` is one of `email`, `chat`, `approval`. `source_ref` is a structured
object appropriate to the kind (e.g. `{"to", "subject", "sent_date"}` for
email) — not a free-text string.""",
    },
    "pi": {
        "FRONTMATTER": """\
---
name: standup
description: "Gather assigned work, chat signal, calendar events, pending replies, git commits, and backlog activity into a daily standup draft, saved to a dated file. Use when the user says 'standup', 'prep for standup', or wants their daily status pulled together."
---""",
        "FETCH_MECHANISM": """\
Call the `standup` tool with action `fetch` — never `standup.py` via bash.

Its optional `date` field (`YYYY-MM-DD`) overrides the reference date
(defaults to today) — use it after a gap longer than one working day
(holiday, PTO) where the default last-working-day boundary would land on the
wrong day.""",
        "RECONCILE_BODY": """\
Status moves one step at a time: `waiting_for_reply` → `reply_received` →
`resolved`. A reply landing doesn't mean the thing is closed out — it means
it needs a look. Don't jump straight to `resolved` on a hunch.

For each entry in `pending_items_open`, check `chat_thread_updates` /
`email_thread_updates` (and `messages`/`email_correspondence` for anything
those targeted fetches missed) for a reply. Propose the transition to the
user in chat first — nothing gets written until they confirm, since "was
this actually answered" is a judgment call, not a pattern match — then call
the `dev_status` tool with `action: "pending_update", slug: "<id>", patch:
{"status": "reply_received"}`.

Only move an item to `resolved` when the user confirms it's actually done,
and record what happened: `action: "pending_update", slug: "<id>", patch:
{"status": "resolved", "outcome": "what actually happened"}`.

For anything in the fetched data that looks like a new item worth tracking
across days (an email/chat message still awaiting a reply, an access
request not yet approved) but isn't already in `pending_items_open`,
propose adding it: `action: "pending_add", patch: {"id", "description",
"kind", "source_ref": {...}, "context", "next_steps": [...]}`.

`<id>` must be the pending item's real slug — `standup.py`'s `fetch` output
already gives you it directly. The `dev_status` tool refuses a numeric
`slug` on `pending_update` outright (call it with `action: "show", slug:
"<N>"` first if you only have a cross-section number, e.g. from the
dashboard).

If the `dev_status` tool is genuinely unavailable, fall back to bash —
`python3 ~/.claude/scripts/dev_status.py pending update <id> '{...}'` /
`pending add '{...}'`, same JSON shapes as above.

`kind` is one of `email`, `chat`, `approval`. `source_ref` is a structured
object appropriate to the kind (e.g. `{"to", "subject", "sent_date"}` for
email) — not a free-text string.""",
    },
}

TO_TICKETS_PARAMS: dict[str, dict[str, str]] = {
    "claude": {
        "FRONTMATTER": """\
---
name: to-tickets
description: "Decompose a plan or spec into multiple linked dev_status.py backlog items — vertical-slice/tracer-bullet tickets joined by blocked_by edges — after confirming the breakdown with the user. Use when the user wants a plan broken into tickets, wants a spec turned into backlog items, or invokes /to-tickets."
argument-hint: [plan or spec file path]
allowed-tools: [Read, Glob, Grep, Write, AskUserQuestion, "Bash(python3 ~/.claude/scripts/to_tickets_runner.py:*)"]
---""",
        "OPENING_LINE": (
            "If $ARGUMENTS is empty, use the plan or spec under discussion in "
            "the conversation; if neither exists, ask what to decompose."
        ),
        "STEP4_CONFIRM_MECHANISM": "Use `AskUserQuestion` for this.",
        "RUNNER_INVOKE": (
            "run `python3 ~/.claude/scripts/to_tickets_runner.py run <that path>`."
        ),
        "RUNNER_MKDIR_OWNER": "`to_tickets_runner.py`",
        "RESUME_MECHANISM": (
            "simply re-invoke the same `to_tickets_runner.py run` command on "
            "the same batch file —"
        ),
        "SHELL_SAFETY_NOTE": (
            "Every batch-JSON write and every `to_tickets_runner.py` "
            "invocation you make inherits this repo's shell-safety rule "
            "(CLAUDE.md): a summary/context field containing an apostrophe "
            "never goes into an inline single-quoted shell string — write "
            "the JSON with the `Write` tool, never construct it inline in a "
            "`Bash` command."
        ),
    },
    "opencode": {
        "FRONTMATTER": """\
---
description: "Decompose a plan or spec into multiple linked dev_status.py backlog items — vertical-slice/tracer-bullet tickets joined by blocked_by edges — after confirming the breakdown with the user. Use when the user wants a plan broken into tickets, wants a spec turned into backlog items, or invokes /to-tickets."
---""",
        "OPENING_LINE": (
            "If $ARGUMENTS is empty, use the plan or spec under discussion in "
            "the conversation; if neither exists, ask what to decompose."
        ),
        "STEP4_CONFIRM_MECHANISM": "Use the `question` tool for this.",
        "RUNNER_INVOKE": (
            "run `python3 ~/.claude/scripts/to_tickets_runner.py run <that path>`."
        ),
        "RUNNER_MKDIR_OWNER": "`to_tickets_runner.py`",
        "RESUME_MECHANISM": (
            "simply re-invoke the same `to_tickets_runner.py run` command on "
            "the same batch file —"
        ),
        "SHELL_SAFETY_NOTE": (
            "Every batch-JSON write and every `to_tickets_runner.py` "
            "invocation you make inherits this repo's shell-safety rule: a "
            "summary/context field containing an apostrophe never goes into "
            "an inline single-quoted shell string — write the JSON file "
            "directly, never construct it inline in a shell command."
        ),
    },
    "pi": {
        "FRONTMATTER": """\
---
name: to-tickets
description: "Decompose a plan or spec into multiple linked dev_status.py backlog items — vertical-slice/tracer-bullet tickets joined by blocked_by edges — after confirming the breakdown with the user. Use when the user wants a plan broken into tickets, wants a spec turned into backlog items, or invokes /to-tickets."
---""",
        "OPENING_LINE": (
            "If the user didn't name a specific plan or spec (and isn't "
            "asking to decompose something already under discussion), ask "
            "what to decompose before proceeding. Otherwise decompose the "
            "named plan/spec, or the one under discussion in the "
            "conversation."
        ),
        "STEP4_CONFIRM_MECHANISM": (
            "State the options in plain text with your recommendation, and "
            "wait for the user's reply, or use the `question` tool when "
            "running interactively — it is a hard error (not a silent "
            "fallback) in headless `-p`/JSON modes, since there's no UI to "
            "prompt through there."
        ),
        "RUNNER_INVOKE": (
            "call the `to_tickets` tool with action `run` and that path as "
            "`batchFile` — never `to_tickets_runner.py` via bash."
        ),
        "RUNNER_MKDIR_OWNER": "the `to_tickets` tool's runner",
        "RESUME_MECHANISM": (
            "simply call `to_tickets` again with the same `batchFile` —"
        ),
        "SHELL_SAFETY_NOTE": (
            "Every batch-JSON write inherits this repo's shell-safety rule: "
            "a summary/context field containing an apostrophe never goes "
            "into an inline single-quoted shell string — write the JSON file "
            "directly, never construct it inline in a shell command. The "
            "`to_tickets` tool takes the path as a discrete argument, so no "
            "shell parses it."
        ),
    },
}

SKILL_PARAMS: dict[str, dict[str, dict[str, str]]] = {
    "dashboard": DASHBOARD_PARAMS,
    "grill-me": GRILL_ME_PARAMS,
    "backlog-item": BACKLOG_ITEM_PARAMS,
    "make-skill": MAKE_SKILL_PARAMS,
    "spec": SPEC_PARAMS,
    "standup": STANDUP_PARAMS,
    "to-tickets": TO_TICKETS_PARAMS,
}
