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
