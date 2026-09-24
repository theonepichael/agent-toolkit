# Cutting over a fork-installed machine to the toolkit-home layout

**Temporary.** Delete this file once Release 2 has landed and every machine
that installs this toolkit is on the toolkit-home layout. It exists only to
carry one kind of machine across the Release 1 cutover.

## Which machines this is for

A machine that differs from the personal machines the main rollout covers in
two ways:

- **It installs from a downstream fork, not from this repository.** A local
  clone of this repository is kept as the upstream mirror, and a sync step
  fetches it and merges it into the fork with
  `git merge --allow-unrelated-histories --no-ff`. The fork's checkout is the
  live install source: its `links.toml` decides every link, and its
  `agent-scripts/` is what the harnesses run.
- **It never syncs its backlog with another machine.** `dev_status_sync.py`
  is not configured there, so the rollout's reconciliation steps are skipped.

Nothing reaches such a machine until someone runs the sync with `--apply`, so
the order below is enforced by when you run it, not by anything automatic.

## Why the fork needs extra steps

- The migration builds its move list from the **running checkout's**
  `links.toml`. A fork's own rows are included, and so is any row it still
  points at a legacy destination under `~/.claude/`.
- Release 1 rewrites upstream's `links.toml` destinations from `~/.claude/...`
  to `~/.agent-toolkit/...`. A fork that has edited or added rows will conflict
  on the merge, and any fork-only row left at a legacy destination keeps a
  toolkit link inside `~/.claude/`. Finalize then cannot empty the legacy
  directory, and the residue audit fails.
- A fork installed before Release 0 has no code that reads the layout pointer
  (`~/.claude/data/toolkit_state.json`) and no installed migration lock. The
  migration's preflight checks for the lock and resolver modules under
  `~/.claude/scripts/`, so Release 0 has to be installed before Release 1 can
  migrate.

## Before you start

The fork's own changes are the one thing on this machine nothing else can
restore, so protect them first. Commit everything in the fork (the working
tree must be clean), then tag the pre-cutover state and keep the tag until
the cutover is finalized:

```bash
FORK=<fork checkout>          # the live install source
UP=<upstream mirror clone>
git -C "$FORK" status --short                  # must print nothing
git -C "$FORK" tag pre-toolkit-home-cutover
git -C "$FORK" show pre-toolkit-home-cutover:links.toml > ~/fork-links-before.toml
git -C "$UP" rev-parse HEAD
readlink -f ~/.claude/scripts/dev_status.py   # must resolve inside $FORK
```

Check the sync script's merge command. It must not resolve conflicts or
discard changes on its own:

```bash
grep -n -E 'merge|checkout|reset|-X|--strategy|theirs|ours' <path to the sync script>
```

If any line shows `-X theirs`, `-s ours`, `--strategy-option`,
`checkout --theirs` or `reset --hard`, don't use the script for steps 2 and 3:
run those merges by hand, as shown there. Using it with `--apply` outside
those two steps is still fine.

The migration itself never writes into the fork checkout: it reads
`links.toml` and runs the checkout's own scripts. Finalize removes only the
legacy links the migration recorded, and only while they still point where it
recorded; it removes a directory only when it is empty. What can lose fork
work is a conflict resolution in steps 2 and 3, so each merge is followed by a
check.

The harness list for this machine is every harness actually installed on it.
Pass exactly that list to every `--harness` below; the migration only rewrites
settings for the harnesses it is told about.

## 1. Hold

Do not run the sync with `--apply` until:

- `release-1` has merged to upstream `main`, and
- the main rollout has finalized cleanly on at least one personal machine.

A dry sync (without `--apply`) only updates the mirror and is always safe.

## 2. Bring the fork up to Release 0

Merge an upstream commit that contains Release 0 but not Release 1: the last
upstream `main` commit before the `release-1` merge. Update the mirror with the
sync script without `--apply`, then find that commit:

```bash
git -C "$UP" log --first-parent --oneline --grep=release-1 origin/main
# note the hash of the merge that brought release-1 into main, then:
R1=<that merge's hash>
R0=$(git -C "$UP" rev-parse "$R1^1")    # its first parent: main just before Release 1
git -C "$UP" log -1 --oneline "$R0"
```

Merge it into the fork by hand, so you control the conflict resolution:

```bash
cd "$FORK"
git merge --allow-unrelated-histories --no-ff "$R0"
# resolve any conflict keeping both upstream's and the fork's changes, then:
git commit
```

Before installing, run step 3's two fork checks (lost `links.toml` rows and
the fork-file diff) against this merge. Then:

```bash
./install.sh --harness=<this machine's harnesses>
./install.sh --check-links
```

This installs the Release 0 runtime (the migration lock and the path
resolver) that the migration's preflight requires. Use the machine normally
for a day: with no layout pointer it keeps running on the legacy layout, and
the hop keeps Release 0's changes separate from the migration's.

## 3. Bring in Release 1

Merge the upstream commit that contains the `release-1` merge, by hand:

```bash
cd "$FORK"
git merge --allow-unrelated-histories --no-ff "$R1"
```

- **Expect conflicts in `links.toml`**, and possibly `install.sh`. Keep
  upstream's new `~/.agent-toolkit/...` destinations for upstream's rows, and
  keep the fork's own rows.
