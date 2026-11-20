# Updating a fork-installed machine to a new upstream release

How to bring a new release of this repository into a machine that installs
from a **downstream fork** of it. Unlike a personal machine that installs
directly from this repo, a fork has its own `links.toml` rows and files that
an upstream merge can silently drop or overwrite, so every upstream merge is
done by hand, with the fork's own changes protected first.

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

## Running this with an agent

An agent on this machine can run the whole procedure while the user watches.
If you are that agent, these rules apply to every step below:

- **Read this document from the upstream mirror, not the fork.** Until the
  merge in step 2, the fork's checkout holds the copy from its last merge,
  which can be older than these instructions. Read
  `<upstream mirror>/docs/fork-machine-updates.md` after the mirror has been
  updated (the sync script without `--apply`). If you were pointed at the
  fork's copy, say so and ask the user for the mirror's path.
- **Stop after each numbered step and each check.** Show the user the
  command's real output, say what it means, and wait for a plain-text go
  before running the next one. Never batch several steps into one turn.
- **Stop on anything unexpected.** A check that prints something the step
  doesn't predict, a non-zero exit, or a prompt you didn't expect goes to the
  user as-is. Don't work around it, retry with different flags, or guess.
- **Never resolve a conflict by taking one side wholesale:** no `-X theirs`,
  `-X ours`, `-s ours`, `checkout --theirs` or `checkout --ours`, and no
  `reset --hard`. A conflict inside a fork-only change goes to the user.
- **Never edit or delete toolkit data by hand.** Store files under
  `~/.agent-toolkit/data/` and the state directory (`~/.local/state/agent-toolkit/`)
  change only through `install.sh` and the toolkit's own scripts.
- **Exit code 75 from any toolkit script means stop.** The migration lock
  refused the write and nothing was written; report it, don't retry in a loop.
- **Don't roll back after a data upgrade.** Once step 5's install has run for
  a release whose `CHANGELOG.md` section lists a data upgrade, the reset in
  "Rolling back" is no longer safe. Stop and ask instead.
- **Nothing is recorded on this machine.** It has no backlog sync, so the
  personal machines learn about this rollout only from the report in step 7.
  Write that report even if the rollout stopped partway.
- **Placeholders are filled from the release.** `<release>` and `<n>` come
  from the release you are taking (`CHANGELOG.md`). For a rename release, so
  do `<old>`, `<new>` and the renamed environment variables. The machine's
  harness list and profile come from the user. Confirm each with the user
  before the first command that uses it.

## Why the fork needs hand-merges

The fork's own changes are the one thing on this machine nothing else can
restore. An upstream merge touches `links.toml`, the install scripts, and the
harness files the fork may also have edited; a blind merge resolves those
conflicts by dropping one side. Every merge below is therefore done by hand,
with a check after it that the fork's own rows and files survived.

## Before you start

Commit everything in the fork (the working tree must be clean), then tag the
pre-release state and keep the tag until the next merge is confirmed good:

```bash
FORK=<fork checkout>          # the live install source
UP=<upstream mirror clone>
git -C "$FORK" status --short                  # must print nothing
git -C "$FORK" status --short --ignored        # read every "!!" line
git -C "$FORK" clean -nd                       # untracked files; must print nothing unexpected
git -C "$FORK" tag pre-update-<release>
git -C "$FORK" show pre-update-<release>:links.toml > ~/fork-links-before.toml
```

Check the sync script's merge command. It must not resolve conflicts or
discard changes on its own:

```bash
grep -n -E 'merge|checkout|reset|-X|--strategy|theirs|ours' <path to the sync script>
```

If any line shows `-X theirs`, `-s ours`, `--strategy-option`,
`checkout --theirs` or `reset --hard`, don't use the script for the merge:
run it by hand, as shown below. Using it with `--apply` outside the merge is
still fine.

The ignored and untracked checks catch leftovers the plain status hides. A
run that used a directory inside the checkout as its `HOME` can leave
`{data,hooks,scripts}` behind there; anything shaped like a home directory
inside the checkout is such a leftover — compare it against the live data
before deleting it.

The harness list for this machine is every harness actually installed on it.
Pass exactly that list to every `--harness` below, and this machine's profile
(`personal` or `work`) to every `--profile` below — a work-provisioned
machine refuses a bare install that defaults to `personal`.

### A day ahead

Run the test suite from the fork checkout before the day of the merge, when
an environment-only failure is cheap to fix:

```bash
scripts/bootstrap-worktree.sh          # uv sync plus both npm installs
command -v shellcheck shfmt             # both must print a path
uv run pytest -q
```

