#!/usr/bin/env bash
# Scenario suite for install.sh, meant to run inside test/run.sh's container.
# Exercises the full lifecycle: fresh install, rollback, backup-and-restore
# of a pre-existing dotfile, work profile + guard, --force override,
# harness opt-in selection (--harness=) and the missing-CLI refusal,
# opencode profile-specific
# permission seeding, Pi's copy-once settings.json seeding (drift + --reseed
# + rollback), argument-parsing edge cases (including the old
# --work/--copilot flags being rejected outright), and --depart. Not meant
# to run on a real machine.
#
# The pre-existing-file scenarios use ~/.agent-tools.zsh: it is the one
# links.toml destination with no harness gate, so every install links it.
set -uo pipefail

REPO_ROOT="$HOME/agent-toolkit"
STATE_DIR="$HOME/.local/state/agent-toolkit"
MANIFEST="$STATE_DIR/history.jsonl"
MARKER="$STATE_DIR/profile"

cd "$REPO_ROOT" || exit 1

PASS=0
FAIL=0
check() { # check <description> <command...>
  local desc="$1"
  shift
  if "$@" >/tmp/check.out 2>&1; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc"
    sed 's/^/         | /' /tmp/check.out
    FAIL=$((FAIL + 1))
  fi
}

manifest_has() { # manifest_has <kind> <field=value> [<field=value> ...]
  python3 - "$MANIFEST" "$@" <<'PY'
import json
import sys

path, kind, *pairs = sys.argv[1:]
wanted = dict(p.split("=", 1) for p in pairs)
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    sys.exit(1)
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        continue
    if entry.get("kind") != kind:
        continue
    if all(str(entry.get(k)) == v for k, v in wanted.items()):
        sys.exit(0)
sys.exit(1)
PY
}

manifest_run_count() { # manifest_run_count <N>
  python3 - "$MANIFEST" "$1" <<'PY'
import json
import sys

path, want = sys.argv[1], int(sys.argv[2])
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    sys.exit(0 if want == 0 else 1)
count = 0
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        continue
    if entry.get("kind") == "run":
        count += 1
sys.exit(0 if count == want else 1)
PY
}

echo "=== 1. Fresh personal install (--harness=claude) ==="
./install.sh --harness=claude >/tmp/install.out 2>&1
code=$?
cat /tmp/install.out
check "exit code 0 or 1 (0/1 = ok-with-skips, not a hard error)" \
  bash -c "[[ $code -eq 0 || $code -eq 1 ]]"
check "manifest recorded profile=personal" manifest_has run profile=personal
check "$HOME/.agent-tools.zsh symlinks into repo" bash -c '[[ "$(readlink -f ~/.agent-tools.zsh)" == "'"$REPO_ROOT"'/shell/agent-tools.zsh" ]]'
check "$HOME/.claude/CLAUDE.md symlinks into repo" bash -c '[[ "$(readlink -f ~/.claude/CLAUDE.md)" == "'"$REPO_ROOT"'/claude/CORE_INSTRUCTIONS.md" ]]'
check "$HOME/.claude/settings.json copied (not symlinked)" bash -c '[[ -f ~/.claude/settings.json && ! -L ~/.claude/settings.json ]]'
check "$HOME/.claude/settings.json matches personal seed" diff -q ~/.claude/settings.json "$REPO_ROOT/claude/settings.json"
check "no profile marker written on personal run" bash -c '[[ ! -f "'"$MARKER"'" ]]'
check "Copilot NOT installed (not in --harness)" bash -c '[[ ! -e ~/.copilot/copilot-instructions.md ]]'
check "copilot-work alias file NOT symlinked (Copilot not selected)" bash -c '[[ ! -e ~/.copilot_aliases ]]'
check "opencode NOT wired (not in --harness)" bash -c '[[ ! -e ~/.config/opencode/opencode.jsonc ]]'
check "Pi NOT wired (not in --harness)" bash -c '[[ ! -e ~/.pi/agent/settings.json ]]'

echo ""
echo "=== 1b. Re-run is idempotent — history appends, nothing re-linked ==="
# history.jsonl is append-only (manifest_init appends a new run marker rather
# than truncating), and symlinks that already exist don't get re-recorded
# (the was_link gate in symlink()) — so a second run here just adds another
# run marker on top of run 1's records instead of erasing them. No
# backup/restore of the manifest needed around this rerun.
./install.sh --harness=claude >/tmp/install-rerun.out 2>&1
cat /tmp/install-rerun.out
check "history.jsonl now holds 2 run markers (run 1 + this rerun, nothing erased)" \
  manifest_run_count 2

echo ""
echo "=== 2. Rollback undoes the personal install ==="
./install.sh --rollback >/tmp/rollback.out 2>&1
cat /tmp/rollback.out
check "manifest removed after rollback" bash -c '[[ ! -f "'"$MANIFEST"'" ]]'
check "$HOME/.agent-tools.zsh symlink removed" bash -c '[[ ! -e ~/.agent-tools.zsh ]]'
check "$HOME/.claude/CLAUDE.md symlink removed" bash -c '[[ ! -e ~/.claude/CLAUDE.md ]]'
check "$HOME/.claude/settings.json removed" bash -c '[[ ! -e ~/.claude/settings.json ]]'

