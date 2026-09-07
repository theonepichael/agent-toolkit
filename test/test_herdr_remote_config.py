#!/usr/bin/env python3
"""Tests for herdr_remote.config: token generation/loading and config parsing.

Everything runs against tmp_path — never the real ~/.config/herdr-bridge/.
Requires Python 3.12+.
"""

import os
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import herdr_remote.config as config  # noqa: E402


def test_gen_token_writes_32_plus_bytes_mode_600(tmp_path):
    token_path = tmp_path / "token"
    config.generate_token(token_path)
    raw = token_path.read_text()
    assert len(raw.strip()) >= 32
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_gen_token_generates_a_fresh_token_each_call(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    config.generate_token(one)
    config.generate_token(two)
    assert one.read_text() != two.read_text()


def test_load_token_accepts_mode_600(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("a" * 40)
    os.chmod(token_path, 0o600)
    assert config.load_token(token_path) == "a" * 40


def test_load_token_refuses_world_readable(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("a" * 40)
    os.chmod(token_path, 0o644)
    try:
        config.load_token(token_path)
    except config.ConfigError as exc:
        assert "600" in str(exc)
    else:
        raise AssertionError("world-readable token must be refused")


def test_load_token_refuses_missing(tmp_path):
    try:
        config.load_token(tmp_path / "absent")
    except config.ConfigError:
        pass
    else:
        raise AssertionError("missing token must be refused")


def test_load_config_parses_all_fields(tmp_path):
    token = tmp_path / "token"
    token.write_text("a" * 40)
    os.chmod(token, 0o600)
    socket_path = tmp_path / "herdr.sock"
    assets = tmp_path / "pwa"
    assets.mkdir()
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'socket_path = "{socket_path}"\n'
        f'token_path = "{token}"\n'
        f'assets_dir = "{assets}"\n'
        'bind_host = "0.0.0.0"\n'
        "bind_port = 8765\n"
    )
    cfg = config.load_config(cfg_file)
    assert cfg.socket_path == socket_path
    assert cfg.token_path == token
    assert cfg.assets_dir == assets
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.bind_port == 8765


def test_load_config_defaults_bind_and_port(tmp_path):
    token = tmp_path / "token"
    token.write_text("a" * 40)
    os.chmod(token, 0o600)
    socket_path = tmp_path / "herdr.sock"
    assets = tmp_path / "pwa"
    assets.mkdir()
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'socket_path = "{socket_path}"\ntoken_path = "{token}"\nassets_dir = "{assets}"\n'
    )
    cfg = config.load_config(cfg_file)
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.bind_port == 8765
