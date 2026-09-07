#!/usr/bin/env zsh
# Deploy the herdr remote PWA + Caddy config to the Fedora box (theon@fedora)
# AND restart the workstation bridge — always both halves together, in one
# command, so the two can never silently drift apart (a bridge-only restart
# or a Fedora-only asset sync used to be able to leave the other stale, with
# no visible signal until a live 502/empty-output symptom showed up).
#
# One-time (manual, see README.md): tailscale serve route + first token gen.

set -euo pipefail

HOST=theon@fedora
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ASSETS="$REPO_ROOT/herdr_remote/pwa"
CADDY_SNIPPET="$REPO_ROOT/herdr_remote/deploy/herdr-remote.fedora.caddyfile"
BRIDGE_HEALTH_URL="http://127.0.0.1:$(grep -oE 'bind_port *= *[0-9]+' ~/.config/herdr-bridge/config.toml | grep -oE '[0-9]+')/api/health"
BRIDGE_TOKEN="$(cat ~/.config/herdr-bridge/token)"
DEPLOY_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

rsync -a --delete "$ASSETS"/ "$HOST":/var/www/herdr-remote/
scp -q "$CADDY_SNIPPET" "$HOST":/tmp/herdr-remote.caddyfile
ssh "$HOST" 'sudo install -m 644 /tmp/herdr-remote.caddyfile /etc/caddy/Caddyfile.d/herdr-remote.caddyfile && rm /tmp/herdr-remote.caddyfile && sudo caddy validate --config /etc/caddy/Caddyfile >/dev/null && sudo systemctl reload caddy'
echo "deployed: PWA + Caddy config on $HOST (from commit $DEPLOY_SHA)"

systemctl --user restart herdr-remote-bridge.service
BRIDGE_VERSION=""
for _ in $(seq 1 10); do
	sleep 1
	BRIDGE_VERSION="$(curl -fsS -H "Authorization: Bearer $BRIDGE_TOKEN" "$BRIDGE_HEALTH_URL" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])' 2>/dev/null || true)"
	[[ -n "$BRIDGE_VERSION" ]] && break
done

if [[ -z "$BRIDGE_VERSION" ]]; then
	echo "error: workstation bridge did not come back healthy after restart" >&2
	exit 1
fi

echo "workstation bridge restarted: reporting version $BRIDGE_VERSION"
if [[ "$BRIDGE_VERSION" != "$DEPLOY_SHA" ]]; then
	echo "warning: bridge version ($BRIDGE_VERSION) != deployed commit ($DEPLOY_SHA) — uncommitted local changes?" >&2
fi