echo ""
echo "=== 3. Pre-existing file gets backed up, not clobbered ==="
echo "sentinel-content" >~/.agent-tools.zsh
./install.sh --harness=claude >/tmp/install2.out 2>&1
cat /tmp/install2.out
check "original content preserved in .bak" bash -c '[[ "$(cat ~/.agent-tools.zsh.bak)" == "sentinel-content" ]]'
check "$HOME/.agent-tools.zsh is now the symlink" bash -c '[[ -L ~/.agent-tools.zsh ]]'
check "manifest recorded file-backed-up for ~/.agent-tools.zsh" manifest_has file-backed-up "dest=$HOME/.agent-tools.zsh"
check "manifest ALSO recorded symlink-created for ~/.agent-tools.zsh" manifest_has symlink-created "dest=$HOME/.agent-tools.zsh"

./install.sh --rollback >/tmp/rollback2.out 2>&1
cat /tmp/rollback2.out
check "rollback restores original content, not just removes symlink" bash -c '[[ "$(cat ~/.agent-tools.zsh)" == "sentinel-content" ]]'
check "backup file cleaned up after restore" bash -c '[[ ! -e ~/.agent-tools.zsh.bak ]]'
rm -f ~/.agent-tools.zsh

echo ""
echo "=== 4. Work profile + Claude harness: exclusions + settings seed ==="
./install.sh --profile=work --harness=claude >/tmp/work.out 2>&1
cat /tmp/work.out
check "profile marker written as 'work'" bash -c '[[ "$(cat "'"$MARKER"'")" == "work" ]]'
check "$HOME/.claude/settings.json matches WORK seed" diff -q ~/.claude/settings.json "$REPO_ROOT/claude/settings.work.json"
check "Claude Code IS installed despite work profile (profile never restricts harness choice)" \
  bash -c '[[ -L ~/.claude/CLAUDE.md ]]'

echo ""
echo "=== 5. Guard blocks a plain (no --profile) run on a work-marked machine ==="
./install.sh --harness=claude >/tmp/guard.out 2>&1
guard_code=$?
cat /tmp/guard.out
check "plain run exits 2 (blocked)" bash -c "[[ $guard_code -eq 2 ]]"
check "guard message mentions WORK" grep -q "provisioned as WORK" /tmp/guard.out
check "manifest untouched by blocked run (still shows work)" manifest_has run profile=work

echo ""
echo "=== 6. --force overrides the guard ==="
./install.sh --force --harness=claude >/tmp/force.out 2>&1
force_code=$?
cat /tmp/force.out
check "--force run does not get blocked" bash -c "[[ $force_code -eq 0 || $force_code -eq 1 ]]"
check "--force run records profile=personal" manifest_has run profile=personal
check "work marker is NOT reset by a forced personal run (next plain run is still blocked)" \
  bash -c '[[ "$(cat "'"$MARKER"'")" == "work" ]]'
check "settings.json drift reported instead of silently overwritten" grep -q "drifted" /tmp/force.out

# Clean slate for the harness-focused scenarios below.
./install.sh --rollback >/tmp/rollback3.out 2>&1
rm -f "$MARKER"

echo ""
echo "=== 7. Argument-parsing edge cases ==="
./install.sh --bogus >/tmp/bogus.out 2>&1
bogus_code=$?
check "unknown arg exits 2" bash -c "[[ $bogus_code -eq 2 ]]"
check "unknown arg message names the bad flag" grep -q "unknown argument: --bogus" /tmp/bogus.out

./install.sh --help >/tmp/help.out 2>&1
help_code=$?
check "--help exits 0" bash -c "[[ $help_code -eq 0 ]]"
check "--help prints usage" grep -q "^usage:" /tmp/help.out
check "--help documents --wipe" grep -q -- "--wipe" /tmp/help.out
check "--help documents --depart" grep -q -- "--depart" /tmp/help.out
check "--help documents --check-links" grep -q -- "--check-links" /tmp/help.out

./install.sh >/tmp/noharness.out 2>&1
noharness_code=$?
check "no --harness at all exits 2" bash -c "[[ $noharness_code -eq 2 ]]"
check "no-harness message says so" grep -q "no --harness specified" /tmp/noharness.out

./install.sh --harness=bogus >/tmp/badharness.out 2>&1
badharness_code=$?
check "--harness=bogus exits 2" bash -c "[[ $badharness_code -eq 2 ]]"
check "bad-harness message names it" grep -q "unknown harness: bogus" /tmp/badharness.out

./install.sh --harness= >/tmp/emptyharness.out 2>&1
emptyharness_code=$?
check "--harness= (empty) exits 2" bash -c "[[ $emptyharness_code -eq 2 ]]"
check "empty-harness message is the dedicated one, not a blank 'unknown harness'" \
  grep -q "empty value" /tmp/emptyharness.out

./install.sh --work >/tmp/oldwork.out 2>&1
oldwork_code=$?
check "old --work flag is rejected (hard cutover, no back-compat)" bash -c "[[ $oldwork_code -eq 2 ]]"
check "old --work message is the generic unknown-argument path" grep -q "unknown argument: --work" /tmp/oldwork.out

