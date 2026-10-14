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
R=$(git -C "$UP" rev-parse 'release-<n>^{commit}')   # the release you are taking
git -C "$FORK" fetch -q "$UP" "$R"                    # the chosen commit must exist in the fork
LAST=$(git -C "$FORK" merge-base pre-update-<release> "$R")   # last upstream commit already in the fork
git -C "$UP" log --oneline "$LAST..$R"               # read every line
git -C "$FORK" diff --text --name-status "$LAST" pre-update-<release> > ~/fork-delta-before.txt
git -C "$FORK" diff --text "$LAST" pre-update-<release> > ~/fork-delta-before.diff
wc -l ~/fork-delta-before.txt    # the fork's own files; must not be 0
```

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

## 4. Install and test

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

### If an agent is helping

An agent can run these steps. Rules for the agent:

- Stop after each check and show the user its output before going on.
- Never resolve a conflict by taking one side wholesale: no `-X theirs`,
  `-X ours`, `-s ours`, `checkout --theirs` or `checkout --ours`, and no
  `reset --hard`. A conflict inside a fork-only change goes to the user.
- If a check prints something unexpected, stop. Don't work around it.

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
stores during step 4's install, and `--rollback` does not undo that. Once the
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
the live `--depart` install baseline, and `history.jsonl` / `timing.jsonl` /
`harness-discovery-*.json` / `trust-sessions/` are live state.
