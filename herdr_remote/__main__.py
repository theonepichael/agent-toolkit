#!/usr/bin/env python3
"""Entry point: `python -m herdr_remote serve` | `python -m herdr_remote gen-token`.

serve starts the bridge; gen-token writes a fresh mode-600 bearer token and
prints nothing but a confirmation (the token itself is only in the file).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aiohttp import web

from .bridge import create_app
from .config import ConfigError, generate_token, load_config, load_token
from .herdr_client import HerdrClient

log = logging.getLogger("herdr_remote.main")


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
    app = create_app(config, herdr)
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
