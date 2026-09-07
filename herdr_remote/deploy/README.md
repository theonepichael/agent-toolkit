# herdr remote — setup

Phone PWA to check in on and drive herdr agents. Topology (verified
2026-09-07, see the grill decision record):

    phone → https://fedora.tail3d556a.ts.net/herdr  (tailscale serve, TLS)
          → Caddy 127.0.0.1:8766 on the Fedora box  (PWA static + /api proxy)
          → bridge 0.0.0.0:8765 on the workstation  (tailnet IP 100.86.58.36)
          → herdr unix socket

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

## One-time: Fedora box (theon@fedora)

```zsh
herdr_remote/deploy/deploy.sh          # PWA static + Caddyfile.d snippet
ssh theon@fedora
# front it with TLS (keep the existing iron-logbook / route untouched):
tailscale serve --bg --set-path=/herdr http://127.0.0.1:8766
tailscale serve status                  # confirm both routes listed
```

## Verify

```zsh
TOKEN=$(cat ~/.config/herdr-bridge/token)
curl -s -H "Authorization: Bearer $TOKEN" localhost:8765/api/agents | head -c 400
curl -s localhost:8765/api/agents                 # → 401
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/api/agents  # from the fedora box? run on fedora:
#   ssh theon@fedora 'curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8766/api/agents'
curl -s https://fedora.tail3d556a.ts.net/herdr/ | head -3
```

Then on the phone (Chrome, tailnet connected): open the ts.net URL, paste the
token, install to home screen, watch agent statuses, send a prompt.

## Rotate the token

```zsh
# --directory cds into the repo: python -m herdr_remote only resolves with
# the repo root as cwd (package = false; --project alone does not cd there)
uv run --directory ~/Workspace/agent-toolkit python -m herdr_remote gen-token
systemctl --user restart herdr-remote-bridge.service
```

Then re-paste the token in the PWA (a 401 clears the stored one).
