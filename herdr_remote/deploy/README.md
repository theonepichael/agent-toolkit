# herdr remote — setup

Phone PWA to check in on and drive herdr agents. Topology (verified
2026-09-07, see the grill decision record):

    phone → https://fedora.tail3d556a.ts.net:8443/  (tailscale serve, TLS)
          → Caddy 127.0.0.1:8766 on the Fedora box  (PWA static + /api proxy)
          → bridge 0.0.0.0:8765 on the workstation  (tailnet IP 100.86.58.36)
          → herdr unix socket

The Fedora side gets its own dedicated tailscale-serve HTTPS port (`:8443`)
rather than a subpath of the existing iron-logbook route — a subpath mount
(`--set-path=/herdr`) was tried first and looked plausible as the culprit
during a live debugging session, but the actual bug was Caddy's site key
lacking `bind 127.0.0.1` (see the `.caddyfile` comment); a dedicated port
was simpler to keep once the real fix landed, so that's what's documented
and what's actually running.

Auth: one static bearer token, enforced by the bridge on every /api route.

## One-time: workstation (this machine, WSL2)

```zsh
mkdir -p ~/.config/herdr-bridge
cat > ~/.config/herdr-bridge/config.toml <<'EOF'
socket_path = "$HOME/.config/herdr/herdr.sock"
token_path = "$HOME/.config/herdr-bridge/token"
assets_dir = "$HOME/Workspace/agent-toolkit/herdr_remote/pwa"
bind_host = "0.0.0.0"
bind_port = 8765
EOF
uv run --directory ~/Workspace/agent-toolkit python -m herdr_remote gen-token
# user unit (adjust --directory path to wherever the toolkit lives)
mkdir -p ~/.config/systemd/user
cp herdr_remote/deploy/herdr-remote-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now herdr-remote-bridge.service
```

## One-time: Fedora box (theon@fedora), first deploy only

```zsh
ssh theon@fedora
tailscale serve --bg --https=8443 http://127.0.0.1:8766
tailscale serve status                  # confirm both :443 and :8443 routes listed
```

## Redeploy — run this for ANY change, bridge or PWA

```zsh
herdr_remote/deploy/deploy.sh
```

This is the only supported way to push a change: it rsyncs the PWA static
assets to the Fedora box, installs the Caddy snippet, **and** restarts the
workstation bridge, then polls `/api/health` until the bridge reports a
version and fails loudly (non-zero exit) if it does not come back healthy.
It also prints the bridge's reported version next to the commit it just
deployed, so a mismatch (e.g. uncommitted local changes) is visible
immediately. Never restart the bridge or rsync the PWA assets separately —
running only one half is exactly the bug this script exists to prevent: the
running bridge silently outliving a fix while the deployed frontend (or
vice versa) still runs the old code, with no error anywhere.

## Verify

```zsh
TOKEN=$(cat ~/.config/herdr-bridge/token)
curl -s -H "Authorization: Bearer $TOKEN" localhost:8765/api/agents | head -c 400
curl -s localhost:8765/api/agents                 # → 401
curl -s -H "Authorization: Bearer $TOKEN" https://fedora.tail3d556a.ts.net:8443/api/agents | head -c 400
curl -s https://fedora.tail3d556a.ts.net:8443/ | head -3
```

Then on the phone (Chrome, tailnet connected): open
`https://fedora.tail3d556a.ts.net:8443/`, paste the token, install to home
screen, watch agent statuses, send a prompt.

## Rotate the token

```zsh
# --directory cds into the repo: python -m herdr_remote only resolves with
# the repo root as cwd (package = false; --project alone does not cd there)
uv run --directory ~/Workspace/agent-toolkit python -m herdr_remote gen-token
herdr_remote/deploy/deploy.sh   # restarts the bridge so it picks up the new token
```

Then re-paste the token in the PWA (a 401 clears the stored one).
