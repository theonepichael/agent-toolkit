#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SWARM_DIR="$ROOT_DIR/copilot/extensions/swarm"

mkdir -p "$SWARM_DIR/lib" "$SWARM_DIR/extensions/swarm"

# Build individual modules for tests / imports
bun build "$SWARM_DIR/src/swarm-scheduling.ts" --outfile "$SWARM_DIR/lib/swarm-scheduling.js" --target node
bun build "$SWARM_DIR/src/swarm-herdr.ts" --outfile "$SWARM_DIR/lib/swarm-herdr.js" --target node
bun build "$SWARM_DIR/src/swarm-picker.ts" --outfile "$SWARM_DIR/lib/swarm-picker.js" --target node
bun build "$SWARM_DIR/src/swarm-tool-logic.ts" --outfile "$SWARM_DIR/lib/swarm-tool-logic.js" --target node

# Bundle the extension entry point for Copilot CLI plugin loading
bun build "$SWARM_DIR/src/extension.ts" --outfile "$SWARM_DIR/extensions/swarm/extension.mjs" --target node --external @github/copilot-sdk --external @github/copilot-sdk/extension
