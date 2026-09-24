#!/usr/bin/env bash
# Rehearse the toolkit-home migration on a scratch copy of this machine's
# legacy home, then prove the real home was not touched.
#
# Temporary: delete together with docs/migration/fork-machine-cutover.md once
# Release 2 has landed.
#
# Usage:
#   scripts/rehearse-toolkit-home-migration.sh --harness=<list> [options]
#
# Options:
#   --harness=LIST    harnesses installed on this machine (required)
#   --profile=NAME    install profile, as for install.sh (default: personal)
#   --checkout=DIR    the install source to rehearse from (default: the
#                     checkout this script lives in)
#   --scratch=DIR     where to build the scratch home; must not exist yet
#                     (default: a new ~/toolkit-rehearsal.XXXXXX)
#   --without-bwrap   run without the read-only sandbox. The installer state
#                     is then NOT copied, so the rehearsal skips link
#                     retirement (reduced fidelity).
#   --yes             don't pause between stages
#
# Why it works this way:
# - HOME and XDG_* are set on each command and never exported, so a pane
#   that opens a new shell can't lose them and silently run against the
#   real home.
# - Every install.sh call runs under bwrap with the whole filesystem
#   read-only except the scratch directory. The installer history records
#   absolute destinations; the copy is rewritten to the scratch home, and
#   anything that still escapes fails instead of changing the real home.
# - Report output that names a real harness home aborts the rehearsal.
# - The real home's recorded links, settings files and legacy data are
#   fingerprinted before and after; any difference is reported on exit
#   and the script exits 3.
set -euo pipefail

usage() { sed -n '8,23p' "$0" | sed 's/^# \{0,1\}//'; }

REAL_HOME="$HOME"
REAL_STATE="${XDG_STATE_HOME:-$REAL_HOME/.local/state}/agent-toolkit"
CHECKOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HARNESS=""
PROFILE="personal"
SCRATCH=""
USE_BWRAP=1
PAUSE=1

for arg in "$@"; do
  case "$arg" in
    --harness=*) HARNESS="${arg#*=}" ;;
    --profile=*) PROFILE="${arg#*=}" ;;
    --checkout=*) CHECKOUT="${arg#*=}" ;;
    --scratch=*) SCRATCH="${arg#*=}" ;;
    --without-bwrap) USE_BWRAP=0 ;;
    --yes) PAUSE=0 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

die() {
  echo "REHEARSAL FAILED: $*" >&2
  exit 1
}

[ -n "$HARNESS" ] || die "--harness=<list> is required"
[ "$(id -u)" -ne 0 ] || die "refusing to run as root"
[ -x "$CHECKOUT/install.sh" ] || die "no install.sh in $CHECKOUT"
if [ -e "$REAL_HOME/.claude/data/toolkit_state.json" ]; then
  die "this machine already has a layout pointer; there is no legacy home to rehearse"
fi
if [ "$USE_BWRAP" -eq 1 ]; then
  command -v bwrap >/dev/null 2>&1 ||
    die "bwrap not found; install bubblewrap, or pass --without-bwrap for a reduced rehearsal"
  bwrap --ro-bind / / --dev /dev --proc /proc true 2>/dev/null ||
    die "bwrap is installed but cannot create a sandbox here (user namespaces disabled?)"
fi

if [ -z "$SCRATCH" ]; then
  SCRATCH="$(mktemp -d "$REAL_HOME/toolkit-rehearsal.XXXXXX")"
else
  [ ! -e "$SCRATCH" ] || die "--scratch $SCRATCH already exists"
  mkdir -p "$SCRATCH"
fi
SCRATCH="$(cd "$SCRATCH" && pwd)"
SHOME="$SCRATCH/home"
LOGS="$SCRATCH/logs"
SSTATE="$SHOME/.local/state/agent-toolkit"
mkdir -p "$SHOME/.claude" "$SHOME/.config" "$SHOME/.cache" \
  "$SHOME/.local/share" "$SHOME/.local/state" "$SCRATCH/tmp" "$LOGS"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
ID1="mig-${TS}-000001"
ID2="mig-${TS}-000002"
H="--harness=$HARNESS"
P="--profile=$PROFILE"

banner() { printf '\n==== %s ====\n' "$*"; }
pause() {
  [ "$PAUSE" -eq 1 ] || return 0
  printf '>>> %s — press Enter to continue, Ctrl-C to stop ' "$*"
  read -r _
}

