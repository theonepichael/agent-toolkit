# MIGRATION.md — what remains before this toolkit stands on its own

**Update 2026-09-07: the cutover (item 5) has run for real.** This repo
began as a snapshot taken from a personal dotfiles repository on
2026-09-02; as of today, the personal machine that authored both repos
installs its shared harness tooling from *this* repository, not dotfiles
-- verified via both repos' `install.py --check-links` reporting clean.
The sections below are kept as the historical record of how that happened
and what remains (coworker access, item 3) -- see "What done looks like"
for the exact final shape, which differs slightly from what this file
originally described as done-criteria.

This file records what is left and, more importantly, **the order**, because
the order is not obvious and getting it wrong is what strands a migration
halfway.

## The state today

Upstream dotfiles is the working repository; changes land there first. Until
2026-09-03 that meant reconciling this repo by hand every time — re-deriving,
per pass, which files belong here, which are personal and must not ship, and
which have diverged in both places. A reconciliation pass completed that day
was stale again within hours: three further upstream commits the same day put
this repo behind, because closing the gap had been a manual act that nothing
repeated.

Item 1 below (`scripts/sync_from_dotfiles.py`) fixes that mechanism, not just
that one gap — reconciliation is now a repeatable command with the blocklist,
conflict-set derivation, and classification rule encoded as code, so the next
sync doesn't re-derive any of it by hand. The repo still lags upstream between
runs (nothing runs the sync automatically), which is why item 5 still exists:
a sync immediately before cutover, using the now-cheap tool, is what makes the
final reconciliation accurate at the moment it matters.

## The order, and why it is this order

### 1. Make reconciliation a command, not a project — DONE (2026-09-03)

`scripts/sync_from_dotfiles.py` encodes the file blocklist, the conflict-set
derivation, and the classification rule as code, so they no longer live in
whoever happens to be running the sync that week. It reports by default and
requires `--apply` to write, and it tracks its own last-synced state in
`scripts/.sync-state.json` so the next run doesn't need a hand-verified
starting point.

The reason this went first: the three items below take real time, and
upstream does not stop while they are worked. Without a repeatable sync, the
repo would drift underneath the migration and the final pre-cutover
reconciliation would be a large, risky, hand-audited diff instead of one
command (see item 5).

### 2. Split the shared instructions from the personal ones — DONE (2026-09-03)

The toolkit now ships `claude/CORE_INSTRUCTIONS.md` — the shareable workflow
content only, symlinked directly to every harness with no generation step
of its own. Personal policy and machine-specific content (the repo-prefix
table's concrete values, cross-machine sync, the personal-project git
bundling preference, the watchcommit auto-commit guard) moved to dotfiles'
own `claude/personal-overlay.md`, composed with `CORE_INSTRUCTIONS.md` by a
new `claude/scripts/gen_core_instructions.py` into the
`claude/global-instructions.md` dotfiles actually symlinks out — never
synced to agent-toolkit (`scripts/sync_from_dotfiles.py`'s `EXCLUDE`).

### 3. Publish the repository — remote and scan DONE, coworker access still pending

The split exists so coworkers can clone this. A private GitHub remote
(`github.com/theonepichael/agent-toolkit`) now exists, with zero collaborators
added yet.

The work here was mostly not the remote itself — it was a secret scan across
the **full history** pushed, not just the working tree or the initial
snapshot's era, since history carries everything ever committed and this
repo's history was imported wholesale. That scan (gitleaks, full history) came
back clean. A separate hardcoded-path audit of the working tree, done as part
of the same pass, was not clean — it found this-machine-specific paths in
three files that would have silently misbehaved for a coworker; those are
fixed, with a regression test added so the class doesn't recur.

Remaining before a coworker actually gets in: a scratch-clone install
verification (done, two harnesses), the onboarding entry point (this file and
the README, being brought current now), and then adding named collaborators —
deliberately last, since two more coworker-facing issues turned up mid-audit
(`atk-pi-prompts-dotfiles-refs`) and should land first.

Independent of items 1 and 2; was done alongside both.

### 4. Write the handover order and its rollback — DONE (2026-09-03, executed 2026-09-07)

The cutover swaps ownership of roughly 114 symlinks on a live machine. Doing
that with no written step order and no tested rollback is how a machine ends
up half-migrated, with some links pointing at the old tree and some at the
new, and no way to tell which state it is in.

Three things must exist before the cutover runs:

- the exact step order, including where exclusive-directory declarations are
  pruned relative to deleting the old trees, and a stated reason for that
  order rather than the reverse
- a description of what a half-completed handover looks like, and the command
  that shows you
- a rollback that has actually been executed, not merely written

This goes last of the four because it describes the cutover, and the cutover's
shape depends on decisions made in items 1 through 3.

Order and rollback settled in the `meta-agent-toolkit-handover-safety` grill
session (2026-09-03, plan at
`~/.claude/data/grill/2026-09-03-meta-agent-toolkit-handover-safe-plan.md`)
and round-trip tested on a scratch HOME that same day. What that plan's
verification did **not** cover, because it hadn't happened yet: a real
`install-with-agent-toolkit.sh` run always runs dotfiles' installer second to
reassert the 4 personal-overlay destinations, and dotfiles' own orphan-cleanup
had no guard against deleting a destination another repo's installer had just
claimed in the same run. Both gaps surfaced only when item 5 actually executed
live on 2026-09-07 (`meta-agent-toolkit-wrapper-enforcement` and its follow-on
orphan-cleanup fix, both repos) — see those items' commits for the fix and a
new regression test each. The scratch-HOME test proved the *symlink-ownership*
handover safe; it did not exercise the wrapper script or its second-install
reassert step, which is exactly where both incidents lived.

### 5. Sync once, then cut over — DONE (2026-09-07)

Ran `install-with-agent-toolkit.sh` for real on the machine that authors both
repos. Both repos' `install.py --check-links` report clean. Collaborator
access (item 3) is still the one open item.

## What done looks like

The local machine's harness config resolves to this repository rather than to
dotfiles for every destination except the 4 that are supposed to keep
resolving to dotfiles' composed `global-instructions.md` (`~/.claude/CLAUDE.md`
and its copilot/gemini/pi equivalents) — that's the personal-overlay design,
not an unfinished cutover, and `install-with-agent-toolkit.sh` is what makes
it durable across future installs, not just this one. The old harness trees
are gone from dotfiles rather than duplicated, except for what's still a
genuine dependency there (install.py's own repo-local script imports,
one-time config seeds, and the personal-overlay composition itself) — `git
log` on `pi/`, `copilot/`, `agy/`, and the pruned parts of `claude/`
in dotfiles shows the 2026-09-04 deletion commit for the full picture.
`sync_from_dotfiles.py` keeps one narrow, permanent, and deliberate upstream
relationship — `CORE_INSTRUCTIONS.md` is authored in dotfiles and synced in —
which is the intended final shape, not a leftover. What's still open: a
coworker cloning this repo can't yet get in, since no collaborators are added
(item 3).

## A note on why this file exists

The ordering above is not arbitrary and it is not recoverable from the
individual pieces of work. Each one looks independently doable, which is
exactly the trap: doing the handover safety work first produces a written
order that the other three then invalidate, and doing the cutover before the
sync tool means reconciling a moving target by hand under time pressure.
