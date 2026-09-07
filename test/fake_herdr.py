#!/usr/bin/env python3
"""In-process fake herdr server for herdr_remote tests.

Speaks the real wire protocol over a throwaway unix socket: JSON-lines
requests ``{"id", "method", "params"}``, one response per connection, replies
``{"id", "result"}`` or ``{"id": "", "error": {code, message}}``. Never
touches the real herdr socket.

Requires Python 3.12+.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class FakeHerdrServer:
    """Minimal herdr RPC server with programmable agent state."""

    def __init__(self, socket_path: Path, agents: list[dict[str, Any]]) -> None:
        self.socket_path = socket_path
        self.agents = agents
        self.requests: list[dict[str, Any]] = []
        self._server: asyncio.Server | None = None
        # Test hook: when set, agent.wait returns as soon as this event fires
        # (status changed), instead of waiting out the timeout.
        self.status_changed = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle, str(self.socket_path)
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def set_status(self, name: str, status: str) -> None:
        for agent in self.agents:
            if agent["name"] == name:
                agent["agent_status"] = status
                agent["state_change_seq"] += 1
        self.status_changed.set()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            req = json.loads(line.decode())
            self.requests.append(req)
            resp = await self._dispatch(req)
        except Exception as exc:  # noqa: BLE001 — fake server, report anything
            resp = {"id": "", "error": {"code": "internal", "message": str(exc)}}
        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()
        writer.close()

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        method = req["method"]
        params = req.get("params", {})
        if method == "ping":
            return {"id": req.get("id", ""), "result": {"type": "pong"}}
        if method == "agent.list":
            return {
                "id": req.get("id", ""),
                "result": {"type": "agent_list", "agents": list(self.agents)},
            }
        if method == "session.snapshot":
            return {
                "id": req.get("id", ""),
                "result": {
                    "type": "session_snapshot",
                    "snapshot": {"agents": list(self.agents)},
                },
            }
        if method == "agent.read":
            if params.get("source") not in (
                "visible",
                "recent",
                "recent_unwrapped",
                "detection",
            ):
                return {
                    "id": "",
                    "error": {
                        "code": "invalid_request",
                        "message": f"unknown variant `{params.get('source')}`",
                    },
                }
            agent = self._find(params["target"])
            if agent is None:
                return {
                    "id": "",
                    "error": {
                        "code": "not_found",
                        "message": f"no agent `{params['target']}`",
                    },
                }
            return {
                "id": req.get("id", ""),
                "result": {
                    "type": "agent_read",
                    "text": f"output of {params['target']}",
                },
            }
        if method == "agent.prompt":
            agent = self._find(params["target"])
            if agent is None:
                return {
                    "id": "",
                    "error": {
                        "code": "not_found",
                        "message": f"no agent `{params['target']}`",
                    },
                }
            if agent["agent_status"] == "working":
                return {
                    "id": "",
                    "error": {
                        "code": "agent_busy",
                        "message": f"agent `{params['target']}` is working",
                    },
                }
            return {"id": req.get("id", ""), "result": {"type": "prompted", "ok": True}}
        if method == "agent.wait":
            until = params.get("until", [])
            timeout_ms = params.get("timeout_ms", 60000)
            agent = self._find(params["target"])
            if agent is None:
                return {
                    "id": "",
                    "error": {
                        "code": "not_found",
                        "message": f"no agent `{params['target']}`",
                    },
                }
            if agent["agent_status"] in until:
                return {
                    "id": req.get("id", ""),
                    "result": {"type": "agent_info", "agent": dict(agent)},
                }
            changed = await self._wait_changed(min(timeout_ms, 2000) / 1000)
            if changed:
                return {
                    "id": req.get("id", ""),
                    "result": {"type": "agent_info", "agent": dict(agent)},
                }
            return {
                "id": req.get("id", ""),
                "result": {"type": "timeout", "agent": dict(agent)},
            }
        return {
            "id": "",
            "error": {
                "code": "invalid_request",
                "message": f"unknown method `{method}`",
            },
        }

    def _find(self, target: str) -> dict[str, Any] | None:
        for agent in self.agents:
            if agent["name"] == target:
                return agent
        return None

    async def _wait_changed(self, timeout: float) -> bool:
        self.status_changed.clear()
        try:
            await asyncio.wait_for(self.status_changed.wait(), timeout)
            return True
        except TimeoutError:
            return False


def sample_agents() -> list[dict[str, Any]]:
    return [
        {
            "name": "worker-one",
            "agent": "pi",
            "agent_status": "working",
            "tab_id": "w2:t3",
            "pane_id": "w2:p8",
            "cwd": "/home/yanil/Workspace",
            "terminal_title": "π - Workspace",
            "state_change_seq": 1,
        },
        {
            "name": "worker-two",
            "agent": "pi",
            "agent_status": "idle",
            "tab_id": "w2:t8",
            "pane_id": "w2:pD",
            "cwd": "/home/yanil/Workspace",
            "terminal_title": "π - Workspace",
            "state_change_seq": 2,
        },
    ]
