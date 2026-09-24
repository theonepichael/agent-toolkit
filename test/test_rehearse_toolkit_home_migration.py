#!/usr/bin/env python3
"""Tests for scripts/rehearse-toolkit-home-migration.sh.

The script is driven against a stub checkout whose install.sh imitates the
migration's dangerous behavior on demand: retiring every link the installer
history records, deleting a path outside the scratch home, or printing a
real-home path. The migration itself is covered by test_migrate_toolkit_home*;
these tests cover only the rehearsal's own guarantees: the copied history
is rewritten, bwrap blocks escapes, leaked real-home paths abort, and the
tripwire reports a changed real home.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "rehearse-toolkit-home-migration.sh"

STUB = r"""#!/usr/bin/env bash
set -eu
case " $* " in
  *" --dry-run "*)
    echo "  [ok] legacy-links: ${STUB_LINKS:-2} installed links recorded"
    echo "dry-run:ok  (migration stub)"
    ;;
  *" --migrate-toolkit-home "*)
    hist="$XDG_STATE_HOME/agent-toolkit/history.jsonl"
    if [ "${STUB_RETIRE:-}" = 1 ] && [ -f "$hist" ]; then
      python3 - "$hist" <<'PY'
import json, os, sys
for line in open(sys.argv[1]):
    entry = json.loads(line)
    if os.path.islink(entry["dest"]):
        os.unlink(entry["dest"])
        print("retired:", entry["dest"])
PY
    fi
    if [ -n "${STUB_ESCAPE:-}" ]; then rm "$STUB_ESCAPE"; fi
    if [ -n "${STUB_ECHO:-}" ]; then echo "$STUB_ECHO"; fi
    echo "committed: every domain moved  (migration stub)"
    ;;
  *"--rollback-toolkit-home-migration"*) echo "rolled-back: ok  (migration stub)" ;;
  *"--finalize-toolkit-home-migration"*) echo "finalized: ok  (migration stub)" ;;
  *"--check-links"*) echo "  2 of 2 entries checked" ;;
  *) echo "stub: unexpected $*" >&2; exit 9 ;;
esac
"""


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_usable(), reason="bwrap cannot sandbox here")


@pytest.fixture
def machine(tmp_path: Path) -> dict[str, Path]:
    """A legacy home with two recorded links, plus a stub checkout."""
    home = tmp_path / "home"  # per test: the suite's sandbox HOME is shared
    checkout = tmp_path / "checkout"
    (checkout / "agent-scripts").mkdir(parents=True)
    (checkout / "agent-scripts" / "tool.py").write_text("# tool\n")
    install = checkout / "install.sh"
    install.write_text(STUB)
    install.chmod(0o755)

    data = home / ".claude" / "data" / "grill"
    data.mkdir(parents=True)
    (data / "plan.md").write_text("# plan\n")
    claude_link = home / ".claude" / "scripts" / "tool.py"
    pi_link = home / ".pi" / "agent" / "extensions" / "tool.py"
    history = home / ".local" / "state" / "agent-toolkit" / "history.jsonl"
    for link in (claude_link, pi_link):
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(checkout / "agent-scripts" / "tool.py")
    history.parent.mkdir(parents=True)
    history.write_text(
        "".join(
            json.dumps({"kind": "symlink-created", "dest": str(link), "src": "x"}) + "\n"
            for link in (claude_link, pi_link)
        )
    )
    return {
        "home": home,
        "checkout": checkout,
        "scratch": home / "scratch",
        "claude_link": claude_link,
        "pi_link": pi_link,
    }


def _rehearse(m: dict[str, Path], *args: str, **stub_env: str) -> subprocess.CompletedProcess:
    home = m["home"]
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        **stub_env,
    }
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--harness=claude,pi",
            f"--checkout={m['checkout']}",
            f"--scratch={m['scratch']}",
            "--yes",
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _real_links_intact(m: dict[str, Path]) -> bool:
    return m["claude_link"].is_symlink() and m["pi_link"].is_symlink()


@pytest.mark.allow_real_subprocess  # runs the script, bwrap and the stub; all writes stay under tmp_path
@needs_bwrap
def test_rewrites_the_copied_history_and_leaves_the_real_home(machine):
    result = _rehearse(machine, STUB_RETIRE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _real_links_intact(machine)
    scratch_home = machine["scratch"] / "home"
    history = (scratch_home / ".local/state/agent-toolkit/history.jsonl").read_text()
    assert str(scratch_home / ".pi/agent/extensions/tool.py") in history
    assert f'"{machine["home"]}/.' not in history
    migrate_log = (machine["scratch"] / "logs" / "2-migrate.log").read_text()
    assert f"retired: {scratch_home}/.pi/agent/extensions/tool.py" in migrate_log
    assert "legacy links: real home 2, scratch 2" in result.stdout
    assert "unchanged: recorded links" in result.stdout


@pytest.mark.allow_real_subprocess  # the stub's escape runs inside bwrap and must fail
@needs_bwrap
def test_bwrap_blocks_a_write_outside_the_scratch(machine):
    result = _rehearse(machine, STUB_ESCAPE=str(machine["claude_link"]))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "2-migrate.log exited nonzero" in result.stderr
    assert _real_links_intact(machine)
    assert "unchanged: recorded links" in result.stdout


@pytest.mark.allow_real_subprocess  # runs the script and the stub under tmp_path
@needs_bwrap
def test_aborts_when_a_report_names_the_real_home(machine):
    result = _rehearse(machine, STUB_ECHO=f"{machine['home']}/.claude/data/grill/plan.md")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "names a real harness home" in result.stderr


@pytest.mark.allow_real_subprocess  # without bwrap the stub really deletes a link under tmp_path
def test_tripwire_reports_a_changed_real_home(machine):
    result = _rehearse(machine, "--without-bwrap", STUB_ESCAPE=str(machine["claude_link"]))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "THE REAL HOME CHANGED" in result.stdout
    assert str(machine["claude_link"]) in result.stdout


@pytest.mark.allow_real_subprocess  # runs the script and the stub under tmp_path
def test_without_bwrap_does_not_copy_the_installer_state(machine):
    result = _rehearse(machine, "--without-bwrap", STUB_LINKS="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reduced fidelity" in result.stdout
    assert not (machine["scratch"] / "home/.local/state/agent-toolkit").exists()
    assert _real_links_intact(machine)


@pytest.mark.allow_real_subprocess  # runs the script; it refuses before writing anything
def test_refuses_a_machine_that_is_already_migrated(machine):
    (machine["home"] / ".claude/data/toolkit_state.json").write_text(
        '{"schema": 1, "layout": "toolkit-home"}'
    )
    result = _rehearse(machine)
    assert result.returncode == 1
    assert "already has a layout pointer" in result.stderr
    assert not machine["scratch"].exists()