Record any failure the suite reports that isn't yours to fix before the
merge.

## 1. Pick the upstream commit

Update the mirror with the sync script without `--apply`, then choose the
target upstream commit for this release. Releases are annotated tags
(`release-<n>`, listed in `CHANGELOG.md`); take the tag's commit, not the tag
object. Record what the fork changes on top of upstream, so step 3 can compare
against it:

```bash
R=$(git -C "$UP" rev-parse 'refs/tags/release-<n>^{commit}')   # the release you are taking
git -C "$FORK" fetch -q "$UP" "$R"                    # the chosen commit must exist in the fork
LAST=$(git -C "$FORK" merge-base pre-update-<release> "$R")   # last upstream commit already in the fork
git -C "$UP" log --oneline "$LAST..$R"               # read every line
git -C "$FORK" diff --text --name-status "$LAST" pre-update-<release> > ~/fork-delta-before.txt
git -C "$FORK" diff --text "$LAST" pre-update-<release> > ~/fork-delta-before.diff
wc -l ~/fork-delta-before.txt    # the fork's own files; must not be 0
```

**Every release this merge takes.** The fork may be more than one release
behind, and a skipped release's per-machine steps still have to be done.
List the releases between the fork's last upstream commit and the target:

```bash
git -C "$UP" tag --list 'release-*' --merged "$R" --no-merged "$LAST"
```

Read each listed release's section of `CHANGELOG.md` (from the mirror, at
`$R`), oldest first. Write down every step it addresses to each machine: a
check, a file to delete, a manual edit, a command to run. Note whether each
belongs before the install (step 4) or after it (step 6); a step that doesn't
say goes after. Show the user the list. Note also which of the releases are
clean-break renames (`<old>` -> `<new>`) and which list a data upgrade.

**Fork impact preview.** Upstream changes only the files it owns, so the
fork's own code can break on any release, not just a rename: a fork-only
script that imports a module whose functions changed, or calls a script that
was renamed or deleted. List every upstream file this merge modifies, renames
or deletes, then search the fork's own lines for each one's file name (and,
for a Python file, its module name):

```bash
git -C "$FORK" diff --name-status -M "$LAST" "$R" \
  | awk -F'\t' '$1 !~ /^A/ {print $2}' > ~/upstream-touched.txt
awk -F/ '{print $NF; s = $NF; if (sub(/\.py$/, "", s)) print s}' ~/upstream-touched.txt \
  | sort -u > ~/upstream-terms.txt
awk '/^\+\+\+ b\//{f=substr($0,7);next} /^\+\+\+ /{next}
     /^\+/{print f": "substr($0,2)}' ~/fork-delta-before.diff \
  | grep -F -w -f ~/upstream-terms.txt
```

Each hit is a fork line that names an upstream file this merge changes. Read
upstream's change to that file (`git -C "$UP" diff "$LAST" "$R" -- <path>`,
using the path from `~/upstream-touched.txt`) and decide whether the fork line
still works after it. Most hits are harmless (the file changed, but not the
part the fork uses). Show the user every hit with that verdict before the
merge; a fork line that needs changing is changed by hand after step 3, then
committed to the fork. No hits (`grep` exits 1) means no fork line names a
changed upstream file. A rename release still gets step 4's old-name search,
which also covers environment variables and hyphenated spellings that this
check can't see.

## 2. Merge by hand

```bash
cd "$FORK"
git merge --no-ff "$R"
```

Resolve conflicts by keeping upstream's change and the fork's change
together. Never resolve by taking one side wholesale: no `-X theirs`,
`-X ours`, `-s ours`, `checkout --theirs` or `checkout --ours`, and no
`reset --hard`. A conflict inside a fork-only change goes to the user; if the
two sides can't both stand, stop and ask.

Don't commit yet if anything conflicted. Run step 3 first, on the resolved
tree.

## 3. Check that the fork kept its own changes

The fork's delta on top of upstream must be the same set of files after the
merge as before it:

```bash
cd "$FORK"
git diff --name-status "$R" > ~/fork-delta-after.txt   # working tree vs new upstream
diff ~/fork-delta-before.txt ~/fork-delta-after.txt
```

`diff` should print nothing. A `<` line is a file the fork changed that the
merge dropped back to upstream's copy; a `>` line is a file the fork didn't
change before but does now.

The file list can't see a conflict resolved by keeping the fork's old copy of
a file the fork already changed: that undoes upstream's change inside the
file, and the list stays the same. Compare the changed lines too:

