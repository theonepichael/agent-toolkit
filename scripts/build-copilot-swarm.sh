#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SWARM_DIR="$ROOT_DIR/copilot/extensions/swarm"
SWARM_LIB_DIR="$ROOT_DIR/pi/extensions/swarm-lib"

mkdir -p "$SWARM_DIR/lib" "$SWARM_DIR/extensions/swarm"

# swarm-scheduling.ts and swarm-herdr.ts are now shared with pi -- built from
# pi/extensions/swarm-lib/, not a local copy. The shared context and the
# Copilot picker adapter also build from that tree.
bun build "$SWARM_LIB_DIR/swarm-scheduling.ts" --outfile "$SWARM_DIR/lib/swarm-scheduling.js" --target node
bun build "$SWARM_LIB_DIR/swarm-herdr.ts" --outfile "$SWARM_DIR/lib/swarm-herdr.js" --target node
bun build "$SWARM_LIB_DIR/swarm-picker-copilot.ts" --outfile "$SWARM_DIR/lib/swarm-picker.js" --target node
bun build "$SWARM_LIB_DIR/swarm-tool-context.ts" --outfile "$SWARM_DIR/lib/swarm-tool-logic.js" --target node

# Bundle the extension entry point for Copilot CLI plugin loading
bun build "$SWARM_DIR/src/extension.ts" --outfile "$SWARM_DIR/extensions/swarm/extension.mjs" --target node --external @github/copilot-sdk --external @github/copilot-sdk/extension
