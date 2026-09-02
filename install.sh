#!/usr/bin/env sh
# Bootstrap only. The installer itself is install.py, next to this file —
# this script exists so the familiar `./install.sh --harness=...` muscle
# memory keeps working, and so a machine with too old a Python fails with a
# clear message instead of a syntax error 900 lines in.
set -eu

# shellcheck disable=SC1007 # CDPATH= clears CDPATH for this one command so cd ignores it.
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

for candidate in python3.13 python3.12 python3 python; do
  bin="$(command -v "$candidate" 2>/dev/null)" || continue
  if "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    exec "$bin" "$DIR/install.py" "$@"
  fi
done

echo "install.sh: no Python 3.12+ found on PATH (tried python3.13, python3.12, python3, python)." >&2
echo "install.sh: install Python 3.12 or newer, then re-run — refusing to fall back to an older one." >&2
exit 1
