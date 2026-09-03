---
description: "Author or revise a skill Pi can use, using a trigger/structure/steering/pruning rubric. Use when the user wants to create a new skill, improve or simplify an existing one, or complains a skill isn't triggering or isn't being followed."
---
Work through these steps in order. Don't draft the skill body before step 1 is decided.

Pi has no `pi/skills/` directory of its own in this repo: it's configured
(`pi/settings.json`'s `skills` array) to discover skills directly from
`agy/skills/` rather than keeping a duplicate copy — confirmed live: `pi -p
--skill /home/yanil/dotfiles/agy/skills --no-context-files "list every skill
available"` correctly discovered all of them, no per-name directory
convention or symlink needed. So a skill authored here is created (and
edited) at `agy/skills/<name>/SKILL.md`, the same file agy itself reads —
not a separate Pi-specific copy.

## 1. Trigger — decide how it starts

Ask (or infer and confirm): model-invoked, user-invoked, or both?

- **Model-invoked** costs context in every session and can silently not fire; it buys hands-off convenience. **User-invoked** is reliable and cheap but the user must remember it exists.
- The frontmatter `description` IS the model-invoke surface. Write it as: what the skill does + "use when" + the literal phrases the user actually says (steal them from real transcripts). For user-invoked-only skills, keep the description one terse line.

## 2. Structure — steps, then reference

- The body is a **procedure**: numbered/ordered steps in imperative voice, addressed to the agent.
- Supporting material (schemas, long examples, lookup tables) does NOT go in the body. Put it in `~/dotfiles/agy/skills/<skill>/references/<topic>.md` (agy's documented skill-folder convention — the directory Pi also reads) and point to it from the step that needs it.
- Keep the body under ~50 lines. If it branches into genuinely different workflows, split into separate skills instead of one branching monster — smaller skills also hide the end goal, which stops the agent from rushing past planning/questioning steps. (Splitting isn't the only way: grill-me gets the same effect inside one skill by forbidding plan-writing until every question is decided. Don't split a skill that demonstrably works.)

## 3. Steering — make it stick

- Use dense, industry-standard terms that carry priors ("vertical slice", "copy-once", "idempotent") instead of paragraphs of description. Pick words that *name the behavior you want*: the agent repeats them in its thinking and output, and every repetition re-anchors it on that behavior — that echo loop is what step 4 listens for.
- State the failure mode you're preventing as a direct instruction ("Pass the integer directly — do not look up the slug").
- Any step that needs a multi-choice decision from the user should phrase it as a plain conversational question: state the question, give a recommendation, wait for a plain-text reply. These skills are also read by agy, which at present exposes no structured-choice widget, so a multi-choice ask must not depend on a widget only Pi has — this repo's `question-tool.ts` extension is Pi-only; plain text is the portable form.

## 4. Verify in a fresh session

Probe with a headless run: `pi -p --skill /home/yanil/dotfiles/agy/skills --no-context-files '<a real trigger phrase>'` for model-invoke, or `pi -p --skill /home/yanil/dotfiles/agy/skills --no-context-files '/skill:<name> <args>'` for a direct user-typed invocation (Pi registers every discovered skill as a `/skill:<name>` command — `docs/skills.md`'s "Skill Commands"). Check the output (and reasoning, if visible) repeats your leading words back. If the agent skips a step, that step needs splitting or stronger steering — not more prose.

## 5. Plumbing (house convention)

1. Create or edit the repo file at `~/dotfiles/agy/skills/<name>/SKILL.md` with frontmatter (`name`, `description`) and the skill body — this is the shared file agy and Pi both read; there is no separate `pi/skills/<name>/SKILL.md` to keep in sync.
2. If `agy/skills/<name>/SKILL.md` doesn't already exist (a genuinely new skill, not a Pi-specific revision of an existing one), follow `agy`'s own `make-skill` plumbing steps to add its `[[link]]` entry in `links.toml` and its live symlink — Pi needs none of its own, since `pi/settings.json` already points at the whole `agy/skills/` directory.
3. Conventional commit, scope `agy` for the skill body itself (it's the same file Pi consumes); scope `pi` only if this pass also touched `pi/settings.json` or another Pi-specific file.

## 6. Pruning (every revision, not just creation)

- **Single source of truth**: never restate what the shared instructions file or another skill already says — point to it.
- **Remove sediment**: delete references to scripts, paths, or behaviors that no longer exist.
- **Deletion test**: if a paragraph looks decorative, remove it and rerun the step-4 probe. Identical behavior → it was a no-op; keep it deleted.