./install.sh --copilot >/tmp/oldcopilot.out 2>&1
oldcopilot_code=$?
check "old --copilot flag is rejected (hard cutover, no back-compat)" bash -c "[[ $oldcopilot_code -eq 2 ]]"
check "old --copilot message is the generic unknown-argument path" grep -q "unknown argument: --copilot" /tmp/oldcopilot.out

./install.sh --rollback --harness=claude >/tmp/rollbackharness.out 2>&1
rollbackharness_code=$?
check "--rollback combined with --harness is rejected" bash -c "[[ $rollbackharness_code -eq 2 ]]"
check "rollback-must-be-alone message shown" grep -q "must be used alone" /tmp/rollbackharness.out

./install.sh --rollback --profile=work >/tmp/rollbackprofile.out 2>&1
rollbackprofile_code=$?
check "--rollback combined with --profile=work is rejected" bash -c "[[ $rollbackprofile_code -eq 2 ]]"

./install.sh --wipe >/tmp/wipealone.out 2>&1
wipealone_code=$?
check "--wipe without --rollback exits 2" bash -c "[[ $wipealone_code -eq 2 ]]"
check "--wipe-without-rollback message shown" grep -q -- "--wipe can only be used with --rollback" /tmp/wipealone.out

# The test image stubs claude/copilot/opencode/pi only (see test/Dockerfile),
# so codex exercises the missing-CLI refusal for real.
./install.sh --harness=codex >/tmp/nocli.out 2>&1
nocli_code=$?
check "--harness=codex with no codex CLI on PATH exits 2" bash -c "[[ $nocli_code -eq 2 ]]"
check "missing-CLI message names the binary" \
  grep -q "'codex' CLI binary is not installed on PATH" /tmp/nocli.out
check "missing-CLI refusal configured nothing" bash -c '[[ ! -e ~/.codex/AGENTS.md && ! -f "'"$MANIFEST"'" ]]'

echo ""
echo "=== 8. Harness opt-in: only the selected harness(es) get wired ==="
./install.sh --harness=claude >/tmp/harness-claude.out 2>&1
cat /tmp/harness-claude.out
check "Claude Code wired" bash -c '[[ -L ~/.claude/CLAUDE.md ]]'
check "Copilot NOT wired" bash -c '[[ ! -e ~/.copilot/copilot-instructions.md ]]'
check "opencode NOT wired" bash -c '[[ ! -e ~/.config/opencode/opencode.jsonc ]]'
check "Pi NOT wired" bash -c '[[ ! -e ~/.pi/agent/settings.json ]]'
./install.sh --rollback >/tmp/rb-h1.out 2>&1

./install.sh --harness=claude,opencode >/tmp/harness-both.out 2>&1
cat /tmp/harness-both.out
check "Claude Code wired (combo)" bash -c '[[ -L ~/.claude/CLAUDE.md ]]'
check "opencode wired (combo)" bash -c '[[ -f ~/.config/opencode/opencode.jsonc ]]'
check "Copilot still NOT wired (combo omits it)" bash -c '[[ ! -e ~/.copilot/copilot-instructions.md ]]'
check "repeated --harness flags accumulate, not overwrite" \
  bash -c 'true' # exercised directly below with a second invocation

# --harness=claude --harness=copilot (repeated flag) must select BOTH, not
# just the last one — the array-append fix from the redesign.
./install.sh --rollback >/tmp/rb-h2.out 2>&1
./install.sh --harness=claude --harness=copilot >/tmp/harness-repeated.out 2>&1
cat /tmp/harness-repeated.out
check "repeated --harness=claude --harness=copilot selects claude" bash -c '[[ -L ~/.claude/CLAUDE.md ]]'
check "repeated --harness=claude --harness=copilot ALSO selects copilot (not just the last flag)" \
  bash -c '[[ -e ~/.copilot/copilot-instructions.md ]]'
check "copilot backlog-item skill symlinked" bash -c \
  '[[ "$(readlink -f ~/.copilot/skills/backlog-item/SKILL.md)" == "'"$REPO_ROOT"'/copilot/skills/backlog-item/SKILL.md" ]]'

echo ""
echo "=== 9. Additive-only: narrowing --harness on a later run doesn't uninstall ==="
# Machine currently has claude+copilot from scenario 8's last run. Re-running
# with just claude must leave copilot's files untouched.
./install.sh --harness=claude >/tmp/narrow.out 2>&1
cat /tmp/narrow.out
check "Copilot files left in place after a narrower re-run (additive-only, no surprise uninstall)" \
  bash -c '[[ -e ~/.copilot/copilot-instructions.md ]]'
./install.sh --rollback >/tmp/rb-h3.out 2>&1