# ── fingerprint of the real home (the tripwire) ──────────────────────────────
fingerprint() {
  python3 - "$REAL_HOME" "$REAL_STATE" "$1" <<'PY'
import hashlib, json, os, sys
from pathlib import Path

home, state, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

def history_dests():
    history = state / "history.jsonl"
    if not history.is_file():
        return []
    dests = set()
    for line in history.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("kind") == "symlink-created" and entry.get("dest"):
            dests.add(str(entry["dest"]))
    return sorted(dests)

def describe(path):
    if os.path.islink(path):
        return ["link", os.readlink(path)]
    if os.path.isfile(path):
        return ["file", hashlib.sha256(Path(path).read_bytes()).hexdigest()]
    if os.path.isdir(path):
        return ["dir"]
    return ["absent"]

links = {d: describe(d) for d in history_dests()}
files = [
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".config/opencode/opencode.jsonc",
    ".pi/agent/settings.json",
    ".gemini/antigravity-cli/settings.json",
    ".claude/data/toolkit_state.json",
]
data = []
root = home / ".claude" / "data"
for dirpath, dirnames, filenames in os.walk(root):
    dirnames.sort()
    for name in sorted(filenames):
        p = Path(dirpath) / name
        st = p.lstat()
        data.append([str(p.relative_to(root)), st.st_size, st.st_mtime_ns])
out.write_text(json.dumps({
    "links": links,
    "files": {f: describe(home / f) for f in files},
    "toolkit_home": describe(home / ".agent-toolkit"),
    "data": hashlib.sha256(json.dumps(data).encode()).hexdigest(),
    "recorded_links": sum(1 for v in links.values() if v[0] == "link"),
}, indent=1, sort_keys=True))
PY
}

BEFORE="$SCRATCH/real-home-before.json"
AFTER="$SCRATCH/real-home-after.json"
tripwire() {
  local status=$?
  [ -f "$BEFORE" ] || exit "$status"
  fingerprint "$AFTER"
  banner "tripwire: real home before vs after"
  if python3 - "$BEFORE" "$AFTER" <<'PY'; then
import json, sys
before, after = (json.load(open(p)) for p in sys.argv[1:3])
diff = []
for key in ("files", "links"):
    for path in sorted(set(before[key]) | set(after[key])):
        if before[key].get(path) != after[key].get(path):
            diff.append(f"{path}: {before[key].get(path)} -> {after[key].get(path)}")
for key in ("toolkit_home", "data"):
    if before[key] != after[key]:
        diff.append(f"{key}: changed")
if diff:
    print("THE REAL HOME CHANGED:")
    print("\n".join("  " + d for d in diff))
    sys.exit(1)
print("unchanged: recorded links, settings files, legacy data, toolkit home")
PY
    echo "Scratch and logs kept at: $SCRATCH"
    exit "$status"
  fi
  echo "Repair before anything else: from the checkout, re-run the normal install." >&2
  exit 3
}
trap tripwire EXIT
fingerprint "$BEFORE"

# ── build the scratch home ───────────────────────────────────────────────────
banner "0 building the scratch home at $SHOME"
for name in data scripts hooks icons commands output-styles settings.json settings.local.json; do
  if [ -e "$REAL_HOME/.claude/$name" ]; then cp -a "$REAL_HOME/.claude/$name" "$SHOME/.claude/"; fi
done
for rel in .config/opencode/opencode.jsonc .pi/agent/settings.json .gemini/antigravity-cli/settings.json; do
  if [ -e "$REAL_HOME/$rel" ]; then
    mkdir -p "$(dirname "$SHOME/$rel")"
    cp -a "$REAL_HOME/$rel" "$SHOME/$rel"
  fi
done

