#!/usr/bin/env bash
# Bootstrap a fresh agent-toolkit checkout or git worktree: run every
# per-directory dependency install the repo needs in one step.
#
# The repo has two dependency roots, and only the uv-managed one is obvious
# from the repo root:
#   - repo root: uv-managed Python venv (pyproject.toml + uv.lock)
#   - pi/:       a separate bun project (package.json); its node_modules is
#                untracked, so a fresh worktree never has it and the pi TS
#                checks skip rather than fail until it is installed
#
# Safe to rerun on an already-bootstrapped checkout: both underlying
# installs are incremental.
set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "ERROR: bootstrap-worktree.sh takes no arguments (got: $1) — it always installs into its own checkout, resolved from its own location" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> bootstrap target: $repo_root"
fail=0

if command -v uv >/dev/null 2>&1; then
  echo "==> uv sync (repo root)"
  (cd "$repo_root" && uv sync) || {
    echo "ERROR: uv sync failed (repo root)" >&2
    fail=1
  }
else
  echo "WARNING: uv not found on PATH — skipping root venv sync (install it: https://docs.astral.sh/uv/)" >&2
fi

if command -v bun >/dev/null 2>&1; then
  echo "==> bun install (pi/)"
  (cd "$repo_root/pi" && bun install) || {
    echo "ERROR: bun install failed (pi/)" >&2
    fail=1
  }
else
  echo "WARNING: bun not found on PATH — skipping pi/ install; the pi TS checks will skip until it is installed (install it: https://bun.sh)" >&2
fi

exit "$fail"