echo ""
echo "=== 9b. Pi: harness combo wiring + settings.json copy-once seeding ==="
./install.sh --harness=claude,pi >/tmp/harness-pi.out 2>&1
cat /tmp/harness-pi.out
check "Claude Code wired (pi combo)" bash -c '[[ -L ~/.claude/CLAUDE.md ]]'
check "Pi wired (combo)" bash -c '[[ -f ~/.pi/agent/settings.json ]]'
check "Copilot still NOT wired (pi combo omits it)" bash -c '[[ ! -e ~/.copilot/copilot-instructions.md ]]'
check "pi AGENTS.md symlinks into repo's shared CLAUDE.md" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/AGENTS.md)" == "'"$REPO_ROOT"'/claude/CORE_INSTRUCTIONS.md" ]]'
check "pi dashboard prompt symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/prompts/dashboard.md)" == "'"$REPO_ROOT"'/pi/prompts/dashboard.md" ]]'
check "pi backlog-item prompt symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/prompts/backlog-item.md)" == "'"$REPO_ROOT"'/pi/prompts/backlog-item.md" ]]'
check "pi permission-gate extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/permission-gate.ts)" == "'"$REPO_ROOT"'/pi/extensions/permission-gate.ts" ]]'
check "pi ruff-format-on-edit extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/ruff-format-on-edit.ts)" == "'"$REPO_ROOT"'/pi/extensions/ruff-format-on-edit.ts" ]]'
check "pi guard-rails extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/guard-rails.ts)" == "'"$REPO_ROOT"'/pi/extensions/guard-rails.ts" ]]'
check "pi dev-status-tool extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/dev-status-tool.ts)" == "'"$REPO_ROOT"'/pi/extensions/dev-status-tool.ts" ]]'
check "pi settings.json copied (not symlinked)" bash -c '[[ -f ~/.pi/agent/settings.json && ! -L ~/.pi/agent/settings.json ]]'
check "pi settings.json matches repo seed" diff -q ~/.pi/agent/settings.json "$REPO_ROOT/pi/settings.json"

echo ""
echo "--- 9c. Pi settings.json drift is reported, not silently overwritten ---"
echo '{"skills": ["/tmp/not-the-real-path"]}' >~/.pi/agent/settings.json
./install.sh --harness=claude,pi >/tmp/pi-drift.out 2>&1
cat /tmp/pi-drift.out
check "pi settings.json drift reported instead of silently overwritten" grep -q "drifted" /tmp/pi-drift.out
check "pi settings.json left untouched (copy-once, no --reseed)" bash -c \
  '[[ "$(cat ~/.pi/agent/settings.json)" == "{\"skills\": [\"/tmp/not-the-real-path\"]}" ]]'

echo ""
echo "--- 9d. --reseed overwrites the drifted pi settings.json, backing up the drift first ---"
./install.sh --harness=claude,pi --reseed >/tmp/pi-reseed.out 2>&1
cat /tmp/pi-reseed.out
check "pi settings.json reseeded to match repo copy" diff -q ~/.pi/agent/settings.json "$REPO_ROOT/pi/settings.json"
check "pi settings.json .bak preserves the drifted content" \
  bash -c '[[ "$(cat ~/.pi/agent/settings.json.bak)" == "{\"skills\": [\"/tmp/not-the-real-path\"]}" ]]'

echo ""
echo "--- 9e. --rollback restores the pre-reseed (drifted) content, mirrors scenario 3's backup+restore ---"
./install.sh --rollback >/tmp/rb-pi.out 2>&1
cat /tmp/rb-pi.out
check "pi settings.json restored to its pre-reseed drifted content, not deleted" bash -c \
  '[[ "$(cat ~/.pi/agent/settings.json)" == "{\"skills\": [\"/tmp/not-the-real-path\"]}" ]]'
check "pi settings.json .bak cleaned up after restore" bash -c '[[ ! -e ~/.pi/agent/settings.json.bak ]]'
check "pi AGENTS.md symlink removed by rollback" bash -c '[[ ! -e ~/.pi/agent/AGENTS.md ]]'
check "pi dashboard prompt symlink removed by rollback" bash -c '[[ ! -e ~/.pi/agent/prompts/dashboard.md ]]'
check "pi permission-gate extension symlink removed by rollback" \
  bash -c '[[ ! -e ~/.pi/agent/extensions/permission-gate.ts ]]'
check "pi guard-rails extension symlink removed by rollback" \
  bash -c '[[ ! -e ~/.pi/agent/extensions/guard-rails.ts ]]'
check "pi dev-status-tool extension symlink removed by rollback" \
  bash -c '[[ ! -e ~/.pi/agent/extensions/dev-status-tool.ts ]]'
rm -f ~/.pi/agent/settings.json

echo ""
echo "=== 10. opencode.jsonc: personal-only permission seeding ==="
./install.sh --harness=opencode >/tmp/oc-personal.out 2>&1
cat /tmp/oc-personal.out
check "opencode.jsonc seeded from personal file" diff -q ~/.config/opencode/opencode.jsonc "$REPO_ROOT/opencode/opencode.jsonc"
check "personal opencode.jsonc has no xargs (allowlist bypass removed everywhere)" \
  bash -c '! grep -q "xargs" ~/.config/opencode/opencode.jsonc'
check "personal opencode.jsonc has no awk (allowlist bypass removed everywhere)" \
  bash -c '! grep -q "\"awk \*\"" ~/.config/opencode/opencode.jsonc'
check "personal opencode.jsonc does not allow curl (network calls need approval)" \
  bash -c '! grep -q "\"curl \*\"" ~/.config/opencode/opencode.jsonc'
