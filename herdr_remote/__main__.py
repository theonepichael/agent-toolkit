#!/usr/bin/env python3
"""Entry point: `python -m herdr_remote serve` | `python -m herdr_remote gen-token`.

serve starts the bridge; gen-token writes a fresh mode-600 bearer token and
prints nothing but a confirmation (the token itself is only in the file).
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from aiohttp import web

from .bridge import create_app
from .config import ConfigError, generate_token, load_config, load_token
from .herdr_client import HerdrClient

log = logging.getLogger("herdr_remote.main")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _repo_version() -> str:
    """Best-effort git sha for /api/health — a fresh checkout without a .git
    dir (or without git installed) degrades to "unknown" rather than
    crashing the bridge over a diagnostic feature."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr_remote", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the bridge daemon")
    serve.add_argument(
        "--config", type=Path, default=Path("~/.config/herdr-bridge/config.toml")
    )
    gen = sub.add_parser("gen-token", help="write a fresh bearer token (mode 600)")
    gen.add_argument(
        "--token-path", type=Path, default=Path("~/.config/herdr-bridge/token")
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    if args.command == "gen-token":
        path = args.token_path.expanduser()
        generate_token(path)
        print(f"token written to {path} (mode 600)")
        return 0

    try:
        config = load_config(args.config.expanduser())
        load_token(config.token_path)  # fail fast on a missing/readable token
    except ConfigError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1

    herdr = HerdrClient(config.socket_path)
    app = create_app(config, herdr, version=_repo_version())
    log.info(
        "bridge listening on %s:%s (herdr socket %s)",
        config.bind_host,
        config.bind_port,
        config.socket_path,
    )
    web.run_app(app, host=config.bind_host, port=config.bind_port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