```bash
# --text on both sides so binary files show +/- lines instead of a bare
# "Binary files differ", which the awk below would otherwise drop silently.
changed() {   # every changed line, prefixed with its file
  awk '/^diff --git /{next} /^--- a\//{f=substr($0,7);next}
       /^\+\+\+ b\//{f=substr($0,7);next} /^(---|\+\+\+) /{next}
       /^[-+]/{print f": "$0}' | sort
}
diff <(changed < ~/fork-delta-before.diff) <(git diff --text "$R" | changed)
```

This is a heuristic, not a hard gate: read every difference before acting.
A `<` line is a fork change the merge lost — restore it from
`pre-update-<release>` only after confirming it isn't one of the two expected
false positives: both sides made the same edit (there is then no delta to
preserve, and the old `-old/+new` pair legitimately disappears), or upstream
renamed a file the fork had edited (the fork's edit moved, it wasn't dropped).
A `>` line is an upstream change the merge undid, usually by keeping the fork's
side of a conflict wholesale: take upstream's version of those lines and
re-apply only the fork's own edit. The one expected difference is where
upstream and the fork edited the same line and the resolution merged both
edits into it. When in doubt, show the user.

Check that no fork row was lost from `links.toml` — compare complete rows
(`src` + `dest`), not just sources, so a dropped row or a changed destination
is visible (two rows sharing a `src` with different `dest`s are distinct):

```bash
diff \
  <(grep -E '^(src|dest) = ' ~/fork-links-before.toml) \
  <(grep -E '^(src|dest) = ' links.toml)
```

A `<` line is a fork row (or destination) the merge lost: restore it. A `>`
line is either an upstream change or a fork row moved by the merge. Row-level
gating (`harness`, `profile_exclude`, ...) is not covered by this grep — eyeball
`git diff "$R" links.toml` for those hunks, and let `--check-links` catch a
fork link that no longer resolves. Then check the fork's other files the same
way:

```bash
git diff --stat pre-update-<release> -- . ':!links.toml'
```

Every fork-only file must still exist with its content, and every change
listed should come from upstream. Then commit the merge.

## 4. Before installing

**Per-machine steps.** Do each before-install step from step 1's list, in
release order, exactly as its `CHANGELOG.md` section says. Show the user
anything a step would delete before deleting it.

<!-- rename-transition: remove after the clean-break renames -->
The rest of this step applies only if a release you are taking is one of the
clean-break renames. With several, do it once per rename, oldest first.

**Fork-only references to the old name.** Upstream's rename sweep changed
every file upstream owns. It can't see the fork's own files: a fork-only
script that imports the old module, or a fork `links.toml` row that points
at the old script name, breaks at install. Search every file the fork
changes, for each spelling of the old name (`<old>` with underscores and
with hyphens) and each old environment variable the release lists:

```bash
cd "$FORK"
awk -F'\t' '$1 !~ /^D/ {print $NF}' ~/fork-delta-after.txt \
  | xargs grep -n -F -e '<old with underscores>' -e '<old with hyphens>' -e '<OLD_ENV_VAR>'
```

Show the user every hit. Each one is a fork change to make by hand (rename
it to the new name, as upstream did), then commit it to the fork. No hits
(`grep` exits 1) is the expected result for a fork that doesn't touch the
renamed tool.

**Preview the data upgrade.** If the release lists a data upgrade, see what
the install will rewrite before it runs:

```bash
./install.sh --dry-run --harness=<this machine's harnesses> --profile=<this machine's profile>
```

Among the `[dry-run]` lines, each `would apply <store> v<a>->v<b> to <file>`
line is a store file the install will rewrite. Show them to the user. No such
line means the install upgrades no data. After step 5's install, rolling back
is no longer safe (see "Rolling back").

## 5. Install and test

```bash
cd "$FORK"
uv run pytest -q
./install.sh --harness=<this machine's harnesses> --profile=<this machine's profile>
./install.sh --check-links --harness=<this machine's harnesses> --profile=<this machine's profile>
```

A suite failure that also fails on `pre-update-<release>` isn't from this
merge; note it and go on. `--check-links` must pass. Start a session in every
harness and check the dashboard renders with no import, permission or
missing-script error.

A plain install run also removes any link the machine recorded installing in a
previous release but whose `links.toml` row this release deleted — the
orphan-cleanup step deletes the dangling symlink, so a removed script does not
leave a stale link in `~/.agent-toolkit/scripts/`.

## 6. After installing

**Per-machine steps.** Do each after-install step from step 1's list, in
release order, exactly as its `CHANGELOG.md` section says.

