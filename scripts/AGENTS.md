# AGENTS.md — scripts

<!-- Paired with CLAUDE.md symlink in the same directory -->

## Responsibilities & Boundary

Repo-maintenance entrypoints run directly by whoever maintains this
toolkit — not harness-runtime code. None of these scripts have a
`links.toml` entry or get installed to a harness config directory.

## Hazards & Signposts

- `bootstrap-worktree.sh` installs both of the repo's dependency roots (the
  uv-managed root venv and the separate npm project in `pi/`) in one step.
  Safe to rerun on an already-bootstrapped checkout. Run it right after
  `git worktree add` — a fresh worktree never has `pi/`'s `node_modules`
  until this runs.
- `build-copilot-swarm.sh` bundles `pi/extensions/swarm-lib/` TypeScript
  into `copilot/extensions/swarm/` — copilot's swarm extension has a build
  step that pi's own native orchestration does not.
