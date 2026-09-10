#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SWARM_DIR="$ROOT_DIR/copilot/extensions/swarm"
SWARM_LIB_DIR="$ROOT_DIR/pi/extensions/swarm-lib"

# The bundler is esbuild, installed as a pi/ devDependency, so it is resolved
# from pi/'s install tree rather than from a global binary. A fresh worktree has
# no pi/node_modules (untracked, unshared across worktrees), so say so in the
# terms of the fix instead of letting esbuild fail as a bare "command not found"
# several lines into the build.
ESBUILD="$ROOT_DIR/pi/node_modules/.bin/esbuild"
if [[ ! -x "$ESBUILD" ]]; then
  echo "ERROR: $ESBUILD not found — run scripts/bootstrap-worktree.sh (or \`npm install\` in pi/) first" >&2
  exit 1
fi

mkdir -p "$SWARM_DIR/lib" "$SWARM_DIR/extensions/swarm"

# Common flags. --format=esm matches the module shape the committed artifacts
# already have (they end in `export { ... }`), and the entry build keeps the
# Copilot SDK external so the plugin resolves it from the host at load time.
build() {
  local src="$1" out="$2"
  shift 2
  "$ESBUILD" "$src" --bundle --platform=node --format=esm --target=node22 --outfile="$out" "$@"
}

# swarm-scheduling.ts and swarm-herdr.ts are now shared with pi -- built from
# pi/extensions/swarm-lib/, not a local copy. The shared context and the
# Copilot picker adapter also build from that tree.
build "$SWARM_LIB_DIR/swarm-scheduling.ts" "$SWARM_DIR/lib/swarm-scheduling.js"
build "$SWARM_LIB_DIR/swarm-herdr.ts" "$SWARM_DIR/lib/swarm-herdr.js"
build "$SWARM_LIB_DIR/swarm-picker-copilot.ts" "$SWARM_DIR/lib/swarm-picker.js"
build "$SWARM_LIB_DIR/swarm-tool-context.ts" "$SWARM_DIR/lib/swarm-tool-logic.js"

# Bundle the extension entry point for Copilot CLI plugin loading. The two
# externals are separate arguments on purpose: brace expansion like
# `@github/copilot-sdk{,/extension}` is not performed inside quotes, so a
# quoted brace form would be passed to esbuild literally and silently bundle
# the SDK into the artifact instead.
build "$SWARM_DIR/src/extension.ts" "$SWARM_DIR/extensions/swarm/extension.mjs" \
  --external:@github/copilot-sdk --external:@github/copilot-sdk/extension