<!-- rename-transition: remove after the clean-break renames -->
The rest of this step applies only if step 4's rename part did, once per
rename.

**Manual edits on this machine.** Some files no command edits: instruction
files and settings that name the old tool, and the shell config that sets the
old environment variables. The checklist scans them for the old name and
lists every hit:

```bash
python3 ~/.agent-toolkit/scripts/rename_transition.py checklist <old> <new> --release release-<n> \
  --file ~/.claude/CLAUDE.md --file ~/.claude/settings.local.json \
  --file <each other harness's instruction file on this machine> \
  --file <the shell config files that export toolkit variables>
```

`--file` replaces the default list, so name every file to scan. Ignore the
"Release checklist" section, which upstream already did. Work through
"Manual edits outside the repository": show the user each `- [ ] <file>:<line>`
hit, edit it to the new name, and re-run the checklist until every file reads
`no occurrences` or `not present`. The two reminder lines at the end always
print. The overlay one applies here too: a file composed from another source
(an instruction file generated from an overlay) is edited at its source, then
regenerated. The work-machine one means this machine, and this step is
how it gets done.

**Renamed environment variables.** Open a new shell so the edited config is
loaded, then:

```bash
./install.sh --report-legacy-env
```

It must print `no legacy environment variables set` and exit 0. Any line it
prints is an old name still set (`OLD -> NEW`): rename it where it is set,
open a new shell, and run it again. The old names still work until the
release named in their warning, but the next rename assumes they are gone.

## 7. Report back

The personal machines track this rollout, and the next release can't land
until they know it finished. This machine can't tell them, so the user
carries this report back. Write it at the end, or wherever the rollout
stopped:

```text
Fork-machine rollout report
release:            release-<n> (<old> -> <new>, or "not a rename release")
releases taken:     <every release step 1 listed, oldest first>
date:               <YYYY-MM-DD>
upstream commit:    <$R>
fork merge commit:  <the fork's merge commit sha>
fork-delta check:   <clean | restored: what>
links.toml check:   <clean | restored: what>
per-machine steps:  <none | each as "release-<n>: step — done | skipped: why">
fork impact preview: <none | each hit as "file — harmless | fixed: what">
fork-only old-name hits: <none | fixed: files>
data upgrade:       <none | applied: store files>
suite:              <passed | failures also on pre-update-<release>: names>
--check-links:      <passed | output>
manual-edit checklist: <no occurrences | remaining: files>
--report-legacy-env:   <clean | still set: names>
harness sessions:   <each harness: dashboard ok | error>
stopped at:         <completed | step and reason>
```

Fill every line from real output, not from memory. Leave a line as `not run`
rather than guessing.

## Rolling back

The merge alone can be undone until it is confirmed good:

```bash
git -C "$FORK" reset --keep pre-update-<release>
./install.sh --harness=<this machine's harnesses> --profile=<this machine's profile>
```

Keep the `pre-update-<release>` tag until the next upstream merge.

**Releases that upgrade stored data cannot be rolled back this way.**
<!-- rename-transition: remove after the clean-break renames -->
A rename release whose `CHANGELOG.md` section lists a data upgrade rewrites
stores during step 5's install, and `--rollback` does not undo that. Once the
install has run, the old code's readers reject the new store version, so
resetting to `pre-update-<release>` leaves a machine that cannot read its own
data. Roll forward instead: fix the problem on top of the release and
re-install. Before the install has run, the reset above is still safe.

## Clean up dead migration state (one-time)

After the release that removes the last of the layout-cutover guards
(the stale-layout refusal, the legacy layout knowledge, and the
legacy-pointer aliasing check), the retired Release 0→1 migrator's per-machine
journal is dead and can be deleted. Resolve the effective XDG state directory
with the same rule `cli_common.state_dir()` uses — `$XDG_STATE_HOME` only when
set to an absolute path, else `~/.local/state` — and delete only the journal:

```bash
STATE="$(python3 -c 'import os; from pathlib import Path; s = os.environ.get("XDG_STATE_HOME"); print(Path(s) if s and Path(s).is_absolute() else Path.home() / ".local/state")')"
rm -rf -- "$STATE/agent-toolkit/migrations"
```

This deletion is **irreversible** — inspect the journal first (and back it up
if its history is wanted). Do **not** remove anything else under the state
directory: `migration.lock` and `migration-observations.jsonl` belong to the
still-live migration lock, `baseline.json` and `baseline-snapshot-*.blob` are
the live `--uninstall` install baseline, and `history.jsonl` / `timing.jsonl` /
`harness-discovery-*.json` / `trust-sessions/` are live state.
