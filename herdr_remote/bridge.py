"""The bridge: aiohttp app exposing herdr as an authenticated HTTP+SSE API.

Auth model (grill decision `auth-enforcement-point`): every non-static route
checks the bearer token itself — the reverse proxy does none — so both entry
paths (via Caddy on the Fedora box, or direct over the tailnet) hit exactly
one enforcement point. 401s are logged with the source IP; the token never
appears in logs.

SSE model (grill decision `pwa-token-handling`): one shared poller runs
per-agent herdr `agent.wait` long-polls; any status change re-broadcasts a
full `state` event (snapshot refetch semantics — always correct, no deltas).
`: keep-alive` comments every `keepalive_interval_s`; every write is flushed
explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Handler

from .config import Config, load_token
from .herdr_client import HerdrClient, HerdrError

log = logging.getLogger("herdr_remote.bridge")

STATIC_PREFIX = ""  # assets are served at "/" — the API owns "/api/*"


def _summarize(agent: dict[str, Any]) -> dict[str, Any]:
    """Project a herdr agent record onto the fields the PWA renders.

    `id` is the routing/RPC-target key and must be unique per agent instance.
    herdr only gives pi agents a real `name` — claude agents have none, so
    falling back to `name` (like `agent`, the display type) collides whenever
    more than one claude session is running, which is the common case, not
    an edge case. `pane_id` is unique per agent instance for every agent
    type and is a valid herdr RPC target (verified against a live socket).
    """
    return {
        "id": agent.get("pane_id") or agent.get("tab_id") or agent.get("name") or "",
        "name": agent.get("name") or agent.get("agent") or "",
        "agent": agent.get("agent", ""),
        "agent_status": agent.get("agent_status", "unknown"),
        "tab_id": agent.get("tab_id", ""),
        "pane_id": agent.get("pane_id", ""),
        "cwd": agent.get("cwd", ""),
        "title": agent.get("terminal_title_stripped")
        or agent.get("terminal_title", ""),
        "state_change_seq": agent.get("state_change_seq", 0),
    }


class StatePoller:
    """Shared poller: per-agent `agent.wait` long-polls, broadcasts on change."""

    def __init__(self, herdr: HerdrClient, timeout_ms: int) -> None:
        self.herdr = herdr
        self.timeout_ms = timeout_ms
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def agents_snapshot(self) -> list[dict[str, Any]]:
        result = await self.herdr.call("agent.list", {})
        return [_summarize(a) for a in result.get("agents", [])]

    async def _run(self) -> None:
        last_seq: tuple | None = None
        while True:
            try:
                agents = await self.agents_snapshot()
                seq = tuple(a["state_change_seq"] for a in agents) + (len(agents),)
                if self.subscribers and seq != last_seq:
                    await self._broadcast({"agents": agents})
                    last_seq = seq
                if self.subscribers:
                    await asyncio.gather(*(self._wait_one(a) for a in agents))
                else:
                    # Nobody is listening — idle until a client connects.
                    await asyncio.sleep(1)
            except HerdrError as exc:
                log.warning("herdr poller: %s", exc)
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise

    async def _wait_one(self, agent: dict[str, Any]) -> None:
        # Long-poll for a CHANGE: ask herdr to wake us when the agent reaches
        # any status other than its current one. Passing the current status
        # itself would return immediately (it is already there) and spin the
        # poller. On return — change or timeout — the loop re-snapshots and
        # re-checks reality.
        statuses = ["idle", "working", "blocked", "done", "unknown"]
        current = agent.get("agent_status", "unknown")
        until = [s for s in statuses if s != current] or statuses
        with contextlib.suppress(HerdrError):
            await self.herdr.call(
                "agent.wait",
                {
                    "target": agent["id"],
                    "until": until,
                    "timeout_ms": self.timeout_ms,
                },
            )


def create_app(config: Config, herdr: HerdrClient) -> web.Application:
    token = load_token(config.token_path)

    async def check_auth(request: web.Request) -> web.StreamResponse | None:
        if request.path.startswith("/api/"):
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {token}":
                log.warning("401 from %s for %s", request.remote, request.path)
                raise web.HTTPUnauthorized(text="unauthorized")
        return None

    @web.middleware
    async def auth_middleware(
        request: web.Request,
        handler: Handler,
    ) -> web.StreamResponse:
        await check_auth(request)
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware])
    app["config"] = config
    poller = StatePoller(herdr, config.herdr_wait_timeout_ms)
    app["poller"] = poller

    async def on_startup(app: web.Application) -> None:
        poller.start()

    async def on_cleanup(app: web.Application) -> None:
        await poller.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    async def agents(request: web.Request) -> web.Response:
        try:
            return web.json_response({"agents": await poller.agents_snapshot()})
        except HerdrError as exc:
            return web.json_response({"error": exc.message}, status=502)

    async def agent_read(request: web.Request) -> web.Response:
        agent_id = request.match_info["id"]
        try:
            lines = int(request.query.get("lines", config.read_lines_default))
        except ValueError:
            return web.json_response({"error": "lines must be an integer"}, status=400)
        lines = max(1, min(lines, config.read_lines_max))
        try:
            result = await herdr.call(
                "agent.read", {"target": agent_id, "source": "recent", "lines": lines}
            )
        except HerdrError as exc:
            status = 404 if exc.code == "not_found" else 502
            return web.json_response({"error": exc.message}, status=status)
        # herdr nests the text under result.read.text, not result.text.
        text = result.get("read", {}).get("text", "")
        return web.json_response({"output": text})

    async def agent_prompt(request: web.Request) -> web.Response:
        agent_id = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed body is a client error
            return web.json_response({"error": "body must be JSON"}, status=400)
        text = body.get("text", "") if isinstance(body, dict) else ""
        if not isinstance(text, str) or not text.strip():
            return web.json_response(
                {"error": "text must be a non-empty string"}, status=400
            )
        try:
            await herdr.call("agent.prompt", {"target": agent_id, "text": text})
        except HerdrError as exc:
            status = (
                404
                if exc.code == "not_found"
                else 409
                if exc.code == "agent_busy"
                else 502
            )
            return web.json_response({"error": exc.message}, status=status)
        return web.json_response({"ok": True})

    async def health(request: web.Request) -> web.Response:
        try:
            await herdr.call("ping", {})
            herdr_ok = True
        except HerdrError:
            herdr_ok = False
        return web.json_response({"ok": True, "herdr": herdr_ok})

    async def events(request: web.Request) -> web.StreamResponse:
        if len(poller.subscribers) >= config.sse_client_cap:
            return web.json_response({"error": "too many event streams"}, status=503)
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)
        queue = poller.subscribe()

        async def write_state(payload: dict[str, Any]) -> None:
            await _write(resp, f"event: state\ndata: {json.dumps(payload)}\n\n")

        try:
            # Initial state on connect — never make the client wait for the
            # poller's current long-poll round to finish.
            await write_state({"agents": await poller.agents_snapshot()})
            while True:
                timeout = config.keepalive_interval_s
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout)
                    await write_state(payload)
                except TimeoutError:
                    await _write(resp, ": keep-alive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            poller.unsubscribe(queue)
        return resp

    async def _write(resp: web.StreamResponse, chunk: str) -> None:
        await resp.write(chunk.encode())
        # Explicit flush per write: mobile NAT middleboxes and the reverse
        # proxy both behave better when nothing batches server-side.
        await resp.write(b"")

    app.router.add_get("/api/agents", agents)
    app.router.add_get("/api/agents/{id}/read", agent_read)
    app.router.add_post("/api/agents/{id}/prompt", agent_prompt)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/events", events)

    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(config.assets_dir / "index.html")

    app.router.add_get("/", index)
    app.router.add_static(STATIC_PREFIX or "/", config.assets_dir)
    return app
