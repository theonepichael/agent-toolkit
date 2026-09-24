#!/usr/bin/env bash
set -euo pipefail

# Usage: build-copilot-swarm.sh [--check [--against DIR]]
#   (no flags)  rebuild the five committed outputs in place
#   --check     build into a temp directory and compare byte-for-byte against
#               DIR (default: this checkout); print each stale output's
#               repo-relative path and exit 1 on any difference. Writes nothing
#               into the checkout.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SWARM_SRC_DIR="$ROOT_DIR/copilot/extensions/swarm"
SWARM_LIB_DIR="$ROOT_DIR/pi/extensions/swarm-lib"

CHECK=0
AGAINST="$ROOT_DIR"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) CHECK=1 ;;
    --against)
      [[ $# -ge 2 ]] || { echo "ERROR: --against needs a directory" >&2; exit 2; }
      AGAINST="$2"
      shift
      ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

OUTPUTS=(
  copilot/extensions/swarm/lib/swarm-scheduling.js
  copilot/extensions/swarm/lib/swarm-herdr.js
  copilot/extensions/swarm/lib/swarm-picker.js
  copilot/extensions/swarm/lib/swarm-tool-logic.js
  copilot/extensions/swarm/extensions/swarm/extension.mjs
)

OUT_ROOT="$ROOT_DIR"
if [[ $CHECK -eq 1 ]]; then
  OUT_ROOT="$(mktemp -d)"
  trap 'rm -rf "$OUT_ROOT"' EXIT
fi
SWARM_DIR="$OUT_ROOT/copilot/extensions/swarm"

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
# --check keeps esbuild quiet so its output is only the stale-file list.
QUIET=()
[[ $CHECK -eq 1 ]] && QUIET=(--log-level=warning)
build() {
  local src="$1" out="$2"
  shift 2
  "$ESBUILD" "$src" --bundle --platform=node --format=esm --target=node22 --outfile="$out" "${QUIET[@]}" "$@"
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
build "$SWARM_SRC_DIR/src/extension.ts" "$SWARM_DIR/extensions/swarm/extension.mjs" \
  --external:@github/copilot-sdk --external:@github/copilot-sdk/extension

if [[ $CHECK -eq 1 ]]; then
  stale=0
  for rel in "${OUTPUTS[@]}"; do
    if ! cmp -s "$OUT_ROOT/$rel" "$AGAINST/$rel"; then
      echo "$rel"
      stale=1
    fi
  done
  exit "$stale"
fi
