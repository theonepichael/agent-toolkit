#!/usr/bin/env zsh
# Deploy the herdr remote PWA + Caddy config to the Fedora box (theon@fedora).
# Mirrors the iron-logbook deploy shape. The bridge itself runs on the
# workstation (systemd user unit — see herdr-remote-bridge.service).
#
# One-time (manual, see README.md): tailscale serve route + first token gen.

set -euo pipefail

HOST=theon@fedora
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ASSETS="$REPO_ROOT/herdr_remote/pwa"
CADDY_SNIPPET="$REPO_ROOT/herdr_remote/deploy/herdr-remote.fedora.caddy"

rsync -a --delete "$ASSETS"/ "$HOST":/var/www/herdr-remote/
scp -q "$CADDY_SNIPPET" "$HOST":/tmp/herdr-remote.caddy
ssh "$HOST" 'sudo install -m 644 /tmp/herdr-remote.caddy /etc/caddy/Caddyfile.d/herdr-remote.caddy && rm /tmp/herdr-remote.caddy && sudo caddy validate --config /etc/caddy/Caddyfile >/dev/null && sudo systemctl reload caddy'
echo "deployed: PWA + Caddy config on $HOST"
