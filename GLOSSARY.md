# GLOSSARY.md

A short list of terms that are either used in more than one sense across
this repo, or sound related without being the same thing. Deliberately not
comprehensive — most domain terms here (`backlog item` vs. `pending item`,
`gate`/`gate-set`/`gate-pass`, `claim`/`claimed_by`, `out-of-scope`,
`rev`/`--if-rev`, `worktree`) are already single-sourced and consistently
documented in `agent-scripts/dev_status_impl.py`'s docstrings and in
`claude/CORE_INSTRUCTIONS.md`, so a second definition here would just be
one more place for that description to go stale. This file exists only
for the terms where that single-sourcing doesn't hold.

## `acceptance` vs. `gate`

`acceptance` is contract-derived criteria set on an item **before any work**
(`acceptance-set <id>`, or `add`'s `acceptance` field), then frozen for the
item's life — a worker can only *append* gate criteria, never drop or reword
an acceptance one. A `gate` is the verification checkpoint `gate-set`/
`gate-pass` manage; on an acceptance-bearing item the gate is the acceptance
plus the worker's extras, and `done`/`approve` refuse unless the passed gate
covers every acceptance criterion. They are distinct fields: `acceptance` is
the locked contract, `gate` is how it (plus any worker additions) is
satisfied. An `acceptance.independent_verifier` item additionally needs a
verifier token — a 0600 file whose *path* (never its content) is passed to
`gate-pass --verifier-token-file` — so a non-worker session holds the verifier
role.

## `contract`

Three unrelated things are called a contract here:

- **Acceptance contract** — the repo's own written rules (its AGENTS.md,
  README, PROJECT_PLAN.md, ...) that an item's `acceptance` criteria cite
  clause by clause. `dev_status.py contract-touch` lists which of those cited
  files a diff changes.
- **CLI contract** — a script's real flags, subcommands, exit codes and env
  vars, fingerprinted in `agent-scripts/contract_fingerprints.json`.
  `gen_interfaces.py --check` exits 3 when a script's contract changed
  without its docs being re-read.
- **Isolation contract** — what a `second_opinion.py` review backend must
  satisfy to be eligible (nothing of yours in write reach, none of your own
  instruction files handed to the reviewer; see `llm_backends.py`);
  `second_opinion.py detect` reports it per backend.

## `dispatch`

Three distinct senses share this word — know which one a given doc means:

- **Argparse routing.** Inside a script like `grill.py`, `second_opinion.py`,
  or `herdr_delegate.py`, "parse arguments and dispatch" just means routing
  to the subcommand handler. Generic, not a domain concept.
- **Cross-harness dispatch.** In README.md ("a review whose dispatched
  backend..."), dispatch means picking which harness/backend a request
  runs against — the `second_opinion.py`/`herdr_delegate.py` sense.
- **Swarm fan-out.** Related to but not synonymous with the swarm skill's
  fan-out (one orchestrator spreading a queue across N workers) — a swarm
  worker is one thing a dispatch can send work to, not the definition of
  dispatch itself.

## `handoff`

Three distinct steps carry this name:

- **Foreman handoff** — `herdr_delegate.py launch --handoff` (pi, single
  `--slug` only): the worker stops after its commit gate and prints a
  `FOREMAN-HANDOFF slug=... branch=... commit=... worktree=...` line, and
  the foreman session does the merge, push and close.
- **backlog-item step 7, "Handoff"** — the decision of *who implements* a
  settled plan: this session, a fresh cheaper session, or an external
  executor.
- **Clear-and-go** — the cheaper-session branch of that step:
  `grill.py mark-pending-execution` flags the plan, and the next session's
  SessionStart `pending-plan` hook points it back at `/backlog-item <slug>`.

## `prefix`

Almost always a plain string-prefix reference in code comments and
docstrings (`argv0.removeprefix`, line-prefix rendering, "prefix onto the
error message"). The one domain-specific sense is the **backlog item
prefix**: the kebab-case string at the front of a backlog id (`atk-`,
`iron-lb-`, `meta-`, ...) that maps 1:1 to a repo, per the prefix table in
`claude/CORE_INSTRUCTIONS.md`, and gates which items `--swarm[=N]` is
allowed to pull in. When
this file or any other doc says "prefix" without qualification, check which
sense is meant — don't assume the domain one.

## `seed` / `seed content` vs. `composed instructions`

Sound related, aren't:

- **Seed / seed content** — settings.json (or similar config) content that
  gets installed or retained at install time. See `seed_hook_subset_guard.py`
  and README's "Seed Rewrite Guard".
- **Composed instructions** — a global instructions file that a downstream
  repo builds by layering its own personal-policy overlay on top of
  `CORE_INSTRUCTIONS.md` (e.g. a global-instructions.md built from
  `CORE_INSTRUCTIONS.md` plus a personal-overlay.md) and links to the harnesses
  itself. This repo never composes anything: it links `CORE_INSTRUCTIONS.md`
  out unmodified (AGENTS.md's opening paragraph), and `install.py` only
  skips linking over a composed file when that downstream checkout is
  present (`_personal_overlay_unwrapped`; see also the comment above `links.toml`'s
  Claude Code entries).

Seeding is about a harness's *config* (settings.json and the like) and
happens in this repo's installer; composing is about *instructions* and
happens in a downstream repo. Neither implies the other.

## workflow / adapter / invocation

The older "skill / command / slash command" vocabulary flattened three
levels into one name. This repository distinguishes them:

- **Workflow** — the user-facing capability and intent (e.g. `/dashboard`,
  `/grill-me`), independent of how any harness delivers it. Workflows are the
  unit this repository ships; the per-harness availability of each one is the
  matrix in `INTERFACES.md` ("Skill and command surface").
- **Adapter** — a harness-specific delivery of a workflow: the concrete file a
  harness loads (a Claude Code command, a Copilot skill, an OpenCode command
  *or* skill, an AGY skill, a Pi prompt or skill, a Codex skill). One workflow
  can have several adapters because harnesses discover and load content
  differently — OpenCode separates `opencode/command/` from `opencode/skills/`,
  and Pi separates `pi/prompts/` from `pi/skills/`.
- **Invocation** — how a workflow is triggered: a slash command, a skill
  match, a tool call, or a hook. The same workflow may be reachable by more
  than one invocation.

The shared workflows are generated by `gen_skills.py` (and
`gen_second_opinion.py` for `/second-opinion`), which render one template into
 each harness's adapter; hand-authored workflows instead live directly in
`claude/commands/`. "Skill" and "slash command" remain useful shorthand for an
adapter, but they name a delivery, not the workflow itself.
