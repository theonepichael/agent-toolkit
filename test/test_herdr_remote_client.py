#!/usr/bin/env python3
"""Tests for herdr_remote.herdr_client: JSON-line RPC over the unix socket.

Runs against the in-process FakeHerdrServer — never the real herdr socket.
Requires Python 3.12+.
"""

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from herdr_remote.herdr_client import HerdrClient, HerdrError  # noqa: E402
from test.fake_herdr import FakeHerdrServer, sample_agents  # noqa: E402


@pytest.fixture()
async def fake_server(tmp_path):
    server = FakeHerdrServer(tmp_path / "herdr.sock", sample_agents())
    await server.start()
    yield server
    await server.stop()


async def test_call_sends_envelope_and_parses_result(fake_server):
    client = HerdrClient(fake_server.socket_path)
    result = await client.call("agent.list", {})
    assert result["type"] == "agent_list"
    pi_names = [a["name"] for a in result["agents"] if a["agent"] == "pi"]
    assert pi_names == ["worker-one", "worker-two"]
    assert fake_server.requests == [
        {"id": client.last_id, "method": "agent.list", "params": {}}
    ]


async def test_call_maps_error_response_to_herdr_error(fake_server):
    client = HerdrClient(fake_server.socket_path)
    with pytest.raises(HerdrError) as excinfo:
        await client.call("agent.read", {"target": "worker-one", "source": "bogus"})
    assert excinfo.value.code == "invalid_request"
    assert "bogus" in excinfo.value.message


async def test_read_returns_recent_output(fake_server):
    client = HerdrClient(fake_server.socket_path)
    result = await client.call(
        "agent.read", {"target": "worker-one", "source": "recent"}
    )
    assert result["read"]["text"] == "output of worker-one"


async def test_read_unknown_agent_is_not_found(fake_server):
    client = HerdrClient(fake_server.socket_path)
    with pytest.raises(HerdrError) as excinfo:
        await client.call("agent.read", {"target": "ghost", "source": "recent"})
    assert excinfo.value.code == "not_found"


async def test_prompt_busy_agent_surfaces_herdr_error(fake_server):
    client = HerdrClient(fake_server.socket_path)
    with pytest.raises(HerdrError) as excinfo:
        await client.call("agent.prompt", {"target": "worker-one", "text": "hi"})
    assert excinfo.value.code == "agent_busy"


async def test_prompt_idle_agent_ok(fake_server):
    client = HerdrClient(fake_server.socket_path)
    result = await client.call("agent.prompt", {"target": "worker-two", "text": "hi"})
    assert result["ok"] is True


async def test_wait_returns_agent_info_on_status_match(fake_server):
    client = HerdrClient(fake_server.socket_path)
    result = await client.call(
        "agent.wait", {"target": "worker-two", "until": ["idle"], "timeout_ms": 100}
    )
    assert result["type"] == "agent_info"
    assert result["agent"]["name"] == "worker-two"


async def test_wait_returns_timeout_result_when_no_change(fake_server):
    client = HerdrClient(fake_server.socket_path)
    result = await client.call(
        "agent.wait", {"target": "worker-one", "until": ["idle"], "timeout_ms": 50}
    )
    assert result["type"] == "timeout"


async def test_connection_refused_raises_herdr_error(tmp_path):
    client = HerdrClient(tmp_path / "absent.sock")
    with pytest.raises(HerdrError):
        await asyncio.wait_for(client.call("agent.list", {}), timeout=5)


async def test_each_call_opens_a_fresh_connection(fake_server):
    client = HerdrClient(fake_server.socket_path)
    await client.call("agent.list", {})
    await client.call("agent.list", {})
    assert len(fake_server.requests) == 2
