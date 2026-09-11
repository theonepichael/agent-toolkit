#!/usr/bin/env python3
"""Codex CLI notification adapter.

Invoked by Codex CLI when configured via `notify` in `~/.codex/config.toml`
upon `agent-turn-complete`. Receives Codex's JSON event payload as a CLI argument
(or on stdin), extracts the message body, and dispatches to the shared notification
dispatcher at `~/.claude/scripts/notify.py`.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path


def extract_message(raw_payload: str) -> str:
    """Extract a human-readable notification message from a Codex JSON event payload."""
    if not raw_payload:
        return "Task completed"
    try:
        data = json.loads(raw_payload)
        if isinstance(data, dict):
            msg = data.get("last-assistant-message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
    except (json.JSONDecodeError, ValueError):
        pass
    return "Task completed"


def handle_notify(args: list[str] | None = None) -> None:
    """Handle the notification invocation from Codex CLI."""
    if args is None:
        args = sys.argv[1:]

    payload = ""
    if args:
        payload = args[0].strip()
    elif not sys.stdin.isatty():
        with contextlib.suppress(OSError):
            payload = sys.stdin.read().strip()

    message = extract_message(payload)

    # Locate notify.py: runtime installed path preferred, fallback to repo location
    notify_script = Path.home() / ".claude" / "scripts" / "notify.py"
    if not notify_script.exists():
        repo_notify = (
            Path(__file__).resolve().parent.parent / "agent-scripts" / "notify.py"
        )
        if repo_notify.exists():
            notify_script = repo_notify

    cmd = [
        "python3",
        str(notify_script),
        "--harness",
        "Codex",
        "--title",
        "Codex CLI",
        "--message",
        message,
        "--type",
        "completed",
    ]

    with contextlib.suppress(Exception):
        subprocess.run(cmd, timeout=10, check=False)


if __name__ == "__main__":
    handle_notify()
