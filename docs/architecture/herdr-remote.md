# Architecture: `herdr_remote` — herdr remote-control bridge + PWA

## Boundary and responsibility

`herdr_remote` is the only component that talks HTTP for herdr control. It
sits between the herdr Unix socket (`~/.config/herdr/herdr.sock`, JSON-lines
RPC, one request per connection) and a phone: an `aiohttp` bridge daemon on
the workstation exposes a small authenticated HTTP+SSE API plus the static
PWA assets; a reverse proxy on the Fedora box (Caddy, fronted by
`tailscale serve` for TLS/secure-context) reaches the bridge over the
tailnet. The bridge is the single auth enforcement point — every non-static
route checks the bearer token itself, so both entry paths (via Caddy or
direct to the bridge's tailnet IP) are covered by one check.

## Interfaces

Exposes (HTTP, all JSON):

- `GET /api/agents` — live agent list (name, agent, status, tab/pane, cwd,
  title) derived from herdr `agent.list`.
- `GET /api/agents/{name}/read?lines=N` — recent terminal output (herdr
  `agent.read`, source `recent`).
- `POST /api/agents/{name}/prompt` — submit a prompt (herdr `agent.prompt`,
  no wait).
- `GET /api/events` — SSE: full-snapshot `state` events on any agent status
  change (backed by per-agent `agent.wait` long-polls), 15 s keep-alive
  comments, flush per write.
- `GET /api/health`, static PWA at `/` (unauthenticated by design).

Consumes: herdr RPC methods `session.snapshot`, `agent.list/get/read/
prompt/wait/send_keys` — and only those; `server.stop`/`server.live_handoff`
are never sent.

## Non-goals

- No full terminal/attach experience — check-in-and-drive only (send a
  prompt, watch status, read recent output as text).
- No multi-user auth, no per-device tokens, no OIDC — single bearer token,
  single user.
- No TLS in the bridge (WireGuard-encrypted tailnet hop; phone-facing TLS is
  tailscale serve's job).
- No iOS/Safari support (Android/Chrome target, iron-logbook precedent).
- No writes to herdr state beyond prompts/send-keys driven by the user.

## Known unknowns / deferred

- herdr's exact error shape when prompting a busy/blocked agent — mapped
  conservatively (409/502) at implementation time, against the live server.
- Whether `agent.wait` per connected SSE client (cap 8) stays cheap enough
  at real agent counts; revisit to a single shared poller if not.
- Phone-side install checklist is manual and user-run (mirrors iron-logbook
  step 8); first run doubles as its verification.
