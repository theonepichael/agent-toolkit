"""JSON-lines RPC client for the herdr unix socket.

Wire format verified against herdr 0.8.2 on 2026-09-07 (and captured in the
bundled `herdr api schema`): send ``{"id", "method", "params"}\\n``, read one
JSON line back — ``{"id", "result"}`` or
``{"id", "error": {"code", "message"}}``. Herdr closes the connection after
one response, so every call opens a fresh connection.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from pathlib import Path


class HerdrError(RuntimeError):
    """A herdr RPC failure, carrying the server's code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"herdr {code}: {message}")
        self.code = code
        self.message = message


class HerdrClient:
    """One-shot RPC client. Safe to share across tasks: each call opens its
    own connection, matching herdr's one-response-per-connection behavior."""

    def __init__(self, socket_path: Path, timeout_s: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout_s = timeout_s
        self.last_id: str = ""

    async def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        request = {"id": uuid.uuid4().hex[:12], "method": method, "params": params}
        self.last_id = str(request["id"])
        try:
            reader, writer = await asyncio.wait_for(self._connect(), self.timeout_s)
        except (TimeoutError, OSError, FileNotFoundError) as exc:
            raise HerdrError(
                "unreachable", f"cannot connect to {self.socket_path}: {exc}"
            ) from exc
        try:
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), self.timeout_s)
        except TimeoutError as exc:
            raise HerdrError(
                "timeout", f"herdr did not answer `{method}` in time"
            ) from exc
        finally:
            writer.close()
        if not line:
            raise HerdrError("closed", "herdr closed the connection without answering")
        response = json.loads(line.decode())
        if "error" in response:
            err = response["error"]
            raise HerdrError(err.get("code", "unknown"), err.get("message", ""))
        return dict(response.get("result", {}))

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.socket_path))
        return await asyncio.open_connection(sock=sock)
