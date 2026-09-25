#!/usr/bin/env python3
"""Control per-session trust for the supported OpenCode permission plugin."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

TOOLKIT_DATA = "none"
STATE_RELATIVE = Path(".local/state/agent-toolkit/trust-sessions")


def state_path(session_id: str) -> Path:
    return Path.home() / STATE_RELATIVE / f"{quote(session_id, safe='')}.json"


def read_state(session_id: str) -> dict[str, object]:
    try:
        value = json.loads(state_path(session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"trusted": False}
    if not isinstance(value, dict) or not isinstance(value.get("trusted"), bool):
        return {"trusted": False}
    return {"trusted": value["trusted"]}


def write_state(session_id: str, trusted: bool) -> None:
    destination = state_path(session_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {"trusted": trusted, "updatedAt": time.time_ns()}
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("on", "off", "status"))
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    if args.operation == "status":
        print("trusted" if read_state(args.session_id)["trusted"] else "untrusted")
        return 0
    write_state(args.session_id, args.operation == "on")
    print("trusted" if args.operation == "on" else "untrusted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
