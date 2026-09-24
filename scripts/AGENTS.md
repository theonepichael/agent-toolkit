# AGENTS.md — scripts

<!-- Paired with CLAUDE.md symlink in the same directory -->

## Responsibilities & Boundary

Repo-maintenance entrypoints run directly by whoever maintains this
toolkit — not harness-runtime code. None of these scripts have a
`links.toml` entry or get installed to a harness config directory.

## Hazards & Signposts

- `bootstrap-worktree.sh` installs all three dependency roots (the uv-managed
  root venv and the separate npm projects in `pi/` and `opencode/`) in one
  step. Safe to rerun on an already-bootstrapped checkout. Run it right after
  `git worktree add` — a fresh worktree has neither `pi/node_modules` nor
  `opencode/node_modules` until this runs.
- `build-copilot-swarm.sh` bundles `pi/extensions/swarm-lib/` TypeScript
  into `copilot/extensions/swarm/` — copilot's swarm extension has a build
  step that pi's own native orchestration does not.
- `check_toolkit_paths.py` runs two repository checks for the toolkit-home
  migration, both enforced by `test/test_check_toolkit_paths.py`:
  `ownership` classifies every `.claude` path reference as toolkit, harness,
  or foreign (a new unclassifiable one fails), and `inventory` checks that
  every `links.toml` entry has a declared kind (`TOOLKIT_DATA` in Python
  modules, a `NON_PYTHON` row for TypeScript/JavaScript). If it fails, add a
  rule or a row there; `--report` prints the release-1 work list.
- `check_regressions.py` statically checks `@pytest.mark.regression(label, red)`
  usage across test suites, enforcing literal arguments and the non-zero marks
  invariant.