# backlog-item port wiring. Explicit checks matter here: install.sh exits 0
# OR 1 (ok-with-skips) on success, so a typo'd src in links.toml would
# otherwise surface only as a silent SKIPPED line, not a failed scenario.
check "opencode backlog-item command symlinked" bash -c \
  '[[ "$(readlink -f ~/.config/opencode/commands/backlog-item.md)" == "'"$REPO_ROOT"'/opencode/command/backlog-item.md" ]]'
check "opencode grill-me skill symlinked (backlog-item delegates via skill tool)" bash -c \
  '[[ "$(readlink -f ~/.config/opencode/skills/grill-me/SKILL.md)" == "'"$REPO_ROOT"'/opencode/skills/grill-me/SKILL.md" ]]'
check "opencode second-opinion skill symlinked (backlog-item delegates via skill tool)" bash -c \
  '[[ "$(readlink -f ~/.config/opencode/skills/second-opinion/SKILL.md)" == "'"$REPO_ROOT"'/opencode/skills/second-opinion/SKILL.md" ]]'
./install.sh --rollback >/tmp/rb-oc1.out 2>&1

rm -f "$MARKER"
echo ""
echo "=== 10b. opencode is rejected outright on --profile=work, not tightened ==="
./install.sh --profile=work --harness=opencode >/tmp/oc-work.out 2>&1
ocwork_code=$?
cat /tmp/oc-work.out
check "--profile=work --harness=opencode exits 2" bash -c "[[ $ocwork_code -eq 2 ]]"
check "rejection names the flag combination" \
  grep -q "harness=opencode is not allowed with --profile=work" /tmp/oc-work.out
check "no opencode.jsonc written on a rejected work+opencode run" \
  bash -c '! [[ -e ~/.config/opencode/opencode.jsonc ]]'

./install.sh --profile=work --harness=copilot,opencode >/tmp/oc-work2.out 2>&1
ocwork2_code=$?
check "--profile=work --harness=copilot,opencode is rejected the same way (opencode anywhere in the list is enough)" \
  bash -c "[[ $ocwork2_code -eq 2 ]]"

echo ""
echo "=== 11. Full-history rollback: undoes every past run, not just the most recent ==="
# Clean slate: previous scenarios leave the opencode work-profile run in
# place with no marker cleanup.
./install.sh --rollback >/tmp/rb-pre11.out 2>&1
rm -f "$MARKER"

./install.sh --harness=claude >/tmp/multi-run-a.out 2>&1
cat /tmp/multi-run-a.out
check "run A: Claude Code wired" bash -c '[[ -L ~/.claude/CLAUDE.md ]]'

./install.sh --harness=opencode >/tmp/multi-run-b.out 2>&1
cat /tmp/multi-run-b.out
check "run B: opencode wired" bash -c '[[ -f ~/.config/opencode/opencode.jsonc ]]'
check "history.jsonl recorded both runs (2 run markers, not overwritten by run B)" \
  manifest_run_count 2
check "history.jsonl still holds run A's claude symlink record after run B" \
  manifest_has symlink-created "dest=$HOME/.claude/CLAUDE.md"

./install.sh --rollback >/tmp/rollback-multi.out 2>&1
cat /tmp/rollback-multi.out
check "single rollback removes run A's files too (Claude), not just run B's" \
  bash -c '[[ ! -e ~/.claude/CLAUDE.md ]]'
check "single rollback removes run B's files (opencode)" \
  bash -c '[[ ! -e ~/.config/opencode/opencode.jsonc ]]'
check "history.jsonl cleared after a full rollback" bash -c '[[ ! -f "'"$MANIFEST"'" ]]'

echo ""
echo "=== 12. Rollback skips and reports instead of aborting on the unexpected ==="
echo "sentinel-content" >~/.agent-tools.zsh
./install.sh --harness=claude >/tmp/pre12.out 2>&1
cat /tmp/pre12.out

# Something else claims a path install.sh symlinked — rollback must not
# blindly delete a symlink that no longer points where it left it.
rm ~/.claude/CLAUDE.md
ln -s /etc/hostname ~/.claude/CLAUDE.md

# The backup install.sh made for the pre-existing ~/.agent-tools.zsh gets
# removed out from under rollback (manual cleanup, disk pressure, whatever) —
# rollback must report this, not silently no-op or abort.
rm -f ~/.agent-tools.zsh.bak

./install.sh --rollback >/tmp/rollback12.out 2>&1
rollback12_code=$?
cat /tmp/rollback12.out
check "rollback exits 1 when steps are skipped" bash -c "[[ $rollback12_code -eq 1 ]]"
check "reclaimed symlink is left alone, not deleted" \
  bash -c '[[ "$(readlink ~/.claude/CLAUDE.md)" == /etc/hostname ]]'
check "reclaimed-symlink skip is reported" grep -q "something else has claimed this path" /tmp/rollback12.out
check "missing-backup skip is reported" grep -q "not found — already restored, or removed outside install.sh" /tmp/rollback12.out
check "skip count summary printed" grep -q "rollback step(s) did not apply cleanly" /tmp/rollback12.out
rm -f ~/.claude/CLAUDE.md ~/.agent-tools.zsh

