# Changelog

agent-toolkit ships as numbered releases. Each release is an annotated git tag,
`release-<n>`, on the commit that completes it. The tags are what the
fork-installed machine merges (see `docs/fork-machine-updates.md`). Personal
machines install from `main`, which is always at or past the latest release.

A release that **upgrades stored data** says so in its section. Its install
rewrites store files, which `install.sh --rollback` does not undo, so the only
recovery from a bad data release is to roll forward. Each rename release in
the clean-break series also lists its renamed environment variables, which
keep working under the old name, with a warning, until the release named in
the warning. Run `./install.sh --report-legacy-env` to see any still set.

## Unreleased

## release-6 — 2026-11-20

- **Clean-break rename 3 (`herdr_delegate` → `herdr_queue_runner`):** renamed
  `agent-scripts/herdr_delegate.py` → `herdr_queue_runner.py` (its `[...]`
  stderr prefix now reads `[herdr_queue_runner]`), with matching updates
  across the `/swarm` and `/foreman` skills (Claude Code, Copilot), the Pi and
  Copilot `/backlog-item` copies (regenerated from their templates), the
  Claude permission seeds (`claude/settings.json`, `settings.work.json`, via
  `permission_matrix.py`), `links.toml`, `INTERFACES.md`, `GLOSSARY.md`, docs,
  and tests. Skill names and triggers are unchanged. There is no alias. The
  external product name `herdr` and every `HERDR_*` environment variable keep
  their names. This release makes no behavior changes, upgrades no stored
  data (launch-ledger records, swarm state files and worker prompts never
  embed the script path), and renames no environment variables (none exist).
  - **Roll out between runs.** A live `/swarm`, `/foreman` or Pi
    `/backlog-item` session still holds the old path.
  - **Per-machine steps, in this order:**
    1. Run the installer: the overlay wrapper where one exists, otherwise
       `./install.sh --harness=... --profile=<profile>`.
    2. **Before `--check-links`** (this goes ahead of the post-install step
       order in `docs/fork-machine-updates.md`, where `--check-links`
       immediately follows the install): confirm that
       `~/.agent-toolkit/scripts/herdr_delegate.py` is gone, dangling links
       included. Orphan cleanup misses a link it has no creation record for,
       and a survivor fails `--check-links` as `unmanaged`. Before removing a
       leftover, check its target, because the installer deliberately keeps a
       link that another owner repointed.
    3. Close every Claude Code session, since a running session rewrites
       `settings.json` and clobbers the edit. Then, in
       `~/.claude/settings.json` (a copy-once seed that install never
       rewrites), replace
       `Bash(python3 ~/.agent-toolkit/scripts/herdr_delegate.py *)` with
       `Bash(python3 ~/.agent-toolkit/scripts/herdr_queue_runner.py *)`. Do
       not use `settings_seed_drift_check.py fix` for this: it only adds
       entries and leaves the old one behind. The alternative,
       `./install.sh --harness=claude --profile=<profile> --reseed`, replaces
       the whole drifted file. Verify with
       `python3 ~/.agent-toolkit/scripts/gen_permissions.py --audit-live`.
    4. Run `./install.sh --check-links` with the same `--harness` and
       `--profile` values.
    5. List saved foreman handoffs that still name the old script
       (`grep -l herdr_delegate ~/.agent-toolkit/data/foreman/*.md`). Replace
       the name in each before resuming from it. Then start fresh Claude Code,
       Copilot and Pi sessions.
    6. Run `python3 ~/.agent-toolkit/scripts/rename_transition.py checklist
       herdr_delegate herdr_queue_runner
       --release release-6`, then once more with
       `--file ~/.claude/settings.json`. `--file` replaces the default scan
       list, which leaves out `settings.json`.

### release-6: `herdr_delegate` -> `herdr_queue_runner` checklist

- [x] Defining source file renamed (`herdr_delegate.py` → `herdr_queue_runner.py`)
- [x] Every Python import updated
- [x] `links.toml` destination and manifest entry updated (one row)
- [x] `install.py` / `install.sh` references updated (one `install.py` comment; `install.sh` names nothing)
- [x] Workflow files regenerated, per harness, from templates — never hand-edited (`swarm.md.tmpl`, `foreman.md.tmpl`, `backlog_item_pi_native.md.tmpl`; no skill file is renamed, so no retired copies)
- [x] Generator parameter tables updated (`gen_skills_params.py`; `permission_matrix.py`, compiled by `gen_permissions.py`; contract fingerprints, carried over unchanged under the new key)
- [x] `INTERFACES.md` regenerated
- [x] `GLOSSARY.md` term added with its first-use definition
- [x] `README.md` references updated
- [x] Tests and fixtures updated; drift checks pass
- [x] Persisted field or value transform written, with a version bump (N/A — no store holds the name as a value)
- [x] Environment variable fallback added with a deprecation warning (N/A — zero `HERDR_DELEGATE*` variables exist; `HERDR_*` names the external product and is not renamed)
- [x] Manual-edit checklist emitted for external instruction files (`rename_transition.py checklist herdr_delegate herdr_queue_runner --release release-6`, scan result on the release machine: `~/.claude/CLAUDE.md` and `settings.local.json` have no occurrences; the `--file ~/.claude/settings.json` scan finds the one seed allow pattern that step 3 replaces. The other machines run their own scans at rollout, per step 6)
- [x] No undocumented alias left behind, except the env-var fallback window

