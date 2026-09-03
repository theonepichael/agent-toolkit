# MIGRATION.md — what remains before this toolkit stands on its own

This repository is not yet independent. It began as a snapshot taken from a
personal dotfiles repository on 2026-09-02, and that dotfiles repository is
still where the harness work actually happens. Until the remaining work below
is done, this repo is a copy that drifts, not a source.

This file records what is left and, more importantly, **the order**, because
the order is not obvious and getting it wrong is what strands a migration
halfway.

## The state today

Upstream dotfiles is the working repository. Changes land there first and are
reconciled into this one by hand. That reconciliation is the problem, not a
detail: it has to re-derive, every time, which files belong here, which are
personal and must not ship, and which have diverged in both places.

The consequence is measurable rather than theoretical. A reconciliation pass
was completed on 2026-09-03, and three further upstream commits the same day
put this repo behind again — missing a backlog prefix scheme, a delegation
script, a new command, and a safety guard in the swarm tooling. The gap
reopened within hours of being closed, because closing it was a manual act
that nothing repeats.

## The order, and why it is this order

### 1. Make reconciliation a command, not a project

**Do this first.** Everything after it is safer once it is done, and nothing
after it is safe while it is not.

Today the sync is a throwaway script rewritten per pass, so the file
blocklist, the conflict set, and the rule for classifying a changed file all
live in whoever is doing it. Encode them as data in a maintained tool that
lives here — this repo is the thing being synced *into*, and a coworker
cloning it has no dotfiles checkout to run the tool from.

The reason this is first: the three items below take real time, and upstream
does not stop while they are worked. Without a repeatable sync, the repo drifts
underneath the migration and the final pre-cutover reconciliation becomes a
large, risky, hand-audited diff instead of one command.

### 2. Split the shared instructions from the personal ones

The toolkit currently ships a personal global-instructions file verbatim.
Some of it is genuinely shareable workflow. Some is personal policy, and some
is specific to one machine. Nobody else can adopt the toolkit until that
boundary exists and is written down.

The classification is not mechanical at the margins — a git worktree policy is
arguably shareable, a per-machine path is not, and several sections sit
between. Decide the boundary deliberately and record the reasoning, because
the next person to add a section needs the rule, not just the outcome.

### 3. Publish the repository

The split exists so coworkers can clone this. Right now there is no remote, so
that is unreachable and the whole exercise has no payoff.

The work here is mostly not the remote itself. It is a secret scan across the
**full history** to be pushed — not the working tree, and not the era of the
initial snapshot. History carries everything ever committed, and this repo's
history was imported wholesale.

Independent of items 1 and 2; can be done alongside either.

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
