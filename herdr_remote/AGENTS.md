# AGENTS.md — herdr_remote

<!-- Paired with CLAUDE.md symlink in the same directory -->

## Responsibilities & Boundary

Authenticated HTTP+SSE bridge in front of the herdr socket, serving a phone
PWA for check-in-and-drive control of herdr-managed agents. Full boundary,
topology, and interface writeup — including the grill-recorded auth
decisions — lives in `docs/architecture/herdr-remote.md`; read that first,
this file only adds the two hazards below.

## Hazards & Signposts

- `config.py` refuses to load a world-readable token file (grill decision
  `auth-model`: 32+ bytes, mode 600, owner-checked) — a perms regression
  fails loudly instead of silently publishing agent control.
- `bridge.py` is the single auth-enforcement point (grill decision
  `auth-enforcement-point`): every non-static route checks the bearer token
  itself, the reverse proxy checks nothing. A new route must check the
  token itself too — never assume the proxy already covered it.

## Local Conventions

Tests use `aiohttp`'s test utilities against the real app factory, not a
mocked bridge. See `test/AGENTS.md` for the repo's general sandboxed-`HOME`
test discipline, which applies here too.
