#!/usr/bin/env python3
"""Tests for the herdr_remote PWA assets: manifest, install-only SW, wiring.

Pure file checks against the packaged assets — no browser, no network.
Requires Python 3.12+.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402
from herdr_remote import bridge  # noqa: E402

ASSETS = Path(bridge.__file__).parent / "pwa"

REQUIRED = [
    "index.html",
    "app.js",
    "style.css",
    "manifest.webmanifest",
    "sw.js",
    "icon-192.png",
    "icon-512.png",
    "icon-512-maskable.png",
]


@pytest.mark.parametrize("name", REQUIRED)
def test_required_asset_exists(name):
    assert (ASSETS / name).is_file(), f"missing PWA asset: {name}"


def test_manifest_is_installable():
    manifest = json.loads((ASSETS / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    assert manifest["name"]
    assert manifest["short_name"]
    purposes = {icon["purpose"] for icon in manifest["icons"]}
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "maskable" in purposes
    assert "192x192" in sizes and "512x512" in sizes
    # Relative paths so the app works under a path prefix (tailscale serve
    # route is /herdr on the Fedora box).
    assert manifest["start_url"] == "./"
    assert manifest["id"] == "./"
    for icon in manifest["icons"]:
        assert not icon["src"].startswith("/")


def test_service_worker_is_install_only():
    sw = (ASSETS / "sw.js").read_text()
    assert "addEventListener" in sw
    assert "install" in sw
    # Iron-logbook precedent: no fetch handler — installability doesn't need
    # one, and a passthrough would intercept every request for no benefit.
    assert "fetch" not in sw


def test_index_references_relative_assets():
    index = (ASSETS / "index.html").read_text()
    for ref in ("app.js", "style.css", "manifest.webmanifest"):
        assert ref in index
        assert f'"/{ref}' not in index  # no absolute asset paths


def test_index_has_token_entry_and_agent_ui_hooks():
    index = (ASSETS / "index.html").read_text()
    assert "token" in index.lower()
    app_js = (ASSETS / "app.js").read_text()
    # The client must send the bearer header and never put the token in a URL.
    assert "Authorization" in app_js
    assert "Bearer" in app_js
    assert "?token" not in app_js and "&token" not in app_js
    # Fetch-based SSE (EventSource can't set headers).
    assert "EventSource" not in app_js
    assert "getReader" in app_js
    # 401 handling: clear stored token, re-show entry.
    assert "401" in app_js
    assert "localStorage" in app_js
