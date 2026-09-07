#!/usr/bin/env python3
"""Tests for herdr_remote.bridge: routes, bearer auth, SSE lifecycle, static.

Runs the aiohttp app against the in-process FakeHerdrServer — never the real
herdr socket or real ~/.config/herdr-bridge/. Requires Python 3.12+.
"""

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402
from aiohttp import ClientResponse  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from herdr_remote import bridge  # noqa: E402
from herdr_remote.config import Config  # noqa: E402
from herdr_remote.herdr_client import HerdrClient  # noqa: E402
from test.fake_herdr import FakeHerdrServer, sample_agents  # noqa: E402

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def assets_dir(tmp_path):
    """Minimal PWA assets so the static routes have something to serve."""
    assets = tmp_path / "pwa"
    assets.mkdir()
    (assets / "index.html").write_text("<html><body>pwa</body></html>")
    (assets / "app.js").write_text("// app")
    return assets


@pytest.fixture()
def config(tmp_path, assets_dir):
    token_path = tmp_path / "token"
    token_path.write_text(TOKEN)
    import os

    os.chmod(token_path, 0o600)
    return Config(
        socket_path=tmp_path / "herdr.sock",
        token_path=tmp_path / "token",
        assets_dir=assets_dir,
        bind_host="0.0.0.0",
        bind_port=8765,
        herdr_wait_timeout_ms=300,
        keepalive_interval_s=1,
        sse_client_cap=8,
    )


@pytest.fixture()
async def fake_server(tmp_path):
    server = FakeHerdrServer(tmp_path / "herdr.sock", sample_agents())
    await server.start()
    yield server
    await server.stop()


@pytest.fixture()
async def client(config, fake_server):
    app = bridge.create_app(config, HerdrClient(config.socket_path))
    server = TestServer(app)
    tc = TestClient(server)
    await tc.start_server()
    yield tc
    await tc.close()


async def test_api_routes_reject_missing_token(client):
    for path, method in [
        ("/api/agents", "get"),
        ("/api/health", "get"),
        ("/api/agents/worker-one/read", "get"),
    ]:
        resp = await client.request(method, path)
        assert resp.status == 401, path


async def test_api_routes_reject_wrong_token(client):
    resp = await client.get("/api/agents", headers={"Authorization": "Bearer wrong"})
    assert resp.status == 401


async def test_unknown_api_path_is_authed_too(client):
    resp = await client.get("/api/nope")
    assert resp.status == 401
    resp = await client.get("/api/nope", headers=AUTH)
    assert resp.status == 404


async def test_static_assets_served_without_auth(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "pwa" in await resp.text()
    resp = await client.get("/app.js")
    assert resp.status == 200


async def test_agents_lists_live_herdr_state(client):
    resp = await client.get("/api/agents", headers=AUTH)
    assert resp.status == 200
    body = await resp.json()
    names = [a["name"] for a in body["agents"]]
    assert names == ["worker-one", "worker-two"]
    assert body["agents"][0]["agent_status"] == "working"


async def test_agent_read_returns_output(client):
    resp = await client.get("/api/agents/worker-one/read", headers=AUTH)
    assert resp.status == 200
    body = await resp.json()
    assert "output of worker-one" in body["output"]


async def test_agent_read_unknown_agent_404(client):
    resp = await client.get("/api/agents/ghost/read", headers=AUTH)
    assert resp.status == 404


async def test_prompt_ok_and_empty_text_rejected(client):
    resp = await client.post(
        "/api/agents/worker-two/prompt", headers=AUTH, json={"text": "do a thing"}
    )
    assert resp.status == 200
    assert (await resp.json())["ok"] is True

    resp = await client.post(
        "/api/agents/worker-two/prompt", headers=AUTH, json={"text": ""}
    )
    assert resp.status == 400

    resp = await client.post(
        "/api/agents/worker-one/prompt", headers=AUTH, json={"text": "busy"}
    )
    assert resp.status == 409


async def test_health_reports_herdr_reachable(client):
    resp = await client.get("/api/health", headers=AUTH)
    assert resp.status == 200
    assert (await resp.json())["herdr"] is True


async def _next_sse_event(
    resp: ClientResponse, timeout: float = 5
) -> tuple[str, dict | None]:
    """Read one SSE event (event: X / data: {...}) from an open stream."""
    event_name = None
    while True:
        raw = await asyncio.wait_for(resp.content.readline(), timeout)
        line = raw.decode().rstrip("\n")
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            return event_name or "message", json.loads(line.split(":", 1)[1])
        elif line == "":
            if event_name is not None:
                return event_name, None


async def test_sse_initial_state_then_change_then_keepalive(client, fake_server):
    resp = await client.get("/api/events", headers=AUTH)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"

    name, data = await _next_sse_event(resp)
    assert name == "state"
    assert [a["name"] for a in data["agents"]] == ["worker-one", "worker-two"]

    # A status change on the fake herdr must produce a fresh state event.
    fake_server.set_status("worker-one", "idle")
    name, data = await _next_sse_event(resp)
    assert name == "state"
    assert data["agents"][0]["agent_status"] == "idle"

    # Keep-alive comments arrive between events (interval = 1s in fixture).
    while True:
        raw = await asyncio.wait_for(resp.content.readline(), 5)
        line = raw.decode().strip()
        if line.startswith(":"):
            break
    resp.close()


async def test_sse_rejects_unauthenticated(client):
    resp = await client.get("/api/events")
    assert resp.status == 401


async def test_sse_client_cap(client, config):
    responses = []
    for _ in range(config.sse_client_cap + 1):
        resp = await client.get("/api/events", headers=AUTH)
        if resp.status == 503:
            resp.close()
            break
        responses.append(resp)
    else:
        raise AssertionError("cap not enforced")
    assert len(responses) == config.sse_client_cap
    for resp in responses:
        resp.close()
