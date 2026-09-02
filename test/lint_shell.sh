#!/usr/bin/env bash
# Run shellcheck and shfmt over the in-scope POSIX-sh / bash files.
# Exits nonzero on any warning+ finding or formatting diff.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

files=(
  "$REPO/install.sh"
  "$REPO/scripts/export_for_print.sh"
  "$REPO/scripts/export_for_print_trimmed.sh"
  "$REPO/test/run.sh"
  "$REPO/test/scenarios.sh"
)

printf '%s\n' "Running shellcheck --severity=warning..."
shellcheck --severity=warning "${files[@]}"

printf '%s\n' "Running shfmt -i 2 -ci -d..."
shfmt -i 2 -ci -d "${files[@]}"

printf '%s\n' "Shell lint passed."