if [ "$USE_BWRAP" -eq 1 ] && [ -d "$REAL_STATE" ]; then
  mkdir -p "$SSTATE"
  for f in "$REAL_STATE"/*; do
    if [ -f "$f" ]; then cp -a "$f" "$SSTATE/"; fi
  done
  # Rewrite the copy's absolute real-home paths to the scratch home, and
  # copy every recorded link that lives outside ~/.claude, so the
  # rehearsal retires and repoints the same links the real run will.
  python3 - "$REAL_HOME" "$SHOME" "$SSTATE" <<'PY'
import json, shutil, sys
from pathlib import Path

real, scratch, state = (Path(a) for a in sys.argv[1:4])
rewritten = 0
for f in state.iterdir():
    if f.is_file() and not f.is_symlink():
        text = f.read_text(errors="surrogateescape")
        rewritten += text.count(f"{real}/")
        f.write_text(text.replace(f"{real}/", f"{scratch}/"), errors="surrogateescape")
copied = 0
history = state / "history.jsonl"
for line in history.read_text().splitlines() if history.is_file() else []:
    try:
        entry = json.loads(line)
    except ValueError:
        continue
    if not (isinstance(entry, dict) and entry.get("kind") == "symlink-created"):
        continue
    dest = Path(str(entry.get("dest", "")))
    if not dest.is_relative_to(scratch):
        continue
    source = real / dest.relative_to(scratch)
    if (dest.is_symlink() or dest.exists()) or not (source.is_symlink() or source.exists()):
        continue
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, dest, symlinks=True)
    else:
        shutil.copy2(source, dest, follow_symlinks=False)
    copied += 1
print(f"installer state: {rewritten} real-home paths rewritten, {copied} recorded links copied")
PY
else
  echo "NOTE: installer state not copied (no bwrap, or no installer state)."
  echo "      The rehearsal skips legacy-link retirement: reduced fidelity."
fi
echo "scratch ready: $(find "$SHOME" | wc -l) entries"

# ── stages ───────────────────────────────────────────────────────────────────
REAL_HOMES_RE="$REAL_HOME/\\.(claude|agent-toolkit|pi|copilot|gemini|codex|config|local)([/[:space:]\"']|$)"

stage() {
  local log="$LOGS/$1" expect="$2"
  shift 2
  local -a cmd=(env HOME="$SHOME" XDG_CONFIG_HOME="$SHOME/.config"
    XDG_CACHE_HOME="$SHOME/.cache" XDG_DATA_HOME="$SHOME/.local/share"
    XDG_STATE_HOME="$SHOME/.local/state" TMPDIR="$SCRATCH/tmp"
    PYTHONDONTWRITEBYTECODE=1)
  if [ "$USE_BWRAP" -eq 1 ]; then
    cmd+=(bwrap --ro-bind / / --dev /dev --proc /proc --bind "$SCRATCH" "$SCRATCH"
      --chdir "$CHECKOUT" --)
  fi
  (cd "$CHECKOUT" && "${cmd[@]}" "$@") 2>&1 | tee "$log" ||
    die "$(basename "$log") exited nonzero; nothing real changed if the tripwire below agrees"
  if sed "s#$SCRATCH#<scratch>#g" "$log" | grep -Eq "$REAL_HOMES_RE"; then
    sed "s#$SCRATCH#<scratch>#g" "$log" | grep -E "$REAL_HOMES_RE" | head -5 >&2
    die "$(basename "$log") names a real harness home; the redirect leaked"
  fi
  if [ -n "$expect" ] && ! grep -Eq "^$expect" "$log"; then
    die "$(basename "$log") did not report '$expect'"
  fi
}

banner "1 dry run (writes nothing)"
pause "dry run"
stage 1-dry-run.log "dry-run:ok" ./install.sh --migrate-toolkit-home "$H" "$P" --migration-id="$ID1" --dry-run --verbose
real_links="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["recorded_links"])' "$BEFORE")"
seen_links="$(sed -n 's/.*legacy-links: \([0-9][0-9]*\) installed links recorded.*/\1/p' "$LOGS/1-dry-run.log" | head -1)"
echo "legacy links: real home ${real_links}, scratch ${seen_links:-?}"
if [ "$USE_BWRAP" -eq 1 ] && [ "${seen_links:-}" != "$real_links" ]; then
  echo "WARNING: the scratch sees a different number of legacy links than the real home." >&2
fi

banner "2 migrate ($ID1)"
pause "real run against the scratch copy"
stage 2-migrate.log "committed" ./install.sh --migrate-toolkit-home "$H" "$P" --migration-id="$ID1" --skip-reconciliation --verbose

banner "3 rollback ($ID1)"
pause "rollback"
stage 3-rollback.log "rolled-back" ./install.sh --rollback-toolkit-home-migration="$ID1" --verbose

banner "4 finalize ($ID1)"
pause "finalize the rolled-back run"
stage 4-finalize.log "finalized" ./install.sh --finalize-toolkit-home-migration="$ID1" --verbose

banner "5 migrate again ($ID2)"
pause "second run"
stage 5-migrate-2.log "committed" ./install.sh --migrate-toolkit-home "$H" "$P" --migration-id="$ID2" --skip-reconciliation --verbose

banner "6 finalize ($ID2)"
pause "second finalize"
stage 6-finalize-2.log "finalized" ./install.sh --finalize-toolkit-home-migration="$ID2" --verbose

banner "7 residue audit (scratch home)"
pause "residue audit"
stage 7-residue.log "" ./install.sh --check-links "$H" "$P" --verbose

banner "rehearsal passed"
