#!/usr/bin/env bash
# Builds the install.sh test images (one per distro, to cover both the apt
# and dnf branches) and runs the scenario suite against a throwaway
# container for each. Mounts the repo read-only so nothing here can touch
# the real machine or the checked-out working tree.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# distro:dockerfile pairs
DISTROS=(
  "ubuntu:$HERE/Dockerfile"
  "fedora:$HERE/Dockerfile.fedora"
)

OVERALL=0
for entry in "${DISTROS[@]}"; do
  distro="${entry%%:*}"
  dockerfile="${entry#*:}"
  IMAGE="dotfiles-install-test-$distro"

  echo "==> Building test image ($distro)..."
  if ! docker build -q -t "$IMAGE" -f "$dockerfile" "$HERE" >/dev/null; then
    echo "==> $distro: image build failed"
    OVERALL=1
    continue
  fi

  echo "==> Running scenario suite ($distro)..."
  # :ro,Z relabels the bind mount for SELinux (rootless podman on an
  # enforcing host otherwise rejects the mount with a bare "Permission
  # denied" inside the container, even for root — no AVC denial logged, so
  # this is easy to misdiagnose as a UID/GID mapping issue instead). Safe
  # under plain Docker too — the SELinux label flag is a documented bind-mount
  # option there as well, just a no-op without SELinux enforcing.
  if ! docker run --rm \
    -v "$REPO:/dotfiles:ro,Z" \
    -w /home/tester \
    "$IMAGE" \
    bash -c 'cp -r /dotfiles /home/tester/dotfiles && bash /home/tester/dotfiles/test/scenarios.sh'; then
    echo "==> $distro: scenario suite failed"
    OVERALL=1
  fi
done

exit $OVERALL
