#!/usr/bin/env bash
# Scenario suite for install.sh, meant to run inside test/run.sh's container.
# Exercises the full lifecycle: fresh install, rollback, backup-and-restore
# of a pre-existing dotfile, work profile + guard, --force override,
# harness opt-in selection (--harness=), opencode profile-specific
# permission seeding, Pi's copy-once settings.json seeding (drift + --reseed
# + rollback), and argument-parsing edge cases (including the old
# --work/--copilot flags being rejected outright). Not meant to run on a
# real machine.
set -uo pipefail

DOTFILES="$HOME/dotfiles"
STATE_DIR="$HOME/.local/state/dotfiles"
MANIFEST="$STATE_DIR/history.jsonl"
MARKER="$STATE_DIR/profile"

cd "$DOTFILES" || exit 1

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

baseline_key_guarded() { # baseline_key_guarded <baseline.json path> <key>
  python3 - "$1" "$2" <<'PY'
import json
import sys

path, want_key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    sys.exit(1)
for layer in data.get("layers", []):
    record = layer.get("records", {}).get(want_key)
    if record is not None:
        sys.exit(0 if record.get("needs_vscode_guard") else 1)
sys.exit(1)
PY
}

echo "=== 1. Fresh personal install (--harness=claude) ==="
./install.sh --harness=claude >/tmp/install.out 2>&1
code=$?
cat /tmp/install.out
check "exit code 0 or 1 (0/1 = ok-with-skips, not a hard error)" \
  bash -c "[[ $code -eq 0 || $code -eq 1 ]]"
check "manifest recorded profile=personal" manifest_has run profile=personal
check "$HOME/.vimrc symlinks into repo" bash -c '[[ "$(readlink -f ~/.vimrc)" == "'"$DOTFILES"'/vim/.vimrc" ]]'
check "$HOME/.zshrc symlinks into repo" bash -c '[[ "$(readlink -f ~/.zshrc)" == "'"$DOTFILES"'/zsh/.zshrc" ]]'
check "$HOME/.claude/CLAUDE.md symlinks into repo" bash -c '[[ "$(readlink -f ~/.claude/CLAUDE.md)" == "'"$DOTFILES"'/claude/global-instructions.md" ]]'
check "$HOME/.claude/settings.json copied (not symlinked)" bash -c '[[ -f ~/.claude/settings.json && ! -L ~/.claude/settings.json ]]'
check "$HOME/.claude/settings.json matches personal seed" diff -q ~/.claude/settings.json "$DOTFILES/claude/settings.json"
check "watchcommit symlinked on personal profile" bash -c '[[ -L ~/.local/bin/watchcommit ]]'
check "watchcommit systemd unit symlinked on personal profile" bash -c '[[ -L ~/.config/systemd/user/watchcommit.service ]]'
check "no profile marker written on personal run" bash -c '[[ ! -f "'"$MARKER"'" ]]'
check "watchcommit starts and exits cleanly against a non-git dir" bash -c \
  'timeout 10 ~/.local/bin/watchcommit /tmp 2>&1 | grep -q "not a git repo"'
check "JetBrainsMono Nerd Font extracted (ttf files present)" bash -c \
  '[[ -n "$(ls ~/.local/share/fonts/JetBrainsMonoNerdFont/*.ttf 2>/dev/null)" ]]'
check "Nerd Font version marker written" bash -c \
  '[[ "$(cat ~/.local/share/fonts/JetBrainsMonoNerdFont/.nerd-fonts-version 2>/dev/null)" == "3.4.0" ]]'
check "fc-list recognizes the installed Nerd Font" bash -c \
  'fc-list | grep -q "JetBrainsMono Nerd Font"'
check "Copilot NOT installed (not in --harness)" bash -c '[[ ! -e ~/.copilot/copilot-instructions.md ]]'
check "copilot-work alias file NOT symlinked (Copilot not selected)" bash -c '[[ ! -e ~/.copilot_aliases ]]'
check "opencode NOT wired (not in --harness)" bash -c '[[ ! -e ~/.config/opencode/opencode.jsonc ]]'
check "Pi NOT wired (not in --harness)" bash -c '[[ ! -e ~/.pi/agent/settings.json ]]'