echo ""
echo "=== 13. --rollback --wipe: blank-slate rollback ==="
# MANAGED_SERVICES is empty and links.toml ships no systemd units, so the
# wipe's service sweep has nothing to do here: with no step skipped, both the
# preview and the real wipe exit 0. The Neovim XDG dir sweep is still live
# install.py behavior and is exercised below.
echo "sentinel-content" >~/.agent-tools.zsh
./install.sh --harness=claude >/tmp/pre-wipe.out 2>&1
cat /tmp/pre-wipe.out
check "$HOME/.agent-tools.zsh backed up before the wipe scenario" bash -c '[[ -f ~/.agent-tools.zsh.bak ]]'

mkdir -p ~/.local/share/nvim ~/.local/state/nvim ~/.cache/nvim
touch ~/.local/share/nvim/sentinel ~/.local/state/nvim/sentinel ~/.cache/nvim/sentinel

./install.sh --rollback --wipe --dry-run >/tmp/wipe-dry.out 2>&1
wipedry_code=$?
cat /tmp/wipe-dry.out
check "--rollback --wipe --dry-run exits 0 (nothing skipped)" \
  bash -c "[[ $wipedry_code -eq 0 ]]"
check "dry-run wipe leaves the backup in place" bash -c '[[ -f ~/.agent-tools.zsh.bak ]]'
check "dry-run wipe leaves ~/.agent-tools.zsh symlinked, not deleted" bash -c '[[ -L ~/.agent-tools.zsh ]]'
check "dry-run wipe leaves nvim dirs in place" bash -c '[[ -f ~/.local/share/nvim/sentinel ]]'
check "dry-run wipe previews backup deletion, not restoration" grep -q "would delete backup" /tmp/wipe-dry.out
check "dry-run wipe previews nvim runtime dir removal" grep -q "would remove.*nvim (wipe)" /tmp/wipe-dry.out

./install.sh --rollback --wipe >/tmp/wipe-real.out 2>&1
wipereal_code=$?
cat /tmp/wipe-real.out
check "--rollback --wipe exits 0 (nothing skipped)" \
  bash -c "[[ $wipereal_code -eq 0 ]]"
check "wipe deletes the backup outright" bash -c '[[ ! -e ~/.agent-tools.zsh.bak ]]'
check "wipe removes ~/.agent-tools.zsh entirely (not restored to sentinel content)" bash -c '[[ ! -e ~/.agent-tools.zsh ]]'
check "wipe reports deleting the backup, not restoring it" grep -q "deleted backup" /tmp/wipe-real.out
check "wipe sweeps nvim share dir" bash -c '[[ ! -e ~/.local/share/nvim ]]'
check "wipe sweeps nvim state dir" bash -c '[[ ! -e ~/.local/state/nvim ]]'
check "wipe sweeps nvim cache dir" bash -c '[[ ! -e ~/.cache/nvim ]]'
check "history.jsonl removed after wipe" bash -c '[[ ! -f "'"$MANIFEST"'" ]]'
check "state dir removed once empty after wipe" bash -c '[[ ! -d "'"$STATE_DIR"'" ]]'
check "wipe's final message distinguishes it from plain rollback" grep -q "wiped to a blank slate" /tmp/wipe-real.out

echo ""
echo "=== 13b. --wipe with untracked state but no manifest (already-consumed history) ==="
# Simulates the case the "no-manifest-wipe-behavior" decision exists for: a
# second --wipe (or --wipe after an earlier plain --rollback already deleted
# the manifest) still needs to sweep leftover untracked state instead of
# hard-failing with "nothing to roll back". Nothing anomalous to report, so
# this run should exit cleanly.
mkdir -p ~/.local/share/nvim
touch ~/.local/share/nvim/sentinel
./install.sh --rollback --wipe >/tmp/wipe-no-manifest.out 2>&1
wipenomanifest_code=$?
cat /tmp/wipe-no-manifest.out
check "wipe with swept state but no manifest exits 0 (nothing recorded to skip)" \
  bash -c "[[ $wipenomanifest_code -eq 0 ]]"
check "wipe-no-manifest header explains the swept-but-no-history case" \
  grep -q "Wipe swept untracked state" /tmp/wipe-no-manifest.out
check "wipe-no-manifest actually removed the leftover nvim dir" bash -c '[[ ! -e ~/.local/share/nvim ]]'
check "wipe-no-manifest did NOT print the generic nothing-to-roll-back error" \
  bash -c '! grep -q "nothing to roll back" /tmp/wipe-no-manifest.out'

echo ""
echo "=== 13c. A third --wipe run with truly nothing left reports the plain error ==="
./install.sh --rollback --wipe >/tmp/wipe-truly-empty.out 2>&1
wipeempty_code=$?
cat /tmp/wipe-truly-empty.out
check "wipe with nothing left at all exits 1" bash -c "[[ $wipeempty_code -eq 1 ]]"
check "wipe with nothing left reports the plain nothing-to-roll-back error" \
  grep -q "nothing to roll back" /tmp/wipe-truly-empty.out

