# GLOSSARY.md

A short list of terms that are either used in more than one sense across
this repo, or sound related without being the same thing. Deliberately not
comprehensive — most domain terms here (`backlog item` vs. `pending item`,
`gate`/`gate-set`/`gate-pass`, `claim`/`claimed_by`, `out-of-scope`,
`rev`/`--if-rev`, `worktree`) are already single-sourced and consistently
documented in `agent-scripts/dev_status_impl.py`'s docstrings and in
CLAUDE.md, so a second definition here would just be one more place for
that description to go stale. This file exists only for the terms where
that single-sourcing doesn't hold.

## `dispatch`

Three distinct senses share this word — know which one a given doc means:

- **Argparse routing.** Inside a script like `grill.py`, `second_opinion.py`,
  or `herdr_delegate.py`, "parse arguments and dispatch" just means routing
  to the subcommand handler. Generic, not a domain concept.
- **Cross-harness dispatch.** In `docs/architecture/overview.md` and
  README.md ("a review whose dispatched backend..."), dispatch means
  picking which harness/backend a request runs against.
- **Swarm fan-out.** Related to but not synonymous with the swarm skill's
  fan-out mechanism (see `fan-out` below) — a swarm worker is one thing a
  dispatch can send work to, not the definition of dispatch itself.

## `prefix`

Almost always a plain string-prefix reference in code comments and
docstrings (`argv0.removeprefix`, line-prefix rendering, "prefix onto the
error message"). The one domain-specific sense is the **backlog item
prefix**: the kebab-case string at the front of a backlog id (`atk-`,
`iron-lb-`, `meta-`, ...) that maps 1:1 to a repo, per the prefix table in
CLAUDE.md, and gates which items `--swarm[=N]` is allowed to pull in. When
this file or any other doc says "prefix" without qualification, check which
sense is meant — don't assume the domain one.

## `seed` / `seed content` vs. `composed instructions`

Sound related, aren't:

- **Seed / seed content** — settings.json (or similar config) content that
  gets installed or retained at install time. See `seed_hook_subset_guard.py`
  and README's "Seed Rewrite Guard".
- **Composed instructions** — dotfiles layering its own personal-policy
  overlay (a claude/personal-overlay.md file in the dotfiles repo, not here)
  on top of `CORE_INSTRUCTIONS.md` before symlinking the result out to each
  harness's global instructions file. See AGENTS.md's opening paragraph.

Seeding happens at install time to a harness's own config; composing
happens in dotfiles before that config ever reaches this repo or a harness.
Neither implies the other.

## skill / command / slash command

Used interchangeably on purpose, not as drift: Claude Code calls these
"skills," other supported harnesses expose the same generated content as
"slash commands." README.md states this explicitly ("Standardized slash
commands and skills across harnesses"). Treat the three as one concept with
harness-specific names, not three concepts.