echo ""
echo "=== 1b. Re-run is idempotent — no re-download of the Nerd Font ==="
# history.jsonl is append-only (manifest_init appends a new run marker rather
# than truncating), and symlinks that already exist don't get re-recorded
# (the was_link gate in symlink()) — so a second run here just adds another
# run marker on top of run 1's records instead of erasing them. No
# backup/restore of the manifest needed around this rerun.
font_mtime_before="$(stat -c %Y ~/.local/share/fonts/JetBrainsMonoNerdFont/.nerd-fonts-version)"
./install.sh --harness=claude >/tmp/install-rerun.out 2>&1
cat /tmp/install-rerun.out
font_mtime_after="$(stat -c %Y ~/.local/share/fonts/JetBrainsMonoNerdFont/.nerd-fonts-version)"
check "version marker untouched by re-run (no re-download)" bash -c \
  "[[ '$font_mtime_before' -eq '$font_mtime_after' ]]"
check "history.jsonl now holds 2 run markers (run 1 + this rerun, nothing erased)" \
  manifest_run_count 2

echo ""
echo "=== 2. Rollback undoes the personal install ==="
./install.sh --rollback >/tmp/rollback.out 2>&1
cat /tmp/rollback.out
check "manifest removed after rollback" bash -c '[[ ! -f "'"$MANIFEST"'" ]]'
check "$HOME/.vimrc symlink removed" bash -c '[[ ! -e ~/.vimrc ]]'
check "$HOME/.claude/settings.json removed" bash -c '[[ ! -e ~/.claude/settings.json ]]'
check "watchcommit symlink removed" bash -c '[[ ! -e ~/.local/bin/watchcommit ]]'
check "watchcommit systemd unit symlink removed" bash -c '[[ ! -e ~/.config/systemd/user/watchcommit.service ]]'
check "Nerd Font NOT removed by rollback (packages aren't rolled back)" bash -c \
  '[[ -n "$(ls ~/.local/share/fonts/JetBrainsMonoNerdFont/*.ttf 2>/dev/null)" ]]'

echo ""
echo "=== 3. Pre-existing file gets backed up, not clobbered ==="
echo "sentinel-content" >~/.vimrc
./install.sh --harness=claude >/tmp/install2.out 2>&1
cat /tmp/install2.out
check "original content preserved in .bak" bash -c '[[ "$(cat ~/.vimrc.bak)" == "sentinel-content" ]]'
check "$HOME/.vimrc is now the symlink" bash -c '[[ -L ~/.vimrc ]]'
check "manifest recorded file-backed-up for ~/.vimrc" manifest_has file-backed-up "dest=$HOME/.vimrc"
check "manifest ALSO recorded symlink-created for ~/.vimrc" manifest_has symlink-created "dest=$HOME/.vimrc"

./install.sh --rollback >/tmp/rollback2.out 2>&1
cat /tmp/rollback2.out
check "rollback restores original content, not just removes symlink" bash -c '[[ "$(cat ~/.vimrc)" == "sentinel-content" ]]'
check "backup file cleaned up after restore" bash -c '[[ ! -e ~/.vimrc.bak ]]'
rm -f ~/.vimrc

echo ""
echo "=== 4. Work profile + Claude harness: exclusions + settings seed ==="
./install.sh --profile=work --harness=claude >/tmp/work.out 2>&1
cat /tmp/work.out
check "profile marker written as 'work'" bash -c '[[ "$(cat "'"$MARKER"'")" == "work" ]]'
check "watchcommit excluded on work profile" bash -c '[[ ! -e ~/.local/bin/watchcommit ]]'
check "watchcommit systemd unit excluded on work profile" bash -c '[[ ! -e ~/.config/systemd/user/watchcommit.service ]]'
check "$HOME/.claude/settings.json matches WORK seed" diff -q ~/.claude/settings.json "$DOTFILES/claude/settings.work.json"
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
  '[[ "$(readlink -f ~/.copilot/skills/backlog-item/SKILL.md)" == "'"$DOTFILES"'/copilot/skills/backlog-item/SKILL.md" ]]'

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
  '[[ "$(readlink -f ~/.pi/agent/AGENTS.md)" == "'"$DOTFILES"'/claude/global-instructions.md" ]]'
