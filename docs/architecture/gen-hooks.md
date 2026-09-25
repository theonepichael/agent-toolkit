# Unified Hook Generator (`gen_hooks.py`)

## Boundary / responsibility

`agent-scripts/gen_hooks.py` is the code generator that compiles lifecycle hook configurations (`PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, `Notification`) from a central declarative manifest in `agent-scripts/harness_spec.py` into harness-specific configuration artifacts. Prior to this generator, hook configurations were independently hand-authored across `claude/settings.json`, `claude/settings.work.json`, `copilot/hooks/*.json`, `agy/hooks.json`, and `pi/extensions/`. This manual separation caused asymmetric safety enforcement (such as divergent tool matchers, uncoordinated timeout values, and Copilot lagging behind Claude on concurrent session-start checks) and required repeated manual interventions to prevent premature commits. `gen_hooks.py` establishes single-source parity while respecting each harness's distinct configuration schema.

## Key interfaces

- `harness_spec.py` declarative definitions:
  - `LIFECYCLE_HOOKS`: central dictionary mapping canonical hook types (`PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, `Notification`) to their per-harness schemas and command payloads.
  - Integration with `HarnessSpec` and `FEATURE_MATRIX` for lifecycle hook verification.
- `agent-scripts/gen_hooks.py` CLI interface:
  - Default execution: compiles and updates all target files (`copilot/hooks/pre-tool-use.json`, `copilot/hooks/post-tool-use.json`, `copilot/hooks/session-start.json`, `copilot/hooks/agent-stop.json`, `agy/hooks.json`, and the `hooks` section in `claude/settings.json` and `claude/settings.work.json`).
  - `--check`: exits with status 0 if on-disk files match compiled output; exits with 1 and displays diffs if any configuration is stale.
  - `--stdout`: prints the compiled JSON configurations to stdout without modifying files on disk.
  - `--repo-root <path>`, `-q/--quiet`, `-v/--verbose`: standard repository path override and logging flags via `cli_common`.
- Pre-commit integration:
  - `githooks/pre-commit` invokes `python3 agent-scripts/gen_hooks.py --check --repo-root ...` to prevent stale hook manifests from being committed.
- Path check integration:
  - Registered in `scripts/check_toolkit_paths.py` `GENERATORS` table and linked in `links.toml`.

## Explicit non-goals

- Does not compile Pi's TypeScript extensions: Pi implements lifecycle behaviors as native TypeScript extensions (`pi/extensions/guard-rails.ts`, `ruff-format-on-edit.ts`, `notify.ts`) because Pi lacks a static JSON hook file format. Pi's coverage is tracked declaratively in `harness_spec.py` for verification rather than generated from Python templates.
- Does not replace non-hook settings in `claude/settings.json`: permissions, model specifications, UI themes, and voice configurations remain user- or profile-specific and are preserved across generation.
- Does not execute hook logic at generation time: compilation produces static JSON configurations targeting deployed `~/.agent-toolkit/scripts/` paths.

## Known unknowns / deferred decisions

- Future harness hook formats: If an additional harness (e.g. Codex or OpenCode) introduces native lifecycle hook JSON manifests, `gen_hooks.py` will add an emitter for that schema without altering the central declarative definitions.
- Event naming differences: Harnesses use varying terminology (e.g., Copilot's camelCase `preToolUse` vs. Claude's PascalCase `PreToolUse`, agy's `PreInvocation` vs. Claude's `SessionStart`). The central manifest maps canonical lifecycle states to harness-specific event names.
