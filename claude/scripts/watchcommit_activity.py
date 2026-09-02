#!/usr/bin/env python3
"""Print watchcommit's last known background pull/commit/push, so a session
(or wc-status) can tell daemon-driven git state changes from manual ones
instead of only seeing a clean/up-to-date working tree."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    / "watchcommit"
)
ACTIVITY_STATE_FILE = STATE_DIR / "last-activity.json"


def _ago(timestamp: str) -> str:
    then = datetime.fromisoformat(timestamp)
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


def main() -> None:
    try:
        data = json.loads(ACTIVITY_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return

    lines: list[str] = []

    pull = data.get("last_pull")
    if pull:
        lines.append(
            f"watchcommit: last background pull {pull['from_sha'][:7]}..{pull['to_sha'][:7]} ({_ago(pull['timestamp'])})"
        )

    commit = data.get("last_commit")
    if commit:
        lines.append(
            f'watchcommit: last auto-commit {commit["sha"][:7]} "{commit["message"]}" ({_ago(commit["timestamp"])})'
        )

    push = data.get("last_push")
    if push:
        lines.append(
            f"watchcommit: last auto-push {push['sha'][:7]} via {push['trigger']} ({_ago(push['timestamp'])})"
        )

    if lines:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