check "pi dashboard prompt symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/prompts/dashboard.md)" == "'"$DOTFILES"'/pi/prompts/dashboard.md" ]]'
check "pi backlog-item prompt symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/prompts/backlog-item.md)" == "'"$DOTFILES"'/pi/prompts/backlog-item.md" ]]'
check "pi permission-gate extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/permission-gate.ts)" == "'"$DOTFILES"'/pi/extensions/permission-gate.ts" ]]'
check "pi ruff-format-on-edit extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/ruff-format-on-edit.ts)" == "'"$DOTFILES"'/pi/extensions/ruff-format-on-edit.ts" ]]'
check "pi guard-rails extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/guard-rails.ts)" == "'"$DOTFILES"'/pi/extensions/guard-rails.ts" ]]'
check "pi dev-status-tool extension symlinked" bash -c \
  '[[ "$(readlink -f ~/.pi/agent/extensions/dev-status-tool.ts)" == "'"$DOTFILES"'/pi/extensions/dev-status-tool.ts" ]]'
check "pi settings.json copied (not symlinked)" bash -c '[[ -f ~/.pi/agent/settings.json && ! -L ~/.pi/agent/settings.json ]]'
check "pi settings.json matches repo seed" diff -q ~/.pi/agent/settings.json "$DOTFILES/pi/settings.json"

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
check "pi settings.json reseeded to match repo copy" diff -q ~/.pi/agent/settings.json "$DOTFILES/pi/settings.json"
check "pi settings.json .bak preserves the drifted content" \
  bash -c '[[ "$(cat ~/.pi/agent/settings.json.bak)" == "{\"skills\": [\"/tmp/not-the-real-path\"]}" ]]'

echo ""
echo "--- 9e. --rollback restores the pre-reseed (drifted) content, mirrors scenario 3's vimrc backup+restore ---"
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
check "opencode.jsonc seeded from personal file" diff -q ~/.config/opencode/opencode.jsonc "$DOTFILES/opencode/opencode.jsonc"
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
  '[[ "$(readlink -f ~/.config/opencode/commands/backlog-item.md)" == "'"$DOTFILES"'/opencode/command/backlog-item.md" ]]'
check "opencode grill-me skill symlinked (backlog-item delegates via skill tool)" bash -c \
  '[[ "$(readlink -f ~/.config/opencode/skills/grill-me/SKILL.md)" == "'"$DOTFILES"'/opencode/skills/grill-me/SKILL.md" ]]'
check "opencode second-opinion skill symlinked (backlog-item delegates via skill tool)" bash -c \
  '[[ "$(readlink -f ~/.config/opencode/skills/second-opinion/SKILL.md)" == "'"$DOTFILES"'/opencode/skills/second-opinion/SKILL.md" ]]'
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
echo "sentinel-content" >~/.vimrc
./install.sh --harness=claude >/tmp/pre12.out 2>&1
cat /tmp/pre12.out

# Something else claims a path install.sh symlinked — rollback must not
# blindly delete a symlink that no longer points where it left it.
rm ~/.claude/CLAUDE.md
ln -s /etc/hostname ~/.claude/CLAUDE.md

# The backup install.sh made for the pre-existing ~/.vimrc gets removed out
# from under rollback (manual cleanup, disk pressure, whatever) — rollback
# must report this, not silently no-op or abort.
rm -f ~/.vimrc.bak

./install.sh --rollback >/tmp/rollback12.out 2>&1
rollback12_code=$?
cat /tmp/rollback12.out
check "rollback exits 1 when steps are skipped" bash -c "[[ $rollback12_code -eq 1 ]]"
check "reclaimed symlink is left alone, not deleted" \
  bash -c '[[ "$(readlink ~/.claude/CLAUDE.md)" == /etc/hostname ]]'