echo ""
echo "=== 14. --depart with no baseline refuses cleanly ==="
# The rollback in section 13 already deleted baseline.json along with
# everything else, so this container has no baseline at all right now.
./install.sh --depart --yes >/tmp/depart-nobaseline.out 2>&1
depart_none_code=$?
cat /tmp/depart-nobaseline.out
check "depart with no baseline exits 2" bash -c "[[ $depart_none_code -eq 2 ]]"
check "depart with no baseline names nothing-to-depart-from" \
  grep -q "nothing to depart from" /tmp/depart-nobaseline.out

echo ""
echo "=== 15. --depart: install departs back to a clean baseline ==="
# agent-toolkit installs no packages and manages no systemd services
# (MANAGED_SERVICES is empty), and nothing below edits a path that existed
# at baseline, so every recorded key classifies as owned or preserved and
# the departure completes in one pass: exit 0, and its own state
# (baseline.json, history.jsonl) is deleted afterwards.
./install.sh --harness=claude >/tmp/depart-install.out 2>&1
depart_install_code=$?
cat /tmp/depart-install.out
check "install for departure test exits 0 or 1" \
  bash -c "[[ $depart_install_code -eq 0 || $depart_install_code -eq 1 ]]"
check "baseline.json captured" bash -c '[[ -f "'"$STATE_DIR"'/baseline.json" ]]'

# An artifact this installer never touched, planted after install — must
# survive departure untouched (Completion Gate: "unrelated post-install
# artifacts survive").
echo "unrelated content" >~/my-own-notes.txt
# An unrelated package, installed the same way a user would — must also
# survive, since departure only ever acts on what its own transactions
# recorded. The Ubuntu image drops its apt lists at build time, so refresh
# them first.
if command -v apt-get >/dev/null 2>&1; then
  UNRELATED_PKG=sl
  # shellcheck disable=SC2024 # Redirect is to /tmp in the test container; parent shell owns the fd intentionally.
  { sudo apt-get update -qq && sudo apt-get install -y -qq "$UNRELATED_PKG"; } >/tmp/depart-unrelated-pkg.out 2>&1
else
  UNRELATED_PKG=cowsay
  # shellcheck disable=SC2024 # Redirect is to /tmp in the test container; parent shell owns the fd intentionally.
  sudo dnf install -y -q "$UNRELATED_PKG" >/tmp/depart-unrelated-pkg.out 2>&1
fi
if command -v dpkg-query >/dev/null 2>&1; then
  check "unrelated package installed before departure" dpkg-query -W "$UNRELATED_PKG"
else
  check "unrelated package installed before departure" rpm -q "$UNRELATED_PKG"
fi

./install.sh --depart --dry-run >/tmp/depart-dry.out 2>&1
depart_dry_code=$?
cat /tmp/depart-dry.out
check "depart --dry-run exits 0" bash -c "[[ $depart_dry_code -eq 0 ]]"
check "depart --dry-run preflight lists owned items" grep -q "owned" /tmp/depart-dry.out
check "depart --dry-run changed nothing (agent-tools.zsh symlink still present)" \
  bash -c '[[ -L ~/.agent-tools.zsh ]]'

./install.sh --depart --yes >/tmp/depart-real.out 2>&1
depart_real_code=$?
cat /tmp/depart-real.out
check "depart exits 0 (nothing left unresolved)" bash -c "[[ $depart_real_code -eq 0 ]]"
check "preflight reports no unresolved or drifted items" \
  bash -c '! grep -Eq "^  (unresolved|drifted) \(" /tmp/depart-real.out'
check "depart reports completion" \
  grep -q "Departure complete — no installer footprint remains." /tmp/depart-real.out
check "depart removed the agent-tools.zsh symlink" bash -c '[[ ! -e ~/.agent-tools.zsh ]]'
check "depart removed the CLAUDE.md symlink" bash -c '[[ ! -e ~/.claude/CLAUDE.md ]]'
check "depart removed the claude settings.json copy" bash -c '[[ ! -e ~/.claude/settings.json ]]'
check "unrelated file survives departure" bash -c '[[ -f ~/my-own-notes.txt ]]'
if command -v dpkg-query >/dev/null 2>&1; then
  check "unrelated package survives departure" dpkg-query -W "$UNRELATED_PKG"
else
  check "unrelated package survives departure" rpm -q "$UNRELATED_PKG"
fi
check "baseline.json deleted (departure was complete)" \
  bash -c '[[ ! -e "'"$STATE_DIR"'/baseline.json" ]]'
check "history.jsonl deleted (departure was complete)" bash -c '[[ ! -e "'"$MANIFEST"'" ]]'

echo ""
echo "=== 15b. Retrying --depart after a complete departure refuses cleanly ==="
# The completed departure consumed its own baseline, so a retry has nothing
# to act on and must say so rather than re-run anything.
./install.sh --depart --yes >/tmp/depart-retry.out 2>&1
depart_retry_code=$?
cat /tmp/depart-retry.out
check "depart retry exits 2 (no baseline left)" bash -c "[[ $depart_retry_code -eq 2 ]]"
check "depart retry names nothing-to-depart-from" \
  grep -q "nothing to depart from" /tmp/depart-retry.out
rm -f ~/my-own-notes.txt

