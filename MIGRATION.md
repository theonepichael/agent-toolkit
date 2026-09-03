# MIGRATION.md — what remains before this toolkit stands on its own

This repository is not yet independent. It began as a snapshot taken from a
personal dotfiles repository on 2026-09-02, and that dotfiles repository is
still where the harness work actually happens. Until the remaining work below
is done, this repo is a copy that drifts, not a source.

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

### 2. Split the shared instructions from the personal ones

The toolkit currently ships a personal global-instructions file verbatim.
Some of it is genuinely shareable workflow. Some is personal policy, and some
is specific to one machine. Nobody else can adopt the toolkit until that
boundary exists and is written down.

The classification is not mechanical at the margins — a git worktree policy is
arguably shareable, a per-machine path is not, and several sections sit
between. Decide the boundary deliberately and record the reasoning, because
the next person to add a section needs the rule, not just the outcome.

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

### 4. Write the handover order and its rollback — last before cutover

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

### 5. Sync once, then cut over

Run the tool from item 1 to bring this repo current, then perform the cutover
using the order from item 4. The sync immediately beforehand is the point of
item 1 existing: it makes the final reconciliation cheap enough to do at the
last possible moment, when it is most accurate.

## What done looks like

The local machine's harness config resolves to this repository rather than to
dotfiles, the old harness trees are gone from dotfiles rather than duplicated,
a coworker can clone this repo and install it without access to anything
personal, and the reconciliation tool has no upstream left to reconcile from.

## A note on why this file exists

The ordering above is not arbitrary and it is not recoverable from the
individual pieces of work. Each one looks independently doable, which is
exactly the trap: doing the handover safety work first produces a written
order that the other three then invalidate, and doing the cutover before the
sync tool means reconciling a moving target by hand under time pressure.