check "reclaimed-symlink skip is reported" grep -q "something else has claimed this path" /tmp/rollback12.out
check "missing-backup skip is reported" grep -q "not found — already restored, or removed outside install.sh" /tmp/rollback12.out
check "skip count summary printed" grep -q "rollback step(s) did not apply cleanly" /tmp/rollback12.out
rm -f ~/.claude/CLAUDE.md ~/.vimrc

echo ""
echo "=== 13. --rollback --wipe: blank-slate rollback ==="
# This container has no init system, so systemd --user is never reachable
# here (either systemctl itself is absent, or the probe call fails with no
# session bus to talk to) — deterministic in a plain `docker run` container,
# not env flakiness. That means _wipe_watchcommit's "systemd --user is
# unavailable" anomaly always fires once the watchcommit unit symlink
# exists, which is exactly the real-world case this branch exists to cover
# (a machine that provisioned watchcommit and no longer has systemd --user,
# e.g. a fresh WSL distro without `enable-systemd` in /etc/wsl.conf yet).
echo "sentinel-content" >~/.vimrc
./install.sh --harness=claude >/tmp/pre-wipe.out 2>&1
cat /tmp/pre-wipe.out
check "$HOME/.vimrc backed up before the wipe scenario" bash -c '[[ -f ~/.vimrc.bak ]]'
check "watchcommit unit symlinked before the wipe scenario" bash -c '[[ -L ~/.config/systemd/user/watchcommit.service ]]'

mkdir -p ~/.local/share/nvim ~/.local/state/nvim ~/.cache/nvim
touch ~/.local/share/nvim/sentinel ~/.local/state/nvim/sentinel ~/.cache/nvim/sentinel

./install.sh --rollback --wipe --dry-run >/tmp/wipe-dry.out 2>&1
wipedry_code=$?
cat /tmp/wipe-dry.out
check "--rollback --wipe --dry-run exits 1 (systemd-unavailable skip is reported even in preview)" \
  bash -c "[[ $wipedry_code -eq 1 ]]"
check "dry-run wipe leaves the backup in place" bash -c '[[ -f ~/.vimrc.bak ]]'
check "dry-run wipe leaves ~/.vimrc symlinked, not deleted" bash -c '[[ -L ~/.vimrc ]]'
check "dry-run wipe leaves nvim dirs in place" bash -c '[[ -f ~/.local/share/nvim/sentinel ]]'
check "dry-run wipe previews backup deletion, not restoration" grep -q "would delete backup" /tmp/wipe-dry.out
check "dry-run wipe previews nvim runtime dir removal" grep -q "would remove.*nvim (wipe)" /tmp/wipe-dry.out
check "dry-run wipe surfaces the watchcommit systemd-unavailable skip" \
  grep -q "systemd --user is unavailable" /tmp/wipe-dry.out

./install.sh --rollback --wipe >/tmp/wipe-real.out 2>&1
wipereal_code=$?
cat /tmp/wipe-real.out
check "--rollback --wipe exits 1 (systemd-unavailable skip pushes the tally to 1)" \
  bash -c "[[ $wipereal_code -eq 1 ]]"
check "wipe deletes the backup outright" bash -c '[[ ! -e ~/.vimrc.bak ]]'
check "wipe removes ~/.vimrc entirely (not restored to sentinel content)" bash -c '[[ ! -e ~/.vimrc ]]'
check "wipe reports deleting the backup, not restoring it" grep -q "deleted backup" /tmp/wipe-real.out
check "wipe sweeps nvim share dir" bash -c '[[ ! -e ~/.local/share/nvim ]]'
check "wipe sweeps nvim state dir" bash -c '[[ ! -e ~/.local/state/nvim ]]'
check "wipe sweeps nvim cache dir" bash -c '[[ ! -e ~/.cache/nvim ]]'
check "wipe reports the watchcommit systemd-unavailable skip" \
  grep -q "systemd --user is unavailable" /tmp/wipe-real.out
check "watchcommit unit symlink still removed by the normal manifest walk despite the skip" \
  bash -c '[[ ! -e ~/.config/systemd/user/watchcommit.service ]]'