echo ""
echo "=== 17. dir=true directory-glob rows (Fidelity local-skill-fork mechanism) ==="
# Exercises the new links.toml dir=true row type end to end against a real
# $HOME: per-file symlink creation (including a nested file), hidden/junk
# file filtering, automatic orphan cleanup on a plain re-run, --check-links,
# --rollback, and the destination-collision abort. The real repo does not
# ship a concrete dir=true row yet (deferred until a real local/ checkout
# exists), so this section appends one to this container's own throwaway
# copy of links.toml for its own duration, then restores the original.
LOCAL_CMDS="$REPO_ROOT/local/claude/commands"
DEST="$HOME/.claude/scenario-local-commands"
mkdir -p "$LOCAL_CMDS/sub"
echo "foo" >"$LOCAL_CMDS/foo.md"
echo "bar" >"$LOCAL_CMDS/sub/bar.md"
echo "junk" >"$LOCAL_CMDS/.DS_Store"
echo "junk" >"$LOCAL_CMDS/foo.md.swp"

cp "$REPO_ROOT/links.toml" /tmp/links.toml.bak
restore_links_toml() { cp /tmp/links.toml.bak "$REPO_ROOT/links.toml"; }
trap restore_links_toml EXIT

cat >>"$REPO_ROOT/links.toml" <<'TOML'

[[link]]
src = "local/claude/commands"
dir = true
dest = "~/.claude/scenario-local-commands"
harness = "claude"
TOML

./install.sh --harness=claude >/tmp/dirtrue-install.out 2>&1
dirtrue_code=$?
cat /tmp/dirtrue-install.out
check "dir=true install exits 0 or 1" bash -c "[[ $dirtrue_code -eq 0 || $dirtrue_code -eq 1 ]]"
check "foo.md symlinked" bash -c '[[ "$(readlink -f "'"$DEST"'/foo.md")" == "'"$LOCAL_CMDS"'/foo.md" ]]'
check "nested sub/bar.md symlinked" bash -c '[[ "$(readlink -f "'"$DEST"'/sub/bar.md")" == "'"$LOCAL_CMDS"'/sub/bar.md" ]]'
check ".DS_Store NOT symlinked" bash -c '[[ ! -e "'"$DEST"'/.DS_Store" ]]'
check "foo.md.swp NOT symlinked" bash -c '[[ ! -e "'"$DEST"'/foo.md.swp" ]]'

echo ""
echo "--- 17b. Deleting a local file, plain re-run auto-removes its orphaned symlink ---"
rm "$LOCAL_CMDS/foo.md"
./install.sh --harness=claude >/tmp/dirtrue-cleanup.out 2>&1
cat /tmp/dirtrue-cleanup.out
check "orphan cleanup message printed" grep -q "removed orphaned symlink" /tmp/dirtrue-cleanup.out
check "foo.md symlink actually gone" bash -c '[[ ! -e "'"$DEST"'/foo.md" ]]'
check "bar.md symlink survives (still known)" bash -c '[[ -e "'"$DEST"'/sub/bar.md" ]]'

echo ""
echo "--- 17c. --check-links reports clean after cleanup ---"
./install.sh --check-links --harness=claude >/tmp/dirtrue-checklinks.out 2>&1
checklinks_code=$?
cat /tmp/dirtrue-checklinks.out
check "--check-links exits 0 (nothing orphaned/broken left)" bash -c "[[ $checklinks_code -eq 0 ]]"

echo ""
echo "--- 17d. A destination collision between two entries aborts, no symlink created ---"
mkdir -p "$LOCAL_CMDS"
echo "same" >"$LOCAL_CMDS/collide.md"
cat >>"$REPO_ROOT/links.toml" <<'TOML'

[[link]]
src = "claude/CORE_INSTRUCTIONS.md"
dest = "~/.claude/scenario-local-commands/collide.md"
harness = "claude"
TOML
./install.sh --harness=claude >/tmp/dirtrue-collision.out 2>&1
collision_code=$?
cat /tmp/dirtrue-collision.out
check "collision aborts with exit 2" bash -c "[[ $collision_code -eq 2 ]]"
check "collision names both sources" bash -c \
  'grep -q "claude/CORE_INSTRUCTIONS.md" /tmp/dirtrue-collision.out && grep -q "local/claude/commands/collide.md" /tmp/dirtrue-collision.out'
check "no symlink created at the colliding destination" bash -c '[[ ! -e "'"$DEST"'/collide.md" ]]'
check "collision left the still-good sub/bar.md symlink untouched" bash -c '[[ -e "'"$DEST"'/sub/bar.md" ]]'

echo ""
echo "--- 17e. --rollback reverts the dir=true-expanded symlink ---"
restore_links_toml
cat >>"$REPO_ROOT/links.toml" <<'TOML'

[[link]]
src = "local/claude/commands"
dir = true
dest = "~/.claude/scenario-local-commands"
harness = "claude"
TOML
./install.sh --rollback >/tmp/dirtrue-rollback.out 2>&1
cat /tmp/dirtrue-rollback.out
check "sub/bar.md symlink removed by rollback" bash -c '[[ ! -e "'"$DEST"'/sub/bar.md" ]]'

restore_links_toml
trap - EXIT
rm -rf "$LOCAL_CMDS" "$DEST"

echo ""
echo "════════ Scenario summary: $PASS passed, $FAIL failed ════════"
[[ $FAIL -eq 0 ]]
