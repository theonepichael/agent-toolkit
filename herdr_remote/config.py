"""Bridge configuration and bearer-token handling.

Token file rules (grill decision `auth-model`): 32+ bytes, mode 600,
yanil-owned; a world-readable token file is refused at load so a perms
regression can't silently publish agent control.
"""

from __future__ import annotations

import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """A configuration or token problem that must stop the bridge."""


@dataclass(frozen=True)
class Config:
    socket_path: Path
    token_path: Path
    assets_dir: Path
    bind_host: str = "0.0.0.0"
    bind_port: int = 8765
    herdr_wait_timeout_ms: int = 60_000
    keepalive_interval_s: float = 15.0
    sse_client_cap: int = 8
    read_lines_default: int = 200
    read_lines_max: int = 1000


def generate_token(token_path: Path) -> str:
    """Write a fresh 32+ byte token, mode 600, and return it."""
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token)
    token_path.chmod(0o600)
    return token


def load_token(token_path: Path) -> str:
    if not token_path.is_file():
        raise ConfigError(f"token file missing: {token_path}")
    mode = stat.S_IMODE(token_path.stat().st_mode)
    if mode != 0o600:
        raise ConfigError(f"token file {token_path} must be mode 600 (is {oct(mode)})")
    token = token_path.read_text().strip()
    if len(token) < 32:
        raise ConfigError(
            f"token file {token_path} holds fewer than 32 bytes — regenerate it"
        )
    return token


def load_config(path: Path) -> Config:
    """Parse the bridge config toml. Only socket_path/token_path/assets_dir
    are required; bind and tuning fields default per the spec."""
    if not path.is_file():
        raise ConfigError(f"config file missing: {path}")
    raw = tomllib.loads(path.read_text())
    for key in ("socket_path", "token_path", "assets_dir"):
        if key not in raw:
            raise ConfigError(f"config file {path} is missing required key `{key}`")
    cfg = Config(
        socket_path=Path(raw["socket_path"]).expanduser(),
        token_path=Path(raw["token_path"]).expanduser(),
        assets_dir=Path(raw["assets_dir"]).expanduser(),
        bind_host=raw.get("bind_host", "0.0.0.0"),
        bind_port=raw.get("bind_port", 8765),
        herdr_wait_timeout_ms=raw.get("herdr_wait_timeout_ms", 60_000),
        keepalive_interval_s=raw.get("keepalive_interval_s", 15.0),
        sse_client_cap=raw.get("sse_client_cap", 8),
        read_lines_default=raw.get("read_lines_default", 200),
        read_lines_max=raw.get("read_lines_max", 1000),
    )
    if not cfg.assets_dir.is_dir():
        raise ConfigError(f"assets_dir does not exist: {cfg.assets_dir}")
    return cfg