check "history.jsonl removed after wipe" bash -c '[[ ! -f "'"$MANIFEST"'" ]]'
check "state dir removed once empty after wipe" bash -c '[[ ! -d "'"$STATE_DIR"'" ]]'
check "wipe's final message distinguishes it from plain rollback" grep -q "wiped to a blank slate" /tmp/wipe-real.out

echo ""
echo "=== 13b. --wipe with untracked state but no manifest (already-consumed history) ==="
# Simulates the case the "no-manifest-wipe-behavior" decision exists for: a
# second --wipe (or --wipe after an earlier plain --rollback already deleted
# the manifest) still needs to sweep leftover untracked state instead of
# hard-failing with "nothing to roll back". The watchcommit unit is already
# gone at this point (removed above), so this run has nothing anomalous to
# report and should exit cleanly.
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
# NOTE: by this point in the suite, apt packages and the Nerd Font were
# already installed by earlier sections and never rolled back (--rollback
# never touches packages) — so this "install" mostly re-confirms already-
# present state rather than creating everything fresh. That's realistic
# (a --depart on a machine that's been through several install.sh runs)
# and is accounted for below: already-installed-unchanged packages and the
# pre-existing Nerd Font directory are correctly *preserved*, not owned.
#
# A real interactive shell sources ~/.zshrc, which puts ~/.local/bin (uv,
# oh-my-posh) on PATH — this non-interactive script doesn't, so it's added
# explicitly here to match realistic usage for the probes below.
export PATH="$HOME/.local/bin:$PATH"

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
# recorded.
if command -v apt-get >/dev/null 2>&1; then
  UNRELATED_PKG=sl
  # shellcheck disable=SC2024 # Redirect is to /tmp in the test container; parent shell owns the fd intentionally.
  sudo apt-get install -y -qq "$UNRELATED_PKG" >/tmp/depart-unrelated-pkg.out 2>&1
else
  UNRELATED_PKG=cowsay
  # shellcheck disable=SC2024 # Redirect is to /tmp in the test container; parent shell owns the fd intentionally.
  sudo dnf install -y -q "$UNRELATED_PKG" >/tmp/depart-unrelated-pkg.out 2>&1
fi

./install.sh --depart --dry-run >/tmp/depart-dry.out 2>&1
depart_dry_code=$?
cat /tmp/depart-dry.out
check "depart --dry-run exits 0 regardless of what's unresolved" \
  bash -c "[[ $depart_dry_code -eq 0 ]]"
check "depart --dry-run preflight lists owned items" grep -q "owned" /tmp/depart-dry.out
check "depart --dry-run changed nothing (vimrc symlink still present)" \
  bash -c '[[ -L ~/.vimrc ]]'

./install.sh --depart --yes >/tmp/depart-real.out 2>&1
depart_real_code=$?
cat /tmp/depart-real.out
# This container has no systemd (no systemctl on PATH), so every managed
# service's probe always comes back "unknown" and the departure always
# finishes with those services unresolved — exit 1, not 0. That's the
# correct, specified behavior (systemd --user unavailable marks
# service/linger unresolved, never treated as already-clean), not a test
# bug: verify it's *specifically* the two systemd service keys, nothing
# else. Two, not one, since opencode-skills-sync.service joined
# watchcommit.service in MANAGED_SERVICES (2026-08-22) — update this count
# again if a future service joins that list.
check "depart exits 1 (only the systemd-unavailable service keys unresolved)" \
  bash -c "[[ $depart_real_code -eq 1 ]]"
check "preflight reports exactly two unresolved items" \
  grep -q "unresolved (2):" /tmp/depart-real.out
check "depart's unresolved items are the two systemd service keys" bash -c \
  'grep -A2 "unresolved (2):" /tmp/depart-real.out | grep -q "service:systemd/watchcommit" &&
   grep -A2 "unresolved (2):" /tmp/depart-real.out | grep -q "service:systemd/opencode-skills-sync"'