- **Move every fork-only row off `~/.claude/`.** Apply the same mapping
  Release 1 applied to upstream's rows: a script under `~/.claude/scripts/`
  goes to `~/.agent-toolkit/scripts/`, a hook under `~/.claude/hooks/` to
  `~/.agent-toolkit/hooks/`, icons to `~/.agent-toolkit/icons`. Leave a row
  alone only if its destination is a harness-owned file (a harness's own
  command, skill or instructions path), which Release 1 also leaves in place.
  The upstream test suite (`test_no_links_target_legacy_claude_dirs`) and the
  migration's preflight check (`legacy-destinations`) refuse if any legacy
  destinations remain under those directories. To check what's left:

  ```bash
  git -C "$FORK" diff pre-toolkit-home-cutover HEAD -- links.toml
  grep -n 'dest = "~/.claude/\(scripts\|hooks\|icons\)' "$FORK/links.toml"
  ```

  The `grep` should print nothing once the fork's rows are moved. Fix any rows
  the test suite or preflight names before proceeding.
- **Check that no fork row was lost in the merge.** Every source the fork
  linked before must still be linked. This prints any that disappeared:

  ```bash
  cd "$FORK"
  comm -23 <(grep -o '^src = ".*"' ~/fork-links-before.toml | sort -u) \
           <(grep -o '^src = ".*"' links.toml | sort -u)
  ```

  Anything it prints is either a row upstream deliberately removed (check the
  upstream log) or a fork row dropped by the conflict resolution: restore
  those. Then check the fork's other files the same way:

  ```bash
  git diff --stat pre-toolkit-home-cutover HEAD -- . ':!links.toml'
  ```

  Every fork-only file must still exist with its content, and every change
  listed should come from upstream.
- Commit the merge and the row moves in the fork. Do **not** re-run the normal
  install on this commit: the migration creates the new links itself.
- Run the test suite from the fork checkout.

## 4. Rehearse on a scratch home

Rehearse the full cycle from the fork checkout before touching real data, as
the main rollout does:

Do this in a fresh shell, which you close afterwards, so the redirected
`HOME` can't leak into anything else:

```bash
SCRATCH=$(mktemp -d "$HOME/toolkit-rehearsal.XXXXXX")
mkdir -p "$SCRATCH/.claude"
cp -a ~/.claude/data ~/.claude/scripts "$SCRATCH/.claude/"
# Do not copy ~/.local/state/agent-toolkit/history.jsonl: its absolute
# paths name the real home, and finalize acts on the links it records.
export HOME="$SCRATCH" XDG_CONFIG_HOME="$SCRATCH/.config" \
       XDG_DATA_HOME="$SCRATCH/.local/share" XDG_STATE_HOME="$SCRATCH/.local/state" \
       XDG_CACHE_HOME="$SCRATCH/.cache"
cd "$FORK"
H=--harness=<this machine's harnesses>
ID1=mig-$(date -u +%Y%m%dT%H%M%SZ)-000001
ID2=mig-$(date -u +%Y%m%dT%H%M%SZ)-000002
./install.sh --migrate-toolkit-home $H --migration-id "$ID1" --dry-run
./install.sh --migrate-toolkit-home $H --migration-id "$ID1" --skip-reconciliation
./install.sh --rollback-toolkit-home-migration "$ID1"
./install.sh --finalize-toolkit-home-migration "$ID1"
./install.sh --migrate-toolkit-home $H --migration-id "$ID2" --skip-reconciliation
./install.sh --finalize-toolkit-home-migration "$ID2"
./install.sh --check-links $H
```

Every command must succeed, and the last one must report no residue,
including for the fork's own links. Then `exit` the shell and delete
`$SCRATCH`. If anything fails, stop: nothing real has changed yet.

## 5. Migrate

1. Stop every agent session on the machine, including any that write markdown
   into the data directories.
2. Set the migration ID and run a dry run, reading every report:

   ```bash
   cd "$FORK"
   ID=mig-$(date -u +%Y%m%dT%H%M%SZ)-000001
   ./install.sh --migrate-toolkit-home --harness=<this machine's harnesses> \
     --migration-id "$ID" --dry-run
   ```

   - **Legacy destination check**: Preflight refuses (`legacy-destinations`) if
     any applicable row in `links.toml` still targets legacy
     `~/.claude/{scripts,hooks,icons}` paths. If refused, fix the rows listed
     in the report (per step 3) and re-run.
   - Check the carry report for anything unexpected under `~/.claude/data`.
3. Real run, with `--skip-reconciliation`, since this machine has no peer:

   ```bash
   ./install.sh --migrate-toolkit-home --harness=<this machine's harnesses> \
     --migration-id "$ID" --skip-reconciliation
   ```
4. Smoke-test from the new location:

   ```bash
   python3 ~/.agent-toolkit/scripts/dev_status.py render
   python3 ~/.agent-toolkit/scripts/dev_status.py show <an item with related_files>
   ```

   The item's `related_files` paths should point at files that exist. Then
   start a session in every harness on the machine, ask for the dashboard,
   and check it renders.

## 6. Finalize

```bash
./install.sh --finalize-toolkit-home-migration "$ID"
./install.sh --check-links --harness=<this machine's harnesses>
```

`--check-links` is the residue audit. It must pass before this machine counts
as cut over.

## Rolling back

Before finalize, at any point:

```bash
./install.sh --rollback-toolkit-home-migration "$ID"
# Only if abandoning the Release 1 merge. --keep refuses, instead of
# discarding, if the fork has uncommitted changes.
git -C "$FORK" reset --keep pre-toolkit-home-cutover
./install.sh --harness=<this machine's harnesses>
```

After finalize, the snapshot is gone and there is no rollback. Fix forward.

## Release 2

Do not run the sync with `--apply` on any commit that contains Release 2
until this machine has been finalized. Release 2 removes the rule that a
missing layout pointer means the legacy layout.
