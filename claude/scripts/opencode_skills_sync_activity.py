#!/usr/bin/env python3
"""Print opencode-skills-sync's pause state and last known snapshot commit,
so a session can tell whether the daemon is running and how current its
mirror is -- mirrors watchcommit_activity.py's SessionStart banner role.

Unlike watchcommit, opencode_skills_sync.py is commit-only into a dedicated
worktree (see scripts/opencode_skills_sync.py's docstring): every commit it
makes there carries a fixed message, so the destination worktree's own git
log already is the activity record -- no separate state file to write or
read."""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import opencode_skills_sync  # noqa: E402

DEST_DEFAULT = opencode_skills_sync.DEST_DEFAULT


def _ago(epoch_seconds: int) -> str:
    then = datetime.fromtimestamp(epoch_seconds, UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _last_snapshot(dest_worktree: Path) -> str | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(dest_worktree),
            "log",
            "-1",
            "--fixed-strings",
            f"--grep={opencode_skills_sync.COMMIT_MESSAGE}",
            "--format=%H%x00%ct",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    sha, timestamp = result.stdout.strip().split("\x00")
    return f"opencode-skills-sync: last snapshot {sha[:7]} ({_ago(int(timestamp))})"


def report(dest_worktree: Path) -> str:
    lines: list[str] = []

    if opencode_skills_sync.PAUSE_FILE.exists():
        lines.append("opencode-skills-sync: paused")

    if (dest_worktree / ".git").exists():
        snapshot = _last_snapshot(dest_worktree)
        if snapshot:
            lines.append(snapshot)
    else:
        lines.append(
            f"opencode-skills-sync: mirror worktree missing at {dest_worktree} -- "
            f"run git worktree add {dest_worktree} -b opencode-skills-live from the dotfiles repo"
        )

    return "\n".join(lines)


def main() -> None:
    output = report(DEST_DEFAULT)
    if output:
        print(output)


if __name__ == "__main__":
    main()