check "depart removed the vimrc symlink" bash -c '[[ ! -e ~/.vimrc ]]'
check "depart removed the zshrc symlink" bash -c '[[ ! -e ~/.zshrc ]]'
check "depart removed the claude settings.json copy" bash -c '[[ ! -e ~/.claude/settings.json ]]'
check "depart removed the watchcommit shim symlink" bash -c '[[ ! -e ~/.local/bin/watchcommit ]]'
check "pre-existing Nerd Font directory survives (preserved, not owned — it predates this install)" \
  bash -c '[[ -e ~/.local/share/fonts/JetBrainsMonoNerdFont ]]'
check "unrelated file survives departure" bash -c '[[ -f ~/my-own-notes.txt ]]'
if command -v dpkg-query >/dev/null 2>&1; then
  check "unrelated package survives departure" dpkg-query -W "$UNRELATED_PKG"
else
  check "unrelated package survives departure" rpm -q "$UNRELATED_PKG"
fi
check "baseline.json and blobs are retained (departure was incomplete)" \
  bash -c '[[ -f "'"$STATE_DIR"'/baseline.json" ]]'

echo ""
echo "=== 15b. Retrying --depart with the two unresolved items still there is a no-op ==="
# Nothing left for the file/directory/package/runtime phases to do (the
# ledger already marked every actionable item complete on the previous
# run) — this should re-report the exact same two unresolved items
# without erroring or re-attempting anything already done.
./install.sh --depart --yes >/tmp/depart-retry.out 2>&1
depart_retry_code=$?
cat /tmp/depart-retry.out
check "depart retry still exits 1 (same unresolved service keys)" \
  bash -c "[[ $depart_retry_code -eq 1 ]]"
check "depart retry reports exactly two unresolved items, same as before" \
  grep -q "unresolved (2):" /tmp/depart-retry.out
check "depart retry's owned bucket never re-lists the already-removed vimrc symlink" bash -c \
  "! awk '/^  owned \\(/{f=1;next} /^  [a-z]+ \\(/{f=0} f' /tmp/depart-retry.out | grep -q vimrc"

echo ""
echo "=== 16. --depart: Windows-side VS Code guard (mocked /mnt/c + tasklist.exe) ==="
# Self-contained: real WSL interop isn't available in this container, so
# /mnt/c, the Windows-side `code` CLI, and tasklist.exe are all stubbed.
# Runs after the full 1-15b lifecycle, on a container that already has two
# permanently-unresolved items (the systemd watchcommit and
# opencode-skills-sync service keys) and a retained baseline.json from
# section 15's incomplete departure -- this section adds
# to that state rather than assuming a clean machine.
WIN_USER="$(id -un)"
FAKE_MNT_C="/mnt/c"
CODE_SHIM_DIR="$FAKE_MNT_C/Users/$WIN_USER/AppData/Local/vscode-shim"
VSCODE_USER_DIR="$FAKE_MNT_C/Users/$WIN_USER/AppData/Roaming/Code/User"
STUB_BIN_DIR="$HOME/.vscode-guard-stubs"

# 16a. Fake Windows layout + code/tasklist.exe stubs on PATH.
# /mnt is root-owned by default in the test image; the code shim's own
# resolved path must live under /mnt/ for _vscode_wsl_user_dir's check.
sudo mkdir -p "$CODE_SHIM_DIR" "$VSCODE_USER_DIR"
sudo chown -R "$WIN_USER" "$FAKE_MNT_C"

mkdir -p "$STUB_BIN_DIR"
cat >"$CODE_SHIM_DIR/code" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$CODE_SHIM_DIR/code"

cat >"$STUB_BIN_DIR/tasklist.exe" <<'SH'
#!/usr/bin/env bash
if [[ -n "${FAKE_TASKLIST_CODE_RUNNING:-}" ]]; then
  echo "Code.exe                     1234 Console                    1     50,000 K"
else
  echo "INFO: No tasks are running which match the specified criteria."
fi
exit 0
SH
chmod +x "$STUB_BIN_DIR/tasklist.exe"

ORIGINAL_PATH="$PATH"
export PATH="$STUB_BIN_DIR:$CODE_SHIM_DIR:$PATH"
export WSL_DISTRO_NAME=FakeWSL

