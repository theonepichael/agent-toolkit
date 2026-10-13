# The data-migration pattern

This file records the pattern the toolkit-home migration used, so the next
data migration can follow it without re-deriving it. The implementation was
deleted with the rest of the cutover machinery — `git log` is the reference.

## A write-ahead journal, keyed by run id

Every migration run gets a unique id and an append-only journal under its own
directory (`~/.local/state/agent-toolkit/migrations/<id>/`). Each step writes
a `begin` record before it mutates anything and a `done` record only after the
mutation is durable (file fsynced, directory fsynced). An inventory of every
pre-migration digest is written read-only before the first mutation, so a
half-finished run can be compared against what it should have produced.

## Named checkpoints and crash-safe resume

A migration is a sequence of phases (preflight, inventory, stage, promote,
flip, validate, carry, stale-scan), each with a begin/done/undo/verify shape
registered under its phase name. Recovery re-reads the journal and resumes at
the first phase whose `begin` has no matching `done`, re-running or undoing
whichever half-state it finds. Nothing is inferred from filesystem state
alone — the journal is the source of truth for what was and wasn't done.

The `fault_checkpoint` module is the testing half of this: it can SIGKILL a
process at a named journal boundary, so a test proves what a crash actually
leaves behind (and that recovery handles it), rather than what a clean
`finally` would have left.

## The finalize / rollback split

A committed migration is deliberately left reversible for a while: the
originals sit in a snapshot, the new links are recorded, and a **narrow
rollback** can put the old layout back while anything written since would
otherwise be lost. **Finalize** is the irreversible point — it deletes the
snapshot and retired links, after which there is no going back. The split
exists because "committed" and "safe to delete the old world" are two
different facts, and merging them into one step is how a migration strands a
machine halfway.

## What to keep from this

- Append-only journal + read-only inventory before the first write.
- Named, resumable phases with an explicit undo for each.
- A SIGKILL-probe test harness to prove recovery, not just assert it.
- A distinct finalize step as the irreversible point, after the rollout has
  been observed good on a real machine.