## release-5 — 2026-11-20

- **Clean-break rename 2 (`refresh_guidance` → `guidance_audit`):** renamed
  `agent-scripts/refresh_guidance.py` → `guidance_audit.py`, the
  `/refresh-guidance` skill → `/guidance-audit` in all six harnesses
  (regenerated from `templates/guidance_audit.md.tmpl`), the auto-discovered
  doc-set config `refresh-guidance.toml` → `guidance-audit.toml`, and the
  default review-state file `refresh-guidance-state.json` →
  `guidance-audit-state.json`, with matching updates across `links.toml`,
  `gen_skills.py`, `INTERFACES.md`, `GLOSSARY.md`, docs, and tests. There is no
  alias and no old-filename fallback: a repo that still carries only
  `refresh-guidance.toml` gets the existing loud "no doc-set specified" error,
  which names the new file. Saying "refresh guidance" still triggers the skill.
  This release makes no behavior changes, upgrades no stored data, and
  renames no environment variables (none exist).
  - **Per-machine order:** pull agent-toolkit and any downstream overlay repo
    that carries a `refresh-guidance.toml` (rename it there in the same
    rollout), then run the installer once. Run the audit only after both pulls.
  - **Per-machine post-install check, before `./install.sh --check-links`:**
    1. Rename any `refresh-guidance.toml` or `refresh-guidance-state.json` left
       at a repo root to `guidance-audit.toml` / `guidance-audit-state.json`.
    2. Delete `~/.codex/skills/refresh-guidance/`. Codex skills are copied,
       not linked, so orphan cleanup never removes them.
    3. Confirm that none of the old destinations survive, dangling links
       included: `~/.agent-toolkit/scripts/refresh_guidance.py`,
       `~/.claude/commands/refresh-guidance.md`,
       `~/.copilot/skills/refresh-guidance/`,
       `~/.config/opencode/commands/refresh-guidance.md`,
       `~/.gemini/antigravity-cli/skills/refresh-guidance/`,
       `~/.pi/agent/prompts/refresh-guidance.md` and
       `~/.pi/agent/skills/refresh-guidance/`. Orphan cleanup misses a link it
       has no creation record for, and a nested skill link whose repo
       directory is gone when the checkout has moved. Remove a confirmed
       leftover by hand. A surviving scripts link otherwise fails
       `--check-links` as `unmanaged`.
    4. Run `rename_transition.py checklist` for both spellings
       (`refresh_guidance guidance_audit` and `refresh-guidance guidance-audit`,
       `--release release-5`), since each run matches one literal spelling.

### release-5: `refresh_guidance` -> `guidance_audit` checklist