check "fake code shim resolves on PATH" bash -c 'command -v code >/dev/null'
check "fake tasklist.exe resolves on PATH" bash -c 'command -v tasklist.exe >/dev/null'

# 16b. Re-run install so seed_vscode_settings seeds both files under the
# fake Windows user dir and capture_departure_baseline tags them guarded.
./install.sh --harness=claude >/tmp/vscode-guard-install.out 2>&1
vscode_install_code=$?
cat /tmp/vscode-guard-install.out
check "install with VS Code stub exits 0 or 1" \
  bash -c "[[ $vscode_install_code -eq 0 || $vscode_install_code -eq 1 ]]"
check "settings.json seeded under the fake Windows user dir" \
  bash -c '[[ -f "'"$VSCODE_USER_DIR"'/settings.json" ]]'
check "keybindings.json seeded under the fake Windows user dir" \
  bash -c '[[ -f "'"$VSCODE_USER_DIR"'/keybindings.json" ]]'
check "baseline tags settings.json as VS Code guarded" \
  baseline_key_guarded "$STATE_DIR/baseline.json" "file:$VSCODE_USER_DIR/settings.json"
check "baseline tags keybindings.json as VS Code guarded" \
  baseline_key_guarded "$STATE_DIR/baseline.json" "file:$VSCODE_USER_DIR/keybindings.json"

# 16c. Dry-run preflight while tasklist.exe reports Code.exe running.
export FAKE_TASKLIST_CODE_RUNNING=1
./install.sh --depart --dry-run >/tmp/vscode-guard-dry.out 2>&1
vscode_dry_code=$?
cat /tmp/vscode-guard-dry.out
check "depart --dry-run with VS Code running exits 0" \
  bash -c "[[ $vscode_dry_code -eq 0 ]]"
check "dry-run preflight flags settings.json as guarded" bash -c \
  'grep "User/settings.json" /tmp/vscode-guard-dry.out | grep -Fq "Windows VS Code is running"'
check "dry-run preflight flags keybindings.json as guarded" bash -c \
  'grep "User/keybindings.json" /tmp/vscode-guard-dry.out | grep -Fq "Windows VS Code is running"'

# 16d. Real depart while still "running" -- guard blocks removal.
./install.sh --depart --yes >/tmp/vscode-guard-real1.out 2>&1
vscode_real1_code=$?
cat /tmp/vscode-guard-real1.out
check "depart --yes with VS Code running exits 1 (guard blocks removal)" \
  bash -c "[[ $vscode_real1_code -eq 1 ]]"
check "settings.json still present (guard blocked removal)" \
  bash -c '[[ -f "'"$VSCODE_USER_DIR"'/settings.json" ]]'
check "keybindings.json still present (guard blocked removal)" \
  bash -c '[[ -f "'"$VSCODE_USER_DIR"'/keybindings.json" ]]'
# do_depart's "attempted but not completed" listing strips the "unresolved: "
# prefix off the ledger outcome before printing it (partition(": ")[2]) --
# assert on the reason text as actually printed, not the raw ledger string.
check "attempted-but-not-completed lists settings.json as guard-unresolved" bash -c \
  'grep "User/settings.json" /tmp/vscode-guard-real1.out | grep -Fq "Windows VS Code is running"'
check "attempted-but-not-completed lists keybindings.json as guard-unresolved" bash -c \
  'grep "User/keybindings.json" /tmp/vscode-guard-real1.out | grep -Fq "Windows VS Code is running"'

# 16e. Flip to "not running". The dry-run directly confirms the guard
# condition itself is now clear (annotation gone). A real `--depart --yes`
# retry now also picks this up: execute_file_symlink_phase re-evaluates
# any key whose ledger history shows a VS-Code-guard-block outcome,
# regardless of the ledger's general done-state exclusion, so this is a
# genuine second attempt -- not a ledger-stripped simulation of a first
# one, which is what this section used to do before the guard's retry
# behavior was fixed.
unset FAKE_TASKLIST_CODE_RUNNING
./install.sh --depart --dry-run >/tmp/vscode-guard-dry2.out 2>&1
vscode_dry2_code=$?
cat /tmp/vscode-guard-dry2.out
check "depart --dry-run with VS Code not running exits 0" \
  bash -c "[[ $vscode_dry2_code -eq 0 ]]"
check "dry-run preflight no longer flags settings.json" bash -c \
  '! grep "User/settings.json" /tmp/vscode-guard-dry2.out | grep -Fq "Windows VS Code is running"'
check "dry-run preflight no longer flags keybindings.json" bash -c \
  '! grep "User/keybindings.json" /tmp/vscode-guard-dry2.out | grep -Fq "Windows VS Code is running"'

./install.sh --depart --yes >/tmp/vscode-guard-real2.out 2>&1
vscode_real2_code=$?
cat /tmp/vscode-guard-real2.out
check "depart --yes with VS Code not running (real retry) still exits 1 (systemd item still unresolved)" \
  bash -c "[[ $vscode_real2_code -eq 1 ]]"
check "settings.json removed once the guard genuinely allows it" \
  bash -c '[[ ! -e "'"$VSCODE_USER_DIR"'/settings.json" ]]'
check "keybindings.json removed once the guard genuinely allows it" \
  bash -c '[[ ! -e "'"$VSCODE_USER_DIR"'/keybindings.json" ]]'
check "baseline.json still retained (departure remains incomplete)" \
  bash -c '[[ -f "'"$STATE_DIR"'/baseline.json" ]]'

# Restore PATH/env so nothing from this section leaks into whatever runs
# after it -- no current section relies on is_wsl being false, but this
# is the only section that mutates either, so it cleans up after itself.
export PATH="$ORIGINAL_PATH"
unset WSL_DISTRO_NAME

echo ""
echo "=== 17. dir=true directory-glob rows (Fidelity local-skill-fork mechanism) ==="
# Exercises the new links.toml dir=true row type end to end against a real
# $HOME: per-file symlink creation (including a nested file), hidden/junk
# file filtering, automatic orphan cleanup on a plain re-run, --check-links,
# --rollback, and the destination-collision abort. The real repo does not
# ship a concrete dir=true row yet (deferred until a real local/ checkout
# exists), so this section appends one to this container's own throwaway
# copy of links.toml for its own duration, then restores the original.
LOCAL_CMDS="$DOTFILES/local/claude/commands"
DEST="$HOME/.claude/scenario-local-commands"
mkdir -p "$LOCAL_CMDS/sub"
echo "foo" >"$LOCAL_CMDS/foo.md"
echo "bar" >"$LOCAL_CMDS/sub/bar.md"
echo "junk" >"$LOCAL_CMDS/.DS_Store"
echo "junk" >"$LOCAL_CMDS/foo.md.swp"

cp "$DOTFILES/links.toml" /tmp/links.toml.bak
restore_links_toml() { cp /tmp/links.toml.bak "$DOTFILES/links.toml"; }
trap restore_links_toml EXIT

cat >>"$DOTFILES/links.toml" <<'TOML'

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
cat >>"$DOTFILES/links.toml" <<'TOML'

[[link]]
src = "claude/global-instructions.md"
dest = "~/.claude/scenario-local-commands/collide.md"
harness = "claude"
TOML
./install.sh --harness=claude >/tmp/dirtrue-collision.out 2>&1
collision_code=$?
cat /tmp/dirtrue-collision.out
check "collision aborts with exit 2" bash -c "[[ $collision_code -eq 2 ]]"
check "collision names both sources" bash -c \
  'grep -q "claude/global-instructions.md" /tmp/dirtrue-collision.out && grep -q "local/claude/commands/collide.md" /tmp/dirtrue-collision.out'
check "no symlink created at the colliding destination" bash -c '[[ ! -e "'"$DEST"'/collide.md" ]]'
check "collision left the still-good sub/bar.md symlink untouched" bash -c '[[ -e "'"$DEST"'/sub/bar.md" ]]'

echo ""
echo "--- 17e. --rollback reverts the dir=true-expanded symlink ---"
restore_links_toml
cat >>"$DOTFILES/links.toml" <<'TOML'

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