- [x] Defining source file renamed (`refresh_guidance.py` → `guidance_audit.py`)
- [x] Every Python import updated
- [x] `links.toml` destination and manifest entry updated (six rows; Pi's skill rides the unchanged `pi/skills` directory row)
- [x] `install.py` / `install.sh` references updated (N/A — neither names the auditor)
- [x] Workflow files regenerated, per harness, from templates — never hand-edited (old copies removed; `gen_skills.py` does not delete retired outputs)
- [x] Generator parameter tables updated (`gen_skills_params.py` `GUIDANCE_AUDIT_PARAMS`, including the Claude `allowed-tools` pattern; `gen_skills.py` registrations; contract fingerprints)
- [x] `INTERFACES.md` regenerated
- [x] `GLOSSARY.md` term added with its first-use definition
- [x] `README.md` references updated
- [x] Tests and fixtures updated; drift checks pass
- [x] Persisted field or value transform written, with a version bump (N/A — the only persisted file is the per-repo review-state file, renamed by hand per the post-install check; none existed on the machines checked)
- [x] Environment variable fallback added with a deprecation warning (N/A — zero `REFRESH_GUIDANCE*` environment variables exist)
- [x] Manual-edit checklist emitted for external instruction files (`rename_transition.py checklist` for both spellings; each machine runs its own scan at rollout, per the post-install check above)
- [x] No undocumented alias left behind, except the env-var fallback window

### Other changes in release-5

- `./install.sh --harness=all` provisions every harness whose CLI is on PATH,
  noting each one it skips; `--force-harness` keeps all six, and
  `--profile=work` leaves out opencode. `all` cannot be mixed with harness
  names.
- An `install.py` run outside a personal-overlay checkout's wrapper, on a
  machine that has one, no longer repoints the global git hooks path or
  records uninstall baselines for the overlay-owned links, and reports those
  deferrals as notes instead of skips, so a clean run exits 0.
- New `githooks/post-merge`: after a merge into `main` that changes what the
  installer links or copies, it reminds you to run
  `./install.sh --harness=all`, or runs it itself in a clone opted in with
  `git config --local agent-toolkit.postMergeInstall true`. Per machine: the
  hook only fires in a clone whose `core.hooksPath` is `githooks`.

- `docs/fork-machine-updates.md` now has the fork-installed machine do the
  per-machine steps of every release it takes, not only the target's: step 1
  lists the releases between the fork's last upstream commit and the target
  and collects each `CHANGELOG.md` section's per-machine steps, steps 4 and 6
  do them before or after the install, and the rollout report records them.
  An agent running it reads the doc from the upstream mirror, since the fork's
  own copy predates the merge.
- `docs/fork-machine-updates.md` step 1 adds a fork impact preview for every
  release: it lists the upstream files the merge modifies, renames or
  deletes and flags each fork-only line that names one, so a fork script
  that depends on a changed upstream module is caught before the merge, not
  only on a rename release. The rollout report gains a matching line.

## release-4 — 2026-11-17

- **Clean-break rename 1 (`depart` → `uninstall`):** renamed `depart.py` →
  `uninstall_state.py`, `depart_exec.py` → `uninstall_exec.py`, `--depart` →
  `--uninstall` (no `--depart` CLI alias; `--depart` now exits 2), the
  confirmation token `DEPART` → `UNINSTALL`, and the transient per-run state
  paths `departure.jsonl` / `departure.lock` → `uninstall.jsonl` /
  `uninstall.lock`, with matching updates across `install.py`,
  `gen_interfaces.py`, `refresh_guidance.py`, `GLOSSARY.md`, `INTERFACES.md`,
  docs, and tests. This release makes no behavior changes and upgrades no stored
  data (`baseline.json` carries no `depart` strings, and zero `DEPART*`
  environment variables exist).
  - **Per-machine pre-install check:** before installing `release-4`, check
    your local state directories (for example `~/.local/state/agent-toolkit/`
    and any downstream overlay repo's state dir under `~/.local/state/`) for
    `departure.jsonl` / `departure.lock`. If found, verify no live process holds
    `departure.lock` and confirm whether `departure.jsonl` is stale residue
    (rather than an in-flight `--depart` run to finish under the old release
    first), show the files to the user, and delete them only after the user
    confirms; never rename them into place.

### release-4: `depart` -> `uninstall` checklist

- [x] Defining source file renamed (`depart.py` → `uninstall_state.py`, `depart_exec.py` → `uninstall_exec.py`)
- [x] Every Python import updated
- [x] `links.toml` destination and manifest entry updated (N/A — repo-root entrypoints have no `links.toml` rows)
- [x] `install.py` / `install.sh` references updated
- [x] Workflow files regenerated, per harness, from templates — never hand-edited (N/A — installer-only, no harness workflow surface)
- [x] Generator parameter tables updated (`gen_interfaces.py` `ROOT_ENTRYPOINTS`)
- [x] `INTERFACES.md` regenerated
- [x] `GLOSSARY.md` term added with its first-use definition
- [x] `README.md` references updated (no `--depart` references in `README.md`)
- [x] Tests and fixtures updated; drift checks pass
- [x] Persisted field or value transform written, with a version bump (N/A — `baseline.json` holds no `depart` strings and `departure.jsonl` is transient per-run state handled by the pre-install check above)
- [x] Environment variable fallback added with a deprecation warning (N/A — zero `DEPART*` environment variables exist)
- [x] Manual-edit checklist emitted for external instruction files (`rename_transition.py checklist depart uninstall --release release-4`; scan result: the only hit was `~/.claude/CLAUDE.md:67` `"departs from the"`, which is ordinary English, so no edit is needed on this machine — the work machine and fedora get their own scan at rollout)
- [x] No undocumented alias left behind, except the env-var fallback window

- `docs/fork-machine-updates.md` can now be run by an agent on the
  fork-installed machine while the user oversees it: a rules section up
  front (stop after every step, no hand edits to toolkit data, exit 75 means
  stop, no rollback after a data upgrade), rename-release steps before and
  after the install (fork-only references to the old name, a dry-run preview
  of data upgrades, the manual-edit checklist run on that machine,
  `--report-legacy-env`), and a fixed-format rollout report for the user to
  carry back to the personal machines.

- `land`'s commit gate now refuses a staged diff that adds any backlog-store
  id string to files that ship in git history (the no-slugs rule, enforced):
  run `dev_status.py slug-check --repo <worktree> [--slug <slug>]` before the
  approval ask — it prints `<file>:<line>: <id>` per finding and the scanned
  staged-diff hash. A genuine non-slug coincidence is overridden per finding
  with `--allow <path>:<line>:<id> <reason>`, each reason recorded to a
  `slug_overrides.jsonl` sidecar (fail-closed: an unrecorded override is
  refused); there is no blanket bypass. Exit codes gate the commit: 0 clean
  or all overridden, 1 findings remain (refused), 2 any error (never read as
  clean). The guard lives in the shared `land` template ahead of the
  approval ask across all six harnesses, and the pi `dev_status` tool gains
  a matching `slug_check` action.

- Backlog items can now carry a contract-derived `acceptance` record (via
  `dev_status.py acceptance-set <slug>` or `add`'s `acceptance` field),
  frozen once work starts so a worker can only append gate criteria, never
  drop or reword acceptance ones. `gate-set` refuses to drop/reword any
  acceptance criterion; `done`/`approve` refuse unless the passed gate covers
  them. When `acceptance.independent_verifier` is set, `gate-pass` requires a
  verifier token — a 0600 file whose path (never its content) is passed via
  `--verifier-token-file` — so a non-worker session holds the verifier role;
  `verifier-check` validates the token, and `contract-touch` reports which
  acceptance contract files a staged/committed/range diff touches. The
  `backlog-item`/`land`/`foreman`/`to-tickets` skills and the pi `dev_status`
  tool (`acceptance_set`/`verifier_check`/`contract_touch` actions) carry the
  matching rules.

- The **foreman** skill (`claude/commands/foreman.md`, `copilot/skills/foreman/SKILL.md`) codifies the manual-scheduling mode used in the 2026-10-14 mvc2 run: one interactive session launches single-item pi workers, watches them, reviews and sends work back, and lands it itself — via two new `herdr_delegate.py` subcommands, `watch` (level-triggered settle watcher) and `answer` (drive a worker's `question` picker by option or free text, verifying before every key), and a `launch --handoff` flag that makes a worker stop after its commit gate and print a `FOREMAN-HANDOFF` line for the foreman to land. The picker parse/match/navigate logic is now a Python twin (`agent-scripts/herdr_picker.py`) of `swarm-picker.ts`, exercised by shared fixtures in `test/fixtures/picker/` read by both suites.
- Rename-transition groundwork for the clean-break renames:
  `agent-scripts/rename_transition.py` (environment-variable fallbacks, the
  `report` and `checklist` commands, and the store-transform framework, all
  shipped with empty tables), `install.sh --report-legacy-env`, a
  session-start line for legacy variables, and a data-upgrade step in a plain
  install that runs registered transforms after a clean link audit. No
  behaviour changes until a rename release registers something. All of it is
  temporary and is deleted after the last rename.
- The remaining layout-cutover guards were removed (`check_not_stale`,
  `StaleLayoutError`, the legacy-pointer aliasing check). `migration_lock`
  stays through the renames, which use it for store transforms.

## release-3 — 2026-10-13 (`e355409`)

Removed the toolkit-home cutover machinery now that every machine is past
Release 2: the Release 0→1 migrator and its tests, the Release 2 pointer-move
command, the legacy-pointer fallback read in `agent_toolkit_paths`, the
retained-legacy-link audit, and the migration-only installer flags
(`--migrate-toolkit-home`, `--move-layout-pointer`, `--json`, and the
migration-id/finalize/rollback flags). The fork-update procedure moved to
`docs/fork-machine-updates.md`. Per machine, a deployed link to the deleted
`retained_legacy_links.py` may be left dangling; `install.sh --check-links`
reports it. Also, delete the dead migration journal directory as described in
the fork-update doc's one-time cleanup section.

## release-2 — 2026-10-09 (`407dd4d`)

Moved the layout pointer out of the legacy Claude data directory into the
toolkit home (`~/.agent-toolkit/data/toolkit_state.json`), with
`install.sh --move-layout-pointer` to move it once per machine. The resolver
read the new pointer first with a legacy fallback for the transition.

## release-1 — 2026-09-24 (`fe52d6f`)

The toolkit-home cutover: runtime data and installed scripts moved from
`~/.claude/` to `~/.agent-toolkit/` (`data/`, `scripts/`, `hooks/`), with a
journaled, resumable migration (`install.sh --migrate-toolkit-home`) and
finalize/rollback commands. The design is summarised in
`docs/migration/migration-pattern.md`.
